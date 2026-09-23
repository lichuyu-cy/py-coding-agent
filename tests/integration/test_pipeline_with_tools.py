"""阶段 07 集成测试：Bootstrap → Runtime → ToolPipeline → 真实编码工具。

验证生产路径：模型提出的调用经统一管线执行真实的 Read/Write 工具，
错误结果（未知工具）以结构化 ToolResult 进入下一轮模型请求。
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.bootstrap import build_registry, build_runtime
from coding_agent.domain.messages import AssistantMessage, ToolResult
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall
from coding_agent.tools.pipeline import ToolPipeline


class TestPipelineWithRealTools:
    async def test_read_tool_through_pipeline(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("line-one\nline-two\n", encoding="utf-8")
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),),
                ),
                FakeResponse(content="read it", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("inspect a.py", tmp_path)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)
        tool_result = log.find_result("call_1")
        assert tool_result is not None
        assert tool_result.status.value == "completed"
        assert "line-one" in tool_result.content
        assert "     1 | line-one" in tool_result.content  # 带行号输出

        # 第二请求携带真实读取结果
        second = provider.requests[1]
        assert "line-one" in second.messages[-1].content

    async def test_write_tool_modifies_workspace(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="write",
                            arguments={"path": "pkg/new.py", "content": "value = 1\n"},
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(content="created", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("create module", tmp_path)

        assert result.status is RunStatus.FINISHED
        assert (tmp_path / "pkg" / "new.py").read_text(encoding="utf-8") == "value = 1\n"
        log = runtime.registry.get(result.session_id)
        tool_result = log.find_result("call_1")
        assert tool_result is not None
        assert "created new file" in tool_result.content

    async def test_unknown_tool_error_then_repair(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="do_magic", arguments={}, id="call_1"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_2"),),
                ),
                FakeResponse(content="recovered", stop_reason=StopReason.END_TURN),
            ]
        )
        (tmp_path / "a.py").write_text("ok\n", encoding="utf-8")
        runtime = build_runtime(provider=provider)
        result = await runtime.run("fix it", tmp_path)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)
        rejected = log.find_result("call_1")
        assert rejected is not None
        assert rejected.status.value == "error"
        assert rejected.error_kind == "unknown_tool"
        assert log.find_result("call_2").status.value == "completed"

        # 模型在第二次请求中看到了未知工具的拒绝结果
        second = provider.requests[1]
        assert "unknown tool" in second.messages[-1].content

    async def test_invalid_arguments_rejected_without_execution(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(name="read", arguments={"path": "a.py", "bogus": 1}, id="call_1"),
                    ),
                ),
                FakeResponse(content="understood", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("read", tmp_path)
        log = runtime.registry.get(result.session_id)
        rejected = log.find_result("call_1")
        assert rejected is not None
        assert rejected.error_kind == "invalid_arguments"
        assert not (tmp_path / "bogus").exists()

    async def test_allowlist_limits_declared_tools(self, tmp_path: Path) -> None:
        events: list = []
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider, tool_allowlist=["read"], observer=events.append)
        await runtime.run("just answer", tmp_path)
        declared = [tool.name for tool in provider.requests[0].tools]
        assert declared == ["read"]

    async def test_pipeline_deadline_cuts_off_slow_tool(self, tmp_path: Path) -> None:
        """Runtime 通过 Pipeline 传递 deadline：慢命令在截止时间被截断，run 预算随之耗尽。"""
        import time

        registry = build_registry()
        pipeline = ToolPipeline(registry, execute_timeout_seconds=5.0)
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="bash",
                            arguments={"command": 'python -c "import time; time.sleep(30)"'},
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ]
        )
        from coding_agent.agent.loop import RunLimits

        runtime = build_runtime(
            provider=provider,
            limits=RunLimits(deadline_seconds=1.0),
            registry=registry,
            pipeline=pipeline,
        )
        started = time.monotonic()
        result = await runtime.run("run slow command", tmp_path)
        elapsed = time.monotonic() - started

        # 30s 的睡眠在约 1s 的 deadline 处被截断；run 因预算耗尽结束
        assert elapsed < 8
        assert result.status is RunStatus.BUDGET_EXHAUSTED
        assert result.limit_hit == "deadline"
        log = runtime.registry.get(result.session_id)
        tool_result = log.find_result("call_1")
        assert tool_result is not None
        assert tool_result.status.value == "timeout"

    async def test_message_log_pairing_through_pipeline(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(name="read", arguments={"path": "a.py"}, id="call_1"),
                        ScriptedToolCall(name="bash", arguments={"command": "echo hi"}, id="call_2"),
                    ),
                ),
                FakeResponse(content="all good", stop_reason=StopReason.END_TURN),
            ]
        )
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        runtime = build_runtime(provider=provider)
        result = await runtime.run("inspect and echo", tmp_path)
        log = runtime.registry.get(result.session_id)
        assistants = [m for m in log.messages if isinstance(m, AssistantMessage)]
        results = [m for m in log.messages if isinstance(m, ToolResult)]
        assert len(assistants) == 2
        assert len(results) == 2
        assert [str(r.tool_call_id) for r in results] == ["call_1", "call_2"]
        assert all(r.status.value == "completed" for r in results)
