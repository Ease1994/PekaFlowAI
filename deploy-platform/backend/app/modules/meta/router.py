"""登录后可读的平台 HTTP 接口清单。

外网 Nginx 只反代 /api 和 /mcp，不配 /docs。需要看有哪些方法时走本接口，
再由前端「调用手册」页渲染；未登录拿不到文档。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, assert_visible_menu, get_current_user
from app.core.response import R
from app.db.session import get_db

router = APIRouter(tags=["调用手册"])


@router.get("/meta/openapi", summary="当前平台 HTTP 接口清单（须登录）")
def openapi_spec(
    request: Request,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """返回 FastAPI 生成的 OpenAPI 文档。

    须登录，且侧栏「调用手册」对该用户可见。管理员在菜单管理里取消该菜单后，
    普通人进不了页面，本接口也 403，避免只藏菜单、文档接口仍裸奔。
    """
    assert_visible_menu(db, current, "handbook")
    return R.ok(request.app.openapi())
