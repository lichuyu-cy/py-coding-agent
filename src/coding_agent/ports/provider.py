"""Provider 端口：隔离模型 API 通信细节，并以可重放的方式复现控制轨迹。

- 本模块只定义协议与共享数据结构；实现位于 `coding_agent.providers.*`。
- `ProviderMessage` 是构建模型请求时的临时投影，不是持久事实（持久事实见 domain.messages）。
- `ModelDelta` 只是类型定义；流式聚合与分片工具参数的运行时处理在阶段 17。
- Provider 不得执行工具、不得修改历史、不得 import AgentLoop。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import ToolCall, new_id
from coding_agent.domain.state import StopReason


class ProviderErrorKind(StrEnum):
    """Provider 故障分类；有限退避由 Runtime 决定，且不得重放已生效的工具。"""

    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class ProviderError(HarnessError):
    def __init__(self, kind: ProviderErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ProviderMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ProviderToolCallPart:
    """助手投影消息中的工具调用片段（id/name/arguments 均完整）。"""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    """请求上下文中的单条投影消息。

    - system/user：使用 content；
    - assistant：content 与（或）tool_calls；
    - tool：tool_call_id 必填，content 为观察文本。
    """

    role: ProviderMessageRole
    content: str = ""
    tool_calls: tuple[ProviderToolCallPart, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """传给模型的工具声明。"""

    name: str
    description: str
    json_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """token 用量；未知项为 None，不得记为 0。"""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ProviderMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()
    model: str = "default"
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    request_id: str = field(default_factory=lambda: new_id("req"))


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """已归一、可验证的完整响应（只有它可以进入持久历史）。"""

    content: str
    stop_reason: StopReason
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage | None = None
    response_id: str = field(default_factory=lambda: new_id("resp"))


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    call: ToolCall


@dataclass(frozen=True, slots=True)
class UsageDelta:
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class StopDelta:
    stop_reason: StopReason


ModelDelta = TextDelta | ToolCallDelta | UsageDelta | StopDelta


class CancelSignal(Protocol):
    """协作式取消信号（具体实现见阶段 09 的 CancelToken）。"""

    @property
    def is_cancelled(self) -> bool: ...


class Provider(Protocol):
    """LLM Provider 协议。

    替换真实 Provider 与 Fake 不改变 Loop 的调用方式；
    complete 返回完整响应，stream 只产出增量。
    """

    async def complete(
        self, request: ModelRequest, cancel: CancelSignal | None = None
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, cancel: CancelSignal | None = None
    ) -> AsyncIterator[ModelDelta]: ...


def normalize_stop_reason(value: Any) -> StopReason:
    """把外部 stop reason 归一到 StopReason；未知值属于无效响应。"""
    try:
        return StopReason(value)
    except ValueError as exc:
        raise ProviderError(
            ProviderErrorKind.INVALID_RESPONSE, f"unknown stop_reason {value!r}"
        ) from exc


def validate_model_request(request: ModelRequest) -> None:
    """请求的最小验证：工具声明合法且不重名。"""
    names: set[str] = set()
    for tool in request.tools:
        if not tool.name:
            raise ProviderError(ProviderErrorKind.INVALID_REQUEST, "tool definition name must not be empty")
        if tool.name in names:
            raise ProviderError(ProviderErrorKind.INVALID_REQUEST, f"duplicate tool definition {tool.name!r}")
        if not isinstance(tool.json_schema, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST, f"tool {tool.name!r} json_schema must be an object"
            )
        names.add(tool.name)


def validate_model_response(response: ModelResponse) -> None:
    """stop reason 与 tool calls 的兼容性及结构校验；冲突属于 ProviderError。"""
    stop_reason = normalize_stop_reason(response.stop_reason)
    if response.tool_calls:
        if stop_reason is not StopReason.TOOL_CALLS:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                f"tool calls present but stop_reason is {stop_reason.value}",
            )
        ordinals = [call.ordinal for call in response.tool_calls]
        if sorted(ordinals) != list(range(len(response.tool_calls))):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                f"tool call ordinals must be consecutive from 0, got {ordinals}",
            )
        ids = [str(call.id) for call in response.tool_calls]
        if len(set(ids)) != len(ids):
            raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "duplicate tool call ids in response")
        for call in response.tool_calls:
            if not call.name:
                raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "tool call name must not be empty")
            try:
                json.dumps(dict(call.arguments))
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE,
                    f"tool call {call.id!r} arguments are not valid JSON",
                ) from exc
    elif stop_reason is StopReason.TOOL_CALLS:
        raise ProviderError(
            ProviderErrorKind.INVALID_RESPONSE, "stop_reason is tool_calls but no tool calls present"
        )
