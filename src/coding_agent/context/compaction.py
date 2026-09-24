"""Compaction：在上下文窗口有限时保留任务状态并安全缩短历史。

核心约束（critical-contracts §3）：
- 只在完整消息组之间切割；一条 assistant 与其全部 tool results 不可拆开；
  未完成工具组之后的内容一律不得被覆盖；
- 最近至少保留 preserve_recent 个完整组（含当前用户请求）；
- 摘要为确定性抽取（不调用模型、不虚构）：可提取字段来自被覆盖消息；
  无法观测的字段写 unknown 并列入 unknown_fields；
- 原始历史永不删除；记录通过校验后才提交（原子性：失败不改任何历史/记录）；
- summary_version 单调递增；covered_through_id 指向覆盖范围的最后一条消息。

阶段边界：摘要为本地确定性实现（设计中的 "Fake Summary" 验证路径）；
模型辅助的高质量摘要属于扩展项。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import (
    AssistantMessage,
    Message,
    ToolResult,
    ToolResultStatus,
    UserMessage,
)
from coding_agent.ports.tokenizer import SimpleTokenCounter, TokenCounter

__all__ = [
    "CompactionError",
    "CompactionRecord",
    "Compactor",
    "CutPlan",
    "MessageGroup",
    "StructuredSummary",
    "extract_summary",
    "find_cut",
    "group_messages",
    "render_summary",
]

_UNKNOWN = "unknown"
_TASK_GOAL_LIMIT = 200


class CompactionError(HarnessError):
    """压缩失败（无合法切点之外的校验失败也走此处）。"""


@dataclass(frozen=True, slots=True)
class MessageGroup:
    """一个不可拆分的消息组：user/system 单独成组；assistant 与其结果成组。"""

    messages: tuple[Message, ...]
    end_index: int  # 该组在历史中的结束下标（inclusive）
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class CutPlan:
    """一次合法切割计划：covered 可被摘要覆盖，preserved 必须保留。"""

    covered: tuple[MessageGroup, ...]
    preserved: tuple[MessageGroup, ...]
    cut_index: int  # messages[cut_index:] 为保留尾部
    covered_ids: tuple[str, ...]
    preserved_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructuredSummary:
    """结构化摘要（字段名对应设计清单；无法观测的字段为 unknown）。"""

    task_goal: str
    current_progress: str
    completed_work: str
    pending_work: str
    files_read: tuple[str, ...]
    files_modified: tuple[str, ...]
    commands_executed: tuple[str, ...]
    test_results: str
    errors: tuple[str, ...]
    important_decisions: str
    next_step: str
    source_ids: tuple[str, ...]
    unknown_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    """一次已提交的压缩记录：覆盖边界 + 摘要 + 版本 + token 前后对比。"""

    summary: StructuredSummary
    covered_through_id: str
    preserved_ids: tuple[str, ...]
    token_before: int
    token_after: int
    summary_version: int


def group_messages(messages: Sequence[Message]) -> tuple[MessageGroup, ...]:
    """按不可拆分边界分组；assistant 组收集紧随其后且属于它的全部工具结果。"""
    groups: list[MessageGroup] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        if isinstance(message, AssistantMessage):
            call_ids = {str(call.id) for call in message.tool_calls}
            collected: list[Message] = [message]
            cursor = index + 1
            while (
                cursor < total
                and isinstance(messages[cursor], ToolResult)
                and str(messages[cursor].tool_call_id) in call_ids
            ):
                collected.append(messages[cursor])
                cursor += 1
            resolved = sum(1 for item in collected if isinstance(item, ToolResult))
            groups.append(
                MessageGroup(
                    messages=tuple(collected),
                    end_index=cursor - 1,
                    incomplete=resolved < len(call_ids),
                )
            )
            index = cursor
        else:
            groups.append(MessageGroup(messages=(message,), end_index=index, incomplete=False))
            index += 1
    return tuple(groups)


def find_cut(messages: Sequence[Message], *, preserve_recent: int) -> CutPlan | None:
    """选择合法切割点：不能拆组、不能越过未完成组、最近至少保留 preserve_recent 组。"""
    if preserve_recent < 1:
        raise ValueError("preserve_recent must be at least 1")
    groups = group_messages(messages)
    blocker = len(groups)
    for position, group in enumerate(groups):
        if group.incomplete:
            blocker = position
            break
    max_coverable = min(len(groups) - preserve_recent, blocker)
    if max_coverable <= 0:
        return None
    covered = groups[:max_coverable]
    preserved = groups[max_coverable:]
    cut_index = covered[-1].end_index + 1
    return CutPlan(
        covered=covered,
        preserved=preserved,
        cut_index=cut_index,
        covered_ids=tuple(str(item.meta.id) for group in covered for item in group.messages),
        preserved_ids=tuple(str(item.meta.id) for group in preserved for item in group.messages),
    )


def extract_summary(messages: Sequence[Message]) -> StructuredSummary:
    """从被覆盖消息中确定性抽取结构化摘要；无法观测的字段写 unknown。"""
    source_ids: list[str] = []
    task_goal = _UNKNOWN
    files_read: list[str] = []
    files_modified: list[str] = []
    commands: list[str] = []
    errors: list[str] = []
    completed_calls = 0
    test_result = _UNKNOWN
    call_info: dict[str, tuple[str, str | None]] = {}
    for message in messages:
        source_ids.append(str(message.meta.id))
        if isinstance(message, UserMessage) and task_goal == _UNKNOWN:
            task_goal = message.content.strip().replace("\n", " ")[:_TASK_GOAL_LIMIT]
        elif isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                arguments = dict(call.arguments)
                command: str | None = None
                if call.name == "read" and isinstance(arguments.get("path"), str):
                    _append_unique(files_read, arguments["path"])
                elif call.name in ("write", "edit") and isinstance(arguments.get("path"), str):
                    _append_unique(files_modified, arguments["path"])
                elif call.name == "bash" and isinstance(arguments.get("command"), str):
                    command = arguments["command"]
                    _append_unique(commands, _shorten(command))
                call_info[str(call.id)] = (call.name, command)
        elif isinstance(message, ToolResult):
            tool_name, command = call_info.get(str(message.tool_call_id), ("tool", None))
            if message.status is ToolResultStatus.COMPLETED:
                completed_calls += 1
                if command is not None and _looks_like_test_command(command):
                    test_result = f"`{command[:80]}` completed (exit_code={message.exit_code})"
            else:
                errors.append(f"{tool_name}: {message.status.value} ({message.error_kind or 'no kind'})")
    unknown_fields: list[str] = []
    for field_name, value in (
        ("current_progress", _UNKNOWN),
        ("pending_work", _UNKNOWN),
        ("important_decisions", _UNKNOWN),
        ("next_step", _UNKNOWN),
    ):
        unknown_fields.append(field_name)
    if task_goal == _UNKNOWN:
        unknown_fields.append("task_goal")
    if test_result == _UNKNOWN:
        unknown_fields.append("test_results")
    return StructuredSummary(
        task_goal=task_goal,
        current_progress=_UNKNOWN,
        completed_work=f"{completed_calls} tool calls completed" if completed_calls else "none",
        pending_work=_UNKNOWN,
        files_read=tuple(files_read),
        files_modified=tuple(files_modified),
        commands_executed=tuple(commands),
        test_results=test_result,
        errors=tuple(errors),
        important_decisions=_UNKNOWN,
        next_step=_UNKNOWN,
        source_ids=tuple(source_ids),
        unknown_fields=tuple(unknown_fields),
    )


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


def _shorten(value: str, limit: int = 120) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _looks_like_test_command(command: str) -> bool:
    lowered = command.lower()
    return "pytest" in lowered or " test" in lowered or lowered.startswith("test")


def render_summary(summary: StructuredSummary) -> str:
    """把结构化摘要渲染为 system 段落文本（稳定格式，便于测试与审计）。"""
    lines = [
        "[summary of earlier history]",
        f"task_goal: {summary.task_goal}",
        f"current_progress: {summary.current_progress}",
        f"completed_work: {summary.completed_work}",
        f"pending_work: {summary.pending_work}",
        f"files_read: {', '.join(summary.files_read) or 'none'}",
        f"files_modified: {', '.join(summary.files_modified) or 'none'}",
        f"commands_executed: {', '.join(summary.commands_executed) or 'none'}",
        f"test_results: {summary.test_results}",
        f"errors: {'; '.join(summary.errors) or 'none'}",
        f"important_decisions: {summary.important_decisions}",
        f"next_step: {summary.next_step}",
    ]
    if summary.unknown_fields:
        lines.append(f"unknown_fields: {', '.join(summary.unknown_fields)}")
    lines.append(f"source_ids: {', '.join(summary.source_ids)}")
    return "\n".join(lines)


class Compactor:
    """合法切点选择 + 结构化摘要 + 记录校验；历史本身永不修改。"""

    def __init__(self, *, preserve_recent: int = 4) -> None:
        if preserve_recent < 1:
            raise ValueError("preserve_recent must be at least 1")
        self._preserve_recent = preserve_recent

    @property
    def preserve_recent(self) -> int:
        return self._preserve_recent

    def find_cut(self, messages: Sequence[Message]) -> CutPlan | None:
        return find_cut(messages, preserve_recent=self._preserve_recent)

    def compact(
        self,
        messages: Sequence[Message],
        *,
        old_record: CompactionRecord | None = None,
        counter: TokenCounter | None = None,
    ) -> CompactionRecord | None:
        """生成新的压缩记录；无合法切点返回 None。失败不改任何输入。"""
        plan = self.find_cut(messages)
        if plan is None:
            return None
        counter = counter or SimpleTokenCounter(chars_per_token=1.0)
        covered_messages = [item for group in plan.covered for item in group.messages]
        summary = extract_summary(covered_messages)
        token_before = sum(counter.count_text(_message_payload(m)) for m in covered_messages)
        token_after = counter.count_text(render_summary(summary))
        version = old_record.summary_version + 1 if old_record is not None else 1
        indexed = _index_by_id(messages)
        record = CompactionRecord(
            summary=summary,
            covered_through_id=plan.covered_ids[-1],
            preserved_ids=plan.preserved_ids,
            token_before=token_before,
            token_after=token_after,
            summary_version=version,
        )
        self.validate(record, messages, indexed=indexed)
        return record

    @staticmethod
    def validate(
        record: CompactionRecord,
        messages: Sequence[Message],
        *,
        indexed: dict[str, int] | None = None,
    ) -> None:
        """校验记录：覆盖边界存在、覆盖范围全为完整组、保留区不被覆盖。"""
        positions = indexed if indexed is not None else _index_by_id(messages)
        if record.covered_through_id not in positions:
            raise CompactionError(
                f"covered_through_id {record.covered_through_id!r} is not in the history"
            )
        boundary = positions[record.covered_through_id]
        covered = messages[: boundary + 1]
        covered_ids = {str(message.meta.id) for message in covered}
        for preserved_id in record.preserved_ids:
            if preserved_id in covered_ids:
                raise CompactionError(
                    f"preserved id {preserved_id!r} falls inside the covered range"
                )
        plan_groups = group_messages(covered)
        for group in plan_groups:
            if group.incomplete:
                raise CompactionError("covered range contains an incomplete tool group")
        if record.token_before < 0 or record.token_after < 0:
            raise CompactionError("token counts must be non-negative")
        if record.summary_version < 1:
            raise CompactionError("summary_version must be >= 1")


def _message_payload(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        calls = ",".join(
            f"{call.name}:{dict(call.arguments)}" for call in message.tool_calls
        )
        return f"{message.content}{calls}"
    return message.content  # type: ignore[union-attr]


def _index_by_id(messages: Sequence[Message]) -> dict[str, int]:
    return {str(message.meta.id): index for index, message in enumerate(messages)}
