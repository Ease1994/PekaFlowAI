"""系统提示词：技能是说明书，工具是函数调用。

SKILL.md 写领域流程；Python Tool 真正执行。
不要在这里拼「如果说了发布就…」——那种写法改一次伤一处。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.ai.memory import render_working


def tool_guide(_db: Session) -> str:
    """跨工具短约定。具体何时调用写在各工具 schema 和已加载的 SKILL.md 里。"""
    return (
        "工具是可执行函数，用函数调用接口按 schema 填参，禁止编造 id。"
        "互相独立的只读查询可以在同一轮一次发起多个调用；有先后依赖的等上一次结果再调。"
        "同一 list_/get_ 不要换参数反复搜：结果仍有多种合法下一步时，用 ask_user 问用户。"
        "调用工具时不要对用户说「正在查询」「稍等」——那不是答案。"
        "高风险写操作（发布、回滚、下发、审批、安装技能）只出一次确认卡，"
        "用户点黄色按钮即完成，不要再口头确认，禁止写「回复确认即提交」。"
        "禁止对目录里的每一条再连环 get_pipeline。"
    )


_IDENTITY = (
    "你是 PekaFlowAI 发布助手。每次请求都从会话日志重建，"
    "包含历史工具调用与结果，禁止假装没有看过上一轮目录。"
)

_PERSONA = (
    "测试环境=env test，生产=env prod。"
    "「第N个」「就这个」「确认」「那条测试」一律指向会话状态和上一轮工具结果。"
    "一次发布是整条流水线按既有配置跑完；对话里指定文件名不会只发那几个文件。"
)

_SKILL_CONTRACT = (
    "技能是 SKILL.md 说明书，工具是真正执行的函数。匹配到的任务：先读说明书，再调工具。"
    "目录只有摘要。已标注「已加载」的正文在下方，视为已经读过："
    "立即按其中 Workflow 调用工具，不要再 skill({name})，不要先复述说明书。"
    "未加载时才 skill({name}) 按精确 name 加载，然后再调工具。"
)


def _pending_attachments(db: Session, current) -> str:
    """用户拖进对话框、还没被下发掉的文件。"""
    from sqlalchemy import select

    from app.modules.ai.models import AiAttachment

    rows = db.scalars(
        select(AiAttachment)
        .where(AiAttachment.user_id == current.id, AiAttachment.consumed_release_id == 0)
        .order_by(AiAttachment.id)
    ).all()
    if not rows:
        return ""
    lines = ["用户已经上传、等着处理的文件："]
    for r in rows:
        lines.append(f"- attachment_id={r.id} {r.rel_path or r.name}（{r.size_bytes} 字节）")
    lines.append(
        "用户要求下发时，attachment_ids 就从这里取，不要自己编号。"
        "用户没提下发就别主动传，这些文件放着不动也没关系。"
    )
    return "\n".join(lines)


def assemble_system(
    db: Session,
    current,
    working: dict | None = None,
    *,
    selected_skills: list[str] | None = None,
    message: str = "",
) -> str:
    from app.modules.harness import skills as harness_skills

    sections = [
        ("identity", _IDENTITY),
        ("persona", _PERSONA),
        ("tools", tool_guide(db)),
        ("skill_contract", _SKILL_CONTRACT),
    ]
    installed_skills = harness_skills.system_prompt(
        db,
        selected=selected_skills,
        viewer_id=getattr(current, "id", None),
        message=message,
    )
    if installed_skills:
        sections.append(("skills", installed_skills))
    files = _pending_attachments(db, current)
    if files:
        sections.append(("attachments", files))
    mem = render_working(working or {})
    if mem:
        sections.append(("working", mem))
    return "\n\n".join(f"## {name}\n{text}" for name, text in sections if text.strip())
