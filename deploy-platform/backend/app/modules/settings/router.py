"""平台配置路由。

管理员改运营项走 GET/PUT /settings；登录页需要显示名和顶栏素材，单独公开、不带密钥。
侧栏和网站图标是前端固定资源，这里不提供替换。顶栏图走专用上传接口。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import database_display, settings as boot
from app.core.deps import CurrentUser, get_current_admin
from app.core.response import R
from app.db.session import get_db
from app.modules.settings import branding
from app.modules.settings.service import get_all_settings, update_settings

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
    data = get_all_settings(db)
    data.pop("header_image_mime", None)
    data["header_has_image"] = branding.header_image_exists(db)
    data["header_image_url"] = branding.header_image_url(db)
    data["bootstrap_database"] = database_display()
    data["bootstrap_version"] = boot.app_version
    return R.ok(data)


@router.put("", summary="更新平台配置（仅管理员）")
def put_settings(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    data = update_settings(db, body)
    data.pop("header_image_mime", None)
    data["header_has_image"] = branding.header_image_exists(db)
    data["header_image_url"] = branding.header_image_url(db)
    return R.ok(data)
