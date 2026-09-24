"""Bootstrap：单一组装入口。

按依赖方向在此组装实现层：注册表（四个编码工具）→ Tool Pipeline（含 SafetyPolicy）→ Runtime。
CLI / Server / Benchmark 只调用本模块暴露的构建函数，不自行拼装内部组件。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from coding_agent.agent.loop import LoopObserver, RunLimits
from coding_agent.agent.runtime import DEFAULT_SYSTEM_PROMPT, AgentRuntime
from coding_agent.context.budget import TokenBudget, TokenManager
from coding_agent.context.builder import ContextManager, ContextPolicy
from coding_agent.context.compaction import Compactor
from coding_agent.context.truncation import ToolOutputTruncator, TruncationPolicy
from coding_agent.domain.messages import ToolResult
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.metrics import MetricsAccumulator
from coding_agent.ports.provider import Provider, ToolDefinition
from coding_agent.ports.store import SessionStore
from coding_agent.ports.tokenizer import SimpleTokenCounter
from coding_agent.tools.bash import BashTool
from coding_agent.tools.edit import EditTool
from coding_agent.tools.pipeline import ToolPipeline
from coding_agent.tools.read import ReadTool
from coding_agent.tools.recovery import to_model_observation
from coding_agent.tools.registry import RegistrySnapshot, ToolRegistry
from coding_agent.tools.safety import SafetyPolicy
from coding_agent.tools.write import WriteTool

CODING_TOOL_NAMES = ("read", "write", "edit", "bash")


def build_registry() -> ToolRegistry:
    """注册四个编码工具，返回装配完成的注册表。"""
    registry = ToolRegistry()
    for tool in (ReadTool(), WriteTool(), EditTool(), BashTool()):
        registry.register(tool)
    return registry


def build_tool_pipeline(registry: ToolRegistry, **kwargs: object) -> ToolPipeline:
    """构建工具管线；kwargs 透传（policy/hooks/observer/execute_timeout 等）。"""
    return ToolPipeline(registry, **kwargs)  # type: ignore[arg-type]


def build_runtime(
    *,
    provider: Provider,
    limits: RunLimits | None = None,
    tool_allowlist: Sequence[str] | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    observer: LoopObserver | None = None,
    model_name: str = "default",
    registry: ToolRegistry | None = None,
    pipeline: ToolPipeline | None = None,
    safety_policy: SafetyPolicy | None = None,
    observation_formatter: Callable[[ToolResult], str] | None = to_model_observation,
    context_manager: ContextManager | None = None,
    truncation_policy: TruncationPolicy | None = None,
    context_limit_tokens: int = 128_000,
    compactor: Compactor | None = None,
    event_bus: EventBus | None = None,
    metrics: MetricsAccumulator | None = None,
    streaming: bool = False,
    session_store: SessionStore | None = None,
) -> AgentRuntime:
    """组装 Runtime：注册表 → 工具声明（快照）→ Pipeline（含安全策略）→ Runtime。"""
    active_registry = registry or build_registry()
    snapshot: RegistrySnapshot = active_registry.snapshot()
    definitions: tuple[ToolDefinition, ...]
    if tool_allowlist is not None:
        definitions = active_registry.definitions(tool_allowlist)
    else:
        definitions = snapshot.definitions()
    active_pipeline = pipeline or ToolPipeline(
        active_registry,
        safety_policy=safety_policy or SafetyPolicy(),
        output_processor=ToolOutputTruncator(policy=truncation_policy or TruncationPolicy()),
    )
    active_context = context_manager
    if active_context is None:
        active_context = ContextManager(
            ContextPolicy(system_prompt=system_prompt),
            observation_formatter=observation_formatter,
            token_manager=TokenManager(
                SimpleTokenCounter(),
                budget=TokenBudget(context_limit=context_limit_tokens),
            ),
            compactor=compactor or Compactor(),
        )
    return AgentRuntime(
        provider=provider,
        executor=active_pipeline,
        limits=limits,
        system_prompt=system_prompt,
        observer=observer,
        tools=definitions,
        model_name=model_name,
        observation_formatter=observation_formatter,
        context_manager=active_context,
        event_bus=event_bus,
        metrics=metrics,
        streaming=streaming,
        session_store=session_store,
    )
