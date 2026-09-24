"""阶段 20 单测：检查点存储契约与恢复判定（模块 24/25）。

覆盖：检查点往返与最新读取、工具意图状态机（planned→started→completed）、
指纹稳定性，以及恢复判定的异常路径（悬空意图 / 无意图证据）→ STOP。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.agent.recovery import RecoveryEngine, RecoveryError, workspace_fingerprint
from coding_agent.domain.messages import (
    AssistantMessage,
    MessageMeta,
    ToolCall,
    UserMessage,
    message_to_dict,
    new_message_id,
    utc_now_rfc3339,
)
from coding_agent.domain.state import StopReason
from coding_agent.ports.checkpoint import (
    BoundaryKind,
    Checkpoint,
    IntentStatus,
    RecoveryDecision,
    ToolIntent,
)
from coding_agent.ports.store import SessionAppend
from coding_agent.storage.checkpoint_store import SQLiteCheckpointStore
from coding_agent.storage.session_store import SQLiteSessionStore

SESSION = "sess_checkpoint_test"


def make_checkpoint(version: int = 1, boundary: BoundaryKind = BoundaryKind.MODEL_RESPONSE) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=f"ckpt_{version}_{boundary.value}",
        session_id=SESSION,
        session_version=version,
        state_seq=version * 2,
        boundary=boundary,
        pending_intent_ids=(),
        workspace_fingerprint="ws1:abc",
        created_at="2026-09-24T00:00:00Z",
    )


def make_intent(call_id: str, status: IntentStatus = IntentStatus.PLANNED) -> ToolIntent:
    return ToolIntent(
        intent_id=f"intent_{call_id}",
        session_id=SESSION,
        run_id="run_seed",
        call_id=call_id,
        tool_name="write",
        arguments_digest="d1gest",
        status=status,
        created_at="2026-09-24T00:00:00Z",
        updated_at="2026-09-24T00:00:00Z",
    )


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SQLiteCheckpointStore]:
    instance = SQLiteCheckpointStore(tmp_path / "sessions.db")
    yield instance
    instance.close()


class TestCheckpointStore:
    def test_save_and_latest_roundtrip(self, store: SQLiteCheckpointStore) -> None:
        assert store.latest(SESSION) is None
        store.save(make_checkpoint(1))
        store.save(make_checkpoint(2, BoundaryKind.TOOL_RESULT))
        latest = store.latest(SESSION)
        assert latest is not None
        assert latest.session_version == 2
        assert latest.boundary is BoundaryKind.TOOL_RESULT
        assert latest.state_seq == 4

    def test_intent_lifecycle(self, store: SQLiteCheckpointStore) -> None:
        store.record_intent(make_intent("call_1"))
        store.record_intent(make_intent("call_2"))
        assert [item.call_id for item in store.open_intents(SESSION)] == ["call_1", "call_2"]

        store.mark_started(SESSION, "call_1")
        store.complete_intent(SESSION, "call_2")
        open_items = store.open_intents(SESSION)
        assert [(item.call_id, item.status) for item in open_items] == [
            ("call_1", IntentStatus.STARTED)
        ]
        all_items = store.all_intents(SESSION)
        assert [item.status for item in all_items] == [IntentStatus.STARTED, IntentStatus.COMPLETED]

        store.set_intent_status(SESSION, "call_1", IntentStatus.ABANDONED)
        assert store.open_intents(SESSION) == ()

    def test_mark_started_only_from_planned(self, store: SQLiteCheckpointStore) -> None:
        # 不存在或已完成/已解决的意图：mark_started 不产生状态回退
        store.mark_started(SESSION, "missing")
        assert store.open_intents(SESSION) == ()
        store.record_intent(make_intent("call_1"))
        store.complete_intent(SESSION, "call_1")
        store.mark_started(SESSION, "call_1")
        (intent,) = store.all_intents(SESSION)
        assert intent.status is IntentStatus.COMPLETED


class TestFingerprint:
    def test_stable_and_discriminating(self, tmp_path: Path) -> None:
        one = tmp_path / "one"
        two = tmp_path / "two"
        assert workspace_fingerprint(one) == workspace_fingerprint(one)
        assert workspace_fingerprint(one) != workspace_fingerprint(two)
        assert workspace_fingerprint(one).startswith("ws1:")
        # 相对形式与绝对形式（解析后）一致
        assert workspace_fingerprint(str(one)) == workspace_fingerprint(one)


class TestInspectIntegrity:
    """记录与 journal 不一致时拒绝猜测：STOP。"""

    def _seed_session(self, sessions: SQLiteSessionStore, tmp_path: Path) -> str:
        record = sessions.create(tmp_path)
        user = UserMessage(
            meta=MessageMeta(
                id=new_message_id(),
                session_id=record.session_id,
                run_id="run_seed",
                turn_id=1,
                created_at=utc_now_rfc3339(),
            ),
            content="do work",
        )
        assistant = AssistantMessage(
            meta=MessageMeta(
                id=new_message_id(),
                session_id=record.session_id,
                run_id="run_seed",
                turn_id=1,
                created_at=utc_now_rfc3339(),
            ),
            content="",
            stop_reason=StopReason.TOOL_CALLS,
            tool_calls=(ToolCall(id="call_x", name="write", arguments={"path": "a.py"}, ordinal=0),),
        )
        sessions.append(
            record.session_id,
            0,
            SessionAppend(messages=(message_to_dict(user), message_to_dict(assistant))),
        )
        return record.session_id

    def test_open_call_without_intent_entry_stops(self, tmp_path: Path) -> None:
        sessions = SQLiteSessionStore(tmp_path / "sessions.db")
        checkpoints = SQLiteCheckpointStore(tmp_path / "sessions.db")
        try:
            session_id = self._seed_session(sessions, tmp_path)
            engine = RecoveryEngine(sessions, checkpoints)
            plan = engine.inspect(session_id)
            assert plan.decision is RecoveryDecision.STOP
            assert "no intent journal entry" in plan.reason
        finally:
            sessions.close()
            checkpoints.close()

    def test_dangling_intent_stops(self, tmp_path: Path) -> None:
        sessions = SQLiteSessionStore(tmp_path / "sessions.db")
        checkpoints = SQLiteCheckpointStore(tmp_path / "sessions.db")
        try:
            session_id = self._seed_session(sessions, tmp_path)
            # journal 有意图但历史中没有对应调用
            checkpoints.record_intent(
                ToolIntent(
                    intent_id="intent_call_ghost",
                    session_id=session_id,
                    run_id="run_seed",
                    call_id="call_ghost",
                    tool_name="bash",
                    arguments_digest="x",
                    status=IntentStatus.PLANNED,
                    created_at=utc_now_rfc3339(),
                    updated_at=utc_now_rfc3339(),
                )
            )
            engine = RecoveryEngine(sessions, checkpoints)
            plan = engine.inspect(session_id)
            assert plan.decision is RecoveryDecision.STOP
            assert "dangling intent" in plan.reason
        finally:
            sessions.close()
            checkpoints.close()

    def test_resume_rejected_for_non_resume_plan(self, tmp_path: Path) -> None:
        sessions = SQLiteSessionStore(tmp_path / "sessions.db")
        checkpoints = SQLiteCheckpointStore(tmp_path / "sessions.db")
        try:
            record = sessions.create(tmp_path)
            engine = RecoveryEngine(sessions, checkpoints)
            plan = engine.inspect(record.session_id)
            assert plan.decision is RecoveryDecision.RESUME  # 空会话无悬空事实
            with pytest.raises(RecoveryError, match="unknown tool intent"):
                engine.resolve_uncertain(record.session_id, "intent_missing", "not_executed")
            with pytest.raises(RecoveryError, match="expected 'not_executed'"):
                engine.resolve_uncertain(record.session_id, "intent_missing", "whatever")
        finally:
            sessions.close()
            checkpoints.close()
