#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红利低波 v7 策略 · 每日 8 点自动更新
抓取 H20269/H30269 行情 -> 计算情绪极值指标 -> 状态机重放 -> 生成自包含 index.html
用法: python3 update.py [--out index.html]
"""
import json, os, sys, time, math, datetime
import urllib.request
import pandas as pd
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--out" else "index.html")

HOLD_DAYS = 60          # 到期强制：60 个自然日
J_LOW, J_HIGH = 1.0, 95.0
J_CROSS_FROM, J_CROSS_TO = 90.0, 80.0     # 动能消失：J 从 >90 跌破 80
RSI_OS = 35.0                             # 周线RSI(14) 超卖阈值
RSI_CROSS_FROM, RSI_CROSS_TO = 70.0, 65.0 # 动能消失：RSI 从 >70 跌破 65
X_UP, Y_DOWN = 20.0, 20.0
START = "2016-09-08"    # 与回测区间起点一致
LOOKBACK_DAYS = 3800    # 自然日，覆盖 2016-09-08 起点 + MA200/周线KDJ收敛/63日动量余量
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


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
        time.sleep(2.0 + 2.0 * k)  # 官方接口限流
    raise RuntimeError(f"fetch {code} failed: {last}")


def load_prices():
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    tr = {r["tradeDate"]: r["close"] for r in fetch_index("H20269", start, end)}
    px = {r["tradeDate"]: r["close"] for r in fetch_index("H30269", start, end)}
    dates = sorted(set(tr) & set(px))
    df = pd.DataFrame({"date": pd.to_datetime(dates), "close": [tr[d] for d in dates], "px": [px[d] for d in dates]})
    df = df.sort_values("date").reset_index(drop=True)
    return df


def add_indicators(df):
    c = df["close"]
    df["ma20"] = c.rolling(20).mean()
    df["std20"] = c.rolling(20).std(ddof=0)
    df["upper"] = df["ma20"] + 2 * df["std20"]
    df["lower"] = df["ma20"] - 2 * df["std20"]
    df["ma200"] = c.rolling(200).mean()
    # 周线 KDJ(9,3,3)，ISO 周
    iso = df["date"].dt.isocalendar()
    wk_key = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    wk = df.groupby(wk_key).agg(close=("close", "last"), date=("date", "last")).reset_index(drop=True)
    low9 = wk["close"].rolling(9).min()
    high9 = wk["close"].rolling(9).max()
    rsv = ((wk["close"] - low9) / (high9 - low9) * 100).fillna(50.0)
    k = np.empty(len(wk)); d = np.empty(len(wk)); k[0] = d[0] = 50.0
    for i in range(1, len(wk)):
        k[i] = 2 / 3 * k[i - 1] + 1 / 3 * rsv.iloc[i]
        d[i] = 2 / 3 * d[i - 1] + 1 / 3 * k[i]
    wk["J"] = 3 * k - 2 * d
    df["wj"] = df["date"].map(wk.set_index("date")["J"]).ffill()
    # 周线 RSI(14) Wilder（v7.2 新增，合理区间 35-70）
    wk_dlt = wk["close"].diff()
    wk_gain = wk_dlt.clip(lower=0); wk_loss = (-wk_dlt).clip(lower=0)
    wg = wk_gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    wl = wk_loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    wk["RSI"] = (100 - 100 / (1 + wg / wl)).fillna(50.0)
    df["wrsi"] = df["date"].map(wk.set_index("date")["RSI"]).ffill()
    # RSI(14) Wilder
    delta = c.diff()
    up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    ru = up.ewm(alpha=1 / 14, adjust=False).mean()
    rd = dn.ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + ru / rd)
    # 近63日动量（前一日窗口，避免用当日自身）
    df["lo63"] = c.rolling(63).min().shift(1)
    df["hi63"] = c.rolling(63).max().shift(1)
    df["up63"] = (c / df["lo63"] - 1) * 100
    df["dn63"] = (c / df["hi63"] - 1) * 100
    # 超卖（买点）2-of-4：J<1 / 跌破布林下轨 / dn63≤-20% / 周线RSI<35 中任意 2 个
    df["oversold"] = ((df["wj"] < J_LOW).astype(int) + (c <= df["lower"]).astype(int)
                      + (df["dn63"] <= -Y_DOWN).astype(int) + (df["wrsi"] < RSI_OS).astype(int)) >= 2
    # A态超买清仓（三维极值）：J>95 且 触及/突破上轨 且 63日涨幅≥20%
    df["overbought"] = (df["wj"] > J_HIGH) & (c >= df["upper"]) & (df["up63"] >= X_UP)
    # 动能消失（临时仓卖出，任一触发）：J 从>90跌破80 / RSI 从>70跌破65 / 10日顶背离（收盘创新高且日线RSI未新高）
    df["hi10c"] = c.rolling(10).max().shift(1)
    df["hi10rsi"] = df["rsi"].rolling(10).max().shift(1)
    df["diverg"] = (c > df["hi10c"]) & (df["rsi"] < df["hi10rsi"])
    df["j_cross"] = (df["wj"].rolling(10).max() > J_CROSS_FROM) & (df["wj"] <= J_CROSS_TO)
    df["rsi_cross"] = (df["wrsi"].rolling(10).max() > RSI_CROSS_FROM) & (df["wrsi"] <= RSI_CROSS_TO)
    df["momentum_lost"] = df["j_cross"] | df["rsi_cross"] | df["diverg"]
    return df


def replay(df, start=START):
    """v7.6 状态机重放：A(100)->C(125)->D(150)；B(0) 离场；
    超卖 2-of-4 共振加仓；A态超买三维极值清仓；临时仓卖出=动能消失确认（J跌破80/RSI跌破65/顶背离）；
    60 自然日到期兜底；B 离场 60 自然日强制回补。与回测口径一致：仅重放 start 之后的交易。
    返回 (state, pos, legs, t0, trades, oversold_closed)；
    oversold_closed 为超卖抄底档的逐笔闭环（买入日期/价、卖出日期/价、卖出原因、收益率）。"""
    state, pos, t0 = "A", 1.0, None
    legs = []  # 临时加仓档 [(交易日索引, 日期, 买入收盘价)]
    trades = []
    oversold_closed = []  # 抄底闭环：{buy_date, buy_price, sell_date, sell_price, reason, ret}
    start_ts = pd.Timestamp(start)
    for i in range(len(df)):
        r = df.iloc[i]
        d = r["date"]
        if d < start_ts:
            continue
        osig = bool(r["oversold"]); obsig = bool(r["overbought"])
        old = pos
        act = None
        if state == "A":
            if osig: act = ("buy", 1.25, "C", "情绪极值超卖共振·加仓至125%")
            elif obsig: act = ("sell", 0.0, "B", "超买极值共振·清仓离场")
        elif state == "B":
            if osig: act = ("buy", 1.25, "C", "离场中现超卖共振·回补并加仓至125%")
            elif t0 is not None and d >= t0 + datetime.timedelta(days=HOLD_DAYS):
                act = ("buy", 1.0, "A", "离场满60自然日·强制回补至100%")
        elif state in ("C", "D"):
            if state == "C" and osig and pos < 1.5:
                act = ("buy", 1.5, "D", "再次超卖共振·加仓至150%")
            elif bool(r["j_cross"]):
                act = ("sell", 1.0, "A", "动能消失(J跌破80)·了结临时仓回100%")
            elif bool(r["rsi_cross"]):
                act = ("sell", 1.0, "A", "动能消失(RSI跌破65)·了结临时仓回100%")
            elif bool(r["diverg"]):
                act = ("sell", 1.0, "A", "动能消失(顶背离)·了结临时仓回100%")
            if act is None and legs and d >= legs[0][1] + datetime.timedelta(days=HOLD_DAYS):
                np_ = pos - 0.25
                act = ("sell", np_, "C" if np_ > 1.0 + 1e-9 else "A", "加仓满60自然日·卖出一档临时仓")
        if act:
            new_pos, new_state = act[1], act[2]
            trades.append({"date": r["date"].strftime("%Y-%m-%d"), "action": "买入" if act[0] == "buy" else "卖出",
                           "close": round(float(r["close"]), 2), "reason": act[3],
                           "pos_before": int(round(old * 100)), "pos_after": int(round(new_pos * 100))})
            if act[0] == "buy" and new_pos > 1.0 + 1e-9 and new_pos > old + 1e-9:
                legs.append((i, d, float(r["close"])))
            if act[0] == "sell":
                n_legs = int(round((old - new_pos) / 0.25))
                for _ in range(n_legs):
                    if legs:
                        bd, bd_, bp = legs.pop(0)
                        oversold_closed.append({
                            "buy_date": bd_.strftime("%Y-%m-%d"), "buy_price": round(bp, 2),
                            "sell_date": r["date"].strftime("%Y-%m-%d"), "sell_price": round(float(r["close"]), 2),
                            "reason": act[3],
                            "ret": round((float(r["close"]) - bp) / bp * 100, 2),
                            "diff": round(float(r["close"]) - bp, 2),
                        })
            pos, state = new_pos, new_state
            if state == "A":
                t0 = None
            if state == "B":
                t0 = d
    return state, pos, legs, t0, trades, oversold_closed


def gap_pct(need, cur):
    """need: 需要满足的值(阈值方向)，cur: 当前值。返回距触发还需的变化幅度(%)，已满足返回 0。"""
    return max(0.0, need - cur)


def build_snapshot(df, state, pos, legs, t0, trades, oversold_closed):
    r = df.iloc[-1]
    close = float(r["close"])
    lo63, hi63 = float(r["lo63"]), float(r["hi63"])
    wj = float(r["wj"]); wrsi = float(r["wrsi"])
    upper = float(r["upper"]); lower = float(r["lower"])
    last_date = r["date"]
    # 抄底胜率：按买入年份拆分 + 按卖出原因分组
    os_stat = {"total": len(oversold_closed),
               "wins": sum(1 for c in oversold_closed if c["ret"] > 0),
               "losses": sum(1 for c in oversold_closed if c["ret"] <= 0),
               "yearly": [], "reason": [], "closed": oversold_closed}
    if oversold_closed:
        os_stat["winrate"] = round(sum(1 for c in oversold_closed if c["ret"] > 0) / len(oversold_closed) * 100, 1)
        os_stat["avg_ret"] = round(sum(c["ret"] for c in oversold_closed) / len(oversold_closed), 2)
        by_year, by_reason = {}, {}
        for c in oversold_closed:
            y = c["buy_date"][:4]
            by_year.setdefault(y, []).append(c)
            key = "到期强制卖出" if "到期" in c["reason"] else ("动能消失·顶背离" if "顶背离" in c["reason"]
                                                                  else ("动能消失·J跌破80" if "J跌破80" in c["reason"]
                                                                        else ("动能消失·RSI跌破65" if "RSI跌破65" in c["reason"] else c["reason"])))
            by_reason.setdefault(key, []).append(c)
        for y in sorted(by_year):
            g = by_year[y]; n = len(g); w = sum(1 for c in g if c["ret"] > 0)
            os_stat["yearly"].append({"year": y, "n": n, "wins": w, "losses": n - w,
                                      "winrate": round(w / n * 100, 1),
                                      "avg_ret": round(sum(c["ret"] for c in g) / n, 2),
                                      "closed": g})
        for k in by_reason:
            g = by_reason[k]; n = len(g); w = sum(1 for c in g if c["ret"] > 0)
            os_stat["reason"].append({"reason": k, "n": n, "wins": w, "losses": n - w,
                                      "winrate": round(w / n * 100, 1),
                                      "avg_ret": round(sum(c["ret"] for c in g) / n, 2)})
        os_stat["reason"].sort(key=lambda x: -x["n"])
    else:
        os_stat["winrate"], os_stat["avg_ret"] = 0.0, 0.0
    # 超卖缺口（2-of-4）：布林下轨 / dn63<=-20% 的跌幅缺口（取较大者）；J 与 RSI 单独展示
    need_band_low = max(0.0, (close / lower - 1) * 100)
    need_dn = max(0.0, (1 - hi63 * (1 - Y_DOWN / 100) / close) * 100)
    os_gap = max(need_band_low, need_dn)
    # A态超买缺口（三维极值）：上轨 / up63>=20% 的涨幅缺口（取较大者）
    need_band_up = max(0.0, (upper / close - 1) * 100)
    need_up = max(0.0, (lo63 * (1 + X_UP / 100) / close - 1) * 100)
    ob_gap = max(need_band_up, need_up)
    # 动能消失缺口（临时仓卖点）：J 从>90跌破80 / RSI 从>70跌破65 / 10日背离
    need_j_drop = max(0.0, wj - J_CROSS_TO)          # 还需跌多少到 80（若曾>90）
    need_rsi_drop = max(0.0, wrsi - RSI_CROSS_TO)    # 还需跌多少到 65（若曾>70）
    # 到期提醒：每档临时仓 60 自然日
    legs_info = []
    for li, ld in legs:
        due_date = ld + datetime.timedelta(days=HOLD_DAYS)
        remaining = max(0, (due_date - last_date).days)
        legs_info.append({
            "buy_date": ld.strftime("%Y-%m-%d"),
            "due_date": due_date.strftime("%Y-%m-%d"),
            "remaining_days": remaining,
            "pct": 25,
        })
    exit_info = None
    if state == "B" and t0 is not None:
        rebuy = t0 + datetime.timedelta(days=HOLD_DAYS)
        exit_info = {
            "exit_date": t0.strftime("%Y-%m-%d"),
            "rebuy_due": rebuy.strftime("%Y-%m-%d"),
            "remaining_days": max(0, (rebuy - last_date).days),
        }
    last_date = r["date"].strftime("%Y-%m-%d")
    snap = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_date": last_date,
        "close": round(close, 2),
        "ma200": round(float(r["ma200"]), 2), "upper": round(upper, 2), "lower": round(lower, 2),
        "ma20": round(float(r["ma20"]), 2),
        "wj": round(wj, 2), "wrsi": round(wrsi, 2), "rsi": round(float(r["rsi"]), 2),
        "up63": round(float(r["up63"]), 2), "dn63": round(float(r["dn63"]), 2),
        "state": state, "pos": int(round(pos * 100)),
        "oversold_now": bool(r["oversold"]), "overbought_now": bool(r["overbought"]),
        "momentum_lost_now": bool(r["momentum_lost"]),
        "os_gap": {  # 距超卖 2-of-4
            "need_drop_pct": round(os_gap, 2),
            "band_gap": round(need_band_low, 2), "dn63_gap": round(need_dn, 2),
            "wj_val": round(wj, 2), "wj_ok": wj < J_LOW,
            "wrsi_val": round(wrsi, 2), "wrsi_ok": wrsi < RSI_OS,
        },
        "ob_gap": {  # 距 A态超买三维极值
            "need_rise_pct": round(ob_gap, 2),
            "band_gap": round(need_band_up, 2), "up63_gap": round(need_up, 2),
            "wj_val": round(wj, 2), "wj_ok": wj > J_HIGH,
        },
        "ml_gap": {  # 距动能消失（临时仓卖点）
            "j_need_drop": round(need_j_drop, 2), "rsi_need_drop": round(need_rsi_drop, 2),
            "j_cross_ok": bool(r["j_cross"]), "rsi_cross_ok": bool(r["rsi_cross"]),
            "diverg_ok": bool(r["diverg"]),
            "hi10c": round(float(r["hi10c"]), 2), "hi10rsi": round(float(r["hi10rsi"]), 2),
        },
        "legs": legs_info, "exit": exit_info,
        "oversold_stat": os_stat,
        "recent_trades": trades[-12:],
        "param": {"j_low": J_LOW, "j_high": J_HIGH,
                  "j_cross_from": J_CROSS_FROM, "j_cross_to": J_CROSS_TO,
                  "rsi_os": RSI_OS,
                  "rsi_cross_from": RSI_CROSS_FROM, "rsi_cross_to": RSI_CROSS_TO,
                  "x_up": X_UP, "y_down": Y_DOWN, "hold_days": HOLD_DAYS},
    }
    return snap


def render(snap):
    bt = json.load(open(os.path.join(BASE, "backtest_data.json"), encoding="utf-8"))
    tpl = open(os.path.join(BASE, "index_template.html"), encoding="utf-8").read()
    payload = {"snapshot": snap, "backtest": bt}
    return tpl.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))


def main():
    print("[1/4] 抓取行情...")
    df = load_prices()
    print(f"      共 {len(df)} 条，最新 {df['date'].iloc[-1].date()}")
    print("[2/4] 计算指标...")
    df = add_indicators(df)
    print("[3/4] 状态机重放...")
    state, pos, legs, t0, trades, oversold_closed = replay(df)
    snap = build_snapshot(df, state, pos, legs, t0, trades, oversold_closed)
    print(f"      当前状态 {state}，仓位 {snap['pos']}%，超卖触发={snap['oversold_now']}，超买触发={snap['overbought_now']}")
    o = snap["oversold_stat"]
    print(f"      抄底闭环 {o['total']} 档，胜率 {o['winrate']}%（{o['wins']}盈/{o['losses']}亏），平均 {o['avg_ret']}%")
    print("[4/4] 生成 HTML...")
    html = render(snap)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      index.html 已生成 ({len(html.encode('utf-8'))//1024} KB) -> {OUT}")


if __name__ == "__main__":
    main()
