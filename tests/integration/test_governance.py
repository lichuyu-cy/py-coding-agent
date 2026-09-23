"""阶段 08 集成测试：SafetyPolicy 与 Error Recovery 在完整 run 中的行为。

覆盖 dependency-graph.md 阶段 08 的验收条件：
- 路径逃逸拒绝；受保护路径保持未修改；
- 错误变结构化结果（命令退出码/拒绝/超时）；
- Fake 能收到错误 ToolResult 后选择其他工具，run 保持一致。
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.bootstrap import build_runtime
from coding_agent.domain.messages import ToolResult
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall
from coding_agent.tools.safety import SafetyPolicy


def results_by_call(runtime_log, call_id: str) -> ToolResult:
    result = runtime_log.find_result(call_id)
    assert result is not None, f"no result for {call_id}"
    return result


class TestGovernance:
    async def test_destructive_command_denied_and_files_untouched(self, tmp_path: Path) -> None:
        (tmp_path / "build").mkdir()
        protected_file = tmp_path / "build" / "artifact.bin"
        protected_file.write_text("keep me", encoding="utf-8")

        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": "rm -rf build"}, id="call_1"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "build/artifact.bin"}, id="call_2"),),
                ),
                FakeResponse(content="used a safer tool", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("clean the build directory", tmp_path)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)

        denied = results_by_call(log, "call_1")
        assert denied.status.value == "denied"
        assert denied.error_kind == "safety_approval_required"
        assert "destructive command" in denied.content

        # 受保护文件保持未修改
        assert protected_file.read_text(encoding="utf-8") == "keep me"

        repaired = results_by_call(log, "call_2")
        assert repaired.status.value == "completed"

        # 模型在错误观察中看到 kind/retryable/hint
        second_request = provider.requests[1]
        observation = second_request.messages[-1].content
        assert "[tool result: denied" in observation
        assert "kind=safety_approval_required" in observation
        assert "retryable=false" in observation
        assert "hint:" in observation

    async def test_protected_path_write_denied_without_modification(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        config = git_dir / "config"
        config.write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")

        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="edit",
                            arguments={
                                "path": ".git/config",
                                "expected_old": "repositoryformatversion = 0",
                                "replacement": "repositoryformatversion = 1",
                            },
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(content="won't touch git internals", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("tweak git config", tmp_path)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)
        denied = results_by_call(log, "call_1")
        assert denied.status.value == "denied"
        assert denied.error_kind == "safety_approval_required"
        assert "repositoryformatversion = 0" in config.read_text(encoding="utf-8")  # 未修改

    async def test_nonzero_exit_observed_then_repaired(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="bash",
                            arguments={"command": 'python -c "import sys; sys.exit(2)"'},
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": "echo all-clear"}, id="call_2"),),
                ),
                FakeResponse(content="recovered from failure", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("run the check", tmp_path)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)

        failed = results_by_call(log, "call_1")
        assert failed.status.value == "error"
        assert failed.error_kind == "nonzero_exit"
        assert failed.exit_code == 2
        assert failed.retryable is False

        second_observation = provider.requests[1].messages[-1].content
        assert "exit_code=2" in second_observation
        assert "retryable=false" in second_observation

        repaired = results_by_call(log, "call_2")
        assert repaired.status.value == "completed"

    async def test_path_escape_denied_via_pipeline(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_text("secret", encoding="utf-8")
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(name="read", arguments={"path": "../" + outside.name}, id="call_1"),
                    ),
                ),
                FakeResponse(content="stayed inside", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("read something outside", tmp_path)
        log = runtime.registry.get(result.session_id)
        denied = results_by_call(log, "call_1")
        assert denied.status.value == "denied"
        assert denied.error_kind == "safety_denied"
        assert "outside the workspace" in denied.content

    async def test_full_trajectory_read_edit_run_tests(self, tmp_path: Path) -> None:
        """修复轨迹：读文件 → 编辑 → 运行测试 → 最终回答（真实工具 + 默认安全策略）。"""
        source = tmp_path / "calc.py"
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "calc.py"}, id="call_1"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="edit",
                            arguments={
                                "path": "calc.py",
                                "expected_old": "return a - b",
                                "replacement": "return a + b",
                            },
                            id="call_2",
                        ),
                    ),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="bash",
                            arguments={"command": 'python -c "import calc; assert calc.add(2, 3) == 5; print(\'tests pass\')"'},
                            id="call_3",
                        ),
                    ),
                ),
                FakeResponse(content="fixed the bug; tests pass", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("fix add()", tmp_path)

        assert result.status is RunStatus.FINISHED
        assert source.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
        log = runtime.registry.get(result.session_id)
        for call_id in ("call_1", "call_2", "call_3"):
            assert results_by_call(log, call_id).status.value == "completed"
        assert "tests pass" in results_by_call(log, "call_3").content
        # 每次提交都有配对结果，顺序与脚本一致
        assert len(provider.requests) == 4

    async def test_safety_policy_configurable(self, tmp_path: Path) -> None:
        import os

        command = "rmdir /s /q build" if os.name == "nt" else "rm -rf build"
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": command}, id="call_1"),),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(
            provider=provider,
            safety_policy=SafetyPolicy(allow_destructive_commands=True),
        )
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "x.txt").write_text("x", encoding="utf-8")
        result = await runtime.run("clean build", tmp_path)
        log = runtime.registry.get(result.session_id)
        executed = results_by_call(log, "call_1")
        assert executed.status.value == "completed"
        assert executed.exit_code == 0
        assert not (tmp_path / "build").exists()
