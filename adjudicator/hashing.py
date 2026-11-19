"""确定性哈希与标识工具。

所有哈希均为纯函数，保证精确重放时结果逐字节一致（幂等的基础）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_bytes(obj: Any) -> bytes:
    """把对象序列化为规范 JSON 字节串：键排序、无多余空白。"""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sealed_event_id(rule_version: str, source_ids: Iterable[str]) -> str:
    """封存事件 ID：规则版本 + 排序后的来源事件 ID，取 SHA-256 前 16 位。"""
    joined = ",".join(sorted(source_ids))
    digest = sha256_hex(f"{rule_version}|{joined}".encode("utf-8"))
    return "EVT-" + digest[:16]


def event_summary_hash(alerts: list[dict], edges: list[tuple[str, str]]) -> str:
    """事件摘要哈希：绑定分量内全部告警字段与实际边证据。

    双签绑定的就是该摘要；任何对告警或边的篡改都会改变摘要。
    """
    payload = {
        "alerts": sorted(
            (
                {
                    "source_event_id": a["source_event_id"],
                    "channel": a["channel"],
                    "site": a["site"],
                    "area": a["area"],
                    "minute": a["minute"],
                    "alert_type": a["alert_type"],
                }
                for a in alerts
            ),
            key=lambda x: x["source_event_id"],
        ),
        "edges": sorted(edges),
    }
    return sha256_hex(canonical_bytes(payload))
