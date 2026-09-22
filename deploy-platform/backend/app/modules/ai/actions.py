"""助手页确认卡片：真正发起发布并登记跟进。"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, check_permission
from app.core.response import BizException
from app.modules.ai.followup import add_watch
from app.modules.ai.history import append_message
from app.modules.pipeline import service
from app.modules.pipeline.models import Pipeline


def perform_action(
    db: Session,
    current: CurrentUser,
    conversation_id: int,
    action_type: str,
    payload: dict,
    *,
    persist: bool = True,
) -> dict:
    from app.modules.audit.context import bind_skill, reset_skill

    payload = payload or {}
    skill_token = bind_skill(action_type)
    try:
        return _perform_action(db, current, conversation_id, action_type, payload, persist=persist)
    finally:
        reset_skill(skill_token)


def _perform_action(
    db: Session,
    current: CurrentUser,
    conversation_id: int,
    action_type: str,
    payload: dict,
    *,
    persist: bool = True,
) -> dict:
    release = None
    text = ""

    if action_type == "confirm_release":
        pipeline_id = int(payload["pipeline_id"])
        if not check_permission(db, current, "pipeline", pipeline_id, "execute"):
            raise BizException.forbidden(f"无权限：对流水线 #{pipeline_id} 发起发布")
        # 页面上点「执行」会把流水线变量的默认值一起带进来，助手这条路原来没带，
        # 于是步骤参数里的 ${{VAR}} 拿不到值，插件收到的是没替换的字面量。
        # 同一条流水线，从页面发能成、让助手发就挂，原因还全在日志深处
        from app.modules.pipeline.sub_pipeline import merge_params
        from app.modules.deploy.service import apply_execute_manifest

        pipe = service.get_pipeline(db, pipeline_id)
        run_params = apply_execute_manifest(
            pipe, merge_params(pipe, None), payload.get("deploy_manifest")
        )
        release = service.create_release(
            db,
            pipeline_id=pipeline_id,
            version=str(payload.get("version") or ""),
            strategy=str(payload.get("strategy") or "rolling"),
            trigger_by="ai",
            operator_id=current.id,
            source_ref=payload.get("source_ref"),
            run_params=run_params,
        )
        if not (release.source_ref or "").strip():
            from app.modules.pipeline import source_ref_service

            source_ref_service.fill_source_ref_later(release.id)
        start_err = ""
        if release.status == "queued":
            release, start_err = service.try_execute_release(db, release.id)
        p = db.get(Pipeline, release.pipeline_id)
        name = p.name if p else pipeline_id
        if start_err:
            # 发布记录已经建出来了（且已被收尾成失败），别报成「没发起」让人重复点
            text = f"⚠️ 发布 #{release.id}（{name}）已创建，但没能启动：{start_err}"
        else:
            text = (
                f"✅ 已发起发布 #{release.id}（{name}），"
                f"当前状态：{release.status}。跑完后我会把结果发到这里。"
            )

    elif action_type == "confirm_node_push":
        # 这条路绕开了流水线，所以权限看的是节点而不是流水线；
        # nodepush.execute 内部会再验一次目录和附件归属，不信任卡片里的 payload
        from app.modules.ai import nodepush

        release, nodes, count = nodepush.execute(db, current, payload)
        target = str(payload.get("target_dir") or "")
        where = "、".join(n.name for n in nodes)
        scope = f"{count} 个文件 → {where}:{target}"
        if release.status == "pending":
            text = (
                f"📝 已提交下发申请 #{release.id}：{scope}。"
                "涉及生产节点，等审批通过才会真正落盘，可到「发布审批」处理。"
            )
        else:
            text = (
                f"✅ 已开始下发 #{release.id}：{scope}。"
                "覆盖前会自动备份，跑完我回报结果。"
            )

    elif action_type == "confirm_rollback":
        rid = int(payload["release_id"])
        src = service.get_release(db, rid)
        if not check_permission(db, current, "pipeline", src.pipeline_id, "execute"):
            raise BizException.forbidden(f"无权限：回滚流水线 #{src.pipeline_id}")
        release = service.rollback_release(db, rid, current.id)
        if release.status == "pending":
            text = f"📝 回滚单 #{release.id} 已提交审批，通过后才会执行。可到「发布审批」处理。"
        else:
            text = f"✅ 已发起回滚，新发布 #{release.id}。跑完后我会回报状态。"

    elif action_type == "confirm_rebuild":
        rid = int(payload["release_id"])
        src = service.get_release(db, rid)
        if not check_permission(db, current, "pipeline", src.pipeline_id, "execute"):
            raise BizException.forbidden(f"无权限：Rebuild 流水线 #{src.pipeline_id}")
        release = service.rebuild_release(db, rid, current.id)
        if release.status == "pending":
            text = f"📝 Rebuild 单 #{release.id} 已提交审批，通过后才会执行。可到「发布审批」处理。"
        else:
            text = f"✅ 已 Rebuild，新发布 #{release.id}。跑完后我会回报状态。"

    elif action_type == "confirm_cancel":
        rid = int(payload["release_id"])
        src = service.get_release(db, rid)
        if not check_permission(db, current, "pipeline", src.pipeline_id, "execute"):
            raise BizException.forbidden(f"无权限：取消流水线 #{src.pipeline_id} 的发布")
        release = service.cancel_release(db, rid, current.id)
        text = f"✅ 已取消发布 #{rid}。"

    elif action_type == "confirm_approve":
        rid = int(payload["release_id"])
        src = service.get_release(db, rid)
        if not check_permission(db, current, "group", src.group_id, "approve"):
            raise BizException.forbidden(f"无权限：审批分组 #{src.group_id}")
        approved = bool(payload.get("approved", True))
        comment = str(payload.get("comment") or "").strip()
        release = service.approve_release(db, rid, approved, comment, reviewer_id=current.id)
        start_err = ""
        if approved and release.status == "queued":
            # 审批已落库，拉起执行失败不能报成审批失败——否则用户会反复点，
            # 而后面几下只会撞上「当前状态 xx 不允许审批」
            release, start_err = service.try_execute_release(db, release.id)
        if start_err:
            text = f"⚠️ 审批已通过（发布 #{rid}），但发布没能启动：{start_err}"
        else:
            text = f"✅ 审批已处理（发布 #{rid}，{release.status}）。"
            if approved:
                text += "通过后的执行结果我会跟进。"

    elif action_type == "confirm_agent_skill":
        from app.modules.ai.skills.skill_authoring import install_proposed

        owner_user_id = None if current.is_admin else current.id
        installed = install_proposed(
            db,
            payload,
            actor_id=current.id,
            actor_name=current.username,
            owner_user_id=owner_user_id,
        )
        if installed.get("personal"):
            text = (
                f"✅ Agent 技能「{installed['display_name']}」(`{installed['name']}`) 已安装到你的助手。\n"
                "别人看不到这份技能。到「技能库 → Agent 技能」可以停用自己装的。"
                "这不是流水线插件，编排器里不会多出步骤。"
            )
        else:
            text = (
                f"✅ Agent 技能「{installed['display_name']}」(`{installed['name']}`) 已安装并启用。\n"
                "到「技能库 → Agent 技能」可以停用。这不是流水线插件，编排器里不会多出步骤。"
            )

    elif action_type == "confirm_agent_skill_lifecycle":
        from app.modules.ai.skills.skill_lifecycle import apply_lifecycle

        changed = apply_lifecycle(db, current, payload)
        text = f"✅ {changed['done']} Agent 技能「{changed['display_name']}」(`{changed['name']}`)。"

    elif action_type == "confirm_plugin_draft":
        # 只落草稿：不进插件仓库、不安装、流水线选不到。发布和安装都还要管理员另外点。
        from app.modules.store import draft_service

        draft = draft_service.create_draft(
            db,
            meta=payload.get("meta") or {},
            files=payload.get("files") or {},
            intent=str(payload.get("intent") or ""),
            source="ai",
            created_by=current.id,
        )
        lint = json.loads(draft.lint_json or "{}")
        text = (
            f"📝 插件草稿 #{draft.id}「{draft.display_name or draft.name}」已存为待审。\n"
            f"体检：高危 {lint.get('high_count', 0)} 项、提醒 {lint.get('warn_count', 0)} 项。\n"
            "到「技能库 → 流水线插件 → 插件草稿」看完整代码，可以先挑一条有执行权的流水线试跑。"
            "发布到全局仓库并安装进编排器仍需要管理员。"
        )

    elif action_type in ("confirm_access_approve", "confirm_access_reject"):
        from app.modules.access.service import review as review_access

        aid = int(payload["application_id"])
        approved = action_type == "confirm_access_approve" or bool(payload.get("approved", False))
        if action_type == "confirm_access_reject":
            approved = False
        row = review_access(db, current, aid, approved, str(payload.get("comment") or ""))
        verb = "通过" if approved else "驳回"
        extra = f"已授予：{row.get('granted_action_labels') or row.get('granted_actions') or '查看、执行'}。" if approved else ""
        text = f"✅ 已{verb}权限申请 #{aid}（{row['pipeline_name']}）。{extra}"
    else:
        raise BizException.bad_request(f"未知操作 {action_type}")

    watching = False
    extra_text = ""
    if release is not None and action_type != "confirm_cancel":
        if release.status not in ("success", "failed", "cancelled", "rolled_back"):
            add_watch(db, conversation_id=conversation_id, user_id=current.id, release=release)
            watching = True
        elif release.status in ("success", "failed", "cancelled", "rolled_back"):
            from app.modules.ai.followup import format_one

            extra_text = format_one(db, release)

    reply = text if not extra_text else f"{text}\n\n{extra_text}"
    if not persist:
        return {
            "reply": reply,
            "release_id": release.id if release is not None else None,
            "status": release.status if release is not None else "",
            "watching": watching,
            "messages": [],
        }

    msg = append_message(db, conversation_id, "assistant", text, kind="action")
    if extra_text:
        extra = append_message(db, conversation_id, "assistant", extra_text, kind="followup")
        return {
            "reply": reply,
            "release_id": release.id,
            "status": release.status,
            "watching": False,
            "messages": [
                {"id": msg.id, "role": "assistant", "content": msg.content, "actions": [], "kind": "action"},
                {"id": extra.id, "role": "assistant", "content": extra.content, "actions": [], "kind": "followup"},
            ],
        }
    return {
        "reply": reply,
        "release_id": release.id if release is not None else None,
        "status": release.status if release is not None else "",
        "watching": watching,
        "messages": [{"id": msg.id, "role": "assistant", "content": msg.content, "actions": [], "kind": "action"}],
    }
