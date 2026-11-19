"""服务层测试（二）：封存期限、双签规则、排班版本绑定、核验与中断恢复。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


def sched(version, site, entries):
    return {"schedule_version": version, "site": site, "entries": entries}


# 封存水位 100 起双组覆盖 105..139（35 个），第 30 个覆盖分钟 134 → 截止 135
S1_V1 = sched(
    "S1-V1",
    "S1",
    [
        {"person": "alice", "group": "G1", "start_minute": 100, "end_minute": 140},
        {"person": "carol", "group": "G1", "start_minute": 110, "end_minute": 120},
        {"person": "bob", "group": "G2", "start_minute": 105, "end_minute": 140},
    ],
)

ROOT = Path(__file__).resolve().parents[1]


class DeadlineSealTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(":memory:")

    def tearDown(self):
        self.store.close()

    def _seal_one(self):
        # 首批 wm=20（不早于事件分钟 20），空批次把水位推过封存安全线到 100
        service.import_batch(
            self.store,
            {
                "batches": [
                    batch("B1", 20, [alert("e1", "CH1", 20, "SMOKE")]),
                    batch("B2", 100, []),
                ],
                "schedules": [S1_V1],
            },
        )
        res = service.seal(self.store)
        self.assertEqual(res["sealed_count"], 1)
        return res["sealed_events"][0]["event_id"]

    def test_deadline_minute_recorded_from_seal_watermark(self):
        event_id = self._seal_one()
        row = self.store.get_sealed_event(event_id)
        self.assertEqual(row["deadline_minute"], 135)
        receipt = json.loads(row["receipt_json"])
        self.assertEqual(receipt["coverage_minutes"], 30)
        self.assertEqual(receipt["schedule_version"], "S1-V1")
        self.assertEqual(receipt["watermark"], 100)

    def test_empty_shift_yields_no_deadline(self):
        service.import_batch(
            self.store,
            {
                "batches": [
                    batch("B1", 20, [alert("e2", "CH1", 20, "SMOKE", site="S2", area="Z1")]),
                    batch("B2", 100, []),
                ],
                "schedules": [sched("S2-V1", "S2", [])],
            },
        )
        res = service.seal(self.store)
        self.assertIsNone(res["sealed_events"][0]["deadline_minute"])
        self.assertEqual(res["sealed_events"][0]["deadline_note"], "无可排期限")

    def test_insufficient_coverage_yields_no_deadline(self):
        sparse = sched(
            "S3-V1",
            "S3",
            [
                {"person": "dan", "group": "G1", "start_minute": 100, "end_minute": 110},
                {"person": "eva", "group": "G2", "start_minute": 100, "end_minute": 105},
            ],
        )
        service.import_batch(
            self.store,
            {
                "batches": [
                    batch("B1", 20, [alert("e3", "CH1", 20, "SMOKE", site="S3", area="Z1")]),
                    batch("B2", 100, []),
                ],
                "schedules": [sparse],
            },
        )
        res = service.seal(self.store)
        self.assertIsNone(res["sealed_events"][0]["deadline_minute"])

    def test_schedule_correction_new_version_keeps_old_event_bound(self):
        event_id = self._seal_one()
        # 排班更正必须新版本
        v2 = sched(
            "S1-V2",
            "S1",
            [
                {"person": "alice", "group": "G1", "start_minute": 200, "end_minute": 300},
                {"person": "bob", "group": "G2", "start_minute": 200, "end_minute": 300},
            ],
        )
        service.import_batch(self.store, {"batches": [], "schedules": [v2]})
        row = self.store.get_sealed_event(event_id)
        self.assertEqual(row["schedule_version"], "S1-V1")  # 永久绑定旧版
        self.assertEqual(row["deadline_minute"], 135)
        # 旧版本内容被篡改式重声明会被拒绝
        tampered = sched(
            "S1-V1",
            "S1",
            [{"person": "x", "group": "G9", "start_minute": 0, "end_minute": 9}],
        )
        with self.assertRaises(service.AdjudicationError):
            service.import_batch(self.store, {"batches": [], "schedules": [tampered]})


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(":memory:")
        service.import_batch(
            self.store,
            {
                "batches": [
                    batch("B1", 20, [alert("e1", "CH1", 20, "SMOKE")]),
                    batch("B2", 100, []),
                ],
                "schedules": [S1_V1],
            },
        )
        self.event_id = service.seal(self.store)["sealed_events"][0]["event_id"]
        self.summary = self.store.get_sealed_event(self.event_id)["summary_hash"]

    def tearDown(self):
        self.store.close()

    def _fresh_event_store(self) -> Storage:
        store2 = Storage(":memory:")
        service.import_batch(
            store2,
            {
                "batches": [
                    batch("B1", 20, [alert("e1", "CH1", 20, "SMOKE")]),
                    batch("B2", 100, []),
                ],
                "schedules": [S1_V1],
            },
        )
        return store2

    def test_two_different_groups_complete_review_async(self):
        r1 = service.review_sign(self.store, self.event_id, "bob", 130)
        self.assertEqual(r1["review_status"], "pending")  # 只签 G2
        r2 = service.review_sign(self.store, self.event_id, "alice", 110)
        self.assertEqual(r2["review_status"], "reviewed")  # 异组第二签
        self.assertEqual(sorted(r2["distinct_groups"]), ["G1", "G2"])
        # 互换顺序结论一致：另起事件再按相反顺序签
        store2 = self._fresh_event_store()
        eid2 = service.seal(store2)["sealed_events"][0]["event_id"]
        service.review_sign(store2, eid2, "alice", 110)
        r = service.review_sign(store2, eid2, "bob", 130)
        self.assertEqual(r["review_status"], "reviewed")
        store2.close()

    def test_same_group_second_sign_does_not_complete(self):
        service.review_sign(self.store, self.event_id, "alice", 110)
        r = service.review_sign(self.store, self.event_id, "carol", 115)
        self.assertEqual(r["review_status"], "pending")
        self.assertEqual(r["distinct_groups"], ["G1"])
        # 随后异组签收即完成
        r2 = service.review_sign(self.store, self.event_id, "bob", 120)
        self.assertEqual(r2["review_status"], "reviewed")

    def test_signature_idempotent_same_content(self):
        r1 = service.review_sign(
            self.store, self.event_id, "alice", 110, self.summary
        )
        r2 = service.review_sign(
            self.store, self.event_id, "alice", 110, self.summary
        )
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["signature_count"], r2["signature_count"])
        self.assertEqual(
            len(self.store.get_signatures(self.event_id)), 1
        )

    def test_signature_time_window_enforced(self):
        # 早于封存水位
        with self.assertRaises(service.AdjudicationError):
            service.review_sign(self.store, self.event_id, "alice", 99)
        # 截止时刻本身不合法（严格早于 135）
        with self.assertRaises(service.AdjudicationError):
            service.review_sign(self.store, self.event_id, "bob", 135)
        # 合法边界：水位 100 与截止前一分钟 134（bob 105 起当班）
        service.review_sign(self.store, self.event_id, "alice", 100)
        service.review_sign(self.store, self.event_id, "bob", 134)
        self.assertEqual(
            self.store.get_sealed_event(self.event_id)["review_status"], "reviewed"
        )

    def test_signer_must_be_on_duty(self):
        # alice 100 才上班；bob 105 才上班
        with self.assertRaises(service.AdjudicationError):
            service.review_sign(self.store, self.event_id, "bob", 100)
        with self.assertRaises(service.AdjudicationError):
            service.review_sign(self.store, self.event_id, "alice", 140)

    def test_summary_hash_binding_rejects_mismatch(self):
        with self.assertRaises(service.AdjudicationError):
            service.review_sign(
                self.store, self.event_id, "alice", 110, "deadbeef"
            )

    def test_no_deadline_event_cannot_be_signed(self):
        store2 = Storage(":memory:")
        service.import_batch(
            store2,
            {
                "batches": [
                    batch("B1", 20, [alert("e2", "CH1", 20, "SMOKE", site="S2", area="Z1")]),
                    batch("B2", 100, []),
                ],
                "schedules": [sched("S2-V1", "S2", [])],
            },
        )
        eid = service.seal(store2)["sealed_events"][0]["event_id"]
        with self.assertRaises(service.AdjudicationError):
            service.review_sign(store2, eid, "alice", 110)
        store2.close()


class VerifyAndRecoveryTests(unittest.TestCase):
    def _build_verified_store(self) -> Storage:
        store = Storage(":memory:")
        service.import_batch(
            store,
            {
                "batches": [
                    batch("B1", 10, [alert("a1", "CH1", 0, "SMOKE"),
                                     alert("a2", "CH2", 15, "SMOKE")]),
                    batch("B2", 15, [alert("a3", "CH3", 10, "TEMP")]),
                    batch("B3", 100, []),
                ],
                "schedules": [S1_V1],
            },
        )
        eid = service.seal(store)["sealed_events"][0]["event_id"]
        service.review_sign(store, eid, "alice", 110)
        service.review_sign(store, eid, "bob", 130)
        result = service.verify(store)
        self.assertTrue(result["ok"], result["problems"])
        return store

    def test_verify_passes_on_clean_state(self):
        store = self._build_verified_store()
        result = service.verify(store)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["checked_events"], 1)
        store.close()

    def test_verify_detects_tampered_deadline(self):
        store = self._build_verified_store()
        # 篡改封存回执中的期限与期限列
        row = store.list_sealed_events()[0]
        receipt = json.loads(row["receipt_json"])
        receipt["deadline_minute"] = 999
        store.conn.execute(
            "UPDATE sealed_events SET receipt_json = ? WHERE event_id = ?",
            (json.dumps(receipt, sort_keys=True), row["event_id"]),
        )
        store.conn.execute(
            "UPDATE sealed_events SET deadline_minute = 999 WHERE event_id = ?",
            (row["event_id"],),
        )
        store.conn.commit()  # 显式提交测试篡改，避免残留隐式事务
        result = service.verify(store)
        self.assertFalse(result["ok"])
        self.assertTrue(any("期限" in p for p in result["problems"]))
        store.close()

    def test_verify_detects_illegal_active_edge(self):
        store = Storage(":memory:")
        service.import_batch(
            store,
            {
                "batches": [
                    batch(
                        "B1",
                        20,
                        [
                            alert("u1", "CH1", 20, "SMOKE", area="ZA"),
                            alert("u2", "CH2", 30, "TEMP", area="ZA"),
                        ],
                    )
                ],
                "schedules": [],
            },
        )
        # 人为把时差改成错误值
        store.conn.execute("UPDATE edges SET diff_minutes = 999")
        store.conn.commit()
        result = service.verify(store)
        self.assertFalse(result["ok"])
        self.assertTrue(any("时差" in p for p in result["problems"]))
        store.close()

    def test_interrupted_batch_recovery_by_replay(self):
        # 模拟“批次中断后恢复”：同一文件导入两次（第二次即崩溃后的重放），
        # 数据库最终状态与一次成功导入完全一致
        data = {
            "batches": [
                batch("B1", 10, [alert("a1", "CH1", 0, "SMOKE"),
                                 alert("a2", "CH2", 15, "SMOKE")]),
                batch("B2", 15, [alert("a3", "CH3", 10, "TEMP")]),
            ],
            "schedules": [S1_V1],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "case.db")
            store = Storage(db)
            service.import_batch(store, data)
            snapshot1 = {
                "alerts": [dict(r) for r in store.get_active_alerts()],
                "edges": [dict(r) for r in store.get_edges()],
                "wm": store.get_watermark(),
            }
            store.close()

            # 重放：全部批次标记 replayed，计数不翻倍
            store2 = Storage(db)
            replay = service.import_batch(store2, data)
            self.assertTrue(all(b["replayed"] for b in replay["batches"]))
            snapshot2 = {
                "alerts": [dict(r) for r in store2.get_active_alerts()],
                "edges": [dict(r) for r in store2.get_edges()],
                "wm": store2.get_watermark(),
            }
            self.assertEqual(snapshot1, snapshot2)
            # 推高水位后封存 + 核验仍然成立
            service.import_batch(store2, {"batches": [batch("B3", 100, [])],
                                          "schedules": []})
            sealed = service.seal(store2)
            self.assertEqual(sealed["sealed_count"], 1)
            self.assertTrue(service.verify(store2)["ok"])
            store2.close()


class CliSmokeTests(unittest.TestCase):
    """端到端子进程冒烟：用 python -I 运行，模拟真实命令行使用。"""

    def test_full_command_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "demo.db")
            payload_path = os.path.join(tmp, "input.json")
            Path(payload_path).write_text(
                json.dumps(
                    {
                        "batches": [
                            batch("B1", 20, [alert("e1", "CH1", 20, "SMOKE")]),
                            batch("B2", 100, []),
                        ],
                        "schedules": [S1_V1],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            runner = str(ROOT / "runner.py")

            def run(*args):
                proc = subprocess.run(
                    [sys.executable, "-I", runner, *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=str(ROOT),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)

            run("import-batch", "--db", db, "--file", payload_path)
            sealed = run("seal", "--db", db)
            event_id = sealed["sealed_events"][0]["event_id"]
            run("review", "--db", db, "--event-id", event_id,
                "--person", "alice", "--minute", "110")
            run("review", "--db", db, "--event-id", event_id,
                "--person", "bob", "--minute", "130")
            report = run("report", "--db", db)
            self.assertEqual(report["sealed_events"][0]["review_status"], "reviewed")
            verified = run("verify", "--db", db)
            self.assertTrue(verified["ok"])

            # 非法签收应返回非零退出码
            proc = subprocess.run(
                [sys.executable, "-I", runner, "review", "--db", db,
                 "--event-id", event_id, "--person", "bob", "--minute", "999"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(ROOT),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("错误", proc.stderr)


if __name__ == "__main__":
    unittest.main()
