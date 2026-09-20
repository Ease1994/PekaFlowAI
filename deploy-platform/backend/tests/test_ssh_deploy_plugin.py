# -*- coding: utf-8 -*-
"""ssh-deploy 参数校验：主机、用户、远程路径必须进不了 shell。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "ssh-deploy"
sys.path.insert(0, str(PLUGIN))

from task import DeployError, validate_host, validate_port, validate_remote_dir, validate_user  # noqa: E402


def test_host_accepts_ip_and_name():
    assert validate_host("192.0.2.10") == "192.0.2.10"
    assert validate_host("nginx.example.com") == "nginx.example.com"


@pytest.mark.parametrize("raw", ["", "a;rm -rf", "host && reboot", "a b", "-oProxyCommand"])
def test_host_rejects_metachar(raw):
    with pytest.raises(DeployError):
        validate_host(raw)


def test_user_and_port():
    assert validate_user("root") == "root"
    assert validate_port(22) == 22
    with pytest.raises(DeployError):
        validate_user("root;id")
    with pytest.raises(DeployError):
        validate_port(0)


def test_remote_dir():
    assert validate_remote_dir("/data/nginx/html/aicoach-web") == "/data/nginx/html/aicoach-web"
    with pytest.raises(DeployError):
        validate_remote_dir("/tmp/../etc")
    with pytest.raises(DeployError):
        validate_remote_dir("relative")
    with pytest.raises(DeployError):
        validate_remote_dir("/tmp/$(id)")
