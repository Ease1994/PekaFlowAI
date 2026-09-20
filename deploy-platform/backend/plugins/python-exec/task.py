# -*- coding: utf-8 -*-
"""在代码目录执行一段 Python。

脚本文件必须在工作区内：否则填 /etc/cron 或 .. 就能读构建机任意文件。
这是「这次检出里要跑哪段脚本」，不是本机 Python 装在哪。
解释器按 PATH、PYTHON_HOME 和 /data/soft 这类安装根探测。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402


class ExecError(Exception):
    """脚本路径不合法或本机没有 Python。"""


def _src_dir(workspace: Path) -> Path:
    """代码目录：工作空间下的 src/，没有则退回工作空间根。"""
    src = workspace / "src"
    return src if src.is_dir() else workspace


def _under_workspace(workspace: Path, path: Path, *, what: str) -> Path:
    """路径必须落在本任务工作区。resolve 会解开符号链接。"""
    root = workspace.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise ExecError(
            f"{what}「{path}」不在任务工作区 {root} 内，已拒绝执行"
        ) from exc
    return resolved


def resolve_script(workspace: Path, file_path: str, content: str) -> Path:
    """确定要执行的 .py：仓库内相对路径，或把表单内容写到工作区临时文件。"""
    text = (file_path or "").strip()
    if text:
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ExecError("脚本文件必须是工作区内的相对路径，不能填绝对路径或 ..")
        src = _src_dir(workspace)
        path = _under_workspace(workspace, src / candidate, what="脚本文件")
        if not path.is_file():
            raise ExecError(f"脚本文件不存在：{path}")
        return path
    body = content or ""
    if not body.strip():
        raise ExecError("请填写脚本内容，或指定仓库内的脚本文件")
    dest = workspace / "_release_python_exec.py"
    dest.write_text(body, encoding="utf-8")
    return dest


def _python_bin_dirs() -> list[Path]:
    """本机 Python 可能在的目录。systemd 不会读用户 .bashrc。"""
    home = Path.home()
    dirs = [
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/snap/bin"),
        Path("/opt/homebrew/bin"),
        home / ".pyenv/shims",
        home / ".local/bin",
        Path(r"C:\Python312"),
        Path(r"C:\Python311"),
        Path(r"C:\Program Files\Python312"),
        Path(r"C:\Program Files\Python311"),
        Path(r"C:\Program Files\Python310"),
    ]
    local_py = Path.home() / "AppData/Local/Programs/Python"
    if local_py.is_dir():
        dirs.extend(sorted(local_py.glob("Python3*")))
    for key in ("PYTHON_HOME", "PYTHON_BIN"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            p = Path(raw)
            dirs.insert(0, p.parent if p.is_file() else p)
    for prefix in (Path("/data/soft"), Path("/opt"), Path("/usr/local")):
        if not prefix.is_dir():
            continue
        try:
            for child in prefix.iterdir():
                if child.is_dir() and "python" in child.name.lower():
                    dirs.append(child / "bin")
                    dirs.append(child)
        except OSError:
            continue
    return dirs


def _which(names: list[str], extra_dirs: list[Path]) -> str | None:
    """先 PATH，再额外目录。Windows 上 .exe 不要求 Unix 执行位。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for folder in extra_dirs:
        if not folder.is_dir():
            continue
        for name in names:
            candidate = folder / name
            if not candidate.is_file():
                continue
            if os.name == "nt" or os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def find_python(configured: str = "") -> str:
    """定位 python 解释器。步骤填了目录就用这一份，再 PATH 和常见安装根。"""
    names = ["python.exe", "python3.exe", "python"] if os.name == "nt" else ["python3", "python"]
    text = (configured or "").strip()
    if text:
        root = Path(text)
        candidates = [root] if root.is_file() else []
        for name in names:
            candidates.append(root / "bin" / name)
            candidates.append(root / name)
        for candidate in candidates:
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
        raise ExecError(f"步骤里填写的 Python 目录「{text}」下找不到 python")
    found = _which(names, _python_bin_dirs())
    if found:
        return found
    raise ExecError(
        "构建机上找不到 python。请在本步骤填写 Python 安装目录，"
        "或把解释器写进 Agent 服务的 PATH"
    )


def main() -> int:
    inp = sdk.get_input()
    workspace = Path(sdk.get_workspace()).resolve()
    src = _src_dir(workspace)

    try:
        python = find_python(str(inp.get("pythonHome") or ""))
        script = resolve_script(
            workspace,
            str(inp.get("file") or ""),
            str(inp.get("content") or inp.get("script") or ""),
        )
    except ExecError as e:
        sdk.log.error(str(e))
        return 1

    sdk.log.info(f"使用 {python}")
    sdk.log.info(f"执行 {script}")
    code = sdk.stream([python, str(script)], cwd=src)
    if code != 0:
        sdk.log.error(f"脚本退出码 {code}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
