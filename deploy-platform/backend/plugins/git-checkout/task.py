# -*- coding: utf-8 -*-
"""git-checkout 插件：Agent 仅执行本入口，不内嵌检出逻辑。

git 一律参数列表 + shell=False，ref 走白名单。仓库凭证走本次调用的
git -c http.extraHeader，不写进 clone URL。Windows 关掉 Credential Manager，
也不用 GIT_ASKPASS=echo（会把提示语当密码发给 GitLab）。
浅克隆到工作区 src/，不在用户主目录做 bare 镜像缓存。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import release_atom_sdk as sdk

# 拉取策略（对齐 task.json 的选项）
REVERT_UPDATE = "REVERT_UPDATE"        # fetch + reset --hard + clean，回到干净工作树
FRESH_CHECKOUT = "FRESH_CHECKOUT"      # 清空 src 重新检出
INCREMENT_UPDATE = "INCREMENT_UPDATE"  # fetch + reset，保留未跟踪文件
_STRATEGIES = {REVERT_UPDATE, FRESH_CHECKOUT, INCREMENT_UPDATE}

# 分支/标签：字母数字和 . _ / -；commit：7–40 位 hex。禁止 ..、前导 /、- 开头（会被 git 当成选项）。
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class CheckoutError(Exception):
    """检出前就能确定跑不下去，例如本机没有 git。"""


def _git_bin_dirs() -> list[Path]:
    """本机 git 可能在的目录。

    官方安装器把 git 放进 Program Files\\Git\\cmd，systemd / Windows 服务 PATH
    里经常没有。只 shutil.which 会把「已安装」判成没装。
    """
    home = Path.home()
    dirs = [
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/snap/bin"),
        Path("/opt/homebrew/bin"),
        home / "bin",
        Path(r"C:\Program Files\Git\cmd"),
        Path(r"C:\Program Files\Git\bin"),
        Path(r"C:\Program Files (x86)\Git\cmd"),
        Path(r"C:\Program Files (x86)\Git\bin"),
    ]
    raw = (os.environ.get("GIT_BIN") or "").strip()
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


def find_git() -> str:
    """定位 git 可执行文件。找不到时说明安装位置和服务 PATH 为什么对不上。"""
    names = ["git.exe", "git"] if os.name == "nt" else ["git"]
    found = _which(names, _git_bin_dirs())
    if found:
        return found
    raise CheckoutError(
        "构建机上找不到 git。请安装 Git，并把运行 Agent 的用户 PATH 包含 git 所在目录"
        "（Windows 常见位置是 C:\\Program Files\\Git\\cmd）。"
    )


def _git_auth_header(secret: str, username: str = "") -> str:
    """Git HTTPS 的 Authorization 头。

    GitLab 的 git smart-HTTP 只认 HTTP Basic。Token 类型用户名用 oauth2，
    密码是 PAT；账号密码类型用真实用户名和密码。Bearer 是 REST 用的，
    git-http-backend 会丢掉，构建机又没有 TTY，就会弹出 Windows 凭据窗口。
    """
    user = (username or "").strip() or "oauth2"
    raw = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {raw}"


def _split_secret(raw: str, username: str = "") -> tuple[str, str]:
    """把注入的凭证拆成用户名和密码。

    账号密码类型在平台里存的是 JSON。若整段被当成 repoToken 传来，
    不能再塞进 oauth2: 前缀，否则 GitLab 拒认、Windows 就会弹密码框。
    """
    text = (raw or "").strip()
    user = (username or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("password"):
            return str(data.get("username") or user).strip(), str(data.get("password") or "")
    return user, text


def _git_env() -> dict[str, str]:
    """git 子进程环境：禁止弹窗，禁止交互要密码。

    认证走本次 git -c http.extraHeader。GIT_ASKPASS 不能设成 echo：
    git 会把提示语当密码发给远端。父进程若带了 ASKPASS，这里摘掉。
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    env["GCM_INTERACTIVE"] = "never"
    env["GCM_MODAL_PROMPT"] = "false"
    env["GCM_GUI_PROMPT"] = "false"
    return env


def _git_argv(argv: list[str], token: str = "", username: str = "") -> list[str]:
    """把 argv[0] 换成 git 绝对路径，关掉 helper，并把 Basic 认证写进本次调用。

    GIT_CONFIG_COUNT 要 Git 2.31+ 才认，构建机上的 Git for Windows 经常更老，
    extraHeader 只写在环境里等于没写。-c 从 Git 1.7 就认，必须走这条。
    """
    if not argv or argv[0] != "git":
        return argv
    out = [
        find_git(),
        "-c", "credential.helper=",
        "-c", "credential.interactive=never",
    ]
    if token:
        out.extend(["-c", f"http.extraHeader={_git_auth_header(token, username)}"])
    out.extend(argv[1:])
    return out


def _auth_usernames(username: str) -> list[str]:
    """HTTPS 认证用户名候选。

    平台注入的可能是真实账号，也可能是 oauth2。GitLab HTTPS 拉代码要 PAT，
    用户名用 oauth2；账号+登录密码会被拒。先试注入的用户名，再试 oauth2。
    """
    user = (username or "").strip()
    names: list[str] = []
    if user:
        names.append(user)
    if user != "oauth2":
        names.append("oauth2")
    return names


def _checkout_with_auth(run_once: Callable[[str], bool], username: str) -> bool:
    """按用户名候选依次尝试 HTTPS 认证。

    run_once: 接受用户名，返回这次检出是否成功。
    username: 平台注入的仓库用户名；空则只试 oauth2。
    """
    for i, user in enumerate(_auth_usernames(username)):
        if i:
            sdk.log.warning("当前用户名未通过认证，改用 oauth2（GitLab Token 方式）重试")
        if run_once(user):
            return True
    return False


def _log_auth_fail_hint(token: str) -> None:
    """clone/fetch 失败且已注入凭证时，说明 GitLab HTTPS 要 PAT 而不是登录密码。"""
    if not token:
        return
    sdk.log.error(
        "若日志是 HTTP Basic Access denied / Authentication failed："
        "GitLab HTTPS 拉代码必须用 Personal Access Token，不能用登录密码。"
        "请到「凭证管理」把该仓库凭证改成 Token 类型，密码栏填 PAT（权限含 read_repository）。"
    )


def _valid_ref(ref: str) -> bool:
    """ref 是否允许传给 git。实现：拒绝空、过长、..、选项形态，其余走白名单正则。"""
    text = (ref or "").strip()
    if not text or len(text) > 255:
        return False
    if text.startswith("-") or text.startswith("/") or ".." in text:
        return False
    if _SHA_RE.fullmatch(text):
        return True
    return bool(_REF_RE.fullmatch(text))


def _run(argv: list[str], cwd: Path, sink=True, token: str = "", username: str = "") -> int:
    """执行一条 git 命令。argv 原样交给 subprocess，shell=False。"""
    argv = _git_argv(argv, token=token, username=username)
    p = subprocess.Popen(
        argv,
        shell=False,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_git_env(),
    )
    assert p.stdout is not None
    for raw in iter(p.stdout.readline, b""):
        line = sdk.decode(raw).rstrip("\r\n")
        if sink:
            sdk.log.info(line)
    return p.wait()


def _capture(argv: list[str], cwd: Path, token: str = "", username: str = "") -> str | None:
    """跑一条命令，只取第一行 stdout。失败返回 None。"""
    try:
        out = subprocess.check_output(
            _git_argv(argv, token=token, username=username),
            shell=False,
            cwd=str(cwd),
            env=_git_env(),
            timeout=30,
        )
        text = sdk.decode(out or b"")
        return text.strip().splitlines()[0].strip() if text.strip() else None
    except Exception:
        return None


def _is_commit_sha(ref: str) -> bool:
    return bool(_SHA_RE.fullmatch(ref or ""))


def _mask_url(url: str) -> str:
    return re.sub(r"(://[^:]+:)[^@]+@", r"\1***@", url)


def _rm_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _finish(ok: bool, src: Path) -> bool:
    if not ok:
        return False
    sha = _capture(["git", "rev-parse", "HEAD"], src)
    if sha and _is_commit_sha(sha):
        sdk.log.info(f"source_ref={sha}")
        sdk.set_output(
            {
                "status": sdk.status.SUCCESS,
                "message": "checkout ok",
                "type": sdk.output_template_type.DEFAULT,
                "data": {"source_ref": {"type": sdk.output_field_type.STRING, "value": sha}},
            }
        )
        return True
    sdk.log.warning("未能读取 HEAD commit（Rebuild 可能不可用）")
    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": "checkout ok without source_ref",
            "type": sdk.output_template_type.DEFAULT,
            "data": {},
        }
    )
    return True


def _seq(steps: list[list[str]], cwd: Path, token: str, username: str = "") -> bool:
    """按顺序跑多条命令，任一条非 0 即停。"""
    for argv in steps:
        if _run(argv, cwd, token=token, username=username) != 0:
            return False
    return True


def main() -> int:
    find_git()
    inp = sdk.get_input()
    workspace = Path(sdk.get_workspace())
    # Agent 注入的是 pipeline 工作区根；代码落在 src/
    pipeline_dir = workspace if (workspace / "src").exists() or workspace.name != "src" else workspace.parent
    if (workspace.name == "src") or workspace.joinpath(".git").exists():
        src = workspace
        pipeline_dir = workspace.parent
    else:
        src = pipeline_dir / "src"

    repo_url = (inp.get("repoUrl") or inp.get("repo") or "").strip()
    repo_user, repo_token = _split_secret(inp.get("repoToken") or "", inp.get("repoUser") or "")
    ref = (inp.get("ref") or inp.get("branch") or "master").strip()
    if not _valid_ref(ref):
        sdk.log.error(f"非法的分支/提交「{ref}」，已拒绝执行")
        sdk.set_output(
            {
                "status": sdk.status.FAILURE,
                "message": "ref 不合法：只允许字母数字与 . _ / -，或 7–40 位 commit sha",
                "type": sdk.output_template_type.DEFAULT,
                "data": {},
            }
        )
        return 1
    strategy = (inp.get("strategy") or REVERT_UPDATE).strip().upper()
    if strategy not in _STRATEGIES:
        sdk.log.warning(f"未知拉取策略 {strategy}，按 {REVERT_UPDATE} 处理")
        strategy = REVERT_UPDATE
    commit_ref = _is_commit_sha(ref)

    if not repo_url:
        # 以前这里会建个空 src 然后报成功，后面照常编译打包，最后把空包发上线。
        # 平台侧已在建任务时挡住，这里再兜一道：拿不到地址就是拉不了代码
        sdk.log.error("没有拿到代码库地址，无法拉取代码")
        sdk.set_output(
            {
                "status": sdk.status.FAILURE,
                "message": "缺少代码库地址：请在流水线编辑页为「拉取代码」步骤选择代码库",
                "type": sdk.output_template_type.DEFAULT,
                "data": {},
            }
        )
        return 1

    sdk.log.info(
        f"拉取 {_mask_url(repo_url)}@{ref}"
        f"（{'commit' if commit_ref else 'branch/tag'}） 到 {src}，策略 {strategy}"
    )
    if not repo_token:
        sdk.log.warning(
            "未注入仓库凭证。已禁止 Git 弹窗要密码，私有库会直接失败。"
            "请在「代码库」绑定凭证后重试。"
        )

    if strategy == FRESH_CHECKOUT and src.exists():
        # 选了全新检出就得真的清干净，否则和增量更新没区别——
        # 用户挑这个选项多半正是因为上次构建残留把这次带坏了
        sdk.log.info("策略 FRESH_CHECKOUT：清空 src 后重新检出")
        _rm_tree(src)

    git_dir = src / ".git"
    if git_dir.exists():
        # INCREMENT_UPDATE 保留未跟踪文件（增量编译产物、本地配置），
        # REVERT_UPDATE 连未跟踪文件一起清掉，回到干净的工作树
        sdk.log.info(
            f"代码已存在，fetch + reset → {ref}"
            f"（{'保留未跟踪文件' if strategy != REVERT_UPDATE else '清理未跟踪文件'}）"
        )
        steps = [
            ["git", "fetch", "--depth", "1", "--no-tags", "origin", ref],
            ["git", "reset", "--hard", "FETCH_HEAD"],
        ]
        if strategy == REVERT_UPDATE:
            steps.append(["git", "clean", "-fdx"])
        def _fetch_once(user: str) -> bool:
            """已有工作树时，用指定用户名 fetch + reset。"""
            return _seq(steps, src, repo_token, user)

        ok = _checkout_with_auth(_fetch_once, repo_user)
        if not ok:
            _log_auth_fail_hint(repo_token)
        return 0 if _finish(ok, src) else 1

    sdk.log.info(f"浅克隆 {'commit ' if commit_ref else 'branch '}{ref}")

    def _clone_once(user: str) -> bool:
        """按一个用户名浅克隆。失败会留下半成品目录，下次尝试先清掉。"""
        _rm_tree(src)
        if commit_ref:
            src.mkdir(parents=True, exist_ok=True)
            return _seq(
                [
                    ["git", "init"],
                    ["git", "remote", "add", "origin", repo_url],
                    ["git", "fetch", "--depth", "1", "--no-tags", "origin", ref],
                    ["git", "checkout", "--detach", "FETCH_HEAD"],
                ],
                src,
                repo_token,
                user,
            )
        return _run(
            [
                "git", "clone", "--depth", "1", "--single-branch", "--no-tags",
                "-b", ref, repo_url, "src",
            ],
            pipeline_dir,
            token=repo_token,
            username=user,
        ) == 0

    ok = _checkout_with_auth(_clone_once, repo_user)
    if not ok:
        _log_auth_fail_hint(repo_token)
        sdk.set_output(
            {
                "status": sdk.status.FAILURE,
                "message": "git checkout failed",
                "type": sdk.output_template_type.DEFAULT,
                "data": {},
            }
        )
        return 1
    return 0 if _finish(True, src) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckoutError as e:
        sdk.log.error(str(e))
        sys.exit(1)
