"""发布实体解析：模型填槽，工具落到可见流水线，编造的 id 作废。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.modules.ai.context import resolve_release_target
from app.modules.ai.skills.delivery import _propose_release


def _catalog(monkeypatch, rows, projects) -> None:
    """把可见流水线和业务项目接到解析器上，不碰数据库。"""
    monkeypatch.setattr("app.modules.ai.context.visible_pipelines", lambda db, current: rows)
    monkeypatch.setattr("app.modules.ai.context.listed_business_projects", lambda db: projects)


def test_model_slots_project_env_resolve_without_id(monkeypatch) -> None:
    """别人说「陪练 + 生产」，不必套「执行X项目Y环境」句式。"""
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    pipe = SimpleNamespace(id=21, name="coach-prod", yaml="", project_id=9)
    prod = SimpleNamespace(id=3, name="生产", type="prod", approval_required=False)
    _catalog(monkeypatch, [(pipe, proj, prod)], [proj])
    found = resolve_release_target(MagicMock(), MagicMock(), project="陪练", env="生产")
    assert found["match"].id == 21
    assert found["grounded"] is True


def test_visible_fake_id_does_not_override_named_query(monkeypatch) -> None:
    """模型瞎填一个可见 id，不能压过用户原话里的项目名。"""
    dms = SimpleNamespace(id=1, name="DMS 经销商协同运营平台", code="COP")
    coach = SimpleNamespace(id=9, name="AI陪练", code="coach")
    dms_pipe = SimpleNamespace(id=12, name="cop-prod", yaml="", project_id=1)
    coach_pipe = SimpleNamespace(id=21, name="coach-prod", yaml="", project_id=9)
    prod = SimpleNamespace(id=3, name="生产", type="prod")
    _catalog(monkeypatch, [(dms_pipe, dms, prod), (coach_pipe, coach, prod)], [dms, coach])
    found = resolve_release_target(
        MagicMock(),
        MagicMock(),
        pipeline_id=12,
        query="执行AI陪练项目生产环境",
    )
    assert found["match"].id == 21


def test_bogus_pipeline_id_is_dropped_when_slots_match(monkeypatch) -> None:
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    pipe = SimpleNamespace(id=21, name="coach-prod", yaml="", project_id=9)
    prod = SimpleNamespace(id=3, name="生产", type="prod", approval_required=False)
    _catalog(monkeypatch, [(pipe, proj, prod)], [proj])
    found = resolve_release_target(
        MagicMock(),
        MagicMock(),
        pipeline_id=108,
        project="AI陪练",
        env="prod",
    )
    assert found["match"].id == 21


def test_yaml_copy_does_not_steal_display_name(monkeypatch) -> None:
    """显示名 test-B、YAML 仍写 test-C 的副本，不能和真正的 test-C 并列。"""
    proj = SimpleNamespace(id=1, name="demo", code="demo")
    a = SimpleNamespace(id=15, name="test-C", yaml="pipeline:\n  name: test-C\n", project_id=1)
    b = SimpleNamespace(id=47, name="test-B", yaml="pipeline:\n  name: test-C\n", project_id=1)
    group = SimpleNamespace(id=2, name="测试", type="test")
    _catalog(monkeypatch, [(a, proj, group), (b, proj, group)], [proj])
    found = resolve_release_target(MagicMock(), MagicMock(), query="发布 test-C 流水线")
    assert found["match"].id == 15


def test_bogus_id_alone_is_not_grounded(monkeypatch) -> None:
    _catalog(monkeypatch, [], [])
    found = resolve_release_target(MagicMock(), MagicMock(), pipeline_id=108)
    assert found.get("match") is None
    assert found["grounded"] is False
    assert "108" in found["error"]
    assert "不要编造" in found["error"]


def test_propose_release_treats_utterance_as_name_not_id(monkeypatch) -> None:
    """模型把整句塞进 pipeline_id 时，按项目名解析，不能 int() 炸掉。"""
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    pipe = SimpleNamespace(id=21, name="coach-prod", yaml="", project_id=9, approval_mode="inherit")
    prod = SimpleNamespace(id=3, name="生产", type="prod", approval_required=False)
    _catalog(monkeypatch, [(pipe, proj, prod)], [proj])
    monkeypatch.setattr("app.core.deps.check_permission", lambda *args, **kwargs: True)
    out = _propose_release(
        MagicMock(),
        MagicMock(),
        {"pipeline_id": "AI陪练项目生产环境", "query": "执行AI陪练项目生产环境"},
    )
    assert out.get("_action") == "confirm_release"
    assert out["payload"]["pipeline_id"] == 21
    assert "#21" in out["reply"]
    assert "流水线=#AI陪练" not in out["reply"]


def test_propose_release_accepts_slots(monkeypatch) -> None:
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    pipe = SimpleNamespace(id=21, name="coach-prod", yaml="", project_id=9, approval_mode="inherit")
    prod = SimpleNamespace(id=3, name="生产", type="prod", approval_required=False)
    _catalog(monkeypatch, [(pipe, proj, prod)], [proj])
    monkeypatch.setattr("app.core.deps.check_permission", lambda *args, **kwargs: True)
    out = _propose_release(MagicMock(), MagicMock(), {"project": "陪练", "env": "线上"})
    assert out.get("_action") == "confirm_release"
    assert out["payload"]["pipeline_id"] == 21


PACK_YAML = """
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


def test_propose_release_empty_manifest_refuses_confirm(monkeypatch) -> None:
    """增量包流水线没指定文件时不出确认卡，避免点确认变成全量上环境。"""
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    pipe = SimpleNamespace(id=50, name="msbuild", yaml=PACK_YAML, project_id=9, approval_mode="inherit")
    prod = SimpleNamespace(id=3, name="生产", type="prod", approval_required=True)
    _catalog(monkeypatch, [(pipe, proj, prod)], [proj])
    monkeypatch.setattr("app.core.deps.check_permission", lambda *args, **kwargs: True)
    out = _propose_release(MagicMock(), MagicMock(), {"pipeline_id": 50})
    assert out.get("_action") != "confirm_release"
    assert "deploy_manifest" not in (out.get("payload") or {})
    assert "必须先指定" in (out.get("error") or "")
    assert "全量打包" in (out.get("error") or "")


def test_propose_release_keeps_files_from_chat(monkeypatch) -> None:
    """对话里点名的文件要进确认卡，点确认时才能写进 DEPLOY_MANIFEST。"""
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    pipe = SimpleNamespace(id=50, name="msbuild", yaml=PACK_YAML, project_id=9, approval_mode="inherit")
    prod = SimpleNamespace(id=3, name="生产", type="prod", approval_required=True)
    _catalog(monkeypatch, [(pipe, proj, prod)], [proj])
    monkeypatch.setattr("app.core.deps.check_permission", lambda *args, **kwargs: True)
    out = _propose_release(
        MagicMock(),
        MagicMock(),
        {"pipeline_id": 50, "deploy_manifest": "bin/*.dll, Areas/"},
    )
    assert out["payload"]["deploy_manifest"] == "bin/*.dll\nAreas/"
    assert "bin/*.dll" in out["reply"]
    assert "本次发布清单" in out["reply"]

