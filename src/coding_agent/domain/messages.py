"""消息系统：用户、助手、工具调用与结果的可追踪事实记录。

- 事实记录（本模块）与 Provider 临时投影分离；本模块不构造 Provider 专属请求。
- 半截 tool arguments 不得作为已提交消息；只有完整、可验证的助手响应才能构造 AssistantMessage。
- 追加验证集中在 MessageLog.append：ID 唯一、助手内 ordinal 连续、结果只能指向已有 call、
  每个 call 至多一个最终结果；不静默修复输入。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, NewType, Union

from coding_agent.domain.errors import MessageValidationError, SchemaVersionError
from coding_agent.domain.state import StopReason

MESSAGE_SCHEMA_VERSION = 1

MessageId = NewType("MessageId", str)
ToolCallId = NewType("ToolCallId", str)


def new_id(prefix: str) -> str:
    """生成不可复用字符串 ID（uuid4）。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def new_message_id() -> MessageId:
    return MessageId(new_id("msg"))


def new_tool_call_id() -> ToolCallId:
    return ToolCallId(new_id("call"))


def utc_now_rfc3339() -> str:
    """当前 UTC 时间的 RFC3339 字符串（含微秒，Z 后缀）。"""
    return format_utc(datetime.now(timezone.utc))


def format_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_mapping(data: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise MessageValidationError("malformed_message", f"{what} must be a JSON object")
    return data


def _require(data: Mapping[str, Any], key: str, what: str) -> Any:
    if key not in data:
        raise MessageValidationError("malformed_message", f"{what} is missing required field {key!r}")
    return data[key]


def _require_str(data: Mapping[str, Any], key: str, what: str) -> str:
    value = _require(data, key, what)
    if not isinstance(value, str) or not value:
        raise MessageValidationError("malformed_message", f"{what}.{key} must be a non-empty string")
    return value


def _require_text(data: Mapping[str, Any], key: str, what: str) -> str:
    """内容类字段：必须是字符串，但允许空串（如纯工具调用的助手回复）。"""
    value = _require(data, key, what)
    if not isinstance(value, str):
        raise MessageValidationError("malformed_message", f"{what}.{key} must be a string")
    return value


def _validate_utc_timestamp(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise MessageValidationError("invalid_timestamp", f"{what} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MessageValidationError("invalid_timestamp", f"{what} is not RFC3339: {value!r}") from exc
    if parsed.tzinfo is None:
        raise MessageValidationError("invalid_timestamp", f"{what} must carry a UTC offset")
    return value


def _require_schema_version(value: Any, what: str) -> int:
    if value != MESSAGE_SCHEMA_VERSION:
        raise SchemaVersionError(f"{what}: unsupported schema_version {value!r}")
    return MESSAGE_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class MessageMeta:
    """所有持久消息的公共元数据。"""

    id: MessageId
    session_id: str
    run_id: str
    turn_id: int
    created_at: str
    schema_version: int = MESSAGE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MessageMeta":
        data = _require_mapping(data, "meta")
        _require_schema_version(_require(data, "schema_version", "meta"), "meta")
        turn_id = _require(data, "turn_id", "meta")
        if not isinstance(turn_id, int) or isinstance(turn_id, bool) or turn_id < 0:
            raise MessageValidationError("malformed_message", "meta.turn_id must be a non-negative int")
        return cls(
            id=MessageId(_require_str(data, "id", "meta")),
            session_id=_require_str(data, "session_id", "meta"),
            run_id=_require_str(data, "run_id", "meta"),
            turn_id=turn_id,
            created_at=_validate_utc_timestamp(_require(data, "created_at", "meta"), "meta.created_at"),
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    """助手提出的一个完整工具调用（arguments 为完整 JSON 对象）。"""

    id: ToolCallId
    name: str
    arguments: Mapping[str, Any]
    ordinal: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "arguments": dict(self.arguments),
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolCall":
        data = _require_mapping(data, "tool_call")
        ordinal = _require(data, "ordinal", "tool_call")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise MessageValidationError("malformed_message", "tool_call.ordinal must be a non-negative int")
        arguments = _require(data, "arguments", "tool_call")
        if not isinstance(arguments, Mapping):
            raise MessageValidationError("malformed_message", "tool_call.arguments must be a JSON object")
        return cls(
            id=ToolCallId(_require_str(data, "id", "tool_call")),
            name=_require_str(data, "name", "tool_call"),
            arguments=dict(arguments),
            ordinal=ordinal,
        )


class ToolResultStatus(StrEnum):
    """工具结果的终态分类。"""

    COMPLETED = "completed"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class SystemMessage:
    meta: MessageMeta
    content: str

    @property
    def message_type(self) -> str:
        return "system"


@dataclass(frozen=True, slots=True)
class UserMessage:
    meta: MessageMeta
    content: str

    @property
    def message_type(self) -> str:
        return "user"


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    meta: MessageMeta
    content: str
    stop_reason: StopReason
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def message_type(self) -> str:
        return "assistant"


@dataclass(frozen=True, slots=True)
class ToolResult:
    meta: MessageMeta
    tool_call_id: ToolCallId
    status: ToolResultStatus
    content: str
    artifact_ref: str | None = None
    error_kind: str | None = None
    retryable: bool | None = None
    exit_code: int | None = None

    @property
    def message_type(self) -> str:
        return "tool_result"


Message = Union[SystemMessage, UserMessage, AssistantMessage, ToolResult]


def message_to_dict(message: Message) -> dict[str, Any]:
    data: dict[str, Any] = {"type": message.message_type, "meta": message.meta.to_dict()}
    if isinstance(message, (SystemMessage, UserMessage)):
        data["content"] = message.content
    elif isinstance(message, AssistantMessage):
        data["content"] = message.content
        data["stop_reason"] = str(message.stop_reason)
        data["tool_calls"] = [call.to_dict() for call in message.tool_calls]
    elif isinstance(message, ToolResult):
        data["tool_call_id"] = str(message.tool_call_id)
        data["status"] = str(message.status)
        data["content"] = message.content
        data["artifact_ref"] = message.artifact_ref
        data["error_kind"] = message.error_kind
        data["retryable"] = message.retryable
        data["exit_code"] = message.exit_code
    else:  # pragma: no cover - 防御未预期类型
        raise MessageValidationError("unknown_message_type", f"unsupported message type {type(message)!r}")
    return data


def _parse_stop_reason(value: Any) -> StopReason:
    try:
        return StopReason(value)
    except ValueError as exc:
        raise MessageValidationError("malformed_message", f"unknown stop_reason {value!r}") from exc


def _parse_tool_result_status(value: Any) -> ToolResultStatus:
    try:
        return ToolResultStatus(value)
    except ValueError as exc:
        raise MessageValidationError("malformed_message", f"unknown tool result status {value!r}") from exc


def message_from_dict(data: Mapping[str, Any]) -> Message:
    data = _require_mapping(data, "message")
    message_type = _require_str(data, "type", "message")
    meta = MessageMeta.from_dict(_require(data, "meta", "message"))
    if message_type == "system":
        return SystemMessage(meta=meta, content=_require_text(data, "content", "system message"))
    if message_type == "user":
        return UserMessage(meta=meta, content=_require_text(data, "content", "user message"))
    if message_type == "assistant":
        raw_calls = _require(data, "tool_calls", "assistant message")
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
            raise MessageValidationError("malformed_message", "assistant.tool_calls must be a list")
        calls = tuple(ToolCall.from_dict(call) for call in raw_calls)
        return AssistantMessage(
            meta=meta,
            content=_require_text(data, "content", "assistant message"),
            stop_reason=_parse_stop_reason(_require(data, "stop_reason", "assistant message")),
            tool_calls=calls,
        )
    if message_type == "tool_result":
        artifact_ref = data.get("artifact_ref")
        error_kind = data.get("error_kind")
        retryable = data.get("retryable")
        exit_code = data.get("exit_code")
        if artifact_ref is not None and not isinstance(artifact_ref, str):
            raise MessageValidationError("malformed_message", "tool_result.artifact_ref must be a string or null")
        if error_kind is not None and not isinstance(error_kind, str):
            raise MessageValidationError("malformed_message", "tool_result.error_kind must be a string or null")
        if retryable is not None and not isinstance(retryable, bool):
            raise MessageValidationError("malformed_message", "tool_result.retryable must be a boolean or null")
        if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
            raise MessageValidationError("malformed_message", "tool_result.exit_code must be an int or null")
        return ToolResult(
            meta=meta,
            tool_call_id=ToolCallId(_require_str(data, "tool_call_id", "tool_result")),
            status=_parse_tool_result_status(_require(data, "status", "tool_result")),
            content=_require_text(data, "content", "tool_result"),
            artifact_ref=artifact_ref,
            error_kind=error_kind,
            retryable=retryable,
            exit_code=exit_code,
        )
    raise MessageValidationError("unknown_message_type", f"unsupported message type {message_type!r}")


class MessageLog:
    """按会话追加的事实消息记录（append-only）。

    输入：待追加的消息；输出：经过验证的记录与查询接口。
    调用方：Runtime 写入；Context、Session、Compaction 只读消费。
    on_append 可选：追加成功后的同步观察者（阶段 19 用于持久化；异常向上传播）。
    """

    def __init__(
        self, session_id: str, *, on_append: Callable[["Message"], None] | None = None
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise MessageValidationError("malformed_message", "session_id must be a non-empty string")
        self._session_id = session_id
        self._messages: list[Message] = []
        self._message_ids: set[str] = set()
        self._tool_calls: dict[str, ToolCall] = {}
        self._results: dict[str, ToolResult] = {}
        self.on_append = on_append

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def messages(self) -> Sequence[Message]:
        """按追加顺序返回只读消息序列。"""
        return tuple(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def append(self, message: Message) -> None:
        """验证并追加一条消息；任何违规都拒绝且不修改原消息。"""
        self._validate(message)
        self._messages.append(message)
        self._message_ids.add(str(message.meta.id))
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                self._tool_calls[str(call.id)] = call
        elif isinstance(message, ToolResult):
            self._results[str(message.tool_call_id)] = message
        if self.on_append is not None:
            self.on_append(message)

    def _validate(self, message: Message) -> None:
        if not isinstance(message, (SystemMessage, UserMessage, AssistantMessage, ToolResult)):
            raise MessageValidationError("unknown_message_type", f"unsupported message type {type(message)!r}")
        meta = message.meta
        if not isinstance(meta, MessageMeta):
            raise MessageValidationError("malformed_message", "message.meta must be MessageMeta")
        if meta.session_id != self._session_id:
            raise MessageValidationError(
                "session_mismatch",
                f"message belongs to session {meta.session_id!r}, log is {self._session_id!r}",
            )
        if str(meta.id) in self._message_ids:
            raise MessageValidationError("duplicate_message_id", f"message id {meta.id!r} already exists")

        if isinstance(message, AssistantMessage):
            self._validate_tool_calls(message)

        if isinstance(message, ToolResult):
            call_id = str(message.tool_call_id)
            if call_id not in self._tool_calls:
                raise MessageValidationError(
                    "orphan_tool_result",
                    f"tool result references unknown tool call {message.tool_call_id!r}",
                )
            if call_id in self._results:
                raise MessageValidationError(
                    "duplicate_tool_result",
                    f"tool call {message.tool_call_id!r} already has a final result",
                )
            if not isinstance(message.status, ToolResultStatus):
                raise MessageValidationError("malformed_message", "tool result status must be ToolResultStatus")
            if message.retryable is not None and not isinstance(message.retryable, bool):
                raise MessageValidationError("malformed_message", "tool result retryable must be a boolean or None")
            if message.exit_code is not None and (
                not isinstance(message.exit_code, int) or isinstance(message.exit_code, bool)
            ):
                raise MessageValidationError("malformed_message", "tool result exit_code must be an int or None")

    def _validate_tool_calls(self, message: AssistantMessage) -> None:
        ordinals = [call.ordinal for call in message.tool_calls]
        if sorted(ordinals) != list(range(len(message.tool_calls))):
            raise MessageValidationError(
                "invalid_tool_call_ordinal",
                f"assistant tool call ordinals must be consecutive from 0, got {ordinals}",
            )
        seen: set[str] = set()
        for call in message.tool_calls:
            call_id = str(call.id)
            if call_id in seen:
                raise MessageValidationError("duplicate_tool_call_id", f"duplicate call id {call.id!r} in message")
            if call_id in self._tool_calls:
                raise MessageValidationError("duplicate_tool_call_id", f"tool call id {call.id!r} already exists")
            if not call.name or not isinstance(call.name, str):
                raise MessageValidationError("malformed_message", "tool call name must be a non-empty string")
            try:
                json.dumps(dict(call.arguments))
            except (TypeError, ValueError) as exc:
                raise MessageValidationError(
                    "arguments_not_json", f"tool call {call.id!r} arguments are not JSON-serializable"
                ) from exc
            seen.add(call_id)

    def find_tool_call(self, call_id: str) -> ToolCall | None:
        return self._tool_calls.get(str(call_id))

    def find_result(self, call_id: str) -> ToolResult | None:
        """返回某个工具调用的最终结果（未完成则为 None）。"""
        return self._results.get(str(call_id))

    def serialize(self) -> dict[str, Any]:
        return {
            "schema_version": MESSAGE_SCHEMA_VERSION,
            "session_id": self._session_id,
            "messages": [message_to_dict(message) for message in self._messages],
        }

    @classmethod
    def deserialize(cls, data: Mapping[str, Any]) -> "MessageLog":
        data = _require_mapping(data, "message log")
        _require_schema_version(_require(data, "schema_version", "message log"), "message log")
        session_id = _require_str(data, "session_id", "message log")
        raw_messages = _require(data, "messages", "message log")
        if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
            raise MessageValidationError("malformed_message", "message log messages must be a list")
        log = cls(session_id)
        for item in raw_messages:
            log.append(message_from_dict(item))
        return log
