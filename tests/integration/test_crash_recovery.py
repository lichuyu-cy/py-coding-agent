"""阶段 20 集成测试：Checkpoint/Resume 的崩溃注入矩阵（模块 24/25 验收）。

覆盖 dependency-graph.md 阶段 20 的验收条件：崩溃注入——不确定副作用不自动重放。
注入点：提交后崩溃、工具启动前崩溃、工具执行后结果提交前崩溃、旧检查点、指纹不匹配。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.agent.recovery import RecoveryEngine, RecoveryError
from coding_agent.bootstrap import build_runtime
from coding_agent.domain.messages import (
    AssistantMessage,
    MessageMeta,
    ToolCall,
    UserMessage,
    message_to_dict,
    new_message_id,
    utc_now_rfc3339,
)
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.ports.checkpoint import (
    BoundaryKind,
    Checkpoint,
    IntentStatus,
    RecoveryDecision,
    ToolIntent,
)
from coding_agent.ports.store import SessionAppend
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall
from coding_agent.storage.checkpoint_store import SQLiteCheckpointStore
from coding_agent.storage.session_store import SQLiteSessionStore


class Crash(BaseException):
    """模拟硬崩溃（不被应用层 except Exception 捕获）。"""


class CrashingExecutor:
    """在执行任何工具时直接崩溃：模拟「工具执行后结果提交前」的进程死亡。"""

    async def execute(self, call, *, workspace, cancel, deadline):  # noqa: ANN001 - 鸭子类型
        raise Crash("simulated process death during tool execution")


def user_payload(session_id: str, text: str, *, run_id: str = "run_seed") -> dict:
    return message_to_dict(
        UserMessage(
            meta=MessageMeta(
                id=new_message_id(),
                session_id=session_id,
                run_id=run_id,
                turn_id=1,
                created_at=utc_now_rfc3339(),
            ),
            content=text,
        )
    )


def assistant_with_call_payload(session_id: str, call_id: str, *, run_id: str = "run_seed") -> dict:
    return message_to_dict(
        AssistantMessage(
            meta=MessageMeta(
                id=new_message_id(),
                session_id=session_id,
                run_id=run_id,
                turn_id=1,
                created_at=utc_now_rfc3339(),
            ),
            content="",
            stop_reason=StopReason.TOOL_CALLS,
            tool_calls=(
                ToolCall(id=call_id, name="write", arguments={"path": "a.py"}, ordinal=0),
            ),
        )
    )


def planned_intent(session_id: str, call_id: str, *, run_id: str = "run_seed") -> ToolIntent:
    now = utc_now_rfc3339()
    return ToolIntent(
        intent_id=f"intent_{call_id}",
        session_id=session_id,
        run_id=run_id,
        call_id=call_id,
        tool_name="write",
        arguments_digest="d1gest",
        status=IntentStatus.PLANNED,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture()
def stores(tmp_path: Path):
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    checkpoints = SQLiteCheckpointStore(tmp_path / "sessions.db")
    yield sessions, checkpoints
    sessions.close()
    checkpoints.close()


class TestCrashInjection:
    async def test_crash_after_commit_resumes(self, tmp_path: Path, stores) -> None:
        """提交后崩溃（run 正常结束，进程随后死亡）：RESUME，无待处理悬念。"""
        sessions, checkpoints = stores
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(
            provider=provider, session_store=sessions, checkpoint_store=checkpoints
        )
        result = await runtime.run("inspect", tmp_path)
        assert result.status is RunStatus.FINISHED

        engine = RecoveryEngine(sessions, checkpoints)
        plan = engine.inspect(result.session_id)
        assert plan.decision is RecoveryDecision.RESUME
        assert plan.pending_intents == ()
        assert plan.uncertain_intents == ()
        assert plan.safe_boundary is not None
        assert plan.safe_boundary.boundary is BoundaryKind.RUN_END
        assert plan.safe_boundary.session_version <= plan.session_version

        outcome = engine.resume(plan)
        assert outcome.appended_message_ids == ()

        # 全部意图都已闭合
        statuses = {item.status for item in checkpoints.all_intents(result.session_id)}
        assert statuses == {IntentStatus.COMPLETED}

    async def test_crash_before_tool_start_records_not_executed(self, tmp_path: Path, stores) -> None:
        """工具启动前崩溃：意图停留在 planned → 确定未执行 → 补记并安全恢复。"""
        sessions, checkpoints = stores
        record = sessions.create(tmp_path)
        session_id = record.session_id
        sessions.append(
            session_id,
            0,
            SessionAppend(
                messages=(
                    user_payload(session_id, "do work"),
                    assistant_with_call_payload(session_id, "call_1"),
                )
            ),
        )
        checkpoints.record_intent(planned_intent(session_id, "call_1"))
        checkpoints.save(
            Checkpoint(
                checkpoint_id="ckpt_1",
                session_id=session_id,
                session_version=1,
                state_seq=3,
                boundary=BoundaryKind.MODEL_RESPONSE,
                pending_intent_ids=("intent_call_1",),
                workspace_fingerprint="ws1:test",
                created_at=utc_now_rfc3339(),
            )
        )

        engine = RecoveryEngine(sessions, checkpoints)
        plan = engine.inspect(session_id)
        assert plan.decision is RecoveryDecision.RESUME
        assert [item.call_id for item in plan.pending_intents] == ["call_1"]
        assert plan.uncertain_intents == ()

        outcome = engine.resume(plan)
        assert len(outcome.appended_message_ids) == 1
        loaded = sessions.load(session_id)
        assert [message["type"] for message in loaded.messages] == [
            "user",
            "assistant",
            "tool_result",
        ]
        result_payload = loaded.messages[-1]
        assert result_payload["tool_call_id"] == "call_1"
        assert result_payload["status"] == "cancelled"
        assert result_payload["error_kind"] == "recovery_not_executed"
        (intent,) = checkpoints.all_intents(session_id)
        assert intent.status is IntentStatus.ABANDONED

        # 补记后会话可安全继续
        provider = FakeProvider([FakeResponse(content="recovered", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(
            provider=provider, session_store=sessions, checkpoint_store=checkpoints
        )
        continued = await runtime.continue_run(session_id)
        assert continued.status is RunStatus.FINISHED
        assert continued.final_text == "recovered"

        replanned = RecoveryEngine(sessions, checkpoints).inspect(session_id)
        assert replanned.decision is RecoveryDecision.RESUME
        assert replanned.pending_intents == ()

    async def test_crash_during_tool_execution_requires_reconcile(self, tmp_path: Path, stores) -> None:
        """工具执行后结果提交前崩溃：副作用未知 → RECONCILE，人工核对后才能继续。"""
        sessions, checkpoints = stores
        record = sessions.create(tmp_path)
        session_id = record.session_id
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                ),
                FakeResponse(content="after", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(
            provider=provider,
            session_store=sessions,
            checkpoint_store=checkpoints,
            pipeline=CrashingExecutor(),
        )
        with pytest.raises(Crash):
            await runtime.run("do work", tmp_path, session_id=session_id)

        # 崩溃后的事实：assistant 已提交、无结果、意图 started
        loaded = sessions.load(session_id)
        assert [message["type"] for message in loaded.messages] == ["user", "assistant"]
        (intent,) = checkpoints.open_intents(session_id)
        assert intent.status is IntentStatus.STARTED

        engine = RecoveryEngine(sessions, checkpoints)
        plan = engine.inspect(session_id)
        assert plan.decision is RecoveryDecision.RECONCILE
        assert [item.call_id for item in plan.uncertain_intents] == ["call_1"]
        with pytest.raises(RecoveryError, match="cannot auto-resume"):
            engine.resume(plan)

        # 非法的核对决策被拒绝
        with pytest.raises(RecoveryError, match="unknown reconciliation decision"):
            engine.resolve_uncertain(session_id, intent.intent_id, "auto_retry")

        outcome = engine.resolve_uncertain(session_id, intent.intent_id, "not_executed")
        assert outcome.resolved_intent_ids == (intent.intent_id,)
        loaded = sessions.load(session_id)
        assert loaded.messages[-1]["error_kind"] == "recovery_not_executed"
        assert RecoveryEngine(sessions, checkpoints).inspect(session_id).decision is RecoveryDecision.RESUME

    async def test_crash_executed_unknown_marks_history_uncertain(self, tmp_path: Path, stores) -> None:
        """确认已执行但结果丢失：补记 UNCERTAIN 结果，历史显式标记不确定。"""
        sessions, checkpoints = stores
        record = sessions.create(tmp_path)
        session_id = record.session_id
        sessions.append(
            session_id,
            0,
            SessionAppend(
                messages=(
                    user_payload(session_id, "do work"),
                    assistant_with_call_payload(session_id, "call_9"),
                )
            ),
        )
        checkpoints.record_intent(planned_intent(session_id, "call_9"))
        checkpoints.mark_started(session_id, "call_9")

        engine = RecoveryEngine(sessions, checkpoints)
        plan = engine.inspect(session_id)
        assert plan.decision is RecoveryDecision.RECONCILE
        engine.resolve_uncertain(session_id, "intent_call_9", "executed_unknown")

        loaded = sessions.load(session_id)
        assert loaded.messages[-1]["status"] == "uncertain"
        assert loaded.messages[-1]["error_kind"] == "recovery_executed_unknown"
        (intent,) = checkpoints.all_intents(session_id)
        assert intent.status is IntentStatus.RESOLVED
        assert RecoveryEngine(sessions, checkpoints).inspect(session_id).decision is RecoveryDecision.RESUME


class TestStaleCheckpointAndFingerprint:
    async def test_stale_checkpoint_still_resumes(self, tmp_path: Path, stores) -> None:
        """旧检查点：会话在其后又有提交 → 以会话尾部为准，检查点仅作索引。"""
        sessions, checkpoints = stores
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(
            provider=provider, session_store=sessions, checkpoint_store=checkpoints
        )
        result = await runtime.run("task", tmp_path)

        # 外部入口在检查点之后继续提交（未生成新检查点）
        version = sessions.load(result.session_id).version
        sessions.append(
            result.session_id,
            version,
            SessionAppend(messages=(user_payload(result.session_id, "next task"),)),
        )

        engine = RecoveryEngine(sessions, checkpoints)
        plan = engine.inspect(result.session_id)
        assert plan.decision is RecoveryDecision.RESUME
        assert plan.safe_boundary is not None
        assert plan.safe_boundary.session_version < plan.session_version
        assert plan.reason.startswith("no uncertain side effects")

    async def test_workspace_fingerprint_mismatch_stops(self, tmp_path: Path, stores) -> None:
        sessions, checkpoints = stores
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(
            provider=provider, session_store=sessions, checkpoint_store=checkpoints
        )
        result = await runtime.run("task", tmp_path)

        engine = RecoveryEngine(sessions, checkpoints)
        matched = engine.inspect(result.session_id, workspace=tmp_path)
        assert matched.decision is RecoveryDecision.RESUME

        other = tmp_path / "elsewhere"
        other.mkdir()
        mismatched = engine.inspect(result.session_id, workspace=other)
        assert mismatched.decision is RecoveryDecision.STOP
        assert "fingerprint mismatch" in mismatched.reason
        with pytest.raises(RecoveryError, match="cannot auto-resume"):
            engine.resume(mismatched)
