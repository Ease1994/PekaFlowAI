# -*- coding: utf-8 -*-
"""用工具链镜像在容器里编译，产物通过绑定挂载写回工作区。

和 docker-build 的分工：那个插件是 `docker build` 打业务镜像；本插件是
`docker run --rm -v 代码:/code 基础镜像 <编译命令>`。
Node、Maven、Gradle、Go、Python、.NET 都走这一套：换镜像就换工具链，
不要改构建机本机 JDK / Node。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402


class CompileError(Exception):
    """步骤参数不合法或本机 Docker 不可用。"""


# 命名卷名：maven_cache、npm_cache。带斜杠的一律当宿主机路径拒绝。
_VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# 容器内绝对路径，禁止空格和冒号，避免再拆出一层挂载。
_CONTAINER_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
# 镜像名含仓库域名和 tag，例如 registry/base/maven:3.8-jdk8。
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?$")
# 容器里用来执行编译命令的 shell，不允许写成 /proc 路径。
_SHELLS = frozenset({"sh", "bash", "ash"})
# 执行方式：shell 覆盖镜像入口；direct 沿用镜像 ENTRYPOINT。
_INVOKES = frozenset({"shell", "direct"})
# 工具链只决定缓存卷留空时的预置，不改编译命令。
_TOOLCHAINS = frozenset({"custom", "node", "maven", "gradle", "go", "python", "dotnet"})

# 缓存卷留空时按工具链挂对应依赖目录，避免每次都从中央仓库拉一遍。
_TOOLCHAIN_CACHES: dict[str, list[str]] = {
    "custom": [],
    "node": ["npm_cache:/root/.npm"],
    "maven": ["maven_cache:/root/.m2"],
    "gradle": ["gradle_cache:/root/.gradle"],
    "go": ["go_mod_cache:/go/pkg/mod", "go_build_cache:/root/.cache/go-build"],
    "python": ["pip_cache:/root/.cache/pip"],
    "dotnet": ["nuget_cache:/root/.nuget/packages"],
}

# extraArgs 没写资源上限时补上，避免一次 Maven 把构建机内存和 CPU 打光。
_DEFAULT_COMPILE_MEMORY = "4g"
_DEFAULT_COMPILE_CPUS = "2"
_ALLOWED_EXTRA_FLAGS = {
    "--memory",
    "-m",
    "--memory-swap",
    "--cpus",
    "--cpu-shares",
    "--user",
    "-u",
    "--ulimit",
    "--label",
    "-l",
    "--add-host",
    "--dns",
    "--tmpfs",
}

_DENIED_FLAGS = {
    "--privileged",
    "--pid",
    "--network",
    "--net",
    "--userns",
    "--ipc",
    "--cgroupns",
    "--cap-add",
    "--cap-drop",
    "--security-opt",
    "--device",
    "--mount",
    "--volume",
    "-v",
    "--entrypoint",
    "--publish",
    "-p",
    "--publish-all",
    "-P",
}

# 文件挂载禁止指向的宿主机前缀。settings.xml 可以在 /data/soft 或 /root/.m2。
_DENIED_HOST_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/etc/ssh",
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
    "/root/.ssh",
    "c:/windows/system32",
    "c:/windows/syswow64",
)


def _flag_name(arg: str) -> str:
    """取出 --foo=bar 里的 --foo。"""
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[0]
    return arg


def _parse_flag_value(args: list[str], index: int) -> tuple[str | None, int]:
    """读当前选项的值，返回 (value, 下一个下标)。无值选项 value 为 None。"""
    arg = args[index]
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[1], index + 1
    nxt = index + 1
    if nxt < len(args) and not args[nxt].startswith("-"):
        return args[nxt], nxt + 1
    return None, nxt


def _assert_safe_extra_args(extra: str) -> list[str]:
    """解析 extraArgs：只放行资源类选项，-v / --privileged / --network 直接失败。"""
    text = (extra or "").strip()
    if not text:
        return []
    try:
        args = shlex.split(text)
    except ValueError as e:
        raise CompileError(f"额外参数无法解析（{e}），请检查引号是否配对：{extra}") from e
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            raise CompileError(f"额外参数里出现了非选项「{arg}」")
        name = _flag_name(arg)
        value, nxt = _parse_flag_value(args, i)
        joined = " ".join(args[i:nxt]).lower()
        if "docker.sock" in joined:
            raise CompileError("禁止挂载 docker.sock")
        if name in _DENIED_FLAGS:
            raise CompileError(f"拒绝危险的 docker 参数：{name}。挂载请用「缓存卷 / 文件挂载」，代码目录由插件自动挂")
        if name not in _ALLOWED_EXTRA_FLAGS:
            raise CompileError(f"不允许的额外参数 {name}")
        out.extend(args[i:nxt])
        i = nxt
        _ = value
    return out


def _with_default_resource_limits(extra: list[str]) -> list[str]:
    """没写 --memory / --cpus 时补上默认上限。

    构建机本来就要跑编译，但不能让一次 `docker run` 把宿主机吃光。
    步骤里已经写了对应选项则尊重用户，不再重复追加。
    """
    names = {_flag_name(a) for a in extra}
    out = list(extra)
    if "--memory" not in names and "-m" not in names:
        out.extend(["--memory", _DEFAULT_COMPILE_MEMORY])
    if "--cpus" not in names:
        out.extend(["--cpus", _DEFAULT_COMPILE_CPUS])
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


def _assert_image(image: str) -> str:
    """校验基础镜像地址。变量没替换、空 tag、命令注入字符都直接失败。"""
    text = (image or "").strip()
    if not text:
        raise CompileError("必须填写基础镜像")
    if "${{" in text or "${" in text:
        raise CompileError(f"镜像名里的变量没有被替换（{text}）。请填完整地址，含 tag")
    if not _IMAGE.fullmatch(text) or text.endswith(":"):
        raise CompileError(
            f"非法的镜像名「{text}」。请填含 tag 的完整地址，例如 "
            "maven:3.8-jdk8 或 node:18-alpine"
        )
    return text


def _assert_command(command: str) -> str:
    """编译命令不能为空。Shell 模式原样交给 sh -c；直接传参再按参数列表拆。"""
    text = str(command or "").strip()
    if not text:
        raise CompileError("必须填写编译命令，例如 mvn -B package 或 npm i && npm run build")
    if "${{" in text:
        raise CompileError(f"编译命令里的变量没有被替换（{text}）")
    return text


def _assert_shell(shell: str) -> str:
    """容器入口只允许常见 shell 名，避免写成宿主机路径。"""
    name = (shell or "sh").strip() or "sh"
    if name not in _SHELLS:
        raise CompileError(f"不支持的 Shell「{name}」，请选 sh / bash / ash")
    return name


def _assert_invoke(invoke: str) -> str:
    """执行方式：shell 覆盖镜像入口，direct 沿用镜像 ENTRYPOINT。"""
    name = (invoke or "shell").strip() or "shell"
    if name not in _INVOKES:
        raise CompileError(f"不支持的执行方式「{name}」，请选 shell 或 direct")
    return name


def _assert_toolchain(toolchain: str) -> str:
    """工具链枚举。未知值不能悄悄当 custom，否则缓存会挂错目录。"""
    name = (toolchain or "custom").strip() or "custom"
    if name not in _TOOLCHAINS:
        raise CompileError(f"不支持的工具链「{name}」")
    return name


def _assert_container_path(path: str, *, what: str) -> str:
    """容器内路径必须是 Unix 绝对路径。"""
    text = (path or "").strip() or "/code"
    if not _CONTAINER_PATH.fullmatch(text) or text == "/":
        raise CompileError(f"{what}「{path}」不是合法的容器内路径，请写成 /code 这种形式")
    if "docker.sock" in text.lower():
        raise CompileError("禁止把 docker.sock 当作容器路径")
    return text


def _assert_named_volume(spec: str) -> str:
    """缓存卷只允许「卷名:容器路径」，例如 maven_cache:/root/.m2。

    代码目录由插件自己挂，这里再接受宿主机路径就会把 /etc、密钥目录挂进编译容器。
    """
    text = (spec or "").strip()
    if not text:
        raise CompileError("缓存卷不能为空行")
    if "docker.sock" in text.lower():
        raise CompileError("禁止挂载 docker.sock")
    parts = text.split(":")
    if len(parts) < 2:
        raise CompileError(f"缓存卷「{text}」应为 卷名:容器路径，例如 maven_cache:/root/.m2")
    name = parts[0]
    container = parts[1]
    mode = parts[2] if len(parts) == 3 else ""
    if len(parts) > 3:
        raise CompileError(f"缓存卷「{text}」格式无法识别")
    if not _VOLUME_NAME.fullmatch(name):
        raise CompileError(
            f"缓存卷「{text}」只能用 Docker 命名卷（如 maven_cache:/root/.m2），"
            "不要写宿主机路径。代码目录由插件按「代码子目录」自动挂载"
        )
    container = _assert_container_path(container, what="缓存卷容器路径")
    if mode and mode not in {"ro", "rw"}:
        raise CompileError(f"缓存卷模式只允许 ro / rw，收到「{mode}」")
    return f"{name}:{container}" + (f":{mode}" if mode else "")


def resolve_cache_volumes(toolchain: str, raw) -> list[str]:
    """缓存卷：步骤里写了就用用户的；留空才按工具链预置。"""
    name = _assert_toolchain(toolchain)
    written = _lines(raw)
    specs = written if written else list(_TOOLCHAIN_CACHES.get(name) or [])
    return [_assert_named_volume(spec) for spec in specs]


def _assert_env_line(line: str) -> str:
    """环境变量必须是 KEY=VALUE。KEY 不允许嵌入空格。"""
    text = (line or "").strip()
    if "=" not in text:
        raise CompileError(f"环境变量「{text}」应为 KEY=VALUE")
    key, value = text.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise CompileError(f"非法的环境变量名「{key}」")
    return f"{key}={value}"


def _under_workspace(workspace: Path, path: Path, *, what: str) -> Path:
    """代码目录必须落在本任务工作区内，不能用 .. 指到别的流水线或宿主机。"""
    root = workspace.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise CompileError(
            f"{what}「{path}」不在任务工作区 {root} 内。不要填绝对路径或 .."
        ) from exc
    return resolved


def _looks_absolute(text: str, path: Path) -> bool:
    """判断是不是构建机上的绝对路径。不以当前进程所在 OS 为准。"""
    if path.is_absolute():
        return True
    if text.startswith("/") or text.startswith("\\\\"):
        return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def _split_file_bind(spec: str) -> tuple[str, str, str]:
    """拆「宿主机文件:容器路径[:ro|rw]」。容器路径必须是 / 开头的 Unix 路径。"""
    text = (spec or "").strip()
    matched = re.match(r"^(.+):(/[^:]+)(?::(ro|rw))?$", text)
    if not matched:
        raise CompileError(
            f"文件挂载「{text}」应为 宿主机文件:容器路径，"
            "例如 /data/soft/maven/conf/settings.xml:/root/.m2/settings.xml"
        )
    return matched.group(1), matched.group(2), matched.group(3) or ""


def _assert_host_file_allowed(host: str) -> None:
    """拒绝 docker.sock、系统目录和 SSH 私钥，其余已存在的文件可以挂。"""
    if "docker.sock" in host.lower():
        raise CompileError("禁止挂载 docker.sock")
    normalized = host.replace("\\", "/").rstrip("/").lower()
    if normalized in {"", "/", "c:", "c:/"}:
        raise CompileError("禁止把宿主机根目录挂进容器")
    for prefix in _DENIED_HOST_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            raise CompileError(f"禁止把宿主机系统目录挂进容器：{host}")
    if "/.ssh/" in normalized + "/" or normalized.endswith("/.ssh"):
        raise CompileError("禁止把 SSH 私钥目录挂进容器")


def resolve_file_bind(workspace: Path, src: Path, spec: str) -> str:
    """把一行文件挂载收成 docker -v 规格。

    相对路径锁在工作区，给仓库里不带密码的配置用。
    绝对路径指向构建机上已有的文件，给 Maven settings.xml 这类凭证用。
    """
    host_raw, container, mode = _split_file_bind(spec)
    host_raw = host_raw.strip()
    container = _assert_container_path(container, what="文件挂载容器路径")
    if _VOLUME_NAME.fullmatch(host_raw):
        raise CompileError(f"「{host_raw}」是命名卷，请写到「缓存卷」而不是文件挂载")
    _assert_host_file_allowed(host_raw)
    host = Path(host_raw)
    if _looks_absolute(host_raw, host):
        if ".." in host.parts:
            raise CompileError("文件挂载路径不能包含 ..")
        resolved = host if host.is_absolute() else Path(host_raw)
        if not resolved.is_file():
            raise CompileError(f"找不到要挂载的文件：{host_raw}")
    else:
        if ".." in host.parts:
            raise CompileError("文件挂载相对路径不能包含 ..")
        resolved = _under_workspace(workspace, src / host, what="文件挂载")
        if not resolved.is_file():
            raise CompileError(f"找不到要挂载的文件：{resolved}")
    return _bind_mount(resolved, container) + (f":{mode}" if mode else "")


def _bind_mount(host: Path, container: str) -> str:
    """工作区目录或单文件挂到容器内。Windows 路径改成正斜杠，Docker 才能解析。"""
    return f"{str(host).replace(chr(92), '/')}:{container}"


def _command_argv(command: str) -> list[str]:
    """直接传参：把编译命令拆成 argv，不能含管道和重定向。"""
    try:
        args = shlex.split(command)
    except ValueError as e:
        raise CompileError(f"编译命令无法解析（{e}），请检查引号是否配对") from e
    if not args:
        raise CompileError("编译命令拆完是空的")
    if any(tok in {"|", ";", "&&", "||", ">"} for tok in args):
        raise CompileError("直接传参不能写管道和 &&，请改用 Shell 执行")
    return args


def _docker_bin_dirs() -> list[Path]:
    """本机 docker 客户端可能在的目录。

    官方 apt 包在 /usr/bin。Ubuntu snap、Docker Desktop 不在服务进程 PATH 里，
    shutil.which 会判成没装。
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


def find_docker() -> str:
    """定位 docker 客户端。找不到时说明怎么装、服务 PATH 为什么看不见。"""
    names = ["docker.exe", "docker"] if os.name == "nt" else ["docker"]
    found = _which(names, _docker_bin_dirs())
    if found:
        return found
    raise CompileError(
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
        raise CompileError(
            "找到了 docker 命令，但无权访问 docker.sock。"
            "请把运行 Agent 的用户加进 docker 组，然后重启 Agent 服务（只重新登录不够）"
        )
    if "cannot connect" in text or "is the docker daemon running" in text:
        raise CompileError("找到了 docker 命令，但 Docker 引擎没有在跑。请在这台机器上启动 docker 服务")
    detail = (out or "").strip()[:300] or f"退出码 {code}"
    raise CompileError(f"docker 无法使用：{detail}")


def _login(docker: str, image: str, user: str, password: str) -> None:
    """只登录基础镜像所属的那个仓库，不动构建机上其它仓库的登录态。"""
    registry = image.split("/")[0]
    if "." not in registry and ":" not in registry:
        registry = ""
    cmd = [docker, "login"]
    if registry:
        cmd.append(registry)
    cmd += ["-u", user, "--password-stdin"]
    code = sdk.stream(cmd, stdin_text=password + "\n", mask=[password])
    if code != 0:
        raise CompileError(f"登录镜像仓库失败（{registry or 'docker.io'}），请检查用户名密码")


def build_run_argv(
    *,
    docker: str,
    image: str,
    command: str,
    host_source: Path,
    workdir: str,
    cache_volumes: list[str],
    file_binds: list[str],
    envs: list[str],
    extra_args: list[str],
    shell: str,
    invoke: str,
) -> list[str]:
    """拼出 docker run 参数列表。

    Shell：--entrypoint sh，覆盖官方 Maven 镜像自带的 mvn 入口，才能跑整句脚本。
    直接传参：不改入口，后面跟拆开的 argv，适合「镜像本身就是编译器」的用法。
    """
    argv = [docker, "run", "--rm"]
    if invoke == "shell":
        argv += ["--entrypoint", shell]
    for spec in cache_volumes:
        argv += ["-v", spec]
    for spec in file_binds:
        argv += ["-v", spec]
    argv += ["-v", _bind_mount(host_source, workdir), "-w", workdir]
    for kv in envs:
        argv += ["-e", kv]
    argv += extra_args
    argv.append(image)
    if invoke == "shell":
        argv += ["-c", command]
    else:
        argv += _command_argv(command)
    return argv


def _src_dir(workspace: Path) -> Path:
    """代码目录：工作空间下的 src/，没有则退回工作空间根。"""
    src = workspace / "src"
    return src if src.is_dir() else workspace


def main() -> int:
    """读步骤参数，定位 docker，按所选基础镜像跑编译命令。"""
    inp = sdk.get_input()
    workspace = Path(sdk.get_workspace())
    src = _src_dir(workspace)

    image = _assert_image(str(inp.get("image") or ""))
    command = _assert_command(str(inp.get("command") or ""))
    workdir = _assert_container_path(str(inp.get("workdir") or "/code"), what="容器工作目录")
    shell = _assert_shell(str(inp.get("shell") or "sh"))
    invoke = _assert_invoke(str(inp.get("invoke") or "shell"))
    toolchain = _assert_toolchain(str(inp.get("toolchain") or "custom"))
    cache_volumes = resolve_cache_volumes(toolchain, inp.get("cacheVolumes"))
    file_binds = [resolve_file_bind(workspace, src, ln) for ln in _lines(inp.get("fileBinds"))]
    envs = [_assert_env_line(ln) for ln in _lines(inp.get("envs"))]
    extra_args = _with_default_resource_limits(
        _assert_safe_extra_args(str(inp.get("extraArgs") or ""))
    )

    source = Path(str(inp.get("sourceDir") or ".").strip() or ".")
    source = source if source.is_absolute() else src / source
    source = _under_workspace(workspace, source, what="代码子目录")
    if not source.is_dir():
        raise CompileError(f"代码子目录不存在：{source}")

    docker = find_docker()
    probe_docker([docker])

    argv = build_run_argv(
        docker=docker,
        image=image,
        command=command,
        host_source=source,
        workdir=workdir,
        cache_volumes=cache_volumes,
        file_binds=file_binds,
        envs=envs,
        extra_args=extra_args,
        shell=shell,
        invoke=invoke,
    )
    sdk.log.info(f"工具链：{toolchain} | 基础镜像：{image}")
    sdk.log.info(f"代码目录：{source} → 容器 {workdir}")
    if invoke == "shell":
        sdk.log.info(f"Shell 执行（--entrypoint {shell}）：{command}")
    else:
        sdk.log.info(f"直接传参：{command}")

    user = str(inp.get("registryUser") or "").strip()
    password = str(inp.get("registryPassword") or "")
    if user and password:
        _login(docker, image, user, password)

    if _truthy(inp.get("pull") if "pull" in inp else True):
        if sdk.stream([docker, "pull", image]) != 0:
            sdk.log.error("拉取基础镜像失败")
            return 1

    if sdk.stream(argv) != 0:
        sdk.log.error("容器内编译失败")
        return 1

    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": "docker compile ok",
            "type": sdk.output_template_type.DEFAULT,
            "data": {
                "compile_image": {"type": "string", "value": image},
                "toolchain": {"type": "string", "value": toolchain},
            },
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CompileError as e:
        sdk.log.error(str(e))
        sys.exit(1)
