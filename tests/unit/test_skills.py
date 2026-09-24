"""阶段 12 单测：技能发现、去重、按需加载与显式错误。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coding_agent.context.skills import (
    SKILL_FILE_NAME,
    MAX_SKILL_BYTES,
    SkillError,
    SkillRegistry,
    UnknownSkillError,
)

FRONTMATTER = """---
name: {name}
description: {description}
version: {version}
---
{body}
"""


def make_skill(root: Path, name: str, *, description: str = "a skill", body: str = "BODY", version: str = "1") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILE_NAME).write_text(
        FRONTMATTER.format(name=name, description=description, version=version, body=body),
        encoding="utf-8",
    )
    return directory


def try_dir_link(target_dir: Path, link: Path) -> bool:
    try:
        link.symlink_to(target_dir, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target_dir)], capture_output=True, check=False
        )
        return result.returncode == 0 and link.exists()
    return False


class TestDiscovery:
    def test_metadata_listing_and_section(self, tmp_path: Path) -> None:
        make_skill(tmp_path, "beta", description="second skill")
        make_skill(tmp_path, "alpha", description="first skill")
        registry = SkillRegistry([tmp_path])
        names = [meta.name for meta in registry.list_metadata()]
        assert names == ["alpha", "beta"]  # 目录内按名称排序
        section = registry.metadata_section()
        assert section is not None
        assert "alpha: first skill" in section.content
        assert "beta: second skill" in section.content
        assert "BODY" not in section.content  # 元数据段不含正文

    def test_empty_directory_has_no_section(self, tmp_path: Path) -> None:
        registry = SkillRegistry([tmp_path])
        assert registry.list_metadata() == ()
        assert registry.metadata_section() is None

    def test_missing_directory_is_ignored(self, tmp_path: Path) -> None:
        registry = SkillRegistry([tmp_path / "nope"])
        assert registry.list_metadata() == ()

    def test_duplicate_names_first_directory_wins(self, tmp_path: Path) -> None:
        first = tmp_path / "a"
        second = tmp_path / "b"
        make_skill(first, "same", description="from first")
        make_skill(second, "same", description="from second")
        registry = SkillRegistry([first, second])
        metadata = registry.list_metadata()
        assert len(metadata) == 1
        assert metadata[0].description == "from first"

    def test_no_frontmatter_uses_directory_name(self, tmp_path: Path) -> None:
        directory = tmp_path / "plain"
        directory.mkdir()
        (directory / SKILL_FILE_NAME).write_text("# Plain skill\nbody text", encoding="utf-8")
        registry = SkillRegistry([tmp_path])
        metadata = registry.list_metadata()[0]
        assert metadata.name == "plain"
        assert "Plain skill" in metadata.description

    def test_unclosed_frontmatter_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / SKILL_FILE_NAME).write_text("---\nname: broken\n", encoding="utf-8")
        with pytest.raises(SkillError, match="not closed"):
            SkillRegistry([tmp_path])

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / f"{tmp_path.name}-outside-skill"
        outside.mkdir(exist_ok=True)
        (outside / SKILL_FILE_NAME).write_text("name: x\ndescription: y", encoding="utf-8")
        search = tmp_path / "skills"
        search.mkdir()
        link = search / "escaped"
        if not try_dir_link(outside, link):
            pytest.skip("directory links not available on this platform/account")
        with pytest.raises(SkillError, match="escapes"):
            SkillRegistry([search])


class TestLoadBody:
    def test_load_selected_body(self, tmp_path: Path) -> None:
        make_skill(tmp_path, "alpha", body="Alpha instructions here.")
        registry = SkillRegistry([tmp_path])
        body = registry.load_body("alpha")
        assert body.content == "Alpha instructions here."
        assert body.metadata.name == "alpha"

    def test_unknown_skill_rejected(self, tmp_path: Path) -> None:
        registry = SkillRegistry([tmp_path])
        with pytest.raises(UnknownSkillError):
            registry.load_body("missing")

    def test_deleted_file_reported(self, tmp_path: Path) -> None:
        directory = make_skill(tmp_path, "alpha")
        registry = SkillRegistry([tmp_path])
        (directory / SKILL_FILE_NAME).unlink()
        with pytest.raises(SkillError, match="missing"):
            registry.load_body("alpha")

    def test_too_large_rejected(self, tmp_path: Path) -> None:
        make_skill(tmp_path, "big", body="x" * (MAX_SKILL_BYTES + 100))
        with pytest.raises(SkillError, match="exceed"):
            SkillRegistry([tmp_path])

    def test_invalid_encoding_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "bad"
        directory.mkdir()
        (directory / SKILL_FILE_NAME).write_bytes(b"\xff\xfe\x00broken")
        with pytest.raises(SkillError):
            SkillRegistry([tmp_path])

    def test_select_dedupes_and_keeps_order(self, tmp_path: Path) -> None:
        make_skill(tmp_path, "alpha", body="A")
        make_skill(tmp_path, "beta", body="B")
        registry = SkillRegistry([tmp_path])
        bodies = registry.select_for_turn(["beta", "alpha", "beta"])
        assert [b.metadata.name for b in bodies] == ["beta", "alpha"]

    def test_select_unknown_rejected(self, tmp_path: Path) -> None:
        make_skill(tmp_path, "alpha")
        registry = SkillRegistry([tmp_path])
        with pytest.raises(UnknownSkillError):
            registry.select_for_turn(["alpha", "gamma"])
