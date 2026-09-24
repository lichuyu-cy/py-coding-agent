"""Runtime：run 的生命周期、会话注册与运行锁、会话持久化与结果包装。

阶段 04 范围：
- `run(task, workspace, session_id?)` 与 `continue_run(session_id)` 入口；
- 内存会话注册表与每会话独占运行锁（补足阶段 02 记录的“无两个活动运行写同一 Session”）；
- 一切持久写入经 MessageLog；Runtime 是 RuntimeState 的唯一写者。

阶段 19：session_store 存在时——消息逐条原子落库、新会话创建持久记录、
未命中会话从 store 水合（含最新摘要）、store 运行锁跨实例互斥、workspace 绑定校验。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.agent.control import (
    AbortReason,
    FollowUpItem,
    FollowUpQueue,
    RunControl,
    SteeringMessage,
    SteeringRejectedError,
    UnknownRunError,
)
from coding_agent.agent.loop import AgentLoop, LoopObserver, MinimalToolExecutor, RunLimits
from coding_agent.agent.recovery import workspace_fingerprint
from coding_agent.context.builder import ContextManager, PromptSection
from coding_agent.context.compaction import (
    CompactionRecord,
    compaction_record_from_store,
    summary_record_of,
)
from coding_agent.context.skills import SKILLS_DIR, SkillRegistry
from coding_agent.domain.errors import HarnessError
from coding_agent.domain.events import EventType
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.metrics import MetricsAccumulator
from coding_agent.domain.messages import (
    AssistantMessage,
    Message,
    MessageLog,
    MessageMeta,
    ToolResult,
    UserMessage,
    message_from_dict,
    message_to_dict,
    new_id,
    new_message_id,
    utc_now_rfc3339,
)
from coding_agent.domain.state import RunStatus, RuntimeState, StateTrigger, StopReason
from coding_agent.ports.checkpoint import (
    BoundaryEvent,
    BoundaryKind,
    Checkpoint,
    CheckpointStore,
    IntentStatus,
    ToolExecutionStart,
    ToolIntent,
)
from coding_agent.ports.provider import Provider, ToolDefinition
from coding_agent.ports.store import (
    SessionAppend,
    SessionRecord,
    SessionStore,
    UnknownSessionRecordError,
)

DEFAULT_SYSTEM_PROMPT = (
    "You are a coding agent. You inspect and modify the workspace using the provided tools, "
    "then answer the user's request with a concise report."
)


def _canonical(path: Path) -> str:
    """工作区路径规范化（大小写/相对路径差异不视为换目录）。"""
    try:
        return str(path.resolve())
    except OSError:  # pragma: no cover - 无法规范化时退回原样
        return str(path)


def _digest_arguments(arguments: Mapping[str, Any]) -> str:
    """工具参数摘要（稳定排序的 SHA-256 截断；journal 不落参数原文）。"""
    payload = json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class UnknownSessionError(HarnessError):
    """引用了不存在的会话。"""


class RunConflictError(HarnessError):
    """同一会话已有活动 run：拒绝第二个写者。"""


class WorkspaceMismatchError(HarnessError):
    """store 模式下会话与工作区绑定：拒绝静默切换目录。"""


class ResumeValidationError(HarnessError):
    """继续运行前尾部校验失败（尾部为助手消息或存在未解决的调用）。"""


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    session_id: str
    status: RunStatus
    stop_reason: StopReason | None
    final_text: str
    turns: int
    tool_calls: int
    provider_calls: int
    limit_hit: str | None = None


class InMemorySessionRegistry:
    """阶段 04 的内存会话表与独占运行锁；阶段 19 由持久存储取代。"""

    def __init__(self) -> None:
        self._logs: dict[str, MessageLog] = {}
        self._active: set[str] = set()

    def create_session(self) -> MessageLog:
        log = MessageLog(new_id("sess"))
        self._logs[log.session_id] = log
        return log

    def get(self, session_id: str) -> MessageLog:
        try:
            return self._logs[session_id]
        except KeyError as exc:
            raise UnknownSessionError(f"unknown session {session_id!r}") from exc

    def register(self, log: MessageLog) -> None:
        """注册外部构造的会话（测试/迁移用）。"""
        if log.session_id in self._logs:
            raise HarnessError(f"session {log.session_id!r} already registered")
        self._logs[log.session_id] = log

    def is_active(self, session_id: str) -> bool:
        return session_id in self._active

    def try_acquire(self, session_id: str) -> bool:
        if session_id in self._active:
            return False
        self._active.add(session_id)
        return True

    def release(self, session_id: str) -> None:
        self._active.discard(session_id)


class AgentRuntime:
    """CLI / Server / Benchmark 共用的应用入口（阶段 18/22 复用同一接口）。"""

    def __init__(
        self,
        *,
        provider: Provider,
        executor: MinimalToolExecutor,
        limits: RunLimits | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        registry: InMemorySessionRegistry | None = None,
        observer: LoopObserver | None = None,
        tools: tuple[ToolDefinition, ...] = (),
        model_name: str = "default",
        observation_formatter: Callable[[ToolResult], str] | None = None,
        context_manager: ContextManager | None = None,
        event_bus: EventBus | None = None,
        metrics: MetricsAccumulator | None = None,
        streaming: bool = False,
        session_store: SessionStore | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.registry = registry or InMemorySessionRegistry()
        # 流式增量属易失事件：慢订阅者丢弃而不阻塞控制路径。
        self.bus = event_bus or EventBus(droppable=[EventType.LLM_REQUEST_STREAM])
        self.metrics = metrics or MetricsAccumulator.connect(self.bus)
        self.session_store = session_store
        self.checkpoint_store = checkpoint_store
        checkpoint_enabled = session_store is not None and checkpoint_store is not None
        self._store_versions: dict[str, int] = {}
        self._workspaces: dict[str, Path] = {}
        self._last_state_seq: dict[str, int] = {}
        self._active_runs: dict[str, RunControl] = {}
        self._follow_ups: dict[str, FollowUpQueue] = {}
        self._context_manager = context_manager
        if context_manager is not None and session_store is not None:
            context_manager.set_record_sink(self._persist_summary)
        self._loop = AgentLoop(
            provider=provider,
            executor=executor,
            limits=limits,
            system_prompt=system_prompt,
            tools=tools,
            model_name=model_name,
            observer=observer,
            observation_formatter=observation_formatter,
            context_manager=context_manager,
            event_bus=self.bus,
            streaming=streaming,
            boundary_sink=self._on_boundary if checkpoint_enabled else None,
            tool_start_sink=self._on_tool_start if checkpoint_enabled else None,
        )

    async def run(
        self,
        task: str,
        workspace: str | Path,
        session_id: str | None = None,
        skills: Sequence[str] | None = None,
        *,
        run_id: str | None = None,
    ) -> RunResult:
        """新建 run：追加用户任务消息，驱动循环，返回最终结果。

        skills 为本次 run 选择的技能名称（正文按需加载）；技能目录不存在时仅无元数据段。
        run_id 可选：入口（如 Server）希望提前获知 run 标识时传入。
        """
        if not isinstance(task, str) or not task.strip():
            raise HarnessError("task must be a non-empty string")
        if session_id is not None:
            log = self._get_session_log(session_id)
        else:
            log = self._create_session_log(Path(workspace))
        if not self.registry.try_acquire(log.session_id):
            raise RunConflictError(f"session {log.session_id!r} already has an active run")
        try:
            resolved_workspace = self._bind_workspace(log, Path(workspace))
            resolved_run_id = run_id or new_id("run")
            if not self._acquire_store_lock(log.session_id, resolved_run_id):
                raise RunConflictError(
                    f"session {log.session_id!r} is locked by another runtime"
                    " (store run lock held)"
                )
            try:
                sections = self._skill_sections(resolved_workspace, skills)
                state = RuntimeState(run_id=resolved_run_id).transition(0, StateTrigger.START)
                control = RunControl(state.run_id, log.session_id)
                self._active_runs[control.run_id] = control
                user_message = UserMessage(
                    meta=self._new_meta(log, state),
                    content=task,
                )
                log.append(user_message)
                try:
                    outcome = await self._loop.run(
                        log=log,
                        state=state,
                        workspace=resolved_workspace,
                        control=control,
                        follow_ups=self._follow_up_queue(log.session_id),
                        extra_sections=sections,
                    )
                finally:
                    self._active_runs.pop(control.run_id, None)
                return self._to_result(log, outcome.state, outcome.turns, outcome.tool_calls, outcome.provider_calls, outcome.final_text, outcome.limit_hit)
            finally:
                self._release_store_lock(log.session_id, resolved_run_id)
        finally:
            self.registry.release(log.session_id)

    async def continue_run(
        self,
        session_id: str,
        workspace: str | Path | None = None,
        skills: Sequence[str] | None = None,
    ) -> RunResult:
        """从已提交历史继续（不追加用户消息）；尾部必须合法。

        workspace 优先使用显式传入值，否则回退到该会话已记录的工作区
        （store 模式下来自持久记录的绑定工作区）。
        """
        log = self._get_session_log(session_id)
        self._validate_tail(log)
        resolved = Path(workspace) if workspace is not None else self._workspaces.get(session_id)
        if resolved is None:
            raise UnknownSessionError(
                f"session {session_id!r} has no recorded workspace; pass workspace explicitly"
            )
        resolved = self._bind_workspace(log, resolved)
        if not self.registry.try_acquire(session_id):
            raise RunConflictError(f"session {session_id!r} already has an active run")
        try:
            resolved_run_id = new_id("run")
            if not self._acquire_store_lock(session_id, resolved_run_id):
                raise RunConflictError(
                    f"session {session_id!r} is locked by another runtime"
                    " (store run lock held)"
                )
            try:
                sections = self._skill_sections(resolved, skills)
                state = RuntimeState(run_id=resolved_run_id).transition(0, StateTrigger.START)
                control = RunControl(state.run_id, session_id)
                self._active_runs[control.run_id] = control
                try:
                    outcome = await self._loop.run(
                        log=log,
                        state=state,
                        workspace=resolved,
                        control=control,
                        follow_ups=self._follow_up_queue(session_id),
                        extra_sections=sections,
                    )
                finally:
                    self._active_runs.pop(control.run_id, None)
                return self._to_result(log, outcome.state, outcome.turns, outcome.tool_calls, outcome.provider_calls, outcome.final_text, outcome.limit_hit)
            finally:
                self._release_store_lock(session_id, resolved_run_id)
        finally:
            self.registry.release(session_id)

    # ---- 控制通道（阶段 09：内存队列；阶段 19 持久化队列位置） ----

    def submit_steering(
        self, run_id: str, text: str, *, client_event_id: str | None = None
    ) -> SteeringMessage:
        """向活动 run 提交 steering；run 不存在或已结束时显式拒绝（改用 submit_follow_up）。"""
        control = self._active_runs.get(run_id)
        if control is None:
            raise SteeringRejectedError(run_id)
        return control.steering.enqueue(text, client_event_id=client_event_id)

    def abort(self, run_id: str, reason: AbortReason | str = AbortReason.USER_REQUEST) -> bool:
        """请求取消活动 run（幂等）：返回本次调用是否首次触发取消。

        仅设置取消标志；由 Loop 在安全边界观察取消并完成 ABORTING→ABORTED。
        """
        control = self._active_runs.get(run_id)
        if control is None:
            raise UnknownRunError(run_id)
        return control.cancel.cancel(reason)

    def submit_follow_up(
        self, session_id: str, text: str, *, client_event_id: str | None = None
    ) -> FollowUpItem:
        """向会话提交下一个用户任务（幂等）；在 run 的 turn 结束边界取出一个。"""
        self.registry.get(session_id)  # 未知会话抛 UnknownSessionError
        return self._follow_up_queue(session_id).enqueue(text, client_event_id=client_event_id)

    def pending_follow_ups(self, session_id: str) -> int:
        queue = self._follow_ups.get(session_id)
        return queue.pending_count() if queue is not None else 0

    def _follow_up_queue(self, session_id: str) -> FollowUpQueue:
        queue = self._follow_ups.get(session_id)
        if queue is None:
            queue = FollowUpQueue()
            self._follow_ups[session_id] = queue
        return queue

    # ---- 会话持久化（阶段 19） ----

    def _create_session_log(self, workspace: Path) -> MessageLog:
        """新会话：store 模式下先创建持久记录并逐条落库；否则退回内存注册表。"""
        if self.session_store is None:
            log = self.registry.create_session()
            self._workspaces[log.session_id] = workspace
            return log
        record = self.session_store.create(workspace)
        log = MessageLog(record.session_id)
        self.registry.register(log)
        self._workspaces[record.session_id] = workspace
        self._attach_persistence(log, record.version)
        return log

    def _get_session_log(self, session_id: str) -> MessageLog:
        """获取会话：内存命中直接返回；store 模式未命中时从持久记录水合。"""
        try:
            return self.registry.get(session_id)
        except UnknownSessionError:
            pass
        if self.session_store is None:
            raise UnknownSessionError(f"unknown session {session_id!r}")
        try:
            record = self.session_store.load(session_id)
        except UnknownSessionRecordError as exc:
            raise UnknownSessionError(f"unknown session {session_id!r}") from exc
        log = MessageLog(record.session_id)
        for payload in record.messages:
            log.append(message_from_dict(payload))
        try:
            self.registry.register(log)
        except HarnessError:
            # 并发水合竞态：退回已注册实例，保证持久化回调只挂一次
            return self.registry.get(session_id)
        self._workspaces.setdefault(session_id, Path(record.workspace))
        self._restore_compaction(session_id, record)
        self._attach_persistence(log, record.version)
        return log

    def _restore_compaction(self, session_id: str, record: SessionRecord) -> None:
        """把最新持久摘要回填上下文管理器：重启后压缩版本继续单调。"""
        if self._context_manager is None or not record.summaries:
            return
        latest = max(record.summaries, key=lambda item: item.summary_version)
        self._context_manager.restore_record(session_id, compaction_record_from_store(latest))

    def _attach_persistence(self, log: MessageLog, version: int) -> None:
        """逐条消息同步落库（append-only 与事实日志同序）；版本号乐观推进。"""
        if self.session_store is None:
            return
        self._store_versions[log.session_id] = version

        def persist(message: Message) -> None:
            store = self.session_store
            if store is None:  # pragma: no cover - attach 仅在有 store 时执行
                return
            expected = self._store_versions[log.session_id]
            new_version = store.append(
                log.session_id,
                expected,
                SessionAppend(messages=(message_to_dict(message),)),
            )
            self._store_versions[log.session_id] = new_version
            if self.checkpoint_store is not None:
                # 意图 journal 是消息事实的投影：计划/完成随提交推进
                self._record_intents(message)

        log.on_append = persist

    def _record_intents(self, message: Message) -> None:
        """从消息事实投影工具意图：assistant 的调用登记为 planned，结果提交转 completed。"""
        store = self.checkpoint_store
        if store is None:  # pragma: no cover - persist 仅在启用检查点时调用
            return
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                now = utc_now_rfc3339()
                store.record_intent(
                    ToolIntent(
                        intent_id=f"intent_{call.id}",
                        session_id=message.meta.session_id,
                        run_id=message.meta.run_id,
                        call_id=str(call.id),
                        tool_name=call.name,
                        arguments_digest=_digest_arguments(call.arguments),
                        status=IntentStatus.PLANNED,
                        created_at=now,
                        updated_at=now,
                    )
                )
        elif isinstance(message, ToolResult):
            store.complete_intent(message.meta.session_id, str(message.tool_call_id))

    def _on_tool_start(self, event: ToolExecutionStart) -> None:
        """执行前标记：planned → started（崩溃后即视为副作用未知）。"""
        store = self.checkpoint_store
        if store is None:  # pragma: no cover - 仅启用检查点时接线
            return
        store.mark_started(event.session_id, event.call_id)

    def _on_boundary(self, event: BoundaryEvent) -> None:
        """Loop 提交边界 → 保存检查点（索引到当前会话版本）。"""
        self._last_state_seq[event.session_id] = event.state_seq
        self._save_checkpoint(event.session_id, event.boundary, event.state_seq)

    def _save_checkpoint(self, session_id: str, boundary: BoundaryKind, state_seq: int) -> None:
        store = self.checkpoint_store
        version = self._store_versions.get(session_id)
        if store is None or version is None:
            return
        workspace = self._workspaces.get(session_id)
        store.save(
            Checkpoint(
                checkpoint_id=new_id("ckpt"),
                session_id=session_id,
                session_version=version,
                state_seq=state_seq,
                boundary=boundary,
                pending_intent_ids=tuple(
                    intent.intent_id for intent in store.open_intents(session_id)
                ),
                workspace_fingerprint=(
                    workspace_fingerprint(workspace) if workspace is not None else ""
                ),
                created_at=utc_now_rfc3339(),
            )
        )

    def _persist_summary(self, session_id: str, record: CompactionRecord) -> None:
        """压缩记录写 store（与消息共用版本号；失败向上传播使压缩不被确认）。"""
        store = self.session_store
        if store is None:  # pragma: no cover - sink 仅在有 store 时注入
            return
        expected = self._store_versions.get(session_id, 0)
        new_version = store.append(
            session_id, expected, SessionAppend(summaries=(summary_record_of(record),))
        )
        self._store_versions[session_id] = new_version
        if self.checkpoint_store is not None:
            self._save_checkpoint(
                session_id,
                BoundaryKind.COMPACTION,
                self._last_state_seq.get(session_id, 0),
            )

    def _bind_workspace(self, log: MessageLog, workspace: Path) -> Path:
        """记录会话工作区；store 模式下会话与工作区绑定，拒绝静默切换。"""
        recorded = self._workspaces.get(log.session_id)
        if (
            self.session_store is not None
            and recorded is not None
            and _canonical(recorded) != _canonical(workspace)
        ):
            raise WorkspaceMismatchError(
                f"session {log.session_id!r} is bound to workspace {str(recorded)!r},"
                f" got {str(workspace)!r}"
            )
        self._workspaces[log.session_id] = workspace
        return workspace

    def _acquire_store_lock(self, session_id: str, run_id: str) -> bool:
        if self.session_store is None:
            return True
        return self.session_store.acquire_run_lock(session_id, run_id)

    def _release_store_lock(self, session_id: str, run_id: str) -> None:
        if self.session_store is None:
            return
        self.session_store.release_run_lock(session_id, run_id)

    # ---- 只读查询（供 Server / 诊断使用） ----

    def active_run_ids(self) -> frozenset[str]:
        return frozenset(self._active_runs)

    def active_run_id(self, session_id: str) -> str | None:
        """某会话当前活动 run 的 ID（无则 None）。"""
        for control in self._active_runs.values():
            if control.session_id == session_id:
                return control.run_id
        return None

    def workspace_of(self, session_id: str) -> Path | None:
        """该会话最近一次 run 使用的工作区（未记录则 None）。"""
        return self._workspaces.get(session_id)

    @staticmethod
    def _skill_sections(workspace: Path, skills: Sequence[str] | None) -> tuple[PromptSection, ...]:
        """发现技能并组装段落：元数据常驻，正文仅按选择加载。"""
        registry = SkillRegistry([workspace / SKILLS_DIR])
        sections: list[PromptSection] = []
        metadata_section = registry.metadata_section()
        if metadata_section is not None:
            sections.append(metadata_section)
        if skills:
            for body in registry.select_for_turn(skills):
                sections.append(
                    PromptSection(name=f"skill:{body.metadata.name}", content=body.content)
                )
        return tuple(sections)

    @staticmethod
    def _new_meta(log: MessageLog, state: RuntimeState) -> MessageMeta:
        return MessageMeta(
            id=new_message_id(),
            session_id=log.session_id,
            run_id=state.run_id,
            turn_id=state.current_turn,
            created_at=utc_now_rfc3339(),
        )

    @staticmethod
    def _validate_tail(log: MessageLog) -> None:
        messages = log.messages
        if not messages:
            raise ResumeValidationError("cannot continue: session has no committed messages")
        last = messages[-1]
        if isinstance(last, AssistantMessage):
            raise ResumeValidationError(
                "cannot continue from an assistant tail: no observation to respond to"
            )
        if not isinstance(last, (UserMessage, ToolResult)):
            raise ResumeValidationError(
                f"cannot continue: unsupported tail message type {last.message_type!r}"
            )
        for message in messages:
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if log.find_result(str(call.id)) is None:
                        raise ResumeValidationError(
                            f"cannot continue: tool call {call.id!r} has no final result"
                        )

    @staticmethod
    def _to_result(
        log: MessageLog,
        state: RuntimeState,
        turns: int,
        tool_calls: int,
        provider_calls: int,
        final_text: str,
        limit_hit: str | None,
    ) -> RunResult:
        return RunResult(
            run_id=state.run_id,
            session_id=log.session_id,
            status=state.status or RunStatus.ERROR,
            stop_reason=state.last_stop_reason,
            final_text=final_text,
            turns=turns,
            tool_calls=tool_calls,
            provider_calls=provider_calls,
            limit_hit=limit_hit,
        )
