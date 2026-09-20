"""起草 AI Agent 技能包：只有 SKILL.md，没有可执行代码。

和流水线插件是两回事。插件会在构建机上跑任意代码；技能包只是教模型
什么时候调用哪些已经存在的内置工具。起草助手说明书走本工具，
不要落到 propose_plugin_draft。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.ai.playbooks import all_playbooks
from app.modules.ai.registry import Skill, all_skills, register
from app.modules.ai.skills.base import card
from app.modules.harness import skills as harness_skills
from app.modules.harness.models import HarnessComponent, HarnessVersion
from app.modules.harness.packages import _NAME_RE

_PLUGIN_MARKERS = ("release_atom_sdk", "task.py", "task.json", "RELEASE_ATOM_INPUT")
_TOOL_TOKEN = re.compile(r"`([a-z][a-z0-9_]{2,40})`")
# 说明书里会把返回字段写成 `created_at`，不能一律当虚构工具。
# 真正要拦的是「调用一个平台没有的 list_/query_ 接口」。
_TOOL_NAME_PREFIXES = (
    "list_",
    "get_",
    "propose_",
    "apply_",
    "lint_",
    "create_",
    "delete_",
    "set_",
    "mark_",
    "query_",
    "fetch_",
    "open_",
    "read_",
    "write_",
    "start_",
    "stop_",
    "update_",
    "search_",
    "load_",
    "save_",
    "confirm_",
    "install_",
    "uninstall_",
    "enable_",
    "disable_",
)


def _known_tools() -> set[str]:
    """已有工具名。说明书里反引号里的标识必须落在这里，否则算发明接口。"""
    from app.modules.ai.skills import load_all

    load_all()
    return {item.name for item in all_skills()} | {"skill"}


def _known_param_names() -> set[str]:
    """已有工具的参数名。SKILL.md 里会写 `notice_id` 这种，不能当成虚构工具。"""
    from app.modules.ai.skills import load_all

    load_all()
    names: set[str] = set()
    for item in all_skills():
        schema = item.parameters or {}
        if not isinstance(schema, dict):
            continue
        props = schema.get("properties") or {}
        if isinstance(props, dict):
            names.update(str(k) for k in props)
        required = schema.get("required") or []
        if isinstance(required, list):
            names.update(str(key) for key in required)
    return names


def _has_when_section(text: str) -> bool:
    """说明书必须能回答「这份技能什么时候加载」。frontmatter 或正文小节均可。"""
    if "何时使用" in text or "什么时候" in text:
        return True
    if "## Workflow" in text or "## 流程" in text:
        return True
    return bool(re.search(r"^description\s*:", text, re.M))


def _looks_like_tool_name(token: str) -> bool:
    """反引号里带下划线的不一定是工具：`notice_id`、`created_at` 是字段。"""
    return any(token.startswith(prefix) for prefix in _TOOL_NAME_PREFIXES)


def _bump_patch(version: str) -> str:
    """把 1.0.0 变成 1.0.1。解析不了就在末尾加 .1，保证一定能错开。"""
    parts = [p.strip() for p in (version or "").split(".") if p.strip()]
    if len(parts) >= 3 and parts[2].isdigit():
        parts[2] = str(int(parts[2]) + 1)
        return ".".join(parts)
    return (version or "1.0.0") + ".1"


def _unused_version(db: Session, component_name: str, requested: str) -> str:
    """同一条个人技能再确认一次时，已占用的版本号自动往上加，避免撞「同版本不可变」。"""
    version = (requested or "").strip() or "1.0.0"
    component = db.scalar(
        select(HarnessComponent).where(
            HarnessComponent.kind == "agent-skill",
            HarnessComponent.name == component_name,
        )
    )
    if component is None:
        return version
    used = set(
        db.scalars(select(HarnessVersion.version).where(HarnessVersion.component_id == component.id)).all()
    )
    while version in used:
        version = _bump_patch(version)
    return version


def _reserved_names() -> set[str]:
    names = {item.name for item in all_playbooks()}
    names.add("skill")
    return names


def _lint(name: str, body: str) -> list[str]:
    errors: list[str] = []
    if not _NAME_RE.fullmatch(name):
        errors.append("name 只能是小写字母、数字、点、下划线、连字符，如 query-pipeline-status")
    if name in _reserved_names() or name.startswith("rp-"):
        errors.append("不能占用平台内置技能名（rp-*）")
    text = (body or "").strip()
    if not text:
        errors.append("SKILL.md 不能为空")
    elif len(text) > harness_skills.MAX_BODY_CHARS:
        errors.append(f"SKILL.md 不能超过 {harness_skills.MAX_BODY_CHARS} 字")
    if any(marker in text for marker in _PLUGIN_MARKERS):
        errors.append("这是流水线插件的写法。AI Agent 技能不要写代码，请改用 propose_plugin_draft")
    if not _has_when_section(text):
        errors.append("SKILL.md 要写清何时使用：YAML description，或「何时使用」/ Workflow 小节")
    known = _known_tools() | _known_param_names()
    invented = sorted(
        {
            token
            for token in _TOOL_TOKEN.findall(text)
            if token not in known and _looks_like_tool_name(token)
        }
    )
    if invented:
        errors.append("不要发明工具名：" + "、".join(invented) + "。只能教模型调用已有内置工具")
    if "get_release_status" in text and re.search(r"没说筛哪种.{0,20}`?all`?", text):
        errors.append("没点名筛法时默认查最近一次执行，不要把 status=all 当成把历史全拉出来")
    return errors


def _propose_agent_skill(db: Session, current, params: dict) -> dict:
    del db
    name = str(params.get("name") or "").strip().lower()
    display_name = str(params.get("display_name") or "").strip() or name
    description = str(params.get("description") or "").strip()
    version = str(params.get("version") or "1.0.0").strip() or "1.0.0"
    body = str(params.get("skill_md") or params.get("body") or "").strip()
    examples = str(params.get("examples_md") or "").strip()
    intent = str(params.get("intent") or "").strip()
    policy = str(params.get("invocation_policy") or "model").strip() or "model"
    if policy not in harness_skills.INVOCATION_POLICIES:
        policy = "model"

    errors = _lint(name, body)
    if errors:
        return {"ok": False, "message": "技能包没写合格，请按下面改完再起草", "findings": errors}

    preview = body if len(body) <= 1800 else body[:1800] + "\n…（完整正文在确认后写入技能库）"
    if getattr(current, "is_admin", False):
        where = "确认后会出现在 **技能库 → Agent 技能**，全员助手都能用。"
    else:
        where = "确认后只装到**你自己的助手**，别人看不到。技能库里也能看到自己装的。"
    reply = (
        f"准备把 **{display_name}**（`{name}`）做成 AI Agent 技能包，不是流水线插件。\n\n"
        f"{description or intent}\n\n"
        f"{where}"
        "技能包里没有可执行代码，只能调用你已有权限的工具。\n\n"
        f"SKILL.md 预览：\n```markdown\n{preview}\n```"
    )
    return card(
        "confirm_agent_skill",
        "确认安装该 Agent 技能",
        {
            "name": name,
            "version": version,
            "display_name": display_name,
            "description": description or intent,
            "skill_md": body,
            "examples_md": examples,
            "invocation_policy": policy,
            "intent": intent,
        },
        reply,
    )


def install_proposed(
    db: Session,
    payload: dict,
    *,
    actor_id: int | None,
    actor_name: str,
    owner_user_id: int | None = None,
) -> dict:
    """把确认卡上的技能包写入组件库。

    owner_user_id 有值时只给该用户用（组件名加 u{id}- 前缀）；管理员安装不传，全员可见。
    """
    name = str(payload.get("name") or "").strip().lower()
    body = str(payload.get("skill_md") or "").strip()
    errors = _lint(name, body)
    if errors:
        raise BizException.bad_request("；".join(errors[:3]))
    component_name = (
        harness_skills.personal_component_name(owner_user_id, name) if owner_user_id else name
    )
    version = _unused_version(db, component_name, str(payload.get("version") or "1.0.0"))
    data = harness_skills.pack_skill_archive(
        name=component_name,
        version=version,
        display_name=str(payload.get("display_name") or name),
        description=str(payload.get("description") or ""),
        body=body,
        examples=str(payload.get("examples_md") or ""),
        invocation_policy=str(payload.get("invocation_policy") or "model"),
        public_name=name if owner_user_id else "",
        owner_user_id=owner_user_id,
    )
    row = harness_skills.install_package(db, data, actor_id=actor_id, actor_name=actor_name)
    return {
        "id": row.id,
        "name": name,
        "display_name": str(payload.get("display_name") or name),
        "version": version,
        "personal": bool(owner_user_id),
    }


def load() -> None:
    register(
        Skill(
            name="propose_agent_skill",
            description=(
                "起草还不在库里的 Agent 技能包（只有 SKILL.md），出确认卡。"
                "在要给助手加技能、写 SKILL.md 时调用。"
                "已有技能装/卸改用 propose_agent_skill_lifecycle，流水线插件改用 propose_plugin_draft。"
            ),
            category="authoring",
            risk="write",
            confirm=True,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能标识，如 query-pipeline-status",
                    },
                    "display_name": {"type": "string", "description": "页面上的中文名"},
                    "description": {
                        "type": "string",
                        "description": "目录摘要：做什么 + 何时用 + 不是什么，第三人称，带检索关键词",
                    },
                    "version": {"type": "string", "description": "默认 1.0.0"},
                    "skill_md": {
                        "type": "string",
                        "description": (
                            "SKILL.md 全文。YAML frontmatter 写 name 和 description（做什么+何时用+不是什么）；"
                            "正文用 Workflow / Do not use / Examples。不要写 Python / task.py。不要发明工具名。"
                            "查状态类：没说要历史时只查最近一次，禁止写死只查 running。"
                        ),
                    },
                    "examples_md": {"type": "string", "description": "可选对话样例"},
                    "invocation_policy": {
                        "type": "string",
                        "description": "model / user / always，默认 model",
                    },
                    "intent": {"type": "string", "description": "用户原话"},
                },
                "required": ["name", "display_name", "skill_md"],
            },
            handler=_propose_agent_skill,
            examples=["帮我实现一个 agent 技能，查询流水线状态", "给助手加一个能查发布成功失败和进行中的技能", "写一份 SKILL.md"],
        )
    )
