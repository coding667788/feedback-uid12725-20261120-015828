"""SQLite 存储层：schema、连接管理与一致快照访问助手。

所有写操作都在单事务内完成；读操作使用 BEGIN DEFERRED 取得一致性快照。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- 键值元数据（当前 watermark 等）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 规则版本：改变规则必须新建版本，规则内容在版本内不可变
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_version TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    max_diff     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_pairs (
    rule_version TEXT NOT NULL REFERENCES rule_versions(rule_version),
    type_a       TEXT NOT NULL,
    type_b       TEXT NOT NULL,
    max_diff     INTEGER NOT NULL,
    PRIMARY KEY (rule_version, type_a, type_b)
);

-- 导入批次登记（幂等与中断恢复）
CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    rule_version   TEXT NOT NULL,
    watermark      INTEGER NOT NULL,
    imported_at    TEXT NOT NULL,
    alert_count    INTEGER NOT NULL
);

-- 排班版本：更正排班必须新建版本，旧封存永久绑定旧版本
CREATE TABLE IF NOT EXISTS schedule_versions (
    schedule_version TEXT PRIMARY KEY,
    site             TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_version TEXT NOT NULL REFERENCES schedule_versions(schedule_version),
    site             TEXT NOT NULL,
    person           TEXT NOT NULL,
    person_group     TEXT NOT NULL,
    start_minute     INTEGER NOT NULL,
    end_minute       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_ver ON schedule_entries(schedule_version);

-- 活动告警（参与关联；迟到告警不进此表）
CREATE TABLE IF NOT EXISTS alerts (
    source_event_id TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    site            TEXT NOT NULL,
    area            TEXT NOT NULL,
    minute          INTEGER NOT NULL,
    alert_type      TEXT NOT NULL,
    rule_version    TEXT NOT NULL,
    batch_id        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_site_area ON alerts(site, area);

-- 迟到档：事件时间严格小于导入时当前 watermark 的新告警
CREATE TABLE IF NOT EXISTS late_alerts (
    source_event_id TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    site            TEXT NOT NULL,
    area            TEXT NOT NULL,
    minute          INTEGER NOT NULL,
    alert_type      TEXT NOT NULL,
    rule_version    TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    watermark       INTEGER NOT NULL
);

-- 候选边（无向，端点按 source_event_id 排序存储）
CREATE TABLE IF NOT EXISTS edges (
    source_a     TEXT NOT NULL,
    source_b     TEXT NOT NULL,
    site         TEXT NOT NULL,
    area         TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    diff_minutes INTEGER NOT NULL,
    PRIMARY KEY (source_a, source_b),
    CHECK (source_a < source_b)
);

-- 封存事件（不可变）
CREATE TABLE IF NOT EXISTS sealed_events (
    event_id           TEXT PRIMARY KEY,
    rule_version       TEXT NOT NULL,
    schedule_version   TEXT,
    site               TEXT NOT NULL,
    area               TEXT NOT NULL,
    watermark          INTEGER NOT NULL,
    max_minute         INTEGER NOT NULL,
    deadline_minute    INTEGER,
    review_status      TEXT NOT NULL DEFAULT 'pending',
    summary_hash       TEXT NOT NULL,
    receipt_json       TEXT NOT NULL,
    sealed_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sealed_site ON sealed_events(site);

-- 封存事件成员
CREATE TABLE IF NOT EXISTS event_members (
    event_id        TEXT NOT NULL REFERENCES sealed_events(event_id),
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (event_id, source_event_id)
);

-- 双签（同内容签收幂等：event_id+person+minute 唯一）
CREATE TABLE IF NOT EXISTS signatures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL REFERENCES sealed_events(event_id),
    person        TEXT NOT NULL,
    person_group  TEXT NOT NULL,
    sign_minute   INTEGER NOT NULL,
    summary_hash  TEXT NOT NULL,
    UNIQUE (event_id, person, sign_minute)
);
"""


class Storage:
    """SQLite 访问封装。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = DELETE")
        self.conn.execute("PRAGMA synchronous = FULL")
        self._in_tx = False
        self.conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self.conn.close()

    # -- 事务 ---------------------------------------------------------------

    @contextmanager
    def transaction(self):
        """显式写事务：正常结束提交，异常回滚（批次中断后可原样重放）。"""
        if self._in_tx:
            yield self
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._in_tx = False

    @contextmanager
    def snapshot(self):
        """一致性读快照（DEFERRED 事务，不产生写锁）。"""
        if self._in_tx:
            yield self
            return
        self.conn.execute("BEGIN DEFERRED")
        try:
            yield self
        finally:
            self.conn.rollback()

    # -- 元数据 -------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_watermark(self) -> int | None:
        raw = self.get_meta("watermark")
        return int(raw) if raw is not None else None

    # -- 规则 ---------------------------------------------------------------

    def rule_version_exists(self, rule_version: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM rule_versions WHERE rule_version = ?", (rule_version,)
        ).fetchone() is not None

    def get_rule_version(self, rule_version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rule_versions WHERE rule_version = ?", (rule_version,)
        ).fetchone()

    def get_rule_index(self, rule_version: str) -> dict[tuple[str, str], int]:
        rows = self.conn.execute(
            "SELECT type_a, type_b, max_diff FROM rule_pairs WHERE rule_version = ?",
            (rule_version,),
        ).fetchall()
        return {(r["type_a"], r["type_b"]): r["max_diff"] for r in rows}

    def insert_rule_version(
        self,
        rule_version: str,
        payload_json: str,
        content_hash: str,
        max_diff: int,
        pairs: list[tuple[str, str, int]],
    ) -> None:
        self.conn.execute(
            "INSERT INTO rule_versions(rule_version, payload_json, content_hash, max_diff)"
            " VALUES(?, ?, ?, ?)",
            (rule_version, payload_json, content_hash, max_diff),
        )
        self.conn.executemany(
            "INSERT INTO rule_pairs(rule_version, type_a, type_b, max_diff)"
            " VALUES(?, ?, ?, ?)",
            [(rule_version, a, b, d) for a, b, d in pairs],
        )

    # -- 批次 ---------------------------------------------------------------

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()

    def insert_batch(
        self,
        batch_id: str,
        content_hash: str,
        rule_version: str,
        watermark: int,
        imported_at: str,
        alert_count: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO batches(batch_id, content_hash, rule_version, watermark,"
            " imported_at, alert_count) VALUES(?, ?, ?, ?, ?, ?)",
            (batch_id, content_hash, rule_version, watermark, imported_at, alert_count),
        )

    # -- 排班 ---------------------------------------------------------------

    def schedule_version_exists(self, schedule_version: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM schedule_versions WHERE schedule_version = ?",
            (schedule_version,),
        ).fetchone() is not None

    def get_schedule_version(self, schedule_version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM schedule_versions WHERE schedule_version = ?",
            (schedule_version,),
        ).fetchone()

    def latest_schedule_version(self, site: str) -> str | None:
        row = self.conn.execute(
            "SELECT schedule_version FROM schedule_versions WHERE site = ?"
            " ORDER BY rowid DESC LIMIT 1",
            (site,),
        ).fetchone()
        return row["schedule_version"] if row else None

    def insert_schedule_version(
        self, schedule_version: str, site: str, content_hash: str, created_at: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO schedule_versions(schedule_version, site, content_hash, created_at)"
            " VALUES(?, ?, ?, ?)",
            (schedule_version, site, content_hash, created_at),
        )

    def insert_schedule_entry(
        self,
        schedule_version: str,
        site: str,
        person: str,
        person_group: str,
        start_minute: int,
        end_minute: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO schedule_entries(schedule_version, site, person, person_group,"
            " start_minute, end_minute) VALUES(?, ?, ?, ?, ?, ?)",
            (schedule_version, site, person, person_group, start_minute, end_minute),
        )

    def get_schedule_entries(self, schedule_version: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT person, person_group, start_minute, end_minute"
            " FROM schedule_entries WHERE schedule_version = ? ORDER BY id",
            (schedule_version,),
        ).fetchall()

    # -- 告警与边 ------------------------------------------------------------

    def alert_exists(self, source_event_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM alerts WHERE source_event_id = ?", (source_event_id,)
        ).fetchone() is not None

    def late_alert_exists(self, source_event_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM late_alerts WHERE source_event_id = ?", (source_event_id,)
        ).fetchone() is not None

    def insert_alert(self, alert: dict, rule_version: str, batch_id: str) -> None:
        self.conn.execute(
            "INSERT INTO alerts(source_event_id, channel, site, area, minute, alert_type,"
            " rule_version, batch_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alert["source_event_id"],
                alert["channel"],
                alert["site"],
                alert["area"],
                alert["minute"],
                alert["alert_type"],
                rule_version,
                batch_id,
            ),
        )

    def insert_late_alert(
        self, alert: dict, rule_version: str, batch_id: str, watermark: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO late_alerts(source_event_id, channel, site, area, minute, alert_type,"
            " rule_version, batch_id, watermark) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alert["source_event_id"],
                alert["channel"],
                alert["site"],
                alert["area"],
                alert["minute"],
                alert["alert_type"],
                rule_version,
                batch_id,
                watermark,
            ),
        )

    def get_active_alerts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alerts ORDER BY source_event_id"
        ).fetchall()

    def get_alert(self, source_event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE source_event_id = ?", (source_event_id,)
        ).fetchone()

    def edge_exists(self, source_a: str, source_b: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM edges WHERE source_a = ? AND source_b = ?",
            (source_a, source_b),
        ).fetchone() is not None

    def insert_edge(
        self,
        source_a: str,
        source_b: str,
        site: str,
        area: str,
        rule_version: str,
        diff_minutes: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO edges(source_a, source_b, site, area, rule_version, diff_minutes)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (source_a, source_b, site, area, rule_version, diff_minutes),
        )

    def get_edges(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM edges ORDER BY source_a, source_b"
        ).fetchall()

    def delete_alert(self, source_event_id: str) -> None:
        self.conn.execute("DELETE FROM alerts WHERE source_event_id = ?", (source_event_id,))

    def delete_edges_of(self, source_event_id: str) -> None:
        self.conn.execute(
            "DELETE FROM edges WHERE source_a = ? OR source_b = ?",
            (source_event_id, source_event_id),
        )

    # -- 封存与签收 ----------------------------------------------------------

    def sealed_event_exists(self, event_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sealed_events WHERE event_id = ?", (event_id,)
        ).fetchone() is not None

    def get_sealed_event(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sealed_events WHERE event_id = ?", (event_id,)
        ).fetchone()

    def list_sealed_events(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sealed_events ORDER BY event_id"
        ).fetchall()

    def insert_sealed_event(self, event: dict) -> None:
        self.conn.execute(
            "INSERT INTO sealed_events(event_id, rule_version, schedule_version, site,"
            " area, watermark, max_minute, deadline_minute, review_status, summary_hash,"
            " receipt_json, sealed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["event_id"],
                event["rule_version"],
                event["schedule_version"],
                event["site"],
                event["area"],
                event["watermark"],
                event["max_minute"],
                event["deadline_minute"],
                event["review_status"],
                event["summary_hash"],
                event["receipt_json"],
                event["sealed_at"],
            ),
        )
        self.conn.executemany(
            "INSERT INTO event_members(event_id, source_event_id) VALUES(?, ?)",
            [(event["event_id"], m) for m in event["members"]],
        )

    def get_event_members(self, event_id: str) -> list[str]:
        return [
            r["source_event_id"]
            for r in self.conn.execute(
                "SELECT source_event_id FROM event_members WHERE event_id = ?"
                " ORDER BY source_event_id",
                (event_id,),
            ).fetchall()
        ]

    def update_review_status(self, event_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE sealed_events SET review_status = ? WHERE event_id = ?",
            (status, event_id),
        )

    def get_signatures(self, event_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM signatures WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()

    def insert_signature(
        self,
        event_id: str,
        person: str,
        person_group: str,
        sign_minute: int,
        summary_hash: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO signatures(event_id, person, person_group, sign_minute, summary_hash)"
            " VALUES(?, ?, ?, ?, ?)",
            (event_id, person, person_group, sign_minute, summary_hash),
        )

    def get_late_alerts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM late_alerts ORDER BY source_event_id"
        ).fetchall()
