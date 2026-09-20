# -*- coding: utf-8 -*-
"""git-checkout 插件：Agent 仅执行本入口，不内嵌检出逻辑。

git 一律参数列表 + shell=False，ref 走白名单。仓库 token 只进 git 的
http.extraHeader 环境变量，不写进 clone URL 的 userinfo，避免出现在进程列表
和远程 URL 日志里。
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
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


def _git_argv(argv: list[str]) -> list[str]:
    """把 argv[0] 的 git 换成绝对路径，避免依赖服务进程那份很窄的 PATH。"""
    if argv and argv[0] == "git":
        return [find_git(), *argv[1:]]
    return argv


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


def _git_auth_header(token: str) -> str:
    """Git 走 HTTPS 时的 Authorization 头。

    GitLab 的 git smart-HTTP 只认 HTTP Basic：用户名任意非空（惯例写 oauth2），
    密码是仓库 token。Bearer 是 REST API 用的，git-http-backend 会直接丢掉，
    构建机又没有 TTY 输密码，表现就是 clone 鉴权失败。
    """
    raw = base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {raw}"


def _git_env(token: str = "") -> dict[str, str]:
    """git 子进程环境。token 只出现在 extraHeader 里，不进 argv 和 clone URL。"""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    if token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = _git_auth_header(token)
    return env


def _run(argv: list[str], cwd: Path, sink=True, token: str = "") -> int:
    """执行一条 git 命令。argv 原样交给 subprocess，shell=False。"""
    argv = _git_argv(argv)
    p = subprocess.Popen(
        argv,
        shell=False,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_git_env(token),
    )
    assert p.stdout is not None
    for raw in iter(p.stdout.readline, b""):
        line = sdk.decode(raw).rstrip("\r\n")
        if sink:
            sdk.log.info(line)
    return p.wait()


def _capture(argv: list[str], cwd: Path, token: str = "") -> str | None:
    """跑一条命令，只取第一行 stdout。失败返回 None。"""
    try:
        out = subprocess.check_output(
            _git_argv(argv),
            shell=False,
            cwd=str(cwd),
            env=_git_env(token),
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


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _rm_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _is_unusable_mirror(mirror: Path) -> bool:
    if (mirror / "shallow").exists():
        return True
    objects = mirror / "objects"
    return not objects.exists() or not any(objects.iterdir())


def _ensure_mirror(repo_key: str, repo_url: str, ref: str, token: str) -> Path | None:
    home = Path.home() / ".release-agent" / "git-cache"
    home.mkdir(parents=True, exist_ok=True)
    mirror = home / f"{_sha1(repo_key)}.git"
    commit_ref = _is_commit_sha(ref)

    if mirror.exists() and _is_unusable_mirror(mirror):
        sdk.log.warning("本地 mirror 不可用，重建")
        _rm_tree(mirror)

    git_dir = ["git", f"--git-dir={mirror}"]
    if not mirror.exists():
        sdk.log.info("首次建立本地镜像缓存（仅此一次较慢）")
        if commit_ref:
            rc = _run(
                ["git", "clone", "--bare", "--single-branch", "--no-tags", repo_url, str(mirror)],
                Path.cwd(),
                token=token,
            )
            if rc != 0:
                return None
            _run(git_dir + ["fetch", "--no-tags", "origin", ref], Path.cwd(), token=token)
        else:
            rc = _run(
                [
                    "git", "clone", "--bare", "--single-branch", "--no-tags",
                    "-b", ref, repo_url, str(mirror),
                ],
                Path.cwd(),
                token=token,
            )
            if rc != 0:
                # 不能改去拉默认分支：指定的 ref 不存在时，默认分支的代码会当成功检出，
                # 后面照常编译打包发上生产，日志里只剩一句 clone 失败，非常难查。
                sdk.log.error(
                    f"指定的分支/标签「{ref}」拉不下来（仓库里没有，或构建机访问不到）。"
                    "已终止，不会改去拉默认分支。"
                )
                _rm_tree(mirror)
                return None
    else:
        if commit_ref:
            _run(git_dir + ["fetch", "--no-tags", "origin", ref], Path.cwd(), token=token)
        else:
            _run(
                git_dir + ["fetch", "--no-tags", "origin", f"{ref}:refs/heads/{ref}"],
                Path.cwd(),
                token=token,
            )

    if _is_unusable_mirror(mirror):
        _rm_tree(mirror)
        return None
    return mirror


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


def _seq(steps: list[list[str]], cwd: Path, token: str) -> bool:
    """按顺序跑多条命令，任一条非 0 即停。"""
    for argv in steps:
        if _run(argv, cwd, token=token) != 0:
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
    repo_token = (inp.get("repoToken") or "").strip()
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
        ok = _seq(steps, src, repo_token)
        return 0 if _finish(ok, src) else 1

    mirror = _ensure_mirror(repo_url, repo_url, ref, repo_token)
    if mirror is not None:
        sdk.log.info("从本地缓存秒级检出（--reference + --dissociate）")
        _rm_tree(src)
        if commit_ref:
            ok = _seq(
                [
                    ["git", "clone", "--no-checkout", str(mirror), "src"],
                    ["git", "-C", "src", "checkout", "--detach", ref],
                    ["git", "-C", "src", "remote", "set-url", "origin", repo_url],
                ],
                pipeline_dir,
                repo_token,
            )
        else:
            ok = _run(
                [
                    "git", "clone", "--reference", str(mirror), "--dissociate",
                    "--depth", "1", "--single-branch", "--no-tags",
                    "-b", ref, repo_url, "src",
                ],
                pipeline_dir,
                token=repo_token,
            ) == 0
        if ok:
            return 0 if _finish(True, src) else 1
        sdk.log.warning("缓存检出失败，清理后降级")
        _rm_tree(src)
        if _is_unusable_mirror(mirror):
            _rm_tree(mirror)

    sdk.log.info(f"降级检出 {'commit ' if commit_ref else 'branch '}{ref}")
    _rm_tree(src)
    if commit_ref:
        src.mkdir(parents=True, exist_ok=True)
        ok = _seq(
            [
                ["git", "init"],
                ["git", "remote", "add", "origin", repo_url],
                ["git", "fetch", "--depth", "1", "--no-tags", "origin", ref],
                ["git", "checkout", "--detach", "FETCH_HEAD"],
            ],
            src,
            repo_token,
        )
    else:
        ok = _run(
            [
                "git", "clone", "--depth", "1", "--single-branch", "--no-tags",
                "-b", ref, repo_url, "src",
            ],
            pipeline_dir,
            token=repo_token,
        ) == 0
    if not ok:
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
