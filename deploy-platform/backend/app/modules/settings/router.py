"""平台配置路由。

管理员改运营项走 GET/PUT /settings；登录页需要显示名和顶栏素材，单独公开、不带密钥。
侧栏和网站图标是前端固定资源，这里不提供替换。顶栏图走专用上传接口。
导入导出只搬运营配置 JSON，不包含顶栏图片，也不包含数据库连接和 JWT 密钥。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import database_display, settings as boot
from app.core.deps import CurrentUser, get_current_admin
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.audit.service import write as write_audit
from app.modules.settings import branding
from app.modules.settings.service import (
    MAX_IMPORT_BYTES,
    export_document,
    get_all_settings,
    import_settings,
    update_settings,
)

router = APIRouter(prefix="/settings", tags=["平台配置"])


@router.get("/branding", summary="公开品牌信息（登录页/侧栏/顶栏）")
def get_branding(db: Session = Depends(get_db)):
    """显示名、顶栏图地址、通知横幅。不要求登录，也不带密钥。"""
    return R.ok(branding.public_branding(db))


@router.get("/header-image", summary="顶栏图片（公开）")
def get_header_image(db: Session = Depends(get_db)):
    """给 <img src> 用。没图返回 404，前端有图才渲染，不会出现裂图占位。"""
    path, mime = branding.header_image_file(db)
    return FileResponse(
        path,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.post("/header-image", summary="上传顶栏图片（仅管理员）")
async def upload_header_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    data = await file.read()
    return R.ok(branding.save_header_image(db, data), message="顶栏图片已更新")


@router.delete("/header-image", summary="清除顶栏图片（仅管理员）")
def clear_header_image(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    branding.delete_header_image(db)
    return R.ok({"header_has_image": False, "header_image_url": ""}, message="顶栏图片已清除")


@router.get("", summary="获取平台配置（仅管理员）")
def get_settings(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    data = _with_header(db, get_all_settings(db))
    data["bootstrap_database"] = database_display()
    data["bootstrap_version"] = boot.app_version
    return R.ok(data)


def _with_header(db: Session, data: dict) -> dict:
    """补上顶栏图状态。MIME 不返回给页面。"""
    data.pop("header_image_mime", None)
    data["header_has_image"] = branding.header_image_exists(db)
    data["header_image_url"] = branding.header_image_url(db)
    return data


@router.get("/export", summary="导出平台配置（仅管理员）")
def export_platform_settings(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_admin),
):
    """下载当前生效的运营配置。文件含密码和接入凭证，审计只记项数，不记值。"""
    payload = export_document(db)
    write_audit(
        db,
        "settings.export",
        resource_type="platform_setting",
        detail=f"keys={len(payload['settings'])}",
        user_id=user.id,
        username=user.username,
    )
    db.commit()
    return R.ok(payload)


@router.post("/import", summary="导入平台配置（仅管理员）")
async def import_platform_settings(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_admin),
):
    """用本页导出的 JSON 覆盖其中写明的项。空文件或格式不对时不改任何配置。

    先看原始字节，超限直接拒绝，避免把超大正文解析进内存后再丢弃。
    审计只记写入了哪些键，不记密码和密钥。
    """
    raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES:
        raise BizException.bad_request("配置文件过大，已拒绝导入")
    if not raw.strip():
        raise BizException.bad_request("文件里没有可导入的配置项")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BizException.bad_request("配置文件格式不正确")
    data, count = import_settings(
        db,
        payload,
        user_id=user.id,
        username=user.username,
    )
    data = _with_header(db, data)
    return R.ok({"imported_count": count, "settings": data}, message="配置已导入")


@router.put("", summary="更新平台配置（仅管理员）")
def put_settings(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(_with_header(db, update_settings(db, body)))
