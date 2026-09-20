# -*- coding: utf-8 -*-
"""从构建机 scp 到远程 Linux，给不能装节点 Agent 的静态站点用。

目标机只开 SSH。没有节点侧备份和一键回滚；能装节点时请用 file-transfer。
命令一律 list 传参，不走 shell，路径和主机名先校验再拼进 scp。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402


class DeployError(Exception):
    """参数不合法或本机缺少 ssh/scp。"""


# 主机名或 IPv4，禁止空格和命令分隔符。
_HOST = re.compile(
    r"^(?:(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*|(?:\d{1,3}\.){3}\d{1,3})$"
)
# SSH 用户名。
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
# 远程绝对路径，只允许常见路径字符。
_REMOTE_DIR = re.compile(r"^/[A-Za-z0-9._/-]+$")


def _truthy(v) -> bool:
    """表单开关在 JSON 里可能是 bool 或字符串。"""
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _src_dir() -> Path:
    """代码目录：工作空间下的 src/，没有则退回工作空间根。"""
    workspace = Path(sdk.get_workspace())
    src = workspace / "src"
    return src if src.is_dir() else workspace


def validate_host(raw: str) -> str:
    """校验目标主机，拒绝能拆命令的字符。"""
    host = (raw or "").strip()
    if not host or not _HOST.fullmatch(host):
        raise DeployError(f"目标主机不合法「{raw}」。请填 IP 或主机名")
    return host


def validate_user(raw: str) -> str:
    """校验 SSH 用户名。"""
    user = (raw or "").strip()
    if not user or not _USER.fullmatch(user):
        raise DeployError(f"SSH 用户不合法「{raw}」")
    return user


def validate_port(raw) -> int:
    """SSH 端口，默认 22。"""
    try:
        port = int(raw if raw not in (None, "") else 22)
    except (TypeError, ValueError) as exc:
        raise DeployError("SSH 端口必须是数字") from exc
    if port < 1 or port > 65535:
        raise DeployError(f"SSH 端口超出范围：{port}")
    return port


def validate_remote_dir(raw: str) -> str:
    """远程目录必须是 Linux 绝对路径，不能带 .. 或 shell 元字符。"""
    text = (raw or "").strip().rstrip("/")
    if not text or not _REMOTE_DIR.fullmatch(text):
        raise DeployError(f"远程目录不合法「{raw}」。请填如 /data/nginx/html/aicoach-web")
    if ".." in text or ".." in Path(text).parts:
        raise DeployError("远程目录不能包含 ..")
    return text


def resolve_source(src_root: Path, rel: str) -> Path:
    """本地目录必须落在代码目录内。"""
    text = (rel or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or Path(text).is_absolute() or ".." in Path(text).parts:
        raise DeployError(f"本地目录「{rel}」必须是相对代码目录的路径，例如 dist")
    root = src_root.resolve()
    path = (src_root / text).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DeployError(f"本地目录「{rel}」不在代码目录 {root} 内") from exc
    if not path.exists():
        raise DeployError(f"本地目录不存在：{path}。请先跑 npm 构建，或检查 sourcePath")
    return path


def _which(names: list[str]) -> str | None:
    """在 PATH 里找 ssh/scp。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _ssh_opts(*, key_file: str | None, password: bool) -> list[str]:
    """ssh/scp 共用的安全选项。密钥登录才开 BatchMode，否则密码会被直接拒绝。"""
    known_hosts = "NUL" if os.name == "nt" else "/dev/null"
    opts = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"GlobalKnownHostsFile={known_hosts}",
        "-o",
        "IdentitiesOnly=yes",
    ]
    if not password:
        opts.extend(["-o", "BatchMode=yes"])
    if key_file:
        opts.extend(["-i", key_file])
    return opts


def _wrap_sshpass(cmd: list[str], password: str) -> tuple[list[str], dict]:
    """账号密码登录：用 sshpass 把密码放进环境变量，不写进 argv。"""
    sshpass = _which(["sshpass", "sshpass.exe"])
    if not sshpass:
        raise DeployError(
            "账号密码登录需要构建机安装 sshpass。更稳妥的做法是在凭证管理里改用 SSH 私钥"
        )
    env = os.environ.copy()
    env["SSHPASS"] = password
    return [sshpass, "-e", *cmd], env


def _write_key(text: str) -> str:
    """私钥写到仅当前用户可读的临时文件，scp 用完删除。"""
    fd, name = tempfile.mkstemp(prefix="rp-ssh-", suffix=".key")
    os.close(fd)
    path = Path(name)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return str(path)


def _fail(msg: str) -> int:
    """失败出口。"""
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
    """读步骤参数，mkdir 远程目录，再 scp 覆盖。"""
    inp = sdk.get_input()
    try:
        host = validate_host(str(inp.get("host") or ""))
        port = validate_port(inp.get("port"))
        user = (str(inp.get("user") or "").strip() or str(inp.get("sshUsername") or "").strip())
        user = validate_user(user)
        remote = validate_remote_dir(str(inp.get("targetDir") or ""))
        source = resolve_source(_src_dir(), str(inp.get("sourcePath") or "dist"))
    except DeployError as exc:
        return _fail(str(exc))

    key_text = str(inp.get("sshPrivateKey") or "").strip()
    password = str(inp.get("sshPassword") or "")
    if not key_text and not password:
        return _fail("没有登录凭证。请在步骤里选择 SSH 私钥或账号密码")

    ssh = _which(["ssh", "ssh.exe"])
    scp = _which(["scp", "scp.exe"])
    if not ssh or not scp:
        return _fail("构建机上找不到 ssh/scp。请安装 OpenSSH 客户端，并保证 Agent 进程能看到它")

    dry_run = _truthy(inp.get("dryRun"))
    dest = f"{user}@{host}:{remote}/"
    secrets = [x for x in (password, key_text) if x]

    key_file = None
    try:
        if key_text:
            key_file = _write_key(key_text)
        ssh_base = [ssh, "-p", str(port), *_ssh_opts(key_file=key_file, password=bool(password))]
        scp_base = [scp, "-P", str(port), *_ssh_opts(key_file=key_file, password=bool(password)), "-r"]

        mkdir_cmd = ssh_base + [f"{user}@{host}", "mkdir", "-p", "--", remote]
        mkdir_env = None
        if password:
            mkdir_cmd, mkdir_env = _wrap_sshpass(mkdir_cmd, password)

        payload = source / "." if source.is_dir() else source
        scp_cmd = scp_base + [str(payload), dest]
        scp_env = None
        if password:
            scp_cmd, scp_env = _wrap_sshpass(scp_cmd, password)

        sdk.log.info(f"目标 {user}@{host}:{port} → {remote}")
        sdk.log.info(f"本地 {source}")
        if dry_run:
            sdk.log.info("预演：不传输。将发送目录内容到远程目录（覆盖同名文件，不删多余文件）")
            if source.is_dir():
                for child in sorted(source.iterdir()):
                    sdk.log.info(f"  {child.name}{'/' if child.is_dir() else ''}")
            else:
                sdk.log.info(f"  {source.name}")
            sdk.set_output(
                {
                    "status": sdk.status.SUCCESS,
                    "message": "dry-run",
                    "type": sdk.output_template_type.DEFAULT,
                    "data": {"host": host, "targetDir": remote},
                }
            )
            return 0

        if sdk.stream(mkdir_cmd, env=mkdir_env, mask=secrets) != 0:
            return _fail("远程创建目录失败。请确认账号有写权限、防火墙放行 SSH")
        if sdk.stream(scp_cmd, env=scp_env, mask=secrets) != 0:
            return _fail("scp 传输失败。请看上方 ssh/scp 日志")
    except DeployError as exc:
        return _fail(str(exc))
    finally:
        if key_file:
            Path(key_file).unlink(missing_ok=True)

    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": "ssh-deploy ok",
            "type": sdk.output_template_type.DEFAULT,
            "data": {"host": host, "targetDir": remote},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
