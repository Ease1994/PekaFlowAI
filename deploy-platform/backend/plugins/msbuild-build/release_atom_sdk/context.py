# -*- coding: utf-8 -*-
"""从环境变量读取 Agent 注入的上下文（对齐蓝鲸 BK_CI_* / 输入 JSON）。"""
from __future__ import annotations

import json
import os


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


def get_workspace() -> str:
    """工作空间根目录（含 src/ 的上一级，或 src 本身由 RELEASE_WORKSPACE 指定）。"""
    return _env("RELEASE_WORKSPACE") or _env("BK_CI_WORKSPACE") or os.getcwd()


def get_input() -> dict:
    """插件入参。优先读 RELEASE_ATOM_INPUT_JSON；值均为字符串或可 JSON 反序列化。"""
    raw = _env("RELEASE_ATOM_INPUT_JSON") or _env("bk_atom_input")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def get_pipeline_id() -> str:
    return _env("RELEASE_PIPELINE_ID") or _env("BK_CI_PIPELINE_ID")


def get_pipeline_name() -> str:
    return _env("RELEASE_PIPELINE_NAME") or _env("BK_CI_PIPELINE_NAME")


def get_pipeline_build_id() -> str:
    return _env("RELEASE_BUILD_ID") or _env("BK_CI_BUILD_ID")


def get_project_name() -> str:
    return _env("RELEASE_PROJECT_NAME") or _env("BK_CI_PROJECT_NAME")


def get_sensitive_conf(key: str) -> str:
    """插件私有配置（平台注入 RELEASE_SENSITIVE_<KEY>）。"""
    return _env(f"RELEASE_SENSITIVE_{key.upper()}")


def get_server_url() -> str:
    """平台地址（Agent 注入 RELEASE_SERVER_URL）。"""
    return _env("RELEASE_SERVER_URL").rstrip("/")


def get_task_token() -> str:
    """本次任务的凭证（Agent 注入 RELEASE_TASK_TOKEN）。

    只能用于以本任务名义回调平台，任务结束即失效。
    """
    return _env("RELEASE_TASK_TOKEN")


def get_agent_token() -> str:
    """已废弃：构建机的长期身份不再下发给插件，新插件请用 get_task_token()。

    仅为兼容尚未升级的 Agent 保留，升级后固定返回空串。
    """
    return _env("RELEASE_AGENT_TOKEN")


def get_agent_id() -> str:
    return _env("RELEASE_AGENT_ID")


def get_release_id() -> str:
    return _env("RELEASE_RELEASE_ID")
