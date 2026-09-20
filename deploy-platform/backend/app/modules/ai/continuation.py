"""多轮意图续接：短回复（确认 / 第 N 个 / 名称）接上上一轮，避免模型丢掉上下文。"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

_CONFIRM = {
    "确认",
    "好的",
    "好",
    "是",
    "对",
    "嗯",
    "行",
    "可以",
    "提交",
    "同意",
    "就这个",
    "就是这个",
    "就它",
    "是的",
    "没问题",
    "yes",
    "ok",
    "okay",
    "y",
}
_CN_NUM = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_TABLE_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*([^|\n]+)\|", re.M)
_PINNED_ID = re.compile(r"pipeline_id\s*[=:：]\s*(\d+)", re.I)
_BULLET_ID = re.compile(r"(?:^|\n)\s*(?:[-*·]|\d+\.)\s*(?:id\s*[=:：]\s*)?(\d+)\b", re.I)
_ORDINAL = re.compile(r"第\s*([0-9]+|[一二两三四五六七八九十]+)\s*[个条项号]?", re.I)
_BARE_ID = re.compile(r"^(?:流水线|id|编号)?\s*#?\s*(\d+)\s*(?:的)?(?:权限|执行权)?$", re.I)
_ACCESS_HINT = (
    "执行权",
    "执行权限",
    "权限申请",
    "可申请",
    "申请哪一条",
    "申请这条",
    "申请执行",
    "的权限",
    "pipeline_id",
    "apply_pipeline",
)
# 出现这些词说明用户在说别的事，别再往权限申请上接。
# 「执行」本身不够：申请执行权限也会带这个字，要靠 is_release_intent 判断。
_OTHER_INTENT = (
    "发布",
    "部署",
    "上线",
    "回滚",
    "rebuild",
    "重跑",
    "重新构建",
    "取消",
    "状态",
    "进度",
    "日志",
    "失败",
    "诊断",
)
_ABORT = {
    "算了",
    "不用了",
    "取消",
    "中止",
    "停止",
    "不申请了",
    "先不了",
    "不要了",
}


def is_short_confirm(text: str) -> bool:
    """口头确认：去掉标点后整句就是「确认 / 好的 / ok」这类。"""
    return _normalized_short(text) in _CONFIRM


def _to_int(raw: str) -> int | None:
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw)


def listed_pipelines(assistant_text: str) -> list[tuple[int, str]]:
    """从上一轮助手回复里抽出目录：表格行或 id= 列表。"""
    text = assistant_text or ""
    rows: list[tuple[int, str]] = []
    seen: set[int] = set()
    for m in _TABLE_ROW.finditer(text):
        pid = int(m.group(1))
        name = (m.group(2) or "").strip().strip("`")
        if pid in seen or name.lower() in {"流水线名称", "name", "id"}:
            continue
        seen.add(pid)
        rows.append((pid, name))
    if rows:
        return rows
    for m in _BULLET_ID.finditer(text):
        pid = int(m.group(1))
        if pid not in seen:
            seen.add(pid)
            rows.append((pid, ""))
    return rows


def pinned_pipeline_id(assistant_text: str) -> int | None:
    ids = [int(x) for x in _PINNED_ID.findall(assistant_text or "")]
    uniq = list(dict.fromkeys(ids))
    if len(uniq) == 1:
        return uniq[0]
    listed = listed_pipelines(assistant_text)
    if len(listed) == 1:
        return listed[0][0]
    return None


def _normalized_short(text: str) -> str:
    """去掉标点空白后的短句，用来认确认/中止这类口头指令。"""
    return re.sub(r"[\s。！!？?，,、.]+", "", (text or "").strip().lower())


def is_abort(text: str) -> bool:
    """用户明确放弃上一轮未完成的选择，而不是在回答项目/环境。"""
    return _normalized_short(text) in _ABORT


def _has_other_intent(user_text: str) -> bool:
    """这句话本身在说别的事，就不该被续接到权限申请上。

    上一轮在问「申请哪个项目」时，用户改口说「执行AI陪练项目生产环境」：
    句子里能匹配到项目名，续接会当成选项目并提交申请，而不是去出发布确认卡。
    """
    from app.modules.ai.intent import is_release_intent

    text = user_text or ""
    if is_release_intent(text) or is_abort(text):
        return True
    return any(k in text.lower() for k in _OTHER_INTENT)


def _is_access_context(user_text: str, assistant_text: str) -> bool:
    if _has_other_intent(user_text):
        return False
    blob = f"{user_text}\n{assistant_text}"
    return any(k in blob for k in _ACCESS_HINT)


def resolve_access_pipeline_id(user_text: str, assistant_text: str, catalog: list | None = None) -> int | None:
    """用户指定「第4个 / 确认 / 名称 / id」时，对照工作记忆目录或上一轮回复解析真实 pipeline_id。"""
    listed: list[tuple[int, str]] = []
    for p in catalog or []:
        try:
            listed.append((int(p.get("id")), str(p.get("name") or "")))
        except (TypeError, ValueError):
            continue
    if not listed:
        listed = listed_pipelines(assistant_text)
    pinned = pinned_pipeline_id(assistant_text)
    if pinned is None and len(listed) == 1:
        pinned = listed[0][0]
    text = (user_text or "").strip()
    if not text:
        return None

    if is_short_confirm(text):
        return pinned

    ord_m = _ORDINAL.search(text)
    if ord_m:
        n = _to_int(ord_m.group(1))
        if n and listed and 1 <= n <= len(listed):
            return listed[n - 1][0]
        if n and pinned and n == pinned:
            return pinned
        if n and listed:
            for pid, _name in listed:
                if pid == n:
                    return pid
        return None

    bare = _BARE_ID.match(text)
    if bare:
        n = int(bare.group(1))
        if listed:
            ids = [pid for pid, _ in listed]
            if n in ids:
                return n
            if 1 <= n <= len(listed):
                return listed[n - 1][0]
        return None

    lowered = text.lower()
    for pid, name in listed:
        nl = (name or "").strip().lower()
        if nl and (nl in lowered or lowered in nl):
            return pid
    return None


_WHOLE_PROJECT = re.compile(r"(整个项目|所有流水线|全部流水线|整项目)")


def _match_project_from_working(message: str, projects: list[dict]) -> int | None:
    """从工作记忆里的项目名单解析用户指出的项目。"""
    from app.modules.ai.intent import parse_project_id

    pid = parse_project_id(message)
    if pid:
        if not projects or any(int(p.get("id") or 0) == pid for p in projects):
            return pid
    compact = (message or "").replace(" ", "")
    hits: list[int] = []
    for p in projects:
        name = str(p.get("name") or "")
        code = str(p.get("code") or "")
        try:
            nid = int(p.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not nid:
            continue
        if name and (name in (message or "") or name.replace(" ", "") in compact):
            hits.append(nid)
        elif code and code.lower() in (message or "").lower():
            hits.append(nid)
        elif compact.isdigit() and int(compact) == nid:
            hits.append(nid)
    uniq = list(dict.fromkeys(hits))
    return uniq[0] if len(uniq) == 1 else None


def _apply_and_return(db: Session, current, working: dict, name: str, params: dict) -> dict:
    """按已确定范围提交申请，并清掉等待状态。"""
    from app.modules.ai.memory import absorb_tool
    from app.modules.ai.registry import dispatch

    actions = working.get("requested_actions")
    if actions and "actions" not in params:
        params = {**params, "actions": actions}
    applied = dispatch(db, name, params, current)
    traces = [{"id": "cont-apply", "name": name, "arguments": params, "result": applied}]
    working = absorb_tool(working, name, params, applied if isinstance(applied, dict) else {})
    if applied.get("error"):
        return {"reply": str(applied["error"]), "actions": [], "audit": False, "traces": traces, "_working": working}
    return {
        "reply": applied.get("reply") or str(applied),
        "actions": [],
        "audit": False,
        "traces": traces,
        "_working": working,
    }


def _continue_access_scope(db: Session, message: str, current, working: dict) -> dict | None:
    """上一轮在问项目或范围时，短回复直接提交，不要再走流水线名单。"""
    from app.modules.ai.intent import parse_env_code

    awaiting = working.get("awaiting")
    projects = working.get("catalog_projects") or []
    text = (message or "").strip()
    if awaiting == "pick_project":
        pid = _match_project_from_working(text, projects)
        if not pid:
            return None
        working = dict(working)
        working["selected_project_id"] = pid
        kind = working.get("access_kind") or "scope"
        env = parse_env_code(text) or working.get("pending_env") or ""
        if kind == "group" or env:
            if not env:
                working["awaiting"] = "pick_scope"
                working["access_kind"] = "group"
                return {
                    "reply": "请说明要哪个环境：测试、生产、UAT、预发或开发。整项目（含生产）请说「整个项目」。",
                    "actions": [],
                    "audit": False,
                    "traces": [],
                    "_working": working,
                }
            return _apply_and_return(
                db,
                current,
                working,
                "apply_group_execute",
                {"project_id": pid, "env": env, "reason": "助手代为申请该环境全部分组流水线权限"},
            )
        if kind == "project" or _WHOLE_PROJECT.search(text):
            return _apply_and_return(
                db,
                current,
                working,
                "apply_project_execute",
                {"project_id": pid, "reason": "助手代为申请该项目全部流水线权限"},
            )
        if kind == "role":
            working["awaiting"] = "pick_role"
            working["selected_project_id"] = pid
            hit = next((p for p in projects if int(p.get("id") or 0) == pid), None)
            applied = _try_apply_role_from_text(db, current, working, hit, text)
            if applied is not None:
                return applied
            names = "、".join(str(r.get("name") or "") for r in (hit or {}).get("roles") or [] if r.get("name"))
            return {
                "reply": f"请指出要申请的角色名称。{('现有：' + names) if names else '该项目还没有角色。'}",
                "actions": [],
                "audit": False,
                "traces": [],
                "_working": working,
            }
        working["awaiting"] = "pick_scope"
        working["access_kind"] = "scope"
        hit = next((p for p in projects if int(p.get("id") or 0) == pid), None)
        from app.modules.access.assistant import _ask_scope

        reply = _ask_scope(hit or {"id": pid, "name": f"项目#{pid}", "groups": working.get("catalog_groups") or []})
        return {"reply": reply, "actions": [], "audit": False, "traces": [], "_working": working}

    if awaiting == "pick_scope":
        pid = int(working.get("selected_project_id") or 0)
        if not pid:
            pid = _match_project_from_working(text, projects) or 0
        if not pid:
            return None
        if _WHOLE_PROJECT.search(text):
            return _apply_and_return(
                db,
                current,
                working,
                "apply_project_execute",
                {"project_id": pid, "reason": "助手代为申请该项目全部流水线权限"},
            )
        env = parse_env_code(text) or working.get("pending_env") or ""
        if env:
            return _apply_and_return(
                db,
                current,
                working,
                "apply_group_execute",
                {"project_id": pid, "env": env, "reason": "助手代为申请该环境全部分组流水线权限"},
            )
        return None

    if awaiting == "pick_role":
        pid = int(working.get("selected_project_id") or 0)
        if not pid:
            pid = _match_project_from_working(text, projects) or 0
        if not pid:
            return None
        hit = next((p for p in projects if int(p.get("id") or 0) == pid), None)
        return _try_apply_role_from_text(db, current, working, hit, text)
    return None


def _try_apply_role_from_text(db: Session, current, working: dict, proj: dict | None, text: str) -> dict | None:
    """短回复点到角色名就提交角色申请。"""
    from app.modules.access.assistant import _match_roles

    roles = list((proj or {}).get("roles") or [])
    hits = _match_roles(roles, text)
    if len(hits) != 1:
        return None
    return _apply_and_return(
        db,
        current,
        working,
        "apply_project_role",
        {"role_id": int(hits[0]["id"]), "reason": "助手代为申请项目角色"},
    )


def try_continue(db: Session, message: str, current, conversation_id: int | None) -> dict | None:
    """能直接接上就执行：优先用会话投影，其次解析上一轮回复。"""
    if not conversation_id:
        return None
    from app.modules.ai.history import last_assistant_turn
    from app.modules.ai.memory import absorb_tool, get_working, set_working

    last = last_assistant_turn(db, conversation_id)
    last_text = (last or {}).get("content") or ""
    last_actions = (last or {}).get("actions") or []
    working = get_working(db, conversation_id)

    if is_short_confirm(message) and last_actions:
        from app.core.response import BizException
        from app.modules.ai.actions import perform_action
        from app.modules.ai.tokens import verify as verify_action

        action = last_actions[0]
        try:
            verify_action(
                str(action.get("token") or ""),
                current.id,
                conversation_id,
                str(action.get("type") or ""),
                action.get("payload") or {},
            )
        except BizException as exc:
            return {
                "reply": exc.message or "确认卡片已失效，请重新向助手发起该操作。",
                "actions": [],
                "audit": False,
            }
        out = perform_action(
            db,
            current,
            conversation_id,
            str(action.get("type") or ""),
            action.get("payload") or {},
            persist=False,
        )
        working["awaiting"] = None
        set_working(db, conversation_id, working)
        return {
            "reply": out.get("reply") or "已处理。",
            "actions": [],
            "audit": True,
            "watching": bool(out.get("watching")),
        }

    if is_abort(message) and working.get("awaiting"):
        working = dict(working)
        working["awaiting"] = None
        working["access_kind"] = None
        set_working(db, conversation_id, working)
        return {
            "reply": "好，已取消刚才未完成的操作。需要的时候再说一次即可。",
            "actions": [],
            "audit": False,
        }

    if _has_other_intent(message):
        if working.get("awaiting"):
            working = dict(working)
            working["awaiting"] = None
            working["access_kind"] = None
            set_working(db, conversation_id, working)
        return None

    awaiting = working.get("awaiting")
    if awaiting in {"pick_scope", "pick_project", "pick_role"}:
        scoped = _continue_access_scope(db, message, current, working)
        if scoped is not None:
            next_working = scoped.pop("_working", working)
            set_working(db, conversation_id, next_working)
            return scoped
        return None

    catalog = working.get("catalog") or []
    awaiting_pick = awaiting == "pick_pipeline" and bool(catalog)
    if not awaiting_pick:
        return None
    pid = resolve_access_pipeline_id(message, last_text, catalog=catalog)
    if not pid:
        return None
    from app.modules.ai.registry import dispatch

    applied = dispatch(
        db,
        "apply_pipeline_execute",
        {"pipeline_id": pid, "reason": "助手代为申请执行权限"},
        current,
    )
    traces = [
        {
            "id": "cont-apply",
            "name": "apply_pipeline_execute",
            "arguments": {"pipeline_id": pid},
            "result": applied,
        }
    ]
    working = absorb_tool(working, "apply_pipeline_execute", {"pipeline_id": pid}, applied)
    set_working(db, conversation_id, working)
    if applied.get("error"):
        return {"reply": str(applied["error"]), "actions": [], "audit": False, "traces": traces}
    return {"reply": applied.get("reply") or str(applied), "actions": [], "audit": False, "traces": traces}
