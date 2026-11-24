"""核查算法单元测试：python -m unittest tests.test_reconcile -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.reconcile import (  # noqa: E402
    MOD,
    analyze_counts,
    candidate_range,
    interval_options,
    range_between,
    verify,
)

# 与 demo/make_demo.py 保持一致的演示账
COUNTS = [12345, 13345, 17809, 37809, 42273, 26737]
# 真实间隔：1000, 4464, 20000, 70000, 50000（前 4 个模糊，最后 1 个固定）
GAPS = [1000, 4464, 20000, 70000, 50000]
UTC = [1700000000000]
for g in GAPS:
    UTC.append(UTC[-1] + g)


def records_of(counts):
    rows = analyze_counts(counts)
    return {r["event_no"]: (r["prefix_min"], r["prefix_amb"]) for r in rows}


class TestIntervalOptions(unittest.TestCase):
    def test_ambiguous_small_residue(self):
        low, high, amb = interval_options(12345, 13345)  # r=1000
        self.assertEqual((low, high), (1000, 1000 + MOD))
        self.assertTrue(amb)

    def test_ambiguous_wrapped_residue(self):
        # 13345 -> 17809：r=4464，候选 4464 与 70000
        low, high, amb = interval_options(13345, 17809)
        self.assertEqual((low, high), (4464, 70000))
        self.assertTrue(amb)

    def test_fixed_large_residue(self):
        # 42273 -> 26737：r=50000 > 24464，唯一候选 50000
        low, high, amb = interval_options(42273, 26737)
        self.assertEqual((low, high), (50000, 50000))
        self.assertFalse(amb)

    def test_zero_residue_forces_full_turn(self):
        low, high, amb = interval_options(100, 100)
        self.assertEqual((low, high, amb), (MOD, MOD, False))


class TestRangeBetween(unittest.TestCase):
    def setUp(self):
        self.records = records_of(COUNTS)

    def test_adjacent_ambiguous(self):
        lo, hi = range_between(self.records, 1, 2)
        self.assertEqual((lo, hi), (1000, 1000 + MOD))

    def test_span_four_ambiguous(self):
        # 事件1->5 跨 4 个间隔，全部模糊
        lo, hi = range_between(self.records, 1, 5)
        self.assertEqual(lo, 29928)
        self.assertEqual(hi, 29928 + 4 * MOD)

    def test_span_including_fixed(self):
        # 事件1->6 含 4 模糊 + 1 固定，模糊条数仍为 4
        lo, hi = range_between(self.records, 1, 6)
        self.assertEqual(lo, 79928)
        self.assertEqual(hi, 79928 + 4 * MOD)

    def test_true_delta_in_progression(self):
        lo, hi = range_between(self.records, 3, 5)
        delta = UTC[4] - UTC[2]  # 90000
        self.assertTrue(lo <= delta <= hi)
        self.assertEqual((delta - lo) % MOD, 0)


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.records = records_of(COUNTS)

    def test_insufficient(self):
        result = verify(self.records, [
            {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
            {"id": 2, "event_no": 3, "utc_ms": UTC[2]},
        ])
        self.assertEqual(result["status"], "insufficient")

    def test_consistent_ledger(self):
        anchors = [
            {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
            {"id": 2, "event_no": 3, "utc_ms": UTC[2]},
            {"id": 3, "event_no": 5, "utc_ms": UTC[4]},
        ]
        result = verify(self.records, anchors)
        self.assertEqual(result["status"], "consistent")
        self.assertTrue(all(p["ok"] for p in result["pairs"]))

    def test_single_error_finds_only_middle_candidate(self):
        # 事件 3 被多抄三圈（超过 1->3 与 3->5 区间的最大模糊度），
        # 此时端点锚点各自的相邻对无法补救，只应报事件 3
        anchors = [
            {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
            {"id": 2, "event_no": 3, "utc_ms": UTC[2] + 3 * MOD},
            {"id": 3, "event_no": 5, "utc_ms": UTC[4]},
        ]
        result = verify(self.records, anchors)
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual([c["event_no"] for c in result["candidates"]], [3])
        cand = result["candidates"][0]
        self.assertEqual(cand["step"], MOD)
        # 正确时刻必须是候选之一
        self.assertTrue(cand["first"] <= UTC[2] <= cand["last"])
        self.assertEqual((UTC[2] - cand["first"]) % MOD, 0)
        # 原错误时刻不在候选集合内
        self.assertFalse(cand["first"] <= UTC[2] + 3 * MOD <= cand["last"])

    def test_first_anchor_candidate(self):
        anchors = [
            {"id": 1, "event_no": 1, "utc_ms": UTC[0] + 2 * MOD},
            {"id": 2, "event_no": 3, "utc_ms": UTC[2]},
            {"id": 3, "event_no": 5, "utc_ms": UTC[4]},
        ]
        result = verify(self.records, anchors)
        self.assertEqual(result["status"], "inconsistent")
        events_fixable = {c["event_no"] for c in result["candidates"]}
        self.assertIn(1, events_fixable)
        cand = next(c for c in result["candidates"] if c["event_no"] == 1)
        self.assertTrue(cand["first"] <= UTC[0] <= cand["last"])

    def test_last_anchor_candidate(self):
        # 末锚点少抄两圈，其唯一相邻对（3->5）必然越界，只能改末锚点
        anchors = [
            {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
            {"id": 2, "event_no": 3, "utc_ms": UTC[2]},
            {"id": 3, "event_no": 5, "utc_ms": UTC[4] - 2 * MOD},
        ]
        result = verify(self.records, anchors)
        events_fixable = {c["event_no"] for c in result["candidates"]}
        self.assertIn(5, events_fixable)

    def test_double_error_no_single_fix(self):
        # 四锚点 1,2,4,6；事件 1 少抄四圈、事件 2 多抄一圈。
        # 1->2 这一对无论改后面哪个锚点都仍然对不上，单处更正无法对平。
        anchors = [
            {"id": 1, "event_no": 1, "utc_ms": UTC[0] - 4 * MOD},
            {"id": 2, "event_no": 2, "utc_ms": UTC[1] + MOD},
            {"id": 3, "event_no": 4, "utc_ms": UTC[3]},
            {"id": 4, "event_no": 6, "utc_ms": UTC[5]},
        ]
        result = verify(self.records, anchors)
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(result["candidates"], [])
        self.assertIn("单处更正无法对平", result["message"])

    def test_candidate_range_directly(self):
        rng = candidate_range(
            self.records, 3,
            ((1, UTC[0]), (5, UTC[4])),
        )
        self.assertIsNotNone(rng)
        self.assertEqual(rng["step"], MOD)
        self.assertTrue(rng["first"] <= UTC[2] <= rng["last"])
        self.assertEqual(rng["count"], (rng["last"] - rng["first"]) // MOD + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
