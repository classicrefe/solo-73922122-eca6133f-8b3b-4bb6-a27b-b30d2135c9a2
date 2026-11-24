"""漏导核算算法单元测试：python -m unittest tests.test_missing -v

DP 最优性用小规模穷举（枚举所有回绕圈分配）交叉验证；
演示账与 demo/make_demo.py、tests/test_reconcile.py 保持一致。
"""

import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.missing import (  # noqa: E402
    MAX_IMPORTED_EVENTS,
    MAX_SPAN_MS,
    MissingError,
    _interval_profile,
    _solve_subsegment,
    compute_missing,
    missing_for_elapsed,
)
from app.reconcile import MAX_GAP, MOD  # noqa: E402

COUNTS = [12345, 13345, 17809, 37809, 42273, 26737]
GAPS = [1000, 4464, 20000, 70000, 50000]
UTC = [1700000000000]
for g in GAPS:
    UTC.append(UTC[-1] + g)

EVENTS = [{"event_no": i + 1, "count": c} for i, c in enumerate(COUNTS)]


def profiles_of(counts):
    return [
        _interval_profile(counts[i], counts[i + 1]) for i in range(len(counts) - 1)
    ]


def feasible_missing(profile, turns):
    """与 app.missing._missing_for_turns 同语义的独立参考实现（穷举 m）。"""
    base = profile["base"]
    d = base + turns * MOD
    # turns 个 +MOD 要分配到 m+1 个小间隔：m+1 >= turns，即 pieces >= turns；
    # 固定间隔靠足够多的漏导也能满足，只是 m=0 时 turns 必须为 0。
    pieces_min = max(1, turns)
    m = 0
    while True:
        pieces = m + 1
        if pieces >= pieces_min and pieces * base <= d <= pieces * MAX_GAP:
            return m
        if pieces * MAX_GAP >= d and pieces >= pieces_min:
            # 上界与条数已满足，下界只会随 pieces 增大而更紧，无解
            return None
        m += 1


def brute_force_min(profiles, elapsed):
    """穷举每个间隔的可行回绕圈（圈数上界取总圈数），返回最少漏导。"""
    base_sum = sum(p["base"] for p in profiles)
    if elapsed < base_sum or (elapsed - base_sum) % MOD != 0:
        return None
    total_turns = (elapsed - base_sum) // MOD
    choices = [range(total_turns + 1) for _ in profiles]
    best = None
    for combo in itertools.product(*choices):
        if sum(combo) != total_turns:
            continue
        cost = 0
        for p, k in zip(profiles, combo):
            m = feasible_missing(p, k)
            if m is None:
                break
            cost += m
        else:
            best = cost if best is None else min(best, cost)
    return best


class TestMissingForElapsed(unittest.TestCase):
    def test_within_one_gap_means_zero_missing(self):
        self.assertEqual(missing_for_elapsed(1), 0)
        self.assertEqual(missing_for_elapsed(MAX_GAP), 0)

    def test_just_over_one_gap_needs_one(self):
        self.assertEqual(missing_for_elapsed(MAX_GAP + 1), 1)
        self.assertEqual(missing_for_elapsed(2 * MAX_GAP), 1)
        self.assertEqual(missing_for_elapsed(2 * MAX_GAP + 1), 2)

    def test_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            missing_for_elapsed(0)


class TestSubsegmentDP(unittest.TestCase):
    def setUp(self):
        self.profiles = profiles_of(COUNTS)

    def test_real_timeline_zero_missing(self):
        # 真实时间线：不额外加圈即可对上，漏导应为 0
        result = _solve_subsegment(self.profiles, UTC[-1] - UTC[0])
        self.assertIsNotNone(result)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(sum(it["missing"] for it in result["intervals"]), 0)

    def test_extra_turns_demo_account(self):
        # 真实时间线本身对应 K=1（一个模糊间隔取了 +MOD 候选），
        # 再整体加 4 圈 -> 总 K=5，最少 1 条漏导（固定间隔 int5 不能加圈，
        # 额外圈压到模糊间隔，其中一个间隔需 1 条漏导承载 2 圈）
        elapsed = UTC[-1] - UTC[0] + 4 * MOD
        result = _solve_subsegment(self.profiles, elapsed)
        self.assertIsNotNone(result)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["extra_turns"], 5)
        # 逐间隔安排的耗时之和必须等于锚点时差
        self.assertEqual(
            sum(it["elapsed_ms"] for it in result["intervals"]), elapsed
        )
        # 每条间隔自洽：m 条漏导把 d 分成 m+1 个同余、且 1..90000 的小间隔
        for it in result["intervals"]:
            pieces = it["missing"] + 1
            self.assertLessEqual(pieces * it["base_ms"], it["elapsed_ms"])
            self.assertLessEqual(it["elapsed_ms"], pieces * MAX_GAP)

    def test_impossible_below_minimum(self):
        self.assertIsNone(_solve_subsegment(self.profiles, 1))

    def test_impossible_residue_mismatch(self):
        base = sum(p["base"] for p in self.profiles)
        self.assertIsNone(_solve_subsegment(self.profiles, base + 1))

    def test_impossible_too_many_turns(self):
        # 超过 30 分钟物理上限的子段耗时
        base = sum(p["base"] for p in self.profiles)
        huge = MAX_SPAN_MS // MOD + 1
        self.assertIsNone(
            _solve_subsegment(
                self.profiles, base + huge * MOD, max_elapsed=MAX_SPAN_MS
            )
        )

    def test_without_max_elapsed_huge_span_still_realizable(self):
        # 不传 30 分钟上限时，纯计数规则下大耗时仍可由漏导承载
        base = sum(p["base"] for p in self.profiles)
        result = _solve_subsegment(self.profiles, base + 28 * MOD)
        self.assertIsNotNone(result)
        self.assertGreater(result["missing"], 0)

    def test_dp_matches_brute_force(self):
        base = sum(p["base"] for p in self.profiles)
        for k in range(8):  # 穷举空间 k^n，小规模即可
            elapsed = base + k * MOD
            self.assertEqual(
                _solve_subsegment(self.profiles, elapsed)["missing"],
                brute_force_min(self.profiles, elapsed),
            )

    def test_dp_matches_brute_force_random_accounts(self):
        import random

        rng = random.Random(20261124)
        for trial in range(60):
            n = rng.randint(2, 7)
            counts = [rng.randrange(MOD)]
            for _ in range(n - 1):
                counts.append(rng.randrange(MOD))
            profiles = profiles_of(counts)
            base = sum(p["base"] for p in profiles)
            for k in range(7):
                elapsed = base + k * MOD
                got = _solve_subsegment(profiles, elapsed)
                want = brute_force_min(profiles, elapsed)
                self.assertIsNotNone(got, (trial, counts, k))
                self.assertEqual(got["missing"], want, (trial, counts, k))


class TestComputeMissing(unittest.TestCase):
    def test_two_anchors_zero_missing(self):
        result = compute_missing(
            EVENTS,
            [
                {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
                {"id": 2, "event_no": 6, "utc_ms": UTC[5]},
            ],
        )
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["total_missing"], 0)
        self.assertEqual(len(result["subsegments"]), 1)
        self.assertEqual(len(result["subsegments"][0]["intervals"]), 5)

    def test_two_anchors_one_missing_with_breakdown(self):
        result = compute_missing(
            EVENTS,
            [
                {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
                {"id": 2, "event_no": 6, "utc_ms": UTC[5] + 4 * MOD},
            ],
        )
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["total_missing"], 1)
        intervals = result["subsegments"][0]["intervals"]
        # 逐间隔都带两端事件号、耗时、漏导条数
        for idx, it in enumerate(intervals):
            self.assertEqual(it["left_event"], idx + 1)
            self.assertEqual(it["right_event"], idx + 2)
            self.assertGreaterEqual(it["elapsed_ms"], 1)
            self.assertLessEqual(it["elapsed_ms"], 3 * MAX_GAP)
        self.assertEqual(sum(it["missing"] for it in intervals), 1)
        self.assertEqual(
            sum(it["elapsed_ms"] for it in intervals), UTC[5] - UTC[0] + 4 * MOD
        )

    def test_three_anchors_split_into_subsegments(self):
        # 第二段（事件 3->6）真实时间线自带 K=1，再加 2 圈到 K=3，
        # 固定间隔需 1 条漏导承载 1 个回绕圈；第一段保持 0 漏导
        result = compute_missing(
            EVENTS,
            [
                {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
                {"id": 2, "event_no": 3, "utc_ms": UTC[2]},
                {"id": 3, "event_no": 6, "utc_ms": UTC[5] + 2 * MOD},
            ],
        )
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(len(result["subsegments"]), 2)
        # 第一段 0 漏导，第二段 1 漏导
        self.assertEqual(result["subsegments"][0]["missing"], 0)
        self.assertEqual(result["subsegments"][1]["missing"], 1)
        self.assertEqual(result["total_missing"], 1)

    def test_impossible_negative_delta(self):
        result = compute_missing(
            EVENTS,
            [
                {"id": 1, "event_no": 1, "utc_ms": UTC[5]},
                {"id": 2, "event_no": 6, "utc_ms": UTC[0]},
            ],
        )
        self.assertEqual(result["status"], "impossible")
        self.assertIsNone(result["total_missing"])
        self.assertIn("核查锚点或计数", result["message"])
        self.assertEqual(result["subsegments"][-1]["reason"], "negative_delta")

    def test_impossible_residue_mismatch_reports_pair(self):
        result = compute_missing(
            EVENTS,
            [
                {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
                {"id": 2, "event_no": 6, "utc_ms": UTC[0] + 79929},
            ],
        )
        self.assertEqual(result["status"], "impossible")
        bad = result["subsegments"][-1]
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "residue_mismatch")

    def test_need_two_anchors(self):
        with self.assertRaises(MissingError):
            compute_missing(
                EVENTS, [{"id": 1, "event_no": 1, "utc_ms": UTC[0]}]
            )

    def test_span_limit_enforced(self):
        with self.assertRaises(MissingError):
            compute_missing(
                EVENTS,
                [
                    {"id": 1, "event_no": 1, "utc_ms": 0},
                    {"id": 2, "event_no": 6, "utc_ms": MAX_SPAN_MS + 1},
                ],
            )

    def test_event_count_limit_enforced(self):
        big = [
            {"event_no": i + 1, "count": i % MOD}
            for i in range(MAX_IMPORTED_EVENTS + 1)
        ]
        with self.assertRaises(MissingError):
            compute_missing(
                big,
                [
                    {"id": 1, "event_no": 1, "utc_ms": 0},
                    {"id": 2, "event_no": MAX_IMPORTED_EVENTS + 1,
                     "utc_ms": MAX_SPAN_MS},
                ],
            )

    def test_anchor_outside_segment_rejected(self):
        with self.assertRaises(MissingError):
            compute_missing(
                EVENTS,
                [
                    {"id": 1, "event_no": 1, "utc_ms": UTC[0]},
                    {"id": 2, "event_no": 7, "utc_ms": UTC[5]},  # 超出选段 1..6
                ],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
