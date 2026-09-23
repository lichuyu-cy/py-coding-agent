"""阶段 06 单测：Tool Registry 的注册、声明导出与快照版本语义。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from coding_agent.ports.tool import ToolContext, ToolExecution, ToolSpec
from coding_agent.tools.registry import (
    ToolRegistry,
    ToolRegistryError,
    UnknownToolError,
    normalize_tool_name,
)


class StubTool:
    """可配置的最小工具，用于注册表测试。"""

    def __init__(self, name: str, *, description: str = "stub tool", schema: Any = None) -> None:
        self._name = name
        self._description = description
        self._schema = schema if schema is not None else _object_schema()

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self._name, description=self._description, json_schema=self._schema)

    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution:
        return ToolExecution(output=f"{self._name} ran")


def _object_schema(**extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
    schema.update(extra)
    return schema


class TestRegistration:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        tool = StubTool("read")
        record = registry.register(tool)
        assert record.name == "read"
        assert registry.get("read") is tool
        assert registry.version == 1

    def test_name_normalized_by_strip(self) -> None:
        registry = ToolRegistry()
        tool = StubTool("  read  ")
        record = registry.register(tool)
        assert record.name == "read"
        assert registry.get("read") is tool
        assert registry.snapshot().get("  read ") is not None

    def test_duplicate_registration_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(StubTool("read"))
        with pytest.raises(ToolRegistryError, match="duplicate"):
            registry.register(StubTool("read"))
        with pytest.raises(ToolRegistryError, match="duplicate"):
            registry.register(StubTool(" read "))  # 规范化后同名
        assert registry.version == 1

    @pytest.mark.parametrize("bad_name", ["", "   ", "Read", "1read", "read me", "read!", "r" * 65])
    def test_invalid_names_rejected(self, bad_name: str) -> None:
        with pytest.raises(ToolRegistryError):
            normalize_tool_name(bad_name)
        registry = ToolRegistry()
        with pytest.raises(ToolRegistryError):
            registry.register(StubTool(bad_name))

    def test_non_string_name_rejected(self) -> None:
        with pytest.raises(ToolRegistryError):
            normalize_tool_name(42)

    def test_invalid_schema_rejected(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="type"):
            registry.register(StubTool("read", schema={"properties": {}}))
        with pytest.raises(ToolRegistryError, match="type"):
            registry.register(StubTool("read", schema={"type": "array"}))
        with pytest.raises(ToolRegistryError, match="object"):
            registry.register(StubTool("read", schema=[1, 2]))
        with pytest.raises(ToolRegistryError, match="JSON"):
            registry.register(StubTool("read", schema=_object_schema(bad=object())))

    def test_empty_description_rejected(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="description"):
            registry.register(StubTool("read", description="   "))

    def test_unknown_tool_get_raises(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(UnknownToolError) as excinfo:
            registry.get("nope")
        assert excinfo.value.name == "nope"

    def test_unregister(self) -> None:
        registry = ToolRegistry()
        registry.register(StubTool("read"))
        registry.unregister("read")
        assert registry.version == 2
        with pytest.raises(UnknownToolError):
            registry.get("read")
        with pytest.raises(UnknownToolError):
            registry.unregister("read")


class TestDefinitionsAndOrder:
    def test_definitions_follow_registration_order(self) -> None:
        registry = ToolRegistry()
        for name in ("read", "write", "edit", "bash"):
            registry.register(StubTool(name))
        definitions = registry.definitions()
        assert [d.name for d in definitions] == ["read", "write", "edit", "bash"]
        assert all(d.description for d in definitions)

    def test_definitions_with_allowlist_filters_and_keeps_order(self) -> None:
        registry = ToolRegistry()
        for name in ("read", "write", "edit", "bash"):
            registry.register(StubTool(name))
        definitions = registry.definitions(allowed_names=["bash", "read"])
        assert [d.name for d in definitions] == ["read", "bash"]

    def test_allowlist_with_unknown_name_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(StubTool("read"))
        with pytest.raises(UnknownToolError):
            registry.definitions(allowed_names=["read", "missing"])

    def test_definition_schema_is_copied(self) -> None:
        registry = ToolRegistry()
        schema = _object_schema()
        registry.register(StubTool("read", schema=schema))
        definition = registry.definitions()[0]
        definition.json_schema["type"] = "tampered"  # type: ignore[index]
        assert registry.snapshot().specs()[0].json_schema["type"] == "object"


class TestSnapshot:
    def test_snapshot_version_and_lookup(self) -> None:
        registry = ToolRegistry()
        tool = StubTool("read")
        registry.register(tool)
        snapshot = registry.snapshot()
        assert snapshot.version == 1
        record = snapshot.get("read")
        assert record is not None
        assert record.tool is tool
        assert snapshot.get("missing") is None
        assert snapshot.names() == ("read",)

    def test_later_registration_does_not_change_old_snapshot(self) -> None:
        registry = ToolRegistry()
        registry.register(StubTool("read"))
        old = registry.snapshot()
        registry.register(StubTool("bash"))
        new = registry.snapshot()
        assert old.version == 1
        assert old.names() == ("read",)
        assert old.get("bash") is None
        assert new.version == 2
        assert new.names() == ("read", "bash")

    def test_in_flight_snapshot_survives_unregister(self) -> None:
        """动态删除中的活动调用：已取出的快照仍能解析到原工具（结构化拒绝由 Pipeline 负责）。"""
        registry = ToolRegistry()
        tool = StubTool("read")
        registry.register(tool)
        snapshot = registry.snapshot()
        registry.unregister("read")
        record = snapshot.get("read")
        assert record is not None
        assert record.tool is tool

    def test_same_turn_declaration_and_lookup_share_version(self) -> None:
        registry = ToolRegistry()
        registry.register(StubTool("read"))
        snapshot = registry.snapshot()
        declared = {d.name for d in snapshot.definitions()}
        record = snapshot.get("read")
        assert record is not None
        assert record.name in declared
        assert snapshot.version == registry.version
