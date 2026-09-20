"""模板 zip 必须能被现有解析器收下，否则模板本身就是错的。"""
from __future__ import annotations

from app.modules.harness import skills, templates, tools
from app.modules.store.plugin_service import parse_task_json_from_zip


def test_skill_template_is_a_valid_skill_package() -> None:
    pkg = skills.parse_package(templates.skill_package())
    assert pkg.manifest.kind == "agent-skill"
    assert pkg.manifest.name == "release-checklist"
    assert "propose_release" in pkg.body


def test_plugin_template_has_task_json_and_sdk() -> None:
    data = templates.plugin_package()
    meta = parse_task_json_from_zip(data)
    assert meta["name"] == "hello-echo"
    assert "python" in meta["entrypoint"]
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "sign.py" in names
    assert any(name.startswith("release_atom_sdk/") for name in names)


def test_tool_template_manifest_matches_parser() -> None:
    import io
    import zipfile

    import yaml

    data = templates.tool_package()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        raw = yaml.safe_load(archive.read("manifest.yaml"))
        assert "tool.py" in names
        assert "sign.py" in names
        assert "manifest.sig" not in names
    tools.parse_tool_manifest(raw)
