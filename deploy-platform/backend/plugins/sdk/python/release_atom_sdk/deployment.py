# -*- coding: utf-8 -*-
"""向平台登记「这次部署怎么撤销」。

部署类插件跑完必须调一次：平台的一键回滚不重新构建，全靠这条记录反向执行。
不上报也不会让发布失败，但那次发布就回滚不了了，所以失败时要把话说明白。
"""
from __future__ import annotations

import json
import urllib.request

from .context import get_server_url, get_task_token
from .log import log

# kind 决定回滚怎么执行，必须是平台认识的几种之一
KIND_DOCKER = "docker-image"
KIND_K8S = "k8s-revision"


def report_deployment(
    kind: str,
    target: str,
    payload: dict,
    summary: str = "",
    step_index: int = 0,
) -> int | None:
    """登记一次部署，返回记录号。

    payload 里必须带 undo_with：回滚时原样交给同一个插件执行的参数。
    """
    server = get_server_url()
    token = get_task_token()
    if not server or not token:
        log.warning("缺少平台地址或任务凭证，本次部署无法登记，届时不能一键回滚")
        return None

    body = json.dumps(
        {
            "kind": kind,
            "target": target,
            "payload": payload or {},
            "summary": summary,
            "step_index": step_index,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{server}/api/v1/plugin-api/deployments",
        data=body,
        headers={"Content-Type": "application/json", "X-Task-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") not in (0, 200):
            raise RuntimeError(data.get("message"))
        return (data.get("data") or {}).get("id")
    except Exception as e:  # noqa: BLE001
        log.warning(f"部署记录登记失败（{e}），这次发布将无法一键回滚")
        return None


def report_undone(record_id: int) -> None:
    """回滚跑完销账，避免同一条记录被撤销两次。"""
    server = get_server_url()
    token = get_task_token()
    if not server or not token or not record_id:
        return
    req = urllib.request.Request(
        f"{server}/api/v1/plugin-api/deployments/{int(record_id)}/undone",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-Task-Token": token},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except Exception as e:  # noqa: BLE001
        log.warning(f"回滚已完成，但销账失败：{e}")
