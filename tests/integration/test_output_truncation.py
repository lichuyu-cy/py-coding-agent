"""阶段 11 集成测试：大输出经 Pipeline 裁剪、原件托管、模型观察在预算内。"""

from __future__ import annotations

from pathlib import Path

from coding_agent.bootstrap import build_runtime
from coding_agent.context.truncation import ARTIFACT_DIR, TruncationPolicy
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall


class TestOutputTruncationFlow:
    async def test_large_bash_output_truncated_and_archived(self, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="bash",
                            arguments={"command": 'python -c "print(\'X\' * 30000)"'},
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(
            provider=provider,
            truncation_policy=TruncationPolicy(max_chars=1000),
        )
        result = await runtime.run("print a lot", tmp_path)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)
        tool_result = log.find_result("call_1")
        assert tool_result is not None
        assert tool_result.status.value == "completed"
        assert len(tool_result.content) <= 1000  # 显示内容在预算内
        assert "characters omitted" in tool_result.content  # 显式标记发生裁剪
        assert tool_result.artifact_ref is not None
        assert tool_result.artifact_ref.startswith(".coding-agent/artifacts/")

        # 完整原件可访问
        artifact = tmp_path / tool_result.artifact_ref
        assert artifact.exists()
        stored = artifact.read_text(encoding="utf-8")
        assert "X" * 30000 in stored.replace("\n", "") or stored.count("X") >= 29000

        # 模型观察同样在预算内且提示了裁剪与原件位置
        observation = provider.requests[1].messages[-1].content
        assert "characters omitted" in observation
        assert tool_result.artifact_ref in observation

    async def test_read_output_truncated_keeps_head(self, tmp_path: Path) -> None:
        big = tmp_path / "big.txt"
        big.write_text("\n".join(f"line-{i:05d}" for i in range(3000)), encoding="utf-8")
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "big.txt"}, id="call_1"),),
                ),
                FakeResponse(content="done", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider, truncation_policy=TruncationPolicy(max_chars=800))
        result = await runtime.run("inspect the file", tmp_path)

        log = runtime.registry.get(result.session_id)
        tool_result = log.find_result("call_1")
        assert tool_result is not None
        assert len(tool_result.content) <= 800
        assert "line-00000" in tool_result.content  # 头部保留
        assert "characters omitted" in tool_result.content
        assert str(ARTIFACT_DIR).replace("\\", "/") in (tool_result.artifact_ref or "")
