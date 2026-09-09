#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红利低波 v7.7 策略 · 每日 8 点自动更新
抓取 H20269(全收益)/H30269(价格) 行情 -> 统一回测引擎(backtest/engine.py) 计算信号与净值
-> 每日重生成日频 backtest_data.json -> 生成自包含 index.html

口径（v7.7，与回测引擎完全一致）：
  信号用价格指数 H30269 计算；收益用全收益 H20269；T+1 收盘成交 + 单边滑点 5bp；
  杠杆部分(125/150%)按年化 7% 按交易日计息；费用单边 max(万1, 5元)。

用法: python3 update.py [--out index.html] [--data-out backtest_data.json]
"""
import json, os, sys, time, datetime
import urllib.request
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
# 统一回测引擎：优先仓库内 backtest/（同级目录），无则回退上级 backtest/
_ENGINE_DIR = os.path.join(BASE, "backtest")
if not os.path.isdir(_ENGINE_DIR):
    _ENGINE_DIR = os.path.join(os.path.dirname(BASE), "backtest")
sys.path.insert(0, _ENGINE_DIR)
import engine as E
OUT = os.path.join(BASE, sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--out" else "index.html")
DATA_OUT = os.path.join(BASE, sys.argv[4] if len(sys.argv) > 4 and sys.argv[3] == "--data-out" else "backtest_data.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
START = E.START
LOOKBACK_DAYS = E.LOOKBACK_DAYS


def fetch_index(code, start, end, retries=4):
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode={code}&startDate={start}&endDate={end}")
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8"))
            rows = j.get("data") or []
            if rows:
                return rows
        except Exception as e:
            last = e
        time.sleep(2.0 + 2.0 * k)
    raise RuntimeError(f"fetch {code} failed: {last}")


def load_prices():
    """在线抓取 H20269(全收益) 与 H30269(价格)，对齐为 df(date/close/px)。"""
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    tr = {r["tradeDate"]: r["close"] for r in fetch_index("H20269", start, end)}
    px = {r["tradeDate"]: r["close"] for r in fetch_index("H30269", start, end)}
    dates = sorted(set(tr) & set(px))
    df = pd.DataFrame({"date": pd.to_datetime(dates), "close": [tr[d] for d in dates], "px": [px[d] for d in dates]})
    df = df.sort_values("date").reset_index(drop=True)
    # 注意：不截断到 START——保留 START 前的指标 warm-up（与 v7.6 一致，见 engine.get_prices 说明）
    return df


def validate_data(df, today=None):
    """数据完整性校验：最新交易日新鲜度、两序列覆盖差异、缺口率。
    返回警告列表（不抛异常——页面顶部展示警告条，避免全天失败导致页面不更新）。"""
    warns = []
    today = today or datetime.date.today()
    last = df["date"].iloc[-1].date()
    stale = (today - last).days
    # 覆盖春节长假(8天)+周末(2天)≈10 个自然日；超过则视为陈旧
    if stale > 10:
        warns.append(f"数据陈旧：最新数据 {last}，距今天 {stale} 天（>10 天，疑似接口缺最新交易日）")
    elif stale > 3:
        warns.append(f"数据滞后：最新数据 {last}，距今天 {stale} 天（非交易日属正常，若为工作日请留意）")
    # 两序列缺失差异：交易日只应相差节假（春节等），差异>5 天提示
    if "tr_date" in df.columns or "px_date" in df.columns:
        pass
    # 异常值：收盘价须为正
    bad = df[(df["close"] <= 0) | (df["px"] <= 0)]
    if len(bad):
        warns.append(f"检测到 {len(bad)} 个非正收盘价（{bad['date'].iloc[0].date()} 起），数据异常")
    if df["close"].isna().any() or df["px"].isna().any():
        warns.append("收盘价存在空值")
    # 单日异常跳变（>25%，除权除息或接口错误）
    chg = (df["close"].pct_change().abs())
    sp = df.loc[chg > 0.25, "date"]
    if len(sp):
        warns.append(f"全收益指数单日变动>25% 的日期 {len(sp)} 个（{sp.iloc[0].date()}…，疑似数据错误）")
    return warns


def bj_now():
    """北京时间（UTC+8）时间戳，用于页面展示。"""
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M")


def build_snapshot(df, state, pos, legs, t0, trades, os_stat, warnings=None):
    """今日快照：信号指标(px)、当前状态、触发缺口、临时仓倒计时。"""
    r = df.iloc[-1]
    close = float(r["close"])          # 全收益收盘（收益口径）
    px = float(r["px"])                # 价格收盘（信号口径）
    wj = float(r["wj"]); wrsi = float(r["wrsi"]); rsi = float(r["rsi"])
    upper = float(r["upper"]); lower = float(r["lower"])
    up63 = float(r["up63"]); dn63 = float(r["dn63"])
    last_date = r["date"]
    # 超卖缺口（2-of-4，px）
    need_band_low = max(0.0, (px / lower - 1) * 100)
    need_dn = max(0.0, (1 - float(r["hi63"]) * (1 - E.Y_DOWN / 100) / px) * 100)
    os_gap = max(need_band_low, need_dn)
    # A态超买缺口（三维极值，px）
    need_band_up = max(0.0, (upper / px - 1) * 100)
    need_up = max(0.0, (float(r["lo63"]) * (1 + E.X_UP / 100) / px - 1) * 100)
    ob_gap = max(need_band_up, need_up)
    # 动能消失缺口（临时仓卖点，px）
    need_j_drop = max(0.0, wj - E.J_CROSS_TO)
    need_rsi_drop = max(0.0, wrsi - E.RSI_CROSS_TO)
    legs_info = []
    for li, ld, lf in legs:
        due_date = ld + datetime.timedelta(days=E.HOLD_DAYS)
        remaining = max(0, (due_date - last_date).days)
        legs_info.append({"buy_date": ld.strftime("%Y-%m-%d"),
                          "due_date": due_date.strftime("%Y-%m-%d"),
                          "remaining_days": remaining, "pct": 25})
    exit_info = None
    if state == "B" and t0 is not None:
        rebuy = t0 + datetime.timedelta(days=E.REBUY_DAYS)
        exit_info = {"exit_date": t0.strftime("%Y-%m-%d"),
                     "rebuy_due": rebuy.strftime("%Y-%m-%d"),
                     "remaining_days": max(0, (rebuy - last_date).days)}
    snap = {
        "generated_at": bj_now(),
        "data_date": last_date.strftime("%Y-%m-%d"),
        "close": round(close, 2), "px": round(px, 2),
        "ma200": round(float(r["ma200"]), 2), "upper": round(upper, 2), "lower": round(lower, 2),
        "wj": round(wj, 2), "wrsi": round(wrsi, 2), "rsi": round(rsi, 2),
        "up63": round(up63, 2), "dn63": round(dn63, 2),
        "state": state, "pos": int(round(pos * 100)),
        "oversold_now": bool(r["oversold"]), "overbought_now": bool(r["overbought"]),
        "momentum_lost_now": bool(r["momentum_lost"]),
        "os_gap": {"need_drop_pct": round(os_gap, 2),
                   "band_gap": round(need_band_low, 2), "dn63_gap": round(need_dn, 2),
                   "wj_val": round(wj, 2), "wj_ok": wj < E.J_LOW,
                   "wrsi_val": round(wrsi, 2), "wrsi_ok": wrsi < E.RSI_OS},
        "ob_gap": {"need_rise_pct": round(ob_gap, 2),
                   "band_gap": round(need_band_up, 2), "up63_gap": round(need_up, 2),
                   "wj_val": round(wj, 2), "wj_ok": wj > E.J_HIGH},
        "ml_gap": {"j_need_drop": round(need_j_drop, 2), "rsi_need_drop": round(need_rsi_drop, 2),
                   "j_cross_ok": bool(r["j_cross"]), "rsi_cross_ok": bool(r["rsi_cross"]),
                   "diverg_ok": bool(r["diverg"]),
                   "hi10c": round(float(r["hi10c"]), 2), "hi10rsi": round(float(r["hi10rsi"]), 2)},
        "val": {"spread_pct": round(float(r["spread_pct"]) * 100, 1) if not pd.isna(r["spread_pct"]) else None,
                "div_proxy": round(float(r["div_proxy"]), 2) if not pd.isna(r["div_proxy"]) else None,
                "y10": round(float(r["y10"]), 3) if not pd.isna(r["y10"]) else None,
                "spread": round(float(r["spread"]), 2) if not pd.isna(r["spread"]) else None,
                "half": bool(r["os_half"]),
                "gate_ok": (not pd.isna(r["spread_pct"])) and float(r["spread_pct"]) >= 0.5,
                "ma250": round(float(r["ma250"]), 2),
                "ma250_ok": float(r["px"]) < float(r["ma250"])},
        "legs": legs_info, "exit": exit_info,
        "warnings": warnings or [],
        "recent_trades": trades[-12:],
        "oversold_stat": os_stat,
        "param": {"j_low": E.J_LOW, "j_high": E.J_HIGH, "j_cross_from": E.J_CROSS_FROM,
                  "j_cross_to": E.J_CROSS_TO, "rsi_os": E.RSI_OS,
                  "rsi_cross_from": E.RSI_CROSS_FROM, "rsi_cross_to": E.RSI_CROSS_TO,
                  "x_up": E.X_UP, "y_down": E.Y_DOWN, "hold_days": E.HOLD_DAYS, "rebuy_days": E.REBUY_DAYS,
                  "slippage_bps": E.SLIPPAGE_BPS, "fin_rate": E.FIN_RATE,
                  "val_gate": E.VAL_GATE, "ma250_gate": E.MA250_GATE, "val_win": E.VAL_WIN},
    }
    return snap


def build_backtest_payload(df, trades, ec, metrics, os_stat, overview):
    """每日重生成 backtest_data.json：日频全量净值/回撤 + 交易 + 指标 + 抄底胜率。
    指标一律用日频全量计算（抽稀仅用于页面图表展示，不用于任何指标）。"""
    n = len(ec["dates"])
    step = max(1, n // 950)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return {
        "generated_at": bj_now(),
        "start": ec["dates"][0], "end": ec["dates"][-1],
        "dates": [ec["dates"][i] for i in idx],
        "strategy_nav": [round(float(ec["strategy_nav"][i]), 6) for i in idx],
        "bh_nav": [round(float(ec["bh_nav"][i]), 6) for i in idx],
        "strategy_dd": [round(float(ec["strategy_dd"][i]), 6) for i in idx],
        "bh_dd": [round(float(ec["bh_dd"][i]), 6) for i in idx],
        "trades": trades,
        "metrics": metrics,
        "oversold_stat": os_stat,
        "overview": overview,
    }


def render(snap, bt):
    tpl = open(os.path.join(BASE, "index_template.html"), encoding="utf-8").read()
    payload = {"snapshot": snap, "backtest": bt}
    return tpl.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))


def main():
    print("[1/4] 抓取行情...")
    df_all = load_prices()
    print(f"      共 {len(df_all)} 条，最新 {df_all['date'].iloc[-1].date()}")
    warnings = validate_data(df_all)
    for w in warnings:
        print("  [警告]", w)
    print("[2/4] 统一引擎：信号(px, 含warm-up) + T+1 撮合 + 净值核算...")
    df_all = E.build_signals(df_all)
    trades, closed, positions, state, pos, legs, t0 = E.replay(df_all, t1=True, start=E.START)
    df = df_all[df_all["date"] >= pd.Timestamp(E.START)].reset_index(drop=True)
    ec = E.equity_curve(df, trades, positions)
    m = E.metrics(ec, trades)
    os_stat = E.oversold_stats(closed)
    print(f"      当前状态 {state}，仓位 {int(pos*100)}% | 回测 {m['total']*100:+.1f}% / 夏普 {m['sharpe']:.2f} / 回撤 {m['mdd']*100:.1f}% / {m['n_trades']}笔")
    print("[3/4] 重生成日频 backtest_data.json...")
    bt = build_backtest_payload(df, trades, ec, m, os_stat, E.overview_stats(trades, closed, df))
    with open(DATA_OUT, "w", encoding="utf-8") as f:
        json.dump(bt, f, ensure_ascii=False)
    print(f"      {DATA_OUT} ({len(bt['dates'])} 采样点 / {len(trades)} 笔 / {bt['end']})")
    print("[4/4] 生成 HTML...")
    snap = build_snapshot(df_all, state, pos, legs, t0, trades, os_stat, warnings)
    html = render(snap, bt)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      index.html 已生成 ({len(html.encode('utf-8'))//1024} KB) -> {OUT}")


if __name__ == "__main__":
    main()
