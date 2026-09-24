"""阶段 15 集成测试：完整 run 的 typed 事件序列与终结唯一。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.bootstrap import build_runtime
from coding_agent.context.compaction import Compactor
from coding_agent.domain.events import EventType
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeFault, FakeProvider, FakeResponse, ScriptedToolCall
from coding_agent.ports.provider import ProviderErrorKind


async def wait_for_active_run(runtime) -> str:
    for _ in range(200):
        if runtime._active_runs:  # noqa: SLF001
            return next(iter(runtime._active_runs))
        await asyncio.sleep(0.01)
    raise AssertionError("no active run appeared")


class TestEventFlow:
    async def test_full_run_event_sequence_and_terminal_once(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN, usage=None),
            ]
        )
        runtime = build_runtime(provider=provider)
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        result = await runtime.run("task", tmp_path)

        events = runtime.bus.events(run_id=result.run_id)
        assert [e.type for e in events] == [
            EventType.AGENT_START,
            EventType.CONTEXT_BUILD,
            EventType.LLM_REQUEST_START,
            EventType.LLM_REQUEST_END,
            EventType.TOOL_CALL_START,
            EventType.TOOL_CALL_END,
            EventType.CONTEXT_BUILD,
            EventType.LLM_REQUEST_START,
            EventType.LLM_REQUEST_END,
            EventType.AGENT_END,
        ]
        # seq 严格递增；终结事件恰好一次
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert [e.type for e in events].count(EventType.AGENT_END) == 1
        end_payload = dict(events[-1].payload)
        assert end_payload["status"] == "finished"
        # LLM_REQUEST_END 携带 usage 字段（未知为 None，不记 0）
        llm_end = [e for e in events if e.type is EventType.LLM_REQUEST_END][0]
        assert dict(llm_end.payload)["usage_input_tokens"] is None

    async def test_tool_failure_emits_tool_call_error(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="do_magic", arguments={}, id="call_1"),),
                ),
                FakeResponse(content="ok", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("task", tmp_path)
        events = runtime.bus.events(run_id=result.run_id, types=[EventType.TOOL_CALL_ERROR])
        assert len(events) == 1
        assert dict(events[0].payload)["error_kind"] == "unknown_tool"

    async def test_provider_failure_emits_error_and_agent_end(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeFault(ProviderErrorKind.RATE_LIMIT, "busy"),
                FakeFault(ProviderErrorKind.RATE_LIMIT, "busy"),
                FakeFault(ProviderErrorKind.RATE_LIMIT, "busy"),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("task", tmp_path)

        assert result.status is RunStatus.ERROR
        events = runtime.bus.events(run_id=result.run_id)
        error_events = [e for e in events if e.type is EventType.ERROR]
        assert len(error_events) == 1
        assert dict(error_events[0].payload)["stage"] == "provider"
        assert [e.type for e in events].count(EventType.AGENT_END) == 1

    async def test_abort_emits_abort_and_single_end(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="late", stop_reason=StopReason.END_TURN, delay_seconds=5.0)])
        runtime = build_runtime(provider=provider)
        task = asyncio.create_task(runtime.run("task", tmp_path))
        run_id = await wait_for_active_run(runtime)
        runtime.abort(run_id)
        result = await task

        events = runtime.bus.events(run_id=result.run_id)
        assert [e.type for e in events].count(EventType.ABORT) == 1
        assert [e.type for e in events].count(EventType.AGENT_END) == 1
        assert dict(events[-1].payload)["status"] == "aborted"

    async def test_steering_and_follow_up_events(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(content="first", stop_reason=StopReason.END_TURN, delay_seconds=0.3),
                FakeResponse(content="second", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        session = runtime.registry.create_session()
        runtime.submit_follow_up(session.session_id, "next task", client_event_id="fu_1")

        task = asyncio.create_task(runtime.run("task", tmp_path, session_id=session.session_id))
        run_id = await wait_for_active_run(runtime)
        runtime.submit_steering(run_id, "also check logs", client_event_id="st_1")
        result = await task

        events = runtime.bus.events(run_id=result.run_id)
        types = [e.type for e in events]
        assert types.count(EventType.STEERING) == 1
        assert types.count(EventType.FOLLOW_UP) == 1

    async def test_compaction_events(self, tmp_path: Path) -> None:
        big = "x" * 800
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": f"echo {big}"}, id="call_1"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": f"echo {big}"}, id="call_2"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": f"echo {big}"}, id="call_3"),),
                ),
                FakeResponse(content="finished", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(
            provider=provider,
            context_limit_tokens=2600,
            compactor=Compactor(preserve_recent=1),
        )
        result = await runtime.run("print stuff", tmp_path)

        events = runtime.bus.events(run_id=result.run_id)
        types = [e.type for e in events]
        assert types.count(EventType.COMPACTION_START) >= 1
        assert types.count(EventType.COMPACTION_END) >= 1
        end_event = [e for e in events if e.type is EventType.COMPACTION_END][0]
        payload = dict(end_event.payload)
        assert payload["summary_version"] >= 1
        assert payload["token_after"] < payload["token_before"]
        # 压缩事件之后仍正常完成且终结一次
        assert types.count(EventType.AGENT_END) == 1

    async def test_subscriber_isolation_during_run(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)

        def bad(event) -> None:
            raise RuntimeError("boom")

        seen: list = []
        runtime.bus.subscribe(handler=bad)
        runtime.bus.subscribe(handler=seen.append)
        result = await runtime.run("task", tmp_path)

        assert result.status is RunStatus.FINISHED  # 订阅者异常不影响运行
        assert len(seen) >= 3
        assert runtime.bus.subscriber_errors()
