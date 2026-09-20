# -*- coding: utf-8 -*-
"""AI Agent 技能起草：SKILL.md，不是流水线插件。"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.modules.ai.skills.skill_authoring import _lint, _propose_agent_skill, install_proposed
from app.modules.audit.models import AuditLog
from app.modules.harness import skills as harness_skills
from app.modules.harness.models import (
    HarnessComponent,
    HarnessDependency,
    HarnessLifecycleAudit,
    HarnessRuntime,
    HarnessToolInvocation,
    HarnessVersion,
)
from app.modules.settings.models import PlatformSetting


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        HarnessComponent.__table__,
        HarnessVersion.__table__,
        HarnessDependency.__table__,
        HarnessRuntime.__table__,
        HarnessToolInvocation.__table__,
        HarnessLifecycleAudit.__table__,
        PlatformSetting.__table__,
        AuditLog.__table__,
    ):
        table.create(engine)
    return Session(engine)


GOOD_MD = """# 查询流水线发布状态

## 何时使用
用户问流水线或发布的状态：正在发布、成功、失败、取消、驳回，或笼统问「发布得怎么样」。

## 何时不要使用
用户要发布流水线，或要做流水线步骤插件。

## 硬规则
调用一次 `get_release_status`。按用户这一句传 `status`：
- 正在发布 / 在跑 → `running`
- 失败 → `failed`
- 成功 → `success`
- 取消 → `cancelled`
- 没说筛哪种、也没说要历史 → 不传 status，只查该流水线**最近一次执行**
不要写死只查 running。不要把 status=all 理解成把执行历史全拉出来。
不要 `list_pipelines`，不要对每一条再 `get_pipeline`。
不要把节点文件下发说成业务发布。
"""


def test_lint_rejects_pipeline_plugin_source() -> None:
    errors = _lint("demo-skill", "import release_atom_sdk as sdk\n## 何时使用\n")
    assert any("流水线插件" in item for item in errors)


def test_lint_rejects_all_as_full_history() -> None:
    errors = _lint(
        "query-pipeline-status",
        "## 何时使用\n调用 `get_release_status`，没说筛哪种 → `all`\n",
    )
    assert any("最近一次" in item for item in errors)


def test_status_skill_body_overrides_history_dump() -> None:
    from app.modules.harness.skills import body_for_model

    old = "## 何时使用\n调用 `get_release_status`，没说筛哪种就 all。\n"
    text = body_for_model("query-pipeline-status", old)
    assert "最近一次" in text
    assert "list_pipelines" in text
    assert body_for_model("rp-release", old) == old


def test_lint_rejects_invented_tools_and_reserved_name() -> None:
    assert any("内置" in item or "release" in item for item in _lint("rp-release", "## 何时使用\n调用 `get_release_status`"))
    errors = _lint("query-running", "## 何时使用\n先调 `query_running_pipelines`")
    assert any("发明" in item for item in errors)


def test_lint_allows_existing_tool_parameters() -> None:
    """`notice_id` 是 get_notification 的参数，不是虚构工具。"""
    errors = _lint(
        "read-unread-notifications",
        "## 何时使用\n调用 `list_notifications` 只列标题。"
        "用户点某条再 `get_notification`，传入 `notice_id`。\n",
    )
    assert not any("发明" in item for item in errors)


def test_lint_allows_response_field_names() -> None:
    """说明书写返回字段 `created_at` / `unread_count` 不能当成虚构工具。"""
    errors = _lint(
        "read-unread-notifications",
        "## 何时使用\n调用 `list_notifications`，只回复 `title`，附上 `detail_url`。"
        "不要把 `content`、`created_at`、`unread_count`、`is_read` 当工具去调。\n",
    )
    assert not any("发明" in item for item in errors)


def test_propose_returns_confirm_card() -> None:
    out = _propose_agent_skill(
        None,
        SimpleNamespace(id=1, username="admin", is_admin=True),
        {
            "name": "query-pipeline-status",
            "display_name": "查询流水线状态",
            "description": "按用户问法查发布状态，不限于运行中",
            "skill_md": GOOD_MD,
            "intent": "实现一个agent技能，查询流水线的状态",
        },
    )
    assert out["_action"] == "confirm_agent_skill"
    assert out["payload"]["name"] == "query-pipeline-status"
    assert "get_release_status" in out["payload"]["skill_md"]


def test_confirm_installs_agent_skill_not_plugin() -> None:
    db = _db()
    try:
        installed = install_proposed(
            db,
            {
                "name": "query-pipeline-status",
                "version": "1.0.0",
                "display_name": "查询流水线状态",
                "description": "按用户问法查发布状态",
                "skill_md": GOOD_MD,
            },
            actor_id=1,
            actor_name="admin",
        )
        assert installed["name"] == "query-pipeline-status"
        names = [item["name"] for item in harness_skills.catalog(db)]
        assert "query-pipeline-status" in names
        prompt = harness_skills.system_prompt(db)
        assert "query-pipeline-status" in prompt
    finally:
        db.close()


def test_non_admin_can_propose_and_install_personal_skill() -> None:
    """普通用户可以起草并安装，技能只出现在自己的目录里。"""
    out = _propose_agent_skill(
        None,
        SimpleNamespace(id=7, username="dev", is_admin=False),
        {
            "name": "read-unread-notifications",
            "display_name": "读取未读通知",
            "description": "列未读标题，点名某条再展开",
            "skill_md": "## 何时使用\n用户问未读消息、通知中心时调用 `list_notifications`。\n",
            "intent": "读取未读消息",
        },
    )
    assert out["_action"] == "confirm_agent_skill"
    db = _db()
    try:
        installed = install_proposed(
            db,
            out["payload"],
            actor_id=7,
            actor_name="dev",
            owner_user_id=7,
        )
        assert installed["name"] == "read-unread-notifications"
        assert installed["personal"] is True
        assert "read-unread-notifications" not in [item["name"] for item in harness_skills.catalog(db)]
        mine = harness_skills.catalog(db, viewer_id=7)
        assert "read-unread-notifications" in [item["name"] for item in mine]
        others = harness_skills.catalog(db, viewer_id=8)
        assert "read-unread-notifications" not in [item["name"] for item in others]
        prompt = harness_skills.system_prompt(db, viewer_id=7)
        assert "read-unread-notifications" in prompt
        assert "read-unread-notifications" not in harness_skills.system_prompt(db, viewer_id=8)
        loaded = harness_skills.load_for_model(db, "read-unread-notifications", viewer_id=7)
        assert loaded.get("code") != "SKILL_NOT_FOUND"
        missing = harness_skills.load_for_model(db, "read-unread-notifications", viewer_id=8)
        assert missing.get("code") == "SKILL_NOT_FOUND"
        row = db.get(HarnessComponent, installed["id"])
        assert harness_skills.viewer_can_see(row, viewer_id=7, is_admin=False)
        assert not harness_skills.viewer_can_see(row, viewer_id=8, is_admin=False)
        assert harness_skills.viewer_can_see(row, viewer_id=8, is_admin=True)
    finally:
        db.close()


def test_personal_skill_reinstall_bumps_version() -> None:
    """同一条个人技能再点一次确认，不能撞「同版本不可变」。"""
    payload = {
        "name": "read-unread-notifications",
        "version": "1.0.0",
        "display_name": "读取未读通知",
        "description": "列未读标题",
        "skill_md": "## 何时使用\n调用 `list_notifications`。\n",
    }
    db = _db()
    try:
        first = install_proposed(db, payload, actor_id=7, actor_name="dev", owner_user_id=7)
        payload["skill_md"] = "## 何时使用\n调用 `list_notifications`，只列标题。\n"
        second = install_proposed(db, payload, actor_id=7, actor_name="dev", owner_user_id=7)
        assert first["version"] == "1.0.0"
        assert second["version"] == "1.0.1"
        assert first["id"] == second["id"]
    finally:
        db.close()


def test_unread_query_inlines_personal_skill_and_hides_unrelated() -> None:
    """查未读时把对应个人技能正文塞进本轮；不相干的技能不要整表进目录。"""
    db = _db()
    try:
        install_proposed(
            db,
            {
                "name": "read-unread-notifications",
                "version": "1.0.0",
                "display_name": "读取未读通知",
                "description": "列未读标题，点名某条再展开",
                "skill_md": "## 何时使用\n用户问未读消息、通知中心时调用 `list_notifications`，只回复 title 和 detail_url。\n",
            },
            actor_id=7,
            actor_name="dev",
            owner_user_id=7,
        )
        install_proposed(
            db,
            {
                "name": "nightly-release-digest",
                "version": "1.0.0",
                "display_name": "晚间发布摘要",
                "description": "把今晚要上线的流水线整理成给业务看的白话",
                "skill_md": "## 何时使用\n用户要今晚发布同步稿时加载。\n",
            },
            actor_id=7,
            actor_name="dev",
            owner_user_id=7,
        )
        prompt = harness_skills.system_prompt(
            db, viewer_id=7, message="查一下我的未读消息"
        )
        assert "read-unread-notifications" in prompt
        assert "只回复 title 和 detail_url" in prompt
        assert "本轮已加载：`read-unread-notifications`" in prompt
        assert "nightly-release-digest" not in prompt
        other = harness_skills.system_prompt(db, viewer_id=7, message="帮我发布 order-service")
        assert "read-unread-notifications" not in other
        assert "`rp-release`" in other
    finally:
        db.close()


def test_pack_roundtrip() -> None:
    data = harness_skills.pack_skill_archive(
        name="query-pipeline-status",
        version="1.0.0",
        display_name="查询流水线状态",
        description="按用户问法查发布状态",
        body=GOOD_MD,
    )
    package = harness_skills.parse_package(data)
    assert package.manifest.kind == "agent-skill"
    assert package.manifest.name == "query-pipeline-status"
    assert "get_release_status" in package.body


def test_colloquial_uninstall_finds_personal_inbox_skill() -> None:
    """用户说「消息通知查看」应对上「读取未读通知」，并出卸载确认卡。"""
    from app.modules.ai.skills.skill_lifecycle import apply_lifecycle, _handler

    db = _db()
    user = SimpleNamespace(id=7, username="dev", is_admin=False)
    try:
        install_proposed(
            db,
            {
                "name": "read-unread-notifications",
                "version": "1.0.0",
                "display_name": "读取未读通知",
                "description": "列未读标题，点名某条再展开",
                "skill_md": "## 何时使用\n调用 `list_notifications`。\n",
            },
            actor_id=7,
            actor_name="dev",
            owner_user_id=7,
        )
        out = _handler(
            db,
            user,
            {"action": "uninstall", "intent": "卸载消息通知查看这个技能"},
        )
        assert out.get("_action") == "confirm_agent_skill_lifecycle"
        assert out["payload"]["name"] == "read-unread-notifications"
        changed = apply_lifecycle(db, user, out["payload"])
        assert changed["done"] == "已卸载"
        visible = [
            item["name"]
            for item in harness_skills.catalog(db, viewer_id=7, enabled_only=False)
            if item.get("status") != "uninstalled"
        ]
        assert "read-unread-notifications" not in visible
    finally:
        db.close()


def test_non_admin_cannot_uninstall_global_skill() -> None:
    """普通人只能卸自己装的个人技能，不能卸全员技能。"""
    from app.modules.ai.skills.skill_lifecycle import _handler

    db = _db()
    try:
        install_proposed(
            db,
            {
                "name": "query-pipeline-status",
                "version": "1.0.0",
                "display_name": "查询流水线状态",
                "description": "按用户问法查发布状态",
                "skill_md": GOOD_MD,
            },
            actor_id=1,
            actor_name="admin",
        )
        out = _handler(
            db,
            SimpleNamespace(id=7, username="dev", is_admin=False),
            {"action": "uninstall", "name": "query-pipeline-status"},
        )
        assert "管理员" in str(out.get("error") or "")
    finally:
        db.close()


def test_tool_retrieve_picks_lifecycle_by_examples_not_regex() -> None:
    """卸载问句靠工具说明/例句检索，不靠「卸载」正则拦截整轮对话。"""
    from app.modules.ai.tool_select import categories_for_turn, names_for_turn, rank_tools_for_message

    text = "卸载消息通知查看这个技能"
    cats = categories_for_turn(text)
    ranked = rank_tools_for_message(text, categories=cats)
    assert ranked
    assert ranked[0][1] == "propose_agent_skill_lifecycle"
    names = names_for_turn(text, categories=cats)
    assert names is not None
    assert "propose_agent_skill_lifecycle" in names
    assert "list_notifications" not in names


def test_tool_retrieve_picks_lifecycle_for_install_unread_skill() -> None:
    """「安装一下读取未读通知」没有「技能」二字，仍应发给 lifecycle，不要 list_notifications。"""
    from app.modules.ai.tool_select import categories_for_turn, names_for_turn, rank_tools_for_message

    text = "安装一下读取未读通知"
    cats = categories_for_turn(text)
    assert "authoring" in cats
    ranked = rank_tools_for_message(text, categories=cats)
    assert ranked
    assert ranked[0][1] == "propose_agent_skill_lifecycle"
    names = names_for_turn(text, categories=cats)
    assert names is not None
    assert "propose_agent_skill_lifecycle" in names
    assert "list_notifications" not in names


def test_reinstall_uninstalled_personal_inbox_skill() -> None:
    """卸掉个人技能后再说安装，应出安装确认卡，确认后目录回来且健康为正常。"""
    from sqlalchemy import select

    from app.modules.ai.skills.skill_lifecycle import apply_lifecycle, _handler
    from app.modules.harness import lifecycle
    from app.modules.harness.models import HarnessRuntime

    lifecycle.initialize_builtin_providers()
    db = _db()
    user = SimpleNamespace(id=7, username="dev", is_admin=False)
    try:
        installed = install_proposed(
            db,
            {
                "name": "read-unread-notifications",
                "version": "1.0.0",
                "display_name": "读取未读通知",
                "description": "列未读标题，点名某条再展开",
                "skill_md": "## 何时使用\n调用 `list_notifications`。\n",
            },
            actor_id=7,
            actor_name="dev",
            owner_user_id=7,
        )
        removed = _handler(
            db,
            user,
            {"action": "uninstall", "name": "读取未读通知"},
        )
        apply_lifecycle(db, user, removed["payload"])
        out = _handler(
            db,
            user,
            {"action": "enable", "intent": "安装一下读取未读通知"},
        )
        assert out.get("_action") == "confirm_agent_skill_lifecycle"
        assert out["payload"]["action"] == "enable"
        assert "确认安装" in str(out.get("label") or "")
        changed = apply_lifecycle(db, user, out["payload"])
        assert changed["done"] == "已安装"
        visible = [
            item
            for item in harness_skills.catalog(db, viewer_id=7, enabled_only=False)
            if item.get("status") != "uninstalled"
        ]
        assert any(item["name"] == "read-unread-notifications" for item in visible)
        row = next(item for item in visible if item["name"] == "read-unread-notifications")
        assert row["enabled"] is True
        runtime = db.scalar(
            select(HarnessRuntime).where(HarnessRuntime.component_id == installed["id"])
        )
        assert runtime is not None
        assert runtime.health_status == "healthy"
    finally:
        db.close()


def test_personal_skill_enable_writes_healthy_and_purge_removes_row() -> None:
    """启用技能包时健康应为正常，而不是 unknown；删除后目录里消失。"""
    from sqlalchemy import select

    from app.modules.harness import lifecycle

    lifecycle.initialize_builtin_providers()
    db = _db()
    try:
        installed = install_proposed(
            db,
            {
                "name": "read-unread-notifications",
                "version": "1.0.0",
                "display_name": "读取未读通知",
                "description": "列未读标题",
                "skill_md": "## 何时使用\n调用 `list_notifications`。\n",
            },
            actor_id=7,
            actor_name="dev",
            owner_user_id=7,
        )
        runtime = db.scalar(
            select(HarnessRuntime).where(HarnessRuntime.component_id == installed["id"])
        )
        assert runtime is not None
        assert runtime.health_status == "healthy"
        cid = installed["id"]
        lifecycle.purge(db, cid, actor_id=7, actor_name="dev")
        assert db.get(HarnessComponent, cid) is None
        names = [
            item["name"]
            for item in harness_skills.catalog(db, viewer_id=7, enabled_only=False)
        ]
        assert "read-unread-notifications" not in names
    finally:
        db.close()
