"""流水线服务（核心）。

包含流水线 CRUD、YAML 解析、可视化图形转换，以及发布任务状态机。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from app.core.response import BizException
from app.modules.pipeline.graph import as_exclusive_triggers, exclusive_trigger_of, to_graph
from app.modules.pipeline.models import Pipeline, Release, UserPipelinePref, UserViewPref
from app.modules.pipeline.schemas import (
    GraphJob,
    GraphStage,
    GraphStep,
    JobSpec,
    PipelineDefinition,
    PipelineGraph,
    PipelineSpec,
    StageSpec,
    StepSpec,
    TriggerSpec,
    dump_yaml,
    parse_yaml,
)
from app.modules.project.models import Group


def _apply_trigger_columns(p: Pipeline, items, *, require_valid: bool = True) -> None:
    """把「只能二选一」的触发方式写到列上，给定时扫描用，不再去解析 YAML。"""
    kind, expr = exclusive_trigger_of(items, require_valid=require_valid)
    p.trigger_type = kind
    p.cron_expr = expr


def _sync_trigger_schedule(p: Pipeline) -> None:
    """保存后立刻同步这一条的 Redis 队列。Redis 挂了不挡保存，靠 30 秒兜底。"""
    from app.modules.pipeline.scheduler import drop_cron_schedule, upsert_cron_schedule

    if p.status == "active" and p.trigger_type == "cron" and (p.cron_expr or "").strip():
        upsert_cron_schedule(p.id, p.cron_expr)
    else:
        drop_cron_schedule(p.id)


def _normalize_yaml_triggers(yaml_text: str, *, require_valid: bool = False) -> tuple[str, str, str]:
    """YAML 里若同时写了手动和定时，收成一条，并回写 YAML。"""
    defn = parse_yaml(yaml_text or "")
    kind, expr = exclusive_trigger_of(defn.pipeline.triggers, require_valid=require_valid)
    defn.pipeline.triggers = [TriggerSpec(type=kind, cron=expr or None)]
    return kind, expr, dump_yaml(defn)


def _apply_yaml_feature_columns(p: Pipeline) -> None:
    """从当前 YAML 写出清单标记和子流水线边，并记下派生列版本。

    列表筛选、环检测都读这些列，不再现场 parse 全站 YAML。
    """
    from app.modules.deploy.service import consumes_deploy_manifest
    from app.modules.pipeline.models import YAML_FEATURES_VER
    from app.modules.pipeline.sub_pipeline import collect_run_pipeline_targets, encode_sub_pipeline_ids

    p.uses_deploy_manifest = bool(consumes_deploy_manifest(p))
    p.sub_pipeline_ids = encode_sub_pipeline_ids(collect_run_pipeline_targets(p.yaml or ""))
    p.yaml_features_ver = YAML_FEATURES_VER


def backfill_trigger_columns(db: Session) -> int:
    """启动时补触发列、清单标记、子流水线边。只处理派生版本落后的行，不回写 YAML。"""
    from app.modules.pipeline.models import YAML_FEATURES_VER

    n = 0
    rows = db.scalars(select(Pipeline).where(Pipeline.yaml_features_ver < YAML_FEATURES_VER)).all()
    for p in rows:
        if (p.yaml or "").strip():
            if not (p.cron_expr or "").strip():
                try:
                    kind, expr, _ = _normalize_yaml_triggers(p.yaml, require_valid=False)
                except (ValueError, BizException):
                    kind, expr = p.trigger_type or "manual", ""
                if kind == "cron" and expr:
                    p.trigger_type = kind
                    p.cron_expr = expr
        _apply_yaml_feature_columns(p)
        n += 1
    if n:
        db.commit()
    return n


def build_no(r: Release) -> str:
    """通知/页面展示用的构建号（老数据没有构建号时退回主键）。"""
    return f"#{r.build_number or r.id}"


def _audit_release(
    db: Session,
    action: str,
    release: Release,
    detail: str,
    *,
    operator_id: int | None = None,
    trigger_by: str | None = None,
) -> None:
    """发布相关操作审计：username=操作人，source=入口（ai/web/cron），不是把操作人写成 AI。"""
    from app.modules.audit.context import current_source, current_user_id
    from app.modules.audit.service import write as write_audit

    src = current_source() or "web"
    tb = trigger_by or (release.trigger_by if release is not None else "")
    if tb == "ai":
        src = "ai"
    elif tb == "cron":
        src = "cron"
    elif tb == "webhook":
        src = "webhook"
    oid = operator_id if operator_id is not None else current_user_id()
    if oid is None and release is not None:
        oid = release.operator_id
    write_audit(db, action, "release", release.id if release is not None else None, detail, user_id=oid, source=src)

# ---- 发布状态常量 ----
RELEASE_PENDING = "pending"
RELEASE_QUEUED = "queued"
RELEASE_RUNNING = "running"
RELEASE_SUCCESS = "success"
RELEASE_FAILED = "failed"
RELEASE_REJECTED = "rejected"
RELEASE_ROLLING_BACK = "rolling_back"
RELEASE_ROLLED_BACK = "rolled_back"
RELEASE_CANCELLED = "cancelled"

# 同流水线互斥：这些状态下视为「正在执行/即将执行」，禁止再开一次
PIPELINE_BUSY_STATUSES = (RELEASE_QUEUED, RELEASE_RUNNING, RELEASE_ROLLING_BACK)
# 对同一份备份再开回滚单：待审批也算在飞，否则双击会各开一张、审批后各还原一次
ROLLBACK_IN_FLIGHT_STATUSES = (
    RELEASE_PENDING,
    RELEASE_QUEUED,
    RELEASE_RUNNING,
    RELEASE_ROLLING_BACK,
)


# ============================================================
# 流水线 CRUD
# ============================================================
PIPELINE_DELETED = "deleted"
# 回收站超期后编排不可恢复，执行记录/制品/部署记录留下，避免一键回滚对不上
PIPELINE_PURGED = "purged"
PIPELINE_ACTIVE = "active"
# 主列表不展示：回收站里的 + 超期只留历史的
PIPELINE_HIDDEN = (PIPELINE_DELETED, PIPELINE_PURGED)
# 回收站保留期：过期后编排不能再恢复，但关联执行数据不物理删除
RECYCLE_RETENTION_DAYS = 30


def list_pipelines(
    db: Session,
    project_id: int | None,
    group_id: int | None,
    *,
    deleted: bool = False,
    uses_deploy_manifest: bool | None = None,
    q: str | None = None,
    limit: int | None = None,
) -> list[Pipeline]:
    """按项目、分组列出流水线，不载 YAML。

    授权页几万条时不能一次倒出全站：先用 project_id / group_id 缩小范围，
    再按名称关键字 q 搜；limit 限制返回条数。
    """
    stmt = select(Pipeline).options(defer(Pipeline.yaml)).order_by(Pipeline.id.desc())
    if deleted:
        stmt = stmt.where(Pipeline.status == PIPELINE_DELETED)
    else:
        stmt = stmt.where(Pipeline.status.notin_(PIPELINE_HIDDEN))
    if project_id:
        stmt = stmt.where(Pipeline.project_id == project_id)
    if group_id:
        stmt = stmt.where(Pipeline.group_id == group_id)
    if uses_deploy_manifest:
        stmt = stmt.where(Pipeline.uses_deploy_manifest.is_(True))
    keyword = (q or "").strip()
    if keyword:
        stmt = stmt.where(Pipeline.name.contains(keyword))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def get_pipeline(db: Session, pipeline_id: int, *, allow_deleted: bool = False) -> Pipeline:
    p = db.get(Pipeline, pipeline_id)
    if p is None:
        raise BizException.not_found("流水线")
    if p.status == PIPELINE_DELETED and not allow_deleted:
        raise BizException.bad_request(
            f"流水线「{p.name}」已在回收站，先到项目的「回收站」恢复后再操作"
        )
    return p


def assert_name_available(
    db: Session, project_id: int, name: str, *, exclude_id: int | None = None
) -> None:
    """同项目内流水线名唯一（回收站和超期归档的不占名）。"""
    text = (name or "").strip()
    if not text:
        raise BizException.bad_request("流水线名称不能为空")
    stmt = select(Pipeline).where(
        Pipeline.project_id == project_id,
        Pipeline.name == text,
        Pipeline.status.notin_(PIPELINE_HIDDEN),
    )
    if exclude_id:
        stmt = stmt.where(Pipeline.id != exclude_id)
    dup = db.scalars(stmt).first()
    if dup is not None:
        raise BizException.bad_request(f"该项目下已有同名流水线「{text}」（#{dup.id}）")


def unique_pipeline_name(db: Session, project_id: int, base_name: str) -> str:
    """在项目里找一个还不占用的流水线名。被占了就在后面加 _2、_3。"""
    text = (base_name or "").strip()
    if not text:
        raise BizException.bad_request("流水线名称不能为空")
    final = text
    n = 1
    while db.scalars(
        select(Pipeline).where(
            Pipeline.project_id == project_id,
            Pipeline.name == final,
            Pipeline.status.notin_(PIPELINE_HIDDEN),
        )
    ).first():
        n += 1
        final = f"{text}_{n}"
    return final


def ensure_pipeline_workspace_uuid(db: Session, pipeline: Pipeline) -> str:
    """保证流水线有持久化的 workspace_uuid（旧数据启动时或首次执行时补齐）。"""
    import uuid

    if pipeline.workspace_uuid:
        return pipeline.workspace_uuid
    pipeline.workspace_uuid = uuid.uuid4().hex
    db.commit()
    db.refresh(pipeline)
    return pipeline.workspace_uuid


def assert_group_assignable(
    db: Session,
    project_id: int,
    new_group_id: int,
    *,
    from_group_id: int | None = None,
    is_admin: bool = False,
):
    """校验流水线能不能挂到目标分组下。

    分组决定了这条线发布时走不走审批，所以改分组等于改审批策略：
    不校验的话，有更新权限的人只要把生产流水线挂到测试分组，再发布就直接过了，
    审批形同虚设——而部署到哪台机器是步骤参数决定的，挪分组并不会真的发去测试环境。
    """
    from app.modules.project.models import Group

    group = db.get(Group, new_group_id)
    if group is None:
        raise BizException.not_found("分组")
    if group.project_id != project_id:
        raise BizException.bad_request("该分组不属于本项目，不能把流水线挂过去")
    if is_admin or from_group_id is None or from_group_id == new_group_id:
        return group
    src = db.get(Group, from_group_id)
    if src is not None and src.approval_required and not group.approval_required:
        raise BizException.forbidden(
            f"「{src.name}」需要发布审批，「{group.name}」不需要，"
            "改挂过去等于自己给自己免了审批。确需调整请让管理员操作"
        )
    return group


def assert_approval_mode(mode: str, *, can_exempt: bool) -> str:
    """校验流水线级审批模式，并守住「收紧免授权、放松要授权」这条线。

    force 谁都能设——把审批门槛调高不会有安全问题，越容易越好。
    exempt 必须有 approval_exempt 权限，否则有编辑权的人就能自己免掉自己的审批。

    这里刻意**不**去校验「目标分组有没有审批人」：管理员对所有分组天然有审批权，
    这个检查几乎永远通过，却要为此把全部用户扫一遍。真正会卡住的情况是
    「发起人自己就是唯一的审批人且没开自审」，那要等到发布时才知道是谁在发，
    配置阶段判断不了——交给 _open_approval 报错，那条消息已经写清了怎么办。
    """
    mode = (mode or "inherit").strip() or "inherit"
    if mode not in APPROVAL_MODES:
        raise BizException.bad_request(
            f"非法的审批模式 {mode}，可选：{'、'.join(APPROVAL_MODES)}"
        )
    if mode == "exempt" and not can_exempt:
        raise BizException.forbidden(
            "你没有「豁免审批」权限，不能把流水线设为免审批。"
            "可以设为强制审批或跟随环境；确需豁免请让管理员授权"
        )
    return mode


def assert_editor_view(value: object) -> str:
    """编辑器视图只允许列表或画布，其它值一律按非法请求拒绝。"""
    view = str(value or "form").strip() or "form"
    if view not in ("form", "canvas"):
        raise BizException.bad_request("编排视图只能是 form（列表）或 canvas（画布）")
    return view


def create_pipeline(
    db: Session,
    data: dict,
    created_by: int,
    *,
    can_exempt: bool = False,
    skip_nested_check: bool = False,
) -> Pipeline:
    """新建流水线。skip_nested_check 给导入用：先落盘再改 run-pipeline 的目标 id。"""
    import uuid

    yaml_text = data.get("yaml", "")
    trigger_kind, cron_expr = "manual", ""
    if yaml_text:
        try:
            trigger_kind, cron_expr, yaml_text = _normalize_yaml_triggers(yaml_text)
        except ValueError as e:
            raise BizException.bad_request(str(e))

    assert_name_available(db, data["project_id"], data.get("name", ""))
    p = Pipeline(
        project_id=data["project_id"],
        group_id=data["group_id"],
        name=data["name"].strip(),
        description=data.get("description", ""),
        yaml=yaml_text,
        version=1,
        status="active",
        created_by=created_by,
        workspace_uuid=uuid.uuid4().hex,
        approval_mode=assert_approval_mode(
            data.get("approval_mode", "inherit"), can_exempt=can_exempt
        ),
        editor_view=assert_editor_view(data.get("editor_view") or data.get("mode") or "form"),
        trigger_type=trigger_kind,
        cron_expr=cron_expr,
    )
    _apply_yaml_feature_columns(p)
    if not skip_nested_check:
        from app.modules.pipeline.sub_pipeline import assert_yaml_nested_isolated

        assert_yaml_nested_isolated(db, p)
    db.add(p)
    db.commit()
    db.refresh(p)
    # 个人分组是创建人自己的视图，跟流水线实体无关。在当前组里点新建，
    # 新线就该落进这个组，否则「个人分组」只是个筛子、装不进新东西
    if data.get("folder"):
        set_pipeline_folder(db, created_by, p.id, str(data.get("folder") or ""))
    _sync_trigger_schedule(p)
    return p


def update_pipeline(
    db: Session,
    pipeline_id: int,
    data: dict,
    *,
    is_admin: bool = False,
    can_exempt: bool = False,
    operator_id: int | None = None,
    skip_nested_check: bool = False,
) -> Pipeline:
    p = get_pipeline(db, pipeline_id)
    if p.status != PIPELINE_ACTIVE:
        raise BizException.bad_request(
            f"流水线「{p.name}」当前不可编辑（状态 {p.status}）"
        )
    if "name" in data and data["name"] and data["name"].strip() != p.name:
        assert_name_available(db, p.project_id, data["name"], exclude_id=p.id)
        p.name = data["name"].strip()
        # YAML 里也存了一份流水线名，改名后要同步，否则编排导出/复制会带着旧名字
        if p.yaml:
            from app.modules.pipeline.schemas import dump_yaml

            try:
                definition = parse_yaml(p.yaml)
                definition.pipeline.name = p.name
                p.yaml = dump_yaml(definition)
            except ValueError:
                pass
    if "description" in data:
        p.description = data["description"]
    if "yaml" in data:
        if data["yaml"]:
            try:
                kind, expr, yaml_text = _normalize_yaml_triggers(data["yaml"], require_valid=True)
            except ValueError as e:
                raise BizException.bad_request(str(e))
            p.yaml = yaml_text
            p.trigger_type = kind
            p.cron_expr = expr
            _apply_yaml_feature_columns(p)
            if not skip_nested_check:
                from app.modules.pipeline.sub_pipeline import assert_yaml_nested_isolated

                assert_yaml_nested_isolated(db, p)
        else:
            p.yaml = ""
            p.trigger_type = "manual"
            p.cron_expr = ""
            _apply_yaml_feature_columns(p)
    if "group_id" in data and data["group_id"] and data["group_id"] != p.group_id:
        assert_group_assignable(
            db, p.project_id, data["group_id"], from_group_id=p.group_id, is_admin=is_admin
        )
        p.group_id = data["group_id"]
        from app.modules.pipeline.sub_pipeline import assert_yaml_nested_isolated

        assert_yaml_nested_isolated(db, p)
    if "approval_mode" in data:
        old_mode = p.approval_mode or "inherit"
        new_mode = assert_approval_mode(data["approval_mode"], can_exempt=can_exempt)
        if new_mode != old_mode:
            p.approval_mode = new_mode
            # 审批策略变更必须留痕：出了事要能查出「这条线是什么时候、被谁免掉审批的」
            from app.modules.audit.service import write as write_audit

            write_audit(
                db,
                "pipeline.approval_mode",
                "pipeline",
                p.id,
                f"流水线「{p.name}」审批模式 {old_mode} → {new_mode}",
                user_id=operator_id,
            )
    if "editor_view" in data:
        p.editor_view = assert_editor_view(data["editor_view"])
    # 只切编辑视图不算一次编排改动，避免点一下画布就把版本号 +1
    if set(data.keys()) <= {"editor_view"}:
        db.commit()
        db.refresh(p)
        return p
    p.version = (p.version or 0) + 1
    p.updated_at = datetime.now()
    db.commit()
    db.refresh(p)
    _sync_trigger_schedule(p)
    return p


def delete_pipeline(db: Session, pipeline_id: int, operator_id: int | None = None) -> Pipeline:
    """删除流水线 → 进回收站。

    执行记录、构建任务、审计都留着，保留期内可以原样恢复。
    正在执行时不允许删，否则构建机还在跑一条已经"不存在"的流水线。
    """
    p = get_pipeline(db, pipeline_id)
    busy = db.scalars(
        select(Release).where(
            Release.pipeline_id == pipeline_id,
            Release.status.in_(PIPELINE_BUSY_STATUSES),
        )
    ).first()
    if busy is not None:
        raise BizException.bad_request(
            f"流水线正在执行（发布 {build_no(busy)}，状态 {busy.status}），先取消或等它结束再删除"
        )

    # 等审批的发布跟着撤掉，否则审批人那里会挂一条永远点不动的待办
    waiting = db.scalars(
        select(Release).where(
            Release.pipeline_id == pipeline_id, Release.status == RELEASE_PENDING
        )
    ).all()
    for r in waiting:
        cancel_release(db, r.id, operator_id or 0)

    p.status = PIPELINE_DELETED
    p.deleted_at = datetime.now()
    p.deleted_by = operator_id
    db.commit()
    db.refresh(p)
    _drop_cron_schedule(pipeline_id)
    return p


def _drop_cron_schedule(pipeline_id: int) -> None:
    """把定时触发条目从 Redis 里摘掉。删除进回收站后调度器不再扫到这条。"""
    from app.modules.pipeline.scheduler import drop_cron_schedule

    drop_cron_schedule(pipeline_id)


def restore_pipeline(db: Session, pipeline_id: int, new_name: str | None = None) -> Pipeline:
    """从回收站恢复。原名被占用时必须给个新名字。"""
    p = get_pipeline(db, pipeline_id, allow_deleted=True)
    if p.status != PIPELINE_DELETED:
        raise BizException.bad_request("该流水线不在回收站")
    name = (new_name or p.name).strip()
    assert_name_available(db, p.project_id, name, exclude_id=p.id)
    p.name = name
    p.status = PIPELINE_ACTIVE
    p.deleted_at = None
    p.deleted_by = None
    db.commit()
    db.refresh(p)
    _sync_trigger_schedule(p)
    return p


def _purge_pipeline_rows(db: Session, pipeline: Pipeline) -> None:
    """物理删除流水线及其关联数据（不提交事务）。

    这些表都是靠 pipeline_id / release_id 关联的裸字段，没有数据库外键，
    不主动清就会变成永远查不到主体的孤儿行。审计日志故意保留。
    """
    from app.modules.agent.models import BuildTask, BuildTaskLog
    from app.modules.ai.models import AiWatch
    from app.modules.approval.models import Approval
    from app.modules.artifact.models import Artifact
    from app.modules.auth.models import Permission, PermissionApplication

    release_ids = list(
        db.scalars(select(Release.id).where(Release.pipeline_id == pipeline.id)).all()
    )
    if release_ids:
        db.query(Approval).filter(Approval.release_id.in_(release_ids)).delete(
            synchronize_session=False
        )
    # 日志分块挂在 task 上，先按 task 清掉，否则删完任务就再也定位不到这些行
    task_ids = list(
        db.scalars(select(BuildTask.id).where(BuildTask.pipeline_id == pipeline.id)).all()
    )
    if task_ids:
        db.query(BuildTaskLog).filter(BuildTaskLog.task_id.in_(task_ids)).delete(
            synchronize_session=False
        )
    db.query(BuildTask).filter(BuildTask.pipeline_id == pipeline.id).delete(
        synchronize_session=False
    )
    # 先清磁盘再删记录：记录没了就再也找不到这些文件，只能靠人去翻目录
    from app.modules.artifact import storage as artifact_storage

    for rid in release_ids:
        artifact_storage.remove_release_artifacts(rid)
    db.query(Artifact).filter(Artifact.pipeline_id == pipeline.id).delete(
        synchronize_session=False
    )
    db.query(AiWatch).filter(AiWatch.pipeline_id == pipeline.id).delete(
        synchronize_session=False
    )
    db.query(PermissionApplication).filter(
        PermissionApplication.pipeline_id == pipeline.id
    ).delete(synchronize_session=False)
    db.query(Permission).filter(
        Permission.resource_type == "pipeline", Permission.resource_id == pipeline.id
    ).delete(synchronize_session=False)
    db.query(Release).filter(Release.pipeline_id == pipeline.id).delete(
        synchronize_session=False
    )
    db.delete(pipeline)


def purge_pipeline(db: Session, pipeline_id: int) -> None:
    """彻底删除（不可恢复）。只允许对回收站里的流水线执行。"""
    p = get_pipeline(db, pipeline_id, allow_deleted=True)
    if p.status != PIPELINE_DELETED:
        raise BizException.bad_request("请先删除到回收站，再彻底删除")
    _purge_pipeline_rows(db, p)
    db.commit()
    _drop_cron_schedule(pipeline_id)


def _expire_pipeline_from_recycle(db: Session, pipeline: Pipeline) -> None:
    """回收站超期：编排不可恢复，执行记录/制品/部署记录留下。

    以前这里会物理删掉 Release、构建任务和制品，节点上的备份还在，
    平台一键回滚却对不上。超期只收回「再跑这条线」的能力，历史还能查。
    管理员点彻底删除仍走 _purge_pipeline_rows。
    """
    pipeline.status = PIPELINE_PURGED
    pipeline.yaml = ""


def purge_expired_pipelines(db: Session, retention_days: int = RECYCLE_RETENTION_DAYS) -> int:
    """回收站超期：编排归档为不可恢复，返回处理条数。"""
    from datetime import timedelta

    cutoff = datetime.now() - timedelta(days=retention_days)
    expired = db.scalars(
        select(Pipeline).where(
            Pipeline.status == PIPELINE_DELETED,
            Pipeline.deleted_at != None,  # noqa: E711
            Pipeline.deleted_at < cutoff,
        )
    ).all()
    ids = [p.id for p in expired]
    for p in expired:
        _expire_pipeline_from_recycle(db, p)
    if expired:
        db.commit()
        for pid in ids:
            _drop_cron_schedule(pid)
    return len(expired)


def duplicate_pipeline(
    db: Session,
    source_id: int,
    new_name: str | None,
    new_group_id: int | None,
    new_description: str | None,
    operator_id: int,
    is_admin: bool = False,
    folder: str | None = None,
) -> Pipeline:
    """复制流水线：复用整份 YAML 字符串（编排/triggers/variables/stages/jobs/steps 全部一起复制）。

    YAML 字符串是不可变 copy，A 和 B 完全独立——改 A 不会影响 B。
    name 默认加 _copy 后缀；项目内 name 唯一，自动追加 _2/_3 避免重名。
    """
    src = get_pipeline(db, source_id)
    if new_group_id and new_group_id != src.group_id:
        # 复制到免审批分组同样能绕开审批，和直接改分组是一条路
        assert_group_assignable(
            db, src.project_id, new_group_id, from_group_id=src.group_id, is_admin=is_admin
        )

    base_name = (new_name or (src.name + "_copy")).strip()
    if not base_name:
        base_name = src.name + "_copy"
    final_name = unique_pipeline_name(db, src.project_id, base_name)

    import uuid

    new_pipeline = Pipeline(
        project_id=src.project_id,
        group_id=new_group_id or src.group_id,
        name=final_name,
        description=new_description if new_description is not None else src.description,
        yaml=src.yaml,  # 整份 YAML 复用（immutable，天然隔离）
        version=1,
        status="active",
        created_by=operator_id,
        workspace_uuid=uuid.uuid4().hex,  # 复制后独立工作区，不共用源流水线目录
        # 审批模式只继承收紧的那一档：force 跟着走，exempt 退回 inherit。
        # 复制不做权限校验，要是把 exempt 也带过来，「复制一份免审批的线」
        # 就成了没有豁免权限的人拿到免审批流水线的现成路子
        approval_mode="force" if (src.approval_mode or "") == "force" else "inherit",
        editor_view=assert_editor_view(getattr(src, "editor_view", None) or "form"),
        trigger_type=src.trigger_type or "manual",
        cron_expr=src.cron_expr or "",
        uses_deploy_manifest=bool(getattr(src, "uses_deploy_manifest", False)),
        sub_pipeline_ids=getattr(src, "sub_pipeline_ids", "") or "",
        yaml_features_ver=getattr(src, "yaml_features_ver", 0) or 0,
    )
    _apply_yaml_feature_columns(new_pipeline)
    from app.modules.pipeline.sub_pipeline import assert_yaml_nested_isolated

    assert_yaml_nested_isolated(db, new_pipeline)
    db.add(new_pipeline)
    db.commit()
    db.refresh(new_pipeline)
    # 没指定分组时，跟着源流水线在「我这边」的个人分组走，
    # 复制出来还在同一个工作篮里，不用再拖一次
    if folder is None:
        src_pref = db.scalar(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == operator_id,
                UserPipelinePref.pipeline_id == src.id,
            )
        )
        folder = (src_pref.folder if src_pref else "") or ""
    if folder:
        set_pipeline_folder(db, operator_id, new_pipeline.id, folder)
    _sync_trigger_schedule(new_pipeline)
    return new_pipeline


# ============================================================
# 个人分组（当前用户私有，看得到项目就能建）
# ============================================================
# 分组名存在 UserViewPref，和流水线偏好分开：空组也要能站住，
# 否则「先建组再往里放」这件事根本做不成，个人分组就只剩一个标签副作用。
_FOLDER_RESERVED = frozenset({"我的收藏", "全部流水线", "未分组"})


def _folder_scope(project_id: int) -> str:
    return f"personal_folders:{int(project_id)}"


def _normalize_folder_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise BizException.bad_request("分组名不能为空")
    if len(name) > 64:
        raise BizException.bad_request("分组名最多 64 个字")
    if name in _FOLDER_RESERVED:
        raise BizException.bad_request(f"「{name}」是系统保留名，换一个")
    return name


def _load_folder_catalog(db: Session, user_id: int, project_id: int) -> list[str]:
    row = db.scalar(
        select(UserViewPref).where(
            UserViewPref.user_id == user_id,
            UserViewPref.scope == _folder_scope(project_id),
        )
    )
    if row is None or not row.data_json:
        return []
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return []
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        n = str(raw or "").strip()[:64]
        if n and n not in seen and n not in _FOLDER_RESERVED:
            seen.add(n)
            out.append(n)
    return out


def _save_folder_catalog(db: Session, user_id: int, project_id: int, names: list[str]) -> None:
    text = json.dumps({"names": names}, ensure_ascii=False)
    row = db.scalar(
        select(UserViewPref).where(
            UserViewPref.user_id == user_id,
            UserViewPref.scope == _folder_scope(project_id),
        )
    )
    if row is None:
        db.add(UserViewPref(user_id=user_id, scope=_folder_scope(project_id), data_json=text))
    else:
        row.data_json = text


def _used_folder_names(db: Session, user_id: int, project_id: int) -> list[str]:
    """这个用户在本项目里实际用过的分组名（流水线还挂在上面的）。"""
    rows = db.execute(
        select(UserPipelinePref.folder)
        .join(Pipeline, Pipeline.id == UserPipelinePref.pipeline_id)
        .where(
            UserPipelinePref.user_id == user_id,
            Pipeline.project_id == project_id,
            UserPipelinePref.folder != "",
        )
    ).all()
    seen: set[str] = set()
    out: list[str] = []
    for (name,) in rows:
        n = (name or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def list_personal_folders(db: Session, user_id: int, project_id: int) -> list[str]:
    """目录 ∪ 实际在用的名字。

    旧数据只有流水线偏好、没有目录。第一次拉列表时把用过的名字写进目录，
    之后就算组被清空，空组也还在，直到用户自己删。
    """
    catalog = _load_folder_catalog(db, user_id, project_id)
    used = _used_folder_names(db, user_id, project_id)
    merged = list(catalog)
    seen = set(catalog)
    grew = False
    for n in used:
        if n not in seen:
            merged.append(n)
            seen.add(n)
            grew = True
    if grew:
        _save_folder_catalog(db, user_id, project_id, merged)
        db.commit()
    return merged


def create_personal_folder(db: Session, user_id: int, project_id: int, name: str) -> list[str]:
    name = _normalize_folder_name(name)
    names = list_personal_folders(db, user_id, project_id)
    if name not in names:
        names.append(name)
        _save_folder_catalog(db, user_id, project_id, names)
        db.commit()
    return names


def rename_personal_folder(
    db: Session, user_id: int, project_id: int, old_name: str, new_name: str
) -> list[str]:
    old_name = _normalize_folder_name(old_name)
    new_name = _normalize_folder_name(new_name)
    names = list_personal_folders(db, user_id, project_id)
    if old_name not in names:
        raise BizException.not_found("个人分组")
    if new_name != old_name and new_name in names:
        raise BizException.bad_request(f"已经有叫「{new_name}」的分组了")
    names = [new_name if n == old_name else n for n in names]
    # 去重保序：rename 到自己时不变
    dedup: list[str] = []
    seen: set[str] = set()
    for n in names:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    _save_folder_catalog(db, user_id, project_id, dedup)
    if new_name != old_name:
        prefs = db.scalars(
            select(UserPipelinePref)
            .join(Pipeline, Pipeline.id == UserPipelinePref.pipeline_id)
            .where(
                UserPipelinePref.user_id == user_id,
                Pipeline.project_id == project_id,
                UserPipelinePref.folder == old_name,
            )
        ).all()
        for pref in prefs:
            pref.folder = new_name
    db.commit()
    return dedup


def delete_personal_folder(db: Session, user_id: int, project_id: int, name: str) -> list[str]:
    name = _normalize_folder_name(name)
    names = [n for n in list_personal_folders(db, user_id, project_id) if n != name]
    _save_folder_catalog(db, user_id, project_id, names)
    prefs = db.scalars(
        select(UserPipelinePref)
        .join(Pipeline, Pipeline.id == UserPipelinePref.pipeline_id)
        .where(
            UserPipelinePref.user_id == user_id,
            Pipeline.project_id == project_id,
            UserPipelinePref.folder == name,
        )
    ).all()
    for pref in prefs:
        pref.folder = ""
    db.commit()
    return names


def set_pipeline_folder(db: Session, user_id: int, pipeline_id: int, folder: str) -> None:
    """把一条流水线放进当前用户的某个个人分组。folder 空串 = 移出。

    新建/复制流水线时顺手归入当前正在看的组，用的就是这个。
    名字还不在目录里时一并建上，避免「归入了，左侧却没有这个组」。

    目录更新和偏好必须同一次提交：组已经在目录里时如果只改目录、不 commit，
    偏好就写不进去；本会话 autoflush 又是关的，下一次再查会以为没有记录、再插一行。
    """
    folder = (folder or "").strip()[:64]
    if folder:
        folder = _normalize_folder_name(folder)
    pref = db.scalar(
        select(UserPipelinePref).where(
            UserPipelinePref.user_id == user_id,
            UserPipelinePref.pipeline_id == pipeline_id,
        )
    )
    if pref is None:
        pref = UserPipelinePref(user_id=user_id, pipeline_id=pipeline_id)
        db.add(pref)
    pref.folder = folder
    if folder:
        p = get_pipeline(db, pipeline_id)
        names = _load_folder_catalog(db, user_id, p.project_id)
        if folder not in names:
            names.append(folder)
            _save_folder_catalog(db, user_id, p.project_id, names)
    db.commit()


# ============================================================
# 可视化图形（核心 API）
# ============================================================
def get_graph(db: Session, pipeline_id: int) -> PipelineGraph:
    p = get_pipeline(db, pipeline_id)
    definition = parse_yaml(p.yaml)
    return to_graph(definition, p.id, p.name, p.version)


def save_graph(db: Session, pipeline_id: int, graph: PipelineGraph) -> Pipeline:
    """将前端可视化编排结果保存为 YAML（Pipeline as Code）。"""
    if graph.open_cuts:
        raise BizException.bad_request(
            "还有断开的步骤连线，不能保存。请拉回箭头，或右键步骤拆成并行 Job。"
        )
    p = get_pipeline(db, pipeline_id)

    # 名字以库里为准：graph 是打开编辑器时拉的快照，期间别处改过名的话
    # 用快照里的 pipeline_name 回写会把改名冲掉。
    definition = _graph_to_definition(graph, name=p.name)
    p.yaml = dump_yaml(definition)
    _apply_trigger_columns(p, definition.pipeline.triggers, require_valid=True)
    _apply_yaml_feature_columns(p)
    p.version = (p.version or 0) + 1
    p.updated_at = datetime.now()
    db.commit()
    db.refresh(p)
    _sync_trigger_schedule(p)
    return p


def _graph_to_definition(graph: PipelineGraph, name: str | None = None) -> PipelineDefinition:
    """前端图形 → YAML 定义。触发器收成手动或定时恰好一条。"""
    triggers = [
        TriggerSpec(type=t.type, cron=t.cron)
        for t in as_exclusive_triggers(graph.triggers, require_valid=True)
    ]
    stages = []
    for gs in sorted(graph.stages, key=lambda s: s.order):
        jobs = []
        for gj in gs.jobs:
            steps = []
            for gst in gj.steps:
                steps.append(StepSpec(name=gst.name or "", plugin=gst.plugin, with_=gst.with_))
            jobs.append(
                JobSpec(
                    id=gj.id,
                    name=gj.name,
                    agent=gj.agent,
                    steps=steps,
                )
            )
        stages.append(StageSpec(name=gs.name, jobs=jobs))

    spec = PipelineSpec(
        name=name or graph.pipeline_name,
        triggers=triggers,
        stages=stages,
        variables=graph.variables or [],
        canvas_layout=graph.layout or {},
    )
    return PipelineDefinition(pipeline=spec)


# ============================================================
# 发布任务（状态机）
# ============================================================
# 列表接口不带这些列：快照 / 计划 / 诊断都是长文本，一页就能把 JSON 撑到几百 KB。
LIST_OMIT_COLUMNS = frozenset({
    "logs",
    "snapshot",
    "plan_json",
    "run_params_json",
    "outputs_json",
    "step_status",
    "diagnosis_text",
    "business_summary",
    "impact_scope",
    "announcement_text",
})


def list_releases(
    db: Session,
    pipeline_id: int | None = None,
    status: str | None = None,
    trigger_by: str | None = None,
    operator_id: int | None = None,
    project_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    visible_ids: list[int] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Release], int]:
    """发布列表（支持多维过滤：状态/触发/操作人/项目/日期范围），返回(当页, 总数)。

    发布记录只增不减，一条常跑的流水线几个月就能攒出几千条。全量返回的话，
    执行历史页每 3 秒轮询一次就是几 MB 起步，翻页也只是把已经进了内存的数据
    切一刀，没有意义。可见性过滤也放进 SQL：留在 Python 里筛会让每页凑不满。
    """
    from datetime import datetime

    from sqlalchemy import func

    from app.modules.pipeline.models import Pipeline

    stmt = select(Release).options(
        *(defer(getattr(Release, col)) for col in LIST_OMIT_COLUMNS if hasattr(Release, col))
    )
    if visible_ids is not None:
        if not visible_ids:
            return [], 0
        stmt = stmt.where(Release.pipeline_id.in_(visible_ids))
    if pipeline_id:
        stmt = stmt.where(Release.pipeline_id == pipeline_id)
    if status:
        stmt = stmt.where(Release.status == status)
    if trigger_by:
        stmt = stmt.where(Release.trigger_by == trigger_by)
    if operator_id:
        stmt = stmt.where(Release.operator_id == operator_id)
    if project_id:
        # project → pipelines → releases
        pipeline_ids = db.scalars(select(Pipeline.id).where(Pipeline.project_id == project_id)).all()
        if pipeline_ids:
            stmt = stmt.where(Release.pipeline_id.in_(pipeline_ids))
        else:
            return [], 0  # 项目下没流水线，直接返回空
    if date_from:
        try:
            stmt = stmt.where(Release.created_at >= datetime.fromisoformat(date_from))
        except Exception:
            pass
    if date_to:
        try:
            stmt = stmt.where(Release.created_at <= datetime.fromisoformat(date_to))
        except Exception:
            pass

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(Release.id.desc())
            .offset(max(page - 1, 0) * page_size)
            .limit(page_size)
        ).all()
    )
    return rows, total


def release_error_summary(
    release: Release, *, max_len: int = 500, db: Session | None = None
) -> str:
    """抽出给人看的失败原因。

    启动失败和步骤失败都会把一句写进 error_message（步骤失败在级联收尾时回填）。
    db 只给明细 / 诊断补历史空字段：会去 Redis/ES 读任务日志。列表禁止传入，
    否则一页里几条旧失败就会把整个列表接口拖到秒级。
    """
    if release.status not in (RELEASE_FAILED, RELEASE_REJECTED, RELEASE_CANCELLED):
        return ""
    text = (getattr(release, "error_message", None) or "").strip()
    if not text:
        # 历史 logs 列已停写。列表把该列 defer 掉了，这里绝不能 getattr 把它懒加载回来。
        try:
            from sqlalchemy import inspect as sa_inspect

            logs_unloaded = "logs" in sa_inspect(release).unloaded
        except Exception:
            logs_unloaded = not hasattr(release, "logs")
        if not logs_unloaded:
            text = (release.logs or "").strip()
    if not text and db is not None:
        text = _error_from_failed_tasks(db, release)
    if not text:
        if release.status == RELEASE_FAILED:
            return "发布已失败，但没有记录到失败原因"
        if release.status == RELEASE_REJECTED:
            return "审批已驳回"
        return "发布已取消"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("[系统]"):
            msg = ln[len("[系统]") :].strip()
            return (msg or ln)[:max_len]
    return lines[-1][:max_len]


def _error_from_failed_tasks(db: Session, release: Release) -> str:
    """从本发布失败任务的日志里抽出第一句有效错误。"""
    from app.modules.agent.models import BuildTask
    from app.modules.agent.task_service import failed_task_error_snippet

    tasks = db.scalars(
        select(BuildTask)
        .where(
            BuildTask.release_id == release.id,
            BuildTask.status.in_(("failed", "timeout")),
        )
        .order_by(BuildTask.id)
    ).all()
    for t in tasks:
        snippet = failed_task_error_snippet(db, t.id)
        if snippet:
            return snippet
    return ""


def set_release_error(release: Release, message: str) -> None:
    """记下启动/中断原因。这是业务状态，不是构建日志，禁止写入 logs 列。"""
    msg = (message or "").strip()
    if msg.startswith("[系统]"):
        msg = msg[len("[系统]") :].strip()
    release.error_message = msg[:512]


def decorate_releases(db: Session, releases: list[Release]) -> list[dict]:
    """给发布列表附加「项目名 / 流水线名 / 发布人」，避免前端只能看到数字 ID。

    R.ok 只序列化表列（__table__.columns），动态属性会被丢弃，
    所以这里手动拼 dict，让列表可读、可跳转。
    大字段（YAML 快照、诊断全文等）不进列表；失败原因只读 error_message，不去拉日志。
    """
    from app.modules.auth.models import User
    from app.modules.project.models import Project

    if not releases:
        return []

    pipe_ids = {r.pipeline_id for r in releases}
    pipes = {
        p.id: p
        for p in db.scalars(
            select(Pipeline).options(defer(Pipeline.yaml)).where(Pipeline.id.in_(pipe_ids))
        ).all()
    }
    proj_ids = {p.project_id for p in pipes.values()}
    projs = (
        {p.id: p for p in db.scalars(select(Project).where(Project.id.in_(proj_ids))).all()}
        if proj_ids
        else {}
    )
    user_ids = {r.operator_id for r in releases if r.operator_id}
    users = (
        {u.id: u for u in db.scalars(select(User).where(User.id.in_(user_ids))).all()}
        if user_ids
        else {}
    )

    group_ids = {r.group_id for r in releases if r.group_id}
    groups = (
        {g.id: g for g in db.scalars(select(Group).where(Group.id.in_(group_ids))).all()}
        if group_ids
        else {}
    )
    from app.modules.pipeline.source_ref_service import checkout_branch_of

    yaml_by_pipe = {
        row.id: row.yaml
        for row in db.execute(select(Pipeline.id, Pipeline.yaml).where(Pipeline.id.in_(pipe_ids))).all()
    }
    branch_by_pipe = {pid: checkout_branch_of(yml) for pid, yml in yaml_by_pipe.items()}

    bypass_on = platform_allows_emergency_bypass(db)
    out: list[dict] = []
    for r in releases:
        d = {
            c.key: getattr(r, c.key)
            for c in r.__table__.columns
            if c.key not in LIST_OMIT_COLUMNS
        }
        p = pipes.get(r.pipeline_id)
        d["pipeline_name"] = p.name if p else f"#{r.pipeline_id}"
        d["project_name"] = projs.get(p.project_id).name if p and p.project_id in projs else "—"
        u = users.get(r.operator_id) if r.operator_id else None
        d["operator_name"] = (u.display_name or u.username) if u else "—"
        # 回滚 / Rebuild 按钮要据此判断是否需要审批、能否应急跳审
        g = groups.get(r.group_id)
        d["group_type"] = g.type if g else ""
        d["approval_required"] = bool(g) and approval_required_for(g, p)
        d["allow_emergency_bypass"] = bool(
            g and getattr(g, "allow_emergency_bypass", False) and bypass_on
        )
        d["error_summary"] = release_error_summary(r)
        d["source_branch"] = branch_by_pipe.get(r.pipeline_id) or "master"
        out.append(d)
    return out


def get_release(db: Session, release_id: int) -> Release:
    r = db.get(Release, release_id)
    if r is None:
        raise BizException.not_found("发布任务")
    return r


def ensure_pipeline_idle(
    db: Session, pipeline_id: int, *, exclude_release_id: int | None = None
) -> None:
    """同一流水线同时只允许一次执行（queued/running/rolling_back）。

    不同流水线互不影响。通过锁住 Pipeline 行串行化并发请求，避免双开。
    """
    # 锁流水线行，防止两个请求同时通过空闲检查
    locked = db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise BizException.not_found("流水线")

    stmt = select(Release).where(
        Release.pipeline_id == pipeline_id,
        Release.status.in_(PIPELINE_BUSY_STATUSES),
    )
    if exclude_release_id is not None:
        stmt = stmt.where(Release.id != exclude_release_id)
    busy = db.scalars(stmt.order_by(Release.id.desc()).limit(1)).first()
    if busy is not None:
        raise BizException.bad_request(
            f"流水线 #{pipeline_id} 已有执行中的发布 #{busy.id}（状态 {busy.status}），"
            f"同一流水线不可并发执行，请等待完成或先取消后再试"
        )


def inflight_rollback_of(db: Session, original_release_id: int) -> Release | None:
    """对某次发布是否已有一张还在处理的回滚单。"""
    return db.scalars(
        select(Release)
        .where(
            Release.rollback_of_release_id == original_release_id,
            Release.status.in_(ROLLBACK_IN_FLIGHT_STATUSES),
        )
        .order_by(Release.id.desc())
    ).first()


def cancel_release(db: Session, release_id: int, operator_id: int) -> Release:
    """取消一次发布：把 release 及未完成的任务标记为 cancelled。

    用于执行卡死/用户主动取消的场景。
    """
    from datetime import datetime

    from app.modules.agent.models import BuildTask
    from app.modules.approval.service import cancel_pending_approvals
    from app.modules.notify import emit
    from app.modules.notify.service import reviewers_of_group

    r = get_release(db, release_id)
    if r.status in (RELEASE_SUCCESS, RELEASE_FAILED, RELEASE_REJECTED, RELEASE_ROLLED_BACK, RELEASE_CANCELLED):
        raise BizException.bad_request(f"当前状态 {r.status} 不可取消")
    was_pending = r.status == RELEASE_PENDING
    r.status = RELEASE_CANCELLED
    r.finished_at = datetime.now()

    # 发起人撤了发布，审批待办就没有意义了，必须跟着撤回
    cancel_pending_approvals(db, release_id=release_id, comment="发起人已取消该发布")
    from app.modules.pm.service import cancel_pending_pm

    cancel_pending_pm(db, release_id, comment="发起人已取消该发布")
    if was_pending:
        emit(
            db,
            "release.approval.reviewed",
            user_ids=reviewers_of_group(db, r.group_id),
            title=f"生产发布已撤回：{build_no(r)}",
            content=(
                f"发起人取消了该发布，审批待办已自动撤回\n"
                f"构建号：{build_no(r)}\n"
                f"流水线：#{r.pipeline_id}"
            ),
            link=f"/executions/{r.pipeline_id}/{r.id}",
            related_id=r.id,
            dedupe_key=f"release.approval.withdrawn:{r.id}",
        )

    # 取消未完成的任务（pending/assigned/running）
    unfinished = db.query(BuildTask).filter(
        BuildTask.release_id == release_id,
        BuildTask.status.in_(["pending", "assigned", "running"]),
    ).all()
    for t in unfinished:
        t.status = "cancelled"
        t.finished_at = datetime.now()

    # 这次发布拉起来的子流水线也要一起停。父的任务取消了没人再去轮询子发布，
    # 子发布就成了没人管的孤儿，还一直占着它自己那条流水线（queued/running 都算忙），
    # 那条线从此发不出新东西——而页面上父发布明明已经是「已取消」了
    children = db.query(Release).filter(
        Release.parent_release_id == release_id,
        Release.status.in_(list(PIPELINE_BUSY_STATUSES) + [RELEASE_PENDING]),
    ).all()
    for child in children:
        try:
            cancel_release(db, child.id, operator_id)
        except BizException:
            pass  # 子发布自己已经结束了，不影响父发布收尾

    _audit_release(
        db,
        "release.cancel",
        r,
        f"取消发布 #{r.id} 流水线#{r.pipeline_id}",
        operator_id=operator_id,
        trigger_by=r.trigger_by,
    )
    db.commit()
    db.refresh(r)
    from app.modules.notify import on_release_finished

    on_release_finished(r.id)
    return r


APPROVAL_MODES = ("inherit", "force", "exempt")


def approval_required_for(g: Group, p: Pipeline | None = None) -> bool:
    """这条流水线这次发布到底要不要审批。

    环境（分组）给出默认策略，流水线自己可以覆盖：force 强制要审、exempt 豁免。
    分成一个独立函数是因为「要不要审」这个判断除了发起发布，页面上还要用来
    提示用户，两处口径必须一致——不一致的话，页面说不用审、点下去却卡在待审批。
    """
    mode = (getattr(p, "approval_mode", "") or "inherit") if p is not None else "inherit"
    if mode == "force":
        return True
    if mode == "exempt":
        return False
    return bool(g.approval_required)


def platform_allows_emergency_bypass(db: Session) -> bool:
    """平台总闸。缺省关闭；必须显式打开后，分组才能再开应急跳审。同一请求只读一次。"""
    cached = db.info.get("_emergency_bypass_enabled")
    if cached is not None:
        return bool(cached)
    from app.modules.settings import get_setting

    raw = (get_setting(db, "emergency_bypass_enabled") or "false").strip().lower()
    on = raw == "true"
    db.info["_emergency_bypass_enabled"] = on
    return on


def can_emergency_bypass(db: Session, g: Group | None) -> bool:
    """页面按钮和闸门共用：平台开 + 该分组开，才能跳审。"""
    return bool(g and getattr(g, "allow_emergency_bypass", False) and platform_allows_emergency_bypass(db))


def _approval_gate(
    db: Session,
    g: Group,
    p: Pipeline | None = None,
    *,
    skip_approval: bool = False,
    emergency_bypass: bool = False,
    emergency_bypass_reason: str = "",
) -> tuple[bool, bool]:
    """判断新发布该直接排队还是进入待审批，顺带校验应急跳审的前置条件。

    返回 (是否直接排队, 是否按应急跳审处理)。
    """
    required = approval_required_for(g, p)
    if emergency_bypass and not required:
        emergency_bypass = False
    if emergency_bypass:
        # 应急跳审是「本来要审，情况紧急先发后补」。流水线被强制要求审批时，
        # 能不能跳仍然由环境的开关决定——force 只提高要求，不该顺带把
        # 环境没开的应急通道给开出来
        if not platform_allows_emergency_bypass(db):
            raise BizException.bad_request(
                "平台已关闭应急跳审，请走审批（含项目经理确认）"
            )
        if not getattr(g, "allow_emergency_bypass", False):
            raise BizException.bad_request("当前分组未开启应急跳审")
        if not (emergency_bypass_reason or "").strip():
            raise BizException.bad_request("应急跳审必须填写原因")
    will_queue = skip_approval or emergency_bypass or (not required)
    return will_queue, emergency_bypass


def _open_approval(
    db: Session,
    r: Release,
    g: Group,
    *,
    operator_id: int,
    action_label: str,
    pipeline_name: str,
    detail: str,
) -> None:
    """给待审批的发布登记审批待办并通知审批人。

    发布、回滚、Rebuild 都是往同一个环境推变更，必须共用这道闸门。
    """
    from app.modules.approval.service import create_release_approvals
    from app.modules.notify import action_path, emit

    approvals = create_release_approvals(
        db,
        release_id=r.id,
        group_id=g.id,
        requester_id=operator_id,
        allow_self_approval=bool(getattr(g, "allow_self_approval", False)),
    )
    if not approvals:
        # 审批要求可能来自环境，也可能来自流水线自己设的强制审批，
        # 所以别写死「生产分组」——报出真实分组名，用户才知道去哪儿加审批人
        raise BizException.bad_request(
            f"分组「{g.name}」未找到可用审批人，无法提交{action_label}；"
            "请为负责人授予该分组的 approve 权限，或开启允许自审/应急跳审"
        )
    emit(
        db,
        "release.approval.pending",
        user_ids=[a.approver_id for a in approvals],
        exclude_user_id=operator_id,
        title=f"生产{action_label}待审批：{pipeline_name} {build_no(r)}",
        content=detail,
        link=action_path("release.approval.pending", related_id=r.id),
        related_id=r.id,
        dedupe_key=f"release.approval.pending:{r.id}",
    )


def _emit_bypass(
    db: Session,
    r: Release,
    g: Group,
    *,
    operator_id: int,
    action_label: str,
    pipeline_name: str,
    reason: str,
) -> None:
    """记录应急跳审并通知审批人。跳过了审批，就更要留痕。"""
    from app.modules.notify import emit

    _audit_release(
        db,
        "release.approval.bypass",
        r,
        f"应急跳审{action_label} #{r.id} reason={reason}",
        operator_id=operator_id,
        trigger_by=r.trigger_by,
    )
    emit(
        db,
        "release.approval.bypassed",
        group_reviewers=g.id,
        exclude_user_id=operator_id,
        title=f"生产{action_label}已应急跳审：{pipeline_name} {build_no(r)}",
        content=(
            f"发布单已按应急策略直接进入执行队列\n"
            f"流水线：{pipeline_name}\n"
            f"构建号：{build_no(r)}\n"
            f"原因：{reason}"
        ),
        link=f"/executions/{r.pipeline_id}/{r.id}",
        related_id=r.id,
        dedupe_key=f"release.approval.bypassed:{r.id}",
    )


def next_build_number(db: Session, pipeline_id: int) -> int:
    """该流水线的下一个构建号：各流水线各自从 1 起。

    并发下可能撞号，但同一条流水线本身有 ensure_pipeline_idle 互斥，
    真撞上也只是显示重复，不影响执行。
    """
    current = db.scalar(
        select(func.max(Release.build_number)).where(Release.pipeline_id == pipeline_id)
    )
    return int(current or 0) + 1


def create_release(
    db: Session,
    pipeline_id: int,
    version: str,
    strategy: str,
    trigger_by: str,
    operator_id: int,
    source_ref: str | None = None,
    parent_pipeline_id: int | None = None,
    parent_release_id: int | None = None,
    run_params: dict | None = None,
    skip_approval: bool = False,
    emergency_bypass: bool = False,
    emergency_bypass_reason: str = "",
    plan_json: str = "",
    brief: dict | None = None,
) -> Release:
    p = get_pipeline(db, pipeline_id)
    if p.status != PIPELINE_ACTIVE:
        raise BizException.bad_request(
            f"流水线「{p.name}」当前状态为 {p.status}，不能发起发布"
        )
    g = db.get(Group, p.group_id)
    if g is None:
        raise BizException.not_found("分组")

    will_queue, emergency_bypass = _approval_gate(
        db,
        g,
        p,
        skip_approval=skip_approval,
        emergency_bypass=emergency_bypass,
        emergency_bypass_reason=emergency_bypass_reason,
    )

    params_json = json.dumps(run_params or {}, ensure_ascii=False)
    r = Release(
        pipeline_id=pipeline_id,
        group_id=g.id,
        build_number=next_build_number(db, pipeline_id),
        version=version,
        source_ref=source_ref,
        strategy=strategy or "rolling",
        trigger_by=trigger_by or "manual",
        operator_id=operator_id,
        parent_pipeline_id=parent_pipeline_id,
        parent_release_id=parent_release_id,
        run_params_json=params_json,
        outputs_json="{}",
        # 步骤现场生成、YAML 里没有的执行（如把文件下发到指定的几台节点）走这里
        plan_json=plan_json or "",
        # 生产分组 → 待审批；测试分组 / 子流水线（父流水线已在跑）→ 直接排队
        status=RELEASE_QUEUED if will_queue else RELEASE_PENDING,
        snapshot=json.dumps({"version": version, "source_ref": source_ref}, ensure_ascii=False),
    )
    from app.modules.pm.service import stamp_release_brief

    stamp_release_brief(r, brief)
    db.add(r)
    db.flush()
    _audit_release(
        db,
        "release.create",
        r,
        f"发起发布 流水线#{pipeline_id} {p.name} version={version or ''} status={r.status}",
        operator_id=operator_id,
        trigger_by=r.trigger_by,
    )

    if not will_queue:
        _open_approval(
            db,
            r,
            g,
            operator_id=operator_id,
            action_label="发布",
            pipeline_name=p.name,
            detail=(
                f"项目发布申请待处理\n"
                f"流水线：{p.name}\n"
                f"构建号：{build_no(r)}\n"
                f"版本：{version or '-'}\n"
                f"代码版本：{source_ref or '-'}"
            ),
        )
    elif emergency_bypass:
        _emit_bypass(
            db,
            r,
            g,
            operator_id=operator_id,
            action_label="发布",
            pipeline_name=p.name,
            reason=emergency_bypass_reason.strip(),
        )
    # 项目经理确认是可选的第二道门，默认关。打开且已指定经理时，即便技术侧能直接排队也先等确认。
    from app.modules.pm.service import apply_pm_gate_on_create
    from app.modules.project.models import Project

    proj = db.get(Project, p.project_id)
    if apply_pm_gate_on_create(
        db,
        r,
        project=proj,
        group=g,
        operator_id=operator_id,
        action_label="发布",
        pipeline_name=p.name,
        emergency=emergency_bypass,
        skip_approval=skip_approval,
    ):
        r.status = RELEASE_PENDING
    elif will_queue:
        # 真要进队列才互斥。挂着项目经理确认时只是 pending，不该因为线上正在跑就提单失败
        ensure_pipeline_idle(db, pipeline_id, exclude_release_id=r.id)
        r.status = RELEASE_QUEUED
    db.commit()
    db.refresh(r)
    return r


def approve_release(
    db: Session,
    release_id: int,
    approved: bool,
    comment: str,
    *,
    reviewer_id: int | None = None,
) -> Release:
    from datetime import datetime

    from app.modules.approval.service import cancel_pending_approvals, pending_approval_for_reviewer
    from app.modules.notify import emit

    r = get_release(db, release_id)
    if r.status != RELEASE_PENDING:
        raise BizException.bad_request(f"当前状态 {r.status} 不允许审批")
    note = (comment or "").strip()
    if not approved and not note:
        raise BizException.bad_request("驳回时必须填写原因")

    approval_row = None
    if reviewer_id is not None:
        from app.modules.auth.models import User

        reviewer = db.get(User, reviewer_id)
        approval_row = pending_approval_for_reviewer(
            db,
            release_id=release_id,
            reviewer_id=reviewer_id,
            is_admin=bool(reviewer is not None and reviewer.is_admin),
        )
        if approval_row is None:
            raise BizException.bad_request("当前用户不在待审批名单中，或该审批已被处理")
        # 谁拍板谁落在审批人上，列表和审计对得上
        approval_row.approver_id = reviewer_id

    if approved:
        # 审批通过将进入排队/执行 → 同流水线互斥
        ensure_pipeline_idle(db, r.pipeline_id, exclude_release_id=r.id)
        if approval_row is not None:
            approval_row.status = "approved"
            approval_row.comment = note
            approval_row.approved_at = datetime.now()
        cancel_pending_approvals(
            db,
            release_id=release_id,
            comment="已由其他审批人处理",
            exclude_approval_id=approval_row.id if approval_row is not None else None,
        )
        from app.modules.pm.service import pm_still_blocking

        # 没开项目经理闸时这里永远是 False，行为和改之前一样直接排队
        r.status = RELEASE_PENDING if pm_still_blocking(db, r.id) else RELEASE_QUEUED
    else:
        r.status = RELEASE_REJECTED
        if approval_row is not None:
            approval_row.status = "rejected"
            approval_row.comment = note
            approval_row.approved_at = datetime.now()
        cancel_pending_approvals(
            db,
            release_id=release_id,
            comment="该发布已被驳回",
            exclude_approval_id=approval_row.id if approval_row is not None else None,
        )
        from app.modules.pm.service import cancel_pending_pm

        cancel_pending_pm(db, release_id, comment="该发布已被技术审批驳回")
    _audit_release(
        db,
        "release.approve" if approved else "release.reject",
        r,
        f"{'通过' if approved else '驳回'}发布 #{r.id} comment={note}",
        operator_id=reviewer_id,
        trigger_by=r.trigger_by,
    )
    emit(
        db,
        "release.approval.reviewed",
        user_ids=[r.operator_id] if r.operator_id else [],
        title=f"生产发布已{'通过' if approved else '驳回'}：{build_no(r)}",
        content=(
            f"流水线发布审批结果已更新\n"
            f"构建号：{build_no(r)}\n"
            f"结果：{'通过' if approved else '驳回'}\n"
            f"意见：{note or '-'}"
        ),
        link=f"/executions/{r.pipeline_id}/{r.id}",
        related_id=r.id,
        dedupe_key=f"release.approval.reviewed:{r.id}:{'approved' if approved else 'rejected'}",
    )
    db.commit()
    db.refresh(r)
    return r


def rollback_release(
    db: Session,
    release_id: int,
    operator_id: int,
    *,
    emergency_bypass: bool = False,
    emergency_bypass_reason: str = "",
    image_tags: dict[int, str] | None = None,
) -> Release:
    """一键回滚：撤销这次发布，把目标环境还原到发布前。

    不重新拉代码、不重新构建——老代码未必还构建得出来（依赖源、SDK 版本都在变），
    故障时也等不起。真正执行的是上次部署留下的逆操作：增量发布还原备份文件、
    容器发布换回上一个镜像、K8s 回退到上一个 revision。

    回滚同样是往生产推变更，需要审批的分组必须先过审；
    线上故障场景可走分组的应急跳审通道，跳审会留审计并通知审批人。
    image_tags 只换容器镜像的 tag，仓库路径仍用这次发布登记的那条。
    """
    from app.modules.deployment import service as deployment_service

    original = get_release(db, release_id)
    p = get_pipeline(db, original.pipeline_id)
    g = db.get(Group, original.group_id)
    if g is None:
        raise BizException.not_found("分组")

    # 先锁流水线，再查「有没有在飞的回滚」。两个人同时点，不能各开一张单
    # 对着同一份备份还原——节点上两次 rollback-files 会互相踩。
    ensure_pipeline_idle(db, original.pipeline_id)
    busy_rb = inflight_rollback_of(db, original.id)
    if busy_rb is not None:
        raise BizException.bad_request(
            f"发布 {build_no(original)} 已有回滚单 #{busy_rb.id}（{busy_rb.status}）在处理中，"
            "不能对同一份备份再开一张回滚单"
        )

    rows, warnings, reason = deployment_service.check_undoable(db, release_id)
    if not rows:
        raise BizException.bad_request(f"发布 {build_no(original)} 无法回滚：{reason}")
    tags = image_tags or {}
    plan_jobs = deployment_service.build_undo_jobs(db, rows, image_tags=tags)
    if not plan_jobs:
        raise BizException.bad_request(
            "这次发布的部署记录无法生成完整回滚步骤，已中止，避免只撤一部分"
        )
    plan_summary = deployment_service.describe(rows)
    if tags:
        chosen = "、".join(f"{rid}:{tag}" for rid, tag in sorted(tags.items()))
        plan_summary = f"{plan_summary}；指定镜像 tag {chosen}"

    will_queue, emergency_bypass = _approval_gate(
        db,
        g,
        p,
        emergency_bypass=emergency_bypass,
        emergency_bypass_reason=emergency_bypass_reason,
    )

    rb = Release(
        pipeline_id=original.pipeline_id,
        group_id=original.group_id,
        build_number=next_build_number(db, original.pipeline_id),
        artifact_id=original.artifact_id,
        version=f"{original.version or build_no(original)}-rollback",
        source_ref=original.source_ref,
        strategy="rolling",
        trigger_by="rollback",
        operator_id=operator_id,
        rollback_of_release_id=original.id,
        plan_json=json.dumps(
            {"jobs": plan_jobs, "summary": plan_summary, "warnings": warnings},
            ensure_ascii=False,
        ),
        status=RELEASE_QUEUED if will_queue else RELEASE_PENDING,
        snapshot=original.snapshot,
    )
    db.add(rb)
    db.flush()
    _audit_release(
        db,
        "release.rollback",
        rb,
        f"回滚发布 #{release_id} → 新发布 #{rb.id} status={rb.status}：{plan_summary}",
        operator_id=operator_id,
        trigger_by="rollback",
    )
    if not will_queue:
        _open_approval(
            db,
            rb,
            g,
            operator_id=operator_id,
            action_label="回滚",
            pipeline_name=p.name,
            detail=(
                f"生产回滚申请待处理\n"
                f"流水线：{p.name}\n"
                f"构建号：{build_no(rb)}\n"
                f"撤销：构建 {build_no(original)}（{original.version or '-'}）\n"
                f"回滚内容：{plan_summary}"
                + ("\n注意：" + "；".join(warnings) if warnings else "")
            ),
        )
    elif emergency_bypass:
        _emit_bypass(
            db,
            rb,
            g,
            operator_id=operator_id,
            action_label="回滚",
            pipeline_name=p.name,
            reason=emergency_bypass_reason.strip(),
        )
    from app.modules.pm.service import apply_pm_gate_on_create, stamp_release_brief
    from app.modules.project.models import Project

    stamp_release_brief(
        rb,
        {
            "business_summary": getattr(original, "business_summary", "") or f"回滚 {build_no(original)}",
            "impact_scope": getattr(original, "impact_scope", "") or "",
            "iteration_tag": getattr(original, "iteration_tag", "") or "",
            "audience": getattr(original, "audience", "") or "",
        },
    )
    proj = db.get(Project, p.project_id)
    if apply_pm_gate_on_create(
        db,
        rb,
        project=proj,
        group=g,
        operator_id=operator_id,
        action_label="回滚",
        pipeline_name=p.name,
        emergency=emergency_bypass,
        skip_approval=False,
    ):
        rb.status = RELEASE_PENDING
    elif will_queue:
        ensure_pipeline_idle(db, original.pipeline_id, exclude_release_id=rb.id)
        rb.status = RELEASE_QUEUED
    db.commit()
    db.refresh(rb)
    # 免审批分组和应急跳审都是 queued，这里不拉起来就会一直停在「排队中」，
    # 构建任务永远不生成——页面上看着像回滚成功了，实际什么都没跑
    if rb.status == RELEASE_QUEUED:
        execute_release(db, rb.id)
        db.refresh(rb)
    return rb


def _rebuild_run_params(pipeline: Pipeline, src: Release, deploy_manifest: str | None) -> str:
    """Rebuild 沿用原 run_params；弹窗带了发布清单则按执行弹窗同一套规则合进去。

    没带清单时：上次点名过文件就原样沿用；上次是空、占位或 `**`（含曾经自动注入的全量）
    一律视为没有清单，交给 apply_execute_manifest 拒绝，避免静默再打全量。
    """
    from app.modules.deploy.service import (
        VAR_MANIFEST,
        apply_execute_manifest,
        is_manifest_placeholder,
        is_unbounded_manifest,
    )

    try:
        params = json.loads(src.run_params_json or "{}")
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    if deploy_manifest is None:
        inherited = str(params.get(VAR_MANIFEST) or "").strip()
        if inherited and not is_unbounded_manifest(inherited) and not is_manifest_placeholder(inherited):
            return json.dumps(params, ensure_ascii=False)
        deploy_manifest = ""
    merged = apply_execute_manifest(pipeline, dict(params), deploy_manifest)
    return json.dumps(merged, ensure_ascii=False)


def rebuild_release(
    db: Session,
    source_id: int,
    operator_id: int,
    *,
    emergency_bypass: bool = False,
    emergency_bypass_reason: str = "",
    deploy_manifest: str | None = None,
) -> Release:
    """Rebuild（蓝盾风格）：用原 release 的 source_ref（commit/branch/tag）重新拉取构建。

    关键：复用原 source_ref，让构建机用同一次提交的代码重新打包——
    不是用最新代码，是**那一次发布时的代码**。
    有提取增量包步骤时，弹窗可改发布清单，合进新 release 的 run_params。
    """
    src = get_release(db, source_id)
    p = get_pipeline(db, src.pipeline_id)
    g = db.get(Group, src.group_id)
    if g is None:
        raise BizException.not_found("分组")

    # Rebuild 是把同一份代码再推一次生产，风险等同发布，同样要过审
    will_queue, emergency_bypass = _approval_gate(
        db,
        g,
        p,
        emergency_bypass=emergency_bypass,
        emergency_bypass_reason=emergency_bypass_reason,
    )

    new = Release(
        pipeline_id=src.pipeline_id,
        group_id=src.group_id,
        build_number=next_build_number(db, src.pipeline_id),
        artifact_id=src.artifact_id,
        version=f"rebuild-of-{src.id}",
        source_ref=src.source_ref,  # 复用原 commit/branch/tag
        strategy=src.strategy,
        trigger_by="rebuild",
        operator_id=operator_id,
        run_params_json=_rebuild_run_params(p, src, deploy_manifest),
        status=RELEASE_QUEUED if will_queue else RELEASE_PENDING,
        snapshot=src.snapshot,
    )
    db.add(new)
    db.flush()
    _audit_release(
        db,
        "release.rebuild",
        new,
        f"Rebuild 发布 #{source_id} → 新发布 #{new.id} status={new.status}",
        operator_id=operator_id,
        trigger_by="rebuild",
    )
    if not will_queue:
        _open_approval(
            db,
            new,
            g,
            operator_id=operator_id,
            action_label="Rebuild",
            pipeline_name=p.name,
            detail=(
                f"生产 Rebuild 申请待处理\n"
                f"流水线：{p.name}\n"
                f"构建号：{build_no(new)}\n"
                f"重建自：构建 {build_no(src)}\n"
                f"代码版本：{src.source_ref or '-'}"
            ),
        )
    elif emergency_bypass:
        _emit_bypass(
            db,
            new,
            g,
            operator_id=operator_id,
            action_label="Rebuild",
            pipeline_name=p.name,
            reason=emergency_bypass_reason.strip(),
        )
    from app.modules.pm.service import apply_pm_gate_on_create, stamp_release_brief
    from app.modules.project.models import Project

    stamp_release_brief(
        new,
        {
            "business_summary": getattr(src, "business_summary", "") or "",
            "impact_scope": getattr(src, "impact_scope", "") or "",
            "iteration_tag": getattr(src, "iteration_tag", "") or "",
            "planned_window": getattr(src, "planned_window", "") or "",
            "audience": getattr(src, "audience", "") or "",
            "need_user_notice": bool(getattr(src, "need_user_notice", False)),
        },
    )
    proj = db.get(Project, p.project_id)
    if apply_pm_gate_on_create(
        db,
        new,
        project=proj,
        group=g,
        operator_id=operator_id,
        action_label="Rebuild",
        pipeline_name=p.name,
        emergency=emergency_bypass,
        skip_approval=False,
    ):
        new.status = RELEASE_PENDING
    elif will_queue:
        ensure_pipeline_idle(db, src.pipeline_id, exclude_release_id=new.id)
        new.status = RELEASE_QUEUED
    db.commit()
    db.refresh(new)
    if new.status == RELEASE_QUEUED:
        execute_release(db, new.id)
        db.refresh(new)
    return new


def statistics(db: Session) -> dict:
    total = db.query(Release).count()
    success = db.query(Release).filter(Release.status == RELEASE_SUCCESS).count()
    failed = db.query(Release).filter(Release.status == RELEASE_FAILED).count()
    rejected = db.query(Release).filter(Release.status == RELEASE_REJECTED).count()
    running = db.query(Release).filter(Release.status == RELEASE_RUNNING).count()
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "rejected": rejected,
        "running": running,
        "success_rate": round(success / total * 100, 2) if total else 0,
    }


# ============================================================
# 流水线执行（生产版：生成构建任务，Agent 拉模式执行）
# ============================================================
def execute_release(db: Session, release_id: int) -> Release:
    """执行一次发布：把流水线拆成构建任务（按 Job 粒度），交给 Agent 拉取执行。

    生产架构：后端只负责拆任务 + 分发，真正的命令执行由构建机 Agent 完成。
    返回生成的任务列表，Agent 通过 GET /agents/{id}/tasks 拉取。
    同一流水线同时只允许一次执行；不同流水线可并发。
    """
    from datetime import datetime

    from app.modules.agent.task_service import create_tasks_for_release
    from app.modules.pipeline.schemas import parse_yaml

    release = get_release(db, release_id)
    # 排除自身：创建时可能已是 queued
    ensure_pipeline_idle(db, release.pipeline_id, exclude_release_id=release.id)

    if release.status in (RELEASE_SUCCESS, RELEASE_FAILED, RELEASE_REJECTED, RELEASE_CANCELLED, RELEASE_ROLLED_BACK):
        raise BizException.bad_request(f"当前状态 {release.status} 不可再次执行")
    if release.status == RELEASE_RUNNING:
        raise BizException.bad_request(f"发布 #{release_id} 已在执行中")
    if release.status == RELEASE_PENDING:
        raise BizException.bad_request(f"发布 #{release_id} 待审批，通过审批后才能执行")

    # 从这里往下的任何失败都必须就地收尾成 failed。
    # 走到这一步时 release 已经是 queued 或 running，两者都算「占着这条流水线」
    # （见 PIPELINE_BUSY_STATUSES），而且没有任何后台任务会回来接手它们——
    # 每个创建 queued 发布的地方都是紧接着同步调 execute_release 的。
    # 所以异常一旦逃出去，这条发布就永远卡着：页面上一直转圈，Agent 没任务可领，
    # 后面的发布还全被它挡住。原因也要落到发布日志里——弹窗关掉就没了，日志还在。
    try:
        pipeline = get_pipeline(db, release.pipeline_id)

        # 带专属计划的执行（回滚）不看 YAML：撤销一次发布跑的是逆操作，
        # 流水线定义里没有这些步骤，也不该为了回滚往用户的编排里塞东西
        if getattr(release, "rollback_of_release_id", None):
            from app.modules.deployment import service as deployment_service

            rows, _, reason = deployment_service.check_undoable(
                db, release.rollback_of_release_id
            )
            if not rows:
                raise BizException.bad_request(f"无法执行回滚：{reason}")
            if not deployment_service.build_undo_jobs(db, rows):
                raise BizException.bad_request(
                    "无法执行回滚：部署记录已不完整，避免只撤一部分"
                )
        plan_stages = _plan_stages(release)
        if plan_stages is not None:
            stages, variables = plan_stages, []
        else:
            definition = parse_yaml(pipeline.yaml)
            stages, variables = definition.pipeline.stages, definition.pipeline.variables

        release.status = RELEASE_RUNNING
        release.started_at = datetime.now()
        db.commit()

        create_tasks_for_release(db, release_id, pipeline.id, stages, variables=variables)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        release = get_release(db, release_id)
        release.status = RELEASE_FAILED
        release.finished_at = datetime.now()
        set_release_error(release, f"发布未能启动：{e}")
        db.commit()
        raise

    db.refresh(release)
    return release


def try_execute_release(db: Session, release_id: int) -> tuple[Release, str]:
    """拉起执行；失败不抛异常，返回 (最新的 release, 给人看的失败原因)。

    专给「前一步已经落库」的接口用：审批已经通过了、提交单已经发出去了、
    发布记录已经建好了——这些都不该因为「拉起执行」这后一步失败，就被报成
    前一步也没做成。真那样的话前端会以为没成功，让用户反复点，而重复的那几下
    只会撞上「当前状态 xx 不允许操作」，人就懵在那了。
    execute_release 内部已经把发布收尾成 failed，所以这里只需要如实转述原因。
    """
    try:
        return execute_release(db, release_id), ""
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return get_release(db, release_id), str(e)


def _db_now(db: Session):
    """取数据库自己的当前时间，用来跟库填的 created_at 比。"""
    from datetime import datetime

    val = db.scalar(select(func.now()))
    if isinstance(val, str):  # SQLite 返回字符串
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return datetime.now()
    return val or datetime.now()


def rescue_stranded_releases(db: Session, min_age_seconds: int = 600) -> int:
    """把「排队中/执行中却一个构建任务都没有」的发布收尾成失败，返回收掉的条数。

    这种发布是启动执行时抛异常留下的。queued 和 running 都算占着流水线
    （见 PIPELINE_BUSY_STATUSES），而且没有任何后台任务会回来接手它们——
    创建 queued 发布的地方都是紧接着同步拉起执行的。所以它会一直转圈：
    Agent 没任务可领，也永远等不到结束，那条流水线还被它挡着。

    min_age_seconds 是保护期：execute_release 是同步的，正常几百毫秒就把任务建出来了。
    留一段时间再动手，才不会把「刚建好、正要拆任务」的发布误杀。
    """
    from datetime import datetime

    from app.modules.agent.models import BuildTask

    candidates = (
        db.query(Release)
        .filter(Release.status.in_((RELEASE_QUEUED, RELEASE_RUNNING)))
        .filter(~db.query(BuildTask.id).filter(BuildTask.release_id == Release.id).exists())
        .limit(50)
        .all()
    )
    # started_at 是代码里用 datetime.now() 写的（本地时区），created_at 是库自己
    # 用 NOW() 填的（SQLite 下是 UTC）。两个钟不能混着比——拿本地时间去比 created_at，
    # 刚建好的发布会凭空老 8 小时，保护期直接失效、正常发布被误杀。
    # 各自跟各自的钟比：started_at 比本地时间，created_at 比库的时间。
    now_local = datetime.now()
    now_db = _db_now(db)

    stuck = []
    for r in candidates:
        if r.started_at is not None:
            age = (now_local - r.started_at).total_seconds()
        elif r.created_at is not None:
            age = (now_db - r.created_at).total_seconds()
        else:
            age = min_age_seconds
        if age >= min_age_seconds:
            stuck.append(r)

    for r in stuck:
        r.status = RELEASE_FAILED
        r.finished_at = r.finished_at or datetime.now()
        set_release_error(r, "未能生成构建任务，发布中断（后台巡检清理）")
    if stuck:
        db.commit()
    return len(stuck)


def _plan_step_with(step: dict) -> dict:
    """计划步骤的参数。试跑草稿的 package 挂在 with 里，建任务时再拎出来。"""
    body = dict(step.get("with") or {})
    pkg = step.get("package")
    if isinstance(pkg, dict) and pkg.get("download_path"):
        body["_trial_package"] = pkg
    return body


def _plan_stages(release: Release) -> list | None:
    """把 release.plan_json 里的 Job 计划还原成 Stage，没有计划返回 None。"""
    from app.modules.pipeline.schemas import JobSpec, StageSpec, StepSpec

    jobs_raw = plan_jobs_of(release)
    if not jobs_raw:
        return None

    # 计划不止回滚在用了，阶段名跟着计划走，别让下发文件在执行页上显示成「回滚」
    meta = plan_meta_of(release)
    stage_name = str(meta.get("name") or "") or "回滚"
    default_job_name = str(meta.get("job_name") or "") or "撤销上次发布"

    jobs = []
    for i, job in enumerate(jobs_raw):
        steps = [
            StepSpec(
                name=s.get("name") or "",
                plugin=s.get("plugin", ""),
                **{"with": _plan_step_with(s)},
            )
            for s in (job.get("steps") or [])
            if isinstance(s, dict) and s.get("plugin")
        ]
        if not steps:
            continue
        jobs.append(JobSpec(
            id=job.get("id") or f"undo-{i}",
            name=job.get("name") or default_job_name,
            agent=job.get("agent") or "any",
            steps=steps,
        ))
    return [StageSpec(name=stage_name, jobs=jobs)] if jobs else None


def plan_meta_of(release: Release) -> dict:
    """读计划里除 jobs 之外的元信息（阶段名、摘要、告警）。"""
    if not (getattr(release, "plan_json", "") or "").strip():
        return {}
    try:
        plan = json.loads(release.plan_json)
    except (TypeError, ValueError):
        return {}
    return plan if isinstance(plan, dict) else {}


def plan_jobs_of(release: Release) -> list[dict]:
    """读出本次执行的专属计划（回滚），没有则空列表。"""
    if not (getattr(release, "plan_json", "") or "").strip():
        return []
    try:
        plan = json.loads(release.plan_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(plan, dict):
        return []
    jobs = plan.get("jobs")
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def execute_release_local(db: Session, release_id: int) -> Release:
    """本地同步执行（无 Agent 时的兜底/演示）。

    执行语义（蓝盾模型）：
      - Stage 串行：一个 Stage 失败，后续 Stage 不执行
      - Stage 内 Job 并行：相互独立，用线程池并发执行
      - Job 内 Step 串行：一个失败则 Job 失败
    """
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime
    import json as _json

    from app.modules.pipeline.executor import execute_step
    from app.modules.pipeline.schemas import parse_yaml

    release = get_release(db, release_id)
    ensure_pipeline_idle(db, release.pipeline_id, exclude_release_id=release.id)
    pipeline = get_pipeline(db, release.pipeline_id)

    definition = parse_yaml(pipeline.yaml)

    release.status = RELEASE_RUNNING
    release.started_at = datetime.now()
    db.commit()

    step_statuses: list[dict] = []
    overall_ok = True

    def run_job(job) -> tuple[bool, list[dict]]:
        """执行单个 Job（Step 串行）。stdout 不落库、不攒内存。"""
        j_statuses: list[dict] = []
        j_ok = True
        for step in job.steps:
            ok, _logs = execute_step(step.plugin, step.with_ or {})
            j_statuses.append({
                "plugin": step.plugin,
                "ok": ok,
                "stage": "",
                "job": job.name or job.agent,
            })
            if not ok:
                j_ok = False
                break
        return j_ok, j_statuses

    for stage in definition.pipeline.stages:
        jobs = stage.jobs or []
        if not jobs:
            continue

        stage_ok = True
        if len(jobs) == 1:
            results = [run_job(jobs[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                results = list(pool.map(run_job, jobs))

        for j_ok, j_statuses in results:
            for s in j_statuses:
                s["stage"] = stage.name
                step_statuses.append(s)
            if not j_ok:
                stage_ok = False

        if not stage_ok:
            overall_ok = False
            break

    release.status = RELEASE_SUCCESS if overall_ok else RELEASE_FAILED
    release.finished_at = datetime.now()
    release.step_status = _json.dumps(step_statuses, ensure_ascii=False)
    db.commit()
    db.refresh(release)
    from app.modules.notify import on_release_finished

    on_release_finished(release.id)
    return release


def get_release_logs(db: Session, release_id: int) -> dict:
    """获取发布日志（只读 Redis / ES，不回落 MySQL 日志列）。"""
    release = get_release(db, release_id)
    import json as _json

    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.modules.agent.log_store import create_log_store
    from app.modules.agent.models import BuildTask
    from app.modules.settings import get_all_settings

    tasks = db.scalars(
        select(BuildTask).where(BuildTask.release_id == release_id).order_by(BuildTask.id)
    ).all()

    store = create_log_store(SessionLocal, lambda: get_all_settings(db))

    from app.modules.agent.log_store import MAX_FETCH_LINES

    budget = MAX_FETCH_LINES
    logs_lines: list[str] = []
    step_statuses: list[dict] = []
    for t in tasks:
        logs_lines.append(f"════════ ⏵ {t.stage_name} / {t.job_name} ({t.status}) ════════")
        if budget > 0:
            try:
                task_lines = store.get(t.id, limit=budget)
            except Exception:  # noqa: BLE001
                task_lines = []
            if task_lines:
                logs_lines.extend(task_lines)
                budget -= len(task_lines)
            if budget <= 0:
                logs_lines.append("…（日志过长已截断，请按步骤查看单个任务的完整日志）")
        else:
            logs_lines.append("…（日志过长已截断，请按步骤查看单个任务的完整日志）")
        for s in json.loads(t.steps_json or "[]"):
            step_statuses.append({
                "plugin": s.get("plugin"),
                "ok": t.status == "success",
                "stage": t.stage_name,
                "job": t.job_name,
            })

    body = "\n".join(logs_lines).strip()
    # 还没拆出构建任务时，日志面板只展示短失败原因（error_message），不读 MySQL 日志列
    if not tasks:
        body = release_error_summary(release, max_len=2000, db=db)

    return {
        "release_id": release.id,
        "status": release.status,
        "logs": body,
        "steps": step_statuses or _json.loads(release.step_status or "[]"),
        "tasks": [
            {"id": t.id, "stage": t.stage_name, "job": t.job_name, "status": t.status}
            for t in tasks
        ],
    }
