# -*- coding: utf-8 -*-
"""Maven 构建：在执行目录（默认代码根）执行 mvn。

步骤里可填本机 Maven / JDK 安装目录；填了就用这一份。不填才按常见安装根扫描。
仓库里的 mvnw 只作最后兜底。多模块仓库 pom 不在根上时，填执行目录指向子模块。
"""
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

# 每个 goal 参数只允许 Maven 认的形态，避免 `package; rm` 进 argv。
_GOAL_TOKEN = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:-]*|-[A-Za-z][A-Za-z0-9._=-]*)$")


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


def resolve_workdir(src: Path, raw: str) -> Path:
    """mvn 的工作目录。留空用代码根；填了必须是代码目录下已存在的子目录。"""
    text = (raw or "").strip()
    if not text or text in {".", "./", ".\\"}:
        return src
    if any(c in text for c in "\n\r;|&`$"):
        raise BuildError("执行目录不合法")
    rel = Path(text)
    if _looks_absolute(text, rel):
        raise BuildError("执行目录必须相对代码根，不能填绝对路径")
    if ".." in rel.parts:
        raise BuildError("执行目录不能包含 ..")
    full = (src / rel).resolve()
    try:
        full.relative_to(src.resolve())
    except ValueError as exc:
        raise BuildError(f"执行目录「{raw}」不在代码目录 {src} 内") from exc
    if not full.is_dir():
        raise BuildError(f"执行目录不存在：{full}")
    return full


def _ensure_executable(path: Path) -> None:
    """给 wrapper 补执行位。从 Windows 检出的 mvnw 在 Linux 上经常没有 +x。"""
    if os.name == "nt" or not path.is_file():
        return
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        return
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _tool_install_prefixes() -> list[Path]:
    """运维手工安装 JDK/Maven 的常见根目录。只扫一层，不递归整盘。"""
    return [
        Path("/opt"),
        Path("/usr/local"),
        Path("/usr/share"),
        Path("/data/soft"),
        Path("/usr/local/soft"),
        Path.home(),
    ]


def _maven_bin_dirs() -> list[Path]:
    """本机 Maven 的 bin 目录。

    systemd 不会加载登录 shell 的 PATH 和 .bashrc，交互登录能跑的 mvn
    服务里经常找不到。按 MAVEN_HOME 和常见安装根找，不依赖 PATH。
    """
    home = Path.home()
    dirs: list[Path] = [
        Path("/usr/local/bin"),
        home / ".sdkman/candidates/maven/current/bin",
        Path(r"C:\Program Files\Apache\maven\bin"),
        Path(r"C:\Program Files\Maven\bin"),
    ]
    for key in ("MAVEN_HOME", "M2_HOME"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            dirs.append(Path(raw) / "bin")
    for prefix in _tool_install_prefixes():
        if not prefix.is_dir():
            continue
        dirs.append(prefix / "maven" / "bin")
        dirs.append(prefix / "apache-maven" / "bin")
        try:
            for child in prefix.iterdir():
                if child.is_dir() and "maven" in child.name.lower():
                    dirs.append(child / "bin")
        except OSError:
            continue
    return dirs


def _which(names: list[str], extra_dirs: list[Path]) -> str | None:
    """先 PATH，再额外目录。Windows 上 .cmd 不要求 Unix 执行位。"""
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


def _exe_from_home(home: str, names: list[str]) -> str | None:
    """把配置的安装根解析成可执行文件。home 可以是根目录或 bin 目录或文件本身。"""
    text = (home or "").strip()
    if not text:
        return None
    root = Path(text)
    candidates: list[Path] = []
    if root.is_file():
        candidates.append(root)
    else:
        for name in names:
            candidates.append(root / "bin" / name)
            candidates.append(root / name)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if os.name == "nt" or os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _wrapper_mvn(src: Path) -> str | None:
    """仓库自带的 mvnw。多数仓库没有，只在本机找不到 mvn 时才用。"""
    if os.name == "nt":
        for name in ("mvnw.cmd", "mvnw.bat", "mvnw"):
            wrapper = src / name
            if wrapper.is_file():
                return str(wrapper)
        return None
    wrapper = src / "mvnw"
    if wrapper.is_file():
        _ensure_executable(wrapper)
        return str(wrapper)
    return None


def find_mvn(src: Path, configured: str = "", workdir: Path | None = None) -> str:
    """解析要用的 mvn：步骤填写的目录 → 扫描本机 → 代码根或执行目录里的 mvnw。"""
    names = ["mvn", "mvn.cmd"]
    text = (configured or "").strip()
    if text:
        found = _exe_from_home(text, names)
        if found:
            return found
        raise BuildError(f"步骤里填写的 Maven 目录「{text}」下找不到 mvn")
    found = _which(names, _maven_bin_dirs())
    if found:
        return found
    wrapper = _wrapper_mvn(src)
    if wrapper:
        return wrapper
    if workdir is not None and workdir.resolve() != src.resolve():
        wrapper = _wrapper_mvn(workdir)
        if wrapper:
            return wrapper
    raise BuildError(
        "构建机上找不到 Maven。请在步骤里填写 Maven 安装目录，例如 /data/soft/maven"
    )


def parse_goals(raw: str) -> list[str]:
    """把「clean package」拆成参数列表，拒绝 shell 元字符。"""
    text = (raw or "").strip() or "package"
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise BuildError(f"Maven 目标无法解析：{exc}") from exc
    if not tokens:
        return ["package"]
    for token in tokens:
        if not _GOAL_TOKEN.fullmatch(token):
            raise BuildError(f"非法的 Maven 参数「{token}」，只允许 goal 名或 -D/-P 这类选项")
    return tokens


def _java_exe(home: Path) -> Path:
    """JDK 根目录下的 java 可执行文件。"""
    return home / ("bin/java.exe" if os.name == "nt" else "bin/java")


def _java_home_for(version: str) -> str | None:
    """按所选 JDK 大版本找 JAVA_HOME。找不到返回 None，调用方沿用当前环境。"""
    ver = (version or "").strip()
    if not ver:
        return None
    home = Path.home()
    candidates: list[Path] = []
    for prefix in _tool_install_prefixes() + [Path("/usr/lib/jvm")]:
        candidates.append(prefix / f"jdk{ver}")
        candidates.append(prefix / f"jdk-{ver}")
        candidates.append(prefix / f"java-{ver}")
        if prefix.is_dir():
            candidates.extend(sorted(prefix.glob(f"jdk-{ver}.*")))
            candidates.extend(sorted(prefix.glob(f"java-{ver}*")))
    candidates.extend(
        [
            Path(f"/usr/lib/jvm/java-{ver}-openjdk"),
            Path(f"/usr/lib/jvm/java-{ver}-openjdk-amd64"),
            Path(f"/usr/lib/jvm/temurin-{ver}-jdk"),
            Path(f"/usr/lib/jvm/jdk-{ver}"),
            home / f".sdkman/candidates/java/{ver}",
        ]
    )
    candidates.extend(sorted(Path(r"C:\Program Files\Java").glob(f"jdk-{ver}*")))
    candidates.extend(sorted(Path(r"C:\Program Files\Eclipse Adoptium").glob(f"jdk-{ver}*")))
    for raw in (os.environ.get(f"JAVA_HOME_{ver}") or "", os.environ.get("JAVA_HOME") or ""):
        if raw:
            candidates.insert(0, Path(raw))
    for path in candidates:
        if _java_exe(path).is_file():
            return str(path)
    return None


def _discover_java_home() -> str | None:
    """服务进程没有 JAVA_HOME 时，从常见安装根找一份能跑的 JDK。"""
    raw = (os.environ.get("JAVA_HOME") or "").strip()
    if raw and _java_exe(Path(raw)).is_file():
        return raw
    for prefix in _tool_install_prefixes() + [Path("/usr/lib/jvm")]:
        if not prefix.is_dir():
            continue
        try:
            children = sorted(prefix.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name.lower()
            if not child.is_dir() or not any(k in name for k in ("jdk", "java")):
                continue
            if _java_exe(child).is_file():
                return str(child)
    found = shutil.which("java")
    if found:
        parent = Path(found).resolve().parent
        # .../bin/java → JDK 根；.../jre/bin/java 再上一层才是 JDK
        if parent.name.lower() == "bin":
            root = parent.parent
            if _java_exe(root).is_file():
                return str(root)
    return None


def resolve_java_home(jdk: str, configured: str) -> str | None:
    """选定要用的 JAVA_HOME。

    步骤填了安装目录就用这一份，填错则失败。没填目录但选了 8/11/17/21，
    再按版本扫描本机；都没填则用 JAVA_HOME 或常见安装根。
    """
    text = (configured or "").strip()
    if text:
        if _java_exe(Path(text)).is_file():
            return text
        raise BuildError(f"步骤里填写的 JDK 目录「{text}」下找不到 java")
    if jdk:
        found = _java_home_for(jdk)
        if found:
            return found
        raise BuildError(
            f"构建机上没有 JDK {jdk}。请在本步骤填写 JDK 安装目录，"
            "或把版本改成这台机器已经装好的，或留空让步骤自动查找"
        )
    return _discover_java_home()


def resolve_settings(src: Path, raw: str) -> Path | None:
    """解析 -s 要用的 settings.xml。

    带仓库密码的 settings 不进 Git：步骤留空则不传 -s，Maven 用构建机
    ~/.m2/settings.xml 或 $MAVEN_HOME/conf/settings.xml。
    也可以填构建机上的绝对路径（文件名必须是 settings.xml）。
    相对路径仍限代码目录，给不带密码的镜像配置用。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if any(c in text for c in "\n\r;|&`$"):
        raise BuildError("settings.xml 路径不合法")
    p = Path(text)
    if _looks_absolute(text, p):
        return _absolute_settings(p if p.is_absolute() else Path(text))
    if ".." in p.parts:
        raise BuildError("settings.xml 相对路径不能包含 ..")
    full = (src / p).resolve()
    try:
        full.relative_to(src.resolve())
    except ValueError as exc:
        raise BuildError(f"settings.xml「{raw}」不在代码目录内") from exc
    if not full.is_file():
        raise BuildError(f"找不到 settings.xml：{full}")
    return full


def _looks_absolute(text: str, p: Path) -> bool:
    """判断是不是构建机上的绝对路径。不以当前进程所在 OS 为准。"""
    if p.is_absolute():
        return True
    if text.startswith("/") or text.startswith("\\\\"):
        return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def _absolute_settings(p: Path) -> Path:
    """构建机本机 settings：只接受名为 settings.xml 的文件，避免 -s 去读任意文件。"""
    if ".." in p.parts:
        raise BuildError("settings.xml 路径不能包含 ..")
    if p.name.lower() != "settings.xml":
        raise BuildError("绝对路径必须指向名为 settings.xml 的文件，例如 /data/soft/maven/conf/settings.xml")
    lowered = str(p).replace("\\", "/").lower()
    blocked = ("/proc/", "/sys/", "/dev/", "/etc/ssh", "/.ssh/", "/windows/system32")
    if any(b in lowered for b in blocked):
        raise BuildError("settings.xml 路径不在允许的位置")
    if not p.is_file():
        raise BuildError(f"找不到 settings.xml：{p}")
    return p


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
    """读步骤参数，定位 mvn，在执行目录（默认代码根）执行构建。"""
    inp = sdk.get_input()
    src = _src_dir()
    try:
        workdir = resolve_workdir(src, str(inp.get("workdir") or ""))
        mvn = find_mvn(src, str(inp.get("mavenHome") or ""), workdir)
        goals = parse_goals(str(inp.get("goal") or "package"))
        settings_path = resolve_settings(src, str(inp.get("settingsFile") or ""))
        java_home = resolve_java_home(
            str(inp.get("jdk") or "").strip(),
            str(inp.get("javaHome") or "").strip(),
        )
    except BuildError as exc:
        return _fail(str(exc))

    if not (workdir / "pom.xml").is_file() and not (workdir / "mvnw").is_file() and not (workdir / "mvnw.cmd").is_file():
        return _fail(f"执行目录 {workdir} 里没有 pom.xml，无法执行 Maven 构建")

    cmd = [mvn, *goals]
    if _truthy(inp.get("skipTests")):
        cmd.append("-DskipTests")
        cmd.append("-DskipITs")

    if settings_path is not None:
        cmd += ["-s", str(settings_path)]

    env = os.environ.copy()
    jdk = str(inp.get("jdk") or "").strip()
    if java_home:
        env["JAVA_HOME"] = java_home
        sdk.log.info(f"使用 JDK{(' ' + jdk) if jdk else ''}：{java_home}")

    # mvn 脚本靠 PATH 找 java；把 Maven 和 JDK 的 bin 放到最前面
    path_parts = []
    mvn_bin = str(Path(mvn).parent)
    if mvn_bin:
        path_parts.append(mvn_bin)
    if java_home:
        path_parts.append(str(Path(java_home) / "bin"))
    old_path = env.get("PATH") or env.get("Path") or ""
    env["PATH"] = os.pathsep.join([*path_parts, old_path] if old_path else path_parts)

    sdk.log.info(f"使用 Maven：{mvn}")
    sdk.log.info(f"工作目录 {workdir}")
    code = sdk.stream(cmd, cwd=workdir, env=env)
    if code != 0:
        return _fail(f"Maven 构建失败（退出码 {code}）")
    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": "maven ok",
            "type": sdk.output_template_type.DEFAULT,
            "data": {},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
