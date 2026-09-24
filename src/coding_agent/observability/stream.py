"""Streaming：把 Provider 增量聚合为完整、可验证的响应。

- `ToolCallAccumulator`：按 fragment_seq 排序拼装参数分片；重复 seq 幂等去重；
  完成对象（ToolCallDelta）与分片可混用；全部拼装完成才解析 JSON；
- `AgentStreamAggregator`：文本按抵达顺序拼接；捕获 usage 与 stop reason；
  无 StopDelta 的流视为中断（InterruptedStreamError），不产出半段调用；
- 最终响应经 `validate_model_response` 校验（stop reason 与 tool calls 兼容性），
  与 complete() 路径共享同一归一契约（同脚本两路径最终产物一致）。

边界：Provider Stream 是原始片段；Agent Stream 是语义事件（EventBus 转发）；
SSE 编码属于阶段 18；重复 delta 由 fragment_seq/事件序号去重。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import ToolCall, ToolCallId
from coding_agent.domain.state import StopReason
from coding_agent.ports.provider import (
    ModelDelta,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    StopDelta,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
    ToolCallFragmentDelta,
    UsageDelta,
    validate_model_response,
)

__all__ = ["AgentStreamAggregator", "InterruptedStreamError", "StreamAssemblyError", "ToolCallAccumulator"]


class StreamAssemblyError(HarnessError):
    """流内容无法组装为合法响应（损坏 JSON、分片缺失等）。"""


class InterruptedStreamError(ProviderError):
    """流在 StopDelta 之前结束（断连）：不得把半段调用送入工具。"""

    def __init__(self, message: str = "provider stream was interrupted before completion") -> None:
        super().__init__(ProviderErrorKind.INVALID_RESPONSE, message)


@dataclass
class _FragmentBuffer:
    name: str | None = None
    fragments: dict[int, str] | None = None
    complete: ToolCall | None = None

    def __post_init__(self) -> None:
        if self.fragments is None:
            self.fragments = {}


class ToolCallAccumulator:
    """工具调用的增量拼装器（顺序无关、重复幂等）。"""

    def __init__(self) -> None:
        self._buffers: dict[str, _FragmentBuffer] = {}
        self._order: list[str] = []

    def _buffer(self, call_id: str) -> _FragmentBuffer:
        buffer = self._buffers.get(call_id)
        if buffer is None:
            buffer = _FragmentBuffer()
            self._buffers[call_id] = buffer
            self._order.append(call_id)
        return buffer

    def add_fragment(self, delta: ToolCallFragmentDelta) -> None:
        buffer = self._buffer(delta.call_id)
        if buffer.complete is not None:
            return  # 已有完整对象：分片忽略（幂等）
        if delta.name:
            buffer.name = delta.name
        assert buffer.fragments is not None
        buffer.fragments.setdefault(delta.fragment_seq, delta.arguments_fragment)  # 重复 seq 去重

    def add_complete(self, call: ToolCall) -> None:
        buffer = self._buffer(str(call.id))
        if buffer.complete is None:
            buffer.complete = call  # 重复完整对象去重（先者优先）

    @property
    def call_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    def finalize(self) -> tuple[ToolCall, ...]:
        """按登记顺序输出完整调用（ordinal 重排 0..n-1）；任何不完整/损坏都显式报错。"""
        calls: list[ToolCall] = []
        for ordinal, call_id in enumerate(self._order):
            buffer = self._buffers[call_id]
            if buffer.complete is not None:
                call = buffer.complete
                calls.append(
                    ToolCall(id=call.id, name=call.name, arguments=dict(call.arguments), ordinal=ordinal)
                )
                continue
            if not buffer.name:
                raise StreamAssemblyError(f"tool call {call_id!r} is missing its name")
            assert buffer.fragments is not None
            assembled = "".join(
                buffer.fragments[seq] for seq in sorted(buffer.fragments.keys())
            )
            if not assembled:
                raise StreamAssemblyError(
                    f"tool call {call_id!r} has no complete arguments (interrupted fragments)"
                )
            try:
                arguments = json.loads(assembled)
            except json.JSONDecodeError as exc:
                raise StreamAssemblyError(
                    f"tool call {call_id!r} carries corrupted JSON arguments: {exc}"
                ) from exc
            if not isinstance(arguments, dict):
                raise StreamAssemblyError(
                    f"tool call {call_id!r} arguments must be a JSON object"
                )
            calls.append(
                ToolCall(id=ToolCallId(call_id), name=buffer.name, arguments=arguments, ordinal=ordinal)
            )
        return tuple(calls)


class AgentStreamAggregator:
    """Provider 增量 → 完整 ModelResponse（与 complete() 同一校验契约）。"""

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._tool_calls = ToolCallAccumulator()
        self._usage: TokenUsage | None = None
        self._stop_reason: StopReason | None = None

    def aggregate(self, delta: ModelDelta) -> None:
        if isinstance(delta, TextDelta):
            self._text_parts.append(delta.text)
        elif isinstance(delta, ToolCallDelta):
            self._tool_calls.add_complete(delta.call)
        elif isinstance(delta, ToolCallFragmentDelta):
            self._tool_calls.add_fragment(delta)
        elif isinstance(delta, UsageDelta):
            self._usage = delta.usage
        elif isinstance(delta, StopDelta):
            self._stop_reason = delta.stop_reason

    @property
    def interrupted(self) -> bool:
        return self._stop_reason is None

    def finalize_response(self) -> ModelResponse:
        """产出完整响应；流未以 StopDelta 结束 → InterruptedStreamError（不入历史/不进工具）。"""
        if self._stop_reason is None:
            raise InterruptedStreamError()
        try:
            tool_calls = self._tool_calls.finalize()
        except StreamAssemblyError as exc:
            raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, str(exc)) from exc
        response = ModelResponse(
            content="".join(self._text_parts),
            stop_reason=self._stop_reason,
            tool_calls=tool_calls,
            usage=self._usage,
        )
        validate_model_response(response)
        return response
