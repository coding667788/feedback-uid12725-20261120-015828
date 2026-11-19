"""服务层：import-batch / seal / review / report / verify 的全部业务规则。

设计要点：
- 每个写命令在单个 BEGIN IMMEDIATE 事务内完成，中断后重放同一输入即可恢复；
- 读命令使用 DEFERRED 快照，封存与签收各自在一致快照上做判定；
- 规则版本、排班版本一经写入即不可变；改规则/改排班只能新建版本；
- 封存会把分量成员与边从活动表移除并冻结进回执，新规则永远接触不到旧事件。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import domain
from .hashing import (
    canonical_bytes,
    event_summary_hash,
    sealed_event_id,
    sha256_hex,
)
from .storage import Storage

REQUIRED_REVIEW_MINUTES = 30


class AdjudicationError(ValueError):
    """业务校验失败（输入不合法、状态不允许等）。"""


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def _require_int(obj: dict, key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AdjudicationError(f"字段 {key} 必须为整数: {obj!r}")
    return value


def _require_str(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise AdjudicationError(f"字段 {key} 必须为非空字符串: {obj!r}")
    return value


def _validate_alert(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise AdjudicationError(f"告警必须是对象: {raw!r}")
    alert = {
        "source_event_id": _require_str(raw, "source_event_id"),
        "channel": _require_str(raw, "channel"),
        "site": _require_str(raw, "site"),
        "area": _require_str(raw, "area"),
        "minute": _require_int(raw, "minute"),
        "alert_type": _require_str(raw, "alert_type"),
    }
    if alert["minute"] < 0:
        raise AdjudicationError(f"告警分钟不能为负: {raw!r}")
    return alert


def _validate_batch(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise AdjudicationError(f"批次必须是对象: {raw!r}")
    batch = {
        "batch_id": _require_str(raw, "batch_id"),
        "rule_version": _require_str(raw, "rule_version"),
        "watermark": _require_int(raw, "watermark"),
        "rules": raw.get("rules"),
        "alerts": raw.get("alerts", []),
    }
    if batch["watermark"] < 0:
        raise AdjudicationError("watermark 不能为负")
    # 含告警的批次必须随批声明规则表（规则版本归属与边判定的依据）；
    # 纯水位推进的空批次允许省略规则。
    if batch["alerts"]:
        if not isinstance(batch["rules"], list) or not batch["rules"]:
            raise AdjudicationError(
                f"批次 {batch['batch_id']} 含告警，必须声明非空规则表"
            )
        # 规则表合法性（domain 负责结构校验）
        domain.build_rule_index(batch["rules"])
    elif batch["rules"] is not None:
        domain.build_rule_index(batch["rules"])
    if not isinstance(batch["alerts"], list):
        raise AdjudicationError(f"批次 {batch['batch_id']} 的 alerts 必须是列表")
    batch["alerts"] = [_validate_alert(a) for a in batch["alerts"]]
    return batch


def _validate_payload(payload: Any) -> tuple[list[dict], list[dict]]:
    if not isinstance(payload, dict):
        raise AdjudicationError("输入文件顶层必须是 JSON 对象")
    batches_raw = payload.get("batches", [])
    schedules_raw = payload.get("schedules", [])
    if not isinstance(batches_raw, list) or not isinstance(schedules_raw, list):
        raise AdjudicationError("batches 与 schedules 必须是列表")
    batches = [_validate_batch(b) for b in batches_raw]
    return batches, schedules_raw


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# import-batch
# ---------------------------------------------------------------------------

def import_batch(store: Storage, payload: dict) -> dict:
    """导入一个输入文件（可含多个批次与多份排班），整文件单事务、可安全重放。"""
    batches, schedules_raw = _validate_payload(payload)
    if not batches and not schedules_raw:
        raise AdjudicationError("输入文件没有任何批次或排班")

    # 内容哈希覆盖全部业务字段，重放时逐字节核对
    content = {"batches": batches, "schedules": schedules_raw}
    file_hash = sha256_hex(canonical_bytes(content))

    result: dict[str, Any] = {
        "content_hash": file_hash,
        "batches": [],
        "schedules": [],
    }
    with store.transaction():
        # -- 排班先处理（封存前即可用）；版本存在则核对内容，不存在则新建 --------
        for raw in schedules_raw:
            if not isinstance(raw, dict):
                raise AdjudicationError(f"排班声明必须是对象: {raw!r}")
            version = _require_str(raw, "schedule_version")
            site = _require_str(raw, "site")
            entries_raw = raw.get("entries")
            if not isinstance(entries_raw, list):
                raise AdjudicationError(f"排班 {version} 的 entries 必须是列表")
            entries = [domain.normalize_schedule_entry(e) for e in entries_raw]
            sched_hash = sha256_hex(
                canonical_bytes({"site": site, "entries": entries})
            )
            existing = store.get_schedule_version(version)
            if existing is not None:
                if existing["content_hash"] != sched_hash:
                    raise AdjudicationError(
                        f"排班版本 {version} 已存在且内容不同；更正排班必须新建版本"
                    )
                result["schedules"].append(
                    {"schedule_version": version, "site": site, "replayed": True}
                )
                continue
            store.insert_schedule_version(version, site, sched_hash, _now_iso())
            for e in entries:
                store.insert_schedule_entry(
                    version, site, e["person"], e["group"], e["start"], e["end"]
                )
            result["schedules"].append(
                {
                    "schedule_version": version,
                    "site": site,
                    "replayed": False,
                    "entries": len(entries),
                }
            )

        # -- 批次 -------------------------------------------------------------
        for batch in batches:
            result["batches"].append(_import_one_batch(store, batch))

    return result


def _import_one_batch(store: Storage, batch: dict) -> dict:
    batch_id = batch["batch_id"]
    rule_version = batch["rule_version"]
    watermark = batch["watermark"]
    rules = batch["rules"]
    alerts = batch["alerts"]

    # 空批次（仅推进 watermark）可以不声明规则
    rule_index = domain.build_rule_index(rules) if rules else {}
    global_max_diff = max(rule_index.values()) if rule_index else None
    rule_payload = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    rule_hash = sha256_hex(canonical_bytes(rules))

    # 幂等：批次已存在 → 必须逐字节一致，然后整体跳过
    existing_batch = store.get_batch(batch_id)
    batch_hash = sha256_hex(
        canonical_bytes(
            {
                "rule_version": rule_version,
                "watermark": watermark,
                "rules": rules,
                "alerts": alerts,
            }
        )
    )
    if existing_batch is not None:
        if existing_batch["content_hash"] != batch_hash:
            raise AdjudicationError(
                f"批次 {batch_id} 已存在但内容不同，拒绝改写历史"
            )
        return {
            "batch_id": batch_id,
            "replayed": True,
            "imported_alerts": 0,
            "late_alerts": 0,
            "new_edges": 0,
            "watermark": existing_batch["watermark"],
        }

    # 规则版本：存在则内容必须一致；否则登记新版本（新规则只影响本批起的新边）。
    # 空批次不携带规则，只做水位推进，不登记规则版本。
    if rules:
        existing_rule = store.get_rule_version(rule_version)
        if existing_rule is not None:
            if existing_rule["content_hash"] != rule_hash:
                raise AdjudicationError(
                    f"规则版本 {rule_version} 已存在且规则内容不同；改变规则必须新建版本"
                )
        else:
            store.insert_rule_version(
                rule_version,
                rule_payload,
                rule_hash,
                global_max_diff,
                [(a, b, d) for (a, b), d in sorted(rule_index.items())],
            )

    # watermark 单调推进（非递减；下降直接拒绝）
    current_wm = store.get_watermark()
    if current_wm is not None and watermark < current_wm:
        raise AdjudicationError(
            f"watermark 不能回退：当前 {current_wm}，批次声明 {watermark}"
        )

    # 源事件 ID 去重（批次内 + 与库内活动/迟到告警冲突）
    seen_ids: set[str] = set()
    for alert in alerts:
        sid = alert["source_event_id"]
        if sid in seen_ids:
            raise AdjudicationError(f"批次 {batch_id} 内源事件 ID 重复: {sid}")
        seen_ids.add(sid)
        if store.alert_exists(sid) or store.late_alert_exists(sid):
            raise AdjudicationError(
                f"源事件 ID {sid} 已在库中且属于其他批次，拒绝重复导入"
            )

    # 迟到判定（字面规则）：事件时间严格小于“导入前”当前 watermark 的新告警
    # 入迟到档，不参与关联；首批导入前尚无 watermark，不存在迟到。
    late_wm = current_wm
    active_new: list[dict] = []
    late_count = 0
    for alert in alerts:
        if late_wm is not None and alert["minute"] < late_wm:
            store.insert_late_alert(alert, rule_version, batch_id, late_wm)
            late_count += 1
        else:
            store.insert_alert(alert, rule_version, batch_id)
            active_new.append(alert)

    # 跨通道候选边：只为本批新告警寻找边（旧边已在历史批次中定案，永不重算）
    all_active = {r["source_event_id"]: dict(r) for r in store.get_active_alerts()}
    new_edges = 0
    for alert in active_new:
        for other_id, other in all_active.items():
            if other_id == alert["source_event_id"]:
                continue
            if domain.edge_is_legal(
                channel_a=alert["channel"],
                channel_b=other["channel"],
                site_a=alert["site"],
                site_b=other["site"],
                area_a=alert["area"],
                area_b=other["area"],
                type_a=alert["alert_type"],
                type_b=other["alert_type"],
                minute_a=alert["minute"],
                minute_b=other["minute"],
                rule_index=rule_index,
            ):
                a, b = sorted((alert["source_event_id"], other_id))
                if not store.edge_exists(a, b):
                    store.insert_edge(
                        a,
                        b,
                        alert["site"],
                        alert["area"],
                        rule_version,
                        abs(alert["minute"] - other["minute"]),
                    )
                    new_edges += 1

    store.set_meta("watermark", str(watermark))
    store.insert_batch(
        batch_id,
        batch_hash,
        rule_version,
        watermark,
        _now_iso(),
        len(alerts),
    )
    return {
        "batch_id": batch_id,
        "replayed": False,
        "rule_version": rule_version,
        "imported_alerts": len(active_new),
        "late_alerts": late_count,
        "new_edges": new_edges,
        "watermark": watermark,
    }


# ---------------------------------------------------------------------------
# 共享：活动图与分量
# ---------------------------------------------------------------------------

def _load_active_graph(store: Storage) -> tuple[dict[str, dict], dict[str, set[str]], list]:
    alerts = {r["source_event_id"]: dict(r) for r in store.get_active_alerts()}
    adjacency: dict[str, set[str]] = {sid: set() for sid in alerts}
    edge_rows = store.get_edges()
    for row in edge_rows:
        a, b = row["source_a"], row["source_b"]
        if a in adjacency and b in adjacency:
            adjacency[a].add(b)
            adjacency[b].add(a)
    return alerts, adjacency, edge_rows


def _component_rule_version(
    store: Storage, members: list[str], alerts: dict[str, dict]
) -> str:
    """事件绑定的规则版本 = 最近触达该分量的批次（按批次登记顺序）的规则版本。"""
    batch_ids = {alerts[m]["batch_id"] for m in members}
    placeholders = ",".join("?" for _ in batch_ids)
    rows = store.conn.execute(
        f"SELECT batch_id, rule_version FROM batches WHERE batch_id IN ({placeholders})",
        tuple(batch_ids),
    ).fetchall()
    # batches 表 rowid 即导入顺序；取最后导入的批次
    latest = rows[-1]
    return latest["rule_version"]


# ---------------------------------------------------------------------------
# seal
# ---------------------------------------------------------------------------

def seal(store: Storage) -> dict:
    """封存所有已越过安全水位的连通分量，单事务、确定性输出。"""
    with store.transaction():
        wm = store.get_watermark()
        if wm is None:
            raise AdjudicationError("尚未导入任何批次，没有 watermark，无法封存")

        # 全局最大时间差：跨所有已声明规则版本，确保任何规则下的后到边都来不及串连
        row = store.conn.execute(
            "SELECT MAX(max_diff) AS m FROM rule_versions"
        ).fetchone()
        global_max_diff = row["m"]

        alerts, adjacency, edge_rows = _load_active_graph(store)
        components = domain.connected_components(list(alerts), adjacency)
        edge_by_pair = {(r["source_a"], r["source_b"]): dict(r) for r in edge_rows}

        sealed: list[dict] = []
        for members in components:
            max_minute = max(alerts[m]["minute"] for m in members)
            # 严格小于：恰相等时仍可能有后到合法边，不予封存
            if not (max_minute + global_max_diff < wm):
                continue

            site = alerts[members[0]]["site"]
            area = alerts[members[0]]["area"]
            rule_version = _component_rule_version(store, members, alerts)
            event_id = sealed_event_id(rule_version, members)

            comp_edges = []
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    pair = tuple(sorted((a, b)))
                    if pair in edge_by_pair:
                        comp_edges.append(edge_by_pair[pair])
            comp_edges.sort(key=lambda e: (e["source_a"], e["source_b"]))
            edge_pairs = [(e["source_a"], e["source_b"]) for e in comp_edges]

            member_alerts = [alerts[m] for m in members]
            summary = event_summary_hash(member_alerts, edge_pairs)

            schedule_version = store.latest_schedule_version(site)
            deadline_minute: int | None = None
            coverage_minutes: int | None = None
            if schedule_version is not None:
                entries = [
                    {
                        "person": r["person"],
                        "group": r["person_group"],
                        "start": r["start_minute"],
                        "end": r["end_minute"],
                    }
                    for r in store.get_schedule_entries(schedule_version)
                ]
                deadline_minute = domain.compute_deadline(
                    entries, wm, REQUIRED_REVIEW_MINUTES
                )
                if deadline_minute is not None:
                    coverage_minutes = domain.count_coverage_minutes(
                        entries, wm, deadline_minute
                    )

            receipt = {
                "event_id": event_id,
                "rule_version": rule_version,
                "site": site,
                "area": area,
                "watermark": wm,
                "global_max_diff": global_max_diff,
                "max_minute": max_minute,
                "members": [
                    {
                        "source_event_id": a["source_event_id"],
                        "channel": a["channel"],
                        "site": a["site"],
                        "area": a["area"],
                        "minute": a["minute"],
                        "alert_type": a["alert_type"],
                    }
                    for a in sorted(member_alerts, key=lambda a: a["source_event_id"])
                ],
                "edges": [
                    {
                        "source_a": e["source_a"],
                        "source_b": e["source_b"],
                        "rule_version": e["rule_version"],
                        "diff_minutes": e["diff_minutes"],
                    }
                    for e in comp_edges
                ],
                "schedule_version": schedule_version,
                "deadline_minute": deadline_minute,
                "coverage_minutes": coverage_minutes,
                "summary_hash": summary,
            }

            if store.sealed_event_exists(event_id):
                # 幂等防御：同 ID 事件已封存则不重复写入
                continue
            store.insert_sealed_event(
                {
                    "event_id": event_id,
                    "rule_version": rule_version,
                    "schedule_version": schedule_version,
                    "site": site,
                    "area": area,
                    "watermark": wm,
                    "max_minute": max_minute,
                    "deadline_minute": deadline_minute,
                    "review_status": "pending",
                    "summary_hash": summary,
                    "receipt_json": json.dumps(
                        receipt, sort_keys=True, ensure_ascii=False
                    ),
                    "sealed_at": _now_iso(),
                    "members": members,
                }
            )
            # 冻结后移出活动图；新规则再也无法触及这些告警与边
            for e in comp_edges:
                store.conn.execute(
                    "DELETE FROM edges WHERE source_a = ? AND source_b = ?",
                    (e["source_a"], e["source_b"]),
                )
            for m in members:
                store.delete_alert(m)

            sealed.append(
                {
                    "event_id": event_id,
                    "members": members,
                    "edge_count": len(comp_edges),
                    "watermark": wm,
                    "schedule_version": schedule_version,
                    "deadline_minute": deadline_minute,
                    "deadline_note": (
                        "无可排期限" if deadline_minute is None else "已排期限"
                    ),
                }
            )

        remaining = len(store.get_active_alerts())
    return {
        "watermark": wm,
        "sealed_count": len(sealed),
        "sealed_events": sealed,
        "staged_alerts_remaining": remaining,
    }


# ---------------------------------------------------------------------------
# review（双签）
# ---------------------------------------------------------------------------

def review_sign(
    store: Storage,
    event_id: str,
    person: str,
    sign_minute: int,
    expected_summary_hash: str | None = None,
) -> dict:
    """记录一次签收；两签可异步、互换顺序，同内容签收幂等。"""
    if not isinstance(sign_minute, int) or isinstance(sign_minute, bool):
        raise AdjudicationError("sign_minute 必须为整数")
    with store.transaction():
        event = store.get_sealed_event(event_id)
        if event is None:
            raise AdjudicationError(f"封存事件不存在: {event_id}")
        if expected_summary_hash is not None and \
                expected_summary_hash != event["summary_hash"]:
            raise AdjudicationError("签收绑定的事件摘要与封存摘要不一致")

        # 幂等优先：同人同事件同分钟的同内容签收直接命中，即使事件已审查完成
        already = store.conn.execute(
            "SELECT id FROM signatures WHERE event_id = ? AND person = ? AND sign_minute = ?",
            (event_id, person, sign_minute),
        ).fetchone()
        if already is not None:
            sig_rows = store.get_signatures(event_id)
            groups = {r["person_group"] for r in sig_rows}
            return {
                "event_id": event_id,
                "person": person,
                "group": next(
                    r["person_group"] for r in sig_rows
                    if r["person"] == person and r["sign_minute"] == sign_minute
                ),
                "sign_minute": sign_minute,
                "replayed": True,
                "signature_count": len(sig_rows),
                "distinct_groups": sorted(groups),
                "review_status": event["review_status"],
            }

        # 新签收：事件已完成双签则不再接受
        if event["review_status"] == "reviewed":
            raise AdjudicationError(f"事件 {event_id} 已完成双签，不再接受签收")

        wm = event["watermark"]
        deadline = event["deadline_minute"]
        if deadline is None:
            raise AdjudicationError(
                f"事件 {event_id} 无可排期限（覆盖分钟不足），不能签收"
            )
        if not (wm <= sign_minute < deadline):
            raise AdjudicationError(
                f"签发时间 {sign_minute} 不在有效区间 [{wm}, {deadline}) 内"
            )

        schedule_version = event["schedule_version"]
        entries = [
            {
                "person": r["person"],
                "group": r["person_group"],
                "start": r["start_minute"],
                "end": r["end_minute"],
            }
            for r in store.get_schedule_entries(schedule_version)
        ]
        on_duty = domain.persons_on_duty(entries, sign_minute)
        if person not in on_duty:
            raise AdjudicationError(
                f"人员 {person} 在第 {sign_minute} 分钟不当班（排班版本 {schedule_version}）"
            )
        group = on_duty[person]

        store.insert_signature(
            event_id, person, group, sign_minute, event["summary_hash"]
        )

        sig_rows = store.get_signatures(event_id)
        groups = {r["person_group"] for r in sig_rows}
        persons = {r["person"] for r in sig_rows}
        if len(groups) >= 2 and len(persons) >= 2:
            status = "reviewed"
        else:
            status = "pending"
        store.update_review_status(event_id, status)

        return {
            "event_id": event_id,
            "person": person,
            "group": group,
            "sign_minute": sign_minute,
            "replayed": False,
            "signature_count": len(sig_rows),
            "distinct_groups": sorted(groups),
            "review_status": status,
        }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _staged_snapshot(store: Storage) -> dict:
    alerts, adjacency, edge_rows = _load_active_graph(store)
    components = domain.connected_components(list(alerts), adjacency)
    edge_by_pair = {(r["source_a"], r["source_b"]): dict(r) for r in edge_rows}
    out = []
    for members in components:
        edges = []
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = tuple(sorted((a, b)))
                if pair in edge_by_pair:
                    edges.append(
                        {
                            "source_a": pair[0],
                            "source_b": pair[1],
                            "rule_version": edge_by_pair[pair]["rule_version"],
                            "diff_minutes": edge_by_pair[pair]["diff_minutes"],
                        }
                    )
        out.append(
            {
                "members": sorted(members),
                "edges": sorted(edges, key=lambda e: (e["source_a"], e["source_b"])),
                "max_minute": max(alerts[m]["minute"] for m in members),
            }
        )
    return {"component_count": len(out), "components": out}


def build_report(store: Storage) -> dict:
    with store.snapshot():
        wm = store.get_watermark()
        staged = _staged_snapshot(store)
        late = [dict(r) for r in store.get_late_alerts()]
        sealed = []
        for row in store.list_sealed_events():
            receipt = json.loads(row["receipt_json"])
            sigs = [
                {
                    "person": r["person"],
                    "group": r["person_group"],
                    "sign_minute": r["sign_minute"],
                    "summary_hash": r["summary_hash"],
                }
                for r in store.get_signatures(row["event_id"])
            ]
            sealed.append(
                {
                    "event_id": row["event_id"],
                    "site": row["site"],
                    "area": row["area"],
                    "rule_version": row["rule_version"],
                    "schedule_version": row["schedule_version"],
                    "watermark": row["watermark"],
                    "max_minute": row["max_minute"],
                    "deadline_minute": row["deadline_minute"],
                    "deadline_note": (
                        "无可排期限" if row["deadline_minute"] is None else "已排期限"
                    ),
                    "review_status": row["review_status"],
                    "summary_hash": row["summary_hash"],
                    "members": store.get_event_members(row["event_id"]),
                    "edges": receipt["edges"],
                    "coverage_minutes": receipt["coverage_minutes"],
                    "signatures": sigs,
                }
            )
        rule_versions = [
            {
                "rule_version": r["rule_version"],
                "max_diff": r["max_diff"],
                "content_hash": r["content_hash"],
            }
            for r in store.conn.execute(
                "SELECT * FROM rule_versions ORDER BY rowid"
            ).fetchall()
        ]
        schedule_versions = [
            {
                "schedule_version": r["schedule_version"],
                "site": r["site"],
                "content_hash": r["content_hash"],
            }
            for r in store.conn.execute(
                "SELECT * FROM schedule_versions ORDER BY rowid"
            ).fetchall()
        ]
    return {
        "watermark": wm,
        "rule_versions": rule_versions,
        "schedule_versions": schedule_versions,
        "staged": staged,
        "late_alerts": late,
        "sealed_events": sealed,
    }


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def verify(store: Storage) -> dict:
    """独立复核全部持久化状态，返回问题清单（空清单表示全部通过）。"""
    problems: list[str] = []
    with store.snapshot():
        # 1. 规则版本内容哈希与配对表
        for rv in store.conn.execute(
            "SELECT * FROM rule_versions ORDER BY rowid"
        ).fetchall():
            rules = json.loads(rv["payload_json"])
            if sha256_hex(canonical_bytes(rules)) != rv["content_hash"]:
                problems.append(f"规则版本 {rv['rule_version']} 内容哈希不符")
            try:
                index = domain.build_rule_index(rules)
            except domain.RuleError as exc:
                problems.append(f"规则版本 {rv['rule_version']} 无法重建: {exc}")
                continue
            if max(index.values()) != rv["max_diff"]:
                problems.append(f"规则版本 {rv['rule_version']} 全局最大时差不符")
            stored = store.get_rule_index(rv["rule_version"])
            if stored != index:
                problems.append(f"规则版本 {rv['rule_version']} 配对表与声明不一致")

        # 2. 排班版本内容哈希
        for sv in store.conn.execute(
            "SELECT * FROM schedule_versions ORDER BY rowid"
        ).fetchall():
            entries = [
                {
                    "person": r["person"],
                    "group": r["person_group"],
                    "start": r["start_minute"],
                    "end": r["end_minute"],
                }
                for r in store.get_schedule_entries(sv["schedule_version"])
            ]
            expect = sha256_hex(
                canonical_bytes({"site": sv["site"], "entries": entries})
            )
            if expect != sv["content_hash"]:
                problems.append(f"排班版本 {sv['schedule_version']} 内容哈希不符")

        # 3. 活动边必须在其规则版本下合法，且两端均为活动告警
        active = {r["source_event_id"]: dict(r) for r in store.get_active_alerts()}
        for edge in store.get_edges():
            a = active.get(edge["source_a"])
            b = active.get(edge["source_b"])
            if a is None or b is None:
                problems.append(
                    f"边 {edge['source_a']}--{edge['source_b']} 端点不是活动告警"
                )
                continue
            index = store.get_rule_index(edge["rule_version"])
            if not domain.edge_is_legal(
                channel_a=a["channel"],
                channel_b=b["channel"],
                site_a=a["site"],
                site_b=b["site"],
                area_a=a["area"],
                area_b=b["area"],
                type_a=a["alert_type"],
                type_b=b["alert_type"],
                minute_a=a["minute"],
                minute_b=b["minute"],
                rule_index=index,
            ):
                problems.append(
                    f"边 {edge['source_a']}--{edge['source_b']} 不满足规则版本 "
                    f"{edge['rule_version']} 的合法性条件"
                )
            if abs(a["minute"] - b["minute"]) != edge["diff_minutes"]:
                problems.append(
                    f"边 {edge['source_a']}--{edge['source_b']} 时差记录错误"
                )

        # 4. 活动告警不应出现在任何封存事件中
        member_rows = store.conn.execute(
            "SELECT DISTINCT source_event_id FROM event_members"
        ).fetchall()
        for row in member_rows:
            if row["source_event_id"] in active:
                problems.append(
                    f"告警 {row['source_event_id']} 既在活动表又属于封存事件"
                )

        # 5. 封存事件：ID、摘要、封存条件、期限、签收
        current_wm = store.get_watermark()
        for ev in store.list_sealed_events():
            receipt = json.loads(ev["receipt_json"])
            members = store.get_event_members(ev["event_id"])
            expect_id = sealed_event_id(ev["rule_version"], members)
            if expect_id != ev["event_id"]:
                problems.append(
                    f"事件 {ev['event_id']} ID 与规则版本+源事件 ID 推算结果不符"
                )
            expect_summary = event_summary_hash(
                receipt["members"],
                [(e["source_a"], e["source_b"]) for e in receipt["edges"]],
            )
            if expect_summary != ev["summary_hash"]:
                problems.append(f"事件 {ev['event_id']} 摘要哈希与回执证据不符")
            # 封存安全条件（严格小于）
            if not (
                receipt["max_minute"] + receipt["global_max_diff"]
                < receipt["watermark"]
            ):
                problems.append(f"事件 {ev['event_id']} 不满足封存水位条件")
            if current_wm is not None and receipt["watermark"] > current_wm:
                problems.append(f"事件 {ev['event_id']} 封存水位超过当前 watermark")
            # 期限重算（绑定排班版本，新版本不得影响旧事件）
            if ev["schedule_version"] is not None:
                entries = [
                    {
                        "person": r["person"],
                        "group": r["person_group"],
                        "start": r["start_minute"],
                        "end": r["end_minute"],
                    }
                    for r in store.get_schedule_entries(ev["schedule_version"])
                ]
                expect_deadline = domain.compute_deadline(
                    entries, ev["watermark"], REQUIRED_REVIEW_MINUTES
                )
                if expect_deadline != ev["deadline_minute"]:
                    problems.append(
                        f"事件 {ev['event_id']} 期限 {ev['deadline_minute']} 与重算值 "
                        f"{expect_deadline} 不符"
                    )
                if expect_deadline is not None:
                    cov = domain.count_coverage_minutes(
                        entries, ev["watermark"], expect_deadline
                    )
                    if cov != receipt["coverage_minutes"] or cov != REQUIRED_REVIEW_MINUTES:
                        problems.append(
                            f"事件 {ev['event_id']} 覆盖分钟计数 {receipt['coverage_minutes']} 有误"
                        )
            # 签收逐条核对
            groups_seen: set[str] = set()
            persons_seen: set[str] = set()
            seen_key: set[tuple[str, int]] = set()
            for sig in store.get_signatures(ev["event_id"]):
                if sig["summary_hash"] != ev["summary_hash"]:
                    problems.append(
                        f"事件 {ev['event_id']} 存在未绑定事件摘要的签收"
                    )
                key = (sig["person"], sig["sign_minute"])
                if key in seen_key:
                    problems.append(
                        f"事件 {ev['event_id']} 签收唯一性被破坏: {key}"
                    )
                seen_key.add(key)
                if ev["deadline_minute"] is None:
                    problems.append(
                        f"事件 {ev['event_id']} 无可排期限却存在签收"
                    )
                elif not (
                    ev["watermark"] <= sig["sign_minute"] < ev["deadline_minute"]
                ):
                    problems.append(
                        f"事件 {ev['event_id']} 签收 {sig['person']} 时间超出有效区间"
                    )
                entries = [
                    {
                        "person": r["person"],
                        "group": r["person_group"],
                        "start": r["start_minute"],
                        "end": r["end_minute"],
                    }
                    for r in store.get_schedule_entries(ev["schedule_version"])
                ]
                on_duty = domain.persons_on_duty(entries, sig["sign_minute"])
                if sig["person"] not in on_duty:
                    problems.append(
                        f"事件 {ev['event_id']} 签收人 {sig['person']} 签发时不当班"
                    )
                elif on_duty[sig["person"]] != sig["person_group"]:
                    problems.append(
                        f"事件 {ev['event_id']} 签收人 {sig['person']} 组信息与排班不符"
                    )
                groups_seen.add(sig["person_group"])
                persons_seen.add(sig["person"])
            expected_status = (
                "reviewed"
                if len(groups_seen) >= 2 and len(persons_seen) >= 2
                else "pending"
            )
            if ev["review_status"] != expected_status:
                problems.append(
                    f"事件 {ev['event_id']} 审查状态 {ev['review_status']} 与签收证据不符"
                )

        # 6. 迟到档：分钟必须严格小于记录的水位
        for late in store.get_late_alerts():
            if not (late["minute"] < late["watermark"]):
                problems.append(
                    f"迟到档告警 {late['source_event_id']} 不满足 minute < watermark"
                )

    return {"ok": not problems, "problems": problems, "checked_events": len(
        store.list_sealed_events()
    )}
