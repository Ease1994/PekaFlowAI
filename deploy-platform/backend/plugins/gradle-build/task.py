# -*- coding: utf-8 -*-
"""Gradle 构建：优先仓库 gradlew，再找本机 gradle。"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402

# 每个任务参数只允许 Gradle 认的形态，避免 `build && reboot` 进 argv。
_TASK_TOKEN = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:-]*|-[A-Za-z][A-Za-z0-9._=-]*)$")


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


def _ensure_executable(path: Path) -> None:
    """给 wrapper 补执行位。从 Windows 检出的 gradlew 在 Linux 上经常没有 +x。"""
    if os.name == "nt" or not path.is_file():
        return
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        return
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _gradle_bin_dirs() -> list[Path]:
    """本机 Gradle 可能在的 bin 目录。systemd 不会读用户 .bashrc。"""
    home = Path.home()
    dirs = [
        Path("/opt/gradle/bin"),
        Path("/usr/share/gradle/bin"),
        Path("/usr/local/bin"),
        Path("/snap/bin"),
        home / ".sdkman/candidates/gradle/current/bin",
        Path(r"C:\Program Files\Gradle\bin"),
    ]
    raw = (os.environ.get("GRADLE_HOME") or "").strip()
    if raw:
        dirs.append(Path(raw) / "bin")
    dirs.extend(sorted(Path("/opt").glob("gradle-*/bin")))
    for prefix in (Path("/data/soft"), Path("/usr/local"), Path("/usr/local/soft"), home):
        if not prefix.is_dir():
            continue
        dirs.append(prefix / "gradle" / "bin")
        try:
            for child in prefix.iterdir():
                if child.is_dir() and "gradle" in child.name.lower():
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


def find_gradle(src: Path, configured: str = "") -> str:
    """解析要用的 gradle。步骤填了目录就用这一份；否则 wrapper，再扫常见目录。"""
    names = ["gradle", "gradle.bat"]
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
        raise BuildError(f"步骤里填写的 Gradle 目录「{text}」下找不到 gradle")
    wrapper = src / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if wrapper.is_file():
        _ensure_executable(wrapper)
        return str(wrapper)
    found = _which(names, _gradle_bin_dirs())
    if found:
        return found
    raise BuildError(
        "构建机上找不到 Gradle。请在本步骤填写 Gradle 安装目录，或把仓库里的 gradlew 一并提交"
    )


def parse_tasks(raw: str) -> list[str]:
    """把「clean build」拆成参数列表，拒绝 shell 元字符。"""
    text = (raw or "").strip() or "build"
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise BuildError(f"Gradle 任务无法解析：{exc}") from exc
    if not tokens:
        return ["build"]
    for token in tokens:
        if not _TASK_TOKEN.fullmatch(token):
            raise BuildError(f"非法的 Gradle 参数「{token}」")
    return tokens


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


def _java_home(configured: str) -> str | None:
    """步骤填写的 JDK 根目录。填了必须能找到 bin/java。"""
    text = (configured or "").strip()
    if not text:
        return None
    root = Path(text)
    java = root / "bin" / ("java.exe" if os.name == "nt" else "java")
    if java.is_file():
        return str(root)
    raise BuildError(f"步骤里填写的 JDK 目录「{text}」下找不到 java")


def main() -> int:
    """读步骤参数，定位 gradle，在代码目录执行构建。"""
    inp = sdk.get_input()
    src = _src_dir()
    try:
        gradle = find_gradle(src, str(inp.get("gradleHome") or ""))
        tasks = parse_tasks(str(inp.get("tasks") or "build"))
        java_home = _java_home(str(inp.get("javaHome") or ""))
    except BuildError as exc:
        return _fail(str(exc))

    cmd = [gradle, *tasks]
    if _truthy(inp.get("skipTests")):
        cmd += ["-x", "test"]

    env = os.environ.copy()
    if java_home:
        env["JAVA_HOME"] = java_home
        bin_dir = str(Path(java_home) / "bin")
        old_path = env.get("PATH") or env.get("Path") or ""
        env["PATH"] = os.pathsep.join([bin_dir, old_path] if old_path else [bin_dir])
        sdk.log.info(f"使用 JDK：{java_home}")

    sdk.log.info(f"使用 Gradle：{gradle}")
    sdk.log.info(f"工作目录 {src}")
    code = sdk.stream(cmd, cwd=src, env=env)
    if code != 0:
        return _fail(f"Gradle 构建失败（退出码 {code}）")
    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": "gradle ok",
            "type": sdk.output_template_type.DEFAULT,
            "data": {},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
