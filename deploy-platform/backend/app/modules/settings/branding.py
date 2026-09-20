"""平台品牌与会话类配置。

侧栏名、发件人名、登录过期走 KV。顶栏图落在 data/branding/，不进 KV 的 value。
侧栏 / 网站图标是前端固定资源（favicon.svg + DeploymentUnitOutlined），这里不提供替换入口。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException

# 显示名、发件人名长度上限，避免侧栏和邮件头被撑爆
DISPLAY_NAME_MAX = 32
FROM_NAME_MAX = 64
# 登录 Session：1～30 天
SESSION_DAYS_DEFAULT = 1
SESSION_DAYS_MIN = 1
SESSION_DAYS_MAX = 30
DEFAULT_DISPLAY_NAME = "PekaFlowAI"
DEFAULT_FROM_NAME = "PekaFlowAI"
# 库里若仍是旧出厂名，按新产品名显示，避免升级后侧栏还写着上一版称呼
LEGACY_FACTORY_NAMES = frozenset(
    {
        "PekaFlow",
        "发布部署平台",
        "發佈部署平台",
        "Release Platform",
        "Release-Plattform",
        "リリースプラットフォーム",
        "रिलीज़ प्लेटफ़ॉर्म",
        "Plataforma de release",
    }
)

# 顶栏通知：空文案不显示横幅；颜色只认这几个醒目色
NOTICE_TEXT_MAX = 200
NOTICE_COLORS = ("red", "orange", "gold", "magenta", "volcano")
NOTICE_COLOR_DEFAULT = "red"

# 顶栏图片：按魔数认类型，禁止 SVG（可嵌脚本）。1MB 够一条横幅图
HEADER_IMAGE_MAX = 1 * 1024 * 1024
HEADER_IMAGE_DIR = Path(__file__).resolve().parents[3] / "data" / "branding"
HEADER_IMAGE_PATH = HEADER_IMAGE_DIR / "header-image"
# 公开地址给 <img src> 用，不走鉴权
HEADER_IMAGE_PUBLIC_PATH = "/api/v1/settings/header-image"


def is_factory_display_name(raw: object) -> bool:
    """空值、当前出厂名、以及历史出厂名，都按未自定义处理。"""
    text = str(raw or "").strip()
    return (not text) or text == DEFAULT_DISPLAY_NAME or text in LEGACY_FACTORY_NAMES


def clamp_display_name(raw: object) -> str:
    """平台显示名：去空白、截断；空或旧出厂名回 PekaFlowAI。"""
    text = str(raw or "").strip()[:DISPLAY_NAME_MAX]
    if is_factory_display_name(text):
        return DEFAULT_DISPLAY_NAME
    return text


def clamp_from_name(raw: object) -> str:
    """通知发件人显示名：去空白、截断；空则回默认。"""
    text = str(raw or "").strip()[:FROM_NAME_MAX]
    return text or DEFAULT_FROM_NAME


def clamp_session_expire_days(raw: object) -> int:
    """登录 Session 天数夹到 1～30；非法输入当 1 天。"""
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = SESSION_DAYS_DEFAULT
    return max(SESSION_DAYS_MIN, min(SESSION_DAYS_MAX, days))


def clamp_public_app_base(raw: object) -> str:
    """对外站点根地址：去空白、去掉末尾斜杠。空表示邮件链接改用请求 Origin。"""
    return str(raw or "").strip().rstrip("/")


def clamp_notice_text(raw: object) -> str:
    """顶栏通知文案：去两端空白并截断；空表示不显示横幅。"""
    return str(raw or "").strip()[:NOTICE_TEXT_MAX]


def clamp_notice_color(raw: object) -> str:
    """顶栏通知颜色：只认预设醒目色，其它回红色。"""
    color = str(raw or "").strip().lower()
    return color if color in NOTICE_COLORS else NOTICE_COLOR_DEFAULT


def session_expire_seconds(db: Session) -> int:
    """当前平台设置换算成 JWT 过期秒数，签发登录 token 时用。"""
    from app.modules.settings.service import get_setting

    days = clamp_session_expire_days(get_setting(db, "session_expire_days", str(SESSION_DAYS_DEFAULT)))
    return days * 86400


def detect_image_mime(data: bytes) -> str:
    """按文件头判断图片类型。不是常见位图就拒绝，避免把 HTML / SVG 当图存。"""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise BizException.bad_request("只支持 PNG / JPEG / GIF / WEBP 图片")


def header_image_exists(db: Session) -> bool:
    """MIME 有值且文件在盘上，才算顶栏有图。"""
    from app.modules.settings.service import get_setting

    mime = (get_setting(db, "header_image_mime") or "").strip()
    return bool(mime) and HEADER_IMAGE_PATH.is_file() and HEADER_IMAGE_PATH.stat().st_size > 0


def header_image_url(db: Session) -> str:
    """有图才给带缓存戳的公开地址；没图返回空串，前端就不渲染 <img>。"""
    if not header_image_exists(db):
        return ""
    stamp = int(HEADER_IMAGE_PATH.stat().st_mtime)
    return f"{HEADER_IMAGE_PUBLIC_PATH}?v={stamp}"


def _write_mime(db: Session, mime: str) -> None:
    """只改顶栏图片 MIME。这条不能走 PUT /settings，避免表单一并保存时把文件状态冲掉。"""
    from app.modules.settings.models import PlatformSetting

    row = db.scalar(select(PlatformSetting).where(PlatformSetting.key == "header_image_mime"))
    if row is None:
        db.add(PlatformSetting(key="header_image_mime", value=mime))
    else:
        row.value = mime
    db.commit()


def save_header_image(db: Session, data: bytes) -> dict:
    """校验体积和类型后落盘，并记下 MIME。覆盖旧文件。"""
    if not data:
        raise BizException.bad_request("请选择图片")
    if len(data) > HEADER_IMAGE_MAX:
        raise BizException.bad_request("图片不能超过 1MB")
    mime = detect_image_mime(data)
    HEADER_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEADER_IMAGE_PATH.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(HEADER_IMAGE_PATH)
    _write_mime(db, mime)
    return {"header_image_url": header_image_url(db), "header_has_image": True}


def delete_header_image(db: Session) -> None:
    """删掉顶栏图，顶栏左侧回到空白。"""
    try:
        HEADER_IMAGE_PATH.unlink(missing_ok=True)
        HEADER_IMAGE_PATH.with_suffix(".tmp").unlink(missing_ok=True)
    except OSError:
        pass
    _write_mime(db, "")


def header_image_file(db: Session) -> tuple[Path, str]:
    """给公开 GET 用：路径 + MIME。没有图就 404，避免 <img> 拿到 HTML 信封。"""
    from app.modules.settings.service import get_setting

    if not header_image_exists(db):
        raise BizException.not_found("顶栏图片")
    mime = (get_setting(db, "header_image_mime") or "").strip() or "application/octet-stream"
    return HEADER_IMAGE_PATH, mime


def public_branding(db: Session) -> dict:
    """登录页、侧栏、顶栏共用的公开品牌信息（不含密钥）。"""
    from app.modules.settings.service import get_setting

    return {
        "display_name": clamp_display_name(
            get_setting(db, "platform_display_name", DEFAULT_DISPLAY_NAME)
        ),
        "header_image_url": header_image_url(db),
        "header_notice_text": clamp_notice_text(get_setting(db, "header_notice_text")),
        "header_notice_color": clamp_notice_color(
            get_setting(db, "header_notice_color", NOTICE_COLOR_DEFAULT)
        ),
    }
