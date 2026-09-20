"""凭证路由（按项目隔离）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    check_permission,
    get_current_user,
    require_project_visible,
)
from app.core.response import BizException, R
from app.core.security import encrypt, mask_secret
from app.db.session import get_db
from app.modules.credential.models import Credential
from app.modules.credential.service import encode_secret

router = APIRouter(tags=["凭证管理"])


def _to_public(c: Credential, project_name: str = "") -> dict:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "project_name": project_name,
        "name": c.name,
        "type": c.type,
        "description": c.description,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "ciphertext": mask_secret(c.ciphertext),  # 不回显明文
    }


@router.get("/credentials", summary="凭证列表（按项目隔离，脱敏）")
def list_credentials(
    project_id: int | None = None,
    scope: str = "",
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """scope=all 返回「凭证管理」页要的全量视图（项目凭证 + 全局凭证，带项目名）；
    不传 scope 时维持老行为：给了 project_id 就是项目视图，没给就是全局凭证。
    """
    from app.core.deps import visible_project_ids
    from app.modules.project.models import Project

    stmt = select(Credential).order_by(Credential.id.desc())
    if scope == "all":
        creds = list(db.scalars(stmt).all())
        if not current.is_admin:
            allowed = visible_project_ids(db, current) or set()
            creds = [c for c in creds if c.project_id is None or c.project_id in allowed]
    elif project_id is not None:
        # 项目视图：项目凭证 + 全局凭证（全局凭证可被任意项目引用）
        require_project_visible(db, current, project_id)
        creds = list(
            db.scalars(
                stmt.where(
                    (Credential.project_id == project_id) | (Credential.project_id.is_(None))
                )
            ).all()
        )
    else:
        # 全局视图：仅管理员可管理全局凭证
        if not current.is_admin:
            raise BizException.forbidden("仅管理员可查看全局凭证")
        creds = list(db.scalars(stmt.where(Credential.project_id.is_(None))).all())

    names: dict[int, str] = {}
    pids = {c.project_id for c in creds if c.project_id}
    if pids:
        for p in db.scalars(select(Project).where(Project.id.in_(pids))).all():
            names[p.id] = p.name
    return R.ok([_to_public(c, names.get(c.project_id or 0, "")) for c in creds])


@router.post("/credentials", summary="创建凭证（加密存储）")
def create_credential(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    cred_type = str(body.get("type") or "token")
    if cred_type == "password":
        plain = encode_secret(
            cred_type=cred_type,
            username=str(body.get("username") or ""),
            password=str(body.get("password") or body.get("secret") or ""),
        )
    else:
        plain = encode_secret(
            cred_type=cred_type,
            secret=str(body.get("secret") or body.get("ciphertext") or ""),
        )

    project_id = body.get("project_id")
    if project_id is not None:
        # 项目级凭证：需要项目 create 权限
        if not check_permission(db, current, "project", project_id, "create"):
            raise BizException.forbidden(f"无权限：在项目 #{project_id} 创建凭证")
    else:
        # 全局凭证：仅管理员
        if not current.is_admin:
            raise BizException.forbidden("仅管理员可创建全局凭证")

    ciphertext, iv = encrypt(plain)
    c = Credential(
        project_id=project_id,
        name=body["name"],
        type=cred_type,
        ciphertext=ciphertext,
        iv=iv,
        description=body.get("description", ""),
        created_by=current.id,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return R.ok(_to_public(c))


@router.delete("/credentials/{cred_id}", summary="删除凭证")
def delete_credential(
    cred_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    c = db.get(Credential, cred_id)
    if c is None:
        raise BizException.not_found("凭证")

    if c.project_id is not None:
        # 项目级凭证：需要项目 delete 权限
        if not check_permission(db, current, "project", c.project_id, "delete"):
            raise BizException.forbidden(f"无权限：删除项目 #{c.project_id} 的凭证")
    else:
        # 全局凭证：仅管理员
        if not current.is_admin:
            raise BizException.forbidden("仅管理员可删除全局凭证")

    # 校验无代码库正在引用该凭证
    from app.modules.repository.models import Repository

    in_use = db.scalar(
        select(Repository.id).where(Repository.credential_id == cred_id).limit(1)
    )
    if in_use:
        raise BizException.bad_request("该凭证正被代码库引用，请先解除关联")
    from app.modules.llm.models import LlmAdapterConfig, LlmModel, LlmProvider

    llm_in_use = (
        db.scalar(select(LlmProvider.id).where(LlmProvider.credential_id == cred_id).limit(1))
        or db.scalar(select(LlmModel.id).where(LlmModel.credential_id == cred_id).limit(1))
        or db.scalar(
            select(LlmAdapterConfig.id)
            .where(LlmAdapterConfig.credential_id == cred_id)
            .limit(1)
        )
    )
    if llm_in_use:
        raise BizException.bad_request("该凭证正被 LLM 配置引用，请先解除关联")

    db.delete(c)
    db.commit()
    return R.ok()


@router.put("/credentials/{cred_id}", summary="修改凭证（名称/描述/密文；仓库引用自动生效）")
def update_credential(
    cred_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    c = db.get(Credential, cred_id)
    if c is None:
        raise BizException.not_found("凭证")

    if c.project_id is not None:
        if not check_permission(db, current, "project", c.project_id, "update"):
            raise BizException.forbidden(f"无权限：修改项目 #{c.project_id} 的凭证")
    else:
        if not current.is_admin:
            raise BizException.forbidden("仅管理员可修改全局凭证")

    if "name" in body and body["name"]:
        c.name = str(body["name"]).strip()
    if "type" in body and body["type"]:
        c.type = str(body["type"])
    if "description" in body:
        c.description = body.get("description") or ""

    cred_type = str(body.get("type") or c.type or "token")
    if cred_type == "password" and (body.get("username") or body.get("password")):
        plain = encode_secret(
            cred_type="password",
            username=str(body.get("username") or ""),
            password=str(body.get("password") or ""),
        )
        ciphertext, iv = encrypt(plain)
        c.ciphertext = ciphertext
        c.iv = iv
        c.type = "password"
    else:
        secret = body.get("secret") or body.get("ciphertext")
        if secret:
            ciphertext, iv = encrypt(str(secret))
            c.ciphertext = ciphertext
            c.iv = iv

    db.commit()
    db.refresh(c)
    return R.ok(_to_public(c))
