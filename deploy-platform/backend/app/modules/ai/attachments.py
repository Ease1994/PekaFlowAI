"""对话框附件的落盘与打包。

存在 data/chat-uploads/{用户}/{批次}/ 下，和制品分开：制品是发布的产物，
这些是发布的输入，生命周期也不一样——没被用掉的过一天就清了。
"""
from __future__ import annotations

import hashlib
import io
import shutil
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.ai.models import AiAttachment
from app.modules.artifact.storage import safe_filename

# app/modules/ai/attachments.py → backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
UPLOAD_DIR = _BACKEND_ROOT / "data" / "chat-uploads"

# 单个文件上限。这个口子是给「临时补个 dll、改个配置」用的，
# 真要发几百兆的东西，说明该走正经流水线了
MAX_FILE_BYTES = 100 * 1024 * 1024
# 一次下发的总量上限
MAX_TOTAL_BYTES = 300 * 1024 * 1024
# 没被用掉的附件留多久
KEEP_HOURS = 24


def _user_dir(user_id: int) -> Path:
    return UPLOAD_DIR / str(user_id)


def save(db: Session, user_id: int, filename: str, rel_path: str, data: bytes) -> AiAttachment:
    """存一个附件，返回落库记录。"""
    if not data:
        raise BizException.bad_request(f"文件「{filename}」是空的")
    if len(data) > MAX_FILE_BYTES:
        raise BizException.bad_request(
            f"文件「{filename}」超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限，"
            "这么大的东西建议走流水线发布"
        )

    batch = f"{int(time.time() * 1000)}-{hashlib.sha1(filename.encode('utf-8')).hexdigest()[:8]}"
    dest_dir = _user_dir(user_id) / batch
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_filename(filename)
    dest.write_bytes(data)

    row = AiAttachment(
        user_id=user_id,
        name=Path(filename.replace("\\", "/")).name or "file",
        rel_path=_clean_rel_path(rel_path or filename),
        size_bytes=len(data),
        storage_key=str(dest),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _clean_rel_path(raw: str) -> str:
    """整理 zip 内的相对路径：挡掉目录穿越和盘符，保留层级。

    浏览器拖文件夹时给的是 webkitRelativePath，形如 dist/bin/a.dll，
    这个层级要留着——用户拖的就是「按这个结构覆盖过去」。
    """
    text = (raw or "").replace("\\", "/").strip()
    parts = []
    for seg in text.split("/"):
        seg = seg.strip()
        # 空段、当前目录、上跳、盘符一律丢掉，剩下的才是干净的层级
        if not seg or seg == "." or seg == ".." or ":" in seg:
            continue
        parts.append(seg)
    return "/".join(parts) or "file"


def list_pending(db: Session, user_id: int, ids: list[int]) -> list[AiAttachment]:
    """取出还没被用掉的附件，顺便挡住越权引用别人的文件。"""
    if not ids:
        return []
    rows = db.scalars(
        select(AiAttachment).where(
            AiAttachment.id.in_(ids), AiAttachment.user_id == user_id
        )
    ).all()
    found = {r.id for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise BizException.bad_request(f"附件 {missing} 不存在或不属于你，请重新上传")
    used = [r for r in rows if r.consumed_release_id]
    if used:
        raise BizException.bad_request(
            f"附件「{used[0].name}」已经在发布 #{used[0].consumed_release_id} 里用过了，请重新上传"
        )
    total = sum(r.size_bytes for r in rows)
    if total > MAX_TOTAL_BYTES:
        raise BizException.bad_request(
            f"这批文件共 {total // 1024 // 1024}MB，超过单次 "
            f"{MAX_TOTAL_BYTES // 1024 // 1024}MB 上限"
        )
    # 按上传顺序，保证 zip 内顺序稳定
    return sorted(rows, key=lambda r: r.id)


def pack(rows: list[AiAttachment]) -> bytes:
    """打成 zip：节点侧的 file-transfer 只认 zip，路径就是相对站点根目录的位置。"""
    if not rows:
        raise BizException.bad_request("没有要下发的文件")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()
        for r in rows:
            path = Path(r.storage_key)
            if not path.is_file():
                raise BizException.bad_request(f"附件「{r.name}」的文件已经不在了，请重新上传")
            arc = r.rel_path or r.name
            # 同名文件只留第一个：zip 里出现重名，解压端行为不确定
            if arc in seen:
                raise BizException.bad_request(f"有两个文件都叫「{arc}」，请改名后重传")
            seen.add(arc)
            zf.write(path, arcname=arc)
    return buf.getvalue()


def mark_consumed(db: Session, rows: list[AiAttachment], release_id: int) -> None:
    for r in rows:
        r.consumed_release_id = release_id
    db.commit()


def cleanup_expired(db: Session) -> int:
    """清掉没被用掉的过期附件。上传时顺手调用，不另起定时任务。"""
    deadline = datetime.now() - timedelta(hours=KEEP_HOURS)
    rows = db.scalars(
        select(AiAttachment).where(
            AiAttachment.consumed_release_id == 0, AiAttachment.created_at < deadline
        )
    ).all()
    removed = 0
    for r in rows:
        try:
            parent = Path(r.storage_key).parent
            if parent.is_dir() and parent.parent.name == str(r.user_id):
                shutil.rmtree(parent, ignore_errors=True)
        except OSError:
            pass
        db.delete(r)
        removed += 1
    if removed:
        db.commit()
    return removed


def public(row: AiAttachment) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "rel_path": row.rel_path,
        "size_bytes": row.size_bytes,
    }
