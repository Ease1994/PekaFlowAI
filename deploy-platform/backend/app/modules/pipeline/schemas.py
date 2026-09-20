"""流水线编排的 Schema（Stage → Job → Step，参考蓝盾）。

同时承载 YAML 解析（Pipeline as Code）与前端可视化图形数据结构。
"""
from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# YAML 定义结构（Pipeline as Code）
# ============================================================
class TriggerSpec(BaseModel):
    type: str = "manual"          # 一条流水线只能是 manual 或 cron
    cron: str | None = None


class StepSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = ""          # 步骤名称，默认等于插件显示名，用户可改
    plugin: str
    with_: dict[str, Any] | None = Field(default=None, alias="with")


class VariableSpec(BaseModel):
    """流水线全局变量（蓝盾自定义变量，供整条流水线的步骤引用）"""
    name: str
    type: str = "text"                  # text / number / boolean / textarea / select / git_ref / code_lib / artifact / pool
    default_value: str = ""
    description: str = ""
    show_on_execution: bool = True      # 执行时是否在参数填写面板展示
    options: list[str] = []             # select 类型的候选项


class JobSpec(BaseModel):
    id: str | None = None
    name: str = ""
    agent: str = "linux"          # linux / windows / any
    steps: list[StepSpec] = []


class StageSpec(BaseModel):
    name: str = ""
    jobs: list[JobSpec] = []


class PipelineSpec(BaseModel):
    name: str = ""
    triggers: list[TriggerSpec] = []
    stages: list[StageSpec] = []
    variables: list[VariableSpec] = []   # 流水线级全局变量
    # 画布节点坐标，编辑页和执行页共用同一套摆放，不影响执行顺序
    canvas_layout: dict[str, Any] = Field(default_factory=dict)


class PipelineDefinition(BaseModel):
    pipeline: PipelineSpec


# ============================================================
# 前端可视化图形结构（React Flow 渲染用）
# ============================================================
class GraphTrigger(BaseModel):
    type: str = "manual"  # manual / cron，一条流水线只有一种
    cron: str | None = None
    label: str = ""


class GraphStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = ""          # 步骤名称，默认等于插件显示名，用户可改
    plugin: str
    display_name: str = ""
    icon: str = "🔧"
    order: int = 0
    with_: dict[str, Any] | None = Field(default=None, alias="with")
    built_in: bool = True


class GraphJob(BaseModel):
    id: str
    name: str
    agent: str = "linux"
    agent_icon: str = "🐧"
    steps: list[GraphStep] = []


class GraphStage(BaseModel):
    id: str
    name: str
    order: int = 0
    jobs: list[GraphJob] = []


class PipelineGraph(BaseModel):
    pipeline_id: int | None = None
    pipeline_name: str = ""
    version: int = 1
    triggers: list[GraphTrigger] = []
    stages: list[GraphStage] = []
    variables: list[VariableSpec] = []   # 流水线级全局变量
    layout: dict[str, Any] = Field(default_factory=dict)
    # 画布上已断开的顺序箭头。有值说明编排还不完整，保存应失败。
    open_cuts: list[str] = Field(default_factory=list)


# ============================================================
# YAML 解析器
# ============================================================
def parse_yaml(yaml_text: str) -> PipelineDefinition:
    """将 YAML 文本解析为结构化定义。"""
    if not yaml_text or not yaml_text.strip():
        return PipelineDefinition(pipeline=PipelineSpec())
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 解析失败: {e}")
    return PipelineDefinition.model_validate(data)


def dump_yaml(definition: PipelineDefinition) -> str:
    """将结构化定义序列化为 YAML（保留 with 别名）。"""
    data = definition.model_dump(by_alias=True, exclude_none=True)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
