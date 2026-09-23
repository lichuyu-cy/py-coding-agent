"""Tool Safety：策略与执行前检查，限制误操作与工作目录逃逸，并留下授权证据。

- `resolve_path`：解析后的规范路径（复用 file_ops 的真实路径包含性校验）；
- `classify`：按操作与参数给出 RiskLevel（LOW/MEDIUM/HIGH）；
- `authorize`：返回 SafetyDecision（ALLOW/DENY/REQUIRE_APPROVAL + 原因 + 风险 + 策略版本），
  由 Pipeline 在 permission 之后调用；审批项在本地无人审批时由 Pipeline 默认拒绝。

如实声明的边界（非承诺拦截）：
- 命令文本的危险词匹配只降低误用风险，不构成可靠隔离；
- TOCTOU、软链替换与 shell 间接执行无法仅靠解析消除；强隔离需 OS/容器沙盒。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from coding_agent.ports.tool import ToolExecutionError
from coding_agent.tools.file_ops import is_within
from coding_agent.tools.pipeline import PermissionDecision, ToolInvocation

__all__ = ["ResolvedPath", "RiskLevel", "SafetyDecision", "SafetyPolicy"]

_SAFETY_POLICY_VERSION = "safety-1"

# 破坏性命令模式（词法匹配，降低误用风险；不构成可靠隔离）。
_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?<![\w-])rm\s+-[a-z]*[rf]",  # rm -r / -f / -rf / -fr
        r"(?<![\w-])del\s+/\w*[sf]",  # Windows: del /s /f
        r"(?<![\w-])rmdir\s+/\w*s",  # Windows: rmdir /s
        r"(?<![\w-])format\b",
        r"(?<![\w-])mkfs\b",
        r"(?<![\w-])shutdown\b",
        r"(?<![\w-])reboot\b",
    )
)

# 网络访问命令模式。
_NETWORK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"(?<![\w-]){tool}\b")
    for tool in ("curl", "wget", "ssh", "scp", "nc", "ncat", "telnet", "ftp")
)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """路径解析结果；escape 时 resolved 为 None 且 inside_root 为 False。"""

    raw: str
    resolved: Path | None
    inside_root: bool


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """安全判断结果（含风险等级与策略版本，作为授权证据）。"""

    decision: PermissionDecision
    reason: str
    risk: RiskLevel
    policy_version: str = _SAFETY_POLICY_VERSION


class SafetyPolicy:
    """默认安全策略：保护路径拒写、破坏性/网络命令需审批（本地默认拒绝）。"""

    policy_version = _SAFETY_POLICY_VERSION
    _PATH_ARG_TOOLS = ("read", "write", "edit")
    _WRITE_TOOLS = ("write", "edit")

    def __init__(
        self,
        *,
        protected_paths: Sequence[str] = (".git",),
        allow_destructive_commands: bool = False,
        allow_network_commands: bool = False,
    ) -> None:
        self._protected = tuple(name.strip().strip("/\\") for name in protected_paths if name.strip())
        self._allow_destructive = allow_destructive_commands
        self._allow_network = allow_network_commands

    # ---- 接口 ----

    def resolve_path(self, path: str, root: Path) -> ResolvedPath:
        """解析路径并校验包含性；越界/非法返回 inside_root=False 的占位结果。"""
        if not isinstance(path, str) or not path.strip():
            return ResolvedPath(raw=str(path), resolved=None, inside_root=False)
        try:
            resolved = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        except OSError:
            return ResolvedPath(raw=path, resolved=None, inside_root=False)
        inside = is_within(resolved, root.resolve())
        return ResolvedPath(raw=path, resolved=resolved if inside else None, inside_root=inside)

    def classify(self, invocation: ToolInvocation) -> RiskLevel:
        name = invocation.record.name
        if name == "bash":
            command = str(invocation.args.get("command", ""))
            if self._match_destructive(command) is not None:
                return RiskLevel.HIGH
            if self._match_network(command) is not None:
                return RiskLevel.MEDIUM
            return RiskLevel.LOW
        if name in ("write", "edit"):
            resolved = self._resolve_invocation_path(invocation)
            if resolved is not None and not resolved.inside_root:
                return RiskLevel.HIGH
            if resolved is not None and resolved.inside_root and self._is_protected(resolved, invocation):
                return RiskLevel.HIGH
            return RiskLevel.LOW
        return RiskLevel.LOW

    def authorize(self, invocation: ToolInvocation) -> SafetyDecision:
        name = invocation.record.name
        root = invocation.context.workspace

        if name in self._PATH_ARG_TOOLS:
            resolved = self.resolve_path(str(invocation.args.get("path", "")), root)
            if not resolved.inside_root:
                return SafetyDecision(
                    PermissionDecision.DENY,
                    f"path {resolved.raw!r} resolves outside the workspace or cannot be resolved",
                    RiskLevel.HIGH,
                )
            if name in self._WRITE_TOOLS and self._is_protected(resolved, invocation):
                return SafetyDecision(
                    PermissionDecision.REQUIRE_APPROVAL,
                    f"path {resolved.raw!r} is inside a protected location "
                    f"({', '.join(self._protected)}); writes need explicit approval",
                    RiskLevel.HIGH,
                )
            return SafetyDecision(PermissionDecision.ALLOW, "path inside workspace", RiskLevel.LOW)

        if name == "bash":
            command = str(invocation.args.get("command", ""))
            matched = self._match_destructive(command)
            if matched is not None:
                if self._allow_destructive:
                    return SafetyDecision(
                        PermissionDecision.ALLOW,
                        f"destructive pattern {matched!r} allowed by policy configuration",
                        RiskLevel.HIGH,
                    )
                return SafetyDecision(
                    PermissionDecision.REQUIRE_APPROVAL,
                    f"destructive command pattern {matched!r} detected; approval required",
                    RiskLevel.HIGH,
                )
            matched = self._match_network(command)
            if matched is not None:
                if self._allow_network:
                    return SafetyDecision(
                        PermissionDecision.ALLOW,
                        f"network command {matched!r} allowed by policy configuration",
                        RiskLevel.MEDIUM,
                    )
                return SafetyDecision(
                    PermissionDecision.REQUIRE_APPROVAL,
                    f"network command {matched!r} detected; approval required",
                    RiskLevel.MEDIUM,
                )
            return SafetyDecision(PermissionDecision.ALLOW, "no risky pattern detected", RiskLevel.LOW)

        return SafetyDecision(PermissionDecision.ALLOW, "no safety rule applies", RiskLevel.LOW)

    # ---- 内部 ----

    def _resolve_invocation_path(self, invocation: ToolInvocation) -> ResolvedPath | None:
        raw = invocation.args.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return self.resolve_path(raw, invocation.context.workspace)

    def _is_protected(self, resolved: ResolvedPath, invocation: ToolInvocation) -> bool:
        if resolved.resolved is None or not self._protected:
            return False
        root = invocation.context.workspace.resolve()
        try:
            relative = resolved.resolved.relative_to(root)
        except ValueError:
            return False
        parts = relative.parts
        if not parts:
            return False
        return parts[0] in self._protected

    @staticmethod
    def _match_destructive(command: str) -> str | None:
        for pattern in _DESTRUCTIVE_PATTERNS:
            if pattern.search(command):
                return pattern.pattern
        return None

    @staticmethod
    def _match_network(command: str) -> str | None:
        for pattern in _NETWORK_PATTERNS:
            if pattern.search(command):
                return pattern.pattern
        return None
