"""阶段 12 集成测试：元数据常驻、正文按需进入 Provider 快照。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.bootstrap import build_runtime
from coding_agent.context.skills import SKILLS_DIR, SKILL_FILE_NAME, UnknownSkillError
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---
{body}
"""


def install_skill(workspace: Path, name: str, *, description: str, body: str) -> None:
    directory = workspace / SKILLS_DIR / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILE_NAME).write_text(
        SKILL_TEMPLATE.format(name=name, description=description, body=body), encoding="utf-8"
    )


class TestSkillsFlow:
    async def test_selected_body_only_in_requests_others_metadata_only(self, tmp_path: Path) -> None:
        install_skill(tmp_path, "alpha", description="alpha desc", body="ALPHA-BODY-MARKER")
        install_skill(tmp_path, "beta", description="beta desc", body="BETA-BODY-MARKER")
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)

        result = await runtime.run("task", tmp_path, skills=["alpha"])

        assert result.status is RunStatus.FINISHED
        system_content = provider.requests[0].messages[0].content
        assert "alpha desc" in system_content  # 元数据常驻
        assert "beta desc" in system_content
        assert "ALPHA-BODY-MARKER" in system_content  # 选中的正文已加载
        assert "BETA-BODY-MARKER" not in system_content  # 未选中的正文不入请求

    async def test_no_selection_keeps_metadata_cheap(self, tmp_path: Path) -> None:
        install_skill(tmp_path, "alpha", description="alpha desc", body="ALPHA-BODY-MARKER")
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)

        await runtime.run("task", tmp_path)

        system_content = provider.requests[0].messages[0].content
        assert "alpha desc" in system_content
        assert "ALPHA-BODY-MARKER" not in system_content

    async def test_workspace_without_skills_dir(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        result = await runtime.run("task", tmp_path)
        assert result.status is RunStatus.FINISHED
        assert "Available skills" not in provider.requests[0].messages[0].content

    async def test_unknown_skill_selection_rejected(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        with pytest.raises(UnknownSkillError):
            await runtime.run("task", tmp_path, skills=["missing"])

    async def test_body_missing_after_selection_rejected(self, tmp_path: Path) -> None:
        install_skill(tmp_path, "alpha", description="alpha desc", body="BODY")
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        (tmp_path / SKILLS_DIR / "alpha" / SKILL_FILE_NAME).unlink()
        with pytest.raises(Exception, match="unknown skill|body is missing"):
            await runtime.run("task", tmp_path, skills=["alpha"])
