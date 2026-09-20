# -*- coding: utf-8 -*-
"""手动执行时的发布清单：有 pack-incremental 才认。

空清单沿用步骤里写死的文件列表；步骤仍是占位且未填则拒绝，不会全量打包。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.modules.deploy.service import (
    MISSING_MANIFEST_MSG,
    VAR_MANIFEST,
    apply_execute_manifest,
    is_manifest_placeholder,
    is_unbounded_manifest,
    pack_incremental_manifest_of,
)


PLACEHOLDER_YAML = """
pipeline:
  name: msbuild
  stages:
    - name: 发布
      jobs:
        - name: pack
          steps:
            - name: 提取增量包
              plugin: pack-incremental
              with:
                sourceDir: _publish
                manifest: ${{DEPLOY_MANIFEST}}
"""

HARDCODED_YAML = """
pipeline:
  name: msbuild
  stages:
    - name: 发布
      jobs:
        - name: pack
          steps:
            - name: 提取增量包
              plugin: pack-incremental
              with:
                sourceDir: _publish
                manifest: |
                  bin/*.dll
                  Areas/
"""

PLAIN_YAML = """
pipeline:
  name: docker
  stages:
    - name: 构建
      jobs:
        - name: build
          steps:
            - name: 镜像
              plugin: docker-build
              with:
                image: app:latest
"""


def test_pack_incremental_manifest_none_without_plugin():
    """没有提取增量包步骤时，执行弹窗不该出现清单，后端也忽略。"""
    assert pack_incremental_manifest_of(SimpleNamespace(yaml=PLAIN_YAML)) is None
    assert pack_incremental_manifest_of(SimpleNamespace(yaml="")) is None


def test_pack_incremental_manifest_reads_step_text():
    """有插件时返回步骤里的清单原文，占位和写死的列表都原样拿出来。"""
    assert pack_incremental_manifest_of(SimpleNamespace(yaml=PLACEHOLDER_YAML)) == (
        "${{DEPLOY_MANIFEST}}"
    )
    text = pack_incremental_manifest_of(SimpleNamespace(yaml=HARDCODED_YAML))
    assert text is not None
    assert "bin/*.dll" in text
    assert "Areas/" in text


def test_placeholder_detects_var_only_and_real_list():
    """空、纯变量占位算「没写死」；有真实路径就不算。"""
    assert is_manifest_placeholder("") is True
    assert is_manifest_placeholder("${{DEPLOY_MANIFEST}}") is True
    assert is_manifest_placeholder("# 注释\n${{ DEPLOY_MANIFEST }}") is True
    assert is_manifest_placeholder("bin/*.dll\nAreas/") is False
    assert is_manifest_placeholder("${{DEPLOY_MANIFEST}}\nbin/*.dll") is False
    assert is_unbounded_manifest("**") is True
    assert is_unbounded_manifest("**/*") is True
    assert is_unbounded_manifest("bin/*.dll") is False
    assert is_unbounded_manifest("**\n!bin/*.pdb") is False


def test_apply_filled_manifest_overrides_placeholder():
    """弹窗填了清单，写入 DEPLOY_MANIFEST，执行时替换步骤占位。"""
    p = SimpleNamespace(yaml=PLACEHOLDER_YAML)
    out = apply_execute_manifest(p, {"FOO": "1"}, "bin/*.dll\nAreas/")
    assert out["FOO"] == "1"
    assert out[VAR_MANIFEST] == "bin/*.dll\nAreas/"


def test_apply_empty_uses_hardcoded_step_and_does_not_blank_it():
    """留空且步骤已写死文件列表：不注入 DEPLOY_MANIFEST，避免把写死内容替换成空。"""
    p = SimpleNamespace(yaml=HARDCODED_YAML)
    out = apply_execute_manifest(p, {"FOO": "1"}, "")
    assert VAR_MANIFEST not in out
    assert out["FOO"] == "1"


def test_apply_empty_placeholder_is_rejected():
    """留空且步骤仍是占位：拒绝执行，不会写入 ** 把编译产物全量打上去。"""
    from app.core.response import BizException

    p = SimpleNamespace(yaml=PLACEHOLDER_YAML)
    with pytest.raises(BizException) as ei:
        apply_execute_manifest(p, {}, "  \n")
    assert "全量打包" in ei.value.message
    assert ei.value.message == MISSING_MANIFEST_MSG


def test_apply_explicit_star_star_is_allowed():
    """本次执行明确写出 ** 才是整包发，和空清单不是一回事。"""
    p = SimpleNamespace(yaml=PLACEHOLDER_YAML)
    out = apply_execute_manifest(p, {}, "**")
    assert out[VAR_MANIFEST] == "**"


def test_apply_ignores_manifest_when_pipeline_has_no_plugin():
    """没有增量插件时丢掉弹窗传来的清单，不能误写入 run_params。"""
    p = SimpleNamespace(yaml=PLAIN_YAML)
    out = apply_execute_manifest(p, {"FOO": "1"}, "bin/*.dll")
    assert VAR_MANIFEST not in out
    assert out == {"FOO": "1"}


def test_rebuild_run_params_merges_manifest_and_keeps_old_when_omitted():
    """Rebuild 带了清单就覆盖 DEPLOY_MANIFEST；没带则原样沿用上次点名过的文件。"""
    import json

    from app.modules.pipeline.service import _rebuild_run_params

    src = SimpleNamespace(run_params_json='{"FOO":"1","DEPLOY_MANIFEST":"old.dll"}')
    p = SimpleNamespace(yaml=PLACEHOLDER_YAML)
    merged = json.loads(_rebuild_run_params(p, src, "bin/*.dll\nAreas/"))
    assert merged["FOO"] == "1"
    assert merged[VAR_MANIFEST] == "bin/*.dll\nAreas/"
    kept = json.loads(_rebuild_run_params(p, src, None))
    assert kept[VAR_MANIFEST] == "old.dll"


def test_rebuild_run_params_rejects_inherited_unbounded_manifest():
    """上次若是 **（含曾经自动注入的全量），Rebuild 不带新清单时必须拒绝。"""
    from app.core.response import BizException
    from app.modules.pipeline.service import _rebuild_run_params

    src = SimpleNamespace(run_params_json='{"DEPLOY_MANIFEST":"**"}')
    p = SimpleNamespace(yaml=PLACEHOLDER_YAML)
    with pytest.raises(BizException) as ei:
        _rebuild_run_params(p, src, None)
    assert "全量打包" in ei.value.message


def test_leftover_manifest_points_to_execute_dialog():
    """步骤里残留 DEPLOY_MANIFEST 时，提示去执行弹窗填写，而不是只说发布提交页。"""
    from app.core.response import BizException
    from app.modules.agent.task_service import _assert_no_leftover_vars

    with pytest.raises(BizException) as ei:
        _assert_no_leftover_vars("msbuild", "提取增量包", {"manifest": "${{DEPLOY_MANIFEST}}"})
    assert "发起发布" in ei.value.message
    assert "发布提交" in ei.value.message
