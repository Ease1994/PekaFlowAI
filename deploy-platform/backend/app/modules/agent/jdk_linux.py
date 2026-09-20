"""Linux 节点安装用的 JDK 包。

包不进 git、也不该只活在镜像层里：每次 docker compose --build 都会换掉镜像，
agent-java/jdk-linux/ 里的 tar.gz 就没了。真正长期放的位置是数据卷
data/shared-packs/jdk-linux/（compose 已挂 backend-data → /app/data）。

查找顺序：公共包目录 → 旧路径 data/jdk-linux → 镜像/源码里的 agent-java。
后两处找到时会拷进公共包目录，之后重建镜像也不用再带这份文件。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from fastapi import UploadFile

from app.core.response import BizException
from app.modules.agent.shared_packs import pack_dir

# 公共包根下按用途细分；旧部署曾直接放在 data/jdk-linux/
DATA_DIR = pack_dir("jdk-linux")
LEGACY_DIR = DATA_DIR.parent.parent / "jdk-linux"
CANONICAL_NAME = "jdk-8u271-linux-x64.tar.gz"
# Oracle / Temurin JDK 8 linux x64 大约 80～200MB
MAX_BYTES = 400 * 1024 * 1024
# gzip 魔数，挡掉改后缀的随便一个文件
_GZIP_MAGIC = b"\x1f\x8b"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]+\.tar\.gz$")


def find() -> Path | None:
    """找到可下发的 JDK 包。公共包目录优先；必要时把旧路径或镜像里的那份固化进去。"""
    found = _in_dir(DATA_DIR)
    if found:
        return found
    for src in (_in_dir(LEGACY_DIR), _bundled()):
        if src is None:
            continue
        seeded = _seed_into_data(src)
        return seeded or src
    return None


def status() -> dict:
    """给节点管理页看：有没有包、是不是已经在公共包目录里。"""
    path = find()
    if path is None:
        return {"ready": False, "filename": "", "size_bytes": 0, "persistent": False}
    return {
        "ready": True,
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "persistent": _is_under(path, DATA_DIR),
    }


async def save_upload(file: UploadFile) -> dict:
    """管理员从页面上传。覆盖公共包目录里旧包，不在镜像层再留一份。"""
    name = Path(str(file.filename or "")).name
    if not _SAFE_NAME.fullmatch(name):
        raise BizException.bad_request(
            "请上传 .tar.gz，文件名只含字母数字和 ._+-，例如 jdk-8u271-linux-x64.tar.gz"
        )
    lower = name.lower()
    if "jdk" not in lower or "linux" not in lower:
        raise BizException.bad_request("文件名需要同时带 jdk 和 linux，避免传成别的压缩包")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f".upload-{name}.tmp"
    size = 0
    try:
        with tmp.open("wb") as out:
            first = True
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                if first:
                    if not chunk.startswith(_GZIP_MAGIC):
                        raise BizException.bad_request("不是 gzip 包（.tar.gz 应以 gzip 魔数开头）")
                    first = False
                size += len(chunk)
                if size > MAX_BYTES:
                    raise BizException.bad_request(
                        f"JDK 包不能超过 {MAX_BYTES // 1024 // 1024}MB"
                    )
                out.write(chunk)
        if size <= 0:
            raise BizException.bad_request("上传的文件是空的")
        dest = DATA_DIR / name
        dest.unlink(missing_ok=True)
        tmp.replace(dest)
        _remove_other_tarballs(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return status()


def _bundled() -> Path | None:
    """镜像或源码树里的包，只作为公共包目录的种子。"""
    backend_root = DATA_DIR.parents[2] if len(DATA_DIR.parents) >= 3 else Path.cwd()
    for base in (backend_root, Path("/app"), Path.cwd()):
        for folder in (base / "agent-java" / "jdk-linux", base / "agent-java"):
            found = _in_dir(folder)
            if found:
                return found
    return None


def _in_dir(folder: Path) -> Path | None:
    """固定文件名优先，否则扫带 jdk 和 linux 的 .tar.gz。"""
    if not folder.is_dir():
        return None
    named = folder / CANONICAL_NAME
    if named.is_file() and named.stat().st_size > 0:
        return named
    matches = [
        child
        for child in folder.iterdir()
        if child.is_file()
        and child.name.lower().endswith(".tar.gz")
        and "jdk" in child.name.lower()
        and "linux" in child.name.lower()
        and child.stat().st_size > 0
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda p: p.name)[0]


def _seed_into_data(src: Path) -> Path | None:
    """把旧路径或镜像里的包拷进公共包目录。失败不挡下载，下次重建就只能再上传。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        dest = DATA_DIR / src.name
        if dest.is_file() and dest.stat().st_size == src.stat().st_size:
            return dest
        shutil.copy2(src, dest)
        return dest
    except OSError:
        return None


def _is_under(path: Path, root: Path) -> bool:
    """path 是否落在 root 目录下（含其自身）。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _remove_other_tarballs(keep: Path) -> None:
    """该子目录只留当前这份，避免多次上传堆出几个 200MB 文件。"""
    if not DATA_DIR.is_dir():
        return
    for child in DATA_DIR.iterdir():
        if child == keep or not child.is_file():
            continue
        if child.name.lower().endswith(".tar.gz"):
            child.unlink(missing_ok=True)
