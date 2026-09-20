# -*- coding: utf-8 -*-
"""run-pipeline 兜底入口。

正常路径：平台编排器（platform-worker）直接启动子流水线并等待，不占用构建机。
仅当旧 Agent 误领到该步骤时，才走本脚本调 HTTP 接口。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

import release_atom_sdk as sdk

TERMINAL = {"success", "failed", "cancelled", "rolled_back"}


def _normalize_ns(ns: str) -> str:
    text = (ns or "sub_pipeline_").strip() or "sub_pipeline_"
    if not text.endswith("_"):
        text += "_"
    return text


def _auth() -> tuple[str, str]:
    """返回 (请求头名, 凭证)。

    新 Agent 只注入任务级凭证；老 Agent 还在注入构建机 token，升级期间两者都认。
    """
    task_token = sdk.get_task_token()
    if task_token:
        return "X-Task-Token", task_token
    legacy = sdk.get_agent_token()
    if legacy:
        return "X-Agent-Token", legacy
    raise RuntimeError("缺少 RELEASE_TASK_TOKEN，无法调用平台")


def _http(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    server = sdk.get_server_url()
    if not server:
        raise RuntimeError("缺少 RELEASE_SERVER_URL，无法调用平台")
    header, token = _auth()
    url = server.rstrip("/") + path
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header(header, token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"message": raw or str(e)}
        raise RuntimeError(payload.get("message") or f"HTTP {e.code}") from e
    if int(payload.get("code") or 0) != 0:
        raise RuntimeError(payload.get("message") or "平台返回失败")
    data_obj = payload.get("data")
    return data_obj if isinstance(data_obj, dict) else {}


def _fail(msg: str) -> int:
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


def _ok(message: str, data: dict) -> int:
    sdk.set_output(
        {
            "status": sdk.status.SUCCESS,
            "message": message,
            "type": sdk.output_template_type.DEFAULT,
            "data": data,
        }
    )
    return 0


def _as_output_fields(values: dict[str, str]) -> dict:
    return {
        k: {"type": sdk.output_field_type.STRING, "value": v}
        for k, v in values.items()
    }


def _pick_outputs(outputs: dict, names: list[str], ns: str) -> dict[str, str]:
    src = outputs if isinstance(outputs, dict) else {}
    picked: dict[str, str] = {}
    keys = names if names else [str(k) for k in src.keys()]
    for name in keys:
        val = src.get(name)
        if val is None:
            continue
        picked[f"{ns}{name}"] = "" if val is None else str(val)
    return picked


def main() -> int:
    inp = sdk.get_input()
    pipeline_id = inp.get("pipelineId") or inp.get("pipeline_id")
    project_id = inp.get("projectId") or inp.get("project_id")
    if not pipeline_id:
        return _fail("未选择要启动的流水线")

    run_mode = str(inp.get("runMode") or inp.get("run_mode") or "sync").lower()
    try:
        poll_interval = int(inp.get("pollInterval") or inp.get("poll_interval") or 10)
    except (TypeError, ValueError):
        poll_interval = 10
    if poll_interval < 1:
        poll_interval = 10
    ns = _normalize_ns(str(inp.get("outputNamespace") or inp.get("output_namespace") or "sub_pipeline_"))
    raw_vars = inp.get("outputVars") or inp.get("output_vars") or ""
    names = [x.strip() for x in str(raw_vars).split(",") if x.strip()]
    params = inp.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params) if params.strip() else {}
        except json.JSONDecodeError:
            params = {}
    if not isinstance(params, dict):
        params = {}

    # 任务级凭证已经绑定了本任务，父发布由平台推出来，不需要也不该由插件指定
    use_task_token = bool(sdk.get_task_token())
    agent_id = sdk.get_agent_id()
    parent_pipeline_id = sdk.get_pipeline_id()
    parent_release_id = sdk.get_release_id()
    run_path = (
        "/api/v1/plugin-api/sub-pipelines/run"
        if use_task_token
        else f"/api/v1/agents/{agent_id}/sub-pipelines/run"
    )

    sdk.log.info(
        f"启动子流水线 pipeline=#{pipeline_id} project=#{project_id or '-'} "
        f"mode={run_mode} poll={poll_interval}s ns={ns}"
    )
    if params:
        sdk.log.info("启动参数: " + json.dumps(params, ensure_ascii=False))

    try:
        started = _http(
            "POST",
            run_path,
            {
                "pipeline_id": int(pipeline_id),
                "project_id": int(project_id) if project_id not in (None, "") else None,
                "params": params,
                "parent_pipeline_id": int(parent_pipeline_id) if parent_pipeline_id else None,
                "parent_release_id": int(parent_release_id) if parent_release_id else None,
            },
        )
    except Exception as e:  # noqa: BLE001
        return _fail(f"启动子流水线失败: {e}")

    child_id = started.get("id")
    status = started.get("status") or ""
    sdk.log.info(f"子流水线已启动 release=#{child_id} status={status}")

    if run_mode in ("async", "asynchronous"):
        picked = _pick_outputs(started.get("outputs") or {}, names, ns)
        picked[f"{ns}release_id"] = str(child_id or "")
        picked[f"{ns}status"] = str(status)
        sdk.log.info("异步模式：不等待子流水线结束")
        return _ok("sub-pipeline started", _as_output_fields(picked))

    if not child_id:
        return _fail("平台未返回子流水线 release id")

    deadline = time.time() + 24 * 3600
    last_status = status
    while time.time() < deadline:
        try:
            status_path = (
                f"/api/v1/plugin-api/releases/{int(child_id)}/status"
                if use_task_token
                else f"/api/v1/agents/{agent_id}/releases/{int(child_id)}/status"
            )
            st = _http("GET", status_path)
        except Exception as e:  # noqa: BLE001
            sdk.log.warning(f"轮询失败，{poll_interval}s 后重试: {e}")
            time.sleep(poll_interval)
            continue
        last_status = str(st.get("status") or "")
        sdk.log.info(f"子流水线 #{child_id} 状态 {last_status}")
        if last_status in TERMINAL:
            outputs = st.get("outputs") or {}
            picked = _pick_outputs(outputs, names, ns)
            picked[f"{ns}release_id"] = str(child_id)
            picked[f"{ns}status"] = last_status
            if last_status == "success":
                sdk.log.info("子流水线执行成功")
                return _ok("sub-pipeline success", _as_output_fields(picked))
            return _fail(f"子流水线执行结束，状态 {last_status}")
        time.sleep(poll_interval)

    return _fail(f"等待子流水线超时（最后状态 {last_status}）")


if __name__ == "__main__":
    sys.exit(main())
