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

用法: python3 sector_update.py [--out index.html] [--data-out sector_data.json] [--dry-run]
"""
import json, os, sys, datetime, gzip, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import sector_universe as U
import sector_engine as E
import payload_util

_ARGS = sys.argv[1:]
_OUT = "index.html"; _DATA_OUT = "sector_data.json"; _DRY = False; _RAW = None
while _ARGS:
    a = _ARGS.pop(0)
    if a == "--out" and _ARGS:
        _OUT = _ARGS.pop(0)
    elif a == "--data-out" and _ARGS:
        _DATA_OUT = _ARGS.pop(0)
    elif a == "--raw" and _ARGS:
        _RAW = _ARGS.pop(0)   # 从归档原始数据重跑（跳过抓取），便于本地复现/验证
    elif a == "--dry-run":
        _DRY = True   # 不覆盖仓库产物、不归档、不携带线上段（幂等断言用）
OUT = os.path.join(BASE, _OUT)
DATA_OUT = os.path.join(BASE, _DATA_OUT)
ARCHIVE_DIR = os.path.join(BASE, "data")

FETCH_WORKERS = 3      # ETF 并发抓取（东财限流明显；全局限流器 + 指数退避见 sector_universe）
FETCH_RETRIES = 6      # 单只 ETF 失败重试次数（含退避 2s×(k+1)）


def bj_now():
    """北京时间（UTC+8）时间戳。dry-run 固定值保证两次生成逐位一致（幂等断言）。"""
    if _DRY:
        return "dry-run"
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M")


def fetch_all():
    """并发抓取全部 ETF + 沪深300（全收益/价格），返回原始响应 dict。
    东财限流窗口是全局的：首轮失败后冷却 8s 只重抓失败项（最多 2 轮），
    配合 sector_universe 全局限流器 + 指数退避，实测可稳定抓全。"""
    raw = {"fetched_at": bj_now(), "etfs": {}, "csi": {}, "failures": []}

    def grab(codes):
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            fut = {ex.submit(U.fetch_em_kline, code, retries=FETCH_RETRIES): (ind, code, name)
                   for ind, code, name in codes}
            for f in as_completed(fut):
                ind, code, name = fut[f]
                try:
                    d = f.result()
                    raw["etfs"][code] = {"industry": ind, "name": name, "klines": d["klines"]}
                except Exception as exc:
                    raw["failures"].append({"code": code, "name": name, "industry": ind, "error": str(exc)[:160]})
                    print(f"  [失败] {code} {name}（{ind}）：{str(exc)[:120]}")

    grab(U.ALL_ETFS)
    for rnd in range(2):
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
    if _RAW:
        print(f"[1/4] 使用归档原始数据重跑（{_RAW}，跳过抓取）...")
        if _RAW.endswith(".gz"):
            with gzip.open(_RAW, "rt", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            with open(_RAW, encoding="utf-8") as f:
                raw = json.load(f)
    else:
        print("[1/4] 抓取 21 行业 × 30 ETF + 沪深300（并发 {}）...".format(FETCH_WORKERS))
        raw = fetch_all()
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
        for k in ("snapshot", "backtest"):
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
        rp, sp = archive(raw, payload)
        print(f"      输入归档 {rp}（gzip），快照 {sp}")


if __name__ == "__main__":
    main()
