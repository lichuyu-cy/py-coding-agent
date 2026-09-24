"""Token Management：统一的 token 估算与预算决策。

- `TokenManager` 是唯一的估算/决策入口（禁止各模块自行各算一套）；
- 预算模型：可用输入窗口 = context_limit × (1 - 输出预留) × (1 - 安全余量)；
  压缩阈值 = 可用输入窗口 × compaction_threshold_ratio；
  FIT（≤阈值）/ COMPACT（阈值~可用窗口）/ REJECT（超可用窗口）；
- 估算是预测，不保证供应商最终计费；最终 usage 以 Provider 返回为准。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from coding_agent.ports.provider import ToolDefinition
from coding_agent.ports.tokenizer import SimpleTokenCounter, TokenCounter

if TYPE_CHECKING:  # 仅类型引用，避免与 builder 的运行时循环依赖
    from coding_agent.context.builder import ContextSnapshot

__all__ = ["BudgetDecision", "TokenBudget", "TokenEstimate", "TokenManager"]


class BudgetDecision(StrEnum):
    FIT = "fit"
    COMPACT = "compact"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class TokenBudget:
    """预算参数（默认取 critical-contracts 第 6 节的建议值，非评测最优结论）。"""

    context_limit: int = 128_000
    output_reserve_ratio: float = 0.2
    compaction_threshold_ratio: float = 0.75
    safety_margin_ratio: float = 0.05

    def __post_init__(self) -> None:
        if self.context_limit <= 0:
            raise ValueError("context_limit must be positive")
        for name, ratio in (
            ("output_reserve_ratio", self.output_reserve_ratio),
            ("compaction_threshold_ratio", self.compaction_threshold_ratio),
            ("safety_margin_ratio", self.safety_margin_ratio),
        ):
            if not 0.0 <= ratio < 1.0:
                raise ValueError(f"{name} must be in [0, 1)")

    @property
    def usable_input_tokens(self) -> int:
        return int(self.context_limit * (1 - self.output_reserve_ratio) * (1 - self.safety_margin_ratio))

    @property
    def compaction_threshold_tokens(self) -> int:
        return int(self.usable_input_tokens * self.compaction_threshold_ratio)


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """一次请求候选的估算结果与预算解释。"""

    total_tokens: int
    fixed_tokens: int
    history_tokens: int
    usable_input_tokens: int
    threshold_tokens: int
    decision: BudgetDecision
    explanation: str


class TokenManager:
    """上下文/输出预算的统一入口。"""

    def __init__(
        self,
        counter: TokenCounter | None = None,
        *,
        budget: TokenBudget | None = None,
    ) -> None:
        self._counter: TokenCounter = counter or SimpleTokenCounter()
        self._budget = budget or TokenBudget()

    @property
    def budget(self) -> TokenBudget:
        return self._budget

    def plan(
        self,
        *,
        context_limit: int,
        output_reserve_ratio: float = 0.2,
        safety_margin_ratio: float = 0.05,
        compaction_threshold_ratio: float = 0.75,
    ) -> TokenBudget:
        """构造一份预算（模型窗口 + 预留 + 余量 + 压缩阈值）。"""
        return TokenBudget(
            context_limit=context_limit,
            output_reserve_ratio=output_reserve_ratio,
            compaction_threshold_ratio=compaction_threshold_ratio,
            safety_margin_ratio=safety_margin_ratio,
        )

    def estimate(
        self,
        snapshot: "ContextSnapshot",
        *,
        tool_definitions: Sequence[ToolDefinition] = (),
    ) -> TokenEstimate:
        """估算候选上下文：固定成本（system + 工具声明）+ 历史。"""
        messages = snapshot.messages
        fixed = 0
        history = 0
        if messages:
            fixed += self._counter.count_text(messages[0].content)
        for definition in tool_definitions:
            fixed += self._counter.count_text(definition.name)
            fixed += self._counter.count_text(definition.description)
            fixed += self._counter.count_text(json.dumps(dict(definition.json_schema), sort_keys=True))
        for message in messages[1:]:
            history += self._counter.count_text(message.content)
            for call in message.tool_calls:
                history += self._counter.count_text(call.name)
                history += self._counter.count_text(json.dumps(dict(call.arguments), sort_keys=True))
        total = fixed + history
        decision = self.decide_total(total)
        usable = self._budget.usable_input_tokens
        threshold = self._budget.compaction_threshold_tokens
        if decision is BudgetDecision.FIT:
            explanation = (
                f"estimated {total} tokens fit within the compaction threshold {threshold}"
            )
        elif decision is BudgetDecision.COMPACT:
            explanation = (
                f"estimated {total} tokens exceed the compaction threshold {threshold} "
                f"but fit the usable input window {usable}; compaction required"
            )
        else:
            explanation = (
                f"estimated {total} tokens exceed the usable input window {usable} "
                f"(context limit {self._budget.context_limit}); cannot fit"
            )
        return TokenEstimate(
            total_tokens=total,
            fixed_tokens=fixed,
            history_tokens=history,
            usable_input_tokens=usable,
            threshold_tokens=threshold,
            decision=decision,
            explanation=explanation,
        )

    def decide(self, estimate: TokenEstimate) -> BudgetDecision:
        """同一估算的决策（幂等，与 estimate.explanation 保持一致）。"""
        return estimate.decision

    def decide_total(self, total_tokens: int) -> BudgetDecision:
        if total_tokens <= self._budget.compaction_threshold_tokens:
            return BudgetDecision.FIT
        if total_tokens <= self._budget.usable_input_tokens:
            return BudgetDecision.COMPACT
        return BudgetDecision.REJECT
