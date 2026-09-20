"""AI 助手路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.ai.actions import perform_action
from app.modules.ai.chat import chat as chat_service, execute_tool
from app.modules.ai.history import (
    append_message,
    get_or_create_conversation,
    list_messages,
    list_session_messages,
    message_public,
    persist_assistant_error,
)
from app.modules.ai.models import AiConversation, AiSession, AiWatch
from app.modules.ai.skills import load_all
from app.modules.harness import tools as harness_tools
from app.modules.ai.tokens import verify as verify_action

router = APIRouter(tags=["AI 助手"])


def _owned_session(db: Session, session_id: int, user_id: int) -> AiSession:
    row = db.get(AiSession, session_id)
    if row is None or row.user_id != user_id or row.status == "deleted":
        raise BizException.not_found("AI 会话")
    return row


def _conversation_for_session(db: Session, session: AiSession | None, user_id: int) -> AiConversation:
    """确认卡片签名绑 conversation_id。会话没挂对话时当场补一条，避免发出去的按钮没有 token。"""
    if session is None:
        return get_or_create_conversation(db, user_id)
    if session.legacy_conversation_id:
        conv = db.get(AiConversation, session.legacy_conversation_id)
        if conv is not None:
            return conv
    conv = AiConversation(user_id=user_id, title=session.title or "新会话", working_json="{}")
    db.add(conv)
    db.flush()
    session.legacy_conversation_id = conv.id
    db.commit()
    db.refresh(conv)
    return conv


def _usable_or_reject(db: Session, model_pk: int) -> int:
    from app.modules.llm import service as llm_service

    pk = llm_service.usable_model_id(db, model_pk)
    if pk is None:
        raise BizException.bad_request("所选模型不可用，请到「模型配置」启用并填写凭证")
    return pk


def _remember_model(db: Session, session: AiSession | None, body: dict) -> int | None:
    """本轮用哪个模型。带了就记进会话，没带就沿用会话上次选的。

    明确指定的 id 必须在可用清单里，避免停用模型还能被调。会话里存着的旧 id
    如果后来被停掉了，静默回落到默认，别把整轮对话打挂。
    """
    from app.modules.llm import service as llm_service

    requested = int(body.get("model_id") or 0)
    if requested:
        pk = _usable_or_reject(db, requested)
        if session is not None:
            session.model_pk = pk
        return pk
    if session is None:
        return None
    return llm_service.usable_model_id(db, session.model_pk)


def _images_of(body: dict) -> list[str]:
    """随消息发给模型看的图，data:image/...;base64 或 http(s) 直链。"""
    out = []
    for item in body.get("images") or []:
        url = str(item.get("url") if isinstance(item, dict) else item or "").strip()
        if url.startswith("data:image/") or url.startswith("http://") or url.startswith("https://"):
            out.append(url)
    return out[:8]


def _selected_names(body: dict, key: str = "skills") -> list[str]:
    raw = body.get(key) or []
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if name:
            names.append(name)
    return names


@router.get("/ai/skills", summary="助手可用工具清单（内置 + 已启用第三方）")
def list_skills(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    load_all()
    return R.ok([item.public() for item in harness_tools.catalog(db)])


@router.get("/ai/tools", summary="MCP 工具清单（兼容旧路径）")
def list_tools(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    load_all()
    return R.ok([item.public() for item in harness_tools.catalog(db)])


@router.get("/ai/models", summary="对话框可选模型（普通用户可见的精简清单）")
def list_assistant_models(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    from app.modules.llm import service as llm_service

    return R.ok(llm_service.list_usable_models(db))


@router.post("/ai/attachments", summary="上传对话框附件（待下发到节点的文件）")
async def upload_attachments(
    files: list[UploadFile] = File(...),
    rel_paths: str = Form(default=""),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """拖进对话框的文件先存在这，等确认卡片点了才真正打包下发。

    rel_paths 是和 files 等长的换行分隔列表，拖整个文件夹时用来保留目录层级。
    """
    from app.modules.ai import attachments

    if not files:
        raise BizException.bad_request("没有选择文件")
    attachments.cleanup_expired(db)

    paths = [p for p in (rel_paths or "").split("\n")]
    out = []
    for i, f in enumerate(files):
        data = await f.read()
        rel = paths[i] if i < len(paths) and paths[i].strip() else (f.filename or "")
        row = attachments.save(db, current.id, f.filename or "file", rel, data)
        out.append(attachments.public(row))
    return R.ok(out)


@router.get("/ai/attachments", summary="待处理的附件")
def list_attachments(
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai import attachments
    from app.modules.ai.models import AiAttachment

    rows = db.scalars(
        select(AiAttachment)
        .where(AiAttachment.user_id == current.id, AiAttachment.consumed_release_id == 0)
        .order_by(AiAttachment.id)
    ).all()
    return R.ok([attachments.public(r) for r in rows])


@router.delete("/ai/attachments/{attachment_id}", summary="撤掉一个还没下发的附件")
def delete_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    import shutil
    from pathlib import Path

    from app.modules.ai.models import AiAttachment

    row = db.get(AiAttachment, attachment_id)
    if row is None or row.user_id != current.id:
        raise BizException.not_found("附件")
    if row.consumed_release_id:
        raise BizException.bad_request("这个文件已经下发过了，撤不回来")
    try:
        parent = Path(row.storage_key).parent
        if parent.is_dir() and parent.parent.name == str(current.id):
            shutil.rmtree(parent, ignore_errors=True)
    except OSError:
        pass
    db.delete(row)
    db.commit()
    return R.ok()


@router.get("/ai/conversation", summary="当前用户聊天记录")
def get_conversation(
    after_id: int = Query(default=0),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    conv = get_or_create_conversation(db, current.id)
    msgs = list_messages(db, conv.id, after_id=after_id)
    watching = (
        db.scalar(
            select(AiWatch.id).where(
                AiWatch.conversation_id == conv.id,
                AiWatch.status == "watching",
            )
        )
        is not None
    )
    live = None
    bound = db.scalars(
        select(AiSession).where(
            AiSession.legacy_conversation_id == conv.id,
            AiSession.status != "deleted",
        )
    ).first()
    if bound is not None:
        from app.modules.ai.sessions import live_turn_of

        live = live_turn_of(db, bound.id)
    return R.ok({"conversation_id": conv.id, "messages": msgs, "watching": watching, "live": live})


@router.delete("/ai/conversation", summary="清空聊天记录和上下文")
def clear_conversation(
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """清掉这个人的聊天记录、工作记忆和未完成的跟进。

    上下文脏了是会自我延续的：模型把上一轮的错误结论当既成事实接着往下推，
    越聊越偏，用户只能眼看着它一路错下去。所以要留一个「推倒重来」的出口。

    只动会话本身——发布、附件、权限都不碰。正在跑的发布继续跑，
    清聊天记录不该有这种副作用。
    """
    from app.modules.ai.history import session_for_conversation
    from app.modules.ai.sessions import append_event

    conv = get_or_create_conversation(db, current.id)
    session = session_for_conversation(db, conv.id)
    append_event(
        db,
        session.id,
        "session/cleared",
        {"reason": "legacy-api"},
        surface="audit",
    )
    # 工作记忆是单独一份投影，不清的话新会话开局就带着旧目标
    conv.working_json = "{}"
    # 跟进结果发到已经清空的会话里只会让人莫名其妙；发布本身不受影响，
    # 结果照样能在发布记录里看到
    stopped = (
        db.query(AiWatch)
        .filter(AiWatch.conversation_id == conv.id, AiWatch.status == "watching")
        .update({AiWatch.status: "cancelled"}, synchronize_session=False)
    )
    db.commit()
    return R.ok({"removed": 0, "stopped_watches": int(stopped or 0)})


@router.post("/ai/chat", summary="AI 对话")
def chat(body: dict, db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    message = (body.get("message") or "").strip()
    requested_session_id = int(body.get("session_id") or 0)
    selected_session = (
        _owned_session(db, requested_session_id, current.id) if requested_session_id else None
    )
    conv = _conversation_for_session(db, selected_session, current.id)
    title_row = selected_session or conv
    if title_row.title in {"默认会话", "新会话"} and message:
        title_row.title = message[:40]
    model_pk = _remember_model(db, selected_session, body)
    db.commit()
    result = chat_service(
        db,
        message,
        current,
        conversation_id=conv.id if conv else None,
        session_id=selected_session.id if selected_session else None,
        model_pk=model_pk,
        images=_images_of(body),
        selected_skills=_selected_names(body),
    )
    event_turn = result.pop("_event_turn", None)
    user_event_seq = result.pop("_user_event_seq", None)
    assistant_event_seq = result.pop("_assistant_event_seq", None)
    if conv is None:
        from app.modules.ai.sessions import project_events, read_events

        result["session_id"] = selected_session.id
        result["messages"] = project_events(read_events(db, selected_session.id))["messages"][-2:]
        return R.ok(result)
    user_msg = append_message(
        db,
        conv.id,
        "user",
        message,
        kind="chat",
        log_event=False,
        turn=event_turn,
        source_event_seq=user_event_seq,
    )
    asst = append_message(
        db,
        conv.id,
        "assistant",
        result.get("reply") or "",
        actions=result["actions"],
        traces=result.get("traces") or [],
        kind="chat",
        log_event=False,
        turn=event_turn,
        source_event_seq=assistant_event_seq,
    )
    result["conversation_id"] = conv.id
    if selected_session:
        result["session_id"] = selected_session.id
        for action in result.get("actions") or []:
            action["session_id"] = selected_session.id
    result["messages"] = [message_public(user_msg), message_public(asst)]
    return R.ok(result)


@router.post("/ai/act", summary="确认助手操作（发布/回滚等）并跟进结果")
def act(body: dict, db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    session_id = int(body.get("session_id") or 0)
    selected = _owned_session(db, session_id, current.id) if session_id else None
    conv = _conversation_for_session(db, selected, current.id)
    action_type = str(body.get("type") or "")
    payload = body.get("payload") or {}
    verify_action(str(body.get("token") or ""), current.id, conv.id, action_type, payload)
    out = perform_action(db, current, conv.id, action_type, payload)
    out["conversation_id"] = conv.id
    return R.ok(out)


@router.post("/ai/chat/stream", summary="AI 对话（SSE 流式）")
def chat_stream(body: dict, current: CurrentUser = Depends(get_current_user)):
    """边跑边推进度。

    大模型客户端本身不支持流式，真正耗时的也是工具调用那几十秒，
    所以这里推的是「在调哪个技能」，跑完再一次性给出回复文本。
    """
    message = (body.get("message") or "").strip()
    user_id = current.id
    # 以前这里完全无视 session_id，回答一律写进 legacy conversation，
    # 而前端按会话去拉，于是选了会话就看不到刚才的回答
    requested_session_id = int(body.get("session_id") or 0)
    images = _images_of(body)

    def generate():
        import json
        import queue
        import threading

        from app.db.session import SessionLocal

        events: queue.Queue = queue.Queue()
        # 立刻推一条，避免首字节前网关把连接掐掉，前端也一直没有任何状态。
        events.put({"type": "status", "text": "正在思考…"})

        def worker():
            # SSE 生成器跑在独立线程里，请求作用域的 Session 不能跨线程复用
            db = SessionLocal()
            conv = None
            session = None
            try:
                session = (
                    _owned_session(db, requested_session_id, user_id)
                    if requested_session_id else None
                )
                conv = _conversation_for_session(db, session, user_id)
                title_row = session or conv
                if title_row is not None and title_row.title in {"默认会话", "新会话"} and message:
                    title_row.title = message[:40]
                model_pk = _remember_model(db, session, body)
                db.commit()
                result = chat_service(
                    db,
                    message,
                    current,
                    conversation_id=conv.id if conv else None,
                    session_id=session.id if session else None,
                    model_pk=model_pk,
                    images=images,
                    selected_skills=_selected_names(body),
                    on_event=events.put,
                )
                event_turn = result.pop("_event_turn", None)
                user_event_seq = result.pop("_user_event_seq", None)
                assistant_event_seq = result.pop("_assistant_event_seq", None)
                if conv is not None:
                    append_message(
                        db,
                        conv.id,
                        "user",
                        message,
                        kind="chat",
                        log_event=False,
                        turn=event_turn,
                        source_event_seq=user_event_seq,
                    )
                    append_message(
                        db,
                        conv.id,
                        "assistant",
                        result.get("reply") or "",
                        actions=result["actions"],
                        traces=result.get("traces") or [],
                        kind="chat",
                        log_event=False,
                        turn=event_turn,
                        source_event_seq=assistant_event_seq,
                    )
                events.put(
                    {
                        "type": "done",
                        "reply": result.get("reply") or "",
                        "actions": result["actions"],
                        "traces": result.get("traces") or [],
                        "model": result.get("model") or {},
                        "watching": bool(result.get("watching")),
                    }
                )
            except Exception as e:  # noqa: BLE001
                from app.core.response import BizException

                err_text = e.message if isinstance(e, BizException) else (str(e) or "助手处理失败")
                # 流可能已被网关掐掉，失败也要写进会话投影，切页再拉才能看见气泡。
                try:
                    persist_assistant_error(
                        db,
                        err_text,
                        conversation_id=conv.id if conv is not None else None,
                        session_id=session.id if session is not None else None,
                    )
                except Exception:  # noqa: BLE001
                    db.rollback()
                events.put({"type": "error", "message": err_text})
            finally:
                db.close()
                events.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            event = events.get()
            if event is None:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        # nginx 默认缓冲响应，会把进度事件憋到最后一次性吐出
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/ai/tool", summary="执行技能")
def run_tool(body: dict, db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    tool = body.get("tool") or body.get("name") or ""
    params = body.get("params") or body.get("arguments") or {}
    confirmed = bool(body.get("user_confirmed"))
    return R.ok(execute_tool(db, tool, params, current, confirmed=confirmed))


@router.get("/ai/sessions", summary="列出 AI 会话")
def sessions_list(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import list_sessions

    return R.ok(list_sessions(db, current.id, limit=limit))


@router.post("/ai/sessions", summary="新建 AI 会话")
def sessions_create(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import create_session, session_public

    title = str(body.get("title") or "新会话")[:128]
    model_pk = int(body.get("model_id") or 0) or None
    if model_pk:
        model_pk = _usable_or_reject(db, model_pk)
    legacy = AiConversation(user_id=current.id, title=title, working_json="{}")
    db.add(legacy)
    db.commit()
    db.refresh(legacy)
    row = create_session(
        db,
        current.id,
        title=title,
        legacy_conversation_id=legacy.id,
        model_pk=model_pk,
    )
    return R.ok(session_public(row))


@router.patch("/ai/sessions/{session_id}", summary="更新会话选项（当前仅切换模型）")
def sessions_patch(
    session_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import session_public

    row = _owned_session(db, session_id, current.id)
    if "model_id" in body:
        pk = int(body.get("model_id") or 0)
        row.model_pk = _usable_or_reject(db, pk) if pk else None
        db.commit()
        db.refresh(row)
    return R.ok(session_public(row))


@router.delete("/ai/sessions/{session_id}", summary="删除 AI 会话")
def sessions_delete(
    session_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import delete_session

    _owned_session(db, session_id, current.id)
    delete_session(db, session_id)
    return R.ok()


@router.post("/ai/sessions/{session_id}/clear", summary="清空会话聊天记录和上下文")
def sessions_clear(
    session_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import clear_session

    _owned_session(db, session_id, current.id)
    return R.ok(clear_session(db, session_id))


@router.get("/ai/sessions/{session_id}/images/{name}", summary="读取会话里用户发出的图片")
def session_image(
    session_id: int,
    name: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """气泡 <img> 不能带头，短链仍走 Bearer。前端用带鉴权的 blob 请求再画。"""
    from app.modules.ai import chat_images

    _owned_session(db, session_id, current.id)
    path, media = chat_images.resolve(session_id, name)
    return FileResponse(path, media_type=media)


@router.get("/ai/sessions/{session_id}", summary="读取会话消息投影")
def sessions_get(
    session_id: int,
    after_id: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import live_turn_of

    row = _owned_session(db, session_id, current.id)
    watching = False
    if row.legacy_conversation_id:
        watching = (
            db.scalar(
                select(AiWatch.id).where(
                    AiWatch.conversation_id == row.legacy_conversation_id,
                    AiWatch.status == "watching",
                )
            )
            is not None
        )
    return R.ok(
        {
            "session_id": row.id,
            "conversation_id": row.legacy_conversation_id,
            "messages": list_session_messages(db, row.id, after_id=after_id),
            "watching": watching,
            "live": live_turn_of(db, row.id),
        }
    )


@router.get("/ai/sessions/{session_id}/inspect", summary="检查会话事件与投影")
def sessions_inspect(
    session_id: int,
    after_seq: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import inspect_session

    _owned_session(db, session_id, current.id)
    return R.ok(inspect_session(db, session_id, after_seq=after_seq))


@router.get("/ai/sessions/{session_id}/search", summary="搜索会话事件")
def sessions_search(
    session_id: int,
    q: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import search_events

    _owned_session(db, session_id, current.id)
    return R.ok(search_events(db, session_id, q, limit=limit))


@router.post("/ai/sessions/{session_id}/resume", summary="冷恢复未完成会话")
def sessions_resume(
    session_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import resume_session

    _owned_session(db, session_id, current.id)
    return R.ok(resume_session(db, session_id))


@router.post("/ai/sessions/{session_id}/fork", summary="从指定事件分支会话")
def sessions_fork(
    session_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import fork_session, session_public

    _owned_session(db, session_id, current.id)
    row = fork_session(
        db,
        session_id,
        current.id,
        at_seq=body.get("at_seq"),
        title=str(body.get("title") or ""),
    )
    return R.ok(session_public(row))


@router.post("/ai/sessions/{session_id}/replay", summary="只读重放；重新运行会显式创建分支")
def sessions_replay(
    session_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.ai.sessions import fork_session, replay_session, session_public

    _owned_session(db, session_id, current.id)
    if body.get("rerun"):
        if not body.get("fork"):
            raise BizException.bad_request("rerun 必须显式设置 fork=true，原会话不会被修改")
        row = fork_session(db, session_id, current.id, at_seq=body.get("at_seq"))
        return R.ok({"read_only": False, "fork": session_public(row)})
    return R.ok(replay_session(db, session_id))
