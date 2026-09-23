"""Tool 端口：工具的统一声明与执行契约。

- 工具不自行决定权限、事件投递与输出裁剪（由 Pipeline / Safety 负责）；
- 工具只接收经过验证的参数与只读上下文（`ToolContext`），不接收 Provider 原始字符串；
- 执行失败上抛带分类的 `ToolExecutionError`，或返回带非零 `exit_code` 的正常结果；
- 工具仅依赖 domain/ports，不 import agent/server/observability。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import ToolResultStatus
from coding_agent.ports.provider import CancelSignal


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具的模型可见声明。"""

    name: str
    description: str
    json_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """工具执行上下文（只读）。

    - workspace：所有相对路径的根，工具不得逃逸；
    - cancel：协作式取消信号（None 表示不可取消）；
    - deadline：距 run 截止时间的剩余秒数（None 表示无全局截止），
      工具应取自身超时与该值中的较小者。
    """

    workspace: Path
    cancel: CancelSignal | None = None
    deadline: float | None = None


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """工具的正常执行结果（结构化字段供 Pipeline 归一）。"""

    output: str
    exit_code: int | None = None
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Pipeline 归一后的工具结果载荷（不含 MessageMeta；由 Runtime 附着元数据后入历史）。

    status 使用 domain 的 ToolResultStatus；error_kind 为 ToolErrorKind（或等价字符串）；
    retryable/exit_code 为可操作恢复信息（阶段 08 起填充）。
    """

    status: ToolResultStatus
    content: str
    artifact_ref: str | None = None
    error_kind: str | None = None
    retryable: bool | None = None
    exit_code: int | None = None


class ToolErrorKind(StrEnum):
    """工具失败分类的权威目录（字符串值即稳定的 error_kind 契约）。

    生产者：编码工具（ToolExecutionError.kind）与 Pipeline（检查点拒绝）。
    消费者：recovery.normalize_*（状态/可重试归一）与 to_model_observation（模型观察格式）。
    """

    FILE_NOT_FOUND = "file_not_found"
    NOT_A_FILE = "not_a_file"
    BINARY_FILE = "binary_file"
    INVALID_ENCODING = "invalid_encoding"
    FILE_TOO_LARGE = "file_too_large"
    PATH_ESCAPE = "path_escape"
    INVALID_ARGUMENTS = "invalid_arguments"
    REPLACE_NOT_FOUND = "replace_not_found"
    REPLACE_NOT_UNIQUE = "replace_not_unique"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SPAWN_FAILED = "spawn_failed"
    WORKSPACE_MISSING = "workspace_missing"
    UNKNOWN_TOOL = "unknown_tool"
    NONZERO_EXIT = "nonzero_exit"
    INTERNAL_ERROR = "internal_error"
    HOOK_DENIED = "hook_denied"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_APPROVAL_REQUIRED = "permission_approval_required"
    SAFETY_DENIED = "safety_denied"
    SAFETY_APPROVAL_REQUIRED = "safety_approval_required"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    UNCERTAIN_EFFECT = "uncertain_effect"


class ToolExecutionError(HarnessError):
    """可分类的工具执行失败；由 Pipeline 归一为结构化工具结果。

    kind 为稳定字符串（阶段 08 起由 ToolErrorKind 统一定义）：
    file_not_found / not_a_file / binary_file / invalid_encoding / file_too_large /
    path_escape / invalid_arguments / replace_not_found / replace_not_unique /
    timeout / cancelled / spawn_failed / workspace_missing。
    output 携带失败前已产生的部分输出（如超时命令的 stdout/stderr）。
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        exit_code: int | None = None,
        output: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code
        self.output = output


class Tool(Protocol):
    """统一工具协议。"""

    def spec(self) -> ToolSpec: ...

    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution: ...
