# -*- coding: utf-8 -*-
"""把工作区内匹配 glob 的文件打成 zip，上传到平台制品库。

归档路径必须落在本任务工作区：产物会进制品库给后续部署步骤下载，
填绝对路径或 .. 就会把构建机上别的目录打进去。
"""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402

PACKAGE_TYPE = "zip"
# 一次归档文件数上限，避免误填 ** 把整棵工作区打进去把构建机打满。
_MAX_FILES = 5000
# 版本号会当文件名用，也会塞进 multipart 的头
_UNSAFE_VERSION_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class ArchiveError(Exception):
    """归档参数不合法或没有匹配到文件。"""


def _safe_version(raw: str) -> str:
    """把版本号收拾成能安全当文件名用的形式。"""
    cleaned = _UNSAFE_VERSION_CHARS.sub("_", (raw or "").strip()).strip("._-")
    return cleaned[:120] or "latest"


def _safe_name(raw: str) -> str:
    """制品名只留文件名安全字符，避免 zip 落到工作区外或拆开 multipart 头。"""
    cleaned = _UNSAFE_NAME_CHARS.sub("_", (raw or "").strip()).strip("._-")
    return cleaned[:80] or "artifact"


def _safe_filename(name: str) -> str:
    """multipart 头里的 filename 不能带引号和换行。"""
    return name.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")


def _under_workspace(workspace: Path, path: Path, *, what: str) -> Path:
    """路径必须落在本任务工作区里。resolve 会解开符号链接。"""
    root = workspace.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise ArchiveError(
            f"{what}「{path}」不在任务工作区 {root} 内。"
            "不要填绝对路径或 ..，否则会把工作区外的文件打进制品"
        ) from exc
    return resolved


def _pattern_ok(pattern: str) -> bool:
    """glob 本身不能是绝对路径，也不能带 .. 段。"""
    text = (pattern or "").strip().replace("\\", "/")
    if not text:
        return False
    if Path(text).is_absolute() or text.startswith("/"):
        return False
    return ".." not in Path(text).parts


def _search_roots(workspace: Path) -> list[Path]:
    """先在 src/ 下匹配，没有再退回工作区根。Maven 产物默认在 src/target。"""
    src = workspace / "src"
    roots: list[Path] = []
    if src.is_dir():
        roots.append(src)
    roots.append(workspace)
    return roots


def collect_files(workspace: Path, pattern: str) -> list[Path]:
    """按 glob 收集文件。所有命中项必须在工作区内。"""
    text = (pattern or "").strip()
    if not _pattern_ok(text):
        raise ArchiveError(
            f"归档路径「{pattern}」不合法：必须是工作区内的相对 glob，例如 target/*.jar"
        )
    posix = text.replace("\\", "/")
    seen: set[Path] = set()
    out: list[Path] = []
    for root in _search_roots(workspace):
        for hit in sorted(root.glob(posix)):
            if not hit.is_file():
                continue
            resolved = _under_workspace(workspace, hit, what="归档文件")
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(resolved)
        if out:
            break
    if not out:
        raise ArchiveError(f"没有匹配到文件：{pattern}")
    if len(out) > _MAX_FILES:
        raise ArchiveError(f"匹配到 {len(out)} 个文件，超过上限 {_MAX_FILES}，请收窄 glob")
    return out


def _rel_arcname(workspace: Path, path: Path) -> str:
    """zip 内路径相对工作区，保持 target/app.jar 这种结构。"""
    try:
        return path.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        src = (workspace / "src").resolve()
        return path.relative_to(src).as_posix()


def _build_zip(workspace: Path, files: list[Path], dest: Path) -> list[str]:
    """打 zip，返回包内相对路径列表。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arc = _rel_arcname(workspace, path)
            zf.write(path, arcname=arc)
            names.append(arc)
    return names


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode("utf-8")


def _upload(dest: Path, version: str, files: list[str]) -> dict:
    """用任务级凭证上传。归属由平台按凭证判定，插件改不了 pipeline_id。"""
    import urllib.request
    import uuid

    server = sdk.get_server_url()
    token = sdk.get_task_token()
    if not server or not token:
        raise RuntimeError("缺少平台地址或任务凭证，请升级构建机 Agent 后重试")

    boundary = uuid.uuid4().hex
    meta = json.dumps({"files": files}, ensure_ascii=False)
    head = b"".join([
        _multipart_field(boundary, "type", PACKAGE_TYPE),
        _multipart_field(boundary, "version", version),
        _multipart_field(boundary, "meta", meta),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{_safe_filename(dest.name)}"\r\n'
            f"Content-Type: application/zip\r\n\r\n"
        ).encode("utf-8"),
    ])
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    size = dest.stat().st_size

    def body():
        yield head
        with dest.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        yield tail

    req = urllib.request.Request(
        f"{server}/api/v1/plugin-api/artifacts/upload",
        data=body(),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + size + len(tail)),
            "X-Task-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body_text = json.loads(resp.read().decode("utf-8"))
    if body_text.get("code") not in (0, 200):
        raise RuntimeError(f"上传制品失败：{body_text.get('message')}")
    return body_text.get("data") or {}


def main() -> int:
    inp = sdk.get_input()
    workspace = Path(sdk.get_workspace()).resolve()

    try:
        files = collect_files(workspace, str(inp.get("sourcePath") or ""))
    except ArchiveError as e:
        sdk.log.error(str(e))
        return 1

    name = _safe_name(str(inp.get("artifactName") or "artifact"))
    version = _safe_version(str(inp.get("version") or sdk.get_release_id() or ""))
    sdk.log.info(f"匹配到 {len(files)} 个文件：")
    for path in files[:50]:
        sdk.log.info(f"  {path}")
    if len(files) > 50:
        sdk.log.info(f"  ... 另有 {len(files) - 50} 个文件")

    dest = workspace / "_release_pack" / f"{name}-{version}.zip"
    names = _build_zip(workspace, files, dest)
    sdk.log.info(f"已打包：{dest.name}（{dest.stat().st_size / 1024:.1f} KB）")

    try:
        data = _upload(dest, version, names)
    except Exception as e:  # noqa: BLE001
        sdk.log.error(f"上传制品失败：{e}")
        return 1

    sdk.log.info(f"制品已上传，artifact_id={data.get('artifact_id')}")
    sdk.set_output(
        {
            "artifact_id": {"type": "string", "value": str(data.get("artifact_id", ""))},
            "artifact_name": {"type": "string", "value": dest.name},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
