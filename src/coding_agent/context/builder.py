"""Context Manager：为每一次模型请求构造确定性、可解释、预算内的上下文。

- 只选择/转换消息，不编辑原始历史，不决定模型答案；
- `build(log)` 每次从同一版本的历史生成一次性 Provider 请求快照（不持久化转换结果）；
- 合法性校验：空历史、助手尾部、未完成的工具调用组都显式返回 ContextError；
- source_ids 保留完整来源（section 名 + 消息 ID），保证请求可解释、可复现；
- 预算检查使用预留的 TokenCounter 接口（阶段 13 的 TokenManager 取代占位计数器）；
  预算不足时返回 ContextError（真实压缩在阶段 14 接入）。

阶段 12/14 的扩展点：`extra_sections`（技能正文、摘要）按顺序并入 system 段；
`summary_version` 字段已预留（阶段 14 填充）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from coding_agent.context.budget import BudgetDecision, TokenManager
from coding_agent.context.compaction import CompactionRecord, Compactor, render_summary
from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import (
    AssistantMessage,
    Message,
    MessageLog,
    SystemMessage,
    ToolResult,
    UserMessage,
)
from coding_agent.ports.provider import (
    ProviderMessage,
    ProviderMessageRole,
    ProviderToolCallPart,
    ToolDefinition,
)
from coding_agent.ports.tokenizer import SimpleTokenCounter, TokenCounter

__all__ = [
    "ContextError",
    "ContextManager",
    "ContextPolicy",
    "ContextSnapshot",
    "PromptSection",
    "convert_to_provider",
]


class ContextError(HarnessError):
    """上下文无法构造：历史不完整、尾部不可响应或预算不足。"""


@dataclass(frozen=True, slots=True)
class PromptSection:
    """system 前缀中的命名段落（按顺序拼接）。"""

    name: str
    content: str


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """上下文策略：system 段落配置与预算上限。"""

    system_prompt: str
    project_context: str | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """一次模型请求的完整上下文快照。"""

    messages: tuple[ProviderMessage, ...]
    source_ids: tuple[str, ...]
    estimated_tokens: int
    sections: tuple[PromptSection, ...]
    summary_version: int | None = None


def convert_to_provider(
    messages: Sequence[Message],
    *,
    observation_formatter: Callable[[ToolResult], str] | None = None,
) -> tuple[ProviderMessage, ...]:
    """把持久消息投影为 Provider 消息（一次性、无副作用）。"""
    converted: list[ProviderMessage] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            converted.append(ProviderMessage(role=ProviderMessageRole.SYSTEM, content=message.content))
        elif isinstance(message, UserMessage):
            converted.append(ProviderMessage(role=ProviderMessageRole.USER, content=message.content))
        elif isinstance(message, AssistantMessage):
            converted.append(
                ProviderMessage(
                    role=ProviderMessageRole.ASSISTANT,
                    content=message.content,
                    tool_calls=tuple(
                        ProviderToolCallPart(id=str(call.id), name=call.name, arguments=dict(call.arguments))
                        for call in sorted(message.tool_calls, key=lambda c: c.ordinal)
                    ),
                )
            )
        elif isinstance(message, ToolResult):
            content = (
                observation_formatter(message)
                if observation_formatter is not None
                else message.content
            )
            converted.append(
                ProviderMessage(
                    role=ProviderMessageRole.TOOL,
                    content=content,
                    tool_call_id=str(message.tool_call_id),
                )
            )
    return tuple(converted)


class ContextManager:
    """上下文构建器；Runtime/Loop 唯一调用者，Provider 只消费结果。"""

    def __init__(
        self,
        policy: ContextPolicy,
        *,
        counter: TokenCounter | None = None,
        observation_formatter: Callable[[ToolResult], str] | None = None,
        token_manager: TokenManager | None = None,
        compactor: Compactor | None = None,
    ) -> None:
        self._policy = policy
        self._counter: TokenCounter = counter or SimpleTokenCounter()
        self._observation_formatter = observation_formatter
        self._token_manager = token_manager
        self._compactor = compactor
        self._records: dict[str, CompactionRecord] = {}  # 会话 → 已提交压缩记录（阶段 19 持久化）

    @property
    def policy(self) -> ContextPolicy:
        return self._policy

    def build(
        self,
        log: MessageLog,
        *,
        extra_sections: Sequence[PromptSection] = (),
        tool_definitions: Sequence[ToolDefinition] = (),
    ) -> ContextSnapshot:
        """按当前历史构造请求快照；相同输入保证相同输出（确定性）。

        tool_definitions 参与预算估算（工具声明是请求的固定成本）。
        """
        messages = log.messages
        self._validate_history(messages)

        sections: list[PromptSection] = [PromptSection(name="system", content=self._policy.system_prompt)]
        if self._policy.project_context:
            sections.append(PromptSection(name="project", content=self._policy.project_context))
        sections.extend(extra_sections)

        system_content = "\n\n".join(section.content for section in sections)
        provider_messages = (
            ProviderMessage(role=ProviderMessageRole.SYSTEM, content=system_content),
            *convert_to_provider(messages, observation_formatter=self._observation_formatter),
        )

        source_ids = tuple(f"section:{section.name}" for section in sections) + tuple(
            str(message.meta.id) for message in messages
        )
        provisional = ContextSnapshot(
            messages=provider_messages,
            source_ids=source_ids,
            estimated_tokens=0,
            sections=tuple(sections),
            summary_version=None,
        )
        if self._token_manager is not None:
            estimate = self._token_manager.estimate(provisional, tool_definitions=tool_definitions)
            if estimate.decision is not BudgetDecision.FIT:
                # 超过阈值或超窗：先尝试压缩（覆盖部分从请求中移除后再评估）；
                # 无压缩器时保持显式失败（设计：无法适配时明确报告）。
                if self._compactor is not None:
                    return self._compact_and_rebuild(log, sections, tool_definitions, estimate.explanation)
                raise ContextError(estimate.explanation)
            estimated = estimate.total_tokens
        else:
            estimated = self._estimate(system_content, provider_messages)
            if self._policy.max_tokens is not None and estimated > self._policy.max_tokens:
                raise ContextError(
                    f"context does not fit the configured budget: estimated {estimated} tokens "
                    f"exceed max_tokens {self._policy.max_tokens}"
                )
        return replace(provisional, estimated_tokens=estimated)

    def _compact_and_rebuild(
        self,
        log: MessageLog,
        sections: Sequence[PromptSection],
        tool_definitions: Sequence[ToolDefinition],
        overflow_explanation: str,
    ) -> ContextSnapshot:
        """请求压缩并重建上下文：summary 段 + 未覆盖尾部；成功后才提交记录。"""
        if self._compactor is None:
            raise ContextError(overflow_explanation)
        previous = self._records.get(log.session_id)
        record = self._compactor.compact(log.messages, old_record=previous)
        if record is None:
            raise ContextError(
                "compaction requested but no legal cut point is available; history is preserved"
            )
        augmented = (*sections, PromptSection(name="summary", content=render_summary(record.summary)))
        system_content = "\n\n".join(section.content for section in augmented)
        tail = self._tail_after(log, record.covered_through_id)
        provider_messages = (
            ProviderMessage(role=ProviderMessageRole.SYSTEM, content=system_content),
            *convert_to_provider(tail, observation_formatter=self._observation_formatter),
        )
        source_ids = tuple(f"section:{section.name}" for section in augmented) + tuple(
            str(message.meta.id) for message in tail
        )
        provisional = ContextSnapshot(
            messages=provider_messages,
            source_ids=source_ids,
            estimated_tokens=0,
            sections=augmented,
            summary_version=record.summary_version,
        )
        assert self._token_manager is not None
        estimate = self._token_manager.estimate(provisional, tool_definitions=tool_definitions)
        if estimate.decision is BudgetDecision.REJECT:
            raise ContextError(f"context still does not fit after compaction: {estimate.explanation}")
        # 原子提交：只有重建成功才保存记录；相同覆盖边界的重算不递增版本
        if previous is None or previous.covered_through_id != record.covered_through_id:
            self._records[log.session_id] = record
        return replace(provisional, estimated_tokens=estimate.total_tokens)

    @staticmethod
    def _tail_after(log: MessageLog, covered_through_id: str) -> tuple[Message, ...]:
        messages = log.messages
        for index, message in enumerate(messages):
            if str(message.meta.id) == covered_through_id:
                return tuple(messages[index + 1 :])
        raise ContextError(f"covered_through_id {covered_through_id!r} is not in the history")

    @staticmethod
    def _validate_history(messages: Sequence[Message]) -> None:
        if not messages:
            raise ContextError("cannot build context from an empty history")
        if isinstance(messages[-1], AssistantMessage):
            raise ContextError(
                "history tail is an assistant message; there is no observation to respond to"
            )
        for message in messages:
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    resolved = any(
                        isinstance(candidate, ToolResult) and candidate.tool_call_id == call.id
                        for candidate in messages
                    )
                    if not resolved:
                        raise ContextError(
                            f"incomplete tool group: call {call.id!r} has no final result"
                        )

    def _estimate(self, system_content: str, messages: Sequence[ProviderMessage]) -> int:
        total = self._counter.count_text(system_content)
        for message in messages[1:]:  # messages[0] 是 system，已单独计入
            total += self._counter.count_text(message.content)
            for call in message.tool_calls:
                total += self._counter.count_text(call.name)
                total += self._counter.count_text(str(dict(call.arguments)))
        return total
