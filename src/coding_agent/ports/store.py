"""Store 端口：会话持久化协议（阶段 19 由 SQLite 实现）。

- `SessionRecord` 是跨请求可读的会话事实（消息、摘要、工作区指纹、版本）；
- `append(expected_version, records)` 以乐观版本并发控制做原子提交；
- `acquire_run_lock` 提供会话级独占运行锁（跨进程）；
- 不序列化 coroutine/socket/进程句柄。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from coding_agent.domain.errors import HarnessError

__all__ = [
    "SESSION_SCHEMA_VERSION",
    "SessionAppend",
    "SessionCorruptionError",
    "SessionRecord",
    "SessionSchemaVersionError",
    "SessionStore",
    "SessionStoreError",
    "SummaryRecord",
    "SummaryVersionConflictError",
    "UnknownSessionRecordError",
    "VersionConflictError",
]

SESSION_SCHEMA_VERSION = 1


class SessionStoreError(HarnessError):
    """会话存储错误基类（协议契约：实现层必须抛这些类型）。"""


class UnknownSessionRecordError(SessionStoreError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"unknown session record {session_id!r}")
        self.session_id = session_id


class VersionConflictError(SessionStoreError):
    """乐观版本冲突（并发写者或陈旧版本）。"""

    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"session {session_id!r} version conflict: expected {expected}, actual {actual}"
        )
        self.session_id = session_id
        self.expected = expected
        self.actual = actual


class SummaryVersionConflictError(SessionStoreError):
    def __init__(self, session_id: str, version: int) -> None:
        super().__init__(f"session {session_id!r} already has summary version {version}")
        self.session_id = session_id
        self.version = version


class SessionSchemaVersionError(SessionStoreError):
    """schema 版本不兼容：显式失败，不自动迁移。"""

    def __init__(self, session_id: str, found: int, expected: int) -> None:
        super().__init__(
            f"session {session_id!r} uses schema_version {found}, expected {expected};"
            " migration rejected"
        )
        self.session_id = session_id
        self.found = found
        self.expected = expected


class SessionCorruptionError(SessionStoreError):
    """会话记录损坏（中段破损 JSON / 结构不符）。"""


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    """一次压缩记录的可持久投影（阶段 14 的 CompactionRecord 摘要字段）。"""

    summary_version: int
    covered_through_id: str
    structured_fields: Mapping[str, Any]
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionAppend:
    """一次原子追加：消息（按序）+ 摘要记录。"""

    messages: tuple[Mapping[str, Any], ...] = ()
    summaries: tuple[SummaryRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """已提交会话快照（version 单调递增）。"""

    session_id: str
    workspace: str
    schema_version: int
    version: int
    messages: tuple[Mapping[str, Any], ...]
    summaries: tuple[SummaryRecord, ...] = ()
    run_metadata: Mapping[str, Any] = field(default_factory=dict)
    filtered_tail: int = 0  # 尾部半写记录被过滤的数量（lenient 加载）


class SessionStore(Protocol):
    def create(self, workspace: Path | str) -> SessionRecord: ...

    def load(self, session_id: str) -> SessionRecord: ...

    def append(self, session_id: str, expected_version: int, records: SessionAppend) -> int: ...

    def acquire_run_lock(self, session_id: str, run_id: str) -> bool: ...

    def release_run_lock(self, session_id: str, run_id: str) -> None: ...

    def close(self) -> None: ...
