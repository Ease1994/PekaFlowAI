"""用户 API Token 路由（拥有与用户同等权限）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizException, R
from app.core.security import generate_api_token
from app.db.session import get_db
from app.modules.auth.models import ApiToken

router = APIRouter(tags=["API Token"])

# 有效期：默认 1 年，最长 3 年
DEFAULT_DAYS = 365
MAX_DAYS = 1095


def _to_public(t: ApiToken) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "revoked": t.revoked,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _purge_expired(db: Session, user_id: int) -> None:
    """过期的自动删除：用户一看列表就顺手清掉，不留垃圾行。"""
    db.query(ApiToken).filter(
        ApiToken.user_id == user_id,
        ApiToken.expires_at.is_not(None),
        ApiToken.expires_at < datetime.now(),
    ).delete(synchronize_session=False)
    db.commit()


@router.post("/api-tokens", summary="创建 API Token（返回明文，仅此一次）")
def create_api_token(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    name = (body.get("name") or "").strip() or "API Token"
    days = int(body.get("days") or DEFAULT_DAYS)
    if days < 1 or days > MAX_DAYS:
        raise BizException.bad_request(f"有效期需在 1 ~ {MAX_DAYS} 天之间（默认 {DEFAULT_DAYS} 天）")

    raw, token_hash = generate_api_token()
    t = ApiToken(
        user_id=current.id,
        name=name,
        token_hash=token_hash,
        expires_at=datetime.now() + timedelta(days=days),
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    data = _to_public(t)
    data["token"] = raw  # 明文只在创建时返回一次
    return R.ok(data)


@router.get("/api-tokens", summary="我的 API Token 列表（脱敏）")
def list_api_tokens(
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    _purge_expired(db, current.id)
    tokens = db.scalars(
        select(ApiToken)
        .where(ApiToken.user_id == current.id, ApiToken.revoked.is_(False))
        .order_by(ApiToken.id.desc())
    ).all()
    return R.ok([_to_public(t) for t in tokens])


@router.delete("/api-tokens/{token_id}", summary="吊销 API Token")
def revoke_api_token(
    token_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    t = db.get(ApiToken, token_id)
    if t is None or t.user_id != current.id:
        raise BizException.not_found("API Token")
    t.revoked = True
    db.commit()
    return R.ok()
