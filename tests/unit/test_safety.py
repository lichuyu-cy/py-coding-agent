"""阶段 08 单测：SafetyPolicy 的路径解析、风险分类与授权决策。"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from coding_agent.domain.messages import ToolCall, ToolCallId
from coding_agent.ports.tool import ToolContext, ToolExecution, ToolSpec
from coding_agent.tools.pipeline import PermissionDecision, ToolInvocation
from coding_agent.tools.registry import RegisteredTool
from coding_agent.tools.safety import RiskLevel, SafetyPolicy


class DummyTool:
    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution:  # pragma: no cover
        return ToolExecution(output="unused")


def invocation(name: str, args: Mapping[str, Any], workspace: Path) -> ToolInvocation:
    record = RegisteredTool(
        name=name,
        tool=DummyTool(),  # type: ignore[arg-type]
        spec=ToolSpec(name=name, description="dummy", json_schema={"type": "object"}),
    )
    call = ToolCall(id=ToolCallId("call_1"), name=name, arguments=dict(args), ordinal=0)
    return ToolInvocation(call=call, record=record, args=dict(args), context=ToolContext(workspace=workspace))


def try_dir_link(target_dir: Path, link: Path) -> bool:
    try:
        link.symlink_to(target_dir, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target_dir)], capture_output=True, check=False
        )
        return result.returncode == 0 and link.exists()
    return False


class TestResolvePath:
    def test_inside_workspace(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        resolved = policy.resolve_path("pkg/mod.py", tmp_path)
        assert resolved.inside_root is True
        assert resolved.resolved is not None

    def test_parent_escape(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.resolve_path("../outside.txt", tmp_path).inside_root is False

    def test_absolute_outside(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.resolve_path(str(tmp_path.parent / "outside.txt"), tmp_path).inside_root is False

    def test_symlink_escape(self, tmp_path: Path) -> None:
        outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-dir"
        outside_dir.mkdir(exist_ok=True)
        link = tmp_path / "linkdir"
        if not try_dir_link(outside_dir, link):
            pytest.skip("directory links not available on this platform/account")
        policy = SafetyPolicy()
        assert policy.resolve_path("linkdir/x.txt", tmp_path).inside_root is False

    def test_invalid_input(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.resolve_path("", tmp_path).inside_root is False


class TestClassify:
    def test_benign_paths_and_commands_are_low(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.classify(invocation("write", {"path": "a.py", "content": ""}, tmp_path)) is RiskLevel.LOW
        assert policy.classify(invocation("bash", {"command": "echo hi"}, tmp_path)) is RiskLevel.LOW

    def test_destructive_command_is_high(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.classify(invocation("bash", {"command": "rm -rf build"}, tmp_path)) is RiskLevel.HIGH
        assert policy.classify(invocation("bash", {"command": "rm -fr /tmp/x"}, tmp_path)) is RiskLevel.HIGH

    def test_network_command_is_medium(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.classify(invocation("bash", {"command": "curl https://example.com"}, tmp_path)) is RiskLevel.MEDIUM

    def test_protected_path_write_is_high(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.classify(invocation("edit", {"path": ".git/config"}, tmp_path)) is RiskLevel.HIGH
        # 读取保护路径不属于高风险写操作
        assert policy.classify(invocation("read", {"path": ".git/HEAD"}, tmp_path)) is RiskLevel.LOW

    def test_escape_is_high(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        assert policy.classify(invocation("write", {"path": "../evil.txt"}, tmp_path)) is RiskLevel.HIGH


class TestAuthorize:
    def test_inside_write_allowed_with_evidence(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        decision = policy.authorize(invocation("write", {"path": "a.py", "content": "x"}, tmp_path))
        assert decision.decision is PermissionDecision.ALLOW
        assert decision.risk is RiskLevel.LOW
        assert decision.policy_version == "safety-1"

    def test_path_escape_denied(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        decision = policy.authorize(invocation("write", {"path": "../evil.txt", "content": "x"}, tmp_path))
        assert decision.decision is PermissionDecision.DENY
        assert decision.risk is RiskLevel.HIGH
        assert "outside the workspace" in decision.reason

    def test_protected_write_requires_approval(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        decision = policy.authorize(invocation("edit", {"path": ".git/config", "expected_old": "a", "replacement": "b"}, tmp_path))
        assert decision.decision is PermissionDecision.REQUIRE_APPROVAL
        assert decision.risk is RiskLevel.HIGH
        assert ".git" in decision.reason

    def test_protected_read_allowed(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        decision = policy.authorize(invocation("read", {"path": ".git/HEAD"}, tmp_path))
        assert decision.decision is PermissionDecision.ALLOW

    def test_destructive_command_requires_approval(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        decision = policy.authorize(invocation("bash", {"command": "rm -rf build"}, tmp_path))
        assert decision.decision is PermissionDecision.REQUIRE_APPROVAL
        assert decision.risk is RiskLevel.HIGH

    def test_destructive_allowed_when_configured(self, tmp_path: Path) -> None:
        policy = SafetyPolicy(allow_destructive_commands=True)
        decision = policy.authorize(invocation("bash", {"command": "rm -rf build"}, tmp_path))
        assert decision.decision is PermissionDecision.ALLOW
        assert decision.risk is RiskLevel.HIGH  # 风险等级仍如实上报

    def test_network_requires_approval_by_default(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        decision = policy.authorize(invocation("bash", {"command": "wget http://x"}, tmp_path))
        assert decision.decision is PermissionDecision.REQUIRE_APPROVAL
        assert decision.risk is RiskLevel.MEDIUM

    def test_network_allowed_when_configured(self, tmp_path: Path) -> None:
        policy = SafetyPolicy(allow_network_commands=True)
        decision = policy.authorize(invocation("bash", {"command": "curl http://x"}, tmp_path))
        assert decision.decision is PermissionDecision.ALLOW

    def test_benign_command_allowed(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        decision = policy.authorize(invocation("bash", {"command": "python -m pytest"}, tmp_path))
        assert decision.decision is PermissionDecision.ALLOW
        assert decision.risk is RiskLevel.LOW

    def test_decision_is_reproducible(self, tmp_path: Path) -> None:
        policy = SafetyPolicy()
        first = policy.authorize(invocation("bash", {"command": "rm -rf build"}, tmp_path))
        second = policy.authorize(invocation("bash", {"command": "rm -rf build"}, tmp_path))
        assert first == second
