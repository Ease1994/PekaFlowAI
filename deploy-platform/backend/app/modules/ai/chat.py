"""对话编排：模型循环 + 工具分发。

技能（playbooks/*.md）是说明书，匹配到就内联进系统提示；
工具（skills/*.py 的 handler）是真正执行的函数调用。
本模块不写具体业务。
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.modules.ai.budget import compact_messages, estimate_tokens, input_budget, tool_result_for_model
from app.modules.ai.context import best_matches, format_release_candidates, resolve_release_target, visible_pipelines
from app.modules.ai.intent import (
    is_access_apply_intent,
    is_release_intent,
    is_skill_lifecycle_utterance,
)
from app.modules.ai.registry import get_skill
from app.modules.ai.skills import load_all
from app.modules.harness import tools as harness_tools

logger = logging.getLogger(__name__)

# 一次提问内允许的最大「模型→工具→模型」往返轮数
MAX_TOOL_ROUNDS = 8

# 模型边调工具边说的口头禅。这不是答案，写进最终回复用户会以为查询卡死了。
_WAIT_MARKERS = (
    "正在查询",
    "正在检索",
    "正在查找",
    "正在获取",
    "正在列出",
    "稍等",
    "请稍候",
    "马上为您",
    "马上列出",
    "让我查",
    "我先查",
    "我来查",
    "帮你查",
)

# 技能返回的目录类列表。漏一种，用户问「有哪些代码库」就会再次停在「正在查询」。
_LIST_SPECS = (
    ("nodes", "部署节点", ("name", "host", "env", "status")),
    ("agents", "机器", ("name", "role", "host", "os", "status")),
    ("pipelines", "流水线", ("name", "project", "group", "env")),
    ("projects", "项目", ("name", "code")),
    ("groups", "分组", ("name", "type")),
    ("repositories", "代码库", ("name", "alias", "url")),
    ("plugins", "插件", ("display_name", "name", "category")),
    ("releases", "发布", ("pipeline", "version", "status")),
    ("notifications", "通知", ("title", "detail_url")),
    ("tools", "工具", ("name", "description", "risk", "category")),
    ("agent_skills", "技能包", ("name", "description")),
    ("approvals", "审批", ("pipeline", "version", "status")),
)
_NAME_FIELDS = ("name", "host", "alias", "display_name", "pipeline", "code", "title")


def _audit_context(messages: list[dict] | None, **extra: object) -> dict:
    """只记摘要。整包 messages 再写一遍会拖慢本轮、撑爆检查页。"""
    rows = messages or []
    chars = 0
    roles: list[str] = []
    for item in rows:
        roles.append(str(item.get("role") or ""))
        content = item.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif content is not None:
            chars += len(str(content))
        for call in item.get("tool_calls") or []:
            chars += len(str(call))
    out: dict = {"message_count": len(rows), "roles": roles, "approx_chars": chars}
    out.update(extra)
    return out


def looks_like_wait_reply(text: str) -> bool:
    """「正在查询……稍等」这类话出现在 tool_call 同一拍，不能当最终回答。"""
    t = (text or "").strip()
    if not t:
        return True
    return any(marker in t for marker in _WAIT_MARKERS)


_TEXT_CONFIRM_MARKERS = (
    "回复『确认』",
    "回复「确认」",
    "回复确认即",
    "你回复确认",
    "回复『确认』即提交",
)

# 模型在 propose_* 失败后仍按说明书写「点黄按钮」，页面上其实没有按钮。
_FAKE_CARD_MARKERS = (
    "黄色确认按钮",
    "点击上方黄色",
    "点一次黄色",
    "点击黄色确认",
    "确认卡片如下",
)


def looks_like_text_confirm_prompt(text: str) -> bool:
    """模型用「请回复确认」或「点黄按钮」冒充确认卡，页面上就不会出现按钮。"""
    t = text or ""
    return any(marker in t for marker in _TEXT_CONFIRM_MARKERS) or any(
        marker in t for marker in _FAKE_CARD_MARKERS
    )


def _finding_text(item: object) -> str:
    """体检项可能是字符串，也可能是带 message 的字典。"""
    if isinstance(item, dict):
        return str(item.get("message") or item.get("code") or "").strip()
    return str(item).strip()


def tool_failures(traces: list[dict] | None) -> list[tuple[str, str]]:
    """轨迹里需要写进正文的失败：error，或 propose_* 体检未通过。"""
    out: list[tuple[str, str]] = []
    for trace in traces or []:
        name = str(trace.get("name") or "").strip() or "工具"
        result = trace.get("result")
        if not isinstance(result, dict):
            continue
        err = str(result.get("error") or "").strip()
        if err:
            out.append((name, err))
            continue
        if name.startswith("propose_") and result.get("ok") is False:
            msg = str(result.get("message") or "没有通过检查").strip()
            findings = result.get("findings") or []
            if isinstance(findings, list):
                bits = [_finding_text(item) for item in findings[:4]]
                bits = [bit for bit in bits if bit]
                if bits:
                    msg = f"{msg}：{'；'.join(bits)}"
            out.append((name, msg))
    return out


def _failure_title(name: str) -> str:
    if name == "propose_agent_skill":
        return "Agent 技能没有起草成功"
    if name == "propose_agent_skill_lifecycle":
        return "技能没有卸载或停用成功"
    if name == "propose_plugin_draft":
        return "插件草稿没有保存"
    if name.startswith("propose_"):
        return "没有生成确认卡片"
    return f"`{name}` 失败"


def format_tool_failures(failures: list[tuple[str, str]]) -> str:
    """把工具失败写成用户不用展开轨迹就能看到的正文。

    只有起草类工具失败才提「没有确认按钮」；查状态失败说这句话会误导。
    """
    if not failures:
        return ""
    propose = any(name.startswith("propose_") for name, _ in failures)
    card_note = "\n页面上没有出现确认按钮。" if propose else ""
    if len(failures) == 1:
        name, err = failures[0]
        return f"{_failure_title(name)}：{err}{card_note}"
    heading = (
        "这一轮有技能没有跑成功，页面上也没有确认按钮。"
        if propose
        else "这一轮有技能没有跑成功。"
    )
    lines = [heading]
    for name, err in failures:
        lines.append(f"- `{name}`：{err}")
    return "\n".join(lines)


def _format_list_items(key: str, label: str, fields: tuple[str, ...], items: list, total: int | None) -> str:
    count = total if total is not None else len(items)
    if not items:
        return f"没有可展示的{label}。"
    lines = [f"共 {count} 个{label}："]
    for item in items:
        if not isinstance(item, dict):
            continue
        bits = []
        for field in fields:
            value = item.get(field)
            if isinstance(value, list):
                value = "、".join(str(x) for x in value if x) or ""
            if value not in (None, ""):
                bits.append(str(value))
        extra = ""
        if key == "nodes":
            groups = "、".join(item.get("groups") or []) or "未分组"
            paths = "、".join(item.get("allow_paths") or []) or "未配置"
            extra = f"，组：{groups}；可写目录：{paths}"
        title = bits[0] if bits else ""
        rest = "，".join(bits[1:])
        detail = f"{title}（{rest}）" if rest else title
        lines.append(f"- #{item.get('id')} {detail}{extra}".rstrip())
    return "\n".join(lines)


_APPLY_TOOLS = frozenset(
    {"apply_project_execute", "apply_group_execute", "apply_pipeline_execute", "apply_project_role"}
)
_ACCESS_MEMORY_TOOLS = _APPLY_TOOLS | frozenset({"list_access_catalog"})


def _traces_need_access_memory(traces: list | None) -> bool:
    """申请相关工具跑过就要记下工作记忆，方便下一轮短回复续接范围。"""
    for item in traces or []:
        if canonicalize_tool_name(str(item.get("name") or "")) in _ACCESS_MEMORY_TOOLS:
            return True
    return False


def _skill_replies(traces: list[dict]) -> list[str]:
    """技能已经写好给用户看的句子。有结论就不必再让模型推理一圈去复述。"""
    out: list[str] = []
    for trace in traces:
        result = trace.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("error") or result.get("_action"):
            continue
        reply = str(result.get("reply") or "").strip()
        if reply:
            out.append(reply)
    return out


def _apply_replies(traces: list[dict]) -> tuple[list[str], list[str]]:
    """权限申请的结论：成功文案优先；失败只留去重后的错误，不要再拼流水线目录。"""
    ok: list[str] = []
    err: list[str] = []
    seen_err: set[str] = set()
    for trace in traces:
        name = canonicalize_tool_name(str(trace.get("name") or ""))
        if name not in _APPLY_TOOLS:
            continue
        result = trace.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("error"):
            text = str(result["error"]).strip()
            if text and text not in seen_err:
                seen_err.add(text)
                err.append(text)
            continue
        reply = str(result.get("reply") or "").strip()
        if reply:
            ok.append(reply)
    return ok, err


def format_tool_reply(traces: list[dict]) -> str:
    """把技能返回值整理成用户能直接看的文字。模型不写结论时走这条路。"""
    ready = _skill_replies(traces)
    if ready:
        return "\n".join(ready)
    apply_ok, apply_err = _apply_replies(traces)
    if apply_ok:
        return "\n".join(apply_ok)
    if apply_err:
        return apply_err[0]
    parts: list[str] = []
    seen_lists: set[str] = set()
    for trace in traces:
        result = trace.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("_action"):
            continue
        if str(trace.get("name") or "") in {"skill", "load_skill"}:
            continue
        if result.get("skill_content"):
            continue
        if result.get("error"):
            parts.append(str(result["error"]))
            continue
        formatted_list = False
        for key, label, fields in _LIST_SPECS:
            if key not in result:
                continue
            if key in seen_lists:
                formatted_list = True
                break
            seen_lists.add(key)
            if key == "notifications":
                from app.modules.ai.skills.inbox import format_list_reply

                parts.append(format_list_reply(result))
                formatted_list = True
                break
            items = result.get(key) or []
            hint = str(result.get("hint") or "").strip()
            if not items:
                parts.append(hint or f"没有可展示的{label}。")
            else:
                chunk = _format_list_items(key, label, fields, items, result.get("total"))
                if hint:
                    chunk = f"{chunk}\n{hint}"
                parts.append(chunk)
            formatted_list = True
            break
        if formatted_list:
            continue
        if "deploy_frequency" in result:
            parts.append(
                f"{result.get('days') or 30} 天内发布 {result.get('deploy_frequency')} 次，"
                f"成功 {result.get('success')}，失败 {result.get('failed')}，"
                f"变更失败率 {result.get('change_failure_rate')}%"
            )
            continue
        if result.get("diagnosis"):
            parts.append(str(result["diagnosis"]))
            continue
        if result.get("logs"):
            parts.append(str(result["logs"]))
            continue
        if result.get("hint") and len(result) <= 2:
            parts.append(str(result["hint"]))
            continue
        if result.get("reply"):
            parts.append(str(result["reply"]))
            continue
        clipped = json.dumps(result, ensure_ascii=False, default=str)
        if len(clipped) > 1200:
            clipped = clipped[:1200] + "…"
        parts.append(clipped)
    return "\n\n".join(parts).strip()


def _resource_name_mentioned(name: str, text: str) -> bool:
    """目录名必须当完整词出现。

    子串匹配会把 order-service-test 认成已经写进了 order-service-tests，
    模型随口编一张「看起来像」的表就能混过。IP 同理：192.0.2.10 不能算命中 192.0.2.20。
    """
    if not name:
        return False
    if not re.search(r"[A-Za-z0-9]", name):
        return name in text
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def reply_misses_listed_resources(text: str, traces: list[dict]) -> bool:
    """技能已经列出了目录数据，但回复里没把真实名字写出来。"""
    t = (text or "").strip()
    if not t or looks_like_wait_reply(t):
        return True
    names: list[str] = []
    for trace in traces:
        result = trace.get("result")
        if not isinstance(result, dict):
            continue
        for key, _label, _fields in _LIST_SPECS:
            for item in result.get(key) or []:
                if not isinstance(item, dict):
                    continue
                for field in _NAME_FIELDS:
                    val = str(item.get(field) or "").strip()
                    if val:
                        names.append(val)
    if not names:
        return False
    unique = list(dict.fromkeys(names))
    mentioned = [name for name in unique if _resource_name_mentioned(name, t)]
    if not mentioned:
        return True
    # 十条里只沾上一条真名的前缀/后缀，仍然是在编目录，不能当答完了
    if len(unique) >= 3 and len(mentioned) < min(3, (len(unique) + 1) // 2):
        return True
    return False


def inbox_reply_from_traces(traces: list[dict]) -> str | None:
    """本轮若查过收件箱，回复必须用查询结果。模型会把上一轮的已读清单再背一遍。"""
    from app.modules.ai.skills.inbox import format_list_reply

    for trace in reversed(traces or []):
        if canonicalize_tool_name(str(trace.get("name") or "")) != "list_notifications":
            continue
        result = trace.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("error"):
            return str(result["error"])
        return format_list_reply(result)
    return None


def finalize_assistant_reply(
    last_text: str,
    traces: list[dict],
    *,
    rounds_exhausted: bool = False,
    actions: list | None = None,
) -> str:
    """工具已经跑完时，禁止把「正在查询」原样交给用户。"""
    text = (last_text or "").strip()
    if rounds_exhausted:
        note = (
            f"我连续调用了 {MAX_TOOL_ROUNDS} 轮技能仍未得出结论，先停下来避免绕圈。"
            "请把问题说得更具体，比如指定流水线或发布单号。"
        )
        if text and not looks_like_wait_reply(text):
            text = f"{text}\n\n{note}"
        else:
            text = note
    ready = _skill_replies(traces)
    if ready:
        text = "\n".join(ready)
    else:
        apply_ok, apply_err = _apply_replies(traces)
        if apply_ok or apply_err:
            formatted_apply = format_tool_reply(traces)
            if formatted_apply:
                text = formatted_apply
        else:
            inbox_text = inbox_reply_from_traces(traces)
            if inbox_text:
                text = inbox_text
            elif not actions and traces and reply_misses_listed_resources(text, traces):
                formatted = format_tool_reply(traces)
                if formatted:
                    text = formatted
    failures = tool_failures(traces)
    propose_fails = [(name, err) for name, err in failures if name.startswith("propose_")]
    if propose_fails and not actions:
        text = format_tool_failures(propose_fails)
    elif not actions and looks_like_text_confirm_prompt(text):
        text = format_tool_failures(failures) if failures else (
            "没有生成确认按钮。请再试一次，或把需求说得更具体。"
        )
    elif failures and not actions:
        block = format_tool_failures(failures)
        if block and not any(err in (text or "") for _, err in failures):
            if looks_like_wait_reply(text) or not text:
                text = block
            else:
                text = f"{block}\n\n{text}"
    if not text and actions:
        text = "请确认是否执行以下操作。"
    if not text and traces:
        text = "已查到结果，但模型没有把结果写进回复。请展开上方技能调用查看，或把范围收窄后再问一次。"
    if not text:
        text = "我可以查流水线、发起发布、诊断失败、查构建机和 DORA。请说具体服务名。"
    return text


def chat(
    db: Session,
    message: str,
    current,
    *,
    conversation_id: int | None = None,
    session_id: int | None = None,
    model_pk: int | None = None,
    images: list[str] | None = None,
    selected_skills: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    """跑一轮对话。on_event 用于把中间进度推给 SSE，不传就是普通同步调用。"""
    load_all()
    from app.modules.ai.continuation import try_continue
    from app.modules.ai.history import session_for_conversation
    from app.modules.ai.sessions import append_event, next_turn
    from app.modules.harness import skills as harness_skills

    selected_token = harness_skills.bind_selected(selected_skills)
    turn_no = None
    user_event_seq = None
    if conversation_id and session_id is None:
        session_id = session_for_conversation(db, conversation_id).id
    if session_id:
        turn_no = next_turn(db, session_id)
        append_event(
            db, session_id, "turn/start", {"source": "chat"}, turn=turn_no, surface="audit"
        )
        user_payload = {"content": message, "kind": "chat"}
        # 图落盘后只把短链写进事件。整段 data URL 会撑爆 MySQL TEXT，
        # 落库失败时前端回显被空投影顶掉，发出去的图就像消失了。
        if images:
            from app.modules.ai.chat_images import persist as persist_chat_images

            try:
                stored = persist_chat_images(session_id, images)
            except OSError:
                logger.exception("会话附图落盘失败 session_id=%s", session_id)
                stored = []
            if stored:
                user_payload["images"] = stored
        user_event = append_event(
            db,
            session_id,
            "user/message",
            user_payload,
            turn=turn_no,
            model_visible=True,
            surface="transcript",
        )
        user_event_seq = user_event.seq
    try:
        persist_access_memory = False
        continued = try_continue(db, message, current, conversation_id)
        if continued is not None:
            result = continued
        else:
            # 有模型时由模型选工具。正则直达只在 _chat_with_rules（没接大模型）里用，
            # 否则「执行发布权限」这类话术会被锁进发布路径，模型根本看不到申请工具。
            result = _run_chat(
                db,
                message,
                current,
                conversation_id=conversation_id,
                session_id=session_id,
                turn_no=turn_no,
                model_pk=model_pk,
                images=images,
                selected_skills=selected_skills,
                on_event=on_event,
            )
            persist_access_memory = _traces_need_access_memory(result.get("traces") if result else None)
        if persist_access_memory and conversation_id:
            from app.modules.ai.memory import absorb_traces, get_working, set_working

            set_working(
                db,
                conversation_id,
                absorb_traces(get_working(db, conversation_id), result.get("traces") or []),
            )
        if conversation_id:
            from app.modules.ai.followup import watch_access_from_traces

            if watch_access_from_traces(
                db, conversation_id, current.id, result.get("traces") or []
            ):
                result["watching"] = True
        if conversation_id and result.get("actions"):
            from app.modules.ai.tokens import sign_actions

            result["actions"] = sign_actions(
                current.id, conversation_id, result.get("actions")
            )
            for action in result["actions"]:
                action["session_id"] = session_id
        if session_id:
            from app.modules.ai.history import slim_traces

            result["traces"] = slim_traces(result.get("traces") or [])
            assistant_event = append_event(
                db,
                session_id,
                "assistant/message",
                {
                    "content": result.get("reply") or "",
                    "actions": result.get("actions") or [],
                    "traces": result.get("traces") or [],
                    "kind": "chat",
                },
                turn=turn_no,
                model_visible=True,
                surface="transcript",
            )
            append_event(
                db,
                session_id,
                "turn/end",
                {"status": "completed"},
                turn=turn_no,
                surface="audit",
            )
            result["_assistant_event_seq"] = assistant_event.seq
        result["_user_event_seq"] = user_event_seq
        result["_event_turn"] = turn_no
        return result
    except Exception as exc:
        if session_id:
            append_event(
                db,
                session_id,
                "error",
                {"phase": "turn", "error": str(exc)},
                turn=turn_no,
                surface="audit",
            )
            append_event(
                db,
                session_id,
                "turn/end",
                {"status": "error"},
                turn=turn_no,
                surface="audit",
            )
        raise
    finally:
        harness_skills.reset_selected(selected_token)


def _run_chat(
    db: Session,
    message: str,
    current,
    *,
    conversation_id: int | None,
    session_id: int | None,
    turn_no: int | None,
    model_pk: int | None = None,
    images: list[str] | None = None,
    selected_skills: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    history = []
    working = {}
    if conversation_id:
        from app.modules.ai.history import load_llm_history
        from app.modules.ai.memory import get_working

        history = load_llm_history(db, conversation_id)
        working = get_working(db, conversation_id)
    elif session_id:
        from app.modules.ai.history import load_session_llm_history

        history = load_session_llm_history(db, session_id)
    llm_result = _chat_with_llm(
        db,
        message,
        current,
        history=history,
        working=working,
        conversation_id=conversation_id,
        session_id=session_id,
        turn_no=turn_no,
        model_pk=model_pk,
        images=images,
        selected_skills=selected_skills,
        on_event=on_event,
    )
    if llm_result is not None:
        return llm_result
    return _chat_with_rules(db, message, current)


# 模型常把技能名说成近义词。不认这些别名时，下发会直接变成「工具不存在」。
TOOL_ALIASES = {
    "prepare_node_push": "propose_node_push",
    "node_push": "propose_node_push",
    "push_to_node": "propose_node_push",
    "create_plugin": "propose_plugin_draft",
    "write_plugin": "propose_plugin_draft",
    "draft_plugin": "propose_plugin_draft",
    "create_skill": "propose_agent_skill",
    "write_skill": "propose_agent_skill",
    "draft_skill": "propose_agent_skill",
    "create_agent_skill": "propose_agent_skill",
    "write_agent_skill": "propose_agent_skill",
    "uninstall_skill": "propose_agent_skill_lifecycle",
    "disable_skill": "propose_agent_skill_lifecycle",
    "enable_skill": "propose_agent_skill_lifecycle",
    "remove_skill": "propose_agent_skill_lifecycle",
    "load_skill": "skill",
}


def canonicalize_tool_name(name: str) -> str:
    raw = (name or "").strip()
    return TOOL_ALIASES.get(raw, raw)


_CATALOG_INTENTS = (
    (re.compile(r"(查询|列出|有哪些|全部).{0,10}(部署节点|节点)"), "list_push_nodes"),
    (re.compile(r"(查询|列出|有哪些|全部).{0,10}(代码库|仓库)"), "list_repositories"),
    (re.compile(r"(查询|列出|有哪些|全部).{0,10}流水线"), "list_pipelines"),
    (re.compile(r"(查询|列出|有哪些|全部).{0,10}项目"), "list_projects"),
    (re.compile(r"(查询|列出|有哪些|全部).{0,10}(构建机|agent)", re.I), "list_agents"),
    (re.compile(r"(查询|列出|有哪些|全部).{0,10}插件"), "list_plugins"),
)


def match_catalog_tool(message: str) -> str | None:
    """目录问答才兜底补查。传到/下发是写操作，不能被「节点」两个字拐去列表。"""
    text = (message or "").strip()
    if re.search(r"(传到|发到|下发|传文件)", text):
        return None
    if re.search(r"(实现|做成|写一个|起草|开发).{0,16}(技能|插件|plugin)", text, re.I):
        return None
    if is_release_intent(text):
        return None
    if is_access_apply_intent(text):
        return None
    for pattern, tool in _CATALOG_INTENTS:
        if pattern.search(text):
            return tool
    return None


def _catalog_answer_ready(message: str, traces: list[dict]) -> bool:
    """目录问句已经查出列表时，不必再花一轮模型把同样的名单复述一遍。"""
    tool = match_catalog_tool(message)
    if not tool or not traces:
        return False
    if re.search(r"(状态|失败|成功|发布单|诊断|回滚|部署|上线|权限)", message or ""):
        return False
    names = {canonicalize_tool_name(str(item.get("name") or "")) for item in traces}
    business = {name for name in names if name not in {"skill", "load_skill"}}
    if tool not in business:
        return False
    return all(name.startswith("list_") for name in business)


def _status_answer_ready(message: str, traces: list[dict]) -> bool:
    """点名查状态时，get_release_status 一次就够，不要再让模型去翻历史。"""
    if not is_status_intent(message) or not traces:
        return False
    names = {canonicalize_tool_name(str(item.get("name") or "")) for item in traces}
    return "get_release_status" in names


def _skip_status_extra_tool(_message: str, tool_name: str, traces: list[dict]) -> str:
    """同一轮 get_release_status 调过就够。不按用户原话锁死其它工具。"""
    name = canonicalize_tool_name(tool_name)
    if name != "get_release_status":
        return ""
    if any(canonicalize_tool_name(str(item.get("name") or "")) == "get_release_status" for item in traces):
        return "查状态只需一次 get_release_status，请根据已有结果回答。"
    return ""


def _skip_access_extra_tool(_message: str, tool_name: str, traces: list[dict]) -> str:
    """同一轮已经提交过申请就不要再循环。不按用户原话指定必须调哪个申请工具。"""
    name = canonicalize_tool_name(tool_name)
    already_applied = any(
        canonicalize_tool_name(str(item.get("name") or "")) in _APPLY_TOOLS for item in traces
    )
    if not already_applied:
        return ""
    if name in _APPLY_TOOLS:
        return "本轮已经申请过，请根据已有结果回答，不要再对每条流水线循环申请。"
    if name in {"list_pipelines", "get_pipeline"}:
        return "申请已经提交，请根据已有结果回答，不必再列流水线。"
    return ""


def _skip_release_extra_tool(_message: str, tool_name: str, traces: list[dict]) -> str:
    """同一轮已经出过发布确认卡就不要再列目录。不按用户原话禁止申请工具。"""
    name = canonicalize_tool_name(tool_name)
    already = any(
        canonicalize_tool_name(str(item.get("name") or "")) == "propose_release" for item in traces
    )
    if name == "propose_release" and already:
        return "本轮已经出过发布确认卡，请根据已有结果回答，不要再列目录。"
    return ""


def _duplicate_tool_skip(tool_name: str, arguments: dict, traces: list[dict]) -> str:
    """同一工具同一参数再调一次就是烧 token，把已有结果指回去。"""
    name = canonicalize_tool_name(tool_name)
    blob = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    for item in traces:
        if canonicalize_tool_name(str(item.get("name") or "")) != name:
            continue
        prev = json.dumps(item.get("arguments") or {}, ensure_ascii=False, sort_keys=True, default=str)
        if prev == blob:
            return f"{name} 已用相同参数调用过，请根据已有结果回答，不要重复调用。"
    return ""


def _repeat_list_tool_skip(tool_name: str, traces: list[dict]) -> str:
    """list_/get_ 换关键字再搜还是空转。有结果就问用户或改用下一步工具。"""
    name = canonicalize_tool_name(tool_name)
    if not (name.startswith("list_") or name.startswith("get_")):
        return ""
    for item in traces:
        if canonicalize_tool_name(str(item.get("name") or "")) == name:
            return (
                f"{name} 本轮已经调用过。根据已有结果用 ask_user 问用户，"
                "或立刻调用下一步工具，不要换参数再搜。"
            )
    return ""


def _loaded_skill_names(
    db: Session,
    message: str,
    selected_skills: list[str] | None,
    viewer_id: int | None,
) -> set[str]:
    """本轮已经内联进系统提示的 SKILL.md 名称。"""
    from app.modules.harness import skills as harness_skills

    return {
        str(item["name"])
        for item in harness_skills.entries_for_turn(
            db, message=message, selected=selected_skills, viewer_id=viewer_id
        )
        if item.get("inlined")
    }


def _skip_already_loaded_skill(tool_name: str, arguments: dict, loaded: set[str]) -> str:
    """说明书已在上下文里就不要再 skill() 空转一轮。读过的 skill 直接干活。"""
    if canonicalize_tool_name(tool_name) not in {"skill", "load_skill"}:
        return ""
    name = str((arguments or {}).get("name") or "").strip()
    if name and name in loaded:
        return f"技能 `{name}` 已在系统提示中加载，请直接按 Workflow 调用工具，不要再 skill。"
    return ""


def _note_llm_usage(
    db: Session,
    current,
    session_id: int | None,
    meta: dict,
    turn,
    messages: list,
    tools: list | None,
    started: float,
    *,
    status: str = "success",
    error: str = "",
) -> None:
    from app.modules.llm.service import record_invocation, usage_tokens_from_turn

    usage, source = usage_tokens_from_turn(turn, messages, tools)
    record_invocation(
        db,
        task="assistant",
        meta=meta,
        usage=usage,
        user_id=getattr(current, "id", None),
        username=str(getattr(current, "username", "") or ""),
        session_id=session_id,
        duration_ms=int((time.perf_counter() - started) * 1000),
        status=status,
        error_message=error,
        usage_source=source,
    )


def _propose_one(db: Session, current, pipeline) -> dict:
    """对唯一命中的流水线调一次 propose_release，生成确认卡。"""
    proposed = execute_tool(db, "propose_release", {"pipeline_id": pipeline.id}, current)
    traces = [
        {
            "id": "direct-release",
            "name": "propose_release",
            "arguments": {"pipeline_id": pipeline.id},
            "result": proposed,
        }
    ]
    if proposed.get("error"):
        return {"reply": str(proposed["error"]), "actions": [], "audit": False, "traces": traces}
    action = proposed.get("_action")
    if not action:
        return {
            "reply": proposed.get("reply") or str(proposed),
            "actions": [],
            "audit": False,
            "traces": traces,
        }
    return {
        "reply": proposed.get("reply") or "请确认是否发布。",
        "actions": [
            {
                "type": action,
                "label": proposed.get("label") or "确认发布",
                "payload": proposed.get("payload") or {},
            }
        ],
        "audit": True,
        "traces": traces,
    }


def try_direct_release(db: Session, current, message: str) -> dict | None:
    """没接大模型时的规则引擎入口：能落到唯一流水线就出确认卡。

    有模型时不要走这里——正则会把「申请执行发布权限」锁进发布路径。
    解析器钉死的结果（点名流水线、项目+环境唯一线）才直达；项目名对不上时
    返回 None，由调用方提示或交给模型抽槽。
    """
    if not is_release_intent(message):
        return None
    found = resolve_release_target(db, current, query=message)
    if found.get("match") is not None:
        return _propose_one(db, current, found["match"])
    if found.get("candidates"):
        return {
            "reply": found.get("reply") or format_release_candidates(found["candidates"], "请指定流水线 id："),
            "actions": [],
            "audit": False,
            "traces": [],
        }
    if found.get("projects"):
        return {"reply": found.get("reply") or "", "actions": [], "audit": False, "traces": []}
    if found.get("grounded"):
        return {
            "reply": found.get("error") or found.get("reply") or "",
            "actions": [],
            "audit": False,
            "traces": [],
        }
    return None


_STATUS_ASK = re.compile(
    r"(状态|进度|怎么样了|咋样了|跑完了没|发布得怎样|正在发布的)",
)
_NOT_STATUS = re.compile(
    r"(实现|做成|写一个|起草|开发).{0,16}(技能|插件|plugin)|"
    r"(权限|申请执行|诊断|失败原因|为什么失败|构建机|DORA|传到|发到|下发|传文件)",
    re.I,
)
_PIPELINE_NAME_CHUNK = re.compile(r"[\da-zA-Z][\da-zA-Z._\-]*[\da-zA-Z]|[\da-zA-Z]{3,}")
_GENERIC_CHUNKS = frozenset({
    "test", "prod", "uat", "dev", "status", "pipeline", "release", "all", "the", "and",
    "query", "list", "get", "http", "https", "com",
})


def is_status_intent(message: str) -> bool:
    """这句话是在查流水线/发布状态，不是写技能、也不是发起发布。"""
    text = (message or "").strip()
    if not text or _NOT_STATUS.search(text):
        return False
    if is_skill_lifecycle_utterance(text) or is_release_intent(text):
        return False
    return bool(_STATUS_ASK.search(text))


def _status_query_params(db: Session, current, message: str) -> dict:
    params: dict = {}
    text = message or ""
    if re.search(r"(历史|最近几次|最近\d+次|全部记录|执行记录|发布记录)", text):
        params["latest"] = False
        params["limit"] = 10
    elif re.search(r"(正在发布的.{0,12}(有哪些|哪些)|在跑的)", text):
        params["status"] = "running"
    matched = best_matches((text or "").lower(), visible_pipelines(db, current))
    if len(matched) == 1:
        params["pipeline_id"] = matched[0][0].id
        return params
    chunks = [
        item for item in _PIPELINE_NAME_CHUNK.findall(text)
        if item.lower() not in _GENERIC_CHUNKS
    ]
    if chunks:
        params["keyword"] = chunks[0]
    return params


def try_direct_status(db: Session, current, message: str) -> dict | None:
    """点名查状态时直接查最近一次，不让模型加载 skill 再把历史翻几轮。"""
    if not is_status_intent(message):
        return None
    params = _status_query_params(db, current, message)
    result = execute_tool(db, "get_release_status", params, current)
    traces = [
        {
            "id": "direct-status",
            "name": "get_release_status",
            "arguments": params,
            "result": result,
        }
    ]
    if result.get("error"):
        return {"reply": str(result["error"]), "actions": [], "audit": False, "traces": traces}
    formatted = format_tool_reply(traces)
    if not formatted:
        formatted = str(result.get("hint") or "").strip() or "没有匹配的发布记录。"
    return {"reply": formatted, "actions": [], "audit": False, "traces": traces}


# 未读问句。默认只查未读；用户点名已读/全部时才放开过滤。
_INBOX_ASK = re.compile(r"(未读(消息|通知|邮件)?|通知中心|铃铛.{0,6}(有|消息|通知)|收件箱)")
_INBOX_ALL = re.compile(r"(已读|全部通知|所有通知|历史通知)")


def is_inbox_intent(message: str) -> bool:
    """这句话是在查站内通知，不是在装/卸技能。"""
    text = (message or "").strip()
    if not text or _NOT_STATUS.search(text):
        return False
    if is_skill_lifecycle_utterance(text):
        return False
    if is_release_intent(text) or is_access_apply_intent(text):
        return False
    return bool(_INBOX_ASK.search(text))


def try_direct_inbox(db: Session, current, message: str) -> dict | None:
    """未读问句直接查库。内置工具始终可查，不看技能装没装。

    装/卸技能的句子即使含「未读」也不走这里，交给对话循环选生命周期工具。
    干净的「查一下未读」仍直达，避免模型把已读背成未读。
    """
    if not is_inbox_intent(message):
        return None
    # 用户没说已读/全部时，一律只查未读，忽略模型可能发明的 unread_only=false。
    unread_only = not bool(_INBOX_ALL.search(message or ""))
    params = {"unread_only": unread_only, "limit": 20}
    result = execute_tool(db, "list_notifications", params, current)
    traces = [
        {
            "id": "direct-inbox",
            "name": "list_notifications",
            "arguments": params,
            "result": result,
        }
    ]
    if result.get("error"):
        return {"reply": str(result["error"]), "actions": [], "audit": False, "traces": traces}
    from app.modules.ai.skills.inbox import format_list_reply

    return {
        "reply": format_list_reply(result),
        "actions": [],
        "audit": False,
        "traces": traces,
    }


def try_direct_access(db: Session, current, message: str) -> dict | None:
    """权限申请/查询/审批走业务分发，不让模型列流水线或循环调用技能。"""
    from app.modules.access.assistant import dispatch_access_utterance

    return dispatch_access_utterance(db, current, message)


def fill_catalog_traces(db: Session, current, message: str, traces: list[dict]) -> list[dict]:
    tool = match_catalog_tool(message)
    if not tool:
        return traces
    if any(canonicalize_tool_name(str(item.get("name") or "")) == tool for item in traces):
        return traces
    try:
        result = execute_tool(db, tool, {}, current)
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}
    filled = list(traces)
    filled.append({"id": "catalog-guard", "name": tool, "arguments": {}, "result": result})
    return filled


def _prepare_release_arguments(message: str, arguments: dict) -> dict:
    """模型常把用户原话塞进 pipeline_id。那不是数字 id，改当名称交给解析器。"""
    out = dict(arguments or {})
    raw = out.get("pipeline_id")
    if raw is not None and not isinstance(raw, bool):
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            out["pipeline_id"] = pid
        else:
            out.pop("pipeline_id", None)
            text = str(raw).strip()
            if text and not str(out.get("pipeline") or "").strip():
                out["pipeline"] = text
    if not str(out.get("query") or "").strip():
        out["query"] = message
    return out


def _card_reply_from_traces(traces: list[dict]) -> str:
    """确认卡的文案以工具结果为准，不用模型自己写的「流水线=#用户原话」。"""
    for item in reversed(traces or []):
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict) or not result.get("_action"):
            continue
        reply = str(result.get("reply") or "").strip()
        if reply:
            return reply
    return ""


def execute_tool(
    db: Session,
    tool: str,
    params: dict,
    current,
    *,
    confirmed: bool = False,
    call_id: str = "",
    session_id: int | None = None,
) -> dict:
    load_all()
    return harness_tools.dispatch(
        db,
        canonicalize_tool_name(tool),
        params or {},
        current,
        confirmed=confirmed,
        call_id=call_id,
        session_id=session_id,
    )


def _chat_with_llm(
    db: Session,
    message: str,
    current,
    *,
    history: list[dict] | None = None,
    working: dict | None = None,
    conversation_id: int | None = None,
    session_id: int | None = None,
    turn_no: int | None = None,
    model_pk: int | None = None,
    images: list[str] | None = None,
    selected_skills: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict | None:
    from app.modules.llm import service as llm_service

    def notify(event: dict) -> None:
        """推给当前 SSE，同时把进度写进会话，刷新后仍能读到正在调用哪个工具。"""
        if on_event is not None:
            on_event(event)
        kind = str(event.get("type") or "")
        text = ""
        if kind == "status":
            text = str(event.get("text") or "").strip()
        elif kind == "reasoning":
            text = str(event.get("text") or event.get("content") or "正在推理…").strip()
        elif kind == "text":
            text = "正在生成回答…"
        elif kind == "tool":
            name = str(event.get("name") or "").strip() or "技能"
            text = f"正在调用 {name}…" if event.get("phase") == "start" else f"{name} 已完成"
        if text:
            log_event("turn/status", {"text": text}, surface="audit")

    def log_event(event_type: str, payload: dict, *, step: int | None = None,
                  call: str = "", model_visible: bool = False,
                  surface: str = "internal") -> None:
        if session_id:
            from app.modules.ai.sessions import append_event

            append_event(
                db,
                session_id,
                event_type,
                payload,
                turn=turn_no,
                step=step,
                call=call,
                model_visible=model_visible,
                surface=surface,
            )

    try:
        client, meta = llm_service.build_client(db, model_pk or None)
    except Exception as e:  # noqa: BLE001
        logger.info("AI 助手未接大模型，回退规则引擎：%s", e)
        log_event("error", {"phase": "client", "error": str(e)}, surface="audit")
        return None

    from app.modules.ai.memory import absorb_tool, set_working
    from app.modules.ai.prompt import assemble_system
    from app.modules.llm.client import FailureKind, LlmFailure

    working = dict(working or {})
    system = assemble_system(
        db, current, working, selected_skills=selected_skills, message=message
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant", "tool"):
            messages.append(turn)
    if images and meta.get("supports_vision"):
        messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": message}]
                + [{"type": "image_url", "image_url": {"url": url}} for url in images],
            }
        )
    else:
        if images:
            notify({"type": "status", "text": "当前模型不看图，已按纯文字处理"})
        messages.append({"role": "user", "content": message})
    actions: list[dict] = []
    traces: list[dict] = []
    last_text = ""
    from app.modules.ai.tool_select import categories_for_turn, names_for_turn

    cats = categories_for_turn(
        message, selected_skills=selected_skills, history=history
    )
    tools = harness_tools.openai_tools(
        db,
        categories=cats,
        names=names_for_turn(message, categories=cats),
    )
    loaded_skills = _loaded_skill_names(
        db, message, selected_skills, getattr(current, "id", None)
    )
    rounds_exhausted = False
    apply_pipeline_calls = 0
    tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    budget = input_budget(
        getattr(client.cfg, "context_window", None),
        getattr(client.cfg, "max_tokens", None),
        tools_tokens,
    )
    think_timeout = max(int(getattr(client.cfg, "timeout_sec", 90) or 90), 120)
    try:
        for round_no in range(MAX_TOOL_ROUNDS):
            step_no = round_no + 1
            log_event("step/start", {}, step=step_no, surface="audit")
            messages = compact_messages(messages, budget=budget)
            log_event(
                "request/header",
                {
                    "model": meta.get("model_id"),
                    "provider": meta.get("provider_name"),
                    "tools": [t["function"]["name"] for t in (tools or [])],
                    "context_budget": budget,
                    "tools_tokens": tools_tokens,
                },
                step=step_no,
                surface="audit",
            )
            log_event(
                "request/context",
                _audit_context(messages),
                step=step_no,
                surface="audit",
            )
            notify({"type": "status", "text": "正在思考…" if round_no == 0 else "正在整理结果…"})
            started = time.perf_counter()
            try:
                turn = client.complete(messages, tools=tools, timeout_sec=think_timeout)
                _note_llm_usage(db, current, session_id, meta, turn, messages, tools, started)
            except LlmFailure as e:
                _note_llm_usage(
                    db, current, session_id, meta, None, messages, tools, started,
                    status="error", error=str(e),
                )
                if e.kind == FailureKind.CONTEXT_LENGTH:
                    logger.info("上下文超限，压缩后重试：%s", e)
                    notify({"type": "status", "text": "上下文过长，已压缩历史后继续…"})
                    messages = compact_messages(messages, budget=max(budget // 2, 3000))
                    log_event(
                        "request/context",
                        _audit_context(messages, retry="compact"),
                        step=step_no,
                        surface="audit",
                    )
                    turn = client.complete(messages, tools=tools, timeout_sec=think_timeout)
                    _note_llm_usage(db, current, session_id, meta, turn, messages, tools, started)
                elif tools and ("HTTP 4" in str(e) or "tool" in str(e).lower()):
                    logger.info("模型不支持 tools，改为目录问答：%s", e)
                    tools = None
                    tools_tokens = 0
                    budget = input_budget(
                        getattr(client.cfg, "context_window", None),
                        getattr(client.cfg, "max_tokens", None),
                        0,
                    )
                    log_event(
                        "request/header",
                        {
                            "model": meta.get("model_id"),
                            "provider": meta.get("provider_name"),
                            "tools": [],
                            "retry": "without-tools",
                        },
                        step=step_no,
                        surface="audit",
                    )
                    log_event(
                        "request/context",
                        _audit_context(messages, retry="without-tools"),
                        step=step_no,
                        surface="audit",
                    )
                    turn = client.complete(messages, tools=None, timeout_sec=think_timeout)
                    _note_llm_usage(db, current, session_id, meta, turn, messages, None, started)
                else:
                    raise
            if turn.finish_reason == "length":
                notify({"type": "status", "text": "推理达到长度上限，改为根据已有结果作答…"})
                log_event(
                    "assistant/truncated",
                    {"finish_reason": "length", "has_tools": bool(turn.tool_calls)},
                    step=step_no,
                    surface="audit",
                )
            if turn.reasoning:
                notify({"type": "reasoning", "text": "正在推理…"})
                log_event(
                    "assistant/reasoning",
                    {"chars": len(turn.reasoning), "truncated": turn.finish_reason == "length"},
                    step=step_no,
                    surface="stream",
                )
            if turn.content:
                log_event(
                    "assistant/chunk",
                    {"content": turn.content},
                    step=step_no,
                    surface="stream",
                )
            if not turn.tool_calls:
                # 只有不再调工具的那一拍才是给用户看的答案。
                incoming = (turn.content or "").strip()
                if actions and last_text:
                    # 确认卡已经由 propose_* 生成。模型常再写一段「回复确认即提交」，
                    # 把按钮文案盖掉，页面上就像只剩打字确认。
                    pass
                elif incoming:
                    last_text = incoming
                log_event("step/end", {"status": "completed"}, step=step_no, surface="audit")
                break
            if round_no == MAX_TOOL_ROUNDS - 1:
                # 跑满上限还在调工具，说明模型没收敛；显式告知，别把半截结论当结果发出去
                rounds_exhausted = True
                log_event("step/end", {"status": "exhausted"}, step=step_no, surface="audit")
                break
            wait_text = looks_like_wait_reply(turn.content or "")
            messages.append(
                {
                    "role": "assistant",
                    "content": None if wait_text else (turn.content or None),
                    "tool_calls": [
                        tc.get("raw")
                        or {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                            },
                        }
                        for tc in turn.tool_calls
                    ],
                }
            )
            for tc in turn.tool_calls:
                call_id = str(tc.get("id") or f"turn-{turn_no}-step-{step_no}")
                tool_name = str(tc.get("name") or "")
                arguments = dict(tc.get("arguments") or {})
                if canonicalize_tool_name(tool_name) == "propose_release":
                    arguments = _prepare_release_arguments(message, arguments)
                skipped = _skip_already_loaded_skill(tool_name, arguments, loaded_skills)
                if not skipped:
                    skipped = _skip_status_extra_tool(message, tool_name, traces)
                if not skipped:
                    skipped = _skip_access_extra_tool(message, tool_name, traces)
                if not skipped:
                    skipped = _skip_release_extra_tool(message, tool_name, traces)
                if not skipped:
                    skipped = _duplicate_tool_skip(tool_name, arguments, traces)
                if not skipped:
                    skipped = _repeat_list_tool_skip(tool_name, traces)
                if skipped:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or "",
                            "content": skipped,
                        }
                    )
                    continue
                log_event(
                    "tool/call",
                    {"name": tool_name, "arguments": arguments},
                    step=step_no,
                    call=call_id,
                    model_visible=True,
                    surface="model_context",
                )
                notify({"type": "tool", "phase": "start", "name": tc.get("name") or ""})
                try:
                    log_event(
                        "tool/pre-execute",
                        {"name": tool_name, "arguments": arguments},
                        step=step_no,
                        call=call_id,
                        surface="audit",
                    )
                    spec, blocked = _preflight_tool(db, tool_name, arguments)
                    decision = "denied" if blocked else ("deferred" if spec and spec.confirm else "not-required")
                    log_event(
                        "tool/approval",
                        {
                            "required": bool(spec is None or spec.confirm),
                            "decision": decision,
                            "isolation": spec.isolation if spec else "",
                        },
                        step=step_no,
                        call=call_id,
                        surface="audit",
                    )
                    if blocked:
                        result = {"error": blocked}
                    elif spec and spec.confirm and spec.source != "builtin":
                        # 第三方隔离工具不能让模型直接跑。内置 propose_* 只生成确认卡片，
                        # 真正改生产在用户点确认之后，必须把 handler 跑完才能出卡片。
                        result = {
                            "approval_required": True,
                            "error": "该工具需要用户显式审批，模型调用未执行",
                            "tool": tool_name,
                            "arguments": arguments,
                        }
                    elif tool_name == "apply_pipeline_execute" and apply_pipeline_calls >= 1:
                        result = {
                            "error": (
                                "一次对话里不能对每条流水线循环申请。"
                                "申请整个项目请改用 apply_project_execute，只调用一次。"
                            )
                        }
                    else:
                        if tool_name == "apply_pipeline_execute":
                            apply_pipeline_calls += 1
                        result = execute_tool(
                            db,
                            tool_name,
                            arguments,
                            current,
                            call_id=call_id,
                            session_id=session_id,
                        )
                except Exception as te:  # noqa: BLE001
                    result = {"error": str(te)}
                    log_event(
                        "error",
                        {"phase": "tool", "name": tool_name, "error": str(te)},
                        step=step_no,
                        call=call_id,
                        surface="audit",
                    )
                log_event(
                    "tool/result",
                    {"status": "error" if result.get("error") else "completed", "result": result},
                    step=step_no,
                    call=call_id,
                    model_visible=True,
                    surface="model_context",
                )
                log_event(
                    "tool/post-execute",
                    {"status": "error" if result.get("error") else "completed"},
                    step=step_no,
                    call=call_id,
                    surface="audit",
                )
                notify(
                    {
                        "type": "tool",
                        "phase": "end",
                        "name": tc.get("name") or "",
                        "error": str(result.get("error") or "") if isinstance(result, dict) else "",
                    }
                )
                traces.append(
                    {
                        "id": tc.get("id") or "",
                        "name": tc.get("name") or "",
                        "arguments": arguments,
                        "result": result,
                    }
                )
                absorb_args = dict(arguments)
                payload = result.get("payload") if isinstance(result, dict) else None
                if isinstance(payload, dict) and payload.get("pipeline_id"):
                    absorb_args["pipeline_id"] = payload["pipeline_id"]
                working = absorb_tool(working, tc.get("name") or "", absorb_args, result)
                if isinstance(result, dict) and result.get("_action"):
                    actions.append(
                        {
                            "type": result["_action"],
                            "label": result.get("label") or "确认",
                            "payload": result.get("payload") or {},
                        }
                    )
                if isinstance(result, dict) and result.get("reply") and not result.get("error"):
                    last_text = str(result["reply"])
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "",
                        "content": tool_result_for_model(tc.get("name") or "", result),
                    }
                )
            log_event("step/end", {"status": "completed"}, step=step_no, surface="audit")
            ready = _skill_replies(traces)
            if ready:
                last_text = "\n".join(ready)
                break
            apply_ok, _apply_err = _apply_replies(traces)
            if apply_ok:
                last_text = apply_ok[0]
                break
            card_reply = _card_reply_from_traces(traces)
            if card_reply:
                last_text = card_reply
            if actions:
                # 确认卡已经是这一轮的答案，再让模型写「回复确认」只会丢掉按钮、也冲掉轨迹。
                break
            propose_fails = [
                (name, err)
                for name, err in tool_failures(traces)
                if name.startswith("propose_")
            ]
            if propose_fails:
                last_text = format_tool_failures(propose_fails)
                break
            if _catalog_answer_ready(message, traces) or _status_answer_ready(message, traces):
                formatted = format_tool_reply(traces)
                if formatted:
                    last_text = formatted
                    break
        if conversation_id:
            set_working(db, conversation_id, working)
        if not actions:
            traces = fill_catalog_traces(db, current, message, traces)
        from app.modules.ai.history import slim_traces

        traces = slim_traces(traces)
        last_text = finalize_assistant_reply(
            last_text, traces, rounds_exhausted=rounds_exhausted, actions=actions
        )
        model = {
            "id": meta.get("id"),
            "name": meta.get("name"),
            "model_id": meta.get("model_id"),
            "provider_name": meta.get("provider_name"),
            "supports_vision": bool(meta.get("supports_vision")),
        }
        if not actions:
            parsed = _parse_inline_action(last_text, db, current, user_message=message)
            if parsed:
                parsed["model"] = model
                parsed["traces"] = traces
                return parsed
        return {"reply": last_text, "actions": actions, "audit": bool(actions), "model": model, "traces": traces}
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 助手大模型调用失败，回退规则：%s", e)
        log_event("error", {"phase": "model-loop", "error": str(e)}, surface="audit")
        if not actions:
            traces = fill_catalog_traces(db, current, message, traces)
        from app.modules.ai.history import slim_traces

        traces = slim_traces(traces)
        if traces:
            model = {
                "id": meta.get("id"),
                "name": meta.get("name"),
                "model_id": meta.get("model_id"),
                "provider_name": meta.get("provider_name"),
                "supports_vision": bool(meta.get("supports_vision")),
            }
            return {
                "reply": finalize_assistant_reply(
                    last_text, traces, rounds_exhausted=False, actions=actions
                ),
                "actions": actions,
                "audit": bool(actions),
                "model": model,
                "traces": traces,
            }
        return None


def _preflight_tool(db: Session, name: str, params: dict):
    """返回工具规格和阻断原因；工具不存在或 schema 不匹配一律 fail-closed。"""
    name = canonicalize_tool_name(name)
    spec = harness_tools.resolve(db, name)
    if spec is None:
        return None, f"工具 {name or '<empty>'} 不存在或未启用"
    if spec.source == "builtin" and not callable(getattr(get_skill(name), "handler", None)):
        return spec, f"工具 {name} 没有可用处理器"
    schema_error = _validate_tool_params(spec.parameters or {}, params)
    if schema_error:
        return spec, f"工具 {name} 参数不符合 schema: {schema_error}"
    return spec, ""


def _validate_tool_params(schema: dict, params: dict) -> str:
    if not isinstance(params, dict):
        return "arguments 必须是对象"
    required = schema.get("required") or []
    missing = [str(name) for name in required if name not in params]
    if missing:
        return "缺少必填参数 " + "、".join(missing)
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        extras = [str(name) for name in params if name not in properties]
        if extras:
            return "包含未知参数 " + "、".join(extras)
    expected_types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for name, value in params.items():
        expected = (properties.get(name) or {}).get("type")
        python_type = expected_types.get(expected)
        if python_type and (not isinstance(value, python_type) or expected == "integer" and isinstance(value, bool)):
            return f"{name} 应为 {expected}"
    return ""


def _parse_inline_action(text: str, db: Session, current, *, user_message: str = "") -> dict | None:
    """模型把确认卡写成正文 JSON 时的兜底。id 必须再经解析器，并带上用户原话，避免散文里的假 id 出卡。"""
    m = re.search(r"\{[^{}]{0,400}\}", text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if str(data.get("action") or "") != "propose_release":
        return None
    raw_id = data.get("pipeline_id")
    try:
        pipeline_id = int(raw_id) if raw_id not in (None, "") else 0
    except (TypeError, ValueError):
        pipeline_id = 0
    params = {
        "version": data.get("version") or "latest",
        "query": user_message or str(data.get("query") or ""),
        "project": str(data.get("project") or ""),
        "env": str(data.get("env") or ""),
        "pipeline": str(data.get("pipeline") or ""),
    }
    if pipeline_id > 0:
        params["pipeline_id"] = pipeline_id
    proposed = execute_tool(db, "propose_release", params, current)
    if proposed.get("error") or not proposed.get("_action"):
        return None
    return {
        "reply": proposed.get("reply") or text,
        "actions": [{"type": proposed["_action"], "label": proposed["label"], "payload": proposed["payload"]}],
        "audit": True,
    }


def _chat_with_rules(db: Session, message: str, current) -> dict:
    rows = visible_pipelines(db, current)
    names = [p.name for p, _a, _b in rows[:20]]
    hint = "当前可见流水线：" + "、".join(names) if names else "当前没有可见流水线。"
    if any(
        k in message
        for k in ["申请权限", "执行权限", "执行发布权限", "发布权限", "申请执行", "权限申请"]
    ):
        if "待审批" in message:
            pending = execute_tool(db, "list_pending_access_applications", {}, current)
            rows = pending.get("applications") or []
            if not rows:
                return {"reply": "当前没有待你审批的权限申请。", "actions": [], "audit": False}
            text_out = "待审批：\n" + "\n".join(
                f"· #{r['id']} {r.get('applicant')} {r.get('project')}/{r.get('group')}/{r.get('pipeline')}"
                for r in rows[:10]
            )
            return {"reply": text_out, "actions": [], "audit": False}
        if any(k in message for k in ["我的", "怎么样", "进度"]):
            mine = execute_tool(db, "list_my_access_applications", {}, current)
            rows = mine.get("applications") or []
            if not rows:
                return {"reply": "你还没有提交过权限申请。", "actions": [], "audit": False}
            text_out = "我的申请：\n" + "\n".join(
                f"· #{r['id']} {r.get('pipeline')} {r.get('status')}" for r in rows[:10]
            )
            return {"reply": text_out, "actions": [], "audit": False}
        direct = try_direct_access(db, current, message)
        if direct is not None:
            return {
                "reply": direct["reply"],
                "actions": direct.get("actions") or [],
                "audit": bool(direct.get("actions")),
                "traces": direct.get("traces") or [],
            }
        return {
            "reply": "请说明申请范围：整个项目（含生产）、某个环境（测试/生产/UAT），或某一条流水线名称。",
            "actions": [],
            "audit": False,
        }
    if is_release_intent(message):
        direct = try_direct_release(db, current, message)
        if direct is not None:
            return direct
        return {"reply": f"没找到对应的流水线。\n{hint}", "actions": [], "audit": False}
    inbox = try_direct_inbox(db, current, message)
    if inbox is not None:
        return inbox
    status = try_direct_status(db, current, message)
    if status is not None:
        return status
    if any(k in message for k in ["列表", "有哪些", "流水线", "项目"]):
        if "构建机" in message:
            return {"reply": json.dumps(execute_tool(db, "list_agents", {}, current), ensure_ascii=False), "actions": [], "audit": False}
        return {"reply": hint, "actions": [], "audit": False}
    if "回滚" in message:
        return {"reply": f"回滚属于高风险操作，请给出发布单号。\n{hint}", "actions": [], "audit": True}
    return {
        "reply": "我是 PekaFlowAI 助手。可以：查项目/流水线、发起发布、查状态、诊断失败、回滚、Rebuild、看构建机和 DORA、申请流水线执行权限。\n"
        + hint
        + "\n请到「模型配置」填 Key 并设默认模型，助手会用大模型调用技能。",
        "actions": [],
        "audit": False,
    }
