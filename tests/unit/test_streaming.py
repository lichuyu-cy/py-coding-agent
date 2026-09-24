"""阶段 17 测试：分片拼装、流中断防护与 complete/stream 最终产物一致。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.bootstrap import build_runtime
from coding_agent.domain.events import EventType
from coding_agent.domain.messages import ToolCall, ToolCallId
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.observability.stream import (
    AgentStreamAggregator,
    InterruptedStreamError,
    StreamAssemblyError,
    ToolCallAccumulator,
)
from coding_agent.ports.provider import (
    ProviderError,
    StopDelta,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
    ToolCallFragmentDelta,
    UsageDelta,
)
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall

FRAGMENT_PAYLOAD = ('{"comm', 'and": "echo ', 'hi"}')


class TestToolCallAccumulator:
    def test_fragments_assembled_regardless_of_order(self) -> None:
        acc = ToolCallAccumulator()
        acc.add_fragment(ToolCallFragmentDelta("call_1", FRAGMENT_PAYLOAD[2], fragment_seq=2, name="bash"))
        acc.add_fragment(ToolCallFragmentDelta("call_1", FRAGMENT_PAYLOAD[0], fragment_seq=0, name="bash"))
        acc.add_fragment(ToolCallFragmentDelta("call_1", FRAGMENT_PAYLOAD[1], fragment_seq=1, name="bash"))
        calls = acc.finalize()
        assert len(calls) == 1
        assert calls[0].arguments == {"command": "echo hi"}
        assert calls[0].ordinal == 0

    def test_duplicate_fragment_seq_deduped(self) -> None:
        acc = ToolCallAccumulator()
        acc.add_fragment(ToolCallFragmentDelta("call_1", FRAGMENT_PAYLOAD[0], fragment_seq=0, name="bash"))
        acc.add_fragment(ToolCallFragmentDelta("call_1", "GARBAGE", fragment_seq=0, name="bash"))
        acc.add_fragment(ToolCallFragmentDelta("call_1", FRAGMENT_PAYLOAD[1], fragment_seq=1, name="bash"))
        acc.add_fragment(ToolCallFragmentDelta("call_1", FRAGMENT_PAYLOAD[2], fragment_seq=2, name="bash"))
        calls = acc.finalize()
        assert calls[0].arguments == {"command": "echo hi"}

    def test_complete_delta_deduped_and_mixed_with_fragments(self) -> None:
        acc = ToolCallAccumulator()
        complete = ToolCall(id=ToolCallId("call_1"), name="read", arguments={"path": "a.py"}, ordinal=0)
        acc.add_complete(complete)
        acc.add_complete(complete)  # 重复完整对象去重
        acc.add_fragment(ToolCallFragmentDelta("call_1", "broken", fragment_seq=0, name="read"))
        acc.add_fragment(ToolCallFragmentDelta("call_2", '{"path":"b.py"}', fragment_seq=0, name="read"))
        calls = acc.finalize()
        assert [str(c.id) for c in calls] == ["call_1", "call_2"]
        assert calls[0].arguments == {"path": "a.py"}  # 完整对象优先
        assert calls[1].arguments == {"path": "b.py"}

    def test_corrupted_json_rejected(self) -> None:
        acc = ToolCallAccumulator()
        acc.add_fragment(ToolCallFragmentDelta("call_1", '{"path": ', fragment_seq=0, name="read"))
        with pytest.raises(StreamAssemblyError, match="corrupted JSON"):
            acc.finalize()

    def test_missing_name_rejected(self) -> None:
        acc = ToolCallAccumulator()
        acc.add_fragment(ToolCallFragmentDelta("call_1", '{"path":"a.py"}', fragment_seq=0))
        with pytest.raises(StreamAssemblyError, match="missing its name"):
            acc.finalize()

    def test_empty_arguments_rejected(self) -> None:
        acc = ToolCallAccumulator()
        acc.add_fragment(ToolCallFragmentDelta("call_1", "", fragment_seq=0, name="read"))
        with pytest.raises(StreamAssemblyError, match="no complete arguments"):
            acc.finalize()


class TestAggregator:
    def test_text_usage_and_stop(self) -> None:
        aggregator = AgentStreamAggregator()
        aggregator.aggregate(TextDelta("Hel"))
        aggregator.aggregate(TextDelta("lo"))
        aggregator.aggregate(UsageDelta(TokenUsage(3, 2)))
        aggregator.aggregate(StopDelta(StopReason.END_TURN))
        response = aggregator.finalize_response()
        assert response.content == "Hello"
        assert response.usage == TokenUsage(3, 2)
        assert response.stop_reason is StopReason.END_TURN

    def test_usage_missing_is_none(self) -> None:
        aggregator = AgentStreamAggregator()
        aggregator.aggregate(TextDelta("done"))
        aggregator.aggregate(StopDelta(StopReason.END_TURN))
        assert aggregator.finalize_response().usage is None

    def test_interrupted_stream_rejected(self) -> None:
        aggregator = AgentStreamAggregator()
        aggregator.aggregate(TextDelta("partial"))
        aggregator.aggregate(ToolCallFragmentDelta("call_1", '{"path":', fragment_seq=0, name="read"))
        with pytest.raises(InterruptedStreamError):
            aggregator.finalize_response()
        assert aggregator.interrupted is True

    def test_stop_reason_incompatible_with_calls_rejected(self) -> None:
        aggregator = AgentStreamAggregator()
        aggregator.aggregate(ToolCallDelta(ToolCall(id=ToolCallId("call_1"), name="read", arguments={}, ordinal=0)))
        aggregator.aggregate(StopDelta(StopReason.END_TURN))
        with pytest.raises(ProviderError):
            aggregator.finalize_response()


class TestParityAndIntegration:
    def _script(self) -> list:
        return [
            FakeResponse(
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                content="thinking about it",
                content_chunks=("thinking", " about it"),  # 与 content 同源（同一响应的分片形态）
            ),
            FakeResponse(content="finished", stop_reason=StopReason.END_TURN, content_chunks=("fin", "ished")),
        ]

    def _structural(self, log) -> list:
        items = []
        for message in log.messages:
            if message.message_type == "tool_result":
                items.append(("tool_result", message.status.value, message.content))
            elif message.message_type == "assistant":
                items.append(
                    (
                        "assistant",
                        message.content,
                        message.stop_reason.value,
                        tuple((c.name, dict(c.arguments), c.ordinal) for c in message.tool_calls),
                    )
                )
            else:
                items.append((message.message_type, message.content))
        return items

    async def test_complete_and_stream_produce_identical_persisted_messages(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

        complete_runtime = build_runtime(provider=FakeProvider(self._script()), streaming=False)
        complete_result = await complete_runtime.run("task", tmp_path)
        complete_log = complete_runtime.registry.get(complete_result.session_id)

        stream_runtime = build_runtime(provider=FakeProvider(self._script()), streaming=True)
        stream_result = await stream_runtime.run("task", tmp_path)
        stream_log = stream_runtime.registry.get(stream_result.session_id)

        assert complete_result.status is RunStatus.FINISHED
        assert stream_result.status is RunStatus.FINISHED
        assert self._structural(stream_log) == self._structural(complete_log)
        # 流式路径发布了易失增量事件
        stream_events = stream_runtime.bus.events(
            run_id=stream_result.run_id, types=[EventType.LLM_REQUEST_STREAM]
        )
        assert len(stream_events) > 0
        assert any(dict(e.payload).get("kind") == "text" for e in stream_events)

    async def test_fragmented_tool_arguments_execute_after_assembly(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="bash",
                            arguments_json_chunks=('{"comm', 'and": "echo ', 'fragmented"}'),
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider, streaming=True)
        result = await runtime.run("task", tmp_path)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)
        tool_result = log.find_result("call_1")
        assert tool_result is not None
        assert tool_result.status.value == "completed"
        assert "fragmented" in tool_result.content

    async def test_interrupted_stream_fails_without_committing_half_calls(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="bash",
                            arguments_json_chunks=('{"comm', 'and": "echo '),
                            id="call_1",
                        ),
                    ),
                    truncate_stream=True,
                ),
            ]
        )
        runtime = build_runtime(provider=provider, streaming=True)
        result = await runtime.run("task", tmp_path)

        assert result.status is RunStatus.ERROR
        log = runtime.registry.get(result.session_id)
        # 半段调用未进入历史、未执行工具
        assert [m.message_type for m in log.messages] == ["user"]
        errors = runtime.bus.events(run_id=result.run_id, types=[EventType.ERROR])
        assert any(dict(e.payload).get("stage") == "provider" for e in errors)
