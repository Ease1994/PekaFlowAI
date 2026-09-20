# -*- coding: utf-8 -*-
"""按发布清单从编译产物里提取增量文件，打成 zip 上传到平台。

包里的文件结构与站点根目录一一对应，目标机上的 Agent 直接解压覆盖即可，
不需要再解析一次清单——包内的文件列表就是「本次会动哪些文件」的权威答案。
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
from manifest import ManifestError, collect  # noqa: E402

PACKAGE_TYPE = "iis-package"

# 版本号会当文件名用，也会塞进 multipart 的头，只留这些字符
_UNSAFE_VERSION_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_version(raw: str) -> str:
    """把版本号收拾成能安全当文件名用的形式。

    值来自流水线变量，没人会故意填坏，但不净化就有两个后果：带 ../ 能让 zip
    落到 workspace 之外去；带引号或换行能把 multipart 的头拆开。都是一填错就
    出怪事、且很难往这儿联想的问题。
    """
    cleaned = _UNSAFE_VERSION_CHARS.sub("_", (raw or "").strip()).strip("._-")
    return cleaned[:120] or "latest"


def _safe_filename(name: str) -> str:
    """multipart 头里的 filename 不能带引号和换行，否则整个请求头就散了。"""
    return name.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")


def _under_workspace(workspace: Path, path: Path, *, what: str) -> Path:
    """打包根必须落在本任务工作区里。

    清单命中的文件会原样打进增量包、再解压到生产站点。sourceDir 若是绝对路径
    或带 ../，就会把工作区外的文件（甚至别的站点）打进包发上去。
    """
    root = workspace.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestError(
            f"{what}「{path}」不在任务工作区 {root} 内。"
            "不要填绝对路径或 ..，否则会把工作区外的文件打进发布包覆盖生产"
        ) from exc
    return resolved


def _resolve_source(workspace: Path, raw: str) -> Path:
    """编译产物目录：相对路径按 src/ 解释；越出工作区直接拒绝。"""
    value = (raw or "").strip()
    if not value:
        raise ManifestError("必须填写编译产物目录（sourceDir）")
    p = Path(value)
    if p.is_absolute():
        candidate = p
    else:
        src = workspace / "src"
        candidate = (src if src.is_dir() else workspace) / value
    return _under_workspace(workspace, candidate, what="编译产物目录")


def _build_zip(root: Path, files: list[str], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(root / rel, arcname=rel)


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode("utf-8")


def _upload(dest: Path, version: str, files: list[str]) -> dict:
    """用任务级凭证上传，归属由平台按凭证判定。

    请求体是边读边发的，不整包进内存：以前的写法先 read_bytes() 再 b"".join()，
    一个 500MB 的包峰值要占 1GB 上下。构建机上常常同时跑着几个任务，
    这一下就可能把内存打满，连带把别人的构建也拖垮。
    """
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
        with dest.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        yield tail

    req = urllib.request.Request(
        f"{server}/api/v1/plugin-api/artifacts/upload",
        data=body(),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            # 必须显式给长度：不给的话 urllib 会退回 chunked 编码，
            # 而 Nginx / 平台侧的上传处理未必认
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
    workspace = Path(sdk.get_workspace())

    try:
        root = _resolve_source(workspace, inp.get("sourceDir", ""))
        files = collect(root, inp.get("manifest", ""))
    except ManifestError as e:
        sdk.log.error(str(e))
        return 1

    sdk.log.info(f"编译产物目录：{root}")
    sdk.log.info(f"按清单命中 {len(files)} 个文件：")
    for rel in files[:50]:
        sdk.log.info(f"  {rel}")
    if len(files) > 50:
        sdk.log.info(f"  ... 另有 {len(files) - 50} 个文件")

    total = sum((root / f).stat().st_size for f in files)
    sdk.log.info(f"合计 {total / 1024:.1f} KB")

    if str(inp.get("dryRun", "")).lower() in ("true", "1", "yes"):
        sdk.log.warning("预演模式：只列出清单命中的文件，不打包也不上传")
        sdk.set_output({"deploy_files": {"type": "string", "value": str(len(files))}})
        return 0

    version = _safe_version(str(inp.get("version") or sdk.get_release_id() or ""))
    dest = workspace / "_release_pack" / f"incremental-{version}.zip"
    _build_zip(root, files, dest)
    sdk.log.info(f"已打包：{dest.name}（{dest.stat().st_size / 1024:.1f} KB）")

    try:
        data = _upload(dest, version, files)
    except Exception as e:  # noqa: BLE001
        sdk.log.error(f"上传制品失败：{e}")
        return 1

    sdk.log.info(f"制品已上传，artifact_id={data.get('artifact_id')}")
    sdk.set_output(
        {
            "deploy_package_id": {"type": "string", "value": str(data.get("artifact_id", ""))},
            "deploy_files": {"type": "string", "value": str(len(files))},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
