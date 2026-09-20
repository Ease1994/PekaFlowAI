"""隔离工具链路：签名、能力 broker、fail-closed 执行与 Skill 包解析。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.audit.models import AuditLog
from app.modules.harness import broker, runner, signing, skills, tools
from app.modules.harness.models import (
    HarnessComponent,
    HarnessDependency,
    HarnessLifecycleAudit,
    HarnessRuntime,
    HarnessToolInvocation,
    HarnessVersion,
)
from app.modules.settings.models import PlatformSetting

TOOL_MANIFEST = {
    "kind": "agent-tool",
    "name": "jira-lookup",
    "version": "1.0.0",
    "display_name": "Jira 查询",
    "description": "按单号查询 Jira",
    "capabilities": ["pipeline:read"],
    "config_schema": {"type": "object", "properties": {"issue": {"type": "string"}}},
    "metadata": {
        "entrypoint_argv": ["python", "tool.py"],
        "output_schema": {"type": "object"},
        "method": "lookup",
    },
}


def _memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        HarnessComponent.__table__,
        HarnessVersion.__table__,
        HarnessDependency.__table__,
        HarnessRuntime.__table__,
        HarnessToolInvocation.__table__,
        HarnessLifecycleAudit.__table__,
        PlatformSetting.__table__,
        AuditLog.__table__,
    ):
        table.create(engine)
    return Session(engine)


def _register_key(db: Session, key_id: str, private_key: Ed25519PrivateKey) -> None:
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    db.add(
        PlatformSetting(
            key=signing.SETTING_KEY,
            value=json.dumps({key_id: base64.b64encode(raw).decode()}),
        )
    )
    db.commit()


def _tool_zip(manifest: dict, private_key: Ed25519PrivateKey | None, key_id: str) -> bytes:
    """按发布方的打包方式生成签名包：先算内容摘要，再把签名写进 manifest.sig。"""
    manifest_text = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=True)
    entries = {"manifest.yaml": manifest_text, "tool.py": "print('{}')\n"}
    unsigned = io.BytesIO()
    with zipfile.ZipFile(unsigned, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
    with zipfile.ZipFile(io.BytesIO(unsigned.getvalue())) as archive:
        digest = signing.content_digest(archive)
    signature = ""
    if private_key is not None:
        signature = base64.b64encode(
            private_key.sign(signing.signing_payload(manifest, digest))
        ).decode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
        archive.writestr("manifest.sig", json.dumps({"key_id": key_id, "signature": signature}))
    return buffer.getvalue()


def test_signature_rejects_tampered_manifest() -> None:
    private_key = Ed25519PrivateKey.generate()
    with _memory_db() as db:
        _register_key(db, "release-team", private_key)
        digest = "a" * 64
        signature = base64.b64encode(
            private_key.sign(signing.signing_payload(TOOL_MANIFEST, digest))
        ).decode()
        assert signing.verify(db, TOOL_MANIFEST, digest, signature, "release-team").verified

        tampered = {**TOOL_MANIFEST, "capabilities": ["pipeline:read", "release:read"]}
        assert not signing.verify(db, tampered, digest, signature, "release-team").verified
        # 换个包体，同一份签名同样不能复用
        assert not signing.verify(db, TOOL_MANIFEST, "b" * 64, signature, "release-team").verified
        assert not signing.verify(db, TOOL_MANIFEST, digest, signature, "unknown").verified


def test_tool_package_requires_registered_signer() -> None:
    from app.modules.harness import packaging

    private_key = Ed25519PrivateKey.generate()
    with _memory_db() as db:
        data = _tool_zip(TOOL_MANIFEST, private_key, "release-team")
        with pytest.raises(BizException, match="签名"):
            packaging.parse_tool_package(db, data)
        _register_key(db, "release-team", private_key)
        manifest, digest, key_id = packaging.parse_tool_package(db, data)
        assert manifest.name == "jira-lookup"
        assert key_id == "release-team"
        assert digest == hashlib.sha256(data).hexdigest()


def test_manifest_rejects_undeclared_capability() -> None:
    with pytest.raises(BizException, match="未知能力"):
        tools.parse_tool_manifest({**TOOL_MANIFEST, "capabilities": ["database:write"]})


def test_capability_token_is_scoped_to_call() -> None:
    import time

    permit = broker.grant("call-1", 7, ["pipeline:read"])
    verified = broker.verify(permit.token)
    assert verified.call_id == "call-1"
    assert verified.capabilities == ("pipeline:read",)
    with pytest.raises(BizException, match="签名不匹配"):
        broker.verify(permit.token[:-4] + "0000")
    expired = broker.CapabilityGrant("call-2", 7, ("pipeline:read",), int(time.time()) - 1)
    with pytest.raises(BizException, match="过期"):
        broker.verify(expired.token)


def test_broker_denies_capability_outside_grant() -> None:
    permit = broker.grant("call-3", 7, ["pipeline:read"])
    with _memory_db() as db:
        with pytest.raises(BizException, match="未授予能力"):
            broker.invoke(db, permit.token, "release:read", {})


def test_third_party_tool_fails_closed_without_runner(monkeypatch) -> None:
    spec = tools.ToolSpec(
        name="jira-lookup",
        description="",
        parameters={},
        source="package",
        isolation=tools.PACKAGE_ISOLATION,
        package_path="harness-tools/jira-lookup-1.0.0.zip",
        package_sha256="a" * 64,
        entrypoint=("python", "tool.py"),
        method="lookup",
    )

    def unavailable(_db):
        raise runner.RunnerUnavailable("未配置隔离 Runner")

    monkeypatch.setattr(runner, "load_config", unavailable)
    with _memory_db() as db:
        result = tools._dispatch_package(db, spec, {}, type("U", (), {"id": 1})(), "call-4")
    assert result["code"] == "RUNNER_UNAVAILABLE"
    assert "拒绝执行" in result["error"]


def test_tool_invocations_are_recorded_with_metrics() -> None:
    with _memory_db() as db:
        current = type("U", (), {"id": 3})()
        result = tools.dispatch(db, "no-such-tool", {}, current)
        assert result["code"] == "TOOL_NOT_FOUND"
        rows = db.scalars(select(HarnessToolInvocation)).all()
        assert [row.success for row in rows] == [False]
        summary = tools.metrics(db)
        assert summary[0]["tool_name"] == "no-such-tool"
        assert summary[0]["success_rate"] == 0.0


def _skill_zip(policy: str = "model") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "manifest.yaml",
            yaml.safe_dump(
                {
                    "kind": "agent-skill",
                    "name": "release-runbook",
                    "version": "1.0.0",
                    "display_name": "发布手册",
                    "description": "发布流程说明",
                    "metadata": {"invocation_policy": policy},
                },
                allow_unicode=True,
            ),
        )
        archive.writestr("SKILL.md", "# 发布手册\n先看流水线状态。")
        archive.writestr("resources/checklist.md", "1. 看日志")
    return buffer.getvalue()


def test_skill_package_parsing_and_policy_validation() -> None:
    package = skills.parse_package(_skill_zip())
    assert package.manifest.name == "release-runbook"
    assert "发布手册" in package.body
    assert "resources/checklist.md" in package.resources
    with pytest.raises(BizException, match="invocation_policy"):
        skills.parse_package(_skill_zip("whenever"))


def test_disabled_skill_never_reaches_the_model() -> None:
    with _memory_db() as db:
        component = skills.install_package(db, _skill_zip("always"))
        assert "先看流水线状态" in skills.system_prompt(db)
        assert "release-runbook" in skills.system_prompt(db)

        from app.modules.harness import lifecycle

        lifecycle.set_enabled(db, component.id, False)
        prompt = skills.system_prompt(db)
        assert "先看流水线状态" not in prompt
        assert "release-runbook" not in prompt


def test_model_policy_exposes_catalog_not_body() -> None:
    with _memory_db() as db:
        skills.install_package(db, _skill_zip("model"))
        prompt = skills.system_prompt(db)
        assert "`release-runbook`" in prompt
        assert "先看流水线状态" not in prompt
        loaded = skills.load_for_model(db, "release-runbook")
        assert "先看流水线状态" in loaded["skill_content"]


def test_user_policy_hidden_until_selected() -> None:
    with _memory_db() as db:
        skills.install_package(db, _skill_zip("user"))
        assert "release-runbook" not in skills.system_prompt(db)
        assert skills.load_for_model(db, "release-runbook").get("code") == "SKILL_NOT_FOUND"
        prompt = skills.system_prompt(db, selected=["release-runbook"])
        assert "先看流水线状态" in prompt
        assert "已加载" in prompt


def test_export_installed_skill_roundtrip() -> None:
    """已安装技能能打回可解析的 zip，方便改 name 再上传。"""
    with _memory_db() as db:
        component = skills.install_package(db, _skill_zip("model"))
        version = db.get(HarnessVersion, component.current_version_id)
        filename, data = skills.export_installed_skill(component, version)
        assert filename.startswith("release-runbook-")
        again = skills.parse_package(data)
        assert again.manifest.name == "release-runbook"
        assert "先看流水线状态" in again.body


def test_builtin_tool_exports_parseable_skill_zip() -> None:
    """内置工具导出的技能包能被同一套解析器收下。"""
    from app.modules.ai.skills import load_all

    load_all()
    with _memory_db() as db:
        spec = tools.resolve(db, "list_push_nodes")
        assert spec is not None
        filename, data = skills.export_builtin_tool_as_skill(spec)
        package = skills.parse_package(data)
        assert filename == "list_push_nodes-skill-1.0.0.zip"
        assert package.manifest.kind == "agent-skill"
        assert package.manifest.name == "list_push_nodes-skill"
        assert "list_push_nodes" in package.body
