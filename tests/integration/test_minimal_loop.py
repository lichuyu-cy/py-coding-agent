"""阶段 04 集成测试：Fake Provider → Runtime → 极简工具执行口 → MessageLog。

覆盖 dependency-graph.md 阶段 04 的验收条件：
- 无工具与虚构工具的确定性循环；
- final / tool→result→final / 多工具 / 失败修复；
- max turn、工具预算、deadline 限额；
- 会话运行锁与 continue_run 尾部校验；
- 终结事件一次、历史顺序与调用次数严格一致。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from coding_agent.agent.loop import LoopEventKind, RunLimits, ToolOutcome
from coding_agent.agent.runtime import (
    AgentRuntime,
    ResumeValidationError,
    RunConflictError,
    UnknownSessionError,
)
from coding_agent.domain.messages import (
    AssistantMessage,
    MessageLog,
    MessageMeta,
    ToolCall,
    ToolCallId,
    ToolResult,
    ToolResultStatus,
    UserMessage,
)
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.ports.provider import CancelSignal, ProviderErrorKind, ProviderMessageRole
from coding_agent.providers.fake import FakeFault, FakeProvider, FakeResponse, ScriptedToolCall


class FakeExecutor:
    """极简工具执行口的测试实现：记录调用，按序返回脚本结果。"""

    def __init__(self, outcomes: list[ToolOutcome] | None = None) -> None:
        self._outcomes = list(outcomes or [])
        self.calls: list[ToolCall] = []
        self.workspaces: list[Path] = []

    async def execute(
        self, call: ToolCall, *, workspace: Path, cancel: CancelSignal | None
    ) -> ToolOutcome:
        self.calls.append(call)
        self.workspaces.append(workspace)
        if self._outcomes:
            return self._outcomes.pop(0)
        return ToolOutcome(status=ToolResultStatus.COMPLETED, content=f"executed {call.name}")


def make_runtime(
    steps: list,
    *,
    limits: RunLimits | None = None,
    executor: FakeExecutor | None = None,
    observer: list | None = None,
) -> tuple[AgentRuntime, FakeProvider, FakeExecutor]:
    provider = FakeProvider(steps)
    tool_executor = executor or FakeExecutor()
    runtime = AgentRuntime(
        provider=provider,
        executor=tool_executor,
        limits=limits,
        observer=observer.append if observer is not None else None,
    )
    return runtime, provider, tool_executor


WS = Path(".")


class TestHappyPaths:
    async def test_final_answer_without_tools(self) -> None:
        runtime, provider, executor = make_runtime(
            [FakeResponse(content="all done", stop_reason=StopReason.END_TURN)]
        )
        result = await runtime.run("say hi", WS)

        assert result.status is RunStatus.FINISHED
        assert result.stop_reason is StopReason.END_TURN
        assert result.final_text == "all done"
        assert result.turns == 1
        assert result.tool_calls == 0
        assert result.provider_calls == 1
        assert result.limit_hit is None
        assert provider.call_count == 1
        assert executor.calls == []

        log = runtime.registry.get(result.session_id)
        assert [m.message_type for m in log.messages] == ["user", "assistant"]
        user = log.messages[0]
        assert isinstance(user, UserMessage)
        assert user.meta.session_id == result.session_id
        assert user.meta.run_id == result.run_id
        assert user.meta.turn_id == 1

    async def test_tool_then_final_with_strict_ordering(self) -> None:
        runtime, provider, executor = make_runtime(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                ),
                FakeResponse(content="fixed", stop_reason=StopReason.END_TURN),
            ]
        )
        result = await runtime.run("fix bug", WS)

        assert result.status is RunStatus.FINISHED
        assert result.turns == 2
        assert result.tool_calls == 1
        assert result.final_text == "fixed"
        log = runtime.registry.get(result.session_id)
        assert [m.message_type for m in log.messages] == ["user", "assistant", "tool_result", "assistant"]
        result_message = log.find_result("call_1")
        assert result_message is not None
        assert result_message.status is ToolResultStatus.COMPLETED

        # 第二次请求必须携带工具结果（Observe 再入上下文）
        second_request = provider.requests[1]
        roles = [m.role for m in second_request.messages]
        assert roles == [
            ProviderMessageRole.SYSTEM,
            ProviderMessageRole.USER,
            ProviderMessageRole.ASSISTANT,
            ProviderMessageRole.TOOL,
        ]
        tool_message = second_request.messages[-1]
        assert tool_message.tool_call_id == "call_1"
        assert executor.calls[0].id == "call_1"

    async def test_multiple_tools_executed_in_ordinal_order(self) -> None:
        runtime, provider, executor = make_runtime(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),
                        ScriptedToolCall(name="bash", arguments={"command": "pytest"}, id="call_2"),
                    ),
                ),
                FakeResponse(content="both done", stop_reason=StopReason.END_TURN),
            ]
        )
        result = await runtime.run("inspect and test", WS)

        assert result.tool_calls == 2
        assert [str(c.id) for c in executor.calls] == ["call_1", "call_2"]
        log = runtime.registry.get(result.session_id)
        assert [m.message_type for m in log.messages] == [
            "user",
            "assistant",
            "tool_result",
            "tool_result",
            "assistant",
        ]
        results = [m for m in log.messages if isinstance(m, ToolResult)]
        assert [str(r.tool_call_id) for r in results] == ["call_1", "call_2"]

    async def test_tool_error_is_observed_by_model(self) -> None:
        executor = FakeExecutor(
            [
                ToolOutcome(
                    status=ToolResultStatus.ERROR,
                    content="file not found: a.py",
                    error_kind="file_not_found",
                )
            ]
        )
        runtime, provider, _ = make_runtime(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                ),
                FakeResponse(content="checked alternative", stop_reason=StopReason.END_TURN),
            ],
            executor=executor,
        )
        result = await runtime.run("fix bug", WS)
        assert result.status is RunStatus.FINISHED

        second_request = provider.requests[1]
        tool_observation = second_request.messages[-1]
        assert tool_observation.role is ProviderMessageRole.TOOL
        assert "file not found" in tool_observation.content

    async def test_events_ordered_and_run_end_once(self) -> None:
        events: list = []
        runtime, _, _ = make_runtime(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={}, id="call_1"),),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ],
            observer=events,
        )
        await runtime.run("go", WS)
        kinds = [event.kind for event in events]
        assert kinds == [
            LoopEventKind.RUN_START,
            LoopEventKind.TURN_START,
            LoopEventKind.ASSISTANT_COMMITTED,
            LoopEventKind.TOOL_RESULT_COMMITTED,
            LoopEventKind.TURN_START,
            LoopEventKind.ASSISTANT_COMMITTED,
            LoopEventKind.RUN_END,
        ]
        assert kinds.count(LoopEventKind.RUN_END) == 1


class TestBudgets:
    async def test_max_turns_budget(self) -> None:
        runtime, provider, executor = make_runtime(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={}, id="call_1"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={}, id="call_2"),),
                ),
                FakeResponse(content="never reached", stop_reason=StopReason.END_TURN),
            ],
            limits=RunLimits(max_turns=2),
        )
        result = await runtime.run("loop forever", WS)

        assert result.status is RunStatus.BUDGET_EXHAUSTED
        assert result.limit_hit == "max_turns"
        assert result.turns == 2
        assert result.tool_calls == 2
        assert provider.remaining_steps == 1  # 第三个脚本步骤未被消费
        log = runtime.registry.get(result.session_id)
        assert len([m for m in log.messages if isinstance(m, ToolResult)]) == 2

    async def test_max_tool_calls_budget_records_cancelled_results(self) -> None:
        runtime, provider, executor = make_runtime(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(name="read", arguments={}, id="call_1"),
                        ScriptedToolCall(name="bash", arguments={}, id="call_2"),
                    ),
                ),
            ],
            limits=RunLimits(max_tool_calls=1),
        )
        result = await runtime.run("run two tools", WS)

        assert result.status is RunStatus.BUDGET_EXHAUSTED
        assert result.limit_hit == "max_tool_calls"
        assert result.tool_calls == 1
        assert [str(c.id) for c in executor.calls] == ["call_1"]

        log = runtime.registry.get(result.session_id)
        cancelled = log.find_result("call_2")
        assert cancelled is not None
        assert cancelled.status is ToolResultStatus.CANCELLED
        assert cancelled.error_kind == "tool_budget_exhausted"
        # 预算终止也必须保持调用配对完整：不存在未解决调用
        assert all(
            log.find_result(str(call.id)) is not None
            for message in log.messages
            if isinstance(message, AssistantMessage)
            for call in message.tool_calls
        )

    async def test_deadline_budget_stops_before_completion(self) -> None:
        runtime, provider, _ = make_runtime(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={}, id="call_1"),),
                ),
                FakeResponse(content="too late", stop_reason=StopReason.END_TURN, delay_seconds=0.5),
            ],
            limits=RunLimits(deadline_seconds=0.15),
        )
        started = time.monotonic()
        result = await runtime.run("slow run", WS)
        elapsed = time.monotonic() - started

        assert result.status is RunStatus.BUDGET_EXHAUSTED
        assert result.limit_hit == "deadline"
        assert elapsed < 0.45  # deadline 生效，未等待 0.5s 的延时
        assert result.turns == 1  # 第二次响应未计入
        assert result.provider_calls == 2


class TestProviderFailures:
    async def test_transient_error_is_retried(self) -> None:
        runtime, provider, _ = make_runtime(
            [
                FakeFault(ProviderErrorKind.RATE_LIMIT, "slow down"),
                FakeResponse(content="recovered", stop_reason=StopReason.END_TURN),
            ]
        )
        result = await runtime.run("try again", WS)
        assert result.status is RunStatus.FINISHED
        assert result.provider_calls == 2
        assert result.final_text == "recovered"

    async def test_retry_exhaustion_fails_run(self) -> None:
        runtime, provider, _ = make_runtime(
            [
                FakeFault(ProviderErrorKind.RATE_LIMIT, "always busy"),
                FakeFault(ProviderErrorKind.RATE_LIMIT, "always busy"),
                FakeFault(ProviderErrorKind.RATE_LIMIT, "always busy"),
            ]
        )
        result = await runtime.run("doomed", WS)
        assert result.status is RunStatus.ERROR
        assert result.stop_reason is StopReason.PROVIDER_ERROR
        assert result.provider_calls == 3  # 1 次初始 + 2 次重试

        log = runtime.registry.get(result.session_id)
        assert [m.message_type for m in log.messages] == ["user"]  # 失败响应不入历史

    async def test_non_retryable_error_fails_immediately(self) -> None:
        runtime, provider, _ = make_runtime(
            [FakeFault(ProviderErrorKind.INVALID_RESPONSE, "malformed")]
        )
        result = await runtime.run("doomed", WS)
        assert result.status is RunStatus.ERROR
        assert result.provider_calls == 1

    async def test_provider_cancelled_response_aborts_run(self) -> None:
        runtime, provider, _ = make_runtime(
            [FakeResponse(content="partial", stop_reason=StopReason.CANCELLED)]
        )
        result = await runtime.run("interrupt me", WS)
        assert result.status is RunStatus.ABORTED
        assert result.stop_reason is StopReason.CANCELLED
        log = runtime.registry.get(result.session_id)
        assert [m.message_type for m in log.messages] == ["user", "assistant"]


class TestSessionGuardAndContinue:
    async def test_conflicting_run_rejected(self) -> None:
        runtime, provider, _ = make_runtime(
            [
                FakeResponse(content="slow", stop_reason=StopReason.END_TURN, delay_seconds=0.3),
                FakeResponse(content="second", stop_reason=StopReason.END_TURN),
            ]
        )
        log = runtime.registry.create_session()
        session_id = log.session_id

        first = asyncio.create_task(runtime.run("first", WS, session_id=session_id))
        await asyncio.sleep(0.05)
        with pytest.raises(RunConflictError):
            await runtime.run("second", WS, session_id=session_id)
        first_result = await first
        assert first_result.status is RunStatus.FINISHED
        assert runtime.registry.is_active(session_id) is False

        second_result = await runtime.run("second", WS, session_id=session_id)
        assert second_result.status is RunStatus.FINISHED
        assert second_result.run_id != first_result.run_id

    async def test_continue_after_completed_run_rejected(self) -> None:
        runtime, _, _ = make_runtime([FakeResponse(content="done")])
        result = await runtime.run("task", WS)
        with pytest.raises(ResumeValidationError, match="assistant tail"):
            await runtime.continue_run(result.session_id)

    async def test_continue_unknown_session_rejected(self) -> None:
        runtime, _, _ = make_runtime([])
        with pytest.raises(UnknownSessionError):
            await runtime.continue_run("sess_missing")

    async def test_unresolved_tool_call_blocks_continue(self) -> None:
        runtime, _, _ = make_runtime([])
        log = runtime.registry.create_session()
        _append_interrupted_history(log, with_result=False)
        with pytest.raises(ResumeValidationError, match="no final result"):
            await runtime.continue_run(log.session_id, workspace=WS)

    async def test_continue_resumes_from_tool_result(self) -> None:
        runtime, provider, _ = make_runtime(
            [FakeResponse(content="resumed answer", stop_reason=StopReason.END_TURN)]
        )
        log = runtime.registry.create_session()
        _append_interrupted_history(log, with_result=True)
        result = await runtime.continue_run(log.session_id, workspace=WS)

        assert result.status is RunStatus.FINISHED
        assert result.final_text == "resumed answer"
        assert provider.call_count == 1
        # 继续运行不追加用户消息，历史在既有尾部之上追加
        assert [m.message_type for m in log.messages] == [
            "user",
            "assistant",
            "tool_result",
            "assistant",
        ]
        request = provider.requests[0]
        assert [m.role for m in request.messages] == [
            ProviderMessageRole.SYSTEM,
            ProviderMessageRole.USER,
            ProviderMessageRole.ASSISTANT,
            ProviderMessageRole.TOOL,
        ]


def _append_interrupted_history(log: MessageLog, *, with_result: bool) -> None:
    """构造中断历史。

    with_result=True：user + assistant(call_1) + result(call_1)（完全解决，尾部为工具结果）。
    with_result=False：user + assistant(call_1, call_2) + result(call_1)
    （尾部类型合法，但 call_2 无最终结果）。
    """

    def meta(turn: int) -> MessageMeta:
        return MessageMeta(
            id=f"msg_{turn}_{len(log)}",  # type: ignore[arg-type]
            session_id=log.session_id,
            run_id="run_interrupted",
            turn_id=turn,
            created_at="2026-09-23T00:00:00.000000Z",
        )

    log.append(UserMessage(meta=meta(1), content="fix bug"))
    calls = (
        (ToolCall(id=ToolCallId("call_1"), name="read", arguments={"path": "a.py"}, ordinal=0),)
        if with_result
        else (
            ToolCall(id=ToolCallId("call_1"), name="read", arguments={"path": "a.py"}, ordinal=0),
            ToolCall(id=ToolCallId("call_2"), name="bash", arguments={"command": "pytest"}, ordinal=1),
        )
    )
    log.append(
        AssistantMessage(meta=meta(1), content="", stop_reason=StopReason.TOOL_CALLS, tool_calls=calls)
    )
    log.append(
        ToolResult(
            meta=meta(1),
            tool_call_id=ToolCallId("call_1"),
            status=ToolResultStatus.COMPLETED,
            content="file contents",
        )
    )
