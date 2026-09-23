"""阶段 02 单测：消息系统的序列化、配对与拒绝规则。"""

from dataclasses import FrozenInstanceError

import pytest

from coding_agent.domain import messages as m
from coding_agent.domain.errors import MessageValidationError, SchemaVersionError
from coding_agent.domain.state import StopReason

SESSION = "sess_test"
RUN = "run_test"
TS = "2026-09-23T00:00:00.000000Z"


def meta(msg_id: str, turn: int = 1) -> m.MessageMeta:
    return m.MessageMeta(id=m.MessageId(msg_id), session_id=SESSION, run_id=RUN, turn_id=turn, created_at=TS)


def call(call_id: str, ordinal: int, name: str = "read", args: dict | None = None) -> m.ToolCall:
    return m.ToolCall(id=m.ToolCallId(call_id), name=name, arguments=args or {"path": "a.py"}, ordinal=ordinal)


def result(result_id: str, call_id: str, content: str = "ok") -> m.ToolResult:
    return m.ToolResult(
        meta=meta(result_id),
        tool_call_id=m.ToolCallId(call_id),
        status=m.ToolResultStatus.COMPLETED,
        content=content,
    )


def populated_log() -> m.MessageLog:
    """system → user → assistant(2 calls) → result(c1) → result(c2) → assistant(final)。"""
    log = m.MessageLog(SESSION)
    log.append(m.SystemMessage(meta=meta("msg_sys"), content="system prompt"))
    log.append(m.UserMessage(meta=meta("msg_u1"), content="fix the bug"))
    log.append(
        m.AssistantMessage(
            meta=meta("msg_a1"),
            content="",
            stop_reason=StopReason.TOOL_CALLS,
            tool_calls=(call("call_1", 0), call("call_2", 1, name="bash", args={"command": "pytest"})),
        )
    )
    log.append(result("msg_r1", "call_1"))
    log.append(result("msg_r2", "call_2", content="exit 0"))
    log.append(
        m.AssistantMessage(meta=meta("msg_a2", turn=2), content="done", stop_reason=StopReason.END_TURN)
    )
    return log


class TestSerialization:
    def test_round_trip_preserves_messages_and_pairing(self) -> None:
        log = populated_log()
        restored = m.MessageLog.deserialize(log.serialize())

        assert list(restored.messages) == list(log.messages)
        assert restored.session_id == SESSION
        assert restored.serialize() == log.serialize()
        assert restored.find_tool_call("call_1") is not None
        assert restored.find_result("call_1") is not None
        assert restored.find_result("call_1").tool_call_id == "call_1"

    def test_call_order_reconstructable(self) -> None:
        restored = m.MessageLog.deserialize(populated_log().serialize())
        order: list[str] = []
        for message in restored.messages:
            if isinstance(message, m.AssistantMessage):
                order.extend(str(c.id) for c in sorted(message.tool_calls, key=lambda c: c.ordinal))
        assert order == ["call_1", "call_2"]

    def test_unknown_log_version_rejected(self) -> None:
        data = populated_log().serialize()
        data["schema_version"] = 2
        with pytest.raises(SchemaVersionError) as excinfo:
            m.MessageLog.deserialize(data)
        assert excinfo.value.code == "schema_version_unsupported"

    def test_unknown_meta_version_rejected(self) -> None:
        data = populated_log().serialize()
        data["messages"][0]["meta"]["schema_version"] = 99
        with pytest.raises(SchemaVersionError):
            m.MessageLog.deserialize(data)

    def test_unknown_message_type_rejected(self) -> None:
        data = populated_log().serialize()
        data["messages"][0]["type"] = "audio"
        with pytest.raises(MessageValidationError) as excinfo:
            m.MessageLog.deserialize(data)
        assert excinfo.value.code == "unknown_message_type"

    def test_missing_required_field_rejected(self) -> None:
        data = populated_log().serialize()
        del data["messages"][0]["content"]
        with pytest.raises(MessageValidationError) as excinfo:
            m.MessageLog.deserialize(data)
        assert excinfo.value.code == "malformed_message"

    def test_invalid_timestamp_rejected(self) -> None:
        data = populated_log().serialize()
        data["messages"][0]["meta"]["created_at"] = "2026-09-23 00:00:00"
        with pytest.raises(MessageValidationError) as excinfo:
            m.MessageLog.deserialize(data)
        assert excinfo.value.code == "invalid_timestamp"


class TestAppendValidation:
    def test_duplicate_message_id_rejected(self) -> None:
        log = m.MessageLog(SESSION)
        log.append(m.UserMessage(meta=meta("msg_1"), content="a"))
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(m.UserMessage(meta=meta("msg_1"), content="b"))
        assert excinfo.value.code == "duplicate_message_id"
        assert len(log) == 1  # 拒绝后原记录不变

    def test_duplicate_tool_call_id_rejected(self) -> None:
        log = populated_log()
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(
                m.AssistantMessage(
                    meta=meta("msg_a3", turn=2),
                    content="",
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(call("call_1", 0),),
                )
            )
        assert excinfo.value.code == "duplicate_tool_call_id"

    def test_orphan_tool_result_rejected(self) -> None:
        log = m.MessageLog(SESSION)
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(result("msg_r1", "call_missing"))
        assert excinfo.value.code == "orphan_tool_result"

    def test_result_before_its_assistant_rejected(self) -> None:
        log = m.MessageLog(SESSION)
        log.append(m.UserMessage(meta=meta("msg_u1"), content="go"))
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(result("msg_r1", "call_1"))
        assert excinfo.value.code == "orphan_tool_result"

    def test_duplicate_tool_result_rejected(self) -> None:
        log = populated_log()
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(result("msg_r3", "call_1", content="again"))
        assert excinfo.value.code == "duplicate_tool_result"

    def test_non_consecutive_ordinals_rejected(self) -> None:
        log = m.MessageLog(SESSION)
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(
                m.AssistantMessage(
                    meta=meta("msg_a1"),
                    content="",
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(call("call_1", 0), call("call_2", 2)),
                )
            )
        assert excinfo.value.code == "invalid_tool_call_ordinal"

    def test_non_json_arguments_rejected(self) -> None:
        log = m.MessageLog(SESSION)
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(
                m.AssistantMessage(
                    meta=meta("msg_a1"),
                    content="",
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(call("call_1", 0, args={"path": object()}),),
                )
            )
        assert excinfo.value.code == "arguments_not_json"

    def test_session_mismatch_rejected(self) -> None:
        log = m.MessageLog(SESSION)
        other = m.MessageMeta(
            id=m.MessageId("msg_x"), session_id="other", run_id=RUN, turn_id=1, created_at=TS
        )
        with pytest.raises(MessageValidationError) as excinfo:
            log.append(m.UserMessage(meta=other, content="a"))
        assert excinfo.value.code == "session_mismatch"

    def test_messages_are_frozen(self) -> None:
        message = m.UserMessage(meta=meta("msg_1"), content="a")
        with pytest.raises(FrozenInstanceError):
            message.content = "mutated"  # type: ignore[misc]


class TestHelpers:
    def test_new_ids_unique_and_prefixed(self) -> None:
        ids = {m.new_message_id() for _ in range(50)}
        assert len(ids) == 50
        assert all(value.startswith("msg_") for value in ids)
        assert str(m.new_tool_call_id()).startswith("call_")

    def test_utc_now_is_rfc3339_z(self) -> None:
        value = m.utc_now_rfc3339()
        assert value.endswith("Z")
        assert m._validate_utc_timestamp(value, "test") == value

    def test_format_utc_rejects_naive_datetime(self) -> None:
        from datetime import datetime

        with pytest.raises(ValueError):
            m.format_utc(datetime(2026, 9, 23))
