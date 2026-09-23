"""git-checkout：argv + ref 白名单，token 不进 clone URL。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_plugin_deploy_safety import _load


def test_ref_whitelist():
    task = _load("git-checkout")
    assert task._valid_ref("master")
    assert task._valid_ref("feature/x-1")
    assert task._valid_ref("a" * 7)
    assert task._valid_ref("deadbeefcafebabe")
    assert not task._valid_ref("$(reboot)")
    assert not task._valid_ref("origin;rm -rf /")
    assert not task._valid_ref("-c")
    assert not task._valid_ref("../etc")
    assert not task._valid_ref("")


def test_git_finds_binary_outside_path(tmp_path, monkeypatch):
    """Windows Git / 非 PATH 安装仍应被找到，不能只认 shutil.which。"""
    import os

    task = _load("git-checkout")
    name = "git.exe" if os.name == "nt" else "git"
    fake = tmp_path / name
    fake.write_bytes(b"")
    if os.name != "nt":
        fake.chmod(0o755)
    monkeypatch.setattr(task.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(task, "_git_bin_dirs", lambda: [tmp_path])
    assert Path(task.find_git()).name == name


def test_git_missing_explains_install(monkeypatch):
    """本机没有 git 时，报错要能看懂该装到哪里。"""
    task = _load("git-checkout")
    monkeypatch.setattr(task.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(task, "_git_bin_dirs", lambda: [])
    with pytest.raises(task.CheckoutError) as exc:
        task.find_git()
    assert "git" in str(exc.value).lower()


def test_run_uses_argv_not_shell(monkeypatch):
    import base64

    task = _load("git-checkout")
    src = Path(task.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src
    assert "shell=False" in src
    # token 不进 clone URL；GitLab git-http 要的是 Basic oauth2:token，不是 Bearer
    assert "https://oauth2:" not in src
    monkeypatch.setattr(task, "find_git", lambda: "git")
    argv = task._git_argv(
        ["git", "clone", "https://example.com/r.git"],
        token="s3cret",
        username="",
    )
    header = argv[4]
    assert header.startswith("http.extraHeader=Authorization: Basic ")
    decoded = base64.b64decode(header.split()[-1]).decode("utf-8")
    assert decoded == "oauth2:s3cret"
    assert "Bearer" not in header
    assert "credential.helper=" not in argv
    env = task._git_env()
    assert env["GCM_INTERACTIVE"] == "never"
    assert "GIT_ASKPASS" not in env
    assert "GIT_CONFIG_COUNT" not in env


def test_password_json_uses_real_username(monkeypatch):
    """账号密码凭证不能再被当成 oauth2:整段 JSON。"""
    import base64

    task = _load("git-checkout")
    user, secret = task._split_secret('{"username":"kang","password":"p@ss"}', "")
    assert user == "kang"
    assert secret == "p@ss"
    monkeypatch.setattr(task, "find_git", lambda: "git")
    argv = task._git_argv(["git", "fetch", "origin"], token=secret, username=user)
    decoded = base64.b64decode(argv[4].split()[-1]).decode("utf-8")
    assert decoded == "kang:p@ss"


def test_parent_askpass_echo_stripped(monkeypatch):
    """父进程残留的 GIT_ASKPASS=echo 必须摘掉，否则会把提示语当密码发出去。"""
    monkeypatch.setenv("GIT_ASKPASS", "echo")
    monkeypatch.setenv("SSH_ASKPASS", "echo")
    task = _load("git-checkout")
    env = task._git_env()
    assert "GIT_ASKPASS" not in env
    assert "SSH_ASKPASS" not in env


def test_auth_usernames_retries_oauth2():
    """GitLab PAT 先用 oauth2，再试平台注入的真实用户名。"""
    task = _load("git-checkout")
    assert task._auth_usernames("kang") == ["oauth2", "kang"]
    assert task._auth_usernames("oauth2") == ["oauth2"]
    assert task._auth_usernames("") == ["oauth2"]


def test_no_home_git_cache(monkeypatch):
    """构建机家目录不再做 bare 镜像，避免占满 C 盘。"""
    task = _load("git-checkout")
    src = Path(task.__file__).read_text(encoding="utf-8")
    assert "git-cache" not in src
    assert "_ensure_mirror" not in src
    monkeypatch.setattr(task, "find_git", lambda: "git")
    argv = task._git_argv(["git", "clone", "https://example.com/r.git"])
    assert argv[1:3] == ["-c", "credential.interactive=never"]
    assert "credential.helper=" not in argv
    assert "http.extraHeader" not in " ".join(argv)

