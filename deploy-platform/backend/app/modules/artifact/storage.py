"""制品落盘存储。

先用本地目录，路径结构与插件包一致（backend/data/ 下）；
以后换对象存储只要替换这一层，Artifact.storage_key 仍然是唯一定位标识。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.core.response import BizException

# app/modules/artifact/storage.py → backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_DIR = _BACKEND_ROOT / "data" / "artifacts"

MAX_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2GB，增量包远小于这个量级
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(name: str) -> str:
    """只保留文件名本体，挡掉目录穿越和奇怪字符。"""
    base = Path(name.replace("\\", "/")).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._")
    return cleaned or "artifact.zip"


def save(release_id: int, filename: str, data: bytes) -> tuple[str, str]:
    """存一个制品，返回 (落盘路径, sha256)。"""
    if not data:
        raise BizException.bad_request("制品内容为空")
    if len(data) > MAX_SIZE_BYTES:
        raise BizException.bad_request("制品超过 2GB 上限")

    dest_dir = ARTIFACT_DIR / str(release_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_filename(filename)
    dest.write_bytes(data)
    return str(dest), hashlib.sha256(data).hexdigest()


def resolve(storage_key: str) -> Path:
    """把 storage_key 还原成文件路径，顺带挡住越界读取。"""
    if not storage_key:
        raise BizException.not_found("制品文件")
    path = Path(storage_key).resolve()
    try:
        path.relative_to(ARTIFACT_DIR.resolve())
    except ValueError:
        raise BizException.forbidden("制品路径越界") from None
    if not path.is_file():
        raise BizException.not_found("制品文件")
    return path


def remove(storage_key: str) -> bool:
    """删掉一个制品文件，顺带清理空掉的发布目录。

    文件已经不在、或被占用删不掉，都不该拦住调用方删数据库记录：
    记录留着而文件没了，页面上就会一直列着一个下不动的制品。
    """
    if not storage_key:
        return False
    try:
        path = resolve(storage_key)
    except Exception:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    parent = path.parent
    try:
        if parent != ARTIFACT_DIR.resolve() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return True


def remove_release_artifacts(release_id: int) -> None:
    """清理某次发布的全部制品文件（流水线彻底删除时调用）。"""
    import shutil

    target = ARTIFACT_DIR / str(release_id)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
