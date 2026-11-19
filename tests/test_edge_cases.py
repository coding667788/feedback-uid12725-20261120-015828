"""补充边界测试：多类型对规则、跨日期限、回执证据与核验篡改发现。"""
from __future__ import annotations

import json
import unittest

from adjudicator import domain, service
from adjudicator.storage import Storage

MULTI_RULES = [
    {"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10},
    {"type_a": "VIBRATION", "type_b": "TEMP", "max_diff": 25},
]


def alert(sid, channel, minute, atype, site="S1", area="Z1"):
    return {
        "source_event_id": sid,
        "channel": channel,
        "site": site,
        "area": area,
        "minute": minute,
        "alert_type": atype,
    }


def batch(bid, wm, alerts, rules=MULTI_RULES, rv="RM"):
    return {
        "batch_id": bid, "rule_version": rv, "watermark": wm,
        "rules": rules, "alerts": alerts,
    }


class MultiRulePairTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(":memory:")

    def tearDown(self):
        self.store.close()

    def test_each_pair_uses_its_own_max_diff(self):
        # SMOKE-TEMP 上限 10：时差 12 无边；VIBRATION-TEMP 上限 25：时差 20 合法
        service.import_batch(
            self.store,
            {"batches": [batch(
                "B1", 100,
                [
                    alert("s1", "CH1", 0, "SMOKE", area="ZA"),
                    alert("t1", "CH2", 12, "TEMP", area="ZA"),
                    alert("v1", "CH1", 0, "VIBRATION", area="ZB"),
                    alert("t2", "CH2", 20, "TEMP", area="ZB"),
                ],
            )], "schedules": []},
        )
        pairs = {(r["source_a"], r["source_b"]) for r in self.store.get_edges()}
        self.assertNotIn(("s1", "t1"), pairs)   # 12 > 10
        self.assertIn(("t2", "v1"), pairs)      # 20 <= 25

    def test_global_max_diff_is_max_across_pairs_for_sealing(self):
        # 全局最大时差 = 25；max_minute=20 的分量需 20+25 < wm 才封存
        service.import_batch(
            self.store,
            {"batches": [
                batch("B1", 40, [alert("v1", "CH1", 0, "VIBRATION", area="ZB"),
                                  alert("t2", "CH2", 20, "TEMP", area="ZB")]),
            ], "schedules": []},
        )
        # 20+25=45 不严格小于 40 → 不封存
        self.assertEqual(service.seal(self.store)["sealed_count"], 0)
        service.import_batch(
            self.store,
            {"batches": [{"batch_id": "B2", "rule_version": "RM",
                          "watermark": 46, "rules": MULTI_RULES, "alerts": []}],
             "schedules": []},
        )
        # 45 < 46 → 封存
        self.assertEqual(service.seal(self.store)["sealed_count"], 1)


class CrossDayDeadlineTests(unittest.TestCase):
    def test_deadline_using_cross_day_shift(self):
        entries = [
            domain.normalize_schedule_entry(
                {"person": "finn", "group": "G3", "start_minute": 1430,
                 "end_minute": 15, "cross_day": True}
            ),
            domain.normalize_schedule_entry(
                {"person": "kim", "group": "G4", "start_minute": 1430,
                 "end_minute": 1460}
            ),
        ]
        # 双组覆盖 1430..1454（25 分钟），要求 25 → 第 25 个覆盖分钟 1454，截止 1455
        self.assertEqual(domain.compute_deadline(entries, 1430, 25), 1455)
        # 要求 30 → 不足，无可排期限
        self.assertIsNone(domain.compute_deadline(entries, 1430, 30))


class ReceiptAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(":memory:")
        service.import_batch(
            self.store,
            {"batches": [
                {"batch_id": "B1", "rule_version": "R1", "watermark": 10,
                 "rules": [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10}],
                 "alerts": [
                     alert("a1", "CH1", 0, "SMOKE"),
                     alert("a2", "CH2", 15, "SMOKE"),
                 ]},
                {"batch_id": "B2", "rule_version": "R1", "watermark": 15,
                 "rules": [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10}],
                 "alerts": [alert("a3", "CH3", 10, "TEMP")]},
                {"batch_id": "B3", "rule_version": "R1", "watermark": 26,
                 "rules": [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10}],
                 "alerts": []},
            ], "schedules": []},
        )
        self.sealed = service.seal(self.store)
        self.event_id = self.sealed["sealed_events"][0]["event_id"]

    def tearDown(self):
        self.store.close()

    def test_receipt_lists_actual_edge_evidence(self):
        row = self.store.get_sealed_event(self.event_id)
        receipt = json.loads(row["receipt_json"])
        edge_pairs = sorted((e["source_a"], e["source_b"]) for e in receipt["edges"])
        self.assertEqual(edge_pairs, [("a1", "a3"), ("a2", "a3")])
        # 每条边带规则版本与时差证据
        for edge in receipt["edges"]:
            self.assertEqual(edge["rule_version"], "R1")
            self.assertIn(edge["diff_minutes"], (10, 5))
        self.assertEqual(
            sorted(m["source_event_id"] for m in receipt["members"]),
            ["a1", "a2", "a3"],
        )

    def test_verify_detects_tampered_summary_hash(self):
        self.store.conn.execute(
            "UPDATE sealed_events SET summary_hash = ? WHERE event_id = ?",
            ("tampered", self.event_id),
        )
        self.store.conn.commit()
        result = service.verify(self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(any("摘要" in p for p in result["problems"]))

    def test_verify_detects_tampered_event_identity(self):
        # 篡改封存事件的规则版本列：按成员重算出的事件 ID 与记录值不再一致
        self.store.conn.execute(
            "UPDATE sealed_events SET rule_version = ? WHERE event_id = ?",
            ("R9", self.event_id),
        )
        self.store.conn.commit()
        result = service.verify(self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(any("ID" in p for p in result["problems"]))

    def test_verify_detects_late_archive_violation(self):
        # 直接构造一条不满足 minute < watermark 的迟到记录
        self.store.conn.execute(
            "INSERT INTO late_alerts(source_event_id, channel, site, area, minute,"
            " alert_type, rule_version, batch_id, watermark)"
            " VALUES('bad-late','CH1','S1','Z1',50,'SMOKE','R1','B3',40)"
        )
        self.store.conn.commit()
        result = service.verify(self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(any("迟到" in p for p in result["problems"]))


if __name__ == "__main__":
    unittest.main()
