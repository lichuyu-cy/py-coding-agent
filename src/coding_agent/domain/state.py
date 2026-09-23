"""运行态：一个 run 的唯一控制位置与退出原因。

RuntimeState 为不可变快照；所有转换经 `transition(expected_seq, trigger)`，
以序号守卫并发冲突。只有 Runtime 持锁调用本模块；观察者不得驱动状态。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from coding_agent.domain.errors import StateTransitionError


class StopReason(StrEnum):
    """Provider 输出的归一枚举（模型侧结束原因）。

    不得把 StopReason 直接当作 Runtime 最终状态（RunStatus）。
    """

    END_TURN = "end_turn"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"


class RunStatus(StrEnum):
    """一次 run 的最终结果（Runtime 侧）。"""

    FINISHED = "finished"
    ABORTED = "aborted"
    ERROR = "error"
    BUDGET_EXHAUSTED = "budget_exhausted"


class AgentState(StrEnum):
    """Runtime 状态机的状态集。"""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    PROCESSING_TOOL_RESULT = "processing_tool_result"
    STEERING = "steering"
    COMPACTING = "compacting"
    FOLLOW_UP = "follow_up"
    ABORTING = "aborting"
    ABORTED = "aborted"
    FINISHED = "finished"
    ERROR = "error"


class CancellationState(StrEnum):
    """取消请求的生命周期。"""

    NONE = "none"
    REQUESTED = "requested"
    COMPLETED = "completed"


class StateTrigger(StrEnum):
    """驱动状态转换的事件（由 Runtime 依据已确认事实发出）。"""

    START = "start"
    TOOL_CALL_COMPLETE = "complete_tool_call"
    TOOL_RESULT_RESOLVED = "tool_result_resolved"
    STEERING_ARRIVED = "steering"
    STEERING_INJECTED = "steering_injected"
    CONTINUE = "continue"
    COMPACTION_REQUIRED = "context_insufficient_and_compactable"
    COMPACTION_SUCCEEDED = "compaction_success"
    COMPACTION_FAILED = "compaction_failure"
    FOLLOW_UP_READY = "task_done_with_followup"
    TURN_START = "new_turn"
    ABORT_REQUESTED = "abort"
    CLEANUP_DONE = "cleanup_done"
    FINISH = "no_more_messages"
    FAIL = "unrecoverable_error"


ACTIVE_STATES: frozenset[AgentState] = frozenset(
    {
        AgentState.RUNNING,
        AgentState.WAITING_TOOL,
        AgentState.PROCESSING_TOOL_RESULT,
        AgentState.STEERING,
        AgentState.COMPACTING,
        AgentState.FOLLOW_UP,
    }
)

TERMINAL_STATES: frozenset[AgentState] = frozenset(
    {AgentState.ABORTED, AgentState.FINISHED, AgentState.ERROR}
)

# 转移表（architecture.md「Runtime 状态机」）。
# 补充行：STEERING --steering_injected--> RUNNING（steering 只在下次模型请求边界注入后回到运行态）。
_TRANSITIONS: dict[AgentState, dict[StateTrigger, AgentState]] = {
    AgentState.IDLE: {StateTrigger.START: AgentState.RUNNING},
    AgentState.RUNNING: {
        StateTrigger.TOOL_CALL_COMPLETE: AgentState.WAITING_TOOL,
        StateTrigger.COMPACTION_REQUIRED: AgentState.COMPACTING,
        StateTrigger.FOLLOW_UP_READY: AgentState.FOLLOW_UP,
        StateTrigger.FINISH: AgentState.FINISHED,
        StateTrigger.FAIL: AgentState.ERROR,
    },
    AgentState.WAITING_TOOL: {StateTrigger.TOOL_RESULT_RESOLVED: AgentState.PROCESSING_TOOL_RESULT},
    AgentState.PROCESSING_TOOL_RESULT: {
        StateTrigger.STEERING_ARRIVED: AgentState.STEERING,
        StateTrigger.CONTINUE: AgentState.RUNNING,
    },
    AgentState.STEERING: {StateTrigger.STEERING_INJECTED: AgentState.RUNNING},
    AgentState.COMPACTING: {
        StateTrigger.COMPACTION_SUCCEEDED: AgentState.RUNNING,
        StateTrigger.COMPACTION_FAILED: AgentState.ERROR,
    },
    AgentState.FOLLOW_UP: {StateTrigger.TURN_START: AgentState.RUNNING},
    AgentState.ABORTING: {StateTrigger.CLEANUP_DONE: AgentState.ABORTED},
}

# abort 可在任意活动态请求（ABORTING 重入为幂等，见 request_abort）。
for _state in ACTIVE_STATES:
    _TRANSITIONS[_state][StateTrigger.ABORT_REQUESTED] = AgentState.ABORTING

_DEFAULT_STATUS: dict[StateTrigger, RunStatus] = {
    StateTrigger.FINISH: RunStatus.FINISHED,
    StateTrigger.FAIL: RunStatus.ERROR,
    StateTrigger.CLEANUP_DONE: RunStatus.ABORTED,
    StateTrigger.COMPACTION_FAILED: RunStatus.ERROR,
}


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """一次 run 的不可变运行态快照。

    state_seq 每次成功转换递增；current_turn 从 1 开始；current_tool_call_id
    在进入 WAITING_TOOL 时设置，仅在 WAITING_TOOL→PROCESSING_TOOL_RESULT 的
    转换中保留，其余转换一律清除。
    """

    run_id: str
    state: AgentState = AgentState.IDLE
    state_seq: int = 0
    current_turn: int = 0
    current_tool_call_id: str | None = None
    status: RunStatus | None = None
    last_stop_reason: StopReason | None = None
    cancellation: CancellationState = CancellationState.NONE
    abort_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    def can_start_tool(self) -> bool:
        """只有 WAITING_TOOL 允许开始新的工具执行；ABORTING 与终结态一律禁止。"""
        return self.state is AgentState.WAITING_TOOL

    def can_transition(self, trigger: StateTrigger) -> bool:
        if self.is_terminal:
            return False
        return trigger in _TRANSITIONS.get(self.state, {})

    def transition(
        self,
        expected_seq: int,
        trigger: StateTrigger,
        *,
        status: RunStatus | None = None,
        stop_reason: StopReason | None = None,
        tool_call_id: str | None = None,
    ) -> RuntimeState:
        """以 expected_seq 守卫执行的原子转换，返回新快照。

        - expected_seq 与当前 state_seq 不符：并发冲突，拒绝（StateTransitionError）。
        - 非法转换：拒绝，不偷偷跳状态。
        - status：仅终结类触发可显式指定（如 FINISH 时区分 FINISHED / BUDGET_EXHAUSTED）。
        """
        if expected_seq != self.state_seq:
            raise StateTransitionError(
                f"stale state_seq: expected {expected_seq}, actual {self.state_seq}",
                current_state=self.state.value,
                trigger=trigger.value,
            )
        if self.is_terminal:
            raise StateTransitionError(
                f"state {self.state.value} is terminal",
                current_state=self.state.value,
                trigger=trigger.value,
            )
        next_state = _TRANSITIONS.get(self.state, {}).get(trigger)
        if next_state is None:
            raise StateTransitionError(
                f"illegal transition: {self.state.value} + {trigger.value}",
                current_state=self.state.value,
                trigger=trigger.value,
            )

        new = replace(
            self,
            state=next_state,
            state_seq=self.state_seq + 1,
            last_stop_reason=stop_reason if stop_reason is not None else self.last_stop_reason,
        )

        # 回合计数：开始为第 1 回合；follow-up 新 turn 递增。
        if trigger is StateTrigger.START:
            new = replace(new, current_turn=1)
        elif trigger is StateTrigger.TURN_START:
            new = replace(new, current_turn=self.current_turn + 1)

        # 在途工具调用：进入 WAITING_TOOL 时设置；离开 PROCESSING_TOOL_RESULT 时清除。
        if trigger is StateTrigger.TOOL_CALL_COMPLETE:
            new = replace(new, current_tool_call_id=tool_call_id)
        elif self.state is AgentState.WAITING_TOOL and trigger is StateTrigger.TOOL_RESULT_RESOLVED:
            new = replace(new, current_tool_call_id=self.current_tool_call_id)
        elif new.current_tool_call_id is not None:
            new = replace(new, current_tool_call_id=None)

        # 取消生命周期与终结状态。
        if trigger is StateTrigger.ABORT_REQUESTED:
            new = replace(new, cancellation=CancellationState.REQUESTED)
        if trigger is StateTrigger.CLEANUP_DONE:
            new = replace(new, cancellation=CancellationState.COMPLETED)
        final_status = status if status is not None else _DEFAULT_STATUS.get(trigger)
        if final_status is not None:
            new = replace(new, status=final_status)
        return new

    def request_abort(self, reason: str) -> RuntimeState:
        """请求取消：活动态进入 ABORTING 并记录原因。

        未开始（IDLE）、已处于 ABORTING 或已终结时为幂等 no-op，重复请求不抛错。
        """
        if self.state in (AgentState.IDLE, AgentState.ABORTING) or self.is_terminal:
            return self
        new = self.transition(self.state_seq, StateTrigger.ABORT_REQUESTED)
        return replace(new, abort_reason=reason)
