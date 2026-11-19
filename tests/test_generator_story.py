"""生成器产出数据的端到端测试：保证演示样本真实可跑且叙事结论成立。"""
from __future__ import annotations

import json
import unittest

from adjudicator import service
from adjudicator.hashing import canonical_bytes, sha256_hex
from adjudicator.storage import Storage
from generator import synthetic


class GeneratorSampleTests(unittest.TestCase):
    def test_full_generated_story(self):
        store = Storage(":memory:")
        bundle = synthetic.file_bundle()
        # 先导入数据批次（01..05），排班更正 06 在封存之后再导入
        for _name, payload in bundle[:5]:
            service.import_batch(store, payload)

        report = service.build_report(store)
        # 迟到档恰有 1 条（minute=30 < 导入前水位 40）
        self.assertEqual(len(report["late_alerts"]), 1)
        self.assertEqual(report["late_alerts"][0]["source_event_id"], "S1-late1")

        # B1/B2 后第三条告警已把两个暂存分量合并为一个（含两条桥接边）
        main_comp = next(
            c for c in report["staged"]["components"]
            if c["members"] == ["S1-a1", "S1-a2", "S1-a3"]
        )
        self.assertEqual(len(main_comp["edges"]), 2)

        # 高水位封存全部暂存分量
        sealed = service.seal(store)
        main_event = next(
            e for e in sealed["sealed_events"]
            if e["members"] == ["S1-a1", "S1-a2", "S1-a3"]
        )
        # 主线事件在 wm=100 封存，S1 v1 排班窗口 100 起 → 截止 135
        self.assertEqual(main_event["watermark"], 100)
        self.assertEqual(main_event["deadline_minute"], 135)
        self.assertEqual(main_event["edge_count"], 2)

        # 封存之后再导入排班更正 v2：旧事件永久绑定 v1，期限不变
        service.import_batch(store, bundle[5][1])
        row = store.get_sealed_event(main_event["event_id"])
        self.assertEqual(row["schedule_version"], "S1-SCH-v1")
        self.assertEqual(row["deadline_minute"], 135)

        # S2 空班 / S3 覆盖不足 → 无可排期限
        report2 = service.build_report(store)
        no_deadline_sites = {
            e["site"] for e in report2["sealed_events"]
            if e["deadline_minute"] is None
        }
        self.assertIn("S2", no_deadline_sites)
        self.assertIn("S3", no_deadline_sites)

        # 主线事件双签：异组、异步 → reviewed
        service.review_sign(store, main_event["event_id"], "alice", 110)
        r = service.review_sign(store, main_event["event_id"], "bob", 130)
        self.assertEqual(r["review_status"], "reviewed")

        # 同分钟重复签收幂等
        r2 = service.review_sign(store, main_event["event_id"], "bob", 130)
        self.assertTrue(r2["replayed"])

        # 同组第二签不能完成审查（在另一条 S1 事件上验证）
        other_s1 = next(
            e for e in report2["sealed_events"]
            if e["event_id"] != main_event["event_id"] and e["site"] == "S1"
            and e["deadline_minute"] is not None
        )
        service.review_sign(store, other_s1["event_id"], "alice", 110)
        rg = service.review_sign(store, other_s1["event_id"], "carol", 115)
        self.assertEqual(rg["review_status"], "pending")

        # 全库核验通过
        verdict = service.verify(store)
        self.assertTrue(verdict["ok"], verdict["problems"])
        store.close()

    def test_watermark_equality_boundary_not_sealed_early(self):
        """封存水位恰相等样本：wm=40 时 max=30 的分量必须保持暂存。"""
        store = Storage(":memory:")
        # 只导到 B4（水位 40）
        for _name, payload in synthetic.file_bundle()[:4]:
            service.import_batch(store, payload)
        res = service.seal(store)
        # 存在 max=30 的恰相等分量，任何 S1/Z8 成员都不应被封存
        sealed_members = {m for e in res["sealed_events"] for m in e["members"]}
        self.assertNotIn("S1-eq3", sealed_members)
        self.assertNotIn("S1-eq4", sealed_members)
        store.close()

    def test_generated_payload_is_deterministic(self):
        h1 = sha256_hex(canonical_bytes(synthetic.build_sample_payload()))
        h2 = sha256_hex(canonical_bytes(synthetic.build_sample_payload()))
        self.assertEqual(h1, h2)
        # 内容是 JSON 可序列化的纯数据
        json.dumps(synthetic.build_sample_payload(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
