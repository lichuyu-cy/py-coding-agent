"""最小 Agent Loop：Think（模型请求）→ Act（工具）→ Observe（结果再入上下文）的有界循环。

阶段 04 的范围与刻意限制：
- 只消费 `Provider.complete()` 的完整响应，不使用 stream（阶段 17）；
- 工具经「极简工具执行口」（`MinimalToolExecutor`）执行，阶段 07 替换为 Tool Pipeline；
- 上下文为最小组装（system + 全部已提交消息），阶段 10 替换为 ContextManager；
- steering / follow-up / abort 队列（阶段 09）、Token 预算与压缩（阶段 13/14）尚未实现，
  仅以 turn / 工具调用 / 截止时间三类限额约束。

状态驱动遵循 domain.state 的转移表；所有持久写入都发生在已确认事实之后。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from coding_agent.domain.messages import (
    AssistantMessage,
    MessageLog,
    MessageMeta,
    SystemMessage,
    ToolCall,
    ToolResult,
    ToolResultStatus,
    UserMessage,
    new_message_id,
    utc_now_rfc3339,
)
from coding_agent.domain.state import (
    AgentState,
    RunStatus,
    RuntimeState,
    StateTrigger,
    StopReason,
)
from coding_agent.ports.provider import (
    CancelSignal,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    ProviderErrorKind,
    ProviderMessage,
    ProviderMessageRole,
    ProviderToolCallPart,
    ToolDefinition,
)
from coding_agent.ports.tool import ToolOutcome


class LoopEventKind(StrEnum):
    """最小循环通知（阶段 15 的 typed EventBus 将取代本机制）。"""

    RUN_START = "run_start"
    TURN_START = "turn_start"
    ASSISTANT_COMMITTED = "assistant_committed"
    TOOL_RESULT_COMMITTED = "tool_result_committed"
    RUN_END = "run_end"


@dataclass(frozen=True, slots=True)
class LoopEvent:
    kind: LoopEventKind
    run_id: str
    session_id: str
    turn: int
    message_id: str | None = None
    detail: str | None = None


LoopObserver = Callable[[LoopEvent], None]

# ToolOutcome 自阶段 07 起归属 ports.tool（Pipeline 归一结果载荷），此处重导出保持兼容。
__all__ = [
    "AgentLoop",
    "LoopEvent",
    "LoopEventKind",
    "LoopObserver",
    "LoopOutcome",
    "MinimalToolExecutor",
    "RunLimits",
    "ToolOutcome",
]


class MinimalToolExecutor(Protocol):
    """工具执行口：阶段 07 起由 ToolPipeline 实现（结构匹配）。

    生产路径不直接暴露给模型；本协议保持"工具必经统一口"的形状。
    """

    async def execute(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        cancel: CancelSignal | None,
        deadline: float | None = None,
    ) -> ToolOutcome: ...


@dataclass(frozen=True, slots=True)
class RunLimits:
    """运行限额（默认值来自 critical-contracts.md 第 6 节，非评测最优结论）。"""

    max_turns: int = 30
    max_tool_calls: int = 80
    deadline_seconds: float | None = 1200.0
    max_provider_retries: int = 2
    provider_retry_base_delay_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    state: RuntimeState
    turns: int
    tool_calls: int
    provider_calls: int
    final_text: str
    limit_hit: str | None  # "max_turns" | "max_tool_calls" | "deadline" | None


_TRANSIENT_PROVIDER_ERRORS = frozenset(
    {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.UNAVAILABLE,
    }
)


class AgentLoop:
    """单 run 的有界循环；Runtime 负责会话、锁与结果包装。"""

    def __init__(
        self,
        *,
        provider: Provider,
        executor: MinimalToolExecutor,
        limits: RunLimits | None = None,
        system_prompt: str,
        tools: tuple[ToolDefinition, ...] = (),
        model_name: str = "default",
        observer: LoopObserver | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._limits = limits or RunLimits()
        self._system_prompt = system_prompt
        self._tools = tools
        self._model_name = model_name
        self._observer = observer

    async def run(
        self, *, log: MessageLog, state: RuntimeState, workspace: Path
    ) -> LoopOutcome:
        run_id = state.run_id
        session_id = log.session_id
        turns = 0
        tool_calls_used = 0
        provider_calls = 0
        final_text = ""
        limit_hit: str | None = None
        started_at = asyncio.get_running_loop().time()

        def emit(
            kind: LoopEventKind,
            *,
            turn: int,
            message_id: str | None = None,
            detail: str | None = None,
        ) -> None:
            if self._observer is not None:
                self._observer(
                    LoopEvent(
                        kind=kind,
                        run_id=run_id,
                        session_id=session_id,
                        turn=turn,
                        message_id=message_id,
                        detail=detail,
                    )
                )

        def new_meta() -> MessageMeta:
            return MessageMeta(
                id=new_message_id(),
                session_id=session_id,
                run_id=run_id,
                turn_id=state.current_turn,
                created_at=utc_now_rfc3339(),
            )

        def commit_result(call: ToolCall, outcome: ToolOutcome) -> None:
            result = ToolResult(
                meta=new_meta(),
                tool_call_id=call.id,
                status=outcome.status,
                content=outcome.content,
                artifact_ref=outcome.artifact_ref,
                error_kind=outcome.error_kind,
            )
            log.append(result)
            emit(LoopEventKind.TOOL_RESULT_COMMITTED, turn=state.current_turn, message_id=str(result.meta.id))

        def remaining_seconds() -> float | None:
            if self._limits.deadline_seconds is None:
                return None
            return self._limits.deadline_seconds - (
                asyncio.get_running_loop().time() - started_at
            )

        emit(LoopEventKind.RUN_START, turn=state.current_turn)

        while True:
            remaining = remaining_seconds()
            if remaining is not None and remaining <= 0:
                limit_hit = "deadline"
                state = state.transition(
                    state.state_seq,
                    StateTrigger.FINISH,
                    status=RunStatus.BUDGET_EXHAUSTED,
                )
                break
            if turns >= self._limits.max_turns:
                limit_hit = "max_turns"
                state = state.transition(
                    state.state_seq,
                    StateTrigger.FINISH,
                    status=RunStatus.BUDGET_EXHAUSTED,
                )
                break

            emit(LoopEventKind.TURN_START, turn=state.current_turn)
            request = self._build_request(log)

            response: ModelResponse | None = None
            provider_error: ProviderError | None = None
            deadline_exceeded = False
            retries = 0
            while True:
                provider_calls += 1
                try:
                    calls = self._provider.complete(request, None)
                    if remaining is not None:
                        response = await asyncio.wait_for(calls, timeout=remaining)
                    else:
                        response = await calls
                    break
                except TimeoutError:
                    deadline_exceeded = True
                    break
                except ProviderError as err:
                    if (
                        err.kind in _TRANSIENT_PROVIDER_ERRORS
                        and retries < self._limits.max_provider_retries
                    ):
                        retries += 1
                        delay = self._limits.provider_retry_base_delay_seconds * (2 ** (retries - 1))
                        if delay > 0:
                            await asyncio.sleep(delay)
                        continue
                    provider_error = err
                    break

            if deadline_exceeded:
                limit_hit = "deadline"
                state = state.transition(
                    state.state_seq,
                    StateTrigger.FINISH,
                    status=RunStatus.BUDGET_EXHAUSTED,
                )
                break
            if provider_error is not None:
                state = state.transition(
                    state.state_seq, StateTrigger.FAIL, stop_reason=StopReason.PROVIDER_ERROR
                )
                break
            assert response is not None

            turns += 1
            stop_reason = response.stop_reason
            assistant = AssistantMessage(
                meta=new_meta(),
                content=response.content,
                stop_reason=stop_reason,
                tool_calls=response.tool_calls,
            )
            log.append(assistant)
            emit(LoopEventKind.ASSISTANT_COMMITTED, turn=state.current_turn, message_id=str(assistant.meta.id))
            final_text = response.content

            if stop_reason is StopReason.PROVIDER_ERROR:
                state = state.transition(state.state_seq, StateTrigger.FAIL, stop_reason=stop_reason)
                break
            if stop_reason is StopReason.CANCELLED:
                state = state.request_abort("provider reported cancelled response")
                state = state.transition(
                    state.state_seq, StateTrigger.CLEANUP_DONE, stop_reason=stop_reason
                )
                break

            if response.tool_calls:
                ordered = sorted(response.tool_calls, key=lambda call: call.ordinal)
                state = state.transition(
                    state.state_seq,
                    StateTrigger.TOOL_CALL_COMPLETE,
                    stop_reason=stop_reason,
                    tool_call_id=str(ordered[0].id),
                )
                budget_hit = False
                for index, call in enumerate(ordered):
                    if tool_calls_used >= self._limits.max_tool_calls:
                        for pending in ordered[index:]:
                            commit_result(
                                pending,
                                ToolOutcome(
                                    status=ToolResultStatus.CANCELLED,
                                    content="tool call budget exhausted; call was not executed",
                                    error_kind="tool_budget_exhausted",
                                ),
                            )
                        limit_hit = "max_tool_calls"
                        budget_hit = True
                        break
                    if state.state is not AgentState.WAITING_TOOL:
                        state = state.transition(
                            state.state_seq,
                            StateTrigger.TOOL_CALL_COMPLETE,
                            tool_call_id=str(call.id),
                        )
                    outcome = await self._executor.execute(
                        call, workspace=workspace, cancel=None, deadline=remaining_seconds()
                    )
                    tool_calls_used += 1
                    state = state.transition(state.state_seq, StateTrigger.TOOL_RESULT_RESOLVED)
                    commit_result(call, outcome)
                    state = state.transition(state.state_seq, StateTrigger.CONTINUE)
                if budget_hit:
                    state = state.transition(
                        state.state_seq,
                        StateTrigger.FINISH,
                        status=RunStatus.BUDGET_EXHAUSTED,
                        stop_reason=stop_reason,
                    )
                    break
                continue  # 下一模型回合

            # 无工具调用：正常结束（含 END_TURN / MAX_TOKENS）
            state = state.transition(state.state_seq, StateTrigger.FINISH, stop_reason=stop_reason)
            break

        emit(LoopEventKind.RUN_END, turn=state.current_turn, detail=state.state.value)
        return LoopOutcome(
            state=state,
            turns=turns,
            tool_calls=tool_calls_used,
            provider_calls=provider_calls,
            final_text=final_text,
            limit_hit=limit_hit,
        )

    def _build_request(self, log: MessageLog) -> ModelRequest:
        """最小上下文组装：system + 已提交消息的 Provider 投影（阶段 10 替换）。"""
        messages: list[ProviderMessage] = [
            ProviderMessage(role=ProviderMessageRole.SYSTEM, content=self._system_prompt)
        ]
        for message in log.messages:
            if isinstance(message, SystemMessage):
                messages.append(ProviderMessage(role=ProviderMessageRole.SYSTEM, content=message.content))
            elif isinstance(message, UserMessage):
                messages.append(ProviderMessage(role=ProviderMessageRole.USER, content=message.content))
            elif isinstance(message, AssistantMessage):
                messages.append(
                    ProviderMessage(
                        role=ProviderMessageRole.ASSISTANT,
                        content=message.content,
                        tool_calls=tuple(
                            ProviderToolCallPart(
                                id=str(call.id), name=call.name, arguments=dict(call.arguments)
                            )
                            for call in sorted(message.tool_calls, key=lambda c: c.ordinal)
                        ),
                    )
                )
            elif isinstance(message, ToolResult):
                messages.append(
                    ProviderMessage(
                        role=ProviderMessageRole.TOOL,
                        content=message.content,
                        tool_call_id=str(message.tool_call_id),
                    )
                )
        return ModelRequest(messages=tuple(messages), tools=self._tools, model=self._model_name)
