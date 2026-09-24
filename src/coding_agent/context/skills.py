"""Skill System：技能的发现、验证与按需加载。

- 元数据常驻：`metadata_section()` 只在 system 提示中加入名称/描述（低成本）；
- 正文按需：只有 `select_for_turn(names)` 选中的技能正文进入下一轮 Context；
- 本地可信文件：SKILL.md（可带 frontmatter 头），发现时做路径包含性与去重校验；
- 显式错误：缺文件/过大/无效编码/未知技能全部报错，不做静默修复；
- 技能内容只是提示文本，不能越过 Tool Safety（技能不改变工具治理路径）。

目录约定：`<search_dir>/<skill-name>/SKILL.md`，SKILL.md 可选头部：

    ---
    name: fix-tests
    description: How to fix failing tests
    version: 1
    ---
    正文……
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from coding_agent.context.builder import PromptSection
from coding_agent.domain.errors import HarnessError

__all__ = [
    "SKILLS_DIR",
    "SkillBody",
    "SkillError",
    "SkillMetadata",
    "SkillRegistry",
    "UnknownSkillError",
]

SKILLS_DIR = Path(".coding-agent") / "skills"
SKILL_FILE_NAME = "SKILL.md"
MAX_SKILL_BYTES = 100_000


class SkillError(HarnessError):
    """技能发现/加载失败（缺文件、过大、编码、路径越界等）。"""


class UnknownSkillError(SkillError):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown skill {name!r}")
        self.name = name


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    """技能清单项；正文不在此结构中。"""

    name: str
    description: str
    path: Path
    version: str = "1"


@dataclass(frozen=True, slots=True)
class SkillBody:
    """已加载并验证的技能正文。"""

    metadata: SkillMetadata
    content: str


def _is_within(path: Path, root: Path) -> bool:
    p = os.path.normcase(str(path))
    r = os.path.normcase(str(root))
    return p == r or p.startswith(r + os.sep)


def _parse_frontmatter(text: str, *, fallback_name: str, path: Path) -> tuple[SkillMetadata, str]:
    """解析可选的 --- 头部；返回元数据与正文。"""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end = index
                break
        if end is None:
            raise SkillError(f"{path}: frontmatter block is not closed")
        fields: dict[str, str] = {}
        for line in lines[1:end]:
            if not line.strip():
                continue
            if ":" not in line:
                raise SkillError(f"{path}: invalid frontmatter line {line!r}")
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
        body = "\n".join(lines[end + 1 :])
        name = fields.get("name") or fallback_name
        description = fields.get("description", "").strip()
        if not description:
            raise SkillError(f"{path}: frontmatter must provide a description")
        return (
            SkillMetadata(name=name, description=description, path=path, version=fields.get("version", "1")),
            body,
        )
    # 无 frontmatter：用目录名作为名称，首行作为描述
    first_line = lines[0].strip() if lines else ""
    description = first_line.lstrip("# ").strip() or "(no description)"
    return SkillMetadata(name=fallback_name, description=description, path=path, version="1"), text


class SkillRegistry:
    """在给定搜索目录中做一次发现（去重、防越界），正文按需加载。"""

    def __init__(self, search_dirs: Sequence[Path]) -> None:
        self._search_dirs = tuple(Path(directory) for directory in search_dirs)
        self._index: dict[str, SkillMetadata] = {}
        self._discover()

    # ---- 接口 ----

    def list_metadata(self) -> tuple[SkillMetadata, ...]:
        """按发现顺序（搜索目录顺序 + 目录内名称排序）返回清单。"""
        return tuple(self._index.values())

    def metadata_section(self) -> PromptSection | None:
        """常驻的轻量入口：名称与描述；无技能时返回 None。"""
        if not self._index:
            return None
        lines = [f"- {meta.name}: {meta.description}" for meta in self._index.values()]
        return PromptSection(name="skills", content="Available skills (load on demand):\n" + "\n".join(lines))

    def load_body(self, name: str) -> SkillBody:
        """加载并验证正文：UTF-8、大小上限、文件仍在原位置。"""
        metadata = self._index.get(name)
        if metadata is None:
            raise UnknownSkillError(name)
        try:
            raw = metadata.path.read_bytes()
        except FileNotFoundError as exc:
            raise SkillError(f"skill {name!r} body is missing: {metadata.path}") from exc
        except OSError as exc:
            raise SkillError(f"skill {name!r} cannot be read: {exc}") from exc
        if len(raw) > MAX_SKILL_BYTES:
            raise SkillError(
                f"skill {name!r} is {len(raw)} bytes, exceeding the {MAX_SKILL_BYTES} byte limit"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError(f"skill {name!r} is not valid UTF-8: {exc}") from exc
        metadata_reloaded, body = _parse_frontmatter(text, fallback_name=name, path=metadata.path)
        if metadata_reloaded.name != name:
            raise SkillError(
                f"skill {name!r} frontmatter name changed to {metadata_reloaded.name!r}; re-index required"
            )
        return SkillBody(metadata=metadata, content=body.strip())

    def select_for_turn(self, names: Sequence[str]) -> tuple[SkillBody, ...]:
        """选择技能正文：去重且保持选择顺序；未知技能显式报错。"""
        selected: list[SkillBody] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            selected.append(self.load_body(name))
        return tuple(selected)

    # ---- 发现 ----

    def _discover(self) -> None:
        for directory in self._search_dirs:
            if not directory.is_dir():
                continue
            root = directory.resolve()
            for child in sorted(directory.iterdir(), key=lambda p: p.name):
                if not child.is_dir():
                    continue
                skill_file = child / SKILL_FILE_NAME
                if not skill_file.is_file():
                    continue
                resolved = skill_file.resolve()
                if not _is_within(resolved, root):
                    raise SkillError(
                        f"skill file {skill_file} escapes its search directory {directory}"
                    )
                try:
                    text = resolved.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise SkillError(f"{resolved} is not valid UTF-8: {exc}") from exc
                if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
                    raise SkillError(f"{resolved} exceeds the {MAX_SKILL_BYTES} byte limit")
                metadata, _ = _parse_frontmatter(text, fallback_name=child.name, path=resolved)
                if metadata.name in self._index:
                    continue  # 去重：先发现的目录优先
                self._index[metadata.name] = metadata
