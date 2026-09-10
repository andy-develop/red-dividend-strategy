#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sector_engine 最小单元测试（确定性合成数据，不联网）。

运行：python3 -m unittest tests.test_sector_engine -v
"""
import os, sys, math, unittest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sector_engine as E
import sector_universe as U


def make_raw(industries, csi_mode="calm", seed=42, start="2015-01-02", end="2022-12-30"):
    """合成原始响应。industries: {ind: [(code, drift, vol, to_level), ...]}；
    csi_mode: calm=平稳上行 / spike=后段高波动 / rally=持续上行。"""
    dates = pd.bdate_range(start, end)
    n = len(dates)
    rng = np.random.default_rng(seed)
    csi_ret = rng.normal(0.0003, 0.008, n)
    if csi_mode == "spike":
        spike = dates > pd.Timestamp("2020-06-01")
        csi_ret[spike] = rng.normal(0.0, 0.045, int(spike.sum()))
    elif csi_mode == "rally":
        csi_ret = rng.normal(0.002, 0.008, n)
    csi_close = 3000 * np.cumprod(1 + csi_ret)
    etfs = {}
    for ind, members in industries.items():
        for code, drift, vol, to_level in members:
            ret = rng.normal(drift, vol, n)
            close = 100 * np.cumprod(1 + ret)
            kl = []
            prev = close[0]
            for i, d in enumerate(dates):
                c = close[i]
                amt = 5e8 * (0.5 + rng.random())
                kl.append(f"{d.strftime('%Y-%m-%d')},{prev:.4f},{c:.4f},{max(prev,c)*1.002:.4f},"
                          f"{min(prev,c)*0.998:.4f},{int(amt/1000)},{amt:.0f},1.0,0.0,0.0,{to_level*(0.5+rng.random()):.4f}")
                prev = c
            etfs[code] = {"industry": ind, "name": f"ETF{code}", "klines": kl}
    csi = {"H00300": [{"tradeDate": d.strftime("%Y%m%d"), "close": float(c)}
                      for d, c in zip(dates, csi_close)],
           "000300": [{"tradeDate": d.strftime("%Y%m%d"), "close": float(c / 1.5)}
                      for d, c in zip(dates, csi_close)]}
    return {"etfs": etfs, "csi": csi}


def six_ind(mode="flat", seed=7):
    """6 个行业：A 高动量、B~E 中动量、F 低动量。"""
    inds = {
        "A": [("111001", 0.0012, 0.012, 1.5)],
        "B": [("111002", 0.0005, 0.012, 1.5)],
        "C": [("111003", 0.0005, 0.012, 1.5)],
        "D": [("111004", 0.0004, 0.012, 1.5)],
        "E": [("111005", 0.0003, 0.012, 1.5)],
        "F": [("111006", -0.0010, 0.012, 1.5)],
    }
    if mode == "crash_a":
        # A 先强后崩（止损场景）
        inds["A"] = [("111001", 0.0012, 0.012, 1.5)]
    return inds


class TestPanel(unittest.TestCase):
    def test_build_panel_shapes(self):
        raw = make_raw(six_ind())
        panel = E.build_panel(raw)
        n_ind, n_d = len(panel["inds"]), len(panel["dates"])
        self.assertEqual(panel["P"].shape, (n_ind, n_d))
        self.assertEqual(len(panel["inds"]), 6)
        self.assertTrue(np.isfinite(panel["P"]).all())
        self.assertTrue(np.isfinite(panel["csi_tr"]).all())
        self.assertEqual(len(panel["csi_tr_ret"]), n_d)


class TestFactors(unittest.TestCase):
    def test_momentum_orders_rising_first(self):
        raw = make_raw(six_ind())
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        t = len(panel["dates"]) - 1
        order = np.argsort(-fac["score"][:, t])
        # A 动量最高应排第一（同波动同拥挤度下）
        self.assertEqual(panel["inds"][order[0]], "A")
        self.assertEqual(panel["inds"][order[-1]], "F")

    def test_zscore_standardized(self):
        lf = pd.DataFrame({"date": ["2020-01-01"] * 4 + ["2020-01-02"] * 4,
                           "ind": [1, 2, 3, 4, 1, 2, 3, 4],
                           "x": [1.0, 2.0, 3.0, 4.0, -1.0, -2.0, 1.0, 2.0]})
        z = E._xs_z(lf, "x")
        g = lf.assign(z=z).groupby("date")["z"]
        for _, s in g:
            self.assertAlmostEqual(float(s.mean()), 0.0, places=10)
            self.assertAlmostEqual(float(s.std(ddof=0)), 1.0, places=10)


class TestSelect(unittest.TestCase):
    def _mk(self, scores, held_idx, n=6):
        T = 1
        score = np.full((n, T), np.nan)
        for i, s in enumerate(scores):
            score[i, 0] = s
        pooled = np.full((n, T), True)
        cur = np.zeros(n)
        for i in held_idx:
            cur[i] = 0.25
        return score, pooled, cur

    def test_topN_empty_holdings(self):
        score, pooled, cur = self._mk([1.0, 0.9, 0.8, 0.7, 0.3, 0.2], [])
        sel = E.select_target(0, cur, score, pooled)
        self.assertEqual(sorted(sel), [0, 1, 2, 3])

    def test_swap_threshold_blocks_weak_entrant(self):
        # 持仓 {0}（score 1.0）；候选 1 得 1.1（<1.15 门槛）→ 不入；候选 2 得 1.3（≥1.15）→ 入
        score, pooled, cur = self._mk([1.0, 1.1, 1.3, 0.5, 0.4, 0.3], [0])
        sel = E.select_target(0, cur, score, pooled)
        self.assertIn(0, sel)
        self.assertNotIn(1, sel)   # 未达 15% 优势，不换
        self.assertIn(2, sel)      # 达 15% 优势，换入

    def test_weak_entrant_leaves_slot_unfilled(self):
        # 持仓 {0}（score 1.0），候选都达不到 1.15 门槛 → 宁缺毋滥，不多持
        score, pooled, cur = self._mk([1.0, 1.1, 1.05, 1.02, 0.9, 0.8], [0])
        sel = E.select_target(0, cur, score, pooled)
        self.assertEqual(sel, [0])

    def test_stay_top60(self):
        # 持仓 {0,1,2,3} 全部在前 60%（0.5 分位）→ 留仓观察，弱行业 4/5 不入
        score, pooled, cur = self._mk([2.0, 1.9, 1.8, 1.7, 0.3, 0.2], [0, 1, 2, 3])
        sel = E.select_target(0, cur, score, pooled)
        self.assertEqual(sorted(sel), [0, 1, 2, 3])

    def test_weak_holding_replaced_by_qualified_entrant(self):
        # 持仓 {5}（排名最末、非前 60%）且存在合格新进入者 → 被替换
        score, pooled, cur = self._mk([2.0, 1.9, 1.8, 1.7, 0.5, 0.4], [5])
        sel = E.select_target(0, cur, score, pooled)
        self.assertNotIn(5, sel)
        self.assertEqual(sorted(sel), [0, 1, 2, 3])

    def test_turnover_cap_zero_budget_keeps_positions(self):
        # 换手预算耗尽时调仓不得清仓：持仓原样保留（防止"加速用光预算→月末全卖"事故）
        weights = np.array([0.25, 0.25, 0.25, 0.25, 0.0, 0.0])
        sel = [0, 1, 4, 5]   # 计划换出 2/3，换入 4/5
        score = np.array([3.0, 2.0, 1.0, 0.5, 2.8, 2.5])
        base, gross, turn = E.apply_turnover_cap(weights, sel, score, 0.0, 0.25)
        np.testing.assert_array_equal(base, weights)   # 一分不动
        self.assertEqual(turn, 0.0)

    def test_turnover_cap_partial_swap_by_score(self):
        # 预算 0.25 单边换手：只能成对换一组——最高分新买入(4) × 最低分卖出(3)，其余保留
        weights = np.array([0.25, 0.25, 0.25, 0.25, 0.0, 0.0])
        sel = [0, 1, 4, 5]   # 保留 0/1，换出 2/3，换入 4/5
        score = np.array([3.0, 2.9, 1.0, 0.5, 2.8, 2.7])
        base, gross, turn = E.apply_turnover_cap(weights, sel, score, 0.25, 0.25)
        np.testing.assert_allclose(base, [0.25, 0.25, 0.25, 0.0, 0.25, 0.0], atol=1e-9)
        self.assertAlmostEqual(turn, 0.25, places=6)
        self.assertAlmostEqual(base.sum(), 1.0, places=9)   # 暴露不超过 100%

    def test_turnover_cap_no_double_scale_on_blocked_rebal(self):
        # 回归：预算拦截调仓时，返回的必须是"已缩放"的现状持仓，不得叠加目标缩放
        # （旧逻辑把 4×0.125 再乘 desired 0.5 → 4×0.0625，双重缩放）
        weights = np.array([0.125, 0.125, 0.125, 0.125, 0.0, 0.0])
        sel = [1, 2, 3, 4]   # 换出 0、换入 4
        score = np.array([0.5, 2.9, 2.8, 2.7, 3.0, 0.4])
        base, gross, turn = E.apply_turnover_cap(weights, sel, score, 0.166, 0.125)
        # 换仓成本 (0.125+0.125)/2=0.125 ≤ 0.166 → 应完成 0→4 成对换仓，其余保持
        np.testing.assert_allclose(base, [0.0, 0.125, 0.125, 0.125, 0.125, 0.0], atol=1e-9)
        self.assertAlmostEqual(base.sum(), 0.5, places=9)   # 保持原暴露，不叠加


class TestBacktest(unittest.TestCase):
    def test_t_plus_1_execution(self):
        raw = make_raw(six_ind())
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        bt = E.backtest(panel, fac)
        me = E._month_end_flags(panel["dates"])
        monthly = [t for t in bt["trades"] if t["reason"] == "月度轮动"]
        self.assertTrue(len(monthly) > 0)
        # 成交日必须是某个月末决策日的下一交易日
        exec_dates = {pd.Timestamp(t["date"]) for t in monthly}
        for d in exec_dates:
            i = panel["dates"].get_loc(d)
            self.assertTrue(me[i - 1], f"{d} 前一交易日应是月末决策日")

    def test_stop_loss_exit(self):
        # 确定性无噪声路径：A 高动量必然首月入选，随后 5 月初急跌触发止损
        # （A 从 2018-05-07 起每日 -4%，约 6 个交易日后跌破建仓成本 -12%）
        dates = pd.bdate_range("2015-01-02", "2022-12-30")
        n = len(dates)
        raw = {"etfs": {}, "csi": {}}
        drifts = {"A": 0.002, "B": 0.0004, "C": 0.0003, "D": 0.0003, "E": 0.0002, "F": 0.0001}
        for j, (k, dr) in enumerate(drifts.items()):
            ret = np.full(n, dr)
            if k == "A":
                crash = (dates >= pd.Timestamp("2018-05-07")) & (dates < pd.Timestamp("2018-05-21"))
                ret[crash] = -0.04
            close = 100 * np.cumprod(1 + ret)
            kl = []
            prev = close[0]
            for i, d in enumerate(dates):
                c = close[i]
                kl.append(f"{d.strftime('%Y-%m-%d')},{prev:.4f},{c:.4f},{max(prev,c)*1.002:.4f},"
                          f"{min(prev,c)*0.998:.4f},1000,5e8,1.0,0.0,0.0,1.5")
                prev = c
            raw["etfs"][f"11{j:04d}"] = {"industry": k, "name": f"ETF{k}", "klines": kl}
        csi_close = 3000 * np.cumprod(1 + np.full(n, 0.0003))
        raw["csi"] = {"H00300": [{"tradeDate": d.strftime("%Y%m%d"), "close": float(c)}
                                 for d, c in zip(dates, csi_close)],
                      "000300": [{"tradeDate": d.strftime("%Y%m%d"), "close": float(c / 1.5)}
                                 for d, c in zip(dates, csi_close)]}
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        bt = E.backtest(panel, fac)
        sl = [t for t in bt["trades"] if t["industry"] == "A" and t["reason"] == "止损"]
        self.assertTrue(len(sl) > 0, "A 崩盘后应触发单行业止损")

    def test_circuit_breaker_scales_down(self):
        # 先赢后输：前期行业普涨、后期行业普跌而沪深300 rally → 超额回撤触发熔断
        inds = {k: [(f"11{i}", 0.0006, 0.012, 1.5)] for i, k in enumerate(["A", "B", "C", "D", "E", "F"])}
        raw = make_raw(inds, csi_mode="rally", seed=3)
        dates = pd.bdate_range("2015-01-02", "2022-12-30")
        n = len(dates)
        # 覆盖：2018-01 起行业横盘走弱（漂移 -0.0006），CSI 持续上行
        rng = np.random.default_rng(3)
        for code in raw["etfs"]:
            ret = rng.normal(-0.0006, 0.012, n)
            close = 100 * np.cumprod(1 + ret)
            kl = []
            prev = close[0]
            for i, d in enumerate(dates):
                c = close[i]
                kl.append(f"{d.strftime('%Y-%m-%d')},{prev:.4f},{c:.4f},{max(prev,c)*1.002:.4f},"
                          f"{min(prev,c)*0.998:.4f},1000,5e8,1.0,0.0,0.0,1.5")
                prev = c
            raw["etfs"][code]["klines"] = kl
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        bt = E.backtest(panel, fac)
        cbs = [h for h in bt["holdings_history"] if h["cb"]]
        self.assertTrue(len(cbs) > 0, "超额回撤应触发熔断")
        self.assertEqual(cbs[0]["scale"], E.CB_EXPOSURE)

    def test_market_filter_spike(self):
        raw = make_raw(six_ind(), csi_mode="spike", seed=9)
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        bt = E.backtest(panel, fac)
        capped = [h for h in bt["holdings_history"] if h.get("mk_scale", 1.0) < 1.0]
        self.assertTrue(len(capped) > 0, "高波动率环境应触发市场状态降仓")
        self.assertLessEqual(capped[0]["mk_scale"], 0.7)

    def test_rebal_respects_market_cap_without_scale_change(self):
        # 回归：调仓日即使 scale 数值未变，新目标权重也必须乘以生效仓位系数；
        # 旧逻辑只在 scale 变化时应用，导致波动率仓位上限在调仓日静默失效、满仓运行。
        # 断言对象为当日目标暴露（tgt）——实际持仓因 T+1 执行天然滞后一天。
        raw = make_raw(six_ind(), csi_mode="spike", seed=9)
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        bt = E.backtest(panel, fac)
        for h in bt["holdings_history"]:
            expo = float(np.sum(h["weights"]))
            self.assertLessEqual(expo, 1.0 + 1e-9, f"{h['date']}: 实际暴露 {expo:.3f} > 100%")
            if h["tgt"] is not None:
                cap = (E.CB_EXPOSURE if h["cb"] else h["mk_scale"]) + 1e-9
                self.assertLessEqual(h["tgt"], cap,
                                     f"{h['date']}: 目标暴露 {h['tgt']:.3f} 超过上限 {cap:.3f}")

    def test_deterministic(self):
        raw = make_raw(six_ind())
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        b1 = E.backtest(panel, fac)
        b2 = E.backtest(panel, fac)
        np.testing.assert_array_equal(b1["nav"], b2["nav"])
        self.assertEqual(b1["trades"], b2["trades"])
        self.assertEqual(b1["metrics"], b2["metrics"])

    def test_metrics_sanity(self):
        raw = make_raw(six_ind())
        panel = E.build_panel(raw)
        fac = E.compute_factors(panel)
        bt = E.backtest(panel, fac)
        m = bt["metrics"]
        self.assertTrue(m["n_trades"] > 0)
        self.assertGreater(m["total"], -1.0)
        self.assertLessEqual(m["mdd"], 0.0)
        self.assertGreater(m["years"], 4)
        self.assertIn("2018", m["annual"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
