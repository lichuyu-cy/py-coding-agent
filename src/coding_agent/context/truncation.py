"""Tool Output Truncation：控制向模型显示的大输出，同时保留诊断信息与完整原件引用。

- 策略：HEAD（保留头部，如文件读取）/ TAIL（保留尾部，如长错误堆栈）/
  HEAD_TAIL（头尾都保留，如命令输出）；
- 原件：完整输出写入受控 artifact 路径（workspace/.coding-agent/artifacts/），
  会话只保存引用；存储失败仍返回有限显示文本并显式标注"无原件"；
- 预算：显示文本（含省略标记）不超过 policy.max_chars（默认取契约值 12,000 字符）；
- 裁剪在已解码的字符串上进行（UTF-8 安全：不会截断码点/代理对）；
- 脱敏边界：artifact 不入公共日志；省略标记只包含计数与引用，不包含内容。

本模块实现 pipeline 的 OutputProcessor 协议（阶段 07 预留的缝）：阶段 11 起由
ToolOutputTruncator 接管；阶段 13 的 TokenManager 将提供 token 维度的配额输入。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from coding_agent.ports.tool import ToolOutcome

if TYPE_CHECKING:  # 仅用于类型标注，避免运行时反向依赖
    from coding_agent.tools.pipeline import ToolInvocation

__all__ = [
    "ARTIFACT_DIR",
    "ArtifactStore",
    "DisplayedOutput",
    "ToolOutputTruncator",
    "TruncationPolicy",
    "TruncationStrategy",
    "choose_strategy",
]

ARTIFACT_DIR = Path(".coding-agent") / "artifacts"

_DEFAULT_MAX_CHARS = 12_000  # critical-contracts 第 6 节：单个工具输出上限
_MARKER_RESERVE = 200  # 省略标记（计数 + 引用路径）的预留长度


class TruncationStrategy(StrEnum):
    """保留策略：保留头部 / 保留尾部 / 头尾都保留。"""

    HEAD = "head"
    TAIL = "tail"
    HEAD_TAIL = "head_tail"


@dataclass(frozen=True, slots=True)
class TruncationPolicy:
    """裁剪策略：显示上限与头尾分配。"""

    max_chars: int = _DEFAULT_MAX_CHARS
    head_ratio: float = 0.6  # HEAD_TAIL 中头部占比

    def __post_init__(self) -> None:
        if self.max_chars <= _MARKER_RESERVE:
            raise ValueError(f"max_chars must be greater than the marker reserve {_MARKER_RESERVE}")
        if not 0.0 < self.head_ratio < 1.0:
            raise ValueError("head_ratio must be in (0, 1)")


@dataclass(frozen=True, slots=True)
class DisplayedOutput:
    """裁剪结果：显示文本 + 省略计数 + 原件引用 + 所用策略。"""

    text: str
    omitted_count: int
    artifact_ref: str | None
    strategy: TruncationStrategy


def choose_strategy(kind: str, *, error_kind: str | None = None) -> TruncationStrategy:
    """按输出类型选择策略：长错误优先保留尾部堆栈；文件读取保留头部；其余头尾兼顾。"""
    if error_kind is not None:
        return TruncationStrategy.TAIL
    if kind == "read":
        return TruncationStrategy.HEAD
    return TruncationStrategy.HEAD_TAIL


def _safe_kind(kind: str) -> str:
    cleaned = "".join(ch for ch in kind if ch.isalnum() or ch in "-_")
    return cleaned or "output"


@dataclass(frozen=True, slots=True)
class ArtifactStore:
    """受控 artifact 存储：写入 workspace 内的固定目录，限制单件字符数。

    存储失败（权限/磁盘等）不抛错，返回 None——调用方仍提供有限显示文本。
    """

    max_content_chars: int = 2_000_000

    def store_full_output(self, kind: str, content: str, *, workspace: Path) -> str | None:
        try:
            directory = workspace / ARTIFACT_DIR
            directory.mkdir(parents=True, exist_ok=True)
            file_name = f"{_safe_kind(kind)}-{uuid.uuid4().hex[:12]}.txt"
            path = directory / file_name
            payload = content
            if len(payload) > self.max_content_chars:
                payload = (
                    content[: self.max_content_chars]
                    + f"\n[... artifact copy truncated at {self.max_content_chars} characters]"
                )
            path.write_text(payload, encoding="utf-8", newline="")
            return str(path.relative_to(workspace)).replace("\\", "/")
        except OSError:
            return None


class ToolOutputTruncator:
    """pipeline 的 OutputProcessor 实现：裁剪显示内容并托管完整原件。"""

    def __init__(
        self,
        *,
        policy: TruncationPolicy | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._policy = policy or TruncationPolicy()
        self._store = artifact_store or ArtifactStore()

    @property
    def policy(self) -> TruncationPolicy:
        return self._policy

    def truncate(
        self,
        kind: str,
        output: str,
        *,
        error_kind: str | None = None,
        workspace: Path | None = None,
    ) -> DisplayedOutput:
        """按策略裁剪输出；未超限时原样返回（omitted_count=0、无引用）。"""
        strategy = choose_strategy(kind, error_kind=error_kind)
        if len(output) <= self._policy.max_chars:
            return DisplayedOutput(text=output, omitted_count=0, artifact_ref=None, strategy=strategy)

        artifact_ref: str | None = None
        if workspace is not None:
            artifact_ref = self._store.store_full_output(kind, output, workspace=workspace)

        budget = max(self._policy.max_chars - _MARKER_RESERVE, 1)
        marker = self._marker(len(output) - budget, artifact_ref)
        if strategy is TruncationStrategy.HEAD:
            kept = output[:budget]
            kept_len = len(kept)
            text = f"{kept}\n{marker}"
        elif strategy is TruncationStrategy.TAIL:
            kept = output[-budget:]
            kept_len = len(kept)
            text = f"{marker}\n{kept}"
        else:  # HEAD_TAIL
            head_budget = max(int(budget * self._policy.head_ratio), 1)
            tail_budget = max(budget - head_budget, 1)
            head = output[:head_budget]
            tail = output[-tail_budget:]
            kept_len = len(head) + len(tail)
            text = f"{head}\n{marker}\n{tail}"
        omitted = len(output) - kept_len
        # omitted 以实际保留量精确重算并更新标记（标记长度受 _MARKER_RESERVE 约束）
        final_marker = self._marker(omitted, artifact_ref)
        return DisplayedOutput(
            text=text.replace(marker, final_marker),
            omitted_count=omitted,
            artifact_ref=artifact_ref,
            strategy=strategy,
        )

    def process(self, outcome: ToolOutcome, invocation: "ToolInvocation") -> ToolOutcome:
        """pipeline 适配器：裁剪 outcome.content（工具名/工作区来自 invocation）。"""
        displayed = self.truncate(
            invocation.record.name,
            outcome.content,
            error_kind=outcome.error_kind,
            workspace=invocation.context.workspace,
        )
        return replace(
            outcome,
            content=displayed.text,
            artifact_ref=displayed.artifact_ref or outcome.artifact_ref,
        )

    @staticmethod
    def _marker(omitted: int, artifact_ref: str | None) -> str:
        ref_text = artifact_ref if artifact_ref is not None else "not stored (storage unavailable)"
        return f"[... {omitted} characters omitted; full output: {ref_text}]"
