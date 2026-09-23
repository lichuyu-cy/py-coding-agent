"""阶段 09 集成测试：steering 注入边界、follow-up 新回合、abort 与工具协作。

覆盖 dependency-graph.md 阶段 09 的验收条件：指定边界注入；中断不启动新工具。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from coding_agent.agent.control import AbortReason, SteeringRejectedError, UnknownRunError
from coding_agent.agent.loop import LoopEventKind
from coding_agent.bootstrap import build_runtime
from coding_agent.domain.messages import UserMessage
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall


async def wait_for_active_run(runtime) -> str:
    for _ in range(200):
        if runtime._active_runs:  # noqa: SLF001 - 测试需要观察活动 run 的 ID
            return next(iter(runtime._active_runs))
        await asyncio.sleep(0.01)
    raise AssertionError("no active run appeared")


class TestSteering:
    async def test_injected_at_next_request_boundary(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    content="thinking",
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                    delay_seconds=0.3,
                ),
                FakeResponse(content="acknowledged", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

        run_task = asyncio.create_task(runtime.run("task", tmp_path))
        run_id = await wait_for_active_run(runtime)
        runtime.submit_steering(run_id, "also check permissions", client_event_id="evt_1")
        result = await run_task

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)
        texts = [m.content for m in log.messages if isinstance(m, UserMessage)]
        assert texts == ["task", "also check permissions"]

        # 首次请求不含 steering；注入后仅出现一次
        assert all("permissions" not in m.content for m in provider.requests[0].messages)
        assert sum("permissions" in m.content for m in provider.requests[1].messages) == 1

    async def test_steering_rejected_after_run_ends(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        result = await runtime.run("task", tmp_path)
        with pytest.raises(SteeringRejectedError):
            runtime.submit_steering(result.run_id, "too late")

    async def test_steering_idempotent_by_client_event_id(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(content="slow", stop_reason=StopReason.END_TURN, delay_seconds=0.3),
                FakeResponse(content="after steering", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        run_task = asyncio.create_task(runtime.run("task", tmp_path))
        run_id = await wait_for_active_run(runtime)
        runtime.submit_steering(run_id, "one message", client_event_id="evt_dup")
        runtime.submit_steering(run_id, "one message", client_event_id="evt_dup")
        result = await run_task

        assert result.status is RunStatus.FINISHED
        assert result.final_text == "after steering"
        log = runtime.registry.get(result.session_id)
        steering_count = sum(1 for m in log.messages if isinstance(m, UserMessage) and m.content == "one message")
        assert steering_count == 1
        assert result.turns == 2  # steering 触发了一次额外模型请求


class TestFollowUp:
    async def test_follow_up_starts_new_turn_after_task_end(self, tmp_path: Path) -> None:
        events: list = []
        provider = FakeProvider(
            [
                FakeResponse(content="first done", stop_reason=StopReason.END_TURN),
                FakeResponse(content="second done", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider, observer=events.append)
        log = runtime.registry.create_session()
        runtime.submit_follow_up(log.session_id, "second task", client_event_id="fu_1")

        result = await runtime.run("first task", tmp_path, session_id=log.session_id)

        assert result.status is RunStatus.FINISHED
        assert result.turns == 2
        assert result.final_text == "second done"
        texts = [m.content for m in log.messages if isinstance(m, UserMessage)]
        assert texts == ["first task", "second task"]
        # follow-up 不在第一个未完成请求中出现
        assert all("second task" not in m.content for m in provider.requests[0].messages)
        assert sum("second task" in m.content for m in provider.requests[1].messages) == 1
        assert [e.kind for e in events].count(LoopEventKind.FOLLOW_UP_STARTED) == 1

    async def test_follow_up_preserved_after_abort(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="late", stop_reason=StopReason.END_TURN, delay_seconds=2.0)])
        runtime = build_runtime(provider=provider)
        session = runtime.registry.create_session()
        runtime.submit_follow_up(session.session_id, "queued task")
        assert runtime.pending_follow_ups(session.session_id) == 1

        run_task = asyncio.create_task(runtime.run("task", tmp_path, session_id=session.session_id))
        run_id = await wait_for_active_run(runtime)
        runtime.abort(run_id)
        result = await run_task

        assert result.status is RunStatus.ABORTED
        # 中止时默认保留队列但不自动启动
        assert runtime.pending_follow_ups(session.session_id) == 1


class TestAbort:
    async def test_abort_during_provider_delay(self, tmp_path: Path) -> None:
        events: list = []
        provider = FakeProvider([FakeResponse(content="never", stop_reason=StopReason.END_TURN, delay_seconds=5.0)])
        runtime = build_runtime(provider=provider, observer=events.append)

        started = time.monotonic()
        run_task = asyncio.create_task(runtime.run("task", tmp_path))
        run_id = await wait_for_active_run(runtime)
        assert runtime.abort(run_id, AbortReason.USER_REQUEST) is True
        assert runtime.abort(run_id) is False  # 幂等：重复请求返回非首次
        result = await run_task
        elapsed = time.monotonic() - started

        assert elapsed < 4
        assert result.status is RunStatus.ABORTED
        assert result.stop_reason is StopReason.CANCELLED
        assert result.provider_calls == 1
        log = runtime.registry.get(result.session_id)
        assert [m.message_type for m in log.messages] == ["user"]  # 碎片请求不入历史
        assert [e.kind for e in events].count(LoopEventKind.RUN_END) == 1

    async def test_abort_mid_tool_batch_does_not_start_next_tool(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="bash", arguments={"command": 'python -c "import time; time.sleep(20)"'}, id="call_1"
                        ),
                        ScriptedToolCall(name="bash", arguments={"command": "echo second"}, id="call_2"),
                    ),
                ),
                FakeResponse(content="never", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)

        started = time.monotonic()
        run_task = asyncio.create_task(runtime.run("task", tmp_path))
        run_id = await wait_for_active_run(runtime)
        await asyncio.sleep(0.4)  # 让第一个 bash 开始执行
        runtime.abort(run_id)
        result = await run_task
        elapsed = time.monotonic() - started

        assert elapsed < 15  # 未等待 20s 的完整睡眠
        assert result.status is RunStatus.ABORTED
        assert provider.call_count == 1  # 中断后不再发起模型请求
        log = runtime.registry.get(result.session_id)
        first = log.find_result("call_1")
        second = log.find_result("call_2")
        assert first is not None and second is not None
        assert first.status.value == "cancelled"  # 已开始的工具被取消信号终止
        assert second.status.value == "cancelled"
        assert "was not executed" in second.content  # 未启动的调用有明确取消结果
        # 所有调用仍保持配对完整
        assert len([m for m in log.messages if m.message_type == "tool_result"]) == 2

    async def test_abort_unknown_or_finished_run(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        with pytest.raises(UnknownRunError):
            runtime.abort("run_missing")
        result = await runtime.run("task", tmp_path)
        with pytest.raises(UnknownRunError):
            runtime.abort(result.run_id)  # run 已结束
