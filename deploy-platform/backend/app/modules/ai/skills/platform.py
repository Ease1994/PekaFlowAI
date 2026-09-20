"""平台只读目录：已挂上的 Tool、已安装的技能包。

技能包只能教模型怎么用现有 Tool。开发新技能时先看这里有没有对应接口，
没有就把只读 HTTP 挂成 Tool，而不是在 SKILL.md 里假装能调。
不返回参数 schema 全文，避免把整份目录塞进上下文。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.ai.registry import Skill, register

# 目录条目里描述截断长度。完整参数仍走 GET /ai/skills。
_DESC_CHARS = 200
_MAX_LIST = 80


def _clip(text: str, limit: int = _DESC_CHARS) -> str:
    raw = (text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "…"


def _keyword(params: dict) -> str:
    return str(params.get("keyword") or "").strip().lower()


def _hit(keyword: str, *parts: object) -> bool:
    if not keyword:
        return True
    blob = " ".join(str(p or "") for p in parts).lower()
    return keyword in blob


def _list_platform_tools(db: Session, current, params: dict) -> dict:
    """列出模型当前能调用的 Tool（内置 + 已启用的第三方）。"""
    del current
    from app.modules.harness import tools as harness_tools

    keyword = _keyword(params)
    rows = []
    for spec in harness_tools.catalog(db):
        if not spec.llm_visible:
            continue
        if not _hit(keyword, spec.name, spec.display_name, spec.description, spec.category):
            continue
        rows.append(
            {
                "name": spec.name,
                "description": _clip(spec.description),
                "category": spec.category,
                "risk": spec.risk,
                "source": spec.source,
                "confirm": spec.confirm,
            }
        )
        if len(rows) >= _MAX_LIST:
            break
    return {
        "tools": rows,
        "total": len(rows),
        "hint": (
            "技能包只能调用这里已有的 Tool。"
            "缺能力就给对应的只读 HTTP 挂 Tool，不要在说明书里编一个不存在的接口。"
            if rows
            else "没有匹配的 Tool"
        ),
    }


def _list_agent_skills(db: Session, current, params: dict) -> dict:
    """列出当前用户能看见的已启用 Agent 技能包（SKILL.md），不含正文。"""
    from app.modules.harness import skills as harness_skills

    keyword = _keyword(params)
    rows = []
    for item in harness_skills.catalog(db, enabled_only=True, viewer_id=getattr(current, "id", None)):
        if not _hit(keyword, item.get("name"), item.get("display_name"), item.get("description")):
            continue
        rows.append(
            {
                "name": item.get("name"),
                "display_name": item.get("display_name") or item.get("name"),
                "description": _clip(str(item.get("description") or "")),
                "version": item.get("version") or "",
                "invocation_policy": item.get("invocation_policy") or "model",
            }
        )
        if len(rows) >= _MAX_LIST:
            break
    return {
        "agent_skills": rows,
        "total": len(rows),
        "hint": (
            "这是目录。要按某项技能做事再用 skill 加载说明书；"
            "要安装、卸载、停用或启用请调用 propose_agent_skill_lifecycle，不要把本列表当成已经装上或卸掉。"
            if rows
            else "没有已启用的技能包"
        ),
    }


def _ask_user(db: Session, current, params: dict) -> dict:
    """向用户澄清还不清楚的选择。有结果但下一步不唯一时停下来问。"""
    del db, current
    question = str(params.get("question") or "").strip()
    if not question:
        return {"error": "缺少 question"}
    raw = params.get("options") or []
    labels: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())
            elif isinstance(item, dict):
                label = str(item.get("label") or item.get("id") or "").strip()
                if label:
                    labels.append(label)
    lines = [question]
    for index, label in enumerate(labels, 1):
        lines.append(f"{index}. {label}")
    return {"reply": "\n".join(lines), "_ask": True, "options": labels}


def load() -> None:
    register(
        Skill(
            name="list_platform_tools",
            description=(
                "列出助手当前能调用的 Tool（名称、用途、风险）。"
                "在问助手现在有哪些工具、能不能查通知时调用。"
                "不是装新技能，也不是列流水线插件。"
            ),
            category="meta",
            risk="read",
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "按名称或描述过滤，如 notify、审批、日志",
                    }
                },
            },
            handler=_list_platform_tools,
            examples=["助手现在有哪些工具", "有没有查通知的工具", "能调哪些 tool"],
        )
    )
    register(
        Skill(
            name="list_agent_skills",
            description=(
                "列出已启用的 Agent 技能包名称和简介。"
                "在问现在装了哪些技能、助手技能清单时调用。"
                "要安装、卸载、停用或启用某一份，改用 propose_agent_skill_lifecycle。"
            ),
            category="meta",
            risk="read",
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "按技能名或描述过滤"},
                },
            },
            handler=_list_agent_skills,
            examples=["现在装了哪些技能包", "有没有发布技能", "助手技能清单"],
        )
    )
    register(
        Skill(
            name="ask_user",
            description=(
                "向用户确认还不清楚的选择（给出可点的选项）。"
                "在工具已经查到对象、但仍有多种合法下一步时调用。"
                "不是再搜一遍目录，也不是猜测后直接执行。"
            ),
            category="meta",
            risk="read",
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问用户的那一句"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选的下一步，如 整个项目、只要测试",
                    },
                },
                "required": ["question"],
            },
            handler=_ask_user,
            examples=["整项目还是只要测试", "你指哪一台机器", "选哪条流水线"],
        )
    )
