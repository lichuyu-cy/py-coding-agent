"""Logging：结构化、脱敏的诊断日志（不记录密钥或大正文）。

- 敏感键（api_key/secret/token/password/authorization/credential/cookie）递归遮蔽为 "***"；
- 长值默认截断（>2000 字符）并显式标注，避免把代码正文/大输出写入日志；
- `correlation` 携带 run_id/session_id/event_id 等关联 ID，便于事件与日志对齐；
- sink 失败只记录失败计数，绝不向上抛出（观测不可用不打断运行）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from coding_agent.domain.messages import utc_now_rfc3339

__all__ = ["LogRecord", "StructuredLogger", "sanitize_fields"]

MAX_VALUE_CHARS = 2000
MASK = "***"

_SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "authorization",
    "credential",
    "cookie",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SENSITIVE_KEYS)


def sanitize_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """递归脱敏：敏感键遮蔽；长字符串截断标注；容器逐项处理。"""
    if not fields:
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        if _is_sensitive(str(key)):
            sanitized[key] = MASK
            continue
        sanitized[key] = _sanitize_value(value)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return sanitize_fields(value)
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + f"...[truncated {len(value) - MAX_VALUE_CHARS} chars]"
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


@dataclass(frozen=True, slots=True)
class LogRecord:
    """一条结构化日志记录。"""

    level: str
    message: str
    utc_time: str
    correlation: Mapping[str, str] = field(default_factory=dict)
    fields: Mapping[str, Any] = field(default_factory=dict)


class StructuredLogger:
    """带脱敏与关联 ID 的内存日志（可选 sink 转发）。"""

    def __init__(
        self,
        sink: Callable[[LogRecord], None] | None = None,
        *,
        max_records: int = 1000,
    ) -> None:
        self._sink = sink
        self._max_records = max_records
        self._records: list[LogRecord] = []
        self._failures = 0

    @property
    def failures(self) -> int:
        """sink 失败次数（观测缺口计数器）。"""
        return self._failures

    def records(self) -> tuple[LogRecord, ...]:
        return tuple(self._records)

    def log(
        self,
        level: str,
        message: str,
        *,
        correlation: Mapping[str, str] | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> LogRecord:
        record = LogRecord(
            level=level,
            message=message,
            utc_time=utc_now_rfc3339(),
            correlation=dict(correlation or {}),
            fields=sanitize_fields(fields),
        )
        self._records.append(record)
        if len(self._records) > self._max_records:
            del self._records[0 : len(self._records) - self._max_records]
        if self._sink is not None:
            try:
                self._sink(record)
            except Exception:  # noqa: BLE001 - 观测失败不打断运行
                self._failures += 1
        return record

    def debug(self, message: str, **kwargs: Any) -> LogRecord:
        return self.log("debug", message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> LogRecord:
        return self.log("info", message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> LogRecord:
        return self.log("warning", message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> LogRecord:
        return self.log("error", message, **kwargs)
