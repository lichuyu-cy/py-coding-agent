"""阶段 19 集成测试：Runtime 与持久会话存储的接线。

覆盖 dependency-graph.md 阶段 19 的验收条件：读回消息顺序；冲突运行拒绝。
覆盖场景：消息逐条落库、工具组关联保留、重启水合继续、跨实例锁冲突、
workspace 绑定校验、压缩摘要持久化与重启恢复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.agent.runtime import RunConflictError, WorkspaceMismatchError
from coding_agent.bootstrap import build_runtime
from coding_agent.context.compaction import Compactor
from coding_agent.domain.messages import MessageMeta, UserMessage, message_to_dict, new_message_id
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.ports.store import SessionAppend
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall
from coding_agent.storage.session_store import SQLiteSessionStore


def seed_user_payload(session_id: str, text: str) -> dict:
    """模拟外部入口向已提交会话追加一条用户任务（跨请求场景）。"""
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


class TestMessagePersistence:
    async def test_messages_persist_in_order(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "sessions.db")
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider, session_store=store)
        try:
            result = await runtime.run("please work", tmp_path)
            assert result.status is RunStatus.FINISHED

            record = store.load(result.session_id)
            assert [message["type"] for message in record.messages] == ["user", "assistant"]
            assert record.messages[0]["content"] == "please work"
            assert record.messages[1]["content"] == "done"
            assert record.version == 2
            assert record.workspace == str(tmp_path)
        finally:
            store.close()

    async def test_tool_round_persists_calls_and_results(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                ),
                FakeResponse(content="read it", stop_reason=StopReason.END_TURN),
            ]
        )
        store = SQLiteSessionStore(tmp_path / "sessions.db")
        runtime = build_runtime(provider=provider, session_store=store)
        try:
            result = await runtime.run("inspect", tmp_path)
            record = store.load(result.session_id)
            types = [message["type"] for message in record.messages]
            assert types == ["user", "assistant", "tool_result", "assistant"]

            calls = record.messages[1]["tool_calls"]
            assert [call["id"] for call in calls] == ["call_1"]
            assert record.messages[2]["tool_call_id"] == "call_1"
            assert record.messages[2]["status"] == "completed"
        finally:
            store.close()


class TestHydration:
    async def test_restart_run_hydrates_history_then_appends(self, tmp_path: Path) -> None:
        db = tmp_path / "sessions.db"
        first_store = SQLiteSessionStore(db)
        first_provider = FakeProvider([FakeResponse(content="first done", stop_reason=StopReason.END_TURN)])
        first_runtime = build_runtime(provider=first_provider, session_store=first_store)
        try:
            first = await first_runtime.run("first task", tmp_path)
        finally:
            first_store.close()

        # 新进程：新 store 连接、新 runtime、空内存注册表
        second_store = SQLiteSessionStore(db)
        second_provider = FakeProvider([FakeResponse(content="second done", stop_reason=StopReason.END_TURN)])
        second_runtime = build_runtime(provider=second_provider, session_store=second_store)
        try:
            second = await second_runtime.run("second task", tmp_path, session_id=first.session_id)
            assert second.status is RunStatus.FINISHED
            assert second.final_text == "second done"

            # 第二个 runtime 的首个请求包含水合历史 + 新任务
            contents = [message.content for message in second_provider.requests[0].messages]
            assert "first task" in contents
            assert contents[-1] == "second task"

            record = second_store.load(first.session_id)
            assert [message["type"] for message in record.messages] == [
                "user",
                "assistant",
                "user",
                "assistant",
            ]
            assert record.version == 4
        finally:
            second_store.close()

    async def test_continue_run_hydrates_and_resumes_tail(self, tmp_path: Path) -> None:
        db = tmp_path / "sessions.db"
        first_store = SQLiteSessionStore(db)
        first_provider = FakeProvider([FakeResponse(content="first done", stop_reason=StopReason.END_TURN)])
        first_runtime = build_runtime(provider=first_provider, session_store=first_store)
        try:
            first = await first_runtime.run("first task", tmp_path)
            # 外部入口提交下一条用户任务（跨请求），尾部变为可响应状态
            version = first_store.load(first.session_id).version
            first_store.append(
                first.session_id,
                version,
                SessionAppend(messages=(seed_user_payload(first.session_id, "resume me"),)),
            )
        finally:
            first_store.close()

        second_store = SQLiteSessionStore(db)
        second_provider = FakeProvider([FakeResponse(content="resumed", stop_reason=StopReason.END_TURN)])
        second_runtime = build_runtime(provider=second_provider, session_store=second_store)
        try:
            result = await second_runtime.continue_run(first.session_id)
            assert result.status is RunStatus.FINISHED
            assert result.final_text == "resumed"
            # 未显式传 workspace：来自持久记录的绑定工作区
            contents = [message.content for message in second_provider.requests[0].messages]
            assert "first task" in contents
            assert contents[-1] == "resume me"
        finally:
            second_store.close()


class TestConcurrentRuns:
    async def test_store_run_lock_blocks_second_runtime(self, tmp_path: Path) -> None:
        db = tmp_path / "sessions.db"
        first_store = SQLiteSessionStore(db)
        first_provider = FakeProvider([FakeResponse(content="first done", stop_reason=StopReason.END_TURN)])
        first_runtime = build_runtime(provider=first_provider, session_store=first_store)
        try:
            first = await first_runtime.run("first task", tmp_path)
            # 模拟另一进程正在运行（持 store 锁）
            assert first_store.acquire_run_lock(first.session_id, "run_external") is True

            second_store = SQLiteSessionStore(db)
            second_provider = FakeProvider([FakeResponse(content="should not run", stop_reason=StopReason.END_TURN)])
            second_runtime = build_runtime(provider=second_provider, session_store=second_store)
            try:
                with pytest.raises(RunConflictError, match="store run lock"):
                    await second_runtime.run("second task", tmp_path, session_id=first.session_id)

                first_store.release_run_lock(first.session_id, "run_external")
                # 锁释放后同一 runtime 可继续（本地注册表已被首个冲突请求水合）
                result = await second_runtime.run("second task", tmp_path, session_id=first.session_id)
                assert result.status is RunStatus.FINISHED
                assert result.final_text == "should not run"
            finally:
                second_store.close()
        finally:
            first_store.close()

    async def test_workspace_switch_rejected_when_bound(self, tmp_path: Path) -> None:
        ws_one = tmp_path / "one"
        ws_two = tmp_path / "two"
        ws_one.mkdir()
        ws_two.mkdir()
        store = SQLiteSessionStore(tmp_path / "sessions.db")
        provider = FakeProvider(
            [
                FakeResponse(content="first done", stop_reason=StopReason.END_TURN),
                FakeResponse(content="never", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider, session_store=store)
        try:
            first = await runtime.run("task", ws_one)
            with pytest.raises(WorkspaceMismatchError):
                await runtime.run("other", ws_two, session_id=first.session_id)
            # 相同路径（规范化后）仍可继续
            result = await runtime.run("follow", ws_one, session_id=first.session_id)
            assert result.status is RunStatus.FINISHED
        finally:
            store.close()

    async def test_without_store_memory_behaviour_unchanged(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        result = await runtime.run("task", tmp_path)
        assert result.status is RunStatus.FINISHED
        assert runtime.session_store is None
        assert runtime.registry.get(result.session_id) is not None


class TestSummaryPersistence:
    async def test_compaction_summary_persisted_and_restored(self, tmp_path: Path) -> None:
        big = "x" * 800
        script = [
            FakeResponse(
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(ScriptedToolCall(name="bash", arguments={"command": f"echo {big}"}, id=f"call_{i}"),),
            )
            for i in range(1, 4)
        ] + [FakeResponse(content="finished", stop_reason=StopReason.END_TURN)]
        provider = FakeProvider(script)

        db = tmp_path / "sessions.db"
        store = SQLiteSessionStore(db)
        runtime = build_runtime(
            provider=provider,
            session_store=store,
            context_limit_tokens=2600,
            compactor=Compactor(preserve_recent=1),
        )
        try:
            result = await runtime.run("print stuff", tmp_path)
            assert result.status is RunStatus.FINISHED
            record = store.load(result.session_id)
            assert len(record.summaries) >= 1
            assert record.summaries[0].summary_version == 1
            assert record.summaries[0].covered_through_id
        finally:
            store.close()

        # 重启：水合恢复压缩记录（版本继续单调的前提）
        second_store = SQLiteSessionStore(db)
        second_provider = FakeProvider([FakeResponse(content="next", stop_reason=StopReason.END_TURN)])
        second_runtime = build_runtime(
            provider=second_provider,
            session_store=second_store,
            context_limit_tokens=2600,
            compactor=Compactor(preserve_recent=1),
        )
        try:
            second_runtime._get_session_log(result.session_id)  # noqa: SLF001 - 触发水合
            manager = second_runtime._context_manager  # noqa: SLF001 - 验证恢复注入
            assert manager is not None
            restored = manager._records.get(result.session_id)  # noqa: SLF001
            assert restored is not None
            assert restored.summary_version == 1
            assert restored.covered_through_id == record.summaries[0].covered_through_id
        finally:
            second_store.close()
