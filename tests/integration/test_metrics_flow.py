"""阶段 16 集成测试：真实 run 的指标快照与手算一致。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.bootstrap import build_runtime
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall


async def wait_for_active_run(runtime) -> str:
    for _ in range(200):
        if runtime._active_runs:  # noqa: SLF001
            return next(iter(runtime._active_runs))
        await asyncio.sleep(0.01)
    raise AssertionError("no active run appeared")


class TestMetricsFlow:
    async def test_snapshot_matches_hand_counted_trajectory(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                    usage=None,
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        result = await runtime.run("task", tmp_path)

        snapshot = runtime.metrics.snapshot(result.run_id)
        assert snapshot is not None
        metrics = snapshot.metrics
        assert metrics.llm_requests_started == 2
        assert metrics.llm_requests_completed == 2
        assert metrics.tool_calls_started == 1
        assert metrics.tool_calls_completed == 1
        assert metrics.tool_calls_failed == 0
        assert metrics.turns == 2
        assert metrics.final_status == "finished"
        assert metrics.completed is True
        assert metrics.duration_seconds is not None and metrics.duration_seconds >= 0
        # usage 缺失记 unknown，不误记为 0
        assert metrics.usage_unknown_requests >= 1
        # 终结时自动输出并持久化（内存）
        persisted = runtime.metrics.persisted()
        assert any(item.run_id == result.run_id for item in persisted)

    async def test_aborted_run_metrics(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="late", stop_reason=StopReason.END_TURN, delay_seconds=5.0)])
        runtime = build_runtime(provider=provider)
        task = asyncio.create_task(runtime.run("task", tmp_path))
        run_id = await wait_for_active_run(runtime)
        runtime.abort(run_id)
        result = await task

        snapshot = runtime.metrics.snapshot(result.run_id)
        assert snapshot is not None
        assert snapshot.metrics.abort_count == 1
        assert snapshot.metrics.final_status == "aborted"
        assert snapshot.metrics.completed is True
        assert result.status is RunStatus.ABORTED

    async def test_failed_run_records_error_stage(self, tmp_path: Path) -> None:
        from coding_agent.ports.provider import ProviderErrorKind
        from coding_agent.providers.fake import FakeFault

        provider = FakeProvider(
            [
                FakeFault(ProviderErrorKind.RATE_LIMIT, "busy"),
                FakeFault(ProviderErrorKind.RATE_LIMIT, "busy"),
                FakeFault(ProviderErrorKind.RATE_LIMIT, "busy"),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("task", tmp_path)

        snapshot = runtime.metrics.snapshot(result.run_id)
        assert snapshot is not None
        assert snapshot.metrics.final_status == "error"
        assert dict(snapshot.metrics.errors_by_stage).get("provider") == 1

    async def test_failed_tool_counted(self, tmp_path: Path) -> None:
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
        snapshot = runtime.metrics.snapshot(result.run_id)
        assert snapshot is not None
        # START 在进入管线前发布（含被拒绝的调用），失败计入 failed
        assert snapshot.metrics.tool_calls_started == 1
        assert snapshot.metrics.tool_calls_completed == 0
        assert snapshot.metrics.tool_calls_failed == 1
