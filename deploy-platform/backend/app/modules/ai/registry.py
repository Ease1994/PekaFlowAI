"""Agent 工具注册表。这里的 Skill 是可执行 Tool（沿用类名），不是 SKILL.md。

SKILL.md 在 playbooks/ 与技能库；本模块的 handler 才改平台状态。
agent-loop 只按名字分发，不写具体业务。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.response import BizException

Handler = Callable[[Session, Any, dict], dict]


@dataclass
class Skill:
    name: str
    description: str
    category: str
    risk: str  # read / write / destructive
    parameters: dict
    handler: Handler
    confirm: bool = False
    examples: list[str] = field(default_factory=list)
    llm_visible: bool = True

    def public(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk": self.risk,
            "confirm": self.confirm,
            "parameters": self.parameters,
            "examples": self.examples,
            "auth": "用户身份，高风险需确认",
        }


_SKILLS: dict[str, Skill] = {}


def register(skill: Skill) -> Skill:
    _SKILLS[skill.name] = skill
    return skill


def get_skill(name: str) -> Skill | None:
    return _SKILLS.get(name)


def all_skills() -> list[Skill]:
    order = {"meta": -1, "catalog": 0, "observe": 1, "diagnose": 2, "delivery": 3, "access": 4, "authoring": 5}
    return sorted(_SKILLS.values(), key=lambda s: (order.get(s.category, 9), s.name))


def openai_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters or {"type": "object", "properties": {}},
            },
        }
        for s in all_skills()
        if s.llm_visible
    ]


def mcp_tools() -> list[dict]:
    return [s.public() for s in all_skills()]


def dispatch(db: Session, name: str, params: dict, current, *, confirmed: bool = False) -> dict:
    skill = _SKILLS.get(name)
    if skill is None:
        return {"error": f"未知技能 {name}"}
    if skill.confirm and skill.risk == "destructive" and not confirmed and not str(name).startswith("propose_"):
        raise BizException.bad_request(f"技能 {name} 属于高风险操作，需要用户显式确认")
    from app.modules.audit.context import bind_skill, reset_skill

    token = bind_skill(name)
    try:
        return skill.handler(db, current, params or {})
    finally:
        reset_skill(token)


def system_skill_guide() -> str:
    lines = ["可用技能（按目录调用，禁止编造 id）："]
    for s in all_skills():
        if not s.llm_visible:
            continue
        lines.append(f"- [{s.category}/{s.risk}] {s.name}: {s.description}")
    return "\n".join(lines)
