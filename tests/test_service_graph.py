"""服务层测试（一）：导入、候选边、第三条告警合并、封存水位边界与迟到档。"""
from __future__ import annotations

import unittest

from adjudicator import service
from adjudicator.storage import Storage

RULES = [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10}]


def alert(sid, channel, minute, atype, site="S1", area="Z1"):
    return {
        "source_event_id": sid,
        "channel": channel,
        "site": site,
        "area": area,
        "minute": minute,
        "alert_type": atype,
    }


def batch(bid, wm, alerts, rules=None, rv="R1"):
    return {
        "batch_id": bid,
        "rule_version": rv,
        "watermark": wm,
        "rules": rules if rules is not None else RULES,
        "alerts": alerts,
    }


def payload(batches, schedules=None):
    return {"batches": batches, "schedules": schedules or []}


class ImportAndGraphTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(":memory:")

    def tearDown(self):
        self.store.close()

    def test_diff_equals_max_creates_edge_one_over_does_not(self):
        # 首批无 watermark，不存在迟到；水位取不超过本批最早事件分钟
        b = batch(
            "B1",
            20,
            [
                alert("eq1", "CH1", 20, "SMOKE", area="Z2"),
                alert("eq2", "CH2", 30, "TEMP", area="Z2"),
                alert("gt1", "CH1", 20, "SMOKE", area="Z3"),
                alert("gt2", "CH2", 31, "TEMP", area="Z3"),
            ],
        )
        service.import_batch(self.store, payload([b]))
        edges = {(r["source_a"], r["source_b"]) for r in self.store.get_edges()}
        self.assertIn(("eq1", "eq2"), edges)  # 时差恰等 10 → 合法
        self.assertNotIn(("gt1", "gt2"), edges)  # 时差 11 → 非法

    def test_same_channel_and_cross_site_cross_area_no_edge(self):
        b = batch(
            "B1",
            24,
            [
                alert("sc1", "CH1", 24, "SMOKE", area="Z4"),
                alert("sc2", "CH1", 30, "TEMP", area="Z4"),
                alert("ar1", "CH1", 26, "SMOKE", area="Z6"),
                alert("ar2", "CH2", 26, "TEMP", area="Z7"),
                alert("st1", "CH1", 27, "SMOKE", site="S2", area="Z8"),
                alert("st2", "CH2", 27, "TEMP", site="S3", area="Z8"),
            ],
        )
        service.import_batch(self.store, payload([b]))
        self.assertEqual(self.store.get_edges(), [])

    def test_third_alert_merges_two_staged_components(self):
        # 第一批：同为 SMOKE（配对未声明）且时差 15 > 10 → 两个孤立暂存分量；
        # wm=10 不超过最早事件分钟，首批不存在迟到
        service.import_batch(
            self.store,
            payload([batch("B1", 10, [alert("a1", "CH1", 0, "SMOKE"),
                                      alert("a2", "CH2", 15, "SMOKE")])]),
        )
        report = service.build_report(self.store)
        self.assertEqual(report["staged"]["component_count"], 2)

        # 第二批：第三条 TEMP 告警 minute=10 不低于当前水位 10（及时数据），
        # 与两条 SMOKE 时差分别为 10（恰等）和 5 → 两条桥接边合并为一个分量
        service.import_batch(
            self.store,
            payload([batch("B2", 15, [alert("a3", "CH3", 10, "TEMP")])]),
        )
        report = service.build_report(self.store)
        self.assertEqual(report["staged"]["component_count"], 1)
        comp = report["staged"]["components"][0]
        self.assertEqual(comp["members"], ["a1", "a2", "a3"])
        self.assertEqual(len(comp["edges"]), 2)

    def test_same_channel_alert_only_joins_via_legal_cross_channel_edge(self):
        b = batch(
            "B1",
            25,
            [
                alert("x1", "CH1", 25, "SMOKE", area="Z5"),
                alert("x2", "CH2", 28, "TEMP", area="Z5"),
                alert("x3", "CH1", 30, "SMOKE", area="Z5"),
            ],
        )
        service.import_batch(self.store, payload([b]))
        report = service.build_report(self.store)
        # x1-x2 合法跨通路边；x3 与 x2 差 2 但同为 SMOKE/TEMP 配对、异通道 → 也合法？
        # x3(CH1,SMOKE,30) 与 x2(CH2,TEMP,28)：异通道、同站同区、类型对合规、时差 2 → 合法
        members = sorted(m for c in report["staged"]["components"] for m in c["members"])
        self.assertEqual(members, ["x1", "x2", "x3"])
        # 同通道 x1-x3 之间永远不存在直接边
        for comp in report["staged"]["components"]:
            for e in comp["edges"]:
                self.assertFalse(
                    (e["source_a"], e["source_b"]) == ("x1", "x3")
                )

    def test_late_alert_goes_to_late_archive_and_never_edges(self):
        # 首批 wm=20：minute=20 不小于水位，及时入库
        service.import_batch(
            self.store, payload([batch("B1", 20, [alert("early", "CH1", 20, "SMOKE")])])
        )
        # 空批次把水位推进到 40；新告警 minute=30 严格小于 40 → 迟到档
        service.import_batch(self.store, payload([batch("B2", 40, [])]))
        res = service.import_batch(
            self.store, payload([batch("B3", 40, [alert("late1", "CH2", 30, "TEMP")])])
        )
        self.assertEqual(res["batches"][0]["late_alerts"], 1)
        self.assertTrue(self.store.late_alert_exists("late1"))
        self.assertFalse(self.store.alert_exists("late1"))
        self.assertEqual(self.store.get_edges(), [])  # 迟到告警不产生边
        report = service.build_report(self.store)
        self.assertEqual(len(report["late_alerts"]), 1)
        self.assertEqual(report["late_alerts"][0]["watermark"], 40)

    def test_watermark_must_advance_monotonically(self):
        service.import_batch(
            self.store, payload([batch("B1", 10, [alert("a", "CH1", 10, "SMOKE")])])
        )
        with self.assertRaises(service.AdjudicationError):
            service.import_batch(
                self.store,
                payload([batch("B2", 9, [alert("b", "CH2", 12, "TEMP")])]),
            )

    def test_duplicate_source_id_rejected(self):
        service.import_batch(
            self.store, payload([batch("B1", 10, [alert("a", "CH1", 10, "SMOKE")])])
        )
        with self.assertRaises(service.AdjudicationError):
            service.import_batch(
                self.store,
                payload([batch("B2", 12, [alert("a", "CH2", 11, "TEMP")])]),
            )


class SealingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(":memory:")

    def tearDown(self):
        self.store.close()

    def test_seal_blocked_when_max_plus_diff_equals_watermark(self):
        # max=30，全局最大差 10，30+10=40 恰等于 watermark → 严格小于不成立，不封存。
        # 首批 wm 取 20（不早于本批最早事件分钟），再以空批次把水位推到 40
        service.import_batch(
            self.store,
            payload(
                [
                    batch(
                        "B1",
                        20,
                        [
                            alert("eq1", "CH1", 20, "SMOKE", area="Z2"),
                            alert("eq2", "CH2", 30, "TEMP", area="Z2"),
                        ],
                    ),
                    batch("B2", 40, []),
                ]
            ),
        )
        res = service.seal(self.store)
        self.assertEqual(res["sealed_count"], 0)
        self.assertEqual(res["staged_alerts_remaining"], 2)

    def test_seal_succeeds_when_strictly_below_watermark(self):
        # 合并后 max=15；wm 推到 26 时 15+10=25 < 26 → 可封存
        service.import_batch(
            self.store,
            payload(
                [
                    batch("B1", 10, [alert("a1", "CH1", 0, "SMOKE"),
                                     alert("a2", "CH2", 15, "SMOKE")]),
                    batch("B2", 15, [alert("a3", "CH3", 10, "TEMP")]),
                    batch("B3", 26, []),
                ]
            ),
        )
        res = service.seal(self.store)
        self.assertEqual(res["sealed_count"], 1)
        event = res["sealed_events"][0]
        self.assertEqual(event["members"], ["a1", "a2", "a3"])
        self.assertEqual(event["edge_count"], 2)
        # 封存后活动图清空，告警冻结进事件
        self.assertEqual(self.store.get_active_alerts(), [])
        report = service.build_report(self.store)
        self.assertEqual(len(report["sealed_events"]), 1)
        self.assertEqual(report["sealed_events"][0]["members"], ["a1", "a2", "a3"])

    def test_isolated_alert_also_seals_as_event(self):
        # 首批 wm=20（不早于事件分钟），空批次把水位推过封存安全线
        service.import_batch(
            self.store,
            payload([batch("B1", 20, [alert("solo", "CH1", 20, "SMOKE")]),
                     batch("B2", 31, [])]),
        )
        res = service.seal(self.store)
        self.assertEqual(res["sealed_count"], 1)
        self.assertEqual(res["sealed_events"][0]["members"], ["solo"])
        self.assertEqual(res["sealed_events"][0]["edge_count"], 0)

    def test_sealing_then_higher_watermark_releases_rest(self):
        # 边界分量在 wm=40 不封存；wm=41 后 30+10=40 < 41 可封存
        service.import_batch(
            self.store,
            payload([batch("B1", 20, [alert("eq1", "CH1", 20, "SMOKE", area="Z2"),
                                      alert("eq2", "CH2", 30, "TEMP", area="Z2")]),
                     batch("B2", 40, [])]),
        )
        self.assertEqual(service.seal(self.store)["sealed_count"], 0)
        service.import_batch(
            self.store, payload([batch("B3", 41, [])])
        )
        res = service.seal(self.store)
        self.assertEqual(res["sealed_count"], 1)

    def test_sealed_event_id_deterministic_and_receipt_complete(self):
        from adjudicator.hashing import sealed_event_id

        service.import_batch(
            self.store,
            payload(
                [
                    batch("B1", 10, [alert("a1", "CH1", 0, "SMOKE"),
                                     alert("a2", "CH2", 15, "SMOKE")]),
                    batch("B2", 15, [alert("a3", "CH3", 10, "TEMP")]),
                    batch("B3", 26, []),
                ]
            ),
        )
        res = service.seal(self.store)
        event_id = res["sealed_events"][0]["event_id"]
        self.assertEqual(event_id, sealed_event_id("R1", ["a1", "a2", "a3"]))
        row = self.store.get_sealed_event(event_id)
        import json
        receipt = json.loads(row["receipt_json"])
        self.assertEqual(receipt["watermark"], 26)
        self.assertEqual(receipt["global_max_diff"], 10)
        self.assertEqual(len(receipt["edges"]), 2)
        self.assertEqual(receipt["summary_hash"], row["summary_hash"])
        # 成员顺序不影响事件 ID
        self.assertEqual(
            sealed_event_id("R1", ["a3", "a1", "a2"]),
            sealed_event_id("R1", ["a1", "a2", "a3"]),
        )


class IdempotencyAndVersionTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(":memory:")

    def tearDown(self):
        self.store.close()

    def test_full_replay_is_idempotent(self):
        p = payload(
            [
                batch("B1", 10, [alert("a1", "CH1", 0, "SMOKE"),
                                 alert("a2", "CH2", 15, "SMOKE")]),
                batch("B2", 15, [alert("a3", "CH3", 10, "TEMP")]),
                batch("B3", 26, []),
            ]
        )
        first = service.import_batch(self.store, p)
        second = service.import_batch(self.store, p)
        self.assertTrue(all(b["replayed"] for b in second["batches"]))
        self.assertEqual(len(self.store.get_active_alerts()), 3)
        self.assertEqual(len(self.store.get_edges()), 2)
        # 封存重放：第二次 seal 不产生重复事件
        r1 = service.seal(self.store)
        r2 = service.seal(self.store)
        self.assertEqual(r1["sealed_count"], 1)
        self.assertEqual(r2["sealed_count"], 0)

    def test_rule_version_content_change_rejected(self):
        service.import_batch(
            self.store,
            payload([batch("B1", 10, [], rules=RULES, rv="R1")]),
        )
        new_rules = [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 20}]
        with self.assertRaises(service.AdjudicationError):
            service.import_batch(
                self.store,
                payload([batch("B2", 20, [], rules=new_rules, rv="R1")]),
            )

    def test_new_rule_version_only_affects_new_batch_edges(self):
        # 旧规则下两条异通道 SMOKE（配对未声明）无边（首批 wm=0，不存在迟到）
        service.import_batch(
            self.store,
            payload([batch("B1", 0, [alert("o1", "CH1", 0, "SMOKE", area="Z9"),
                                     alert("o2", "CH2", 15, "SMOKE", area="Z9")])]),
        )
        self.assertEqual(self.store.get_edges(), [])
        # 新版本放宽到 20：新 TEMP 告警与两条旧 SMOKE 都在 20 分钟内，
        # 只对新告警建 R2 边，旧告警对不回溯重算
        rules2 = [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 20}]
        service.import_batch(
            self.store,
            payload([batch("B2", 15, [alert("n1", "CH3", 18, "TEMP", area="Z9")],
                           rules=rules2, rv="R2")]),
        )
        pairs = {(r["source_a"], r["source_b"]): r["rule_version"]
                 for r in self.store.get_edges()}
        self.assertNotIn(("o1", "o2"), pairs)  # 旧对仍无边
        self.assertEqual(pairs[("n1", "o1")], "R2")  # 新边按 R2
        self.assertEqual(pairs[("n1", "o2")], "R2")

    def test_sealed_events_never_rewritten_by_new_rules(self):
        # 首批 wm=20（不早于事件分钟），空批次推高水位后封存
        service.import_batch(
            self.store, payload([batch("B1", 20, [alert("solo", "CH1", 20, "SMOKE")]),
                                 batch("B2", 31, [])])
        )
        sealed = service.seal(self.store)
        event_id = sealed["sealed_events"][0]["event_id"]
        before = dict(self.store.get_sealed_event(event_id))
        # 新规则版本 + 新高水位封存其他告警，旧事件保持原样
        rules2 = [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 99}]
        service.import_batch(
            self.store,
            payload([batch("B3", 100, [alert("other", "CH2", 90, "TEMP")],
                           rules=rules2, rv="R2")]),
        )
        service.seal(self.store)
        after = self.store.get_sealed_event(event_id)
        self.assertEqual(before["rule_version"], after["rule_version"])
        self.assertEqual(before["receipt_json"], after["receipt_json"])


if __name__ == "__main__":
    unittest.main()
