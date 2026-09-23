"""Fake Provider：以脚本复现确定性的模型输出、故障与延时。

用于本地验证与测试，不需要网络和真实密钥：
- 固定脚本轨迹：按顺序消费 `FakeResponse` / `FakeFault` 步骤；
- 故障与延时：速率限制、超时、无效响应、损坏 JSON、可配置等待；
- 取消：延迟期间与请求开始前检查取消信号，取消归类为 ProviderError(CANCELLED)；
- 脚本被多消费一次即视为测试脚本错误，立即失败（不静默返回默认值）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from coding_agent.domain.messages import ToolCall, ToolCallId, new_tool_call_id
from coding_agent.domain.state import StopReason
from coding_agent.ports.provider import (
    CancelSignal,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    StopDelta,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
    UsageDelta,
    validate_model_request,
    validate_model_response,
)

_CANCEL_POLL_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class ScriptedToolCall:
    """脚本化工具调用；arguments 与 arguments_json 二选一。

    arguments_json 用于模拟真实 Provider 的原始 JSON 分片/损坏场景。
    """

    name: str
    arguments: Mapping[str, Any] | None = None
    arguments_json: str | None = None
    id: str | None = None  # 缺省时由 Fake 生成唯一 ID


@dataclass(frozen=True, slots=True)
class FakeResponse:
    """脚本化成功响应。content_chunks 供 stream() 切分文本。"""

    content: str = ""
    stop_reason: StopReason = StopReason.END_TURN
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    usage: TokenUsage | None = None
    delay_seconds: float = 0.0
    content_chunks: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class FakeFault:
    """脚本化故障步骤。"""

    kind: ProviderErrorKind
    message: str = "scripted fault"
    delay_seconds: float = 0.0


FakeStep = FakeResponse | FakeFault


class FakeScriptExhaustedError(ProviderError):
    """脚本被多消费：属于测试脚本缺陷，必须立即失败。"""

    def __init__(self, message: str) -> None:
        super().__init__(ProviderErrorKind.INVALID_RESPONSE, message)


class FakeProvider:
    """按脚本顺序返回响应的 Provider 实现。"""

    def __init__(self, steps: Sequence[FakeStep], *, model: str = "fake-model") -> None:
        self._steps: tuple[FakeStep, ...] = tuple(steps)
        self._cursor = 0
        self._model = model
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """已接收的请求（按调用顺序），供断言使用。"""
        return tuple(self._requests)

    @property
    def call_count(self) -> int:
        return len(self._requests)

    @property
    def remaining_steps(self) -> int:
        return len(self._steps) - self._cursor

    async def complete(
        self, request: ModelRequest, cancel: CancelSignal | None = None
    ) -> ModelResponse:
        self._requests.append(request)
        validate_model_request(request)
        step = self._pop_step()
        await self._wait(step.delay_seconds, cancel)
        if isinstance(step, FakeFault):
            raise ProviderError(step.kind, step.message)
        return self._build_response(step)

    async def stream(
        self, request: ModelRequest, cancel: CancelSignal | None = None
    ) -> AsyncIterator[ModelDelta]:
        """按脚本产出增量（阶段 03 仅提供协议骨架与 Fake 的切分能力）。"""
        self._requests.append(request)
        validate_model_request(request)
        step = self._pop_step()
        await self._wait(step.delay_seconds, cancel)
        if isinstance(step, FakeFault):
            raise ProviderError(step.kind, step.message)
        response = self._build_response(step)
        chunks: tuple[str, ...]
        if step.content_chunks is not None:
            chunks = step.content_chunks
        elif response.content:
            chunks = (response.content,)
        else:
            chunks = ()
        for chunk in chunks:
            yield TextDelta(chunk)
        for call in response.tool_calls:
            yield ToolCallDelta(call)
        if response.usage is not None:
            yield UsageDelta(response.usage)
        yield StopDelta(response.stop_reason)

    def _pop_step(self) -> FakeStep:
        if self._cursor >= len(self._steps):
            raise FakeScriptExhaustedError(
                f"fake script exhausted: {self._cursor} steps consumed, no more steps defined"
            )
        step = self._steps[self._cursor]
        self._cursor += 1
        return step

    async def _wait(self, seconds: float, cancel: CancelSignal | None) -> None:
        self._raise_if_cancelled(cancel)
        if seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            self._raise_if_cancelled(cancel)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, _CANCEL_POLL_SECONDS))

    @staticmethod
    def _raise_if_cancelled(cancel: CancelSignal | None) -> None:
        if cancel is not None and cancel.is_cancelled:
            raise ProviderError(ProviderErrorKind.CANCELLED, "request cancelled before completion")

    def _build_response(self, step: FakeResponse) -> ModelResponse:
        calls: list[ToolCall] = []
        for ordinal, scripted in enumerate(step.tool_calls):
            calls.append(self._build_tool_call(scripted, ordinal))
        response = ModelResponse(
            content=step.content,
            stop_reason=step.stop_reason,
            tool_calls=tuple(calls),
            usage=step.usage,
        )
        validate_model_response(response)
        return response

    @staticmethod
    def _build_tool_call(scripted: ScriptedToolCall, ordinal: int) -> ToolCall:
        call_id = scripted.id if scripted.id is not None else str(new_tool_call_id())
        if scripted.arguments_json is not None:
            try:
                parsed: Any = json.loads(scripted.arguments_json)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE,
                    f"tool call {scripted.name!r} carries corrupted JSON arguments",
                ) from exc
            if not isinstance(parsed, Mapping):
                raise ProviderError(
                    ProviderErrorKind.INVALID_RESPONSE,
                    f"tool call {scripted.name!r} arguments must be a JSON object",
                )
            arguments: Mapping[str, Any] = dict(parsed)
        else:
            arguments = dict(scripted.arguments or {})
        return ToolCall(id=ToolCallId(call_id), name=scripted.name, arguments=arguments, ordinal=ordinal)
