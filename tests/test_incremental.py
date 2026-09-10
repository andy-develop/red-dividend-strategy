#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增量更新机制单测（v1.1）：
基线重建 / klines 合并去重 / 前复权刻度检测 / 缺口检测 / csi 合并 / 归档滚动清理。"""
import gzip, json, os, sys, tempfile, unittest
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import sector_update as S


def kl(d, px):
    # 构造 klines 行：date,open,close,high,low,volume,amount,amp,pct,chg,turn
    return f"{d},10,{px},11,9,1000,1e7,2.0,1.0,0.1,3.0"


class TestMerge(unittest.TestCase):
    def test_merge_klines_dedup_sorted(self):
        old = [kl("2026-09-01", 10.0), kl("2026-09-02", 10.2)]
        new = [kl("2026-09-02", 10.3), kl("2026-09-03", 10.5)]   # 同日新覆盖旧
        m = S.merge_klines(old, new)
        self.assertEqual([r.split(",")[0] for r in m], ["2026-09-01", "2026-09-02", "2026-09-03"])
        self.assertEqual(m[1].split(",")[2], "10.3")   # 9/2 用新值

    def test_gap_days_normal_and_large(self):
        old = [kl("2026-09-04", 10.0)]
        new = [kl("2026-09-07", 10.2)]    # 周末间隔 3 天
        self.assertLessEqual(S.kline_gap_days(old, new), 10)
        new_bad = [kl("2026-09-22", 10.2)]  # 间隔 18 天 → 缺口
        self.assertGreater(S.kline_gap_days(old, new_bad), 10)

    def test_scale_jump_detects_ex_dividend(self):
        old = [kl("2026-09-04", 10.0)]
        new = [kl("2026-09-07", 8.5)]     # -15% 跳变 → 除权，刻度失效
        self.assertTrue(S.kline_scale_jump(old, new))
        new_ok = [kl("2026-09-07", 10.1)]  # +1% 正常
        self.assertFalse(S.kline_scale_jump(old, new_ok))

    def test_merge_csi(self):
        old = [{"tradeDate": "2026-09-01", "close": 100.0}, {"tradeDate": "2026-09-02", "close": 101.0}]
        new = [{"tradeDate": "2026-09-02", "close": 101.5}, {"tradeDate": "2026-09-03", "close": 102.0}]
        m = S.merge_csi(old, new)
        self.assertEqual([r["tradeDate"] for r in m], ["2026-09-01", "2026-09-02", "2026-09-03"])
        self.assertEqual(m[1]["close"], 101.5)


class TestRebuild(unittest.TestCase):
    def _write_gz(self, path, obj):
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_rebuild_week_plus_incr(self):
        with tempfile.TemporaryDirectory() as td:
            old_arch = S.ARCHIVE_DIR
            S.ARCHIVE_DIR = td
            try:
                week = {"fetched_at": "t", "etfs": {
                    "512760": {"industry": "半导体", "name": "x", "klines": [kl("2026-09-01", 10.0), kl("2026-09-04", 10.1)]}},
                    "csi": {"H00300": [{"tradeDate": "2026-09-04", "close": 100.0}]}, "failures": []}
                self._write_gz(os.path.join(td, "sector-week-20260904.json.gz"), week)
                incr1 = {"date": "20260907", "base": "sector-week-20260904.json.gz",
                         "etfs": {"512760": [kl("2026-09-07", 10.2), kl("2026-09-08", 10.3)]},
                         "csi": {"H00300": [{"tradeDate": "2026-09-07", "close": 101.0}, {"tradeDate": "2026-09-08", "close": 102.0}]}}
                self._write_gz(os.path.join(td, "sector-incr-20260907.json.gz"), incr1)
                incr2 = {"date": "20260908", "base": "sector-week-20260904.json.gz",
                         "etfs": {"512760": [kl("2026-09-08", 10.4)]}, "csi": {}}
                self._write_gz(os.path.join(td, "sector-incr-20260908.json.gz"), incr2)
                raw, day, mode = S.rebuild_base()
                self.assertEqual(mode, "incr")
                self.assertEqual(day, date(2026, 9, 8))
                klines = raw["etfs"]["512760"]["klines"]
                self.assertEqual([r.split(",")[0] for r in klines],
                                 ["2026-09-01", "2026-09-04", "2026-09-07", "2026-09-08"])
                self.assertEqual(klines[-1].split(",")[2], "10.4")   # 增量覆盖
                self.assertEqual([r["tradeDate"] for r in raw["csi"]["H00300"]], ["2026-09-04", "2026-09-07", "2026-09-08"])
            finally:
                S.ARCHIVE_DIR = old_arch

    def test_rebuild_legacy_migrate(self):
        with tempfile.TemporaryDirectory() as td:
            old_arch = S.ARCHIVE_DIR
            S.ARCHIVE_DIR = td
            try:
                legacy = {"fetched_at": "t", "etfs": {"512760": {"industry": "半导体", "name": "x",
                          "klines": [kl("2026-09-01", 10.0)]}}, "csi": {}, "failures": []}
                self._write_gz(os.path.join(td, "sector-raw-20260910.json.gz"), legacy)
                raw, day, mode = S.rebuild_base()
                self.assertEqual(mode, "migrate")
                self.assertEqual(day, date(2026, 9, 10))
                self.assertEqual(len(raw["etfs"]), 1)
            finally:
                S.ARCHIVE_DIR = old_arch

    def test_archive_rolling_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            old_arch = S.ARCHIVE_DIR
            S.ARCHIVE_DIR = td
            try:
                for i in range(1, 6):
                    p = os.path.join(td, f"sector-incr-2026090{i}.json.gz")
                    with gzip.open(p, "wt", encoding="utf-8") as f:
                        json.dump({"date": f"2026090{i}"}, f, ensure_ascii=False)
                S.KEEP_INCR = 3
                S.archive_incremental("incr", {}, {"date": "x", "etfs": {"a": []}, "csi": {}})
                remain = sorted(os.listdir(td))
                self.assertEqual(len([x for x in remain if x.startswith("sector-incr-")]), 3)
            finally:
                S.ARCHIVE_DIR = old_arch


if __name__ == "__main__":
    unittest.main()
