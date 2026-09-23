"""阶段 08 单测：错误归一、恢复提示与模型观察格式；retryable/exit_code 的持久化。"""

from __future__ import annotations

from coding_agent.domain.messages import (
    MessageLog,
    MessageMeta,
    ToolCall,
    ToolCallId,
    ToolResult,
    ToolResultStatus,
)
from coding_agent.domain.state import StopReason
from coding_agent.ports.tool import ToolErrorKind, ToolExecutionError, ToolOutcome
from coding_agent.tools.recovery import (
    hint_for_kind,
    normalize_internal_error,
    normalize_tool_error,
    to_model_observation,
)


def make_result(
    *,
    status: ToolResultStatus = ToolResultStatus.COMPLETED,
    content: str = "some output",
    error_kind: str | None = None,
    retryable: bool | None = None,
    exit_code: int | None = None,
) -> ToolResult:
    return ToolResult(
        meta=MessageMeta(
            id="msg_r1",  # type: ignore[arg-type]
            session_id="sess_1",
            run_id="run_1",
            turn_id=1,
            created_at="2026-09-23T00:00:00.000000Z",
        ),
        tool_call_id=ToolCallId("call_1"),
        status=status,
        content=content,
        error_kind=error_kind,
        retryable=retryable,
        exit_code=exit_code,
    )


class TestHints:
    def test_known_kinds_have_hints(self) -> None:
        assert hint_for_kind("file_not_found").retryable is True
        assert hint_for_kind("replace_not_unique").retryable is True
        assert hint_for_kind("timeout").retryable is False
        assert hint_for_kind("cancelled").retryable is False
        assert hint_for_kind("safety_denied").retryable is False
        assert "rerun" in hint_for_kind("timeout").suggestion

    def test_unknown_and_none_kinds(self) -> None:
        assert hint_for_kind("mystery_kind").retryable is False
        assert hint_for_kind(None).retryable is False

    def test_every_kind_has_a_hint(self) -> None:
        for kind in ToolErrorKind:
            hint = hint_for_kind(kind.value)
            assert hint.suggestion, kind


class TestNormalize:
    def test_file_not_found(self) -> None:
        outcome = normalize_tool_error(ToolExecutionError("file_not_found", "file not found: a.py"))
        assert outcome.status is ToolResultStatus.ERROR
        assert outcome.error_kind == "file_not_found"
        assert outcome.retryable is True

    def test_timeout_keeps_partial_output_and_is_not_retryable(self) -> None:
        outcome = normalize_tool_error(
            ToolExecutionError("timeout", "command timed out", exit_code=-1, output="partial")
        )
        assert outcome.status is ToolResultStatus.TIMEOUT
        assert outcome.retryable is False
        assert outcome.exit_code == -1
        assert "partial" in outcome.content
        assert "timed out" in outcome.content

    def test_cancelled_maps_to_cancelled_status(self) -> None:
        outcome = normalize_tool_error(ToolExecutionError("cancelled", "cancelled"))
        assert outcome.status is ToolResultStatus.CANCELLED
        assert outcome.retryable is False

    def test_internal_error_normalized(self) -> None:
        outcome = normalize_internal_error(ValueError("boom"))
        assert outcome.status is ToolResultStatus.ERROR
        assert outcome.error_kind == "internal_error"
        assert outcome.retryable is False
        assert "ValueError" in outcome.content

    def test_tool_cancelled_is_distinct_from_timeout(self) -> None:
        assert normalize_tool_error(ToolExecutionError("cancelled", "x")).status is ToolResultStatus.CANCELLED
        assert normalize_tool_error(ToolExecutionError("timeout", "x")).status is ToolResultStatus.TIMEOUT


class TestObservation:
    def test_completed_observation(self) -> None:
        text = to_model_observation(make_result(content="line-one\nline-two"))
        assert text.splitlines()[0] == "[tool result: completed]"
        assert "line-one" in text

    def test_error_observation_contains_kind_exit_code_retryable(self) -> None:
        text = to_model_observation(
            make_result(
                status=ToolResultStatus.ERROR,
                content="boom",
                error_kind="nonzero_exit",
                retryable=False,
                exit_code=2,
            )
        )
        header = text.splitlines()[0]
        assert header.startswith("[tool result: error")
        assert "kind=nonzero_exit" in header
        assert "exit_code=2" in header
        assert "retryable=false" in header
        assert any(line.startswith("hint:") for line in text.splitlines())

    def test_retryable_falls_back_to_hint(self) -> None:
        text = to_model_observation(
            make_result(status=ToolResultStatus.ERROR, content="missing", error_kind="invalid_arguments")
        )
        assert "retryable=true" in text.splitlines()[0]

    def test_denied_observation(self) -> None:
        text = to_model_observation(
            make_result(
                status=ToolResultStatus.DENIED,
                content="denied at safety: destructive command",
                error_kind="safety_approval_required",
                retryable=False,
            )
        )
        assert "[tool result: denied" in text
        assert "kind=safety_approval_required" in text


class TestPersistence:
    def test_retryable_and_exit_code_round_trip(self) -> None:
        log = MessageLog("sess_1")
        log.append(_assistant_with_call())
        log.append(
            ToolResult(
                meta=MessageMeta(
                    id="msg_r1",  # type: ignore[arg-type]
                    session_id="sess_1",
                    run_id="run_1",
                    turn_id=1,
                    created_at="2026-09-23T00:00:00.000000Z",
                ),
                tool_call_id=ToolCallId("call_1"),
                status=ToolResultStatus.ERROR,
                content="exit 2",
                error_kind="nonzero_exit",
                retryable=False,
                exit_code=2,
            )
        )
        restored = MessageLog.deserialize(log.serialize())
        result = restored.find_result("call_1")
        assert result is not None
        assert result.retryable is False
        assert result.exit_code == 2

    def test_outcome_defaults_serialize_as_null(self) -> None:
        log = MessageLog("sess_1")
        log.append(_assistant_with_call())
        log.append(
            ToolResult(
                meta=MessageMeta(
                    id="msg_r2",  # type: ignore[arg-type]
                    session_id="sess_1",
                    run_id="run_1",
                    turn_id=1,
                    created_at="2026-09-23T00:00:00.000000Z",
                ),
                tool_call_id=ToolCallId("call_1"),
                status=ToolResultStatus.COMPLETED,
                content="fine",
            )
        )
        data = log.serialize()
        payload = data["messages"][-1]
        assert payload["retryable"] is None
        assert payload["exit_code"] is None


def _assistant_with_call():
    from coding_agent.domain.messages import AssistantMessage

    return AssistantMessage(
        meta=MessageMeta(
            id="msg_a1",  # type: ignore[arg-type]
            session_id="sess_1",
            run_id="run_1",
            turn_id=1,
            created_at="2026-09-23T00:00:00.000000Z",
        ),
        content="",
        stop_reason=StopReason.TOOL_CALLS,
        tool_calls=(ToolCall(id=ToolCallId("call_1"), name="bash", arguments={"command": "x"}, ordinal=0),),
    )
