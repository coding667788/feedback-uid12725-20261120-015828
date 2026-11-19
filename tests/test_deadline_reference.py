"""期限计算核对：生产实现与独立逐分钟参考实现逐分钟对照。

参考实现见 tests/reference_minute.py（不导入生产代码，独立编写）。
"""
from __future__ import annotations

import unittest

from adjudicator import domain
from tests.reference_minute import ref_coverage_count, ref_deadline


def _entries(spec):
    return [domain.normalize_schedule_entry(e) for e in spec]


class DeadlineReferenceCrossCheck(unittest.TestCase):
    def _cross_check(self, spec, from_minute, required=30):
        entries = _entries(spec)
        prod = domain.compute_deadline(entries, from_minute, required)
        ref, timeline = ref_deadline(
            [
                {
                    "group": e["group"],
                    "start_minute": e["start"],
                    "end_minute": e["end"],
                }
                for e in entries
            ],
            from_minute,
            required,
        )
        self.assertEqual(prod, ref, f"期限不一致；逐分钟轨迹: {timeline}")
        if prod is not None:
            # 截止前覆盖分钟必须恰好等于 required
            self.assertEqual(
                domain.count_coverage_minutes(entries, from_minute, prod), required
            )
            self.assertEqual(
                ref_coverage_count(
                    [
                        {
                            "group": e["group"],
                            "start_minute": e["start"],
                            "end_minute": e["end"],
                        }
                        for e in entries
                    ],
                    from_minute,
                    prod,
                ),
                required,
            )
        return prod, timeline

    def test_small_hand_built_sample_minute_by_minute(self):
        """独立小样本：from=40，双组覆盖恰为 45..74，截止必须是 75。"""
        spec = [
            {"person": "alice", "group": "G1", "start_minute": 40, "end_minute": 80},
            # 同组重叠人员：分钟仍只计一次（不按人头累计）
            {"person": "carol", "group": "G1", "start_minute": 50, "end_minute": 60},
            {"person": "bob", "group": "G2", "start_minute": 45, "end_minute": 80},
        ]
        entries = _entries(spec)
        deadline, timeline = self._cross_check(spec, 40)
        self.assertEqual(deadline, 75)
        # 逐分钟断言：40-44 只有 G1（非覆盖），45 起为覆盖
        coverage_minutes = [t["minute"] for t in timeline if t["coverage"]]
        self.assertEqual(coverage_minutes[:5], [45, 46, 47, 48, 49])
        # 扫到第 30 个覆盖分钟（74）即停止并返回截止 75
        self.assertEqual(coverage_minutes[-1], 74)
        self.assertEqual(len(coverage_minutes), 30)
        # 全程实际有 35 个覆盖分钟（45..79），用区间计数独立核对
        self.assertEqual(
            domain.count_coverage_minutes(entries, 40, 80), 35
        )
        # carol 当班的 50..59 不产生额外计数
        at_55 = next(t for t in timeline if t["minute"] == 55)
        self.assertEqual(at_55["groups"], ["G1", "G2"])
        self.assertTrue(at_55["coverage"])

    def test_empty_shift_no_deadline(self):
        deadline, _ = self._cross_check([], 40)
        self.assertIsNone(deadline)

    def test_only_one_group_never_covered(self):
        spec = [
            {"person": "g1a", "group": "G1", "start_minute": 40, "end_minute": 200},
            {"person": "g1b", "group": "G1", "start_minute": 60, "end_minute": 120},
        ]
        deadline, _ = self._cross_check(spec, 40)
        self.assertIsNone(deadline)

    def test_insufficient_coverage_no_deadline(self):
        spec = [
            {"person": "dan", "group": "G1", "start_minute": 100, "end_minute": 110},
            {"person": "eva", "group": "G2", "start_minute": 100, "end_minute": 105},
        ]
        deadline, timeline = self._cross_check(spec, 100)
        self.assertIsNone(deadline)
        self.assertEqual(sum(1 for t in timeline if t["coverage"]), 5)

    def test_exactly_thirty_coverage_minutes(self):
        # 恰好 30 个覆盖分钟：最后一分钟 69，截止 70
        spec = [
            {"person": "a", "group": "G1", "start_minute": 40, "end_minute": 70},
            {"person": "b", "group": "G2", "start_minute": 40, "end_minute": 70},
        ]
        deadline, _ = self._cross_check(spec, 40)
        self.assertEqual(deadline, 70)

    def test_gap_then_coverage_resumes(self):
        # G2 中途离岗造成覆盖空洞，两边覆盖分钟要拼接累计
        spec = [
            {"person": "a", "group": "G1", "start_minute": 0, "end_minute": 100},
            {"person": "b", "group": "G2", "start_minute": 0, "end_minute": 20},
            {"person": "c", "group": "G2", "start_minute": 30, "end_minute": 60},
        ]
        deadline, timeline = self._cross_check(spec, 0)
        # 覆盖：0..19（20 个）+ 30..39（再 10 个）→ 第 30 个覆盖分钟是 39 → 截止 40
        self.assertEqual(deadline, 40)
        gap = [t for t in timeline if t["minute"] in (20, 25, 29)]
        self.assertTrue(all(not t["coverage"] for t in gap))

    def test_cross_day_entries(self):
        spec = [
            {
                "person": "finn",
                "group": "G3",
                "start_minute": 1430,
                "end_minute": 15,
                "cross_day": True,
            },
            {"person": "kim", "group": "G4", "start_minute": 1430, "end_minute": 1460},
        ]
        entries = _entries(spec)
        # 双组覆盖 1430..1455 共 25 分钟 → 不足 30
        self.assertIsNone(domain.compute_deadline(entries, 1430, 30))
        # 要求降到 25 → 第 25 个覆盖分钟 1454，截止 1455
        self.assertEqual(domain.compute_deadline(entries, 1430, 25), 1455)

    def test_overlapping_multi_group_dedup_by_minute(self):
        # 三组人员重叠：每分钟仍只算 1 个覆盖分钟
        spec = [
            {"person": "a", "group": "G1", "start_minute": 0, "end_minute": 40},
            {"person": "b", "group": "G2", "start_minute": 0, "end_minute": 40},
            {"person": "c", "group": "G3", "start_minute": 0, "end_minute": 40},
        ]
        deadline, _ = self._cross_check(spec, 0)
        self.assertEqual(deadline, 30)


if __name__ == "__main__":
    unittest.main()
