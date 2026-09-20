# -*- coding: utf-8 -*-
"""npm 构建：安装依赖并跑 package.json 里的 script。"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402

# npm script 名对应 package.json 的 key，禁止跟额外命令。
_SCRIPT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class BuildError(Exception):
    pass


def _truthy(v) -> bool:
    """表单 checkbox 在 JSON 里可能是 bool 或字符串。"""
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _src_dir() -> Path:
    """代码目录：工作空间下的 src/，没有则退回工作空间根。"""
    workspace = Path(sdk.get_workspace())
    src = workspace / "src"
    return src if src.is_dir() else workspace


def _tool_install_prefixes() -> list[Path]:
    """运维手工安装 Node 的常见根目录。只扫一层。"""
    return [
        Path("/opt"),
        Path("/usr/local"),
        Path("/usr/local/nodejs"),
        Path("/data/soft"),
        Path("/usr/local/soft"),
        Path.home(),
    ]


def _npm_bin_dirs() -> list[Path]:
    """本机 npm 可能在的目录。nvm / NODEJS_HOME 对 systemd PATH 不可见。"""
    home = Path.home()
    dirs = [
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/snap/bin"),
        Path("/usr/local/nodejs/bin"),
        home / ".nvm/versions/node",
        Path(r"C:\Program Files\nodejs"),
        Path(r"C:\Program Files (x86)\nodejs"),
    ]
    for key in ("NODEJS_HOME", "NODE_HOME"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            p = Path(raw)
            dirs.append(p / "bin")
            dirs.append(p)
    nvm = home / ".nvm/versions/node"
    if nvm.is_dir():
        dirs.extend(sorted(nvm.glob("*/bin"), reverse=True))
    for prefix in _tool_install_prefixes():
        if not prefix.is_dir():
            continue
        dirs.append(prefix / "bin")
        dirs.append(prefix / "nodejs" / "bin")
        dirs.append(prefix / "node" / "bin")
        try:
            for child in prefix.iterdir():
                if child.is_dir() and "node" in child.name.lower():
                    dirs.append(child / "bin")
        except OSError:
            continue
    return dirs


def _which(names: list[str], extra_dirs: list[Path]) -> str | None:
    """先 PATH，再额外目录。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for folder in extra_dirs:
        if not folder.is_dir():
            continue
        for name in names:
            candidate = folder / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def find_npm(configured: str = "") -> str:
    """解析 npm。步骤填了 Node 目录就用这一份，再 PATH 和常见安装根。"""
    names = ["npm.cmd", "npm"] if os.name == "nt" else ["npm"]
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
        raise BuildError(f"步骤里填写的 Node 目录「{text}」下找不到 npm")
    found = _which(names, _npm_bin_dirs())
    if found:
        return found
    raise BuildError(
        "构建机上找不到 npm。请在本步骤填写 Node.js 安装目录"
        "（如 /usr/local/nodejs），或把 npm 写进 Agent 服务的 PATH"
    )


def _fail(msg: str) -> int:
    """失败出口：写日志和插件输出，返回非 0。"""
    sdk.log.error(msg)
    sdk.set_output(
        {
            "status": sdk.status.FAILURE,
            "message": msg,
            "type": sdk.output_template_type.DEFAULT,
            "data": {},
        }
    )
    return 1


def main() -> int:
    """读步骤参数，定位 npm，安装依赖并跑指定 script。"""
    inp = sdk.get_input()
    src = _src_dir()
    if not (src / "package.json").is_file():
        return _fail(f"代码目录 {src} 里没有 package.json，无法执行 npm 构建")

    script = str(inp.get("script") or "build").strip()
    if not _SCRIPT_NAME.fullmatch(script):
        return _fail(f"非法的 npm script 名「{script}」")

    try:
        npm = find_npm(str(inp.get("nodeHome") or ""))
    except BuildError as exc:
        return _fail(str(exc))

    sdk.log.info(f"工作目录 {src}")
    if _truthy(inp.get("install") if "install" in inp else True):
        install_cmd = [npm, "ci"] if (src / "package-lock.json").is_file() else [npm, "install"]
        code = sdk.stream(install_cmd, cwd=src)
        if code != 0:
            return _fail(f"npm 安装依赖失败（退出码 {code}）")

    code = sdk.stream([npm, "run", script], cwd=src)
    if code != 0:
        return _fail(f"npm run {script} 失败（退出码 {code}）")
    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": "npm ok",
            "type": sdk.output_template_type.DEFAULT,
            "data": {},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
