"""启动子流水线：权限、循环检测、参数合并、输出收集。

对应蓝盾「启动流水线 / 子流水线」插件：
- 只能调用当前用户有 execute 权限的流水线
- YAML 静态环 + 运行时祖先链，发现循环则直接失败
- 启动参数与默认值合并；完成后写入 outputs_json
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, check_permission
from app.core.response import BizException
from app.modules.pipeline.models import Pipeline, Release
from app.modules.pipeline.schemas import parse_yaml

PLUGIN_NAME = "run-pipeline"
DEFAULT_NAMESPACE = "sub_pipeline_"
# rejected 也是终态：子流水线走了审批又被驳回时，少了它父任务会一直等下去
TERMINAL_STATUSES = ("success", "failed", "cancelled", "rolled_back", "rejected")


def normalize_namespace(ns: str | None) -> str:
    text = (ns or DEFAULT_NAMESPACE).strip() or DEFAULT_NAMESPACE
    if not text.endswith("_"):
        text += "_"
    return text


def parse_output_var_names(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def encode_sub_pipeline_ids(ids: set[int]) -> str:
    """把 run-pipeline 目标 id 收成列上的逗号串。"""
    return ",".join(str(i) for i in sorted(ids))


def parse_sub_pipeline_ids(raw: str | None) -> set[int]:
    """读列上的逗号串。非法片段丢掉，避免脏数据把环检测打崩。"""
    out: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            pid = int(part)
        except ValueError:
            continue
        if pid > 0:
            out.add(pid)
    return out


def collect_run_pipeline_targets(yaml_text: str) -> set[int]:
    """从 YAML 中收集 run-pipeline 步骤指向的流水线 ID。"""
    ids: set[int] = set()
    try:
        definition = parse_yaml(yaml_text or "")
    except Exception:  # noqa: BLE001
        return ids
    for stage in definition.pipeline.stages or []:
        for job in stage.jobs or []:
            for step in job.steps or []:
                if (step.plugin or "") != PLUGIN_NAME:
                    continue
                with_ = step.with_ or {}
                raw = with_.get("pipelineId") if with_.get("pipelineId") is not None else with_.get("pipeline_id")
                try:
                    pid = int(raw)
                except (TypeError, ValueError):
                    continue
                if pid > 0:
                    ids.add(pid)
    return ids


def rewrite_run_pipeline_ids(yaml_text: str, id_map: dict[int, int]) -> str:
    """把 YAML 里 run-pipeline 的源环境 id 换成导入后的新 id。映射里没有的保持原值。"""
    if not (yaml_text or "").strip() or not id_map:
        return yaml_text or ""
    from app.modules.pipeline.schemas import dump_yaml

    try:
        definition = parse_yaml(yaml_text)
    except Exception:  # noqa: BLE001
        return yaml_text
    changed = False
    for stage in definition.pipeline.stages or []:
        for job in stage.jobs or []:
            for step in job.steps or []:
                if (step.plugin or "") != PLUGIN_NAME:
                    continue
                with_ = dict(step.with_ or {})
                key = "pipelineId" if "pipelineId" in with_ else "pipeline_id" if "pipeline_id" in with_ else None
                if key is None:
                    continue
                try:
                    old = int(with_[key])
                except (TypeError, ValueError):
                    continue
                new = id_map.get(old)
                if new is None or new == old:
                    continue
                with_[key] = new
                step.with_ = with_
                changed = True
    if not changed:
        return yaml_text
    return dump_yaml(definition)


def sync_yaml_pipeline_name(yaml_text: str, name: str) -> str:
    """编排正文里的 pipeline.name 跟库里的显示名对齐。"""
    text = (name or "").strip()
    if not text or not (yaml_text or "").strip():
        return yaml_text or ""
    from app.modules.pipeline.schemas import dump_yaml

    try:
        definition = parse_yaml(yaml_text)
    except Exception:  # noqa: BLE001
        return yaml_text
    if definition.pipeline.name == text:
        return yaml_text
    definition.pipeline.name = text
    return dump_yaml(definition)


def env_code_of(db: Session, pipeline: Pipeline) -> str:
    """流水线所属环境码，分组缺失或没标码时直接拒绝。"""
    from app.core.env import pipeline_env_of
    from app.modules.project.models import Group

    grp = db.get(Group, pipeline.group_id)
    if grp is None:
        raise BizException.bad_request(
            f"流水线「{pipeline.name}」所属环境分组不存在，无法做生产和测试隔离"
        )
    return pipeline_env_of(grp.type)


def assert_same_pipeline_env(db: Session, caller: Pipeline, target: Pipeline) -> None:
    """父子流水线必须同一环境码：测试不能调生产，生产也不能调测试。"""
    from app.core.env import assert_same_env

    assert_same_env(
        env_code_of(db, caller),
        env_code_of(db, target),
        action=f"调用流水线「{target.name}」",
    )


def assert_yaml_nested_isolated(db: Session, pipeline: Pipeline) -> None:
    """保存编排时拦住跨环境的 run-pipeline，避免拖到发布才失败。"""
    from app.modules.pipeline.service import PIPELINE_ACTIVE

    for tid in collect_run_pipeline_targets(pipeline.yaml or ""):
        if pipeline.id and tid == pipeline.id:
            continue
        target = db.get(Pipeline, tid)
        if target is None or target.status != PIPELINE_ACTIVE:
            continue
        assert_same_pipeline_env(db, pipeline, target)


def build_run_pipeline_graph(db: Session) -> dict[int, set[int]]:
    """静态调用图：只读派生列，不 parse YAML。"""
    graph: dict[int, set[int]] = {}
    for pid, raw in db.execute(select(Pipeline.id, Pipeline.sub_pipeline_ids)).all():
        graph[pid] = parse_sub_pipeline_ids(raw)
    return graph


def _reachable(graph: dict[int, set[int]], start: int, goal: int, visiting: set[int] | None = None) -> bool:
    if start == goal:
        return True
    visiting = visiting if visiting is not None else set()
    if start in visiting:
        return False
    visiting.add(start)
    for nxt in graph.get(start) or set():
        if _reachable(graph, nxt, goal, visiting):
            return True
    return False


def assert_no_cycle(db: Session, caller_pipeline_id: int, target_pipeline_id: int) -> None:
    """禁止 A 调 A，以及 YAML 中 run-pipeline 互相成环。"""
    if caller_pipeline_id == target_pipeline_id:
        raise BizException.bad_request("不允许调用自身作为子流水线（循环调用）")
    graph = build_run_pipeline_graph(db)
    graph.setdefault(caller_pipeline_id, set()).add(target_pipeline_id)
    if _reachable(graph, target_pipeline_id, caller_pipeline_id):
        raise BizException.bad_request(
            f"检测到流水线循环调用：#{caller_pipeline_id} 与 #{target_pipeline_id} 经 run-pipeline 互相可达，插件拒绝执行"
        )


def assert_no_runtime_cycle(db: Session, parent_release_id: int | None, target_pipeline_id: int) -> None:
    """沿 parent_release_id 向上走，目标流水线不能出现在祖先链上。"""
    rid = parent_release_id
    seen: set[int] = set()
    depth = 0
    while rid and depth < 64:
        rel = db.get(Release, rid)
        if rel is None:
            break
        if rel.pipeline_id == target_pipeline_id:
            raise BizException.bad_request(
                f"检测到运行时循环调用：祖先发布 #{rid} 已是流水线 #{target_pipeline_id}"
            )
        if rel.pipeline_id in seen:
            break
        seen.add(rel.pipeline_id)
        rid = rel.parent_release_id
        depth += 1


def list_start_params(pipeline: Pipeline) -> list[dict[str, Any]]:
    """子流水线启动参数（仅 show_on_execution=true）。"""
    try:
        definition = parse_yaml(pipeline.yaml or "")
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for v in definition.pipeline.variables or []:
        if not v.show_on_execution:
            continue
        out.append(
            {
                "name": v.name,
                "type": v.type or "text",
                "default_value": v.default_value or "",
                "description": v.description or "",
                "options": list(v.options or []),
            }
        )
    return out


def merge_params(pipeline: Pipeline, overrides: dict | None) -> dict[str, str]:
    """默认值 + 调用方覆盖。"""
    try:
        definition = parse_yaml(pipeline.yaml or "")
    except Exception:  # noqa: BLE001
        definition = None
    merged: dict[str, str] = {}
    if definition is not None:
        for v in definition.pipeline.variables or []:
            merged[v.name] = "" if v.default_value is None else str(v.default_value)
    if not overrides:
        return merged
    for k, val in overrides.items():
        if val is None:
            continue
        key = str(k)
        if isinstance(val, list):
            merged[key] = ",".join(str(x) for x in val)
        elif isinstance(val, bool):
            merged[key] = "true" if val else "false"
        else:
            merged[key] = str(val)
    return merged


def collect_release_outputs(db: Session, release: Release) -> dict[str, str]:
    from app.modules.pipeline.service import get_pipeline

    pipeline = get_pipeline(db, release.pipeline_id)
    try:
        params = json.loads(release.run_params_json or "{}")
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return merge_params(pipeline, params)


def namespaced_outputs(
    outputs: dict | None,
    *,
    output_vars: str | None,
    namespace: str | None,
    extra: dict | None = None,
) -> dict[str, str]:
    """把子流水线输出按命名空间导出（末尾自动补 _）。"""
    ns = normalize_namespace(namespace)
    src = {str(k): "" if v is None else str(v) for k, v in (outputs or {}).items()}
    names = parse_output_var_names(output_vars)
    keys = names if names else list(src.keys())
    picked: dict[str, str] = {}
    for name in keys:
        if name in src:
            picked[f"{ns}{name}"] = src[name]
    if extra:
        for k, v in extra.items():
            picked[f"{ns}{k}"] = "" if v is None else str(v)
    return picked


def user_from_operator(db: Session, operator_id: int | None) -> CurrentUser:
    from app.modules.auth.models import User

    if not operator_id:
        raise BizException.forbidden("父流水线缺少操作人，无法校验子流水线权限")
    u = db.get(User, operator_id)
    if u is None:
        raise BizException.forbidden("父流水线操作人不存在，无法校验子流水线权限")
    return CurrentUser(id=u.id, username=u.username, is_admin=bool(u.is_admin))


def start_sub_pipeline(
    db: Session,
    *,
    user: CurrentUser,
    pipeline_id: int,
    project_id: int | None = None,
    params: dict | None = None,
    parent_pipeline_id: int | None = None,
    parent_release_id: int | None = None,
    trusted_nested: bool = False,
) -> Release:
    """权限 + 循环检测 + 创建并执行子流水线发布。

    trusted_nested：调用方是否**已经证明**这确实是父发布链上的一环
    （任务级 token，或核实过该构建机正在执行父发布的任务）。
    只有它为真时才继承父发布过过的审批闸门，见下面 is_nested 处的说明。
    """
    from app.modules.pipeline import service

    def _opt_int(v) -> int | None:
        if v is None or v == "":
            return None
        return int(v)

    pipeline_id = int(pipeline_id)
    project_id = _opt_int(project_id)
    parent_pipeline_id = _opt_int(parent_pipeline_id)
    parent_release_id = _opt_int(parent_release_id)

    target = service.get_pipeline(db, pipeline_id)
    if project_id and int(project_id) != target.project_id:
        raise BizException.bad_request(
            f"流水线 #{pipeline_id} 不属于项目 #{project_id}（实际项目 #{target.project_id}）"
        )
    if not check_permission(db, user, "pipeline", pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：执行流水线 #{pipeline_id}")

    caller_id = parent_pipeline_id
    if parent_release_id:
        parent = service.get_release(db, parent_release_id)
        caller_id = parent.pipeline_id
        assert_no_runtime_cycle(db, parent_release_id, pipeline_id)
    if caller_id:
        caller = service.get_pipeline(db, int(caller_id))
        assert_same_pipeline_env(db, caller, target)
        assert_no_cycle(db, int(caller_id), pipeline_id)

    merged = merge_params(target, params)
    version = f"sub-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    # 只有真正的「子流水线调用」才继承父发布已经过的审批闸门——那条链是父发布
    # 走 create_release 时把过关的。顶层调用（控制台 /run、AI 触发流水线）没有父级，
    # 必须走正常审批：否则就是一条绕开生产审批的旁路，有 execute 却没有 approve 的人
    # 改走 /pipelines/{id}/run 就能把生产直接发出去，而正常的 /releases 会拦在待审批。
    #
    # 关键：父子关系必须由**服务端**证明（任务级 token 绑定的 release，或核实过
    # 该构建机正在跑父发布的任务）。光看客户端传没传 parent_release_id 不算数——
    # 那个值就是调用方自己填的，随便塞一个历史 release 就能跳审。
    # 嵌套调用仍可能跳过目标线自己的审批，但生产和测试不能互相调。
    # 父线是测试、子线是生产时，有 execute 没有 approve 的人会借测试线把生产发出去。
    is_nested = trusted_nested and bool(parent_release_id or parent_pipeline_id)
    release = service.create_release(
        db,
        pipeline_id=pipeline_id,
        version=version,
        strategy="rolling",
        trigger_by="sub_pipeline",
        operator_id=user.id,
        parent_pipeline_id=caller_id,
        parent_release_id=parent_release_id,
        run_params=merged,
        skip_approval=is_nested,
    )
    if release.status == service.RELEASE_QUEUED:
        service.execute_release(db, release.id)
        db.refresh(release)
    return release


def release_status_payload(db: Session, release: Release) -> dict[str, Any]:
    outputs = {}
    try:
        outputs = json.loads(release.outputs_json or "{}") or {}
    except json.JSONDecodeError:
        outputs = {}
    if not outputs and release.status in TERMINAL_STATUSES:
        outputs = collect_release_outputs(db, release)
    return {
        "id": release.id,
        "pipeline_id": release.pipeline_id,
        "status": release.status,
        "version": release.version,
        "outputs": outputs if isinstance(outputs, dict) else {},
        "parent_pipeline_id": release.parent_pipeline_id,
        "parent_release_id": release.parent_release_id,
    }
