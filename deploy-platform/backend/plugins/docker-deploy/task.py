# -*- coding: utf-8 -*-
"""拉镜像、重建容器，并登记「上一个镜像是什么」供一键回滚。

容器发布的回滚不需要重新构建：上一个版本的镜像还在仓库里，用它重新起一个容器就行。
所以部署前先把当前容器跑的镜像记下来，那就是回滚的目标。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402


class DeployError(Exception):
    pass


# 这些入参是凭证，不能进部署记录。记录的 payload 是明文存进平台数据库的，
# 而且回滚时会原样回填成任务参数——带上它就等于把仓库密码写进库表，
# task.json 里这个字段标的是 type: password，本来就不该落地。
# 回滚也不需要它：原始部署已经在这台机器上 docker login 过，凭证在
# ~/.docker/config.json 里，拉旧镜像照样能过。
_SECRET_INPUT_KEYS = ("registryPassword", "registryUser")

# extraArgs 只放这些：其余一律拒绝，避免 --privileged / --pid=host 从这里进来。
_ALLOWED_EXTRA_FLAGS = {
    "--label", "-l",
    "--memory", "-m",
    "--memory-swap",
    "--cpus",
    "--cpu-shares",
    "--ulimit",
    "--stop-timeout",
    "--log-driver",
    "--log-opt",
    "--health-cmd",
    "--health-interval",
    "--health-retries",
    "--health-timeout",
    "--health-start-period",
    "--workdir", "-w",
    "--user", "-u",
    "--hostname",
    "--add-host",
    "--dns",
}

# 这些即使用户写进 extraArgs 也直接拒绝。
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
    "--publish-all",
    "-P",
}


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
    """解析 extraArgs，只放行允许列表，危险参数直接失败。"""
    text = (extra or "").strip()
    if not text:
        return []
    try:
        args = shlex.split(text)
    except ValueError as e:
        raise DeployError(f"额外参数无法解析（{e}），请检查引号是否配对：{extra}") from e
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            raise DeployError(f"额外参数里出现了非选项「{arg}」")
        name = _flag_name(arg)
        value, nxt = _parse_flag_value(args, i)
        if name in _DENIED_FLAGS or name == "--privileged":
            raise DeployError(f"拒绝危险的 docker 参数：{name}")
        if name not in _ALLOWED_EXTRA_FLAGS:
            raise DeployError(f"不允许的额外参数 {name}（仅允许 {sorted(_ALLOWED_EXTRA_FLAGS)}）")
        out.extend(args[i:nxt])
        i = nxt
        _ = value
    return out


def _volume_host_path(spec: str) -> str:
    """取出 volume 规格里的宿主机路径。Windows 盘符 C:\\foo:C:\\bar 按第二个冒号切。"""
    text = (spec or "").strip()
    if re.match(r"^[A-Za-z]:[\\/]", text):
        m = re.match(r"^([A-Za-z]:[\\/][^:]*)", text)
        return m.group(1) if m else text
    return text.split(":", 1)[0]


def _assert_safe_volume(spec: str) -> None:
    """拒绝 docker.sock、宿主机根目录，以及会把系统目录挂进容器的路径。"""
    text = (spec or "").strip()
    if not text:
        return
    if "docker.sock" in text.lower():
        raise DeployError("禁止挂载 docker.sock")
    host = _volume_host_path(text).replace("\\", "/").rstrip("/")
    if host in {"", "/", "C:", "c:", "C:/", "c:/"}:
        raise DeployError("禁止把宿主机根目录挂进容器")
    lowered = host.lower()
    denied_prefixes = (
        "/root",
        "/etc/ssh",
        "/etc/shadow",
        "/proc",
        "/sys",
        "/dev",
        "c:/windows/system32",
        "c:/windows/syswow64",
        "c:/boot",
    )
    for prefix in denied_prefixes:
        if lowered == prefix or lowered.startswith(prefix + "/"):
            raise DeployError(f"禁止把宿主机系统目录挂进容器：{host}")
    if "/.ssh/" in lowered + "/" or lowered.endswith("/.ssh"):
        raise DeployError("禁止把 SSH 私钥目录挂进容器")


def _assert_safe_network(network: str) -> None:
    """容器网络不能直接用 host，否则绕过端口与隔离。"""
    name = (network or "").strip().lower()
    if name in {"host", "container:host"}:
        raise DeployError("禁止使用 --network=host")


def _undo_with(inp: dict, previous: str) -> dict:
    """回滚时原样回填的参数：去掉内部字段和凭证，并把镜像换成上一个版本。"""
    out = {
        k: v
        for k, v in inp.items()
        if not str(k).startswith("_") and k not in _SECRET_INPUT_KEYS
    }
    out["image"] = previous
    return out


def _int_input(inp: dict, key: str, default: int, maximum: int) -> int:
    """读一个整数入参，读不出来就用默认值，并且封顶。

    封顶是因为这些值都会变成「等下去」的时长：填个 999999 就能让这台构建机的
    执行槽被占上十几天，那台机器等于从队列里消失了。
    """
    raw = inp.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        sdk.log.warning(f"{key} 的值「{raw}」不是整数，按默认值 {default} 处理")
        return default
    if value < 0:
        return 0
    if value > maximum:
        sdk.log.warning(f"{key}={value} 过大，按上限 {maximum} 处理")
        return maximum
    return value


def _lines(raw) -> list[str]:
    """多行文本按行拆，忽略空行和 # 注释。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _docker_bin_dirs() -> list[Path]:
    """本机 docker 客户端可能在的目录。snap / Docker Desktop 不在 systemd 默认 PATH 里。"""
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
            if not candidate.is_file():
                continue
            if os.name == "nt" or os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def find_docker() -> str:
    """定位 docker 客户端。远程部署（-H）也需要本机有这份 CLI。"""
    names = ["docker.exe", "docker"] if os.name == "nt" else ["docker"]
    found = _which(names, _docker_bin_dirs())
    if found:
        return found
    raise DeployError(
        "找不到 docker 命令。请在构建机上安装 Docker Engine。"
        "snap 安装的二进制在 /snap/bin，需要写进 Agent 服务的 PATH"
    )


def probe_docker(base: list[str]) -> None:
    """确认这次要用的引擎能连上。本机 sock 没权限、远程 Docker 没起来，报错要分开。"""
    code, out = sdk.capture(base + ["info"], timeout=20)
    if code == 0:
        return
    text = (out or "").lower()
    if "permission denied" in text:
        raise DeployError(
            "找到了 docker 命令，但无权访问 docker.sock。"
            "请把运行 Agent 的用户加进 docker 组，然后重启 Agent 服务"
        )
    if "cannot connect" in text or "is the docker daemon running" in text:
        raise DeployError("找到了 docker 命令，但目标 Docker 引擎没有在跑")
    detail = (out or "").strip()[:300] or f"退出码 {code}"
    raise DeployError(f"docker 无法使用：{detail}")


def _docker(host: str) -> list[str]:
    """拼好 docker 命令前缀（可选 -H），并确认能连上引擎。"""
    found = find_docker()
    base = [found, "-H", host] if host else [found]
    probe_docker(base)
    return base


def _current_image(base: list[str], name: str) -> str:
    """容器当前跑的镜像。没有这个容器就返回空串（首次部署）。"""
    code, out = sdk.capture(base + ["inspect", "-f", "{{.Config.Image}}", name], timeout=60)
    if code != 0:
        return ""
    return out.strip().splitlines()[-1].strip() if out.strip() else ""


def _container_state(base: list[str], name: str) -> str:
    code, out = sdk.capture(base + ["inspect", "-f", "{{.State.Status}}", name], timeout=60)
    return out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""


def _login(base: list[str], image: str, user: str, password: str) -> None:
    registry = image.split("/")[0]
    if "." not in registry and ":" not in registry:
        registry = ""
    cmd = base + ["login"]
    if registry:
        cmd.append(registry)
    cmd += ["-u", user, "--password-stdin"]
    if sdk.stream(cmd, stdin_text=password + "\n", mask=[password]) != 0:
        raise DeployError(f"登录镜像仓库失败（{registry or 'docker.io'}）")


def _run_container(base: list[str], inp: dict, image: str, name: str) -> None:
    cmd = base + ["run", "-d", "--name", name]
    restart = str(inp.get("restart") or "always").strip()
    if restart and restart != "no":
        cmd += ["--restart", restart]
    for p in _lines(inp.get("ports")):
        cmd += ["-p", p]
    for e in _lines(inp.get("envs")):
        cmd += ["-e", e]
    for v in _lines(inp.get("volumes")):
        _assert_safe_volume(v)
        cmd += ["-v", v]
    network = str(inp.get("network") or "").strip()
    if network:
        _assert_safe_network(network)
        cmd += ["--network", network]
    extra = str(inp.get("extraArgs") or "").strip()
    if extra:
        cmd += _assert_safe_extra_args(extra)
    cmd.append(image)
    if sdk.stream(cmd) != 0:
        raise DeployError("启动容器失败")


def _wait_healthy(base: list[str], name: str, seconds: int) -> bool:
    """观察一段时间，确认容器没有起来就退。

    容器 run 成功只代表进程拉起来了，配置错误导致的秒退要等几秒才看得出来。
    这里不判失败就直接返回成功，回滚的时机会被推迟到用户发现故障之后。
    """
    if seconds <= 0:
        return True
    sdk.log.info(f"观察 {seconds} 秒确认容器稳定运行 ...")
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = _container_state(base, name)
        if state in ("exited", "dead"):
            sdk.log.error(f"容器已退出（状态 {state}），部署失败。最后 50 行日志：")
            sdk.stream(base + ["logs", "--tail", "50", name], echo_command=False)
            return False
        time.sleep(2)
    return True


def main() -> int:
    inp = sdk.get_input()
    image = str(inp.get("image") or "").strip()
    name = str(inp.get("containerName") or "").strip()
    if not image or not name:
        raise DeployError("必须填写镜像和容器名")
    if "${{" in image or "${" in image:
        raise DeployError(f"镜像地址里的变量没有被替换（{image}），请检查流水线变量或在参数面板里填具体值")

    host = str(inp.get("dockerHost") or "").strip()
    base = _docker(host)
    where = host or "构建机本机"
    sdk.log.info(f"目标：{where} | 容器：{name} | 镜像：{image}")

    user = str(inp.get("registryUser") or "").strip()
    password = str(inp.get("registryPassword") or "")
    if user and password:
        _login(base, image, user, password)

    # 先记下当前镜像：拉新镜像、删旧容器之后就问不到了
    previous = _current_image(base, name)
    if previous:
        sdk.log.info(f"当前容器跑的是：{previous}")
    else:
        sdk.log.info("目标上还没有同名容器，本次是首次部署")

    if sdk.stream(base + ["pull", image]) != 0:
        raise DeployError("拉取镜像失败，请确认镜像已推送、目标机器能访问仓库")

    record_id = inp.get("_deployment_record_id")

    # 回滚点必须在删旧容器之前登记。旧容器一删，之后任何一步失败线上就没有能服务的
    # 容器了，而那恰恰是最需要一键回滚的时刻——把登记放在函数末尾，等于把回滚凭据
    # 写在「只有一切顺利才会走到」的分支里，最需要它的时候一定是空的。
    # 平台按 (发布, 任务, 步骤) 去重覆盖，提前登记不会留下重复记录。
    if not record_id:
        if previous:
            sdk.report_deployment(
                kind=sdk.KIND_DOCKER,
                target=f"{where}/{name}",
                payload={
                    "undo_with": _undo_with(inp, previous),
                    "previous_image": previous,
                    "current_image": image,
                },
                summary=f"{previous} → {image}",
            )
        else:
            sdk.log.warning(
                "目标上原本没有这个容器，无法登记回滚点。"
                "如需回滚，只能删掉容器或手动指定一个历史镜像重新部署"
            )

    if _container_state(base, name):
        sdk.log.info(f"停止并删除旧容器 {name}")
        if sdk.stream(base + ["rm", "-f", name]) != 0:
            # 删不掉就别往下走：接着 run 会因为「名字已被占用」失败，报出来的
            # 是个不相干的错。中止在这里，旧容器还在跑，线上没受影响
            raise DeployError(f"删除旧容器 {name} 失败，已中止（旧容器仍在运行，线上未受影响）")

    _run_container(base, inp, image, name)
    if not _wait_healthy(base, name, _int_input(inp, "healthTimeout", 0, 3600)):
        sdk.log.error(
            f"新容器没能稳定运行。可在平台上对本次发布执行回滚，回到 {previous or '上一个版本'}"
        )
        return 1

    sdk.log.info(f"容器 {name} 已运行新版本")

    # 本次执行本身就是一次回滚，跑完销账，免得同一条记录被撤销两次
    if record_id:
        sdk.report_undone(int(record_id))

    sdk.set_output({
        "deployed_image": {"type": "string", "value": image},
        "previous_image": {"type": "string", "value": previous},
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeployError as e:
        sdk.log.error(str(e))
        sys.exit(1)
