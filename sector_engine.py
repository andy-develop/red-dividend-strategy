#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 行业轮动 · 统一策略引擎（v1.0，可复现 · 每日更新与回测共用单一实现）

实现《ETF行业轮动量化策略方案》（2026-09）：
  一、核心逻辑：行业截面动量（趋势中段）+ 环境识别与拥挤度控制
  二、标的池：21 行业 × 30 ETF（sector_universe.py），行业篮子=成员等权
  三、信号（多因子复合）：
     - 动量因子（约 50%）：20 日动量跳过最近 5 日 + 60 日 + 120 日，年化×各自 R²（趋势质量）后等权合成，截面 z 标准化
     - 波动率调整（约 20%）：20 日已实现波动率截面前 30% → 动量 ×0.7
     - 拥挤度控制（约 20%）：60 日均换手率 3 年分位 + 成交额占比 60 日变化 3 年分位，合成后截面前 20% → ×0.85
       （v1 暂缺 ETF 份额变化率维度——东财无稳定历史份额接口，见页面数据口径说明）
     - 宏观/情绪辅助（约 10%）：近 5 日跌幅截面前 10% 行业 +0.15 反转加分
     - final = mom_z × vol_mult × crowd_mult + rev_bonus
  四、组合与调仓：
     - 月度调仓（每月最后一个交易日决策，T+1 收盘执行），持有 Top-4 行业等权
     - 换仓门槛：新入选行业得分须 ≥ 当前持仓最低分 ×1.15；持仓得分排名前 60% 留仓观察
     - 月中加速：持仓跌出前 50% 且候选行业得分 ≥ 持仓最高分 ×1.2 → 当月额外轮动一次
  五、风控：
     - 动量失效熔断：策略近 20 日超额收益（vs 沪深300全收益）从高点回撤 >8% → 仓位×0.5、暂停新开仓，超额转正恢复
     - 单行业止损：行业篮子自建仓成本回撤 >12% → 无条件平仓
     - 市场状态过滤：沪深300 的 60 日波动率处于过去一年 >90% 分位 → 总仓位上限 50%；>80% → 70%
     - 换手率上限：单月组合换手 ≤100%，超出按得分高低优先保留高分行
  六、执行口径：决策 T 日收盘 → T+1 收盘成交；单边综合成本 6bp（滑点 5bp + 佣金万1，双边 12bp，落在方案建议的千1~千1.5 区间）；年化 252、夏普无风险利率 0。

用法:
  python3 sector_engine.py [--start 2018-01-01]   # 独立回测（在线抓数据）
  或作为模块被 sector_update.py import。
"""
import json, os, sys, math, datetime
import numpy as np
import pandas as pd

import sector_universe as U

# ============ 参数（v1.0） ============
START = U.BACKTEST_START          # 回测起点（2018-01-01；行业动态入池）
TOP_N = 4                         # 持有 Top-4 行业（方案推荐 3-5，取 4 平衡收益与集中度）
SWAP_GAP = 0.15                   # 换仓门槛：新入选得分 ≥ 当前持仓最低分 ×(1+15%)
STAY_PCT = 0.60                   # 留仓观察：持仓得分排名前 60% 不强制换出
ACCEL_RANK = 0.50                 # 月中加速：持仓排名跌出前 50%
ACCEL_GAP = 0.20                  # 月中加速：候选得分 ≥ 持仓最高分 ×(1+20%)
COST_PER_SIDE = 0.0006            # 单边综合成本 6bp（滑点 5bp + 佣金万1）→ 双边 12bp
SL_STOP = 0.12                    # 单行业止损：自建仓成本回撤 >12%
CB_EXCESS_DD = 0.08               # 熔断：20 日超额收益从高点回撤 >8%（或超额从未为正时 < -8%）
CB_EXPOSURE = 0.50                # 熔断时仓位降至 50%
CB_ON_DWELL = 5                   # 触发需连续确认 5 个交易日（防瞬时抖动）
CB_MIN_ON_DAYS = 10               # 熔断激活后至少保持 10 个交易日（防反复开关）
MK_VOL_WIN = 60                   # 沪深300 波动率窗口（60 日）
MK_VOL_LOOK = 252                 # 波动率分位回看（过去一年）
MK_CAP80, MK_CAP90 = 0.70, 0.50   # 波动率 >80% 分位→70% 上限；>90% 分位→50% 上限
TURNOVER_CAP = 1.0                # 单月组合换手率上限 100%
RISK_FREE = 0.0
TRADING_DAYS = 252
WARM_CROWD = 756                  # 拥挤度分位回看（3 年 ≈756 交易日）
MOM_SPANS = {20: 14, 60: 60, 120: 120}   # 动量窗口 → 年化用到的实际收益跨度（20 日动量跳过近 5 日=14 天）

INIT_CAPITAL = 100000.0           # 页面展示口径：初始 10 万


# ============ 面板构建 ============
def build_panel(raw):
    """把抓取的 ETF K 线 + 沪深300 原始响应整理为对齐的行业面板。

    行业篮子：成员 ETF 当日收益率等权平均 → 行业日收益；行业价格=cumprod(1+r)×100；
    行业成交额=成员求和；行业换手率=成员均值。
    返回 dict：dates / P / R / AMT / TO / active / avail_days / csi / meta
    """
    member = {}   # code -> DataFrame(date, close, amt, to, ret)
    for code, v in raw["etfs"].items():
        rows = []
        for line in v["klines"]:
            p = line.split(",")
            if len(p) < 11:
                continue
            d = pd.Timestamp(p[0])
            rows.append((d, float(p[2]), float(p[6]), float(p[10])))
        df = pd.DataFrame(rows, columns=["date", "close", "amt", "to"]).drop_duplicates("date")
        df = df.sort_values("date").reset_index(drop=True)
        df["ret"] = df["close"].pct_change()
        member[code] = df.set_index("date")
    ind_dfs = {}
    # 行业列表以原始响应自身的 industry 字段为准（测试与生产通用）
    ind_order = []
    for code, v in raw["etfs"].items():
        ind = v.get("industry")
        if ind and ind not in ind_order:
            ind_order.append(ind)
    for ind in ind_order:
        codes = [c for c, v in raw["etfs"].items() if v.get("industry") == ind]
        mdfs = [member[c] for c in codes if c in member and len(member[c]) > 60]
        if not mdfs:
            continue
        idx = sorted(set().union(*[set(m.index) for m in mdfs]))
        ret = pd.concat([m["ret"].reindex(idx) for m in mdfs], axis=1).mean(axis=1, skipna=True)
        amt = pd.concat([m["amt"].reindex(idx) for m in mdfs], axis=1).sum(axis=1, skipna=True)
        to = pd.concat([m["to"].reindex(idx) for m in mdfs], axis=1).mean(axis=1, skipna=True)
        price = (1 + ret.fillna(0)).cumprod() * 100
        d = pd.DataFrame({"P": price, "R": ret, "AMT": amt, "TO": to}, index=pd.DatetimeIndex(idx))
        ind_dfs[ind] = d
    dates = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in ind_dfs.values()])))
    n_ind = len(ind_dfs)
    inds = list(ind_dfs.keys())
    P = np.full((n_ind, len(dates)), np.nan)
    R = np.full((n_ind, len(dates)), np.nan)
    AMT = np.full((n_ind, len(dates)), np.nan)
    TO = np.full((n_ind, len(dates)), np.nan)
    for i, ind in enumerate(inds):
        d = ind_dfs[ind].reindex(dates)
        P[i] = d["P"].values
        R[i] = d["R"].values
        AMT[i] = d["AMT"].values
        TO[i] = d["TO"].values
    active = ~np.isnan(P)
    avail_days = np.cumsum(active.astype(int), axis=1)

    def csi_series(code):
        rows = {pd.Timestamp(r["tradeDate"]): float(r["close"]) for r in raw["csi"].get(code, [])}
        s = pd.Series(rows).sort_index().reindex(dates).ffill()
        return s.values
    csi_tr = csi_series("H00300") if "H00300" in raw.get("csi", {}) else csi_series("000300")
    csi_px = csi_series("000300")
    csi_tr_ret = np.zeros(len(dates))
    with np.errstate(divide="ignore", invalid="ignore"):
        _r = csi_tr[1:] / csi_tr[:-1] - 1
    csi_tr_ret[1:] = np.where(np.isfinite(_r), _r, 0.0)
    meta = {"n_industries": n_ind, "n_dates": len(dates),
            "industries": inds, "first_date": str(dates[0].date()), "last_date": str(dates[-1].date())}
    return {"dates": dates, "P": P, "R": R, "AMT": AMT, "TO": TO,
            "active": active, "avail_days": avail_days, "inds": inds,
            "csi_tr": csi_tr, "csi_px": csi_px, "csi_tr_ret": csi_tr_ret, "meta": meta}


def _rolling_percentile(s, w):
    """当前值在含其自身的前 w 个观测中的历史分位（0-1；越接近 1 越处于自身历史高位）。"""
    return s.rolling(w).apply(lambda x: float((x <= x[-1]).mean()), raw=True)


def _xs_z(df, col):
    """截面 z 标准化（按 date 分组）。transform 回调收到的是 Series。"""
    def f(s):
        sd = s.std(ddof=0)
        return (s - s.mean()) / sd if sd > 1e-12 else (s * 0.0)
    return df.groupby("date")[col].transform(f)


def _xs_pct(df, col):
    """截面排名分位（0-1，越小越靠前）。"""
    return df.groupby("date")[col].transform(lambda s: s.rank(pct=True))


def compute_factors(panel):
    """各行业逐日因子得分。返回 (n_ind, n_dates) 数组 + 长表 lf。"""
    dates = panel["dates"]
    inds = panel["inds"]
    n_ind = len(inds)
    P = pd.DataFrame(panel["P"].T, index=dates, columns=inds)
    R = pd.DataFrame(panel["R"].T, index=dates, columns=inds)
    AMT = pd.DataFrame(panel["AMT"].T, index=dates, columns=inds)
    TO = pd.DataFrame(panel["TO"].T, index=dates, columns=inds)
    logP = np.log(P)
    t_idx = pd.Series(np.arange(len(dates)), index=dates)
    mom = {}
    for w in (20, 60, 120):
        rw = {20: P.shift(6) / P.shift(20) - 1,
              60: P / P.shift(60) - 1,
              120: P / P.shift(120) - 1}[w]
        r2w = logP.rolling(w).corr(t_idx) ** 2
        ann = (1 + rw) ** (TRADING_DAYS / MOM_SPANS[w]) - 1
        mom[w] = ann * r2w
    mom_raw = (mom[20] + mom[60] + mom[120]) / 3.0
    vol20 = R.rolling(20).std(ddof=0) * math.sqrt(TRADING_DAYS)
    to60 = TO.rolling(60).mean()
    to_perc = to60.apply(lambda s: _rolling_percentile(s, WARM_CROWD))
    tot_amt = AMT.sum(axis=1)
    ash = AMT.div(tot_amt, axis=0)
    ash_chg = ash / ash.shift(60) - 1
    ash_perc = ash_chg.apply(lambda s: _rolling_percentile(s, WARM_CROWD))
    crowd = 0.5 * to_perc + 0.5 * ash_perc
    r5 = P / P.shift(5) - 1
    lf = pd.DataFrame({
        "date": dates.repeat(n_ind),
        "ind": list(inds) * len(dates),
        "mom_raw": mom_raw.values.reshape(-1),
        "vol20": vol20.values.reshape(-1),
        "crowd": crowd.values.reshape(-1),
        "r5": r5.values.reshape(-1),
    })
    lf["mom_z"] = _xs_z(lf, "mom_raw")
    lf["vol_pct"] = _xs_pct(lf, "vol20")
    lf["crowd_pct"] = _xs_pct(lf, "crowd")
    lf["r5_pct"] = _xs_pct(lf, "r5")
    lf["vol_mult"] = np.where(lf["vol_pct"] >= 0.70, 0.7, 1.0)
    lf["crowd_mult"] = np.where(lf["crowd_pct"] >= 0.80, 0.85, 1.0)
    lf["rev_bonus"] = np.where(lf["r5_pct"] < 0.10, 0.15, 0.0)
    lf["score"] = lf["mom_z"] * lf["vol_mult"] * lf["crowd_mult"] + lf["rev_bonus"]
    lf["pooled"] = panel["avail_days"].T.reshape(-1) >= U.MIN_HISTORY
    for c in ("mom_z", "score"):
        lf.loc[~lf["pooled"], c] = np.nan

    def wide(col):
        # lf 按 (日期×行业) 顺序构造，直接 reshape 为 (n_ind, n_dates)，避免 pivot 行为差异
        return lf[col].values.reshape(len(dates), n_ind).T
    return {
        "mom_raw": wide("mom_raw"), "mom_z": wide("mom_z"),
        "vol20": wide("vol20"), "vol_pct": wide("vol_pct"),
        "crowd": wide("crowd"), "crowd_pct": wide("crowd_pct"),
        "r5": wide("r5"), "rev_bonus": wide("rev_bonus"),
        "score": wide("score"), "pooled": wide("pooled"),
        "lf": lf,
    }


# ============ 回测 ============
def _month_end_flags(dates):
    months = dates.to_period("M").asi8
    flag = np.ones(len(dates), dtype=bool)
    flag[:-1] = months[:-1] != months[1:]
    return flag


def select_target(t, cur, score, pooled):
    """Top-N + 15% 换仓门槛 + 前 60% 留仓观察。返回行业下标列表（模块级，便于单测）。

    统一规则（持仓满/不满均一致）：
      1. 持仓排名前 60% → 留仓观察（protected），直接保留；
      2. 其余槽位按得分从高到低填充：持仓行业自动保留（非换仓），非持仓行业须
         得分 ≥ 当前持仓最低分 ×(1+15%) 才允许进入（换仓门槛，防无效换手）；
      3. 无合格新进入者时弱持仓保留——宁可少换，不因微小差距反复换仓。
    """
    s = score[:, t]
    avail = pooled[:, t] & ~np.isnan(s)
    order = np.where(avail)[0]
    if len(order) < 1:
        return []
    order = order[np.argsort(-s[order])]
    held = set(np.where(cur > 0)[0])
    if not held:
        return list(order[:TOP_N])
    rank_pct = {i: r / len(order) for r, i in enumerate(order)}
    protected = {h for h in held if rank_pct.get(h, 1.0) <= STAY_PCT}
    min_held = min(s[h] for h in held)
    target = set(protected)
    for i in order:
        if len(target) >= TOP_N:
            break
        if i in target:
            continue
        if i in held:
            target.add(i)
        else:
            if s[i] < min_held * (1 + SWAP_GAP):
                break
            target.add(i)
    return list(target)


def apply_turnover_cap(weights, sel, score_col, budget, tgt_w):
    """换手预算受限时按得分差成对换仓；预算耗尽保留现状，绝不清仓、绝不超仓。

    budget 为单边换手预算（方案"单月≤100%＝最多全部换一遍"）。
    成对执行：最高分新买入 × 最低分卖出（"按动量得分差距依次执行"），tgt_w 为缩放后目标权重；
    预算不足整对则跳过——宁可少换，不因预算截断造成暴露超上限或反向清仓。
    返回 (base, gross, turn)：gross=Σ|Δ| 双边绝对额（成本用），turn=gross/2 单边换手。
    """
    base = weights.copy()
    if budget <= 1e-12 or not sel:
        return base, 0.0, 0.0
    held_set = set(np.where(weights > 0)[0].tolist())
    sel_set = set(sel)
    buys = sorted(sel_set - held_set, key=lambda i: -score_col[i])
    sells = sorted(held_set - sel_set, key=lambda i: score_col[i])
    b = budget
    for bi, si in zip(buys, sells):
        cost = (weights[si] + tgt_w) / 2.0
        if cost > b + 1e-12:
            break
        base[si] = 0.0
        base[bi] += tgt_w
        b -= cost
    gross = np.abs(base - weights).sum()
    return base, gross, gross / 2.0


def backtest(panel, fac, start=START):
    """月度轮动 + 风控回测（T+1 收盘执行）。"""
    dates, n = panel["dates"], panel["P"].shape[1]
    n_ind = panel["P"].shape[0]
    P, R = panel["P"], panel["R"]
    csi_ret = panel["csi_tr_ret"]
    score, pooled = fac["score"], fac["pooled"]
    month_end = _month_end_flags(dates)
    start_i = int(np.searchsorted(dates, pd.Timestamp(start)))

    weights = np.zeros(n_ind)
    cost_price = np.full(n_ind, np.nan)
    nav = np.zeros(n)
    nav[:start_i] = 1.0
    port_ret = np.zeros(n)
    trades = []
    holdings_history = []
    cb_active = False
    last_cb = False
    cb_cand = 0
    cb_days = 0
    scale = 1.0
    exc20_hist = np.full(n, np.nan)
    month_budget = TURNOVER_CAP
    cur_month = None
    accel_used_month = -1
    cost_yuan = 0.0
    pending = None

    for t in range(start_i, n):
        # ---- 当日组合收益（t-1 收盘执行后生效的权重） ----
        pr = float(np.nansum(weights * R[:, t]))
        port_ret[t] = pr
        nav[t] = (nav[t - 1] * (1 + pr)) if t > start_i else (1.0 * (1 + pr))

        # ---- 执行 t-1 日挂单（T+1 收盘成交，成交价=执行日收盘） ----
        if t > start_i and pending is not None:
            exec_w, exec_cost, exec_trades, cost_yuan_add = pending
            nav[t] *= (1 - exec_cost)
            cost_yuan += cost_yuan_add
            for tr in exec_trades:
                tr["date"] = str(dates[t].date())
                i = tr["_i"]
                fill_px = float(P[i, t]) * (1 + COST_PER_SIDE) if tr["action"] == "买入" else float(P[i, t]) * (1 - COST_PER_SIDE)
                tr["fill_px"] = round(fill_px, 4)
                trades.append(tr)
                if tr["action"] == "买入":
                    old_w, old_cost = weights[i], cost_price[i]
                    new_w = old_w + abs(tr["w"])
                    if new_w > 1e-12:
                        cost_price[i] = (old_w * (old_cost if not np.isnan(old_cost) else fill_px)
                                         + abs(tr["w"]) * fill_px) / new_w
                else:
                    # 部分减仓保留成本基准；清仓才清除（否则风控降仓会误清成本、止损失效）
                    if weights[i] - abs(tr["w"]) <= 1e-12:
                        cost_price[i] = np.nan
            weights = exec_w
        pending = None

        # ---- 信号：20 日超额收益（策略 vs 沪深300全收益）与熔断（防抖状态机） ----
        if t - 19 >= start_i:
            st_ret = np.prod(1 + port_ret[t - 19:t + 1]) - 1
            csi_20 = np.prod(1 + csi_ret[t - 19:t + 1]) - 1
            exc20 = st_ret - csi_20
            exc20_hist[t] = exc20
            exc_vals = exc20_hist[start_i:t + 1]
            run_max = float(np.nanmax(exc_vals)) if np.isfinite(exc_vals).any() else 0.0
            if cb_active:
                cb_days += 1
                # 恢复条件：市场 20 日收益转正（"直到信号恢复"）。
                # 注：不能用"策略超额转正"——熔断期间策略半仓/空仓，上涨行情中超额结构性为负、
                # 永不转正，会形成现金锁定（已实测原条件把策略锁死在空仓）。以市场动量转正为准。
                if csi_20 > 0 and cb_days >= CB_MIN_ON_DAYS:
                    cb_active = False
                    cb_days, cb_cand = 0, 0
            else:
                trig = ((run_max > 0 and exc20 < run_max * (1 - CB_EXCESS_DD) and exc20 < 0)
                        or (run_max <= 0 and exc20 < -CB_EXCESS_DD))
                if trig:
                    cb_cand += 1
                    if cb_cand >= CB_ON_DWELL:
                        cb_active = True
                        cb_days, cb_cand = 0, 0
                else:
                    cb_cand = max(0, cb_cand - 1)

        # ---- 市场状态过滤：沪深300 60 日波动率分位（每日计算；仅在调仓/熔断变化日生效） ----
        if t - MK_VOL_LOOK + 1 >= 0 and t - MK_VOL_WIN >= 0:
            v60 = np.std(csi_ret[t - MK_VOL_WIN + 1:t + 1], ddof=0) * math.sqrt(TRADING_DAYS)
            win = np.lib.stride_tricks.sliding_window_view(csi_ret[max(0, t - MK_VOL_LOOK + 1):t + 1], MK_VOL_WIN)
            mk_pct = float((np.std(win, axis=1) * math.sqrt(TRADING_DAYS) <= v60).mean())
            mk_scale = MK_CAP90 if mk_pct > 0.90 else (MK_CAP80 if mk_pct > 0.80 else 1.0)
        else:
            mk_pct, mk_scale = 0.0, 1.0
        desired_scale = min(CB_EXPOSURE if cb_active else 1.0, mk_scale)

        # ---- 止损：持仓行业自建仓成本回撤 >12% ----
        sl_exits = [i for i in np.where(weights > 0)[0]
                    if not np.isnan(cost_price[i]) and P[i, t] / cost_price[i] - 1 <= -SL_STOP]

        # ---- 轮动决策（月度 / 月中加速） ----
        rebal, reason = False, ""
        if dates[t].month != cur_month:
            cur_month = dates[t].month
            month_budget = TURNOVER_CAP
            accel_used_month = -1
        if month_end[t] and not cb_active:
            rebal, reason = True, "月度轮动"
        elif not cb_active and not month_end[t] and month_budget > 0.3 and dates[t].month != accel_used_month:
            held = np.where(weights > 0)[0]
            if len(held) > 0:
                s = score[:, t]
                avail = pooled[:, t] & ~np.isnan(s)
                order = np.where(avail)[0]
                if len(order) > 1:
                    order = order[np.argsort(-s[order])]
                    rank_map = {i: r / len(order) for r, i in enumerate(order)}
                    worst = max(rank_map.get(h, 1.0) for h in held)
                    best_held = max(s[h] for h in held)
                    cands = [i for i in order if i not in set(held) and not np.isnan(s[i])]
                    if worst > ACCEL_RANK and cands and s[cands[0]] >= best_held * (1 + ACCEL_GAP):
                        rebal, reason = True, "加速轮动"
                        accel_used_month = dates[t].month

        # ---- 目标权重 ----
        # 调仓日：目标 = 等权 × 生效仓位系数（desired_scale），一次成型，避免二次缩放。
        # 换手率口径：turn 为单边换手（Σ|Δ|/2，方案"单月≤100%＝最多全部换一遍"），
        # gross 为双边绝对额（用于成本）。上限超支时按得分成对换仓，预算耗尽即停——
        # 保留现状持仓（已缩放），绝不反向清仓、绝不二次缩放（见 test_turnover_cap_no_liquidation）。
        if rebal and not cb_active:
            cur_for_sel = weights.copy()
            for i in sl_exits:
                cur_for_sel[i] = 0.0
            sel = [i for i in select_target(t, cur_for_sel, score, pooled) if i not in sl_exits]
            tgt_w = (1.0 / len(sel)) * desired_scale if sel else 0.0
            base = np.zeros(n_ind)
            if sel:
                base[sel] = tgt_w
            gross = np.abs(base - weights).sum()
            turn = gross / 2.0
            if turn > month_budget:
                base, gross, turn = apply_turnover_cap(weights, sel, score[:, t], month_budget, tgt_w)
        else:
            base = weights.copy()
            gross = 0.0
            turn = 0.0

        # ---- 止损平仓（无条件，不受换手率上限约束） ----
        for i in sl_exits:
            gross += weights[i]
            turn += weights[i] / 2.0
            base[i] = 0.0

        # ---- 风控缩放 ----
        # 波动率"仓位上限"是持仓目标约束，逐日/止损日套用会因分位抖动反复加减仓（实测产生 5% 级抖仓）。
        # 调仓日目标已按 desired_scale 一次成型（见上），无需再乘；熔断切换是主动风控事件，
        # 对现有（已缩放）仓位按新旧系数比例调整。
        if not rebal and cb_active != last_cb:
            if abs(desired_scale - scale) > 1e-9:
                new_base = base * (desired_scale / scale) if scale > 1e-12 else base * desired_scale
                gross += np.abs(new_base - base).sum()
                turn += np.abs(new_base - base).sum() / 2.0
                base = new_base
                scale = desired_scale
        last_cb = cb_active

        # ---- 挂单（T+1 执行） ----
        if turn > 1e-9:
            exec_trades = []
            cost_yuan_add = 0.0
            for i in range(n_ind):
                d = base[i] - weights[i]
                if abs(d) > 1e-9:
                    action = "买入" if d > 0 else "卖出"
                    fill_px = float(P[i, t])
                    fill = fill_px * (1 + COST_PER_SIDE) if d > 0 else fill_px * (1 - COST_PER_SIDE)
                    exec_trades.append({
                        "_i": i, "industry": panel["inds"][i], "action": action,
                        "w": round(float(d), 6), "fill_px": round(fill, 4),
                        "reason": "止损" if i in sl_exits else (reason or "风控调整"),
                        "score": round(float(score[i, t]), 3) if not np.isnan(score[i, t]) else None,
                    })
                    cost_yuan_add += abs(d) * COST_PER_SIDE * nav[t] * INIT_CAPITAL
            pending = (base, gross * COST_PER_SIDE, exec_trades, cost_yuan_add)
            month_budget = max(0.0, month_budget - turn)
            if rebal:
                scale = desired_scale   # 调仓挂单执行后实际暴露即目标系数
        holdings_history.append({"date": str(dates[t].date()), "weights": weights.copy(),
                                 "scale": scale, "cb": cb_active,
                                 "mk_pct": round(float(mk_pct), 3), "mk_scale": float(mk_scale),
                                 "tgt": float(base.sum()) if turn > 1e-9 else None})

    # ---- 基准 ----
    csi_nav = np.ones(n)
    csi_nav[start_i:] = np.cumprod(1 + csi_ret[start_i:])
    ew_ret = np.full(n, np.nan)
    cnt = np.sum(pooled, axis=0)
    with np.errstate(invalid="ignore"):
        ew_ret[cnt > 0] = np.nansum(np.where(pooled, R, 0.0), axis=0)[cnt > 0] / cnt[cnt > 0]
    ew_nav = np.ones(n)
    ew_nav[start_i:] = np.cumprod(1 + np.nan_to_num(ew_ret[start_i:]))
    dd = nav / np.maximum.accumulate(nav) - 1
    csi_dd = csi_nav / np.maximum.accumulate(csi_nav) - 1
    ew_dd = ew_nav / np.maximum.accumulate(ew_nav) - 1
    metrics = compute_metrics(nav, csi_nav, ew_nav, port_ret, dates, trades, start_i, cost_yuan)
    return {"nav": nav, "csi_nav": csi_nav, "ew_nav": ew_nav, "dd": dd, "csi_dd": csi_dd,
            "ew_dd": ew_dd, "dates": dates, "trades": trades, "metrics": metrics,
            "port_ret": port_ret, "exc20_hist": exc20_hist,
            "holdings_history": holdings_history, "cost_yuan": cost_yuan}


def compute_metrics(nav, csi_nav, ew_nav, port_ret, dates, trades, start_i, cost_yuan):
    seg = slice(start_i, len(nav))
    nav_s = nav[seg]
    if len(nav_s) < 2 or nav_s[-1] <= 0:
        return {}
    total = nav_s[-1] / nav_s[0] - 1
    years = (dates[seg][-1] - dates[seg][0]).days / 365.25
    cagr = (nav_s[-1] / nav_s[0]) ** (1 / years) - 1 if years > 0 else 0.0
    rets = port_ret[seg]
    sd = rets.std(ddof=0)
    sharpe = (rets.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 1e-12 else 0.0
    mdd = float((nav_s / np.maximum.accumulate(nav_s) - 1).min())
    calmar = (cagr / abs(mdd)) if mdd < 0 else 0.0
    s = pd.Series(nav_s, index=dates[seg])
    m = s.resample("ME").last().pct_change().dropna()
    mw = float((m > 0).mean()) if len(m) else 0.0
    yr = {}
    for y, g in s.groupby(s.index.year):
        if len(g) > 1:
            yr[str(y)] = round(float(g.iloc[-1] / g.iloc[0] - 1), 4)
    total_gross = sum(abs(t["w"]) for t in trades)      # 双边绝对额（Σ|Δ|）
    total_turn = total_gross / 2.0                       # 单边换手（与方案"≤100%"同口径）
    cost_pct = total_gross * COST_PER_SIDE               # 成本占比（每边各 6bp）
    return {
        "start": str(dates[seg][0].date()), "end": str(dates[seg][-1].date()),
        "total": round(float(total), 4), "cagr": round(float(cagr), 4),
        "sharpe": round(float(sharpe), 3), "mdd": round(float(mdd), 4),
        "calmar": round(float(calmar), 3), "month_win": round(float(mw), 3),
        "n_trades": len(trades), "total_turn": round(float(total_turn), 3),
        "avg_annual_turn": round(float(total_turn / years), 3) if years > 0 else 0.0,
        "cost_pct": round(float(cost_pct), 4), "cost_yuan": round(float(cost_yuan), 0),
        "years": round(float(years), 2), "annual": yr,
        "csi_total": round(float(csi_nav[seg][-1] / csi_nav[seg][0] - 1), 4),
        "ew_total": round(float(ew_nav[seg][-1] / ew_nav[seg][0] - 1), 4),
        "csi_sharpe": None,
        "csi_mdd": round(float((csi_nav[seg] / np.maximum.accumulate(csi_nav[seg]) - 1).min()), 4),
        "ew_mdd": round(float((ew_nav[seg] / np.maximum.accumulate(ew_nav[seg]) - 1).min()), 4),
    }


# ============ 今日快照（页面数据） ============
def snapshot(panel, fac, bt):
    dates, n = panel["dates"], panel["P"].shape[1]
    t = n - 1
    inds = panel["inds"]
    s = fac["score"][:, t]
    avail = fac["pooled"][:, t] & ~np.isnan(s)
    order = np.where(avail)[0]
    order = order[np.argsort(-s[order])]
    n_avail = len(order)
    rank_map = {i: r for r, i in enumerate(order)}
    t20 = max(0, t - 20)
    s20 = fac["score"][:, t20]
    a20 = fac["pooled"][:, t20] & ~np.isnan(s20)
    order20 = np.where(a20)[0]
    order20 = order20[np.argsort(-s20[order20])]
    rank20 = {i: r for r, i in enumerate(order20)}
    rankings = []
    for i in order:
        rankings.append({
            "ind": inds[i], "rank": rank_map[i] + 1,
            "score": round(float(s[i]), 3),
            "mom": round(float(fac["mom_z"][i, t]), 2) if not np.isnan(fac["mom_z"][i, t]) else None,
            "mom_raw": round(float(fac["mom_raw"][i, t]), 4) if not np.isnan(fac["mom_raw"][i, t]) else None,
            "vol": round(float(fac["vol20"][i, t]), 2) if not np.isnan(fac["vol20"][i, t]) else None,
            "vol_mult": round(float(fac["vol_pct"][i, t]), 2) if not np.isnan(fac["vol_pct"][i, t]) else None,
            "crowd": round(float(fac["crowd_pct"][i, t]), 2) if not np.isnan(fac["crowd_pct"][i, t]) else None,
            "rev": round(float(fac["rev_bonus"][i, t]), 2) if not np.isnan(fac["rev_bonus"][i, t]) else None,
            "rank_chg": int(rank_map[i] - rank20.get(i, rank_map[i])),
        })
    hh = bt["holdings_history"][-1]
    w = hh["weights"]
    holdings = [{"ind": inds[i], "w": round(float(w[i] * 100), 1), "rank": rank_map.get(i, -1) + 1}
                for i in np.where(w > 0)[0]]
    exc20 = float(bt["exc20_hist"][t]) if not np.isnan(bt["exc20_hist"][t]) else None
    m = bt["metrics"]
    return {
        "generated_at": None,   # 由 update 脚本填北京时间
        "data_date": str(dates[t].date()),
        "universe": {"industries": len(inds), "etfs": len(U.ALL_ETFS), "available": int(n_avail)},
        "rankings": rankings,
        "holdings": holdings,
        "risk": {"cb": bool(hh["cb"]), "exc20": round(exc20, 3) if exc20 is not None else None,
                 "mk_pct": hh.get("mk_pct"), "mk_cap": hh.get("mk_scale"),
                 "scale": round(float(w.sum()), 2)},
        "params": {"top_n": TOP_N, "swap_gap": SWAP_GAP, "stay_pct": STAY_PCT,
                   "accel_rank": ACCEL_RANK, "accel_gap": ACCEL_GAP,
                   "cost_per_side": COST_PER_SIDE, "sl_stop": SL_STOP,
                   "cb_excess_dd": CB_EXCESS_DD, "cb_exposure": CB_EXPOSURE,
                   "mk_cap80": MK_CAP80, "mk_cap90": MK_CAP90, "turnover_cap": TURNOVER_CAP},
        "metrics": m,
    }


def run(start=START):
    raw = U.fetch_universe()
    panel = build_panel(raw)
    fac = compute_factors(panel)
    bt = backtest(panel, fac, start=start)
    return panel, fac, bt


if __name__ == "__main__":
    import time
    t0 = time.time()
    print("[1/3] 抓取行情...")
    raw = U.fetch_universe()
    print(f"      完成（{time.time()-t0:.0f}s）")
    print("[2/3] 构建面板 + 计算因子...")
    panel = build_panel(raw)
    fac = compute_factors(panel)
    print(f"      {panel['meta']}")
    print("[3/3] 回测...")
    bt = backtest(panel, fac)
    m = bt["metrics"]
    print(f"      策略 {m['total']*100:+.1f}% / 夏普 {m['sharpe']:.2f} / 回撤 {m['mdd']*100:.1f}% / "
          f"{m['n_trades']} 笔 / 年换手 {m['avg_annual_turn']:.2f}")
    print(f"      沪深300 {m['csi_total']*100:+.1f}% / 行业等权 {m['ew_total']*100:+.1f}% / 成本 {m['cost_yuan']:.0f} 元")
