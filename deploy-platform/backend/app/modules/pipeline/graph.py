"""流水线定义 ↔ 可视化图形 的转换器。"""
from __future__ import annotations

from app.core.response import BizException
from app.modules.pipeline.plugins import get_agent_icon, get_plugin_meta
from app.modules.pipeline.schemas import (
    GraphJob,
    GraphStage,
    GraphStep,
    GraphTrigger,
    PipelineDefinition,
    PipelineGraph,
)

_CRON_TYPES = {"cron", "timer", "TIME_TRIGGER", "time_trigger", "TIMER"}
TRIGGER_MANUAL = "manual"
TRIGGER_CRON = "cron"


def exclusive_trigger_of(items, *, require_valid: bool = False) -> tuple[str, str]:
    """一条流水线只能是手动或定时。旧数据里两者并存时保留定时。

    items 可以是 TriggerSpec / GraphTrigger。未接入的 webhook 等一律当手动。
    require_valid=True 用于保存：定时必须带合法 Cron 表达式。
    """
    cron_item = None
    for t in items or []:
        typ = (getattr(t, "type", None) or "").strip()
        if typ in _CRON_TYPES:
            cron_item = t
            break
    if cron_item is None:
        return TRIGGER_MANUAL, ""
    expr = (getattr(cron_item, "cron", None) or "").strip()
    if require_valid:
        if not expr:
            raise BizException.bad_request("定时触发必须填写 Cron 表达式")
        try:
            import croniter

            croniter.croniter(expr)
        except BizException:
            raise
        except Exception as e:
            raise BizException.bad_request(f"Cron 表达式无效：{expr}") from e
    return TRIGGER_CRON, expr


def as_exclusive_triggers(items, *, require_valid: bool = False) -> list[GraphTrigger]:
    """把任意触发器列表收成恰好一条，给编辑器和 YAML 回写用。"""
    kind, expr = exclusive_trigger_of(items, require_valid=require_valid)
    if kind == TRIGGER_CRON:
        return [GraphTrigger(type=TRIGGER_CRON, cron=expr, label=_trigger_label(TRIGGER_CRON))]
    return [GraphTrigger(type=TRIGGER_MANUAL, cron=None, label=_trigger_label(TRIGGER_MANUAL))]


def _stage_graph_id(order: int) -> str:
    """阶段在画布上的 id，与编辑器节点 `stage-${id}` 对应。第 1 个阶段是 stage-1。"""
    return f"stage-{order + 1}"


def canvas_layout_of(definition: PipelineDefinition) -> dict:
    """YAML 里保存的节点坐标，执行画布和编辑画布共用。"""
    return dict(definition.pipeline.canvas_layout or {})


def yaml_stage_ids(definition: PipelineDefinition) -> list[dict]:
    """按编排顺序给出阶段 id / 名称，用来给执行序列补上和编辑器相同的 id。"""
    out = []
    for order, stage in enumerate(definition.pipeline.stages or []):
        out.append({
            "id": _stage_graph_id(order),
            "name": stage.name or f"阶段 {order + 1}",
        })
    return out


def attach_stage_ids(stages: list[dict], yaml_stages: list[dict]) -> list[dict]:
    """给执行序列的阶段补上与编辑器相同的 id，保存的 layout 才能对上号。"""
    if len(stages) == len(yaml_stages):
        names_ok = all(
            (not y["name"]) or s.get("name") == y["name"]
            for s, y in zip(stages, yaml_stages)
        )
        if names_ok:
            for s, y in zip(stages, yaml_stages):
                s["id"] = y["id"]
            return stages
    used: set[str] = set()
    for i, s in enumerate(stages):
        y = next((x for x in yaml_stages if x["name"] == s.get("name") and x["id"] not in used), None)
        if y:
            s["id"] = y["id"]
            used.add(y["id"])
        else:
            s["id"] = s.get("id") or _stage_graph_id(i)
    return stages


def to_graph(definition: PipelineDefinition, pipeline_id: int | None, name: str, version: int) -> PipelineGraph:
    """将 YAML 定义转换为前端可视化图形结构。"""
    spec = definition.pipeline

    triggers = as_exclusive_triggers(spec.triggers or [])

    stages = []
    for order, stage in enumerate(spec.stages or []):
        jobs = []
        for ji, job in enumerate(stage.jobs or []):
            job_id = job.id or f"{order + 1}-{ji + 1}"
            steps = []
            for si, step in enumerate(job.steps or []):
                display, icon, _ = get_plugin_meta(step.plugin)
                steps.append(
                    GraphStep(
                        # YAML 里没写 name 时，编辑器用插件显示名填进步骤名称
                        name=(step.name or "").strip() or display,
                        plugin=step.plugin,
                        display_name=display,
                        icon=icon,
                        order=si + 1,
                        with_=step.with_,
                    )
                )
            jobs.append(
                GraphJob(
                    id=job_id,
                    name=job.name or f"作业 {job_id}",
                    agent=job.agent,
                    agent_icon=get_agent_icon(job.agent),
                    steps=steps,
                )
            )
        stages.append(
            GraphStage(
                id=_stage_graph_id(order),
                name=stage.name or f"阶段 {order + 1}",
                order=order + 1,
                jobs=jobs,
            )
        )

    return PipelineGraph(
        pipeline_id=pipeline_id,
        pipeline_name=name,
        version=version,
        triggers=triggers,
        stages=stages,
        variables=spec.variables or [],
        layout=spec.canvas_layout or {},
    )


def _trigger_label(t: str) -> str:
    return {"manual": "手动触发", "webhook": "代码库事件触发", "cron": "定时触发", "remote": "远程触发", "service": "API 触发", "pipeline": "子流水线触发"}.get(t, t)
