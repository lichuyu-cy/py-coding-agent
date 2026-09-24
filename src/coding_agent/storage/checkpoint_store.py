"""SQLite 检查点存储：恢复边界索引 + 工具意图 journal。

与 SQLiteSessionStore 使用同一数据库文件（不同连接，WAL 下并发安全）；
检查点通过 `session_version` 链接会话事实——不复制历史，只做恢复索引。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from coding_agent.ports.checkpoint import (
    BoundaryKind,
    Checkpoint,
    IntentStatus,
    ToolIntent,
)

__all__ = ["SQLiteCheckpointStore"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class SQLiteCheckpointStore:
    """单机检查点与意图 journal；状态转换全部为受约束的 UPDATE。"""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                session_version INTEGER NOT NULL,
                state_seq INTEGER NOT NULL,
                boundary TEXT NOT NULL,
                pending_intent_ids TEXT NOT NULL,
                workspace_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                ON checkpoints (session_id, session_version);
            CREATE TABLE IF NOT EXISTS tool_intents (
                session_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, call_id)
            );
            """
        )

    # ---- 检查点 ----

    def save(self, checkpoint: Checkpoint) -> None:
        self._connection.execute(
            "INSERT INTO checkpoints (checkpoint_id, session_id, session_version, state_seq,"
            " boundary, pending_intent_ids, workspace_fingerprint, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                checkpoint.checkpoint_id,
                checkpoint.session_id,
                checkpoint.session_version,
                checkpoint.state_seq,
                str(checkpoint.boundary),
                json.dumps(list(checkpoint.pending_intent_ids), ensure_ascii=False),
                checkpoint.workspace_fingerprint,
                checkpoint.created_at,
            ),
        )

    def latest(self, session_id: str) -> Checkpoint | None:
        row = self._connection.execute(
            "SELECT checkpoint_id, session_version, state_seq, boundary, pending_intent_ids,"
            " workspace_fingerprint, created_at FROM checkpoints WHERE session_id = ?"
            " ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        checkpoint_id, version, state_seq, boundary, pending_json, fingerprint, created_at = row
        return Checkpoint(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            session_version=version,
            state_seq=state_seq,
            boundary=BoundaryKind(boundary),
            pending_intent_ids=tuple(json.loads(pending_json)),
            workspace_fingerprint=fingerprint,
            created_at=created_at,
        )

    # ---- 工具意图 ----

    def record_intent(self, intent: ToolIntent) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO tool_intents (session_id, call_id, intent_id, run_id,"
            " tool_name, arguments_digest, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent.session_id,
                intent.call_id,
                intent.intent_id,
                intent.run_id,
                intent.tool_name,
                intent.arguments_digest,
                str(intent.status),
                intent.created_at,
                intent.updated_at,
            ),
        )

    def mark_started(self, session_id: str, call_id: str) -> None:
        self._connection.execute(
            "UPDATE tool_intents SET status = ?, updated_at = ?"
            " WHERE session_id = ? AND call_id = ? AND status = ?",
            (str(IntentStatus.STARTED), _utc_now(), session_id, call_id, str(IntentStatus.PLANNED)),
        )

    def complete_intent(self, session_id: str, call_id: str) -> None:
        self._connection.execute(
            "UPDATE tool_intents SET status = ?, updated_at = ?"
            " WHERE session_id = ? AND call_id = ?",
            (str(IntentStatus.COMPLETED), _utc_now(), session_id, call_id),
        )

    def set_intent_status(self, session_id: str, call_id: str, status: IntentStatus) -> None:
        self._connection.execute(
            "UPDATE tool_intents SET status = ?, updated_at = ?"
            " WHERE session_id = ? AND call_id = ?",
            (str(status), _utc_now(), session_id, call_id),
        )

    def open_intents(self, session_id: str) -> tuple[ToolIntent, ...]:
        rows = self._connection.execute(
            "SELECT intent_id, run_id, call_id, tool_name, arguments_digest, status,"
            " created_at, updated_at FROM tool_intents"
            " WHERE session_id = ? AND status IN (?, ?) ORDER BY rowid",
            (session_id, str(IntentStatus.PLANNED), str(IntentStatus.STARTED)),
        ).fetchall()
        return tuple(self._to_intent(session_id, row) for row in rows)

    def all_intents(self, session_id: str) -> tuple[ToolIntent, ...]:
        rows = self._connection.execute(
            "SELECT intent_id, run_id, call_id, tool_name, arguments_digest, status,"
            " created_at, updated_at FROM tool_intents WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()
        return tuple(self._to_intent(session_id, row) for row in rows)

    @staticmethod
    def _to_intent(session_id: str, row: tuple) -> ToolIntent:
        (
            intent_id,
            run_id,
            call_id,
            tool_name,
            arguments_digest,
            status,
            created_at,
            updated_at,
        ) = row
        return ToolIntent(
            intent_id=intent_id,
            session_id=session_id,
            run_id=run_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments_digest=arguments_digest,
            status=IntentStatus(status),
            created_at=created_at,
            updated_at=updated_at,
        )

    def close(self) -> None:
        self._connection.close()
