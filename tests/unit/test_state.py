"""阶段 02 单测：运行态状态机的转换表、并发守卫与终止语义。"""

import pytest

from coding_agent.domain.errors import StateTransitionError
from coding_agent.domain.state import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    AgentState,
    CancellationState,
    RunStatus,
    RuntimeState,
    StateTrigger,
    StopReason,
)

# 依据 architecture.md「Runtime 状态机」整理的完整转换表（含显式补充行通过注释标注）。
EXPECTED_TRANSITIONS: list[tuple[AgentState, StateTrigger, AgentState]] = [
    (AgentState.IDLE, StateTrigger.START, AgentState.RUNNING),
    (AgentState.RUNNING, StateTrigger.TOOL_CALL_COMPLETE, AgentState.WAITING_TOOL),
    (AgentState.WAITING_TOOL, StateTrigger.TOOL_RESULT_RESOLVED, AgentState.PROCESSING_TOOL_RESULT),
    (AgentState.PROCESSING_TOOL_RESULT, StateTrigger.STEERING_ARRIVED, AgentState.STEERING),
    (AgentState.PROCESSING_TOOL_RESULT, StateTrigger.CONTINUE, AgentState.RUNNING),
    (AgentState.STEERING, StateTrigger.STEERING_INJECTED, AgentState.RUNNING),  # 补充行：注入后回到运行态
    (AgentState.RUNNING, StateTrigger.COMPACTION_REQUIRED, AgentState.COMPACTING),
    (AgentState.COMPACTING, StateTrigger.COMPACTION_SUCCEEDED, AgentState.RUNNING),
    (AgentState.COMPACTING, StateTrigger.COMPACTION_FAILED, AgentState.ERROR),
    (AgentState.RUNNING, StateTrigger.FOLLOW_UP_READY, AgentState.FOLLOW_UP),
    (AgentState.FOLLOW_UP, StateTrigger.TURN_START, AgentState.RUNNING),
    (AgentState.RUNNING, StateTrigger.ABORT_REQUESTED, AgentState.ABORTING),
    (AgentState.WAITING_TOOL, StateTrigger.ABORT_REQUESTED, AgentState.ABORTING),
    (AgentState.PROCESSING_TOOL_RESULT, StateTrigger.ABORT_REQUESTED, AgentState.ABORTING),
    (AgentState.STEERING, StateTrigger.ABORT_REQUESTED, AgentState.ABORTING),
    (AgentState.COMPACTING, StateTrigger.ABORT_REQUESTED, AgentState.ABORTING),
    (AgentState.FOLLOW_UP, StateTrigger.ABORT_REQUESTED, AgentState.ABORTING),
    (AgentState.ABORTING, StateTrigger.CLEANUP_DONE, AgentState.ABORTED),
    (AgentState.RUNNING, StateTrigger.FINISH, AgentState.FINISHED),
    (AgentState.RUNNING, StateTrigger.FAIL, AgentState.ERROR),
]


def state_at(agent_state: AgentState, seq: int = 0, turn: int = 1) -> RuntimeState:
    return RuntimeState(run_id="run_1", state=agent_state, state_seq=seq, current_turn=turn)


class TestTransitionTable:
    @pytest.mark.parametrize("current,trigger,expected", EXPECTED_TRANSITIONS)
    def test_legal_transition(self, current: AgentState, trigger: StateTrigger, expected: AgentState) -> None:
        before = state_at(current)
        after = before.transition(before.state_seq, trigger)
        assert after.state is expected
        assert after.state_seq == before.state_seq + 1
        assert before.state is current  # 原快照不可变

    @pytest.mark.parametrize(
        "current,trigger",
        [
            (AgentState.IDLE, StateTrigger.CONTINUE),
            (AgentState.IDLE, StateTrigger.FINISH),
            (AgentState.WAITING_TOOL, StateTrigger.CONTINUE),
            (AgentState.RUNNING, StateTrigger.CLEANUP_DONE),
            (AgentState.RUNNING, StateTrigger.TURN_START),
            (AgentState.FINISHED, StateTrigger.START),
            (AgentState.ABORTED, StateTrigger.FINISH),
            (AgentState.ERROR, StateTrigger.CONTINUE),
            (AgentState.ABORTING, StateTrigger.FINISH),
        ],
    )
    def test_illegal_transition_rejected(self, current: AgentState, trigger: StateTrigger) -> None:
        with pytest.raises(StateTransitionError):
            state_at(current).transition(0, trigger)

    def test_stale_seq_rejected(self) -> None:
        current = state_at(AgentState.RUNNING, seq=3)
        with pytest.raises(StateTransitionError, match="stale state_seq"):
            current.transition(2, StateTrigger.FINISH)

    def test_loser_of_race_rejected(self) -> None:
        """竞态重复提交：两个提交者基于同一快照，后到者以旧序号提交被拒。"""
        base = state_at(AgentState.IDLE)
        winner = base.transition(base.state_seq, StateTrigger.START)
        with pytest.raises(StateTransitionError):
            winner.transition(base.state_seq, StateTrigger.START)


class TestTerminationSemantics:
    def test_can_start_tool_only_in_waiting_tool(self) -> None:
        for agent_state in AgentState:
            expected = agent_state is AgentState.WAITING_TOOL
            assert state_at(agent_state).can_start_tool() is expected

    def test_terminal_states_cannot_transition(self) -> None:
        for agent_state in TERMINAL_STATES:
            with pytest.raises(StateTransitionError, match="terminal"):
                state_at(agent_state).transition(0, StateTrigger.START)

    def test_abort_idempotent_from_active_states(self) -> None:
        for agent_state in ACTIVE_STATES:
            aborted = state_at(agent_state).request_abort("user request")
            assert aborted.state is AgentState.ABORTING
            assert aborted.cancellation is CancellationState.REQUESTED
            assert aborted.abort_reason == "user request"
            assert aborted.request_abort("again") is aborted  # 幂等 no-op

    def test_abort_noop_from_idle_and_terminal(self) -> None:
        for agent_state in (AgentState.IDLE, *TERMINAL_STATES):
            before = state_at(agent_state)
            assert before.request_abort("idle") is before

    def test_cleanup_completes_cancellation_and_status(self) -> None:
        aborted = state_at(AgentState.RUNNING).request_abort("stop")
        done = aborted.transition(aborted.state_seq, StateTrigger.CLEANUP_DONE)
        assert done.state is AgentState.ABORTED
        assert done.cancellation is CancellationState.COMPLETED
        assert done.status is RunStatus.ABORTED


class TestStopReasonIndependence:
    def test_stop_reason_recorded_without_forcing_status(self) -> None:
        running = state_at(AgentState.RUNNING)
        after = running.transition(
            running.state_seq,
            StateTrigger.TOOL_CALL_COMPLETE,
            stop_reason=StopReason.TOOL_CALLS,
            tool_call_id="call_1",
        )
        assert after.last_stop_reason is StopReason.TOOL_CALLS
        assert after.status is None  # 模型 stop reason 不决定 RunStatus

    def test_finish_with_explicit_budget_status(self) -> None:
        running = state_at(AgentState.RUNNING)
        done = running.transition(
            running.state_seq,
            StateTrigger.FINISH,
            status=RunStatus.BUDGET_EXHAUSTED,
            stop_reason=StopReason.MAX_TOKENS,
        )
        assert done.state is AgentState.FINISHED
        assert done.status is RunStatus.BUDGET_EXHAUSTED
        assert done.last_stop_reason is StopReason.MAX_TOKENS

    def test_default_statuses_for_terminal_triggers(self) -> None:
        assert state_at(AgentState.RUNNING).transition(0, StateTrigger.FINISH).status is RunStatus.FINISHED
        assert state_at(AgentState.RUNNING).transition(0, StateTrigger.FAIL).status is RunStatus.ERROR
        assert (
            state_at(AgentState.COMPACTING).transition(0, StateTrigger.COMPACTION_FAILED).status is RunStatus.ERROR
        )

    def test_enum_value_spaces_are_disjoint(self) -> None:
        stop_values = {reason.value for reason in StopReason}
        status_values = {status.value for status in RunStatus}
        assert stop_values.isdisjoint(status_values)


class TestTurnAndToolTracking:
    def test_start_sets_first_turn(self) -> None:
        state = state_at(AgentState.IDLE, turn=0).transition(0, StateTrigger.START)
        assert state.current_turn == 1

    def test_tool_call_tracking_lifecycle(self) -> None:
        running = state_at(AgentState.RUNNING)
        waiting = running.transition(running.state_seq, StateTrigger.TOOL_CALL_COMPLETE, tool_call_id="call_1")
        assert waiting.current_tool_call_id == "call_1"
        processing = waiting.transition(waiting.state_seq, StateTrigger.TOOL_RESULT_RESOLVED)
        assert processing.current_tool_call_id == "call_1"  # 处理结果期间保留
        resumed = processing.transition(processing.state_seq, StateTrigger.CONTINUE)
        assert resumed.current_tool_call_id is None  # 回到模型调用前清除

    def test_follow_up_starts_next_turn(self) -> None:
        running = state_at(AgentState.RUNNING, turn=1)
        follow_up = running.transition(running.state_seq, StateTrigger.FOLLOW_UP_READY)
        next_turn = follow_up.transition(follow_up.state_seq, StateTrigger.TURN_START)
        assert next_turn.current_turn == 2
