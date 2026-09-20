"""用户管理路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_admin
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.account import service
from app.modules.settings import get_all_settings

router = APIRouter(tags=["用户管理"])


@router.get("/account/users", summary="用户列表")
def list_users(
    keyword: str = Query(default=""),
    source: str = Query(default=""),
    status: str = Query(default=""),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(service.list_users(db, keyword=keyword, source=source, status=status))


@router.post("/account/users", summary="新增本地用户")
def create_user(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(
        service.create_local(
            db,
            username=str(body.get("username") or ""),
            password=str(body.get("password") or ""),
            display_name=str(body.get("display_name") or ""),
            email=str(body.get("email") or ""),
            is_admin=bool(body.get("is_admin")),
        )
    )


@router.put("/account/users/{user_id}", summary="编辑用户")
def update_user(
    user_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    return R.ok(
        service.update_user(
            db,
            current,
            user_id,
            display_name=body.get("display_name"),
            email=body.get("email"),
            is_admin=body.get("is_admin"),
            status=body.get("status"),
        )
    )


@router.post("/account/users/{user_id}/reset-password", summary="重置本地用户密码")
def reset_password(
    user_id: int,
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(service.reset_password(db, user_id, str(body.get("password") or "")))


@router.delete("/account/users/{user_id}", summary="删除用户")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    return R.ok(service.delete_user(db, current, user_id))


@router.post("/account/ldap/search", summary="搜索 LDAP 用户")
def ldap_search(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    from app.modules.auth.ldap_service import ldap_search_users

    cfg = get_all_settings(db)
    if cfg.get("ldap_enabled") != "true":
        raise BizException.bad_request("请先在平台设置中开启 LDAP")
    try:
        rows = ldap_search_users(cfg, str(body.get("keyword") or ""), limit=int(body.get("limit") or 30))
    except Exception as e:  # noqa: BLE001
        raise BizException.service_unavailable(f"LDAP 不可用：{e}") from e
    return R.ok(rows)


@router.post("/account/ldap/import", summary="导入 LDAP 用户")
def ldap_import(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    entries = body.get("users") or []
    if not entries and body.get("username"):
        entries = [body]
    created = [service.import_ldap_user(db, e) for e in entries]
    return R.ok({"imported": len(created), "users": created})


@router.post("/account/ldap/test", summary="测试 LDAP 连通")
def ldap_test(
    body: dict | None = None,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    from app.modules.auth.ldap_service import ldap_test as do_test

    cfg = get_all_settings(db)
    username = str((body or {}).get("username") or "")
    try:
        return R.ok(do_test(cfg, username))
    except Exception as e:  # noqa: BLE001
        raise BizException.service_unavailable(f"LDAP 连接失败：{e}") from e
