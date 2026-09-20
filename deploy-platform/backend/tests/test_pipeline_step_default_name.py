# -*- coding: utf-8 -*-
"""编排图里步骤名称为空时，用插件显示名填上。"""
from __future__ import annotations

from app.modules.pipeline.graph import to_graph
from app.modules.pipeline.schemas import JobSpec, PipelineDefinition, PipelineSpec, StageSpec, StepSpec


def test_empty_step_name_defaults_to_plugin_display() -> None:
    definition = PipelineDefinition(
        pipeline=PipelineSpec(
            stages=[
                StageSpec(
                    name="构建",
                    jobs=[
                        JobSpec(
                            id="1-1",
                            steps=[StepSpec(plugin="pack-incremental")],
                        )
                    ],
                )
            ]
        )
    )
    graph = to_graph(definition, 1, "demo", 1)
    assert graph.stages[0].jobs[0].steps[0].name == "提取增量发布包"


def test_custom_step_name_kept() -> None:
    definition = PipelineDefinition(
        pipeline=PipelineSpec(
            stages=[
                StageSpec(
                    name="构建",
                    jobs=[
                        JobSpec(
                            id="1-1",
                            steps=[StepSpec(name="停应用池", plugin="pack-incremental")],
                        )
                    ],
                )
            ]
        )
    )
    graph = to_graph(definition, 1, "demo", 1)
    assert graph.stages[0].jobs[0].steps[0].name == "停应用池"
