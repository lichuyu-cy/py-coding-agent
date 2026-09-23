"""Tool Registry：集中维护工具实现与模型可见声明。

- 只做注册/查找/快照；不执行、不授权工具；
- 名称规范化（strip + 小写命名规则）与唯一性、schema 合法性在注册时一次性检查；
- `snapshot()` 返回不可变快照（版本号单调），同一 turn 的声明与执行查找使用同一快照，
  注册表后续变更不影响已取出的快照（动态删除中的活动调用仍可解析到原工具，
  未知工具的结构化拒绝由 Pipeline 负责）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from coding_agent.domain.errors import HarnessError
from coding_agent.ports.provider import ToolDefinition
from coding_agent.ports.tool import Tool, ToolSpec

_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_\-]{0,63}")


class ToolRegistryError(HarnessError):
    """注册表配置错误（非法名称、非法 schema、重复注册）。"""


class UnknownToolError(HarnessError):
    """请求了未注册的工具。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool {name!r}")
        self.name = name


def normalize_tool_name(name: object) -> str:
    """规范化工具名：strip 后必须满足小写命名规则。"""
    if not isinstance(name, str):
        raise ToolRegistryError(f"tool name must be a string, got {type(name).__name__}")
    normalized = name.strip()
    if not _NAME_PATTERN.fullmatch(normalized):
        raise ToolRegistryError(
            f"invalid tool name {name!r}: expected [a-z][a-z0-9_-]{{0,63}}"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """注册表中的一条记录：规范化名称 + 实现 + 声明。"""

    name: str
    tool: Tool
    spec: ToolSpec


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """不可变注册表快照；声明与执行查找共享同一版本。"""

    version: int
    tools: tuple[RegisteredTool, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(record.name for record in self.tools)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(record.spec for record in self.tools)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """按快照稳定顺序导出模型可见声明。"""
        return tuple(
            ToolDefinition(
                name=record.spec.name,
                description=record.spec.description,
                json_schema=dict(record.spec.json_schema),
            )
            for record in self.tools
        )

    def get(self, name: str) -> RegisteredTool | None:
        normalized = name.strip()
        for record in self.tools:
            if record.name == normalized:
                return record
        return None


class ToolRegistry:
    """注册、查找与快照；版本号随每次变更单调递增。"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def register(self, tool: Tool) -> RegisteredTool:
        spec = tool.spec()
        name = normalize_tool_name(spec.name)
        self._validate_spec(name, spec)
        if name in self._tools:
            raise ToolRegistryError(f"duplicate tool registration: {name!r}")
        record = RegisteredTool(name=name, tool=tool, spec=spec)
        self._tools[name] = record
        self._version += 1
        return record

    def unregister(self, name: str) -> None:
        normalized = normalize_tool_name(name)
        if normalized not in self._tools:
            raise UnknownToolError(normalized)
        del self._tools[normalized]
        self._version += 1

    def get(self, name: str) -> Tool:
        """按名称取工具实现；未知工具抛 UnknownToolError。"""
        normalized = name.strip()
        record = self._tools.get(normalized)
        if record is None:
            raise UnknownToolError(normalized)
        return record.tool

    def definitions(self, allowed_names: Sequence[str] | None = None) -> tuple[ToolDefinition, ...]:
        """按注册顺序导出声明；allowed_names 过滤时对未注册名称报错（配置错误尽早暴露）。"""
        if allowed_names is None:
            return self.snapshot().definitions()
        wanted = {normalize_tool_name(name) for name in allowed_names}
        missing = sorted(name for name in wanted if name not in self._tools)
        if missing:
            raise UnknownToolError(missing[0])
        records = tuple(record for record in self._tools.values() if record.name in wanted)
        return RegistrySnapshot(version=self._version, tools=records).definitions()

    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(version=self._version, tools=tuple(self._tools.values()))

    @staticmethod
    def _validate_spec(name: str, spec: ToolSpec) -> None:
        if not isinstance(spec.description, str) or not spec.description.strip():
            raise ToolRegistryError(f"tool {name!r} description must be a non-empty string")
        schema = spec.json_schema
        if not isinstance(schema, Mapping):
            raise ToolRegistryError(f"tool {name!r} json_schema must be an object")
        if schema.get("type") != "object":
            raise ToolRegistryError(f"tool {name!r} json_schema.type must be 'object'")
        try:
            json.dumps(dict(schema))
        except (TypeError, ValueError) as exc:
            raise ToolRegistryError(f"tool {name!r} json_schema is not JSON-serializable") from exc
