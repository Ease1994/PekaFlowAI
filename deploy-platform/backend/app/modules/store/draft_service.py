"""插件草稿：落库、体检、发布到插件仓库。

草稿是「还没进仓库的插件源码」。AI 生成的插件一律先到这里，
经过静态校验和（可选的）沙箱试跑，再由管理员发布 —— 发布也只是变成「已上传待安装」，
真正让流水线用上还要管理员再点一次安装。多留一道，是因为插件在构建机上是以 Agent 身份跑的任意代码。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.store import plugin_lint, plugin_service
from app.modules.store.models import Plugin, PluginDraft

DRAFT_PENDING = "pending"
DRAFT_PUBLISHED = "published"
DRAFT_REJECTED = "rejected"


def _as_files(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise BizException.bad_request("files 必须是 {文件名: 源码} 的对象")
    out: dict[str, str] = {}
    for path, content in raw.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise BizException.bad_request("files 的键和值都必须是字符串")
        out[path.strip()] = content
    return out


def _as_schema(raw: object) -> dict:
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise BizException.bad_request(f"config_schema 不是合法 JSON: {e}") from e
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


def build_meta(draft: PluginDraft) -> dict:
    """草稿 → task.json 内容。"""
    return {
        "name": draft.name,
        "display_name": draft.display_name or draft.name,
        "category": draft.category or "exec",
        "version": draft.version or "1.0.0",
        "language": draft.language or "python",
        "entrypoint": draft.entrypoint or "python3 task.py",
        "description": draft.description or "",
        "config_schema": _as_schema(draft.config_schema),
    }


def lint_payload(db: Session, meta: dict, files: dict[str, str]) -> dict:
    """静态体检，并补一条「会覆盖已有插件」的提醒（这条要查库，放在 lint 之外）。"""
    result = plugin_lint.lint_draft(meta, files)
    existing = db.scalar(select(Plugin).where(Plugin.name == meta.get("name")))
    if existing is not None:
        result["findings"].append({
            "level": plugin_lint.LEVEL_WARN,
            "code": "name_exists",
            "message": (
                f"仓库里已有同名插件（当前 v{existing.version}），发布会覆盖它的元数据和包，"
                f"请确认 version 更高且改动是预期的"
            ),
            "where": "task.json:name",
        })
        result["warn_count"] += 1
    return result


def create_draft(
    db: Session,
    *,
    meta: dict,
    files: dict[str, str],
    intent: str = "",
    source: str = "ai",
    created_by: int | None = None,
) -> PluginDraft:
    files = _as_files(files)
    schema = _as_schema(meta.get("config_schema"))
    normalized = dict(meta)
    normalized["config_schema"] = schema

    result = lint_payload(db, normalized, files)
    if not result["ok"]:
        blocking = [f["message"] for f in result["findings"] if f["level"] == plugin_lint.LEVEL_ERROR]
        raise BizException.bad_request("插件草稿不合法：" + "；".join(blocking[:3]))

    draft = PluginDraft(
        name=str(normalized.get("name") or "").strip(),
        display_name=str(normalized.get("display_name") or "").strip(),
        category=str(normalized.get("category") or "exec"),
        version=str(normalized.get("version") or "1.0.0"),
        description=str(normalized.get("description") or ""),
        config_schema=json.dumps(schema, ensure_ascii=False),
        language=str(normalized.get("language") or "python"),
        entrypoint=str(normalized.get("entrypoint") or "python3 task.py"),
        files_json=json.dumps(files, ensure_ascii=False),
        source=source,
        intent=intent,
        created_by=created_by,
        status=DRAFT_PENDING,
        lint_json=json.dumps(result, ensure_ascii=False),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def get_draft(db: Session, draft_id: int) -> PluginDraft:
    draft = db.get(PluginDraft, draft_id)
    if draft is None:
        raise BizException.not_found("插件草稿")
    return draft


def list_drafts(
    db: Session, status: str | None = None, created_by: int | None = None
) -> list[PluginDraft]:
    stmt = select(PluginDraft).order_by(PluginDraft.id.desc())
    if status:
        stmt = stmt.where(PluginDraft.status == status)
    if created_by is not None:
        stmt = stmt.where(PluginDraft.created_by == created_by)
    return list(db.scalars(stmt).all())


def assert_draft_access(draft: PluginDraft, current) -> None:
    """管理员能看全部草稿；普通人只能碰自己创建的。"""
    if getattr(current, "is_admin", False):
        return
    if draft.created_by != getattr(current, "id", None):
        raise BizException.forbidden("只能操作自己的插件草稿")


def draft_files(draft: PluginDraft) -> dict[str, str]:
    try:
        raw = json.loads(draft.files_json or "{}")
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def relint(db: Session, draft: PluginDraft) -> dict:
    result = lint_payload(db, build_meta(draft), draft_files(draft))
    draft.lint_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    return result


def pack_draft(draft: PluginDraft) -> bytes:
    """草稿 → 插件 zip（task.json + 源码 + Python SDK）。"""
    meta = build_meta(draft)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("task.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for path, content in draft_files(draft).items():
            norm = path.replace("\\", "/")
            if norm.startswith("/") or any(part == ".." for part in Path(norm).parts):
                continue
            zf.writestr(norm, content)
        if (draft.language or "python") == "python":
            _bundle_python_sdk(zf)
    return buf.getvalue()


def _bundle_python_sdk(zf: zipfile.ZipFile) -> None:
    """把 SDK 一起打进去，插件包才能在构建机上离线跑起来（无 pip 依赖）。"""
    sdk_root = plugin_service.PLUGIN_SRC_ROOT / "sdk" / "python" / "release_atom_sdk"
    if not sdk_root.is_dir():
        return
    for path in sorted(sdk_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        zf.writestr(
            f"release_atom_sdk/{path.relative_to(sdk_root).as_posix()}",
            path.read_text(encoding="utf-8"),
        )


def publish_draft(
    db: Session,
    draft_id: int,
    *,
    reviewer_id: int,
    acknowledge_high: bool = False,
    comment: str = "",
) -> Plugin:
    """草稿 → 插件仓库。发布后仍是「已上传未安装」，要再点安装才会被流水线用到。"""
    draft = get_draft(db, draft_id)
    if draft.status == DRAFT_PUBLISHED:
        raise BizException.bad_request("该草稿已经发布过了")

    result = relint(db, draft)
    if not result["ok"]:
        raise BizException.bad_request("校验未通过，不能发布，请先修正错误项")
    if result["high_count"] > 0 and not acknowledge_high:
        raise BizException.bad_request(
            f"存在 {result['high_count']} 个高危项，需要确认已逐条审阅代码后才能发布"
        )

    data = pack_draft(draft)
    plugin_service.refuse_builtin_overwrite(draft.name)
    plugin_service.require_third_party_signature(db, data, draft.name)
    path, sha = plugin_service.save_package(draft.name, draft.version, data)
    plugin = plugin_service.upsert_plugin_from_meta(
        db, build_meta(draft), package_path=path, package_sha=sha, installed=False
    )
    draft.status = DRAFT_PUBLISHED
    draft.reviewed_by = reviewer_id
    draft.review_comment = comment
    db.commit()
    return plugin


def reject_draft(db: Session, draft_id: int, *, reviewer_id: int, comment: str) -> PluginDraft:
    draft = get_draft(db, draft_id)
    if draft.status == DRAFT_PUBLISHED:
        raise BizException.bad_request("已发布的草稿不能驳回")
    draft.status = DRAFT_REJECTED
    draft.reviewed_by = reviewer_id
    draft.review_comment = comment
    db.commit()
    db.refresh(draft)
    return draft


def delete_draft(db: Session, draft_id: int) -> None:
    """从草稿箱删掉记录。已发布进仓库的插件不受影响。"""
    draft = get_draft(db, draft_id)
    db.delete(draft)
    db.commit()


def start_trial(
    db: Session,
    draft_id: int,
    *,
    pipeline_id: int,
    agent_tag: str,
    params: dict | None,
    operator_id: int,
) -> "object":
    """在指定流水线上跑一次这个草稿插件。

    走 create_release：占线互斥、环境隔离、测试分组直接排队、生产分组要审批。
    试跑只允许测试/开发流水线，避免未发布插件在生产构建机上跑。
    """
    import json as _json

    from app.core.env import is_disposable
    from app.modules.pipeline.service import (
        PIPELINE_ACTIVE,
        RELEASE_QUEUED,
        create_release,
        execute_release,
        get_pipeline,
    )
    from app.modules.pipeline.sub_pipeline import env_code_of
    from app.modules.project.models import Group

    draft = get_draft(db, draft_id)
    result = relint(db, draft)
    if not result["ok"]:
        raise BizException.bad_request("校验未通过，先修正错误项再试跑")

    pipeline = get_pipeline(db, pipeline_id)
    if pipeline.status != PIPELINE_ACTIVE:
        raise BizException.bad_request("只能在未删除的流水线上试跑")
    env = env_code_of(db, pipeline)
    if not is_disposable(env):
        grp = db.get(Group, pipeline.group_id)
        raise BizException.bad_request(
            f"插件试跑只能选测试/开发流水线，不能占用「{grp.name if grp else env}」。"
            "生产/UAT/预发请先把插件发布到仓库再走正常发布"
        )
    tag = (agent_tag or "").strip() or "linux"

    step = {
        "name": f"试跑 {draft.name}",
        "plugin": draft.name,
        "with": params or {},
        "package": {
            "name": draft.name,
            "version": f"draft{draft.id}-{draft.version}",
            "language": draft.language or "python",
            "entrypoint": draft.entrypoint or "python3 task.py",
            "sha256": hashlib.sha256(pack_draft(draft)).hexdigest(),
            "download_path": f"/api/v1/store/plugin-drafts/{draft.id}/package",
        },
    }
    plan_json = _json.dumps(
        {
            "name": "插件试跑",
            "job_name": f"试跑 {draft.name}",
            "jobs": [
                {
                    "id": "plugin-trial",
                    "name": f"试跑 {draft.name}",
                    "agent": tag,
                    "steps": [step],
                }
            ],
        },
        ensure_ascii=False,
    )
    release = create_release(
        db,
        pipeline_id=pipeline.id,
        version=f"plugin-trial-{draft.name}-{draft.version}",
        strategy="rolling",
        trigger_by="plugin_trial",
        operator_id=operator_id,
        plan_json=plan_json,
        skip_approval=False,
    )
    if release.status == RELEASE_QUEUED:
        execute_release(db, release.id)
        db.refresh(release)

    draft.trial_release_id = release.id
    draft.trial_status = release.status
    db.commit()
    db.refresh(release)
    return release


def sync_trial_status(db: Session, draft: PluginDraft) -> str:
    """试跑状态跟着发布走，读的时候顺手同步一下。"""
    if not draft.trial_release_id:
        return ""
    from app.modules.pipeline.models import Release

    release = db.get(Release, draft.trial_release_id)
    if release is None:
        return draft.trial_status or ""
    if release.status != draft.trial_status:
        draft.trial_status = release.status
        db.commit()
    return draft.trial_status


def serialize(draft: PluginDraft, *, with_files: bool = False) -> dict:
    try:
        lint = json.loads(draft.lint_json or "{}")
    except json.JSONDecodeError:
        lint = {}
    data = {
        "id": draft.id,
        "name": draft.name,
        "display_name": draft.display_name,
        "category": draft.category,
        "version": draft.version,
        "description": draft.description,
        "language": draft.language,
        "entrypoint": draft.entrypoint,
        "source": draft.source,
        "intent": draft.intent,
        "status": draft.status,
        "review_comment": draft.review_comment,
        "created_by": draft.created_by,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "lint": lint,
        "trial_release_id": draft.trial_release_id,
        "trial_status": draft.trial_status,
    }
    if with_files:
        data["config_schema"] = _as_schema(draft.config_schema)
        data["files"] = draft_files(draft)
        data["task_json"] = json.dumps(build_meta(draft), ensure_ascii=False, indent=2)
    return data
