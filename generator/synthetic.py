"""确定性虚构样本：批次、排班与操作指引。

叙事线（整数分钟均为虚构纪元；watermark 由输入声明，可低于同批最晚事件分钟）：
  B1 (wm=10)  两条异通道 SMOKE（类型对未声明 + 时差 15 > 10）→ 两个暂存分量；
              max(15)+10=25 不严格小于 10，封存安全条件不成立
  B2 (wm=15)  第三条告警 CH3/TEMP/minute=10 不低于当前水位 10（及时数据），
              与两条 SMOKE 的时差分别为 10（恰等，合法）和 5
              → 两条桥接边把两个暂存分量合并为一个
  B3 (wm=20)  边界样本：时差恰等 10 的合法边、时差 11 的非法边、同通道不连边、
              跨区域/跨站点不连边；各分量 max+10 >= 20 → 一律不封存
  B4 (wm=40)  封存水位恰相等样本：max=30 的分量 30+10=40 不严格小于 40，仍不封存
  B5 (wm=100) 迟到告警 minute=30 < 导入前水位 40 → 迟到档；高水位封存全部暂存分量
排班：
  S1 v1 覆盖窗口从 100 起，双组覆盖 105..139 共 35 分钟 → 截止 135
  S1 v2 更正窗口（演示旧事件永久绑定 v1）；S2 空班；S3 覆盖不足 30 分钟；
  S4 同组重叠班（再多人员也不产生双组覆盖分钟）
"""
from __future__ import annotations

import json
from pathlib import Path

RULE_VERSION = "R1"

RULES = [
    {"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10},
]


def _alert(source_id, channel, site, area, minute, alert_type):
    return {
        "source_event_id": source_id,
        "channel": channel,
        "site": site,
        "area": area,
        "minute": minute,
        "alert_type": alert_type,
    }


def batch_b1() -> dict:
    return {
        "batch_id": "B1",
        "rule_version": RULE_VERSION,
        "watermark": 10,
        "rules": RULES,
        "alerts": [
            # 两条同为 SMOKE：配对未声明且时差 15 > 10 → 两个暂存分量
            _alert("S1-a1", "CH1", "S1", "Z1", 0, "SMOKE"),
            _alert("S1-a2", "CH2", "S1", "Z1", 15, "SMOKE"),
        ],
    }


def batch_b2() -> dict:
    return {
        "batch_id": "B2",
        "rule_version": RULE_VERSION,
        "watermark": 15,
        "rules": RULES,
        "alerts": [
            # minute=10 不低于当前水位 10 → 及时数据；TEMP 与两条 SMOKE
            # 时差 10（恰等，合法）与 5 → 两条桥接边合并为一个分量
            _alert("S1-a3", "CH3", "S1", "Z1", 10, "TEMP"),
        ],
    }


def batch_b3() -> dict:
    return {
        "batch_id": "B3",
        "rule_version": RULE_VERSION,
        "watermark": 20,
        "rules": RULES,
        "alerts": [
            # 时差恰等 10 → 合法边（<= 判定）
            _alert("S1-eq1", "CH1", "S1", "Z2", 20, "SMOKE"),
            _alert("S1-eq2", "CH2", "S1", "Z2", 30, "TEMP"),
            # 时差 11 > 10 → 无边，两条各自成分量
            _alert("S1-gt1", "CH1", "S1", "Z3", 22, "SMOKE"),
            _alert("S1-gt2", "CH2", "S1", "Z3", 33, "TEMP"),
            # 同通道：即使时差合规也不建边
            _alert("S1-sc1", "CH1", "S1", "Z4", 24, "SMOKE"),
            _alert("S1-sc2", "CH1", "S1", "Z4", 30, "TEMP"),
            # 同通道两条 + 一条异通道：异通道只能与其中一条相连，
            # 另一条同通道告警不能经“同通道关系”间接并入
            _alert("S1-x1", "CH1", "S1", "Z5", 25, "SMOKE"),
            _alert("S1-x2", "CH2", "S1", "Z5", 28, "TEMP"),
            _alert("S1-x3", "CH1", "S1", "Z5", 30, "SMOKE"),
            # 跨区域：其余全合规也不连边
            _alert("S1-ar1", "CH1", "S1", "Z6", 26, "SMOKE"),
            _alert("S1-ar2", "CH2", "S1", "Z7", 26, "TEMP"),
            # 跨站点：不连边
            _alert("S2-c1", "CH1", "S2", "Z1", 27, "SMOKE"),
            _alert("S2-c2", "CH2", "S3", "Z1", 27, "TEMP"),
        ],
    }


def batch_b4() -> dict:
    return {
        "batch_id": "B4",
        "rule_version": RULE_VERSION,
        "watermark": 40,
        "rules": RULES,
        "alerts": [
            # 封存水位恰相等样本：max=30，30+10=40 不严格小于 wm=40 → 仍不封存
            _alert("S1-eq3", "CH1", "S1", "Z8", 29, "SMOKE"),
            _alert("S1-eq4", "CH2", "S1", "Z8", 30, "TEMP"),
        ],
    }


def batch_b5() -> dict:
    return {
        "batch_id": "B5",
        "rule_version": RULE_VERSION,
        "watermark": 100,
        "rules": RULES,
        "alerts": [
            # 30 < 导入前水位 40 → 迟到档，不参与关联
            _alert("S1-late1", "CH1", "S1", "Z1", 30, "SMOKE"),
        ],
    }


def schedule_s1_v1() -> dict:
    """S1 初版排班：封存水位 100 起双组覆盖分钟为 105..139 共 35 分钟。

    第 30 个覆盖分钟是 134 → 截止时刻 135（半开 [100, 135)）。
    carol 与 alice 同组且班次重叠，用于验证“按去重分钟、不按人头累计”。
    """
    return {
        "schedule_version": "S1-SCH-v1",
        "site": "S1",
        "entries": [
            {"person": "alice", "group": "G1", "start_minute": 100, "end_minute": 140},
            {"person": "carol", "group": "G1", "start_minute": 110, "end_minute": 120},
            {"person": "bob", "group": "G2", "start_minute": 105, "end_minute": 140},
        ],
    }


def schedule_s1_v2() -> dict:
    """S1 更正版：窗口整体平移。旧封存事件仍绑定 v1，新事件才用 v2。"""
    return {
        "schedule_version": "S1-SCH-v2",
        "site": "S1",
        "entries": [
            {"person": "alice", "group": "G1", "start_minute": 200, "end_minute": 240},
            {"person": "bob", "group": "G2", "start_minute": 205, "end_minute": 240},
        ],
    }


def schedule_s2_empty() -> dict:
    """S2 空班：无任何覆盖分钟 → 无可排期限。"""
    return {"schedule_version": "S2-SCH-v1", "site": "S2", "entries": []}


def schedule_s3_sparse() -> dict:
    """S3 稀疏班：仅 5 分钟双组覆盖 + 一条跨日班次，不足 30 → 无可排期限。"""
    return {
        "schedule_version": "S3-SCH-v1",
        "site": "S3",
        "entries": [
            {"person": "dan", "group": "G1", "start_minute": 100, "end_minute": 110},
            {"person": "eva", "group": "G2", "start_minute": 100, "end_minute": 105},
            # 跨日班次：end 15 为次日分钟，规范化为 1440+15
            {
                "person": "finn",
                "group": "G3",
                "start_minute": 1430,
                "end_minute": 15,
                "cross_day": True,
            },
        ],
    }


def schedule_s4_samegroup() -> dict:
    """S4 同组重叠班：只有一个组，再多人员也不产生双组覆盖分钟。"""
    return {
        "schedule_version": "S4-SCH-v1",
        "site": "S4",
        "entries": [
            {"person": "gina", "group": "G1", "start_minute": 100, "end_minute": 160},
            {"person": "hugo", "group": "G1", "start_minute": 120, "end_minute": 180},
        ],
    }


def file_bundle() -> list[tuple[str, dict]]:
    """按导入顺序返回 (文件名, 负载) 列表。

    06 号文件是封存之后的排班更正，演示旧封存事件永久绑定 v1。
    """
    return [
        (
            "01_b1.json",
            {
                "batches": [batch_b1()],
                "schedules": [schedule_s1_v1(), schedule_s2_empty()],
            },
        ),
        ("02_b2.json", {"batches": [batch_b2()], "schedules": []}),
        (
            "03_b3.json",
            {
                "batches": [batch_b3()],
                "schedules": [schedule_s3_sparse(), schedule_s4_samegroup()],
            },
        ),
        ("04_b4.json", {"batches": [batch_b4()], "schedules": []}),
        ("05_b5.json", {"batches": [batch_b5()], "schedules": []}),
        (
            "06_schedule_fix.json",
            {"batches": [], "schedules": [schedule_s1_v2()]},
        ),
    ]


def operations_guide() -> dict:
    """人工/脚本操作指引（事件 ID 由封存输出确定，脚本里按站点+区域选取）。"""
    return {
        "steps": [
            {"cmd": "import-batch", "file": "01_b1.json", "expect": "2 条活动告警，0 条边"},
            {"cmd": "import-batch", "file": "02_b2.json", "expect": "桥接告警产生 2 条边，三分量合并"},
            {"cmd": "seal", "expect": "无封存（wm=15，安全条件不成立）"},
            {"cmd": "import-batch", "file": "03_b3.json", "expect": "边界样本暂存；恰等时差边合法"},
            {"cmd": "seal", "expect": "无事件封存（严格小于条件不成立）"},
            {"cmd": "import-batch", "file": "04_b4.json", "expect": "封存水位恰相等样本暂存"},
            {"cmd": "seal", "expect": "仍无封存（30+10 恰等于 40）"},
            {"cmd": "import-batch", "file": "05_b5.json", "expect": "1 条迟到告警入迟到档，水位推进到 100"},
            {"cmd": "seal", "expect": "高水位封存全部暂存分量；S1 主线事件期限 135；S2/S3 无可排期限"},
            {"cmd": "import-batch", "file": "06_schedule_fix.json", "expect": "排班更正为 v2；已封存事件仍绑定 v1、期限不变"},
            {
                "cmd": "review",
                "target": "S1/Z1 主线事件",
                "signs": [
                    {"person": "alice", "minute": 110, "note": "G1，区间内当班"},
                    {"person": "bob", "minute": 130, "note": "G2，异步第二签 → reviewed"},
                ],
            },
            {
                "cmd": "review-negative",
                "cases": [
                    "alice 重复同分钟签收 → 幂等",
                    "carol 在 115 签收 → 与 alice 同组，不能完成双签",
                    "bob 在 135 签收 → 等于截止时刻，拒绝（严格早于）",
                    "alice 在 99 签收 → 早于封存水位 100，拒绝",
                    "bob 在 100 签收 → 100 时 bob 尚未上班（105 才上班），拒绝",
                ],
            },
            {"cmd": "verify", "expect": "ok=true，问题清单为空"},
            {"cmd": "report", "expect": "全量 JSON：暂存分量、迟到档、封存事件与签收"},
        ]
    }


def build_sample_payload() -> dict:
    """单文件全量负载（便于快速演示）。"""
    return {
        "batches": [batch_b1(), batch_b2(), batch_b3(), batch_b4(), batch_b5()],
        "schedules": [
            schedule_s1_v1(),
            schedule_s2_empty(),
            schedule_s3_sparse(),
            schedule_s4_samegroup(),
            schedule_s1_v2(),
        ],
        "operations": operations_guide(),
    }


def write_sample_bundle(out_dir: str) -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, payload in file_bundle():
        path = out / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(str(path))
    guide = out / "operations.json"
    guide.write_text(
        json.dumps(operations_guide(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    written.append(str(guide))
    return written
