"""会话气泡里的附图。

发给模型看的是 data URL，不能原样写进 user/message：MySQL TEXT 只有 64KB，
整图 base64 会撑爆事件，落库失败后前端回显被空投影顶掉，发出去的图就像消失了。
这里把图落到 data/chat-images/{会话}/，事件里只记本会话短链。
"""
from __future__ import annotations

import base64
import re
import shutil
import uuid
from pathlib import Path

from app.core.response import BizException

# app/modules/ai/chat_images.py → backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
# 运行时数据，和待下发附件分开：这些图只给对话气泡和模型看，不下发到节点
CHAT_IMAGE_DIR = _BACKEND_ROOT / "data" / "chat-images"

# 单张上限。截图和手机照片够用；再大的应走附件下发
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# 和前端待发送预览上限一致
MAX_IMAGES = 8
# 短链文件名：32 位 hex + 扩展名，挡掉目录穿越
_SAFE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|webp|gif)$")
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_EXT_MEDIA = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def persist(session_id: int, images: list[str] | None) -> list[str]:
    """把本轮附图收成可写进事件的短链。

    data URL 解码后落盘；http(s) 直链本身就短，原样留下。
    单张坏图跳过，不把整轮对话打挂。
    """
    out: list[str] = []
    sid = int(session_id)
    for url in (images or [])[:MAX_IMAGES]:
        text = str(url or "").strip()
        if not text:
            continue
        if text.startswith("http://") or text.startswith("https://"):
            out.append(text)
            continue
        if not text.startswith("data:image/"):
            continue
        try:
            data, ext = _decode_data_url(text)
        except ValueError:
            continue
        name = f"{uuid.uuid4().hex}.{ext}"
        dest = CHAT_IMAGE_DIR / str(sid) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        out.append(_public_path(sid, name))
    return out


def public_urls(session_id: int, raw) -> list[str]:
    """投影给前端的图：只认本会话短链和 http(s)，不把超长 data URL 塞回 GET。"""
    sid = int(session_id)
    prefix = _public_path(sid, "")
    out: list[str] = []
    for item in raw or []:
        url = str(item.get("url") if isinstance(item, dict) else item or "").strip()
        if not url:
            continue
        if url.startswith("http://") or url.startswith("https://"):
            out.append(url)
            continue
        name = url[len(prefix):] if url.startswith(prefix) else ""
        if name and _SAFE_NAME.fullmatch(name):
            out.append(url)
    return out[:MAX_IMAGES]


def resolve(session_id: int, name: str) -> tuple[Path, str]:
    """按短链文件名取出落盘文件。不属于本会话、名字不合法都当不存在。"""
    if not _SAFE_NAME.fullmatch(name or ""):
        raise BizException.not_found("图片")
    root = (CHAT_IMAGE_DIR / str(int(session_id))).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise BizException.not_found("图片")
    ext = path.suffix.lstrip(".").lower()
    return path, _EXT_MEDIA.get(ext, "application/octet-stream")


def remove_session(session_id: int) -> None:
    """会话删除后清掉附图目录，避免截图在盘上一直留着。"""
    root = CHAT_IMAGE_DIR / str(int(session_id))
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


def _public_path(session_id: int, name: str) -> str:
    """前端 axios 的 baseURL 已是 /api/v1，短链从 /ai 起。"""
    return f"/ai/sessions/{int(session_id)}/images/{name}"


def _decode_data_url(url: str) -> tuple[bytes, str]:
    """解析 data:image/...;base64,... 得到二进制和扩展名。"""
    header, sep, payload = url.partition(",")
    if not sep or "base64" not in header.lower():
        raise ValueError("not base64 data url")
    mime = header[5:].split(";")[0].strip().lower()
    ext = _MIME_EXT.get(mime)
    if not ext:
        raise ValueError("unsupported image type")
    try:
        data = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("empty or too large")
    return data, ext
