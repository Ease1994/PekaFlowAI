"""已有 Agent 技能的安装、卸载、停用、启用。

对话循环不靠关键词判断「这是不是装/卸」。模型看到本工具的说明后自己调用。
本模块只做两件事：按名称或说明在目录里找到那条技能，再出确认卡。
已卸载的技能用 action=enable 重新安装，不要再起草一份。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.ai.registry import Skill, register
from app.modules.ai.skills.base import card
from app.modules.ai.textmatch import overlap_score
from app.modules.harness import lifecycle, skills as harness_skills

# 确认卡落地时只认这三个动作。
ACTIONS = ("uninstall", "disable", "enable")
# 确认卡按钮和回复里的中文。
_ACTION_LABEL = {
    "uninstall": "卸载",
    "disable": "停用",
    "enable": "启用",
}
# 模型可能填英文枚举，也可能把用户原话塞进 action。闭集词用来收槽，不是对话路由。
_ACTION_WORDS = (
    ("恢复启用", "enable"),
    ("uninstall", "uninstall"),
    ("disable", "disable"),
    ("enable", "enable"),
    ("卸载", "uninstall"),
    ("卸掉", "uninstall"),
    ("卸了", "uninstall"),
    ("删掉", "uninstall"),
    ("删除", "uninstall"),
    ("去掉", "uninstall"),
    ("移除", "uninstall"),
    ("停用", "disable"),
    ("禁用", "disable"),
    ("关掉", "disable"),
    ("安装", "enable"),
    ("装一下", "enable"),
    ("启用", "enable"),
)


def normalize_action(raw: str) -> str:
    """把工具参数收成 uninstall / disable / enable。对不上返回空串。"""
    text = (raw or "").strip().lower()
    if not text:
        return ""
    for token, action in _ACTION_WORDS:
        if text == token.lower() or text == action:
            return action
    for token, action in _ACTION_WORDS:
        if token.lower() in text:
            return action
    return ""


def _visible_catalog(db: Session, current, *, include_uninstalled: bool = False) -> list[dict]:
    """当前用户能看见的技能。安装/启用时要带上已卸载的，否则用户装不回去。"""
    rows = harness_skills.catalog(
        db,
        enabled_only=False,
        viewer_id=getattr(current, "id", None),
        include_all_personal=bool(getattr(current, "is_admin", False)),
    )
    if include_uninstalled:
        return rows
    return [item for item in rows if (item.get("status") or "") != "uninstalled"]


def _can_manage(current, item: dict) -> bool:
    """个人技能本人可改；全员技能只有发布者或管理员可以改。别人不能删。"""
    if getattr(current, "is_admin", False):
        return True
    owner = item.get("owner_user_id")
    if owner is not None and int(owner) == int(current.id):
        return True
    publisher = item.get("publisher_id")
    return owner is None and publisher is not None and int(publisher) == int(current.id)


def _refuse(item: dict) -> dict:
    """普通人动别人共享的技能时的拒绝文案。"""
    return {
        "error": (
            f"「{item.get('display_name') or item.get('name')}」是全员技能，"
            "只有发布者或管理员可以停用或卸载。别人没有删除权。"
        )
    }


def find_skills(db: Session, current, query: str, *, include_uninstalled: bool = False) -> list[dict]:
    """按标识、中文名或说明找技能。用户说「消息通知查看」应对上「读取未读通知」。"""
    items = _visible_catalog(db, current, include_uninstalled=include_uninstalled)
    q = (query or "").strip()
    if not q:
        return items
    ranked: list[tuple[int, dict]] = []
    for item in items:
        blob = " ".join(
            str(item.get(k) or "")
            for k in ("name", "display_name", "component_name", "description")
        )
        ranked.append((overlap_score(q, blob), item))
    ranked = [pair for pair in ranked if pair[0] > 0]
    if not ranked:
        return []
    best = max(score for score, _ in ranked)
    return [item for score, item in ranked if score == best]


def _propose(db: Session, current, params: dict, *, action: str, query: str) -> dict:
    """目录里钉死一条技能后出确认卡。对不上或多条时把名单还给模型。"""
    name = str(params.get("name") or params.get("skill") or "").strip()
    needle = name or query
    found = find_skills(
        db, current, needle, include_uninstalled=(action == "enable")
    )
    verb = _ACTION_LABEL[action]
    if not found:
        return {
            "error": (
                f"没有找到要{verb}的技能「{needle or '（未指定名称）'}」。"
                "把技能中文名或标识放进 name，用户原话放进 intent。"
                "若这是一份还不在库里的新技能，改用 propose_agent_skill 起草。"
            )
        }
    if len(found) > 1:
        lines = "、".join(f"{item.get('display_name')}（`{item.get('name')}`）" for item in found[:8])
        return {"error": f"匹配到多份技能，请把精确名称放进 name：{lines}"}
    item = found[0]
    if not _can_manage(current, item):
        return _refuse(item)
    enabled = bool(item.get("enabled"))
    uninstalled = (item.get("status") or "") == "uninstalled"
    if action == "enable" and enabled and not uninstalled:
        return {"reply": f"「{item.get('display_name')}」已经是启用状态，不用再开。", "ok": True}
    if action == "disable" and not enabled and not uninstalled:
        return {"reply": f"「{item.get('display_name')}」已经停用。要彻底删掉请说卸载。", "ok": True}
    title = item.get("display_name") or item.get("name")
    ident = item.get("name")
    if action == "uninstall":
        reply = (
            f"准备卸载 Agent 技能 **{title}**（`{ident}`）。\n\n"
            "卸掉后助手不再加载这份说明书，技能库里也看不到。"
            "内置工具还在，只是不再按这份技能的回复规则整理。"
        )
        label = f"确认卸载 {title}"
    elif action == "disable":
        reply = (
            f"准备停用 Agent 技能 **{title}**（`{ident}`）。\n\n"
            "停用后本轮对话不再加载它，技能包仍留在库里，以后可以再启用。"
        )
        label = f"确认停用 {title}"
    elif uninstalled:
        reply = (
            f"准备重新安装 Agent 技能 **{title}**（`{ident}`）。\n\n"
            "装上后助手会按这份说明书整理回复。内置工具本来就在，不必再传 zip。"
        )
        label = f"确认安装 {title}"
    else:
        reply = f"准备重新启用 Agent 技能 **{title}**（`{ident}`）。"
        label = f"确认启用 {title}"
    return card(
        "confirm_agent_skill_lifecycle",
        label,
        {
            "component_id": item["component_id"],
            "action": action,
            "name": ident,
            "display_name": title,
        },
        reply,
    )


def _handler(db: Session, current, params: dict) -> dict:
    """模型调用入口。action / name 由模型按语义填写，这里只收槽和找目录。"""
    action = normalize_action(str(params.get("action") or ""))
    if not action:
        action = normalize_action(str(params.get("intent") or ""))
    if action not in ACTIONS:
        return {"error": "请把 action 设为 uninstall（卸载）、disable（停用）或 enable（启用/安装）"}
    # 模型有时把整句塞进 action，名称槽为空时用原话在目录里找。
    query = str(params.get("name") or params.get("query") or params.get("intent") or "")
    if not query.strip():
        query = str(params.get("action") or "")
    return _propose(db, current, params, action=action, query=query)


def apply_lifecycle(db: Session, current, payload: dict) -> dict:
    """用户点确认卡后改库。再鉴权一次，避免卡片被改成别人的组件。"""
    try:
        component_id = int(payload.get("component_id") or 0)
    except (TypeError, ValueError):
        component_id = 0
    action = normalize_action(str(payload.get("action") or ""))
    if not component_id or action not in ACTIONS:
        raise BizException.bad_request("确认信息不完整")
    items = [
        item
        for item in _visible_catalog(db, current, include_uninstalled=True)
        if item.get("component_id") == component_id
    ]
    if not items:
        raise BizException.not_found("技能不存在")
    item = items[0]
    if not _can_manage(current, item):
        raise BizException.forbidden(_refuse(item)["error"])
    actor_id = getattr(current, "id", None)
    actor_name = str(getattr(current, "username", "") or "")
    if action == "uninstall":
        lifecycle.uninstall(db, component_id, actor_id=actor_id, actor_name=actor_name, source="ai")
        done = "已卸载"
    elif action == "disable":
        lifecycle.set_enabled(
            db, component_id, False, actor_id=actor_id, actor_name=actor_name, source="ai"
        )
        done = "已停用"
    elif (item.get("status") or "") == "uninstalled":
        lifecycle.reinstall(db, component_id, actor_id=actor_id, actor_name=actor_name, source="ai")
        done = "已安装"
    else:
        lifecycle.set_enabled(
            db, component_id, True, actor_id=actor_id, actor_name=actor_name, source="ai"
        )
        done = "已启用"
    title = item.get("display_name") or item.get("name")
    return {"display_name": title, "name": item.get("name"), "action": action, "done": done}


def load() -> None:
    register(
        Skill(
            name="propose_agent_skill_lifecycle",
            description=(
                "安装、卸载、停用或启用已有的 Agent 技能包，出确认卡。"
                "在要装上、卸掉、关掉、打开某份技能时调用。"
                "不是去查未读或使用该技能的能力，也不是起草新技能。"
            ),
            category="authoring",
            risk="destructive",
            confirm=True,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["uninstall", "disable", "enable"],
                        "description": "uninstall 卸载；disable 停用仍保留；enable 启用或把已卸载的再装上",
                    },
                    "name": {
                        "type": "string",
                        "description": "技能标识或中文名，如 read-unread-notifications、读取未读通知",
                    },
                    "intent": {
                        "type": "string",
                        "description": "用户原话。名称不精确时用整句在目录里匹配",
                    },
                },
                "required": ["action"],
            },
            handler=_handler,
            examples=[
                "安装一下读取未读通知",
                "卸载读取未读通知这个技能",
                "停用查询流水线状态",
                "把未读通知技能卸了",
                "卸载消息通知查看这个技能",
                "启用发布技能",
            ],
        )
    )
