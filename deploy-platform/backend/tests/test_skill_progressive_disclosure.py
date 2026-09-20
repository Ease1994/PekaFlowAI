"""技能走渐进披露：目录只有摘要，正文由 skill 工具加载。"""
from __future__ import annotations

from types import SimpleNamespace

from app.modules.ai.budget import tool_result_for_model
from app.modules.ai.playbooks import all_playbooks, get
from app.modules.ai.prompt import assemble_system, tool_guide
from app.modules.ai.skills import load_all
from app.modules.harness import skills, tools
from tests.test_harness_isolation import _memory_db


def test_builtin_playbooks_cover_the_jobs() -> None:
    names = {item.name for item in all_playbooks()}
    assert names == {
        "rp-catalog",
        "rp-release",
        "rp-release-control",
        "rp-access",
        "rp-node-push",
        "rp-observe",
        "rp-diagnose",
        "rp-inbox",
        "rp-approve",
        "rp-pm",
        "rp-agent-skill",
        "rp-plugin-draft",
        "rp-skill-lifecycle",
    }
    release = get("rp-release")
    assert release is not None
    assert "propose_release" in release.body
    assert "apply_pipeline_execute" in release.body
    assert "list_pipelines" in release.body
    for item in all_playbooks():
        assert item.description.strip()
        assert "Workflow" in item.body
        assert "Do not use" in item.body


def test_system_prompt_is_index_not_playbook_body() -> None:
    with _memory_db() as db:
        prompt = skills.system_prompt(db)
    assert "<available_skills>" in prompt
    assert "`rp-release`" in prompt
    assert "没有滚动 / 蓝绿 / 灰度可选" not in prompt
    loaded = skills.load_for_model(db, "rp-release")
    assert "没有滚动 / 蓝绿 / 灰度可选" in loaded["skill_content"]
    assert loaded["skill_content"].startswith("<skill_content name=\"rp-release\">")


def test_assemble_system_drops_the_old_intent_essay(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.ai.prompt._pending_attachments", lambda *a, **k: "")
    with _memory_db() as db:
        text = assemble_system(db, SimpleNamespace(id=1), {})
    assert "不要理解成申请权限" not in text
    assert "skill({name})" in text
    assert "`rp-release`" in text
    assert "没有滚动 / 蓝绿 / 灰度可选" not in text
    assert "向用户确认后使用" not in text


def test_tool_guide_stays_short() -> None:
    text = tool_guide(None)
    assert "propose_release" not in text
    assert "apply_pipeline_execute" not in text
    assert len(text) < 400


def test_matching_turn_inlines_skill_then_skips_reload(monkeypatch) -> None:
    """相关 SKILL.md 先进入上下文，再调工具；不要再 skill() 空转。"""
    from app.modules.ai.chat import _skip_already_loaded_skill
    from app.modules.harness.skills import entries_for_turn

    monkeypatch.setattr("app.modules.ai.prompt._pending_attachments", lambda *a, **k: "")
    with _memory_db() as db:
        text = assemble_system(
            db,
            SimpleNamespace(id=1),
            {},
            message="把订单服务发到测试",
        )
        entries = entries_for_turn(db, message="把订单服务发到测试")
    loaded = {item["name"] for item in entries if item.get("inlined")}
    assert "rp-release" in loaded
    assert "propose_release" in text
    assert "已加载" in text
    skipped = _skip_already_loaded_skill("skill", {"name": "rp-release"}, loaded)
    assert "已在系统提示中加载" in skipped
    assert not _skip_already_loaded_skill("skill", {"name": "rp-inbox"}, loaded)


def test_skill_tool_is_in_the_model_catalog() -> None:
    load_all()
    with _memory_db() as db:
        names = [item["function"]["name"] for item in tools.openai_tools(db)]
    assert "skill" in names
    assert names[0] == "skill"
    loaded = tools.dispatch(db, "skill", {"name": "rp-release"}, SimpleNamespace(id=1))
    assert "skill_content" in loaded
    assert "propose_release" in loaded["skill_content"]
    missing = tools.dispatch(db, "skill", {"name": "no-such-skill"}, SimpleNamespace(id=1))
    assert missing.get("code") == "SKILL_NOT_FOUND"


def test_skill_tool_result_caps_giant_body() -> None:
    from app.modules.ai.budget import SKILL_RESULT_MAX_TOKENS, estimate_tokens

    small = "hello playbook"
    rendered = skills.render_skill_content("rp-release", small, provider="builtin")
    text = tool_result_for_model(
        "skill",
        {"name": "rp-release", "content": small, "skill_content": rendered},
        max_tokens=200,
    )
    assert small in text

    body = "正文" * 8000
    rendered = skills.render_skill_content("rp-release", body, provider="builtin")
    text = tool_result_for_model(
        "skill",
        {"name": "rp-release", "content": body, "skill_content": rendered},
    )
    assert estimate_tokens(text) <= SKILL_RESULT_MAX_TOKENS + 80
    assert "正文" in text
