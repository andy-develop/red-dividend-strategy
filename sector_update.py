#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 行业轮动策略 · 每日更新（与红利低波流水线同仓、同工程规范）

数据链路（纯 HTTP，无 akshare，GitHub Actions 可用）：
  1) 21 行业 × 30 只 ETF 日线：东方财富 push2his（前复权收盘 / 成交额 / 换手率）
  2) 沪深300 全收益 H00300 + 价格 000300：中证指数官网 index-perf（与红利低波同源）
→ 硬校验（有效行业占比 ≥ MIN_VALID_RATIO，不足直接红）→ 统一引擎 sector_engine.py
  （因子 / 回测 / 风控 / 快照，与单测共用同一实现）
→ 生成 sector payload（排行 / 持仓 / 风控 / 回测 / ETF 映射 / 口径披露）
→ 与现有 index.html 中的红利低波 payload 合并注入（carry-forward，互不覆盖）
→ 原始响应与当日快照归档 data/（可复现、可审计）

增量更新（v1.1，避免每次全量重抓）：
  基线 = 最近一次全量周归档 data/sector-week-<date>.json.gz + 其后每日增量
        data/sector-incr-<date>.json.gz（重建为完整 raw，确定性）
  每次抓取只请求 beg=基线末日+1 的增量区间（每 ETF ~几十行，而非全量几千行）：
    - 前复权刻度检测：增量首日与基线末日收盘价跳变 >11% → 期间除权、历史刻度失效 → 该 ETF 全量兜底
    - 缺口检测：增量首日与基线末日间隔 >10 自然日 → 该 ETF 全量兜底
    - 基线陈旧 > FULL_EVERY_DAYS(7) 天或无基线 → 全量重抓（周基线轮换）
    - SECTOR_QUICK=1（CI）：增量请求失败直接沿用基线行（发布不中断），由 workflow 归档回退双保险
  归档治理：incr 保留最近 30 个、week 保留最近 2 个，其余本地删除（git 树体积可控）；
  旧格式 data/sector-raw-<date>.json.gz 作为一次性迁移基线自动兼容。

用法: python3 sector_update.py [--out index.html] [--data-out sector_data.json] [--dry-run]
                     [--raw data/sector-week-....json.gz] [--fetch-only /tmp/raw.json]
"""
import json, os, sys, datetime, gzip, time, glob
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import sector_universe as U
import sector_engine as E
import payload_util

_ARGS = sys.argv[1:]
_OUT = "index.html"; _DATA_OUT = "sector_data.json"; _DRY = False; _RAW = None; _FETCH_ONLY = None
while _ARGS:
    a = _ARGS.pop(0)
    if a == "--out" and _ARGS:
        _OUT = _ARGS.pop(0)
    elif a == "--data-out" and _ARGS:
        _DATA_OUT = _ARGS.pop(0)
    elif a == "--raw" and _ARGS:
        _RAW = _ARGS.pop(0)   # 从归档原始数据重跑（跳过抓取），便于本地复现/验证
    elif a == "--fetch-only" and _ARGS:
        _FETCH_ONLY = _ARGS.pop(0)   # 只抓取+校验，写原始 JSON 到指定路径（CI 一次抓取三跑共用）
    elif a == "--dry-run":
        _DRY = True   # 不覆盖仓库产物、不归档、不携带线上段（幂等断言用）
OUT = os.path.join(BASE, _OUT)
DATA_OUT = os.path.join(BASE, _DATA_OUT)
ARCHIVE_DIR = os.path.join(BASE, "data")

FETCH_WORKERS = 3      # ETF 并发抓取（东财限流明显；全局限流器 + 指数退避见 sector_universe）
FETCH_RETRIES = 6      # 单只 ETF 失败重试次数（含退避 2s×(k+1)）
FETCH_RETRIES_QUICK = 2   # SECTOR_QUICK=1（CI）：封锁时快速失败（2 次重试 + 20s 超时），由基线回退兜底
FULL_EVERY_DAYS = 7    # 基线陈旧超过该天数 → 全量重抓（周基线轮换）
KEEP_INCR = 30         # 增量归档保留个数
KEEP_WEEK = 2          # 全量周归档保留个数


def bj_now():
    """北京时间（UTC+8）时间戳。dry-run 固定值保证两次生成逐位一致（幂等断言）。"""
    if _DRY:
        return "dry-run"
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M")


# ============ 增量归档：基线重建 / 合并（纯函数，单测覆盖） ============

def _kl_date(row):
    """klines 行首列日期（YYYY-MM-DD）。"""
    return row.split(",")[0]


def merge_klines(old, new_rows):
    """按日期合并 klines 行（新行覆盖同日旧行），升序返回。"""
    d = {_kl_date(r): r for r in old}
    for r in new_rows:
        d[_kl_date(r)] = r
    return [d[k] for k in sorted(d)]


def kline_gap_days(old, new_rows):
    """增量首日与基线末日间隔（自然日）；无任一侧返回 0。"""
    if not old or not new_rows:
        return 0
    last_old = datetime.date.fromisoformat(_kl_date(old[-1]))
    first_new = datetime.date.fromisoformat(_kl_date(new_rows[0]))
    return (first_new - last_old).days


def kline_scale_jump(old, new_rows, tol=0.11):
    """前复权刻度检测：基线末日与增量首日收盘价跳变 ≥ tol → 期间除权、历史刻度已失效，需全量兜底。
    （fqt=1 前复权以最新价为基准回溯调整，除权后旧基线整体刻度过期，增量拼接会产生价格断层）"""
    if not old or not new_rows:
        return False
    p_old = float(old[-1].split(",")[2])
    p_new = float(new_rows[0].split(",")[2])
    return p_old > 0 and abs(p_new / p_old - 1.0) >= tol


def merge_csi(old, new_rows):
    """按 tradeDate 合并 csi rows（新覆盖旧），升序返回。"""
    d = {r["tradeDate"]: r for r in old}
    for r in new_rows:
        d[r["tradeDate"]] = r
    return [d[k] for k in sorted(d)]


def load_raw(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fdate(name, prefix):
    """从归档文件名解析日期。如 sector-week-20260910.json.gz -> date(2026,9,10)。"""
    return datetime.datetime.strptime(os.path.basename(name).split(prefix)[1].split(".")[0], "%Y%m%d").date()


def rebuild_base():
    """重建最近完整基线：最新周归档 + 其后每日增量（确定性合并）。
    兼容旧格式 data/sector-raw-<date>.json.gz（一次性迁移基线，增量照常）。
    返回 (raw, base_day, mode)；mode ∈ full | incr | migrate | none。"""
    weeks = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-week-*.json.gz")))
    if weeks:
        p = weeks[-1]
        day = _fdate(p, "sector-week-")
        raw = load_raw(p)
        for incr in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-incr-*.json.gz"))):
            iday = _fdate(incr, "sector-incr-")
            if iday <= day:
                continue
            d = load_raw(incr)
            for code, rows in d.get("etfs", {}).items():
                if code not in raw["etfs"]:
                    raw["etfs"][code] = {"industry": "", "name": "", "klines": []}
                old = raw["etfs"][code].get("klines", [])
                raw["etfs"][code]["klines"] = merge_klines(old, rows)
            for c, rows in d.get("csi", {}).items():
                raw["csi"][c] = merge_csi(raw.get("csi", {}).get(c, []), rows)
            day = max(day, iday)
        return raw, day, "incr"
    old = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-raw-*.json.gz")))
    if old:
        return load_raw(old[-1]), _fdate(old[-1], "sector-raw-"), "migrate"
    return None, None, "none"


def _fetch_retries():
    return FETCH_RETRIES_QUICK if os.environ.get("SECTOR_QUICK") else FETCH_RETRIES


def _grab_em(raw, codes, start, incr=None, base_raw=None):
    """并发抓取一批 ETF（增量区间或全量），并入 raw；失败记录 failures。
    incr 非空时同时记录增量行；base_raw 提供旧行做刻度/缺口检测与合并。"""
    quick = os.environ.get("SECTOR_QUICK")
    retries = _fetch_retries()
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futs = {ex.submit(U.fetch_em_kline, code, start=start, retries=retries,
                          timeout=20 if quick else 40): (ind, code, name)
                for ind, code, name in codes}
        for f in as_completed(futs):
            ind, code, name = futs[f]
            old = (base_raw or {}).get("etfs", {}).get(code, {}).get("klines", []) if base_raw else []
            try:
                new_rows = f.result()["klines"]
                if incr is not None and base_raw is not None and old:
                    if kline_scale_jump(old, new_rows) or kline_gap_days(old, new_rows) > 10:
                        d2 = U.fetch_em_kline(code, start=U.FETCH_START, retries=retries,
                                              timeout=20 if quick else 40)
                        merged = d2["klines"]
                        incr["etfs"][code] = d2["klines"]
                        print(f"      {code} {name} 刻度/缺口检测触发，全量兜底（{len(merged)} 行）")
                    else:
                        merged = merge_klines(old, new_rows)
                        incr["etfs"][code] = new_rows
                else:
                    merged = new_rows
                raw["etfs"][code] = {"industry": ind, "name": name, "klines": merged}
            except Exception as exc:
                if quick and old:
                    raw["etfs"][code] = {"industry": ind, "name": name, "klines": old}
                    raw["failures"].append({"code": code, "name": name, "industry": ind, "error": str(exc)[:120]})
                    print(f"  [快速回退] {code} {name} 增量失败，沿用基线数据：{str(exc)[:90]}")
                else:
                    raw["failures"].append({"code": code, "name": name, "industry": ind, "error": str(exc)[:160]})
                    print(f"  [失败] {code} {name}（{ind}）：{str(exc)[:120]}")


def fetch_all():
    """并发抓取全部 ETF + 沪深300（全收益/价格），返回原始响应 dict。
    东财限流窗口是全局的：首轮失败后冷却 8s 只重抓失败项（最多 2 轮），
    配合 sector_universe 全局限流器 + 指数退避，实测可稳定抓全。"""
    raw = {"fetched_at": bj_now(), "etfs": {}, "csi": {}, "failures": []}

    def grab(codes):
        _grab_em(raw, codes, U.FETCH_START)

    grab(U.ALL_ETFS)
    # CI 场景（SECTOR_QUICK=1）：runner IP 被东财连接级封锁时快速失败，由 workflow 回退到归档
    max_rnd = 1 if os.environ.get("SECTOR_QUICK") else 2
    for rnd in range(max_rnd):
        if not raw["failures"]:
            break
        pending = [(f["industry"], f["code"], f["name"]) for f in raw["failures"]]
        print(f"      冷却 8s 后重试 {len(pending)} 只失败 ETF（第 {rnd + 1} 轮）...")
        time.sleep(8)
        raw["failures"] = []
        grab(pending)
    for c in ("H00300", "000300"):
        raw["csi"][c] = U.fetch_csi_index(c)
    return raw


def fetch_incremental():
    """增量抓取（v1.1）：基线=重建的最近完整归档，只请求 beg=基线末日+1 的增量区间。
    返回 (raw, mode, incr)；incr 为增量行 dict（供归档，full 时为 None）。"""
    base_raw, base_day, mode = rebuild_base()
    if base_raw is None:
        print("[增量] 无基线归档，首次全量抓取")
        return fetch_all(), "full", None
    if (datetime.date.today() - base_day).days > FULL_EVERY_DAYS:
        print(f"[增量] 基线 {base_day} 陈旧 >{FULL_EVERY_DAYS} 天，全量重抓建立新周基线")
        return fetch_all(), "full", None
    print(f"[增量] 基线 {base_day}（mode={mode}），只抓 beg={base_day + datetime.timedelta(days=1):%Y%m%d} 至今的增量区间")
    quick = os.environ.get("SECTOR_QUICK")
    raw = {"fetched_at": bj_now(), "etfs": {}, "csi": {}, "failures": []}
    incr = {"date": datetime.date.today().strftime("%Y%m%d"), "base": os.path.basename(
        sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-week-*.json.gz")))[-1]) if glob.glob(
        os.path.join(ARCHIVE_DIR, "sector-week-*.json.gz")) else "legacy",
        "etfs": {}, "csi": {}}
    start = (base_day + datetime.timedelta(days=1)).strftime("%Y%m%d")
    if start > datetime.date.today().strftime("%Y%m%d"):
        # 基线已是最新（如当日 CI 与本地先后触发）：无增量区间，沿用基线
        print(f"[增量] 基线 {base_day} 已是最新，无增量区间，沿用基线数据")
        return base_raw, "incr", incr
    _grab_em(raw, U.ALL_ETFS, start, incr=incr, base_raw=base_raw)
    # 非 quick：失败项冷却一轮重试（保持鲁棒性）
    if not quick and raw["failures"]:
        pending = [(f["industry"], f["code"], f["name"]) for f in raw["failures"]]
        print(f"      冷却 8s 后重试 {len(pending)} 只失败 ETF...")
        time.sleep(8)
        raw["failures"] = []
        _grab_em(raw, pending, start, incr=incr, base_raw=base_raw)
    # 沪深300（中证无前复权问题，纯合并；失败沿用基线）
    for c in ("H00300", "000300"):
        old = base_raw.get("csi", {}).get(c, [])
        try:
            new_rows = U.fetch_csi_index(c, start=start)
            raw["csi"][c] = merge_csi(old, new_rows)
            incr["csi"][c] = new_rows
        except Exception as exc:
            raw["csi"][c] = old
            raw["failures"].append({"code": c, "name": c, "industry": "CSI", "error": str(exc)[:120]})
            print(f"  [快速回退] {c} 增量失败，沿用基线数据：{str(exc)[:90]}")
    return raw, "incr", incr


def archive_incremental(mode, raw, incr):
    """增量/全量归档 + 滚动清理（incr 保留 KEEP_INCR、week 保留 KEEP_WEEK）。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    day = datetime.date.today().strftime("%Y%m%d")
    if mode == "full":
        p = os.path.join(ARCHIVE_DIR, f"sector-week-{day}.json.gz")
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        print(f"      全量周归档 {p}")
    elif mode == "incr" and incr is not None:
        p = os.path.join(ARCHIVE_DIR, f"sector-incr-{day}.json.gz")
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(incr, f, ensure_ascii=False)
        print(f"      增量归档 {p}（ETF {len(incr.get('etfs', {}))} 只 / CSI {len(incr.get('csi', {}))}）")
    # 滚动清理
    for pat, keep in (("sector-incr-*.json.gz", KEEP_INCR), ("sector-week-*.json.gz", KEEP_WEEK)):
        for p in sorted(glob.glob(os.path.join(ARCHIVE_DIR, pat)))[:-keep]:
            try:
                os.remove(p)
            except OSError:
                pass


def validate(raw):
    """硬校验：有效行业（有 ≥1 只成员 ETF 数据 ≥ MIN_HISTORY 日）占比 < 80% 直接失败。
    返回 (ok_inds, ratio, issues)。"""
    issues = []
    ok = 0
    for ind in U.INDUSTRY_LIST:
        codes = [c for c, v in raw["etfs"].items() if v.get("industry") == ind]
        best = max((len(raw["etfs"][c]["klines"]) for c in codes), default=0)
        if best >= U.MIN_HISTORY:
            ok += 1
        else:
            issues.append(f"{ind}({','.join(codes) or '无'}) 仅 {best} 日")
    ratio = ok / len(U.INDUSTRY_LIST)
    if ratio < U.MIN_VALID_RATIO:
        raise RuntimeError(
            f"有效行业 {ok}/{len(U.INDUSTRY_LIST)}（{ratio:.0%}）< {U.MIN_VALID_RATIO:.0%}，"
            f"拒绝发布：" + "；".join(issues))
    return ok, ratio, issues


def thin_series(arr, n):
    step = max(1, len(arr) // n)
    idx = list(range(0, len(arr), step))
    if idx[-1] != len(arr) - 1:
        idx.append(len(arr) - 1)
    return [round(float(arr[i]), 6) for i in idx], [str(arr[i]) for i in idx]


def build_sector_payload(panel, fac, bt, snap, issues, raw):
    """页面用 sector 数据段：排行 / 持仓 / 风控 / 回测曲线 / 交易 / 年度 / ETF 映射 / 披露。"""
    n = len(bt["dates"])
    s_nav, s_dates = thin_series(bt["nav"], 950)
    _, _ = thin_series(bt["csi_nav"], 950)
    c_nav = thin_series(bt["csi_nav"], 950)[0]
    e_nav = thin_series(bt["ew_nav"], 950)[0]
    s_dd = thin_series(bt["dd"], 950)[0]
    c_dd = thin_series(bt["csi_dd"], 950)[0]
    e_dd = thin_series(bt["ew_dd"], 950)[0]
    trades = [{"date": t["date"], "industry": t["industry"], "action": t["action"],
               "w": round(t["w"] * 100, 1), "fill_px": t["fill_px"], "reason": t["reason"],
               "score": t.get("score")}
              for t in bt["trades"]]
    etf_map = {}
    for ind in U.INDUSTRY_LIST:
        etf_map[ind] = [[code, name] for ind2, code, name in U.ALL_ETFS if ind2 == ind]
    m = snap["metrics"] or {}
    return {
        "generated_at": bj_now(),
        "data_date": snap["data_date"],
        "version": "v1.0",
        "start": E.START,
        "universe": snap["universe"],
        "rankings": snap["rankings"],
        "holdings": snap["holdings"],
        "risk": snap["risk"],
        "params": snap["params"],
        "metrics": m,
        "dates": s_dates,
        "strategy_nav": s_nav,
        "csi_nav": c_nav,
        "ew_nav": e_nav,
        "strategy_dd": s_dd,
        "csi_dd": c_dd,
        "ew_dd": e_dd,
        "annual": m.get("annual", {}),
        "trades": trades[-150:],
        "n_trades_total": len(trades),
        "cost_yuan": bt.get("cost_yuan", 0.0),
        "etf_map": etf_map,
        "issues": issues,
        "fetch_failures": raw.get("failures", []),
        "disclosure": [
            "实现口径：行业篮子＝成员 ETF 日收益等权合成；行业价格＝篮子累计净值 ×100；成交额求和、换手率取均值。",
            "信号＝动量 50%（20 日跳过近 5 日 + 60 日 + 120 日，年化 × 各自 R² 趋势质量后等权，截面 z 标准化）"
            "＋波动率 20%（20 日已实现波动率截面前 30% ×0.7）＋拥挤度 20%（60 日均换手 3 年分位 0.5 + 成交额占比 60 日变化 3 年分位 0.5，截面前 20% ×0.85）"
            "＋反转 10%（近 5 日跌幅截面前 10% 行业 +0.15）。",
            "拥挤度 v1 暂缺「份额变化率」第三维：东财无稳定历史份额接口，页面按两维合成并在参数表中如实标注；"
            "后续接份额数据后按方案恢复 1/3 等权。",
            "持仓：月度最后一个交易日决策、T+1 收盘成交，持有 Top-4 行业等权；换仓门槛 15%（新入选 ≥ 当前持仓最低分 ×1.15），"
            "持仓得分排名前 60% 留仓观察；月中加速：持仓跌出前 50% 且候选 ≥ 持仓最高分 ×1.2 时当月额外轮动一次（每月最多一次）。",
            "执行口径：成交价＝执行日行业收盘价 ×(1±单边 6bp)，双边 12bp（落在方案千 1~千 1.5 区间）；单月换手 ≤100%，超出按得分保留高分行。",
            "风控：策略近 20 日超额收益（vs 沪深300 全收益）自高点回撤 >8% → 仓位 50% 并暂停开仓，超额转正后恢复"
            "（触发需连续确认 5 日、熔断至少保持 10 个交易日，防抖）；单行业自建仓成本回撤 >12% 无条件平仓；"
            "沪深300 60 日波动率处过去一年 >80% 分位 → 仓位上限 70%，>90% → 50%（调仓日生效）。",
            "基准：沪深300 全收益 + 行业等权；回测 2018 至今，初始 10 万；夏普年化 252 日、无风险利率 0。",
            "信号与回测基于行业篮子净值（成员等权），实盘请按 ETF 映射以相应 ETF 执行，存在跟踪误差。",
            "本页面仅供策略验证与监控参考，不构成投资建议。",
        ],
    }


def archive(raw, payload):
    """原始响应（gzip 压缩，当日不可变输入）+ 当日计算快照，归档 data/。"""
    day = datetime.date.today().strftime("%Y%m%d")
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    raw_path = os.path.join(ARCHIVE_DIR, f"sector-raw-{day}.json.gz")
    with gzip.open(raw_path, "wt", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    snap_path = os.path.join(ARCHIVE_DIR, f"sector-snapshot-{day}.json")
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return raw_path, snap_path


def main():
    if _FETCH_ONLY:
        # CI 用：增量抓取+硬校验，原始 JSON 写盘，供 --raw 三跑共用（抓取量 3×→1×，且只抓增量区间）
        print("[fetch-only] 增量抓取 21 行业 × 30 ETF + 沪深300（并发 {}）...".format(FETCH_WORKERS))
        raw, mode, incr = fetch_incremental()
        etfs_ok = len(raw["etfs"])
        print(f"      ETF 有效 {etfs_ok}/{len(U.ALL_ETFS)}；失败 {len(raw['failures'])}；"
              f"CSI 全收益/价格 {len(raw['csi'].get('H00300', []))}/{len(raw['csi'].get('000300', []))} 条")
        ok_inds, ratio, issues = validate(raw)
        print(f"      有效行业 {ok_inds}/{len(U.INDUSTRY_LIST)}（{ratio:.0%}）")
        for w in issues:
            print("  [警告]", w)
        with open(_FETCH_ONLY, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        print(f"      原始数据已写 {_FETCH_ONLY}")
        archive_incremental(mode, raw, incr)
        return
    if _RAW:
        print(f"[1/4] 使用归档原始数据重跑（{_RAW}，跳过抓取）...")
        raw = load_raw(_RAW)
    else:
        print("[1/4] 增量抓取 21 行业 × 30 ETF + 沪深300（并发 {}）...".format(FETCH_WORKERS))
        raw, mode, incr = fetch_incremental()
    etfs_ok = len(raw["etfs"])
    print(f"      ETF 成功 {etfs_ok}/{len(U.ALL_ETFS)}；失败 {len(raw['failures'])}；"
          f"CSI 全收益/价格 {len(raw['csi'].get('H00300', []))}/{len(raw['csi'].get('000300', []))} 条")
    print("[2/4] 硬校验（有效行业占比）...")
    ok_inds, ratio, issues = validate(raw)
    print(f"      有效行业 {ok_inds}/{len(U.INDUSTRY_LIST)}（{ratio:.0%}）")
    for w in issues:
        print("  [警告]", w)
    print("[3/4] 统一引擎：因子 + 回测 + 风控 + 快照...")
    panel = E.build_panel(raw)
    fac = E.compute_factors(panel)
    bt = E.backtest(panel, fac)
    snap = E.snapshot(panel, fac, bt)
    m = snap["metrics"] or {}
    print(f"      覆盖 {len(panel['inds'])} 行业 / {panel['meta']['n_dates']} 交易日，最新 {snap['data_date']}")
    print(f"      回测 {m.get('total', 0)*100:+.1f}% / 夏普 {m.get('sharpe', 0):.2f} / "
          f"回撤 {m.get('mdd', 0)*100:.1f}% / {m.get('n_trades', 0)} 笔 / 年换手 {m.get('avg_annual_turn', 0):.2f}")
    payload = build_sector_payload(panel, fac, bt, snap, issues, raw)
    print("[4/4] 合并注入 index.html（携带红利低波段）...")
    tpl = open(os.path.join(BASE, "index_template.html"), encoding="utf-8").read()
    prev_html = None
    if os.path.exists(OUT) and not _DRY:
        with open(OUT, encoding="utf-8") as f:
            prev_html = f.read()
    prev = payload_util.extract(prev_html) if prev_html else None
    merged = {}
    if prev:
        for k in ("snapshot", "backtest", "hs300"):
            if prev.get(k) is not None:
                merged[k] = prev[k]
    merged["sector"] = payload
    html = payload_util.inject(tpl, merged)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      index.html 已生成 ({len(html.encode('utf-8'))//1024} KB) -> {OUT}")
    with open(DATA_OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"      sector_data.json 已生成 -> {DATA_OUT}")
    if not _DRY:
        if _RAW:
            # 三跑共用同一 raw：增量/全量归档由 fetch-only 阶段负责，这里只写当日快照
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            day = datetime.date.today().strftime("%Y%m%d")
            with open(os.path.join(ARCHIVE_DIR, f"sector-snapshot-{day}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            print(f"      快照归档 data/sector-snapshot-{day}.json")
        else:
            # 本地直接跑（非 --raw）：增量/全量归档已在抓取阶段完成（fetch_incremental 返回前），再补快照
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            day = datetime.date.today().strftime("%Y%m%d")
            with open(os.path.join(ARCHIVE_DIR, f"sector-snapshot-{day}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            print(f"      快照归档 data/sector-snapshot-{day}.json")


if __name__ == "__main__":
    main()
