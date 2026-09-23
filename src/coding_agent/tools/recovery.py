"""Tool Error Recovery：把工具故障归类为可操作的模型观察，并给出恢复提示。

- `hint_for_kind`：每个稳定 error_kind 对应一个 RecoveryHint（是否可重试、建议动作）；
- `normalize_tool_error` / `normalize_internal_error`：ToolExecutionError / 未知异常
  → 归一 ToolOutcome（状态、retryable、exit_code、脱敏消息）；
- `to_model_observation`：把已提交的 ToolResult 渲染为模型可见观察文本
  （阶段 10 的 Context 与阶段 04 起的 Loop 共用；不改变持久事实）。

边界（设计声明）：
- 不对任意命令自动重试；retryable 只表达"同一参数直接重试是否可能安全有效"，
  真实重试决策由模型/用户做出；
- 命令退出码、超时与取消优先归入可见结果，不伪装为文件错误；
- UNCERTAIN_EFFECT 的生产者来自崩溃恢复与取消核对（阶段 09/20），本阶段定义分类与提示。
"""

from __future__ import annotations

from dataclasses import dataclass

from coding_agent.domain.messages import ToolResult, ToolResultStatus
from coding_agent.ports.tool import ToolErrorKind, ToolExecutionError, ToolOutcome

__all__ = [
    "RecoveryHint",
    "hint_for_kind",
    "normalize_internal_error",
    "normalize_tool_error",
    "to_model_observation",
]


@dataclass(frozen=True, slots=True)
class RecoveryHint:
    """针对某一错误分类的恢复建议；retryable 仅表示可能有效，不代表自动重试。"""

    retryable: bool
    suggestion: str


_NO_RETRY_POLICY = "policy decision; change the request or ask for approval instead of retrying"


def _hints() -> dict[str, RecoveryHint]:
    return {
        ToolErrorKind.FILE_NOT_FOUND.value: RecoveryHint(True, "verify the path (list the directory) and retry"),
        ToolErrorKind.NOT_A_FILE.value: RecoveryHint(True, "the target is a directory; pick a file path"),
        ToolErrorKind.BINARY_FILE.value: RecoveryHint(
            False, "the file is binary; text tools cannot edit it"
        ),
        ToolErrorKind.INVALID_ENCODING.value: RecoveryHint(
            False, "the file is not UTF-8; choose another file or tool"
        ),
        ToolErrorKind.FILE_TOO_LARGE.value: RecoveryHint(
            True, "read a smaller line range (start_line/end_line) instead of the whole file"
        ),
        ToolErrorKind.PATH_ESCAPE.value: RecoveryHint(True, "stay inside the workspace and retry"),
        ToolErrorKind.INVALID_ARGUMENTS.value: RecoveryHint(
            True, "fix the arguments to match the tool schema and retry"
        ),
        ToolErrorKind.REPLACE_NOT_FOUND.value: RecoveryHint(
            True, "re-read the file and copy the exact text to replace"
        ),
        ToolErrorKind.REPLACE_NOT_UNIQUE.value: RecoveryHint(
            True, "include more surrounding context or set replace_all"
        ),
        ToolErrorKind.TIMEOUT.value: RecoveryHint(
            False, "do not blindly rerun; narrow the command or split the work first"
        ),
        ToolErrorKind.CANCELLED.value: RecoveryHint(False, "the run was cancelled; do not retry automatically"),
        ToolErrorKind.SPAWN_FAILED.value: RecoveryHint(False, "the process could not start; check the environment"),
        ToolErrorKind.WORKSPACE_MISSING.value: RecoveryHint(
            False, "the workspace directory is missing; report it instead of retrying"
        ),
        ToolErrorKind.UNKNOWN_TOOL.value: RecoveryHint(True, "use one of the declared tools"),
        ToolErrorKind.NONZERO_EXIT.value: RecoveryHint(
            False, "inspect stdout/stderr before rerunning side-effect commands"
        ),
        ToolErrorKind.INTERNAL_ERROR.value: RecoveryHint(
            False, "internal failure; report it and avoid repeating the same call"
        ),
        ToolErrorKind.HOOK_DENIED.value: RecoveryHint(False, _NO_RETRY_POLICY),
        ToolErrorKind.PERMISSION_DENIED.value: RecoveryHint(False, _NO_RETRY_POLICY),
        ToolErrorKind.PERMISSION_APPROVAL_REQUIRED.value: RecoveryHint(False, _NO_RETRY_POLICY),
        ToolErrorKind.SAFETY_DENIED.value: RecoveryHint(False, _NO_RETRY_POLICY),
        ToolErrorKind.SAFETY_APPROVAL_REQUIRED.value: RecoveryHint(False, _NO_RETRY_POLICY),
        ToolErrorKind.TOOL_BUDGET_EXHAUSTED.value: RecoveryHint(
            False, "the tool call budget for this run is exhausted"
        ),
        ToolErrorKind.UNCERTAIN_EFFECT.value: RecoveryHint(
            False, "side-effect state is unknown; manual reconciliation is required"
        ),
    }


_HINTS = _hints()

_UNKNOWN_HINT = RecoveryHint(False, "no recovery hint for this failure; inspect the message")


def hint_for_kind(kind: str | None) -> RecoveryHint:
    if kind is None:
        return _UNKNOWN_HINT
    return _HINTS.get(kind, _UNKNOWN_HINT)


def normalize_tool_error(err: ToolExecutionError) -> ToolOutcome:
    """预期工具故障 → 结构化 ToolOutcome（含 retryable/exit_code）。"""
    kind = str(err.kind)
    if kind == ToolErrorKind.TIMEOUT.value:
        status = ToolResultStatus.TIMEOUT
    elif kind == ToolErrorKind.CANCELLED.value:
        status = ToolResultStatus.CANCELLED
    else:
        status = ToolResultStatus.ERROR
    content = f"{err.message}\n{err.output}" if err.output else err.message
    return ToolOutcome(
        status=status,
        content=content,
        error_kind=kind,
        retryable=hint_for_kind(kind).retryable,
        exit_code=err.exit_code,
    )


def normalize_internal_error(err: BaseException) -> ToolOutcome:
    """未知内部异常 → internal_error 结果；不猜测故障类型，不自动重试。"""
    kind = ToolErrorKind.INTERNAL_ERROR.value
    return ToolOutcome(
        status=ToolResultStatus.ERROR,
        content=f"tool failed with an internal error: {type(err).__name__}: {err}",
        error_kind=kind,
        retryable=hint_for_kind(kind).retryable,
    )


def to_model_observation(result: ToolResult) -> str:
    """把已提交的 ToolResult 渲染为模型可见观察文本（不含工具名；调用方可自行标注）。

    格式稳定，便于测试与后续 Context 复用：
        [tool result: completed]
        [tool result: error kind=nonzero_exit exit_code=2 retryable=false]
        hint: <suggestion>          # 仅失败结果且存在分类时给出
        <content>
    """
    header_parts = [f"[tool result: {result.status.value}"]
    if result.error_kind:
        header_parts.append(f"kind={result.error_kind}")
    if result.exit_code is not None:
        header_parts.append(f"exit_code={result.exit_code}")
    if result.status is not ToolResultStatus.COMPLETED:
        retryable = result.retryable
        if retryable is None:
            retryable = hint_for_kind(result.error_kind).retryable
        header_parts.append(f"retryable={'true' if retryable else 'false'}")
    header = " ".join(header_parts) + "]"

    lines = [header]
    if result.status is not ToolResultStatus.COMPLETED:
        hint = hint_for_kind(result.error_kind)
        if hint is not _UNKNOWN_HINT:
            lines.append(f"hint: {hint.suggestion}")
    lines.append(result.content)
    return "\n".join(lines)
