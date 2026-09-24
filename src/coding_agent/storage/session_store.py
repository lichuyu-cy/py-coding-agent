"""SQLite 会话存储：原子提交、乐观版本控制、跨进程运行锁与半写尾部过滤。

事务语义：每次 append 一个事务（BEGIN IMMEDIATE）——要么整批消息进入提交历史，
要么什么都没发生（崩溃不会留下半批数据）。
加载语义：校验 schema 版本（不兼容显式失败，不迁移）；逐行解析，
仅允许过滤"尾部"破损记录（崩溃残留），中段损坏 → 会话损坏错误，拒绝继续使用。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from coding_agent.ports.store import (
    SESSION_SCHEMA_VERSION,
    SessionAppend,
    SessionCorruptionError,
    SessionRecord,
    SessionSchemaVersionError,
    SessionStoreError,
    SummaryRecord,
    SummaryVersionConflictError,
    UnknownSessionRecordError,
    VersionConflictError,
)

# 错误族定义于 ports（协议契约的一部分），此处重导出以兼容既有导入路径。
__all__ = [
    "SessionCorruptionError",
    "SessionSchemaVersionError",
    "SessionStoreError",
    "SQLiteSessionStore",
    "SummaryVersionConflictError",
    "UnknownSessionRecordError",
    "VersionConflictError",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class SQLiteSessionStore:
    """单机可靠存储：SQLite 事务 + 乐观版本 + 会话级锁表。"""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    # ---- schema ----

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                workspace TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                session_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (session_id, ordinal),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS summaries (
                session_id TEXT NOT NULL,
                summary_version INTEGER NOT NULL,
                covered_through_id TEXT NOT NULL,
                structured_fields TEXT NOT NULL,
                source_ids TEXT NOT NULL,
                PRIMARY KEY (session_id, summary_version),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS run_locks (
                session_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            );
            """
        )

    # ---- 接口 ----

    def create(self, workspace: Path | str) -> SessionRecord:
        session_id = f"sess_{uuid.uuid4().hex}"
        now = _utc_now()
        self._connection.execute(
            "INSERT INTO sessions (session_id, schema_version, workspace, version, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, ?, ?)",
            (session_id, SESSION_SCHEMA_VERSION, str(workspace), now, now),
        )
        return SessionRecord(
            session_id=session_id,
            workspace=str(workspace),
            schema_version=SESSION_SCHEMA_VERSION,
            version=0,
            messages=(),
        )

    def load(self, session_id: str, *, lenient_tail: bool = True) -> SessionRecord:
        row = self._connection.execute(
            "SELECT schema_version, workspace, version FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise UnknownSessionRecordError(session_id)
        schema_version, workspace, version = row
        if schema_version != SESSION_SCHEMA_VERSION:
            raise SessionSchemaVersionError(session_id, schema_version, SESSION_SCHEMA_VERSION)
        rows = self._connection.execute(
            "SELECT ordinal, payload FROM messages WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        messages: list[dict] = []
        filter_from: int | None = None
        for position, (ordinal, payload) in enumerate(rows):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                if position == len(rows) - 1 and lenient_tail:
                    filter_from = ordinal
                    break
                raise SessionCorruptionError(
                    f"session {session_id!r} message at ordinal {ordinal} is corrupted"
                ) from None
            messages.append(parsed)
        filtered_tail = 0
        if filter_from is not None:
            # 过滤尾部半写记录（崩溃残留）：仅允许连续尾部段
            trailing = self._connection.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND ordinal >= ?",
                (session_id, filter_from),
            ).fetchone()[0]
            if trailing != 1:
                raise SessionCorruptionError(
                    f"session {session_id!r} has {trailing} invalid tail rows; refusing to guess"
                )
            filtered_tail = trailing
        summaries = self._load_summaries(session_id)
        return SessionRecord(
            session_id=session_id,
            workspace=workspace,
            schema_version=schema_version,
            version=version,
            messages=tuple(messages),
            summaries=summaries,
            filtered_tail=filtered_tail,
        )

    def _load_summaries(self, session_id: str) -> tuple[SummaryRecord, ...]:
        rows = self._connection.execute(
            "SELECT summary_version, covered_through_id, structured_fields, source_ids"
            " FROM summaries WHERE session_id = ? ORDER BY summary_version",
            (session_id,),
        ).fetchall()
        summaries: list[SummaryRecord] = []
        for summary_version, covered_through_id, fields_json, source_ids_json in rows:
            try:
                fields = json.loads(fields_json)
                source_ids = tuple(json.loads(source_ids_json))
            except json.JSONDecodeError as exc:
                raise SessionCorruptionError(
                    f"session {session_id!r} summary v{summary_version} is corrupted"
                ) from exc
            summaries.append(
                SummaryRecord(
                    summary_version=summary_version,
                    covered_through_id=covered_through_id,
                    structured_fields=fields,
                    source_ids=source_ids,
                )
            )
        return tuple(summaries)

    def append(self, session_id: str, expected_version: int, records: SessionAppend) -> int:
        """原子追加并返回新版本；expected_version 不符即冲突。"""
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise UnknownSessionRecordError(session_id)
            current_version = row[0]
            if current_version != expected_version:
                raise VersionConflictError(session_id, expected_version, current_version)
            start = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            for offset, message in enumerate(records.messages):
                connection.execute(
                    "INSERT INTO messages (session_id, ordinal, payload) VALUES (?, ?, ?)",
                    (session_id, start + offset, json.dumps(dict(message), ensure_ascii=False)),
                )
            for summary in records.summaries:
                existing = connection.execute(
                    "SELECT 1 FROM summaries WHERE session_id = ? AND summary_version = ?",
                    (session_id, summary.summary_version),
                ).fetchone()
                if existing is not None:
                    raise SummaryVersionConflictError(session_id, summary.summary_version)
                connection.execute(
                    "INSERT INTO summaries (session_id, summary_version, covered_through_id,"
                    " structured_fields, source_ids) VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        summary.summary_version,
                        summary.covered_through_id,
                        json.dumps(dict(summary.structured_fields), ensure_ascii=False),
                        json.dumps(list(summary.source_ids), ensure_ascii=False),
                    ),
                )
            new_version = current_version + 1
            connection.execute(
                "UPDATE sessions SET version = ?, updated_at = ? WHERE session_id = ?",
                (new_version, _utc_now(), session_id),
            )
            connection.execute("COMMIT")
            return new_version
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    # ---- 运行锁 ----

    def acquire_run_lock(self, session_id: str, run_id: str) -> bool:
        try:
            self._connection.execute(
                "INSERT INTO run_locks (session_id, run_id, acquired_at) VALUES (?, ?, ?)",
                (session_id, run_id, _utc_now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_run_lock(self, session_id: str, run_id: str) -> None:
        self._connection.execute(
            "DELETE FROM run_locks WHERE session_id = ? AND run_id = ?", (session_id, run_id)
        )

    def close(self) -> None:
        self._connection.close()
