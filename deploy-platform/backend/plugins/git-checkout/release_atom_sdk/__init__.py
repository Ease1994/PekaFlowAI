# -*- coding: utf-8 -*-
"""release Atom SDK（对齐蓝鲸 python_atom_sdk 用法）。

Agent 只跑入口命令；业务逻辑写在插件里，通过本 SDK 读输入/打日志/写输出。
"""
from . import status, output_template_type, output_field_type, output_error_type
from .context import (
    get_workspace,
    get_input,
    get_pipeline_id,
    get_pipeline_name,
    get_pipeline_build_id,
    get_project_name,
    get_sensitive_conf,
    get_server_url,
    get_task_token,
    get_agent_token,
    get_agent_id,
    get_release_id,
)
from .deployment import KIND_DOCKER, KIND_K8S, report_deployment, report_undone
from .log import log
from .output import set_output
from .proc import capture, decode, redact, stream

__all__ = [
    "status",
    "output_template_type",
    "output_field_type",
    "output_error_type",
    "get_workspace",
    "get_input",
    "get_pipeline_id",
    "get_pipeline_name",
    "get_pipeline_build_id",
    "get_project_name",
    "get_sensitive_conf",
    "get_server_url",
    "get_task_token",
    "get_agent_token",
    "get_agent_id",
    "get_release_id",
    "log",
    "set_output",
    "stream",
    "capture",
    "decode",
    "redact",
    "report_deployment",
    "report_undone",
    "KIND_DOCKER",
    "KIND_K8S",
]
