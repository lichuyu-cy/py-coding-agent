"""阶段 03 单测：Fake Provider 的脚本轨迹、故障、延时与取消。"""

import asyncio
import time

import pytest

from coding_agent.domain.state import StopReason
from coding_agent.ports.provider import (
    ModelRequest,
    ProviderError,
    ProviderErrorKind,
    ProviderMessage,
    ProviderMessageRole,
    StopDelta,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
    ToolDefinition,
    UsageDelta,
)
from coding_agent.providers.fake import (
    FakeFault,
    FakeProvider,
    FakeResponse,
    FakeScriptExhaustedError,
    ScriptedToolCall,
)


class Token:
    """测试用取消信号。"""

    def __init__(self) -> None:
        self.cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled


def make_request(tools: tuple[ToolDefinition, ...] = ()) -> ModelRequest:
    return ModelRequest(
        messages=(ProviderMessage(role=ProviderMessageRole.USER, content="hello"),),
        tools=tools,
        model="fake-model",
    )


class TestScriptedResponses:
    async def test_final_response_and_request_recording(self) -> None:
        provider = FakeProvider(
            [FakeResponse(content="done", stop_reason=StopReason.END_TURN, usage=TokenUsage(10, 4))]
        )
        response = await provider.complete(make_request())

        assert response.content == "done"
        assert response.stop_reason is StopReason.END_TURN
        assert response.tool_calls == ()
        assert response.usage == TokenUsage(10, 4)
        assert provider.call_count == 1
        assert provider.requests[0].request_id.startswith("req_")

    async def test_tool_calls_normalized_with_ordinals(self) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    content="working",
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),
                        ScriptedToolCall(name="bash", arguments={"command": "pytest"}, id="call_2"),
                    ),
                )
            ]
        )
        response = await provider.complete(make_request())
        assert [c.ordinal for c in response.tool_calls] == [0, 1]
        assert [str(c.id) for c in response.tool_calls] == ["call_1", "call_2"]
        assert response.tool_calls[1].arguments == {"command": "pytest"}

    async def test_missing_call_ids_are_generated(self) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={}),),
                )
            ]
        )
        response = await provider.complete(make_request())
        assert str(response.tool_calls[0].id).startswith("call_")

    async def test_usage_missing_stays_none(self) -> None:
        provider = FakeProvider([FakeResponse(content="ok")])
        response = await provider.complete(make_request())
        assert response.usage is None

    async def test_empty_response_is_valid(self) -> None:
        provider = FakeProvider([FakeResponse(content="", stop_reason=StopReason.END_TURN)])
        response = await provider.complete(make_request())
        assert response.content == ""
        assert response.stop_reason is StopReason.END_TURN


class TestInvalidResponses:
    async def test_tool_calls_with_wrong_stop_reason_rejected(self) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.END_TURN,
                    tool_calls=(ScriptedToolCall(name="read", arguments={}),),
                )
            ]
        )
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete(make_request())
        assert excinfo.value.kind is ProviderErrorKind.INVALID_RESPONSE

    async def test_stop_tool_calls_without_calls_rejected(self) -> None:
        provider = FakeProvider([FakeResponse(stop_reason=StopReason.TOOL_CALLS)])
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete(make_request())
        assert excinfo.value.kind is ProviderErrorKind.INVALID_RESPONSE

    async def test_corrupted_arguments_json_rejected(self) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments_json='{"path": '),),
                )
            ]
        )
        with pytest.raises(ProviderError, match="corrupted JSON") as excinfo:
            await provider.complete(make_request())
        assert excinfo.value.kind is ProviderErrorKind.INVALID_RESPONSE

    async def test_non_object_arguments_json_rejected(self) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments_json="[1, 2]"),),
                )
            ]
        )
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete(make_request())
        assert excinfo.value.kind is ProviderErrorKind.INVALID_RESPONSE

    async def test_duplicate_tool_definitions_rejected(self) -> None:
        tool = ToolDefinition(name="read", description="read a file", json_schema={"type": "object"})
        provider = FakeProvider([FakeResponse(content="ok")])
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete(make_request(tools=(tool, tool)))
        assert excinfo.value.kind is ProviderErrorKind.INVALID_REQUEST


class TestFaults:
    async def test_fault_kind_propagates(self) -> None:
        provider = FakeProvider(
            [
                FakeFault(ProviderErrorKind.RATE_LIMIT, "slow down"),
                FakeResponse(content="recovered"),
            ]
        )
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete(make_request())
        assert excinfo.value.kind is ProviderErrorKind.RATE_LIMIT

        response = await provider.complete(make_request())
        assert response.content == "recovered"  # 故障后按脚本继续

    async def test_script_exhausted_fails_loud(self) -> None:
        provider = FakeProvider([])
        with pytest.raises(FakeScriptExhaustedError):
            await provider.complete(make_request())

    async def test_delay_is_awaited(self) -> None:
        provider = FakeProvider([FakeResponse(content="late", delay_seconds=0.05)])
        started = time.monotonic()
        await provider.complete(make_request())
        assert time.monotonic() - started >= 0.03


class TestCancellation:
    async def test_cancelled_before_start(self) -> None:
        token = Token()
        token.cancelled = True
        provider = FakeProvider([FakeResponse(content="never")])
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete(make_request(), token)
        assert excinfo.value.kind is ProviderErrorKind.CANCELLED

    async def test_cancelled_during_delay(self) -> None:
        token = Token()
        provider = FakeProvider([FakeResponse(content="never", delay_seconds=5.0)])
        task = asyncio.create_task(provider.complete(make_request(), token))
        await asyncio.sleep(0.05)
        token.cancelled = True
        with pytest.raises(ProviderError) as excinfo:
            await task
        assert excinfo.value.kind is ProviderErrorKind.CANCELLED


class TestStreamSkeleton:
    async def test_text_chunks_and_stop_delta(self) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    content="Hello",
                    stop_reason=StopReason.END_TURN,
                    content_chunks=("Hel", "lo"),
                    usage=TokenUsage(3, 2),
                )
            ]
        )
        deltas = [delta async for delta in provider.stream(make_request())]
        assert [d.text for d in deltas if isinstance(d, TextDelta)] == ["Hel", "lo"]
        assert any(isinstance(d, UsageDelta) for d in deltas)
        assert isinstance(deltas[-1], StopDelta)
        assert deltas[-1].stop_reason is StopReason.END_TURN

    async def test_tool_call_deltas_carry_complete_calls(self) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a"}, id="call_1"),),
                )
            ]
        )
        deltas = [delta async for delta in provider.stream(make_request())]
        tool_deltas = [d for d in deltas if isinstance(d, ToolCallDelta)]
        assert len(tool_deltas) == 1
        assert str(tool_deltas[0].call.id) == "call_1"
        assert isinstance(deltas[-1], StopDelta)
        assert deltas[-1].stop_reason is StopReason.TOOL_CALLS
