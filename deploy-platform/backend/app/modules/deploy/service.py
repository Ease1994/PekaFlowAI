"""发布提交单的业务逻辑。

单子只做「开发报清单 → 发布人员核对 → 触发流水线」这一件事，
触发时把清单和更新日志当执行参数交给流水线，不碰发布流程本身。

手动点「执行」也可以带一份清单：有「提取增量发布包」步骤才认。
填了就覆盖本次；留空则沿用步骤里已经写死的文件列表。
步骤仍是 ${{DEPLOY_MANIFEST}} 占位且执行时也没填：直接拒绝，
不会按编译产物全量打包——空范围不能上环境。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.deploy.models import DeployRequest

# 清单和更新日志以变量形式进入流水线，插件参数里写 ${{DEPLOY_MANIFEST}} 即可取用
VAR_MANIFEST = "DEPLOY_MANIFEST"
VAR_CHANGELOG = "DEPLOY_CHANGELOG"
VAR_REQUEST_ID = "DEPLOY_REQUEST_ID"
# 编译产物下全部文件。只允许步骤或本次执行显式写出，禁止把空清单翻译成它
ALL_FILES_MANIFEST = "**"
# 整行只引用发布提交变量，没有写任何真实路径
_MANIFEST_PLACEHOLDER = re.compile(r"^\$\{\{\s*DEPLOY_MANIFEST\s*\}\}$")
# 占位未填时的拒绝说明：空清单不能上环境
MISSING_MANIFEST_MSG = (
    "这条流水线按发布清单打增量包，但没有指定要发布哪些文件。"
    "空清单不会按编译产物全量打包。请填写发布清单，"
    "或把文件列表写死在「提取增量发布包」步骤里。"
)

OPEN_STATUSES = ("draft", "submitted")
# 发失败了通常是改完重发，不该逼着人另开一单
RELEASABLE_STATUSES = OPEN_STATUSES + ("release_failed",)
# 正在发或已经发成功的单子不能再改，否则单子内容和实际发出去的对不上
LOCKED_STATUSES = ("releasing", "released")

# 真正会消费发布清单的插件：清单就是给它挑文件打增量包用的
MANIFEST_PLUGINS = ("pack-incremental",)


def consumes_deploy_manifest(pipeline) -> bool:
    """这条流水线是不是「按清单增量发布」的（Windows/IIS 那一类）。

    发布提交单的价值全在清单上：清单经 DEPLOY_MANIFEST 交给 pack-incremental
    挑文件打增量包。选一条不读清单的流水线（Docker/K8s 整包发布那种），清单会
    照样写进 run_params 然后无人问津——静默失效比报错更难查，所以要能筛出来。
    """
    yaml_text = getattr(pipeline, "yaml", "") or ""
    if not yaml_text:
        return False
    try:
        from app.modules.pipeline.schemas import parse_yaml

        definition = parse_yaml(yaml_text)
        for stage in definition.pipeline.stages:
            for job in stage.jobs:
                for step in job.steps:
                    if step.plugin in MANIFEST_PLUGINS:
                        return True
                    # 也认「参数里直接引用了 DEPLOY_MANIFEST」的自定义编排
                    for value in (step.with_ or {}).values():
                        if VAR_MANIFEST in str(value):
                            return True
        return False
    except Exception:  # noqa: BLE001
        # YAML 有问题时不该把整个下拉搞空，退化成文本判断
        return any(p in yaml_text for p in MANIFEST_PLUGINS) or VAR_MANIFEST in yaml_text


def pack_incremental_manifest_of(pipeline) -> str | None:
    """第一条「提取增量发布包」步骤里配置的清单。

    没有该插件返回 None，执行弹窗就不应再出现发布清单，后端也忽略该字段。
    有插件时返回步骤 `manifest` 原文（可能是写死的文件列表，也可能仍是
    `${{DEPLOY_MANIFEST}}` 占位）。YAML 坏了但文本里能看到插件名时返回空串，
    让前端仍弹出清单框，避免静默丢掉这次填写。
    """
    yaml_text = getattr(pipeline, "yaml", "") or ""
    if not yaml_text:
        return None
    try:
        from app.modules.pipeline.schemas import parse_yaml

        definition = parse_yaml(yaml_text)
        for stage in definition.pipeline.stages:
            for job in stage.jobs:
                for step in job.steps:
                    if step.plugin in MANIFEST_PLUGINS:
                        return str((step.with_ or {}).get("manifest") or "")
        return None
    except Exception:  # noqa: BLE001
        if not any(p in yaml_text for p in MANIFEST_PLUGINS):
            return None
        return ""


def is_manifest_placeholder(text: str) -> bool:
    """清单是否没有写死任何文件（空，或每一行都只引用 DEPLOY_MANIFEST）。"""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _MANIFEST_PLACEHOLDER.match(line):
            return False
        # 这一行是占位，继续看有没有真实路径
    # 去掉注释后要么什么都没有，要么全是占位，都算「步骤里没有写死清单」
    return True


def is_unbounded_manifest(text: str) -> bool:
    """清单是否只有「全部文件」通配，没有点名任何路径。

    `**` 本身可以写在步骤里表示这条线就是整包发；不能把「没填」翻译成它。
    Rebuild 沿用上次参数时，上次若是自动注入的 `**`，也按没有清单处理。
    """
    rules: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            return False
        rules.append(line.replace("\\", "/").lstrip("/"))
    return rules == [ALL_FILES_MANIFEST] or rules == ["**/*"]


def apply_execute_manifest(pipeline, run_params: dict, user_text: str | None) -> dict:
    """把执行弹窗里的发布清单合进 run_params。

    没有提取增量发布包步骤：原样返回，弹窗即使误传了清单也丢掉。
    填了：写入 DEPLOY_MANIFEST，覆盖步骤里的 `${{DEPLOY_MANIFEST}}`。
    空：步骤里已经写死文件列表时不写这个变量，否则空串会把写死的内容替换掉；
    步骤仍是占位符时拒绝执行，不会写入 `**`。
    """
    configured = pack_incremental_manifest_of(pipeline)
    if configured is None:
        return run_params
    text = (user_text or "").strip()
    if text:
        run_params[VAR_MANIFEST] = text
        return run_params
    if is_manifest_placeholder(configured):
        raise BizException.bad_request(MISSING_MANIFEST_MSG)
    return run_params


def list_requests(
    db: Session,
    *,
    project_id: int | None = None,
    status: str = "",
    creator_id: int | None = None,
    allowed_project_ids: set[int] | None = None,
    limit: int | None = None,
) -> list[DeployRequest]:
    stmt = select(DeployRequest).order_by(DeployRequest.id.desc())
    if project_id:
        stmt = stmt.where(DeployRequest.project_id == project_id)
    if status:
        stmt = stmt.where(DeployRequest.status == status)
    if creator_id:
        stmt = stmt.where(DeployRequest.created_by == creator_id)
    if allowed_project_ids is not None:
        if not allowed_project_ids:
            return []
        stmt = stmt.where(DeployRequest.project_id.in_(allowed_project_ids))
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def get_request(db: Session, request_id: int) -> DeployRequest:
    req = db.get(DeployRequest, request_id)
    if req is None:
        raise BizException.not_found("发布提交单")
    return req


def _pipeline_of(db: Session, pipeline_id: int | None):
    """按 id 取流水线。没填 id 返回 None；填了却找不到则拒绝，避免计划挂到空线上。"""
    if not pipeline_id:
        return None
    from app.modules.pipeline.models import Pipeline

    p = db.get(Pipeline, int(pipeline_id))
    if p is None:
        raise BizException.bad_request(f"流水线 #{pipeline_id} 不存在")
    return p


def manifest_required_for(pipeline) -> bool:
    """只有按清单增量发布的流水线（Windows/IIS pack-incremental）才强制填清单。

    前端静态、容器、K8s 整包那类不读 DEPLOY_MANIFEST，空着即可。
    """
    return bool(pipeline) and consumes_deploy_manifest(pipeline)


def _validate(title: str, manifest: str, *, require_manifest: bool) -> None:
    """标题必填；增量线还要清单。其它类型空清单不能当成错误。"""
    if not (title or "").strip():
        raise BizException.bad_request("请填写发布标题")
    if require_manifest and not (manifest or "").strip():
        raise BizException.bad_request("Windows 增量发布必须填写发布清单（要发布哪些文件）")


def create_request(
    db: Session,
    *,
    project_id: int,
    pipeline_id: int | None,
    title: str,
    repo: str,
    source_ref: str,
    changelog: str,
    manifest: str,
    status: str,
    operator_id: int,
    extra: dict | None = None,
) -> DeployRequest:
    status = status if status in OPEN_STATUSES else "submitted"
    pipe = _pipeline_of(db, pipeline_id)
    if status == "submitted":
        _validate(title, manifest, require_manifest=manifest_required_for(pipe))
    req = DeployRequest(
        project_id=project_id,
        pipeline_id=pipeline_id,
        title=(title or "").strip(),
        repo=(repo or "").strip(),
        source_ref=(source_ref or "").strip(),
        changelog=changelog or "",
        manifest=manifest or "",
        status=status,
        created_by=operator_id,
    )
    from app.modules.pm.service import apply_brief

    apply_brief(req, extra)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def update_request(db: Session, request_id: int, body: dict) -> DeployRequest:
    req = get_request(db, request_id)
    req = sync_release_status(db, req)
    if req.status in LOCKED_STATUSES:
        label = "正在发布" if req.status == "releasing" else "已发布"
        raise BizException.bad_request(f"{label}的单子不能再改，请另提一单")

    for field in ("title", "repo", "source_ref", "changelog", "manifest"):
        if field in body:
            setattr(req, field, body[field] or "")
    from app.modules.pm.service import apply_brief

    apply_brief(req, body)
    if "pipeline_id" in body:
        req.pipeline_id = body["pipeline_id"] or None
    if body.get("status") in OPEN_STATUSES:
        req.status = body["status"]
    if req.status == "submitted":
        pipe = _pipeline_of(db, req.pipeline_id)
        _validate(req.title, req.manifest, require_manifest=manifest_required_for(pipe))
    db.commit()
    db.refresh(req)
    return req


def reject_request(db: Session, request_id: int, reason: str) -> DeployRequest:
    req = get_request(db, request_id)
    if req.status not in OPEN_STATUSES:
        raise BizException.bad_request(f"当前状态（{req.status}）不能驳回")
    if not (reason or "").strip():
        raise BizException.bad_request("请填写驳回原因，让提交人知道要改什么")
    req.status = "rejected"
    req.reject_reason = reason.strip()
    db.commit()
    db.refresh(req)
    return req


def reopen_request(db: Session, request_id: int) -> DeployRequest:
    """被驳回的单子改完可以重新提交。"""
    req = get_request(db, request_id)
    if req.status != "rejected":
        raise BizException.bad_request("只有被驳回的单子能重新提交")
    pipe = _pipeline_of(db, req.pipeline_id)
    _validate(req.title, req.manifest, require_manifest=manifest_required_for(pipe))
    req.status = "submitted"
    req.reject_reason = ""
    db.commit()
    db.refresh(req)
    return req


def mark_releasing(db: Session, request_id: int, release_id: int) -> DeployRequest:
    """单子进入「发布中」。

    注意不是「已发布」：这一刻流水线才刚被触发，可能还在等审批、排队或编译。
    真正的结果由 sync_release_status 在读取时按关联 release 回填。
    """
    req = get_request(db, request_id)
    req.status = "releasing"
    req.release_id = release_id
    db.commit()
    db.refresh(req)
    return req


def delete_request(db: Session, request_id: int) -> None:
    """删掉发布计划。正在跑的不能删，否则页面上的计划和实际执行对不上。"""
    req = get_request(db, request_id)
    req = sync_release_status(db, req)
    if req.status == "releasing":
        raise BizException.bad_request("正在发布的计划不能删除，等这次跑完或取消后再删")
    db.delete(req)
    db.commit()


# release 的终态 → 单子的终态
_RELEASE_FINAL = {
    "success": "released",
    "failed": "release_failed",
    "rejected": "release_failed",
    "cancelled": "release_failed",
    "rolled_back": "release_failed",
}


def sync_release_status(db: Session, req: DeployRequest) -> DeployRequest:
    """按关联 release 的实时状态回填单子状态。

    发布是异步的，流水线跑完不会回头通知单子。与其加一套回调（那就侵入发布流程了），
    不如在读取单子时顺带对一次账——单子本来就只在有人看的时候才需要准确。

    对账是双向的：流水线还在跑却标着「已发布」的单子（早期版本一触发就置成已发布）
    也会被拉回「发布中」。
    """
    if req.status not in LOCKED_STATUSES or not req.release_id:
        return req

    from app.modules.pipeline.models import Release

    release = db.get(Release, req.release_id)
    if release is None:
        return req
    target = _RELEASE_FINAL.get(release.status, "releasing")
    if req.status != target:
        req.status = target
        db.commit()
        db.refresh(req)
    return req


def run_params_for(req: DeployRequest, pipeline, extra: dict | None = None) -> dict:
    """把单子上的内容转成流水线执行参数。

    清单与执行弹窗、AI 发布共用 apply_execute_manifest：
    增量线步骤仍是占位且单子没填 → 当场拒绝，不写入空串、也不会写成 `**`。
    步骤已写死文件列表且单子为空 → 不注入 DEPLOY_MANIFEST，避免把写死内容替换成空。
    extra 里即使带了 DEPLOY_MANIFEST 也丢掉，发出去的必须和单子上写的一致。
    更新日志和单号始终带上，给步骤引用。
    """
    params: dict = {}
    for key, val in (extra or {}).items():
        if key == VAR_MANIFEST or val is None:
            continue
        params[str(key)] = val
    params[VAR_CHANGELOG] = req.changelog or ""
    params[VAR_REQUEST_ID] = str(req.id)
    return apply_execute_manifest(pipeline, params, req.manifest)
