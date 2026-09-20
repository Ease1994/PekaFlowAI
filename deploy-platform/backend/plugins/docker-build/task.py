# -*- coding: utf-8 -*-
"""构建 Docker 镜像并推送到仓库。

镜像 tag 默认取构建号而不是 latest：回滚要靠「上一个版本的镜像还在仓库里」，
所有版本都叫 latest 的话，回退到哪一个都无从谈起。
"""
from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402


class BuildError(Exception):
    pass


_ALLOWED_EXTRA_FLAGS = {
    "--build-arg",
    "--label",
    "--target",
    "--cache-from",
    "--cache-to",
    "--progress",
    "--pull",
    "--no-cache",
    "--quiet",
    "-q",
    "--network",
    "--add-host",
}

_DENIED_FLAGS = {
    "--privileged",
    "--mount",
    "--volume",
    "-v",
    "--security-opt",
    "--cap-add",
    "--device",
    "--cgroup-parent",
}


def _flag_name(arg: str) -> str:
    """取出 --foo=bar 里的 --foo。"""
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[0]
    return arg


def _parse_flag_value(args: list[str], index: int) -> tuple[str | None, int]:
    """读当前选项的值，返回 (value, 下一个下标)。"""
    arg = args[index]
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[1], index + 1
    nxt = index + 1
    if nxt < len(args) and not args[nxt].startswith("-"):
        return args[nxt], nxt + 1
    return None, nxt


def _assert_safe_extra_args(extra: str) -> list[str]:
    """构建 extraArgs 走允许列表；--network=host / --privileged 直接拒绝。"""
    text = (extra or "").strip()
    if not text:
        return []
    try:
        args = shlex.split(text)
    except ValueError as e:
        raise BuildError(f"额外参数无法解析（{e}），请检查引号是否配对：{extra}") from e
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            raise BuildError(f"额外参数里出现了非选项「{arg}」")
        name = _flag_name(arg)
        value, nxt = _parse_flag_value(args, i)
        if name in _DENIED_FLAGS:
            raise BuildError(f"拒绝危险的 docker 参数：{name}")
        if name not in _ALLOWED_EXTRA_FLAGS:
            raise BuildError(f"不允许的额外参数 {name}")
        if name == "--network" and (value or "").strip().lower() in {"host", "container:host"}:
            raise BuildError("禁止使用 --network=host")
        if name == "--network" and (value or "").strip().lower().startswith("container:"):
            raise BuildError("禁止使用 --network=container:...，会绑到宿主机上其它容器的网络栈")
        joined = " ".join(args[i:nxt]).lower()
        if "docker.sock" in joined:
            raise BuildError("禁止挂载 docker.sock")
        out.extend(args[i:nxt])
        i = nxt
    return out


def _truthy(v) -> bool:
    """表单 switch 在 JSON 里可能是 bool 或字符串。"""
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _lines(raw) -> list[str]:
    """多行文本按行拆，忽略空行和 # 注释。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _docker_bin_dirs() -> list[Path]:
    """本机 docker 客户端可能在的目录。

    官方 apt 包在 /usr/bin，systemd 默认 PATH 能看到。Ubuntu snap、Docker Desktop
    装在 /snap/bin 或 Program Files 下，服务进程 PATH 里没有，shutil.which 会判成没装。
    """
    home = Path.home()
    dirs = [
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/snap/bin"),
        Path("/usr/local/sbin"),
        home / ".docker/bin",
        Path(r"C:\Program Files\Docker\Docker\resources\bin"),
        Path(r"C:\Program Files\Docker\Docker\resources"),
        Path(r"C:\ProgramData\DockerDesktop\version-bin"),
    ]
    raw = (os.environ.get("DOCKER_BIN") or "").strip()
    if raw:
        p = Path(raw)
        dirs.insert(0, p.parent if p.is_file() else p)
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


def _under_workspace(workspace: Path, path: Path, *, what: str) -> Path:
    """构建上下文和 Dockerfile 必须在本任务工作区内。

    docker build 会把上下文整棵发给引擎。指到 / 或其它流水线目录，
    就会把宿主机文件打进镜像层。
    """
    root = workspace.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise BuildError(
            f"{what}「{path}」不在任务工作区 {root} 内。"
            "不要填绝对路径或 .."
        ) from exc
    return resolved


def find_docker() -> str:
    """定位 docker 客户端。找不到时说明怎么装、服务 PATH 为什么看不见。"""
    names = ["docker.exe", "docker"] if os.name == "nt" else ["docker"]
    found = _which(names, _docker_bin_dirs())
    if found:
        return found
    raise BuildError(
        "构建机上找不到 docker 命令。请安装 Docker Engine，并把运行 Agent 的用户加进 docker 组。"
        "若是 snap 安装，二进制在 /snap/bin，需要写进 Agent 服务的 PATH 或重启服务后再试"
    )


def probe_docker(base: list[str]) -> None:
    """确认客户端能连上引擎。命令在、sock 没权限、引擎没启动，三种失败要分开说。"""
    code, out = sdk.capture(base + ["info"], timeout=20)
    if code == 0:
        return
    text = (out or "").lower()
    if "permission denied" in text:
        raise BuildError(
            "找到了 docker 命令，但无权访问 docker.sock。"
            "请把运行 Agent 的用户加进 docker 组，然后重启 Agent 服务（只重新登录不够）"
        )
    if "cannot connect" in text or "is the docker daemon running" in text:
        raise BuildError("找到了 docker 命令，但 Docker 引擎没有在跑。请在这台机器上启动 docker 服务")
    detail = (out or "").strip()[:300] or f"退出码 {code}"
    raise BuildError(f"docker 无法使用：{detail}")


def _login(docker: str, image: str, user: str, password: str) -> None:
    """只登录镜像所属的那个仓库，不动构建机上其它仓库的登录态。"""
    registry = image.split("/")[0]
    if "." not in registry and ":" not in registry:
        registry = ""  # 没写域名就是 Docker Hub
    cmd = [docker, "login"]
    if registry:
        cmd.append(registry)
    cmd += ["-u", user, "--password-stdin"]
    code = sdk.stream(cmd, stdin_text=password + "\n", mask=[password])
    if code != 0:
        raise BuildError(f"登录镜像仓库失败（{registry or 'docker.io'}），请检查用户名密码")


def main() -> int:
    """读步骤参数，定位 docker，构建并按需推送镜像。"""
    inp = sdk.get_input()
    workspace = Path(sdk.get_workspace())
    src = workspace / "src"
    if not src.is_dir():
        src = workspace

    image = str(inp.get("image") or "").strip().rstrip(":")
    if not image:
        raise BuildError("必须填写镜像名")
    tag = str(inp.get("tag") or "").strip() or sdk.get_pipeline_build_id() or "latest"
    if "${{" in tag or "${" in tag:
        raise BuildError(
            f"镜像 tag 里的变量没有被替换（{tag}）。手动执行时请在参数面板里填一个具体的 tag"
        )
    full = f"{image}:{tag}"

    context = Path(str(inp.get("context") or ".").strip() or ".")
    context = context if context.is_absolute() else src / context
    context = _under_workspace(workspace, context, what="构建上下文")
    if not context.is_dir():
        raise BuildError(f"构建上下文目录不存在：{context}")
    dockerfile = Path(str(inp.get("dockerfile") or "Dockerfile").strip())
    dockerfile = dockerfile if dockerfile.is_absolute() else context / dockerfile
    dockerfile = _under_workspace(workspace, dockerfile, what="Dockerfile")
    if not dockerfile.is_file():
        raise BuildError(f"找不到 Dockerfile：{dockerfile}")

    docker = find_docker()
    probe_docker([docker])
    sdk.log.info(f"镜像：{full}")
    sdk.log.info(f"上下文：{context} | Dockerfile：{dockerfile}")

    extra_tags = [t.strip() for t in str(inp.get("extraTags") or "").split(",") if t.strip()]

    cmd = [docker, "build", "-f", str(dockerfile), "-t", full]
    for t in extra_tags:
        cmd += ["-t", f"{image}:{t}"]
    for kv in _lines(inp.get("buildArgs")):
        cmd += ["--build-arg", kv]
    extra = str(inp.get("extraArgs") or "").strip()
    if extra:
        cmd += _assert_safe_extra_args(extra)
    cmd.append(str(context))

    if sdk.stream(cmd, cwd=src) != 0:
        sdk.log.error("镜像构建失败")
        return 1

    if _truthy(inp.get("push", True)):
        user = str(inp.get("registryUser") or "").strip()
        password = str(inp.get("registryPassword") or "")
        if user and password:
            _login(docker, image, user, password)
        for t in [tag] + extra_tags:
            if sdk.stream([docker, "push", f"{image}:{t}"]) != 0:
                sdk.log.error(f"推送 {image}:{t} 失败")
                return 1
        sdk.log.info(f"已推送：{full}")
    else:
        sdk.log.warning("未推送镜像。部署到其它机器时那边会拉不到这个镜像")

    sdk.set_output({
        "docker_image": {"type": "string", "value": full},
        "docker_tag": {"type": "string", "value": tag},
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        sdk.log.error(str(e))
        sys.exit(1)
