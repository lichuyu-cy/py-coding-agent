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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from coding_agent.agent.control import FollowUpQueue, RunControl
from coding_agent.context.builder import ContextError, ContextManager, ContextPolicy, PromptSection
from coding_agent.domain.messages import (
    AssistantMessage,
    MessageLog,
    MessageMeta,
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
from coding_agent.domain.events import EventType
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.stream import AgentStreamAggregator
from coding_agent.ports.provider import (
    CancelSignal,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    ProviderErrorKind,
    StopDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallFragmentDelta,
    ToolDefinition,
    UsageDelta,
)
from coding_agent.ports.tool import ToolOutcome


class LoopEventKind(StrEnum):
    """最小循环通知（阶段 15 的 typed EventBus 将取代本机制）。"""

    RUN_START = "run_start"
    TURN_START = "turn_start"
    ASSISTANT_COMMITTED = "assistant_committed"
    TOOL_RESULT_COMMITTED = "tool_result_committed"
    STEERING_INJECTED = "steering_injected"
    FOLLOW_UP_STARTED = "follow_up_started"
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
        observation_formatter: Callable[[ToolResult], str] | None = None,
        context_manager: ContextManager | None = None,
        event_bus: EventBus | None = None,
        streaming: bool = False,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._limits = limits or RunLimits()
        self._system_prompt = system_prompt
        self._tools = tools
        self._model_name = model_name
        self._observer = observer
        self._observation_formatter = observation_formatter
        self._event_bus = event_bus
        self._streaming = streaming
        self._context = context_manager or ContextManager(
            ContextPolicy(system_prompt=system_prompt),
            observation_formatter=observation_formatter,
        )

    async def run(
        self,
        *,
        log: MessageLog,
        state: RuntimeState,
        workspace: Path,
        control: RunControl | None = None,
        follow_ups: FollowUpQueue | None = None,
        extra_sections: Sequence[PromptSection] = (),
    ) -> LoopOutcome:
        control = control or RunControl(state.run_id)
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
                retryable=outcome.retryable,
                exit_code=outcome.exit_code,
            )
            log.append(result)
            emit(LoopEventKind.TOOL_RESULT_COMMITTED, turn=state.current_turn, message_id=str(result.meta.id))

        def remaining_seconds() -> float | None:
            if self._limits.deadline_seconds is None:
                return None
            return self._limits.deadline_seconds - (
                asyncio.get_running_loop().time() - started_at
            )

        def publish(event_type: EventType, payload: dict | None = None) -> None:
            """发布 typed 事件（无总线时为 no-op）；订阅者失败由总线隔离。"""
            if self._event_bus is not None:
                self._event_bus.emit(
                    event_type, run_id=run_id, session_id=session_id, payload=payload or {}
                )

        def inject_user_text(text: str, kind: LoopEventKind, detail: str | None = None) -> None:
            """在下一个模型请求边界前，把控制消息作为 UserMessage 注入历史。"""
            message = UserMessage(meta=new_meta(), content=text)
            log.append(message)
            emit(kind, turn=state.current_turn, message_id=str(message.meta.id), detail=detail)
            if kind is LoopEventKind.STEERING_INJECTED:
                publish(EventType.STEERING, {"steering_id": detail, "text_length": len(text)})
            elif kind is LoopEventKind.FOLLOW_UP_STARTED:
                publish(EventType.FOLLOW_UP, {"follow_up_id": detail, "text_length": len(text)})

        def abort_run(reason: str) -> None:
            """请求已下达的取消：进入 ABORTING→ABORTED（不启动新工具/新请求）。"""
            nonlocal state
            publish(EventType.ABORT, {"reason": reason})
            state = state.request_abort(reason)
            state = state.transition(
                state.state_seq, StateTrigger.CLEANUP_DONE, stop_reason=StopReason.CANCELLED
            )

        emit(LoopEventKind.RUN_START, turn=state.current_turn)
        publish(EventType.AGENT_START, {"turn": state.current_turn})

        while True:
            # 取消优先级最高：任何活动态在边界处观察到取消都立即终止。
            if control.cancel.is_cancelled:
                abort_run(control.cancel.reason or "abort requested")
                break
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

            # steering 只在模型请求边界注入（一次性 drain，按入队顺序）。
            if state.state is AgentState.STEERING:
                state = state.transition(state.state_seq, StateTrigger.STEERING_INJECTED)
            for steering_message in control.steering.drain_before_next_request():
                inject_user_text(
                    steering_message.text, LoopEventKind.STEERING_INJECTED, detail=steering_message.id
                )

            emit(LoopEventKind.TURN_START, turn=state.current_turn)
            try:
                request = self._build_request(log, extra_sections, publish)
            except ContextError:
                # 上下文无法适配（预算不足或需要压缩且不可用）：可解释的预算终止。
                limit_hit = "context_overflow"
                state = state.transition(
                    state.state_seq,
                    StateTrigger.FINISH,
                    status=RunStatus.BUDGET_EXHAUSTED,
                )
                break

            publish(
                EventType.LLM_REQUEST_START,
                {"request_id": request.request_id, "turn": state.current_turn},
            )

            async def call_model() -> ModelResponse:
                """按配置选择 complete 或 stream（增量聚合为同一完整响应契约）。"""
                if not self._streaming:
                    return await self._provider.complete(request, control.cancel)
                aggregator = AgentStreamAggregator()
                async for delta in self._provider.stream(request, control.cancel):
                    aggregator.aggregate(delta)
                    publish(EventType.LLM_REQUEST_STREAM, _stream_delta_payload(request, delta))
                return aggregator.finalize_response()

            response: ModelResponse | None = None
            provider_error: ProviderError | None = None
            deadline_exceeded = False
            retries = 0
            while True:
                provider_calls += 1
                try:
                    calls = call_model()
                    if remaining is not None:
                        response = await asyncio.wait_for(calls, timeout=remaining)
                    else:
                        response = await calls
                    break
                except TimeoutError:
                    deadline_exceeded = True
                    break
                except ProviderError as err:
                    if err.kind is ProviderErrorKind.CANCELLED or control.cancel.is_cancelled:
                        provider_error = err
                        break
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
                publish(EventType.ERROR, {"stage": "llm_request", "kind": "timeout"})
                limit_hit = "deadline"
                state = state.transition(
                    state.state_seq,
                    StateTrigger.FINISH,
                    status=RunStatus.BUDGET_EXHAUSTED,
                )
                break
            if provider_error is not None:
                if (
                    provider_error.kind is ProviderErrorKind.CANCELLED
                    or control.cancel.is_cancelled
                ):
                    abort_run(control.cancel.reason or "provider request cancelled")
                    break
                publish(
                    EventType.ERROR,
                    {"stage": "provider", "kind": provider_error.kind.value},
                )
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
            usage = response.usage
            publish(
                EventType.LLM_REQUEST_END,
                {
                    "request_id": request.request_id,
                    "response_id": response.response_id,
                    "stop_reason": stop_reason.value,
                    "usage_input_tokens": usage.input_tokens if usage is not None else None,
                    "usage_output_tokens": usage.output_tokens if usage is not None else None,
                },
            )
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
                aborted_mid_batch = False
                for index, call in enumerate(ordered):
                    if control.cancel.is_cancelled:
                        # 取消发生在两次执行之间：为剩余完整调用记录明确的 CANCELLED 结果。
                        for pending in ordered[index:]:
                            commit_result(
                                pending,
                                ToolOutcome(
                                    status=ToolResultStatus.CANCELLED,
                                    content="run aborted; call was not executed",
                                    error_kind="cancelled",
                                    retryable=False,
                                ),
                            )
                            publish(
                                EventType.TOOL_CALL_ERROR,
                                {
                                    "tool_call_id": str(pending.id),
                                    "name": pending.name,
                                    "status": "cancelled",
                                    "error_kind": "cancelled",
                                    "executed": False,
                                },
                            )
                        aborted_mid_batch = True
                        break
                    if tool_calls_used >= self._limits.max_tool_calls:
                        for pending in ordered[index:]:
                            commit_result(
                                pending,
                                ToolOutcome(
                                    status=ToolResultStatus.CANCELLED,
                                    content="tool call budget exhausted; call was not executed",
                                    error_kind="tool_budget_exhausted",
                                    retryable=False,
                                ),
                            )
                            publish(
                                EventType.TOOL_CALL_ERROR,
                                {
                                    "tool_call_id": str(pending.id),
                                    "name": pending.name,
                                    "status": "cancelled",
                                    "error_kind": "tool_budget_exhausted",
                                    "executed": False,
                                },
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
                    publish(
                        EventType.TOOL_CALL_START,
                        {"tool_call_id": str(call.id), "name": call.name, "ordinal": call.ordinal},
                    )
                    outcome = await self._executor.execute(
                        call,
                        workspace=workspace,
                        cancel=control.cancel,
                        deadline=remaining_seconds(),
                    )
                    tool_calls_used += 1
                    state = state.transition(state.state_seq, StateTrigger.TOOL_RESULT_RESOLVED)
                    commit_result(call, outcome)
                    publish(
                        EventType.TOOL_CALL_END
                        if outcome.status is ToolResultStatus.COMPLETED
                        else EventType.TOOL_CALL_ERROR,
                        {
                            "tool_call_id": str(call.id),
                            "name": call.name,
                            "status": outcome.status.value,
                            "error_kind": outcome.error_kind,
                            "exit_code": outcome.exit_code,
                            "retryable": outcome.retryable,
                        },
                    )
                    is_last_call = index == len(ordered) - 1
                    if is_last_call and control.steering.has_pending():
                        # 工具结果处理期间到达的 steering：按状态机进入 STEERING，下次请求前注入。
                        state = state.transition(state.state_seq, StateTrigger.STEERING_ARRIVED)
                    else:
                        state = state.transition(state.state_seq, StateTrigger.CONTINUE)
                if budget_hit:
                    state = state.transition(
                        state.state_seq,
                        StateTrigger.FINISH,
                        status=RunStatus.BUDGET_EXHAUSTED,
                        stop_reason=stop_reason,
                    )
                    break
                if aborted_mid_batch:
                    abort_run(control.cancel.reason or "abort requested")
                    break
                if control.cancel.is_cancelled:
                    abort_run(control.cancel.reason or "abort requested")
                    break
                continue  # 下一模型回合

            # 无工具调用：结束当前 turn。steering 优先于 follow-up；否则正常结束。
            if state.state is AgentState.STEERING:
                state = state.transition(state.state_seq, StateTrigger.STEERING_INJECTED)
            steering_after_turn = control.steering.drain_before_next_request()
            if steering_after_turn:
                for steering_message in steering_after_turn:
                    inject_user_text(
                        steering_message.text, LoopEventKind.STEERING_INJECTED, detail=steering_message.id
                    )
                continue
            next_task = follow_ups.dequeue_after_turn_end() if follow_ups is not None else None
            if next_task is not None:
                state = state.transition(state.state_seq, StateTrigger.FOLLOW_UP_READY)
                state = state.transition(state.state_seq, StateTrigger.TURN_START)
                inject_user_text(next_task.text, LoopEventKind.FOLLOW_UP_STARTED, detail=next_task.id)
                continue
            state = state.transition(state.state_seq, StateTrigger.FINISH, stop_reason=stop_reason)
            break

        emit(LoopEventKind.RUN_END, turn=state.current_turn, detail=state.state.value)
        publish(
            EventType.AGENT_END,
            {
                "status": state.status.value if state.status is not None else None,
                "limit_hit": limit_hit,
                "turns": turns,
                "tool_calls": tool_calls_used,
                "provider_calls": provider_calls,
            },
        )
        return LoopOutcome(
            state=state,
            turns=turns,
            tool_calls=tool_calls_used,
            provider_calls=provider_calls,
            final_text=final_text,
            limit_hit=limit_hit,
        )

    def _build_request(
        self,
        log: MessageLog,
        extra_sections: Sequence[PromptSection] = (),
        report: Callable[[EventType, dict], None] | None = None,
    ) -> ModelRequest:
        """经 ContextManager 构造确定性的 Provider 请求（阶段 10 接入）。"""
        snapshot = self._context.build(
            log,
            extra_sections=extra_sections,
            tool_definitions=self._tools,
            report=(lambda event_type, payload: report(event_type, dict(payload))) if report else None,
        )
        return ModelRequest(messages=snapshot.messages, tools=self._tools, model=self._model_name)


def _stream_delta_payload(request: ModelRequest, delta: object) -> dict:
    """易失增量事件的载荷（文本内容或分片长度；不含大对象）。"""
    payload: dict = {"request_id": request.request_id}
    if isinstance(delta, TextDelta):
        payload.update(kind="text", text=delta.text)
    elif isinstance(delta, ToolCallDelta):
        payload.update(kind="tool_call", tool_call_id=str(delta.call.id), name=delta.call.name)
    elif isinstance(delta, ToolCallFragmentDelta):
        payload.update(
            kind="tool_fragment",
            tool_call_id=delta.call_id,
            fragment_seq=delta.fragment_seq,
            fragment_len=len(delta.arguments_fragment),
        )
    elif isinstance(delta, UsageDelta):
        payload.update(kind="usage", usage=delta.usage)
    elif isinstance(delta, StopDelta):
        payload.update(kind="stop", stop_reason=delta.stop_reason.value)
    else:  # pragma: no cover - 防御未知增量
        payload.update(kind="unknown")
    return payload
