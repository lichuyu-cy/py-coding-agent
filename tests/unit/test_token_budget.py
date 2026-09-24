"""阶段 13 单测：预算模型、估算、决策与 ContextManager/Loop 接线。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.bootstrap import build_runtime
from coding_agent.context.budget import BudgetDecision, TokenBudget, TokenManager
from coding_agent.context.builder import ContextError, ContextManager, ContextPolicy
from coding_agent.domain.messages import AssistantMessage, MessageLog, MessageMeta, UserMessage
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.ports.provider import (
    ProviderMessage,
    ProviderMessageRole,
    ToolDefinition,
)
from coding_agent.ports.tokenizer import SimpleTokenCounter
from coding_agent.providers.fake import FakeProvider, FakeResponse

SESSION = "sess_budget"


def meta(msg_id: str) -> MessageMeta:
    return MessageMeta(
        id=msg_id, session_id=SESSION, run_id="run_1", turn_id=1, created_at="2026-09-24T00:00:00.000000Z"
    )


def make_snapshot(system: str = "base", history: str = "") -> object:
    from coding_agent.context.builder import ContextSnapshot

    messages = [ProviderMessage(role=ProviderMessageRole.SYSTEM, content=system)]
    if history:
        messages.append(ProviderMessage(role=ProviderMessageRole.USER, content=history))
    return ContextSnapshot(messages=tuple(messages), source_ids=(), estimated_tokens=0, sections=())


class TestBudgetModel:
    def test_usable_window_and_threshold(self) -> None:
        budget = TokenBudget(context_limit=10_000, output_reserve_ratio=0.2, safety_margin_ratio=0.0)
        assert budget.usable_input_tokens == 8000
        assert budget.compaction_threshold_tokens == 6000

    def test_defaults_follow_contract_ratios(self) -> None:
        budget = TokenBudget(context_limit=128_000)
        assert budget.usable_input_tokens == int(128_000 * 0.8 * 0.95)
        assert budget.compaction_threshold_tokens == int(budget.usable_input_tokens * 0.75)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"context_limit": 0},
            {"context_limit": 100, "output_reserve_ratio": 1.0},
            {"context_limit": 100, "safety_margin_ratio": -0.1},
            {"context_limit": 100, "compaction_threshold_ratio": 1.5},
        ],
    )
    def test_invalid_parameters_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            TokenBudget(**kwargs)

    def test_plan_builds_budget(self) -> None:
        manager = TokenManager(SimpleTokenCounter())
        plan = manager.plan(context_limit=64_000, output_reserve_ratio=0.1)
        assert plan.context_limit == 64_000
        assert plan.output_reserve_ratio == 0.1


class TestDecisionBoundaries:
    def _manager(self) -> TokenManager:
        return TokenManager(
            SimpleTokenCounter(chars_per_token=1.0),
            budget=TokenBudget(context_limit=1000, output_reserve_ratio=0.0, safety_margin_ratio=0.0),
        )

    def test_boundaries(self) -> None:
        manager = self._manager()  # usable=1000, threshold=750
        assert manager.decide_total(750) is BudgetDecision.FIT
        assert manager.decide_total(751) is BudgetDecision.COMPACT
        assert manager.decide_total(1000) is BudgetDecision.COMPACT
        assert manager.decide_total(1001) is BudgetDecision.REJECT

    def test_decision_matches_estimate(self) -> None:
        manager = self._manager()
        snapshot = make_snapshot(history="x" * 800)
        estimate = manager.estimate(snapshot)
        assert manager.decide(estimate) is estimate.decision

    def test_estimates_are_deterministic(self) -> None:
        manager = self._manager()
        snapshot = make_snapshot(history="hello world")
        assert manager.estimate(snapshot) == manager.estimate(snapshot)


class TestEstimate:
    def test_fixed_and_history_split(self) -> None:
        manager = TokenManager(SimpleTokenCounter(chars_per_token=1.0))
        snapshot = make_snapshot(system="S" * 10, history="H" * 20)
        estimate = manager.estimate(snapshot)
        assert estimate.fixed_tokens == 10
        assert estimate.history_tokens == 20
        assert estimate.total_tokens == 30

    def test_tool_definitions_counted_as_fixed_cost(self) -> None:
        manager = TokenManager(SimpleTokenCounter(chars_per_token=1.0))
        tool = ToolDefinition(name="read", description="read a file", json_schema={"type": "object"})
        without = manager.estimate(make_snapshot())
        with_tool = manager.estimate(make_snapshot(), tool_definitions=(tool,))
        assert with_tool.fixed_tokens > without.fixed_tokens
        assert with_tool.total_tokens == with_tool.fixed_tokens + with_tool.history_tokens

    def test_fixed_cost_over_window_rejected(self) -> None:
        manager = TokenManager(
            SimpleTokenCounter(chars_per_token=1.0),
            budget=TokenBudget(context_limit=50, output_reserve_ratio=0.0, safety_margin_ratio=0.0),
        )
        estimate = manager.estimate(make_snapshot(system="S" * 100))
        assert estimate.decision is BudgetDecision.REJECT
        assert "exceed" in estimate.explanation


class TestContextManagerIntegration:
    def _manager(self, context_limit: int) -> TokenManager:
        return TokenManager(
            SimpleTokenCounter(chars_per_token=1.0),
            budget=TokenBudget(context_limit=context_limit, output_reserve_ratio=0.0, safety_margin_ratio=0.0),
        )

    def _log(self, content_size: int) -> MessageLog:
        log = MessageLog(SESSION)
        log.append(UserMessage(meta=meta("msg_u1"), content="u" * content_size))
        return log

    def test_fit_passes_and_estimated_tokens_from_manager(self) -> None:
        ctx = ContextManager(ContextPolicy(system_prompt="base"), token_manager=self._manager(10_000))
        snapshot = ctx.build(self._log(100))
        assert snapshot.estimated_tokens == 100 + len("base")

    def test_compaction_required_rejected_without_compactor(self) -> None:
        ctx = ContextManager(ContextPolicy(system_prompt="base"), token_manager=self._manager(2000))
        # usable=2000, threshold=1500；估算 1600 → 需要压缩
        with pytest.raises(ContextError, match="compaction"):
            ctx.build(self._log(1596))

    def test_overflow_rejected(self) -> None:
        ctx = ContextManager(ContextPolicy(system_prompt="base"), token_manager=self._manager(2000))
        with pytest.raises(ContextError, match="cannot fit"):
            ctx.build(self._log(3000))


class TestLoopBudgetTermination:
    async def test_tiny_context_limit_terminates_run_with_budget_status(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="never", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider, context_limit_tokens=120)
        result = await runtime.run("task", tmp_path)

        assert result.status is RunStatus.BUDGET_EXHAUSTED
        assert result.limit_hit == "context_overflow"
        assert provider.call_count == 0  # 未发起任何模型请求
        log = runtime.registry.get(result.session_id)
        assert [m.message_type for m in log.messages] == ["user"]

    async def test_default_budget_does_not_interfere(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        result = await runtime.run("task", tmp_path)
        assert result.status is RunStatus.FINISHED
