"""流水线编辑器视图字段：只接受 form / canvas。"""
from __future__ import annotations

import pytest

from app.core.response import BizException
from app.modules.pipeline.service import assert_editor_view


def test_assert_editor_view_accepts_form_and_canvas() -> None:
    assert assert_editor_view("form") == "form"
    assert assert_editor_view("canvas") == "canvas"
    assert assert_editor_view(None) == "form"
    assert assert_editor_view("  canvas  ") == "canvas"


def test_assert_editor_view_rejects_unknown() -> None:
    with pytest.raises(BizException):
        assert_editor_view("graph")


def test_attach_stage_ids_matches_yaml_order() -> None:
    from app.modules.pipeline.graph import attach_stage_ids

    stages = [{"name": "构建"}, {"name": "发布"}]
    yaml_stages = [{"id": "stage-1", "name": "构建"}, {"id": "stage-2", "name": "发布"}]
    out = attach_stage_ids(stages, yaml_stages)
    assert [s["id"] for s in out] == ["stage-1", "stage-2"]
