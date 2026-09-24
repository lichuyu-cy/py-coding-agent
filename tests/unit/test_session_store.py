"""阶段 19 单测：SQLite 会话存储契约。

覆盖 dependency-graph.md 阶段 19 的验收条件：读回消息顺序；冲突运行拒绝。
另覆盖设计文档模块 23 的测试清单：读回、并发冲突、尾部半写记录过滤、迁移拒绝、
摘要读回与版本冲突、损坏显式失败。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.context.compaction import (
    StructuredSummary,
    compaction_record_from_store,
    summary_payload,
    summary_record_of,
)
from coding_agent.domain.messages import MessageMeta, UserMessage, message_to_dict, new_message_id
from coding_agent.ports.store import (
    SessionAppend,
    SessionCorruptionError,
    SessionSchemaVersionError,
    SummaryRecord,
    SummaryVersionConflictError,
    UnknownSessionRecordError,
    VersionConflictError,
)
from coding_agent.storage.session_store import SQLiteSessionStore


def user_payload(session_id: str, text: str) -> dict:
    return message_to_dict(
        UserMessage(
            meta=MessageMeta(
                id=new_message_id(),
                session_id=session_id,
                run_id="run_seed",
                turn_id=1,
                created_at="2026-09-24T00:00:00Z",
            ),
            content=text,
        )
    )


def make_summary() -> StructuredSummary:
    return StructuredSummary(
        task_goal="fix the bug",
        current_progress="halfway",
        completed_work="wrote failing test",
        pending_work="apply patch",
        files_read=("a.py", "b.py"),
        files_modified=("a.py",),
        commands_executed=("pytest -q",),
        test_results="1 failed, 2 passed",
        errors=("AssertionError: boom",),
        important_decisions="keep API stable",
        next_step="edit a.py",
        source_ids=("msg_1", "msg_2"),
        unknown_fields=("something",),
    )


def make_summary_record(version: int = 1) -> SummaryRecord:
    summary = make_summary()
    return SummaryRecord(
        summary_version=version,
        covered_through_id="msg_2",
        structured_fields=summary_payload(summary),
        source_ids=summary.source_ids,
    )


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SQLiteSessionStore]:
    instance = SQLiteSessionStore(tmp_path / "sessions.db")
    yield instance
    instance.close()


class TestMessageRoundtrip:
    def test_create_append_load_preserves_order_and_versions(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        assert record.version == 0
        assert record.messages == ()

        first = store.append(
            record.session_id, 0, SessionAppend(messages=(user_payload(record.session_id, "one"),))
        )
        second = store.append(
            record.session_id, first, SessionAppend(messages=(user_payload(record.session_id, "two"),))
        )
        assert (first, second) == (1, 2)

        loaded = store.load(record.session_id)
        assert [message["content"] for message in loaded.messages] == ["one", "two"]
        assert all(message["type"] == "user" for message in loaded.messages)
        assert loaded.version == 2
        assert loaded.workspace == str(tmp_path)

    def test_batch_append_is_atomic_under_version_conflict(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        store.append(
            record.session_id,
            0,
            SessionAppend(
                messages=(
                    user_payload(record.session_id, "a"),
                    user_payload(record.session_id, "b"),
                )
            ),
        )
        # 陈旧版本重放整批：冲突且不写入任何消息
        with pytest.raises(VersionConflictError):
            store.append(
                record.session_id,
                0,
                SessionAppend(messages=(user_payload(record.session_id, "c"),)),
            )
        loaded = store.load(record.session_id)
        assert [message["content"] for message in loaded.messages] == ["a", "b"]
        assert loaded.version == 1

    def test_unknown_session_raises(self, store: SQLiteSessionStore) -> None:
        with pytest.raises(UnknownSessionRecordError):
            store.load("sess_missing")
        with pytest.raises(UnknownSessionRecordError):
            store.append("sess_missing", 0, SessionAppend(messages=()))


class TestRunLocks:
    def test_exclusive_acquire_and_release(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        assert store.acquire_run_lock(record.session_id, "run_a") is True
        assert store.acquire_run_lock(record.session_id, "run_b") is False
        # 非持有者释放不生效
        store.release_run_lock(record.session_id, "run_b")
        assert store.acquire_run_lock(record.session_id, "run_c") is False
        store.release_run_lock(record.session_id, "run_a")
        assert store.acquire_run_lock(record.session_id, "run_c") is True

    def test_lock_visible_across_connections(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        assert store.acquire_run_lock(record.session_id, "run_a") is True
        peer = SQLiteSessionStore(tmp_path / "sessions.db")
        try:
            assert peer.acquire_run_lock(record.session_id, "run_b") is False
        finally:
            peer.close()
        store.release_run_lock(record.session_id, "run_a")
        peer = SQLiteSessionStore(tmp_path / "sessions.db")
        try:
            assert peer.acquire_run_lock(record.session_id, "run_b") is True
        finally:
            peer.close()


class TestLoadIntegrity:
    def _corrupt(self, db_path: Path, sql: str, params: tuple) -> None:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def test_single_half_written_tail_row_filtered(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        store.append(
            record.session_id,
            0,
            SessionAppend(
                messages=(
                    user_payload(record.session_id, "good"),
                    user_payload(record.session_id, "torn"),
                )
            ),
        )
        self._corrupt(
            tmp_path / "sessions.db",
            "UPDATE messages SET payload = ? WHERE session_id = ? AND ordinal = 1",
            ("{half", record.session_id),
        )
        loaded = store.load(record.session_id)
        assert [message["content"] for message in loaded.messages] == ["good"]
        assert loaded.filtered_tail == 1

    def test_multiple_bad_tail_rows_refused(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        # 仅允许过滤唯一的尾部半写记录；坏行多于一条说明损坏更深，拒绝加载
        record = store.create(tmp_path)
        store.append(
            record.session_id,
            0,
            SessionAppend(
                messages=(
                    user_payload(record.session_id, "good"),
                    user_payload(record.session_id, "torn1"),
                    user_payload(record.session_id, "torn2"),
                )
            ),
        )
        self._corrupt(
            tmp_path / "sessions.db",
            "UPDATE messages SET payload = ? WHERE session_id = ? AND ordinal IN (1, 2)",
            ("{half", record.session_id),
        )
        with pytest.raises(SessionCorruptionError, match="ordinal 1 is corrupted"):
            store.load(record.session_id)

    def test_mid_history_corruption_raises(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        store.append(
            record.session_id,
            0,
            SessionAppend(
                messages=(
                    user_payload(record.session_id, "first"),
                    user_payload(record.session_id, "middle"),
                    user_payload(record.session_id, "last"),
                )
            ),
        )
        self._corrupt(
            tmp_path / "sessions.db",
            "UPDATE messages SET payload = ? WHERE session_id = ? AND ordinal = 1",
            ("{broken", record.session_id),
        )
        with pytest.raises(SessionCorruptionError, match="corrupted"):
            store.load(record.session_id)

    def test_schema_version_mismatch_rejected(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        self._corrupt(
            tmp_path / "sessions.db",
            "UPDATE sessions SET schema_version = 999 WHERE session_id = ?",
            (record.session_id,),
        )
        with pytest.raises(SessionSchemaVersionError, match="migration rejected"):
            store.load(record.session_id)


class TestSummaries:
    def test_summary_roundtrip_and_version_conflict(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        summary = make_summary_record(version=1)
        version = store.append(record.session_id, 0, SessionAppend(summaries=(summary,)))
        assert version == 1

        loaded = store.load(record.session_id)
        assert len(loaded.summaries) == 1
        restored = loaded.summaries[0]
        assert restored.summary_version == 1
        assert restored.covered_through_id == "msg_2"
        assert restored.source_ids == ("msg_1", "msg_2")
        assert restored.structured_fields["task_goal"] == "fix the bug"
        assert restored.structured_fields["files_read"] == ["a.py", "b.py"]

        with pytest.raises(SummaryVersionConflictError):
            store.append(record.session_id, version, SessionAppend(summaries=(summary,)))

    def test_summary_corruption_raises(self, store: SQLiteSessionStore, tmp_path: Path) -> None:
        record = store.create(tmp_path)
        store.append(record.session_id, 0, SessionAppend(summaries=(make_summary_record(),)))
        connection = sqlite3.connect(tmp_path / "sessions.db")
        try:
            connection.execute(
                "UPDATE summaries SET structured_fields = ? WHERE session_id = ?",
                ("{broken", record.session_id),
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(SessionCorruptionError, match="summary"):
            store.load(record.session_id)


class TestSummaryConversions:
    def test_summary_payload_roundtrip(self) -> None:
        from coding_agent.context.compaction import summary_from_payload

        summary = make_summary()
        payload = summary_payload(summary)
        assert json.loads(json.dumps(payload)) == payload  # JSON 友好
        assert summary_from_payload(payload) == summary

    def test_payload_tolerates_missing_fields(self) -> None:
        from coding_agent.context.compaction import summary_from_payload

        summary = summary_from_payload({})
        assert summary.task_goal == "unknown"
        assert summary.files_read == ()
        assert summary.unknown_fields == ()

    def test_compaction_record_store_roundtrip(self) -> None:
        from coding_agent.context.compaction import CompactionRecord

        record = CompactionRecord(
            summary=make_summary(),
            covered_through_id="msg_2",
            preserved_ids=("msg_3",),
            token_before=1234,
            token_after=56,
            summary_version=3,
        )
        persisted = summary_record_of(record)
        assert persisted.summary_version == 3
        assert persisted.source_ids == record.summary.source_ids
        restored = compaction_record_from_store(persisted)
        assert restored.summary == record.summary
        assert restored.covered_through_id == "msg_2"
        assert restored.summary_version == 3
        # token 对比值属观测数据，不参与持久化
        assert (restored.token_before, restored.token_after) == (0, 0)
        assert restored.preserved_ids == ()
