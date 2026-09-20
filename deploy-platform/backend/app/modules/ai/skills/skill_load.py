"""面向模型的 skill 工具：按 name 加载 SKILL.md，结果以 <skill_content> 进入下一轮。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.ai.registry import Skill, register
from app.modules.harness import skills as harness_skills


def _load_skill(db: Session, current, params: dict) -> dict:
    name = str(params.get("name") or "").strip()
    if not name:
        return {"error": "缺少 name", "code": "SKILL_NAME_REQUIRED"}
    return harness_skills.load_for_model(db, name, viewer_id=getattr(current, "id", None))


def load() -> None:
    register(
        Skill(
            name="skill",
            description=(
                "按精确 name 加载一份 SKILL.md 说明书，然后按其中 Workflow 调用工具。"
                "在目录里匹配到某条、且尚未标注已加载时调用。"
                "已标注「已加载」的不要再调。不是安装或卸载技能，也不是执行发布。"
            ),
            category="meta",
            risk="read",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能目录里的精确 name，如 rp-release",
                    }
                },
                "required": ["name"],
            },
            handler=_load_skill,
            examples=["加载发布技能", "把 rp-release 说明书调出来"],
        )
    )
