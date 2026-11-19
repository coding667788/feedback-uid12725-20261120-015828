"""领域规则单元测试：候选边、连通分量、排班区间与边界条件。"""
from __future__ import annotations

import unittest

from adjudicator import domain
from adjudicator.domain import RuleError, ScheduleError


def rule_index(rules):
    return domain.build_rule_index(rules)


class EdgeRuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = rule_index(
            [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10}]
        )

    def _legal(self, **overrides):
        base = dict(
            channel_a="CH1",
            channel_b="CH2",
            site_a="S1",
            site_b="S1",
            area_a="Z1",
            area_b="Z1",
            type_a="SMOKE",
            type_b="TEMP",
            minute_a=20,
            minute_b=25,
        )
        base.update(overrides)
        return domain.edge_is_legal(rule_index=self.rules, **base)

    def test_diff_equals_max_is_legal(self):
        # 与最大时差恰等：<= 判定，合法
        self.assertTrue(self._legal(minute_a=20, minute_b=30))

    def test_diff_one_over_max_is_illegal(self):
        self.assertFalse(self._legal(minute_a=20, minute_b=31))

    def test_same_channel_never_edges(self):
        # 同通道即使时差合规也不建边，只能经跨通路边间接归并
        self.assertFalse(self._legal(channel_a="CH1", channel_b="CH1"))

    def test_different_site_no_edge(self):
        self.assertFalse(self._legal(site_a="S1", site_b="S2"))

    def test_different_area_no_edge(self):
        self.assertFalse(self._legal(area_a="Z1", area_b="Z2"))

    def test_undeclared_type_pair_no_edge(self):
        self.assertFalse(self._legal(type_a="SMOKE", type_b="VIBRATION"))

    def test_same_type_pair_not_declared(self):
        # 规则表只声明 SMOKE-TEMP，同类型配对查不到
        self.assertFalse(self._legal(type_a="TEMP", type_b="TEMP"))

    def test_rule_table_validation(self):
        with self.assertRaises(RuleError):
            rule_index([{"type_a": "A", "type_b": "A", "max_diff": 5}])
        with self.assertRaises(RuleError):
            rule_index([{"type_a": "A", "type_b": "B", "max_diff": -1}])
        with self.assertRaises(RuleError):
            rule_index([])
        with self.assertRaises(RuleError):
            rule_index(
                [
                    {"type_a": "A", "type_b": "B", "max_diff": 5},
                    {"type_a": "B", "type_b": "A", "max_diff": 9},
                ]
            )


class ComponentTests(unittest.TestCase):
    def test_bridge_merges_two_components_via_third_alert(self):
        # a1--a3--a2：第三条告警把两个暂存事件合并
        active = ["a1", "a2", "a3"]
        adjacency = {"a1": {"a3"}, "a2": {"a3"}, "a3": {"a1", "a2"}}
        comps = domain.connected_components(active, adjacency)
        self.assertEqual(comps, [["a1", "a2", "a3"]])

    def test_isolated_alert_is_its_own_component(self):
        active = ["x", "y"]
        adjacency = {"x": set(), "y": set()}
        self.assertEqual(
            domain.connected_components(active, adjacency), [["x"], ["y"]]
        )

    def test_same_channel_alerts_only_join_via_cross_channel_path(self):
        # x1(CH1) 与 x3(CH1) 之间没有边；x2(CH2) 只连 x1 → x3 保持孤立
        active = ["x1", "x2", "x3"]
        adjacency = {"x1": {"x2"}, "x2": {"x1"}, "x3": set()}
        comps = domain.connected_components(active, adjacency)
        self.assertEqual(comps, [["x1", "x2"], ["x3"]])

    def test_deterministic_ordering(self):
        active = ["b", "a", "d", "c"]
        adjacency = {k: set() for k in active}
        self.assertEqual(
            domain.connected_components(active, adjacency),
            [["a"], ["b"], ["c"], ["d"]],
        )


class ScheduleEntryTests(unittest.TestCase):
    def test_half_open_interval(self):
        e = domain.normalize_schedule_entry(
            {"person": "p", "group": "G", "start_minute": 10, "end_minute": 12}
        )
        self.assertIn("G", domain.groups_on_duty([e], 10))
        self.assertIn("G", domain.groups_on_duty([e], 11))
        self.assertEqual(domain.groups_on_duty([e], 12), set())
        self.assertEqual(domain.groups_on_duty([e], 9), set())

    def test_cross_day_normalized(self):
        e = domain.normalize_schedule_entry(
            {
                "person": "p",
                "group": "G",
                "start_minute": 1430,
                "end_minute": 15,
                "cross_day": True,
            }
        )
        self.assertEqual(e["end"], 1455)
        self.assertIn("G", domain.groups_on_duty([e], 1440))
        self.assertEqual(domain.groups_on_duty([e], 1455), set())

    def test_overlapping_shifts_same_person_conflicting_group_rejected(self):
        entries = [
            {"person": "p", "group": "G1", "start": 0, "end": 10},
            {"person": "p", "group": "G2", "start": 5, "end": 15},
        ]
        with self.assertRaises(ScheduleError):
            domain.persons_on_duty(entries, 7)

    def test_invalid_entries(self):
        with self.assertRaises(ScheduleError):
            domain.normalize_schedule_entry(
                {"person": "p", "group": "G", "start_minute": 10, "end_minute": 10}
            )
        with self.assertRaises(ScheduleError):
            domain.normalize_schedule_entry(
                {"person": "p", "group": "G", "start_minute": -1, "end_minute": 5}
            )


if __name__ == "__main__":
    unittest.main()
