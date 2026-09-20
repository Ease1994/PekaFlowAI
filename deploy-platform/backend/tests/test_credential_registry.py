"""镜像仓库凭证：账号密码分栏存储，流水线步骤执行时注入 docker login。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.core.security import encrypt
from app.db.base import Base
from app.modules.credential.models import Credential
from app.modules.credential.service import (
    docker_login,
    encode_secret,
    inject_registry_login,
    inject_ssh_login,
)


PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def test_encode_secret_password_roundtrip():
    plain = encode_secret(cred_type="password", username="swr_user", password="swr_pass")
    assert json.loads(plain) == {"username": "swr_user", "password": "swr_pass"}
    assert docker_login(plain, "password") == ("swr_user", "swr_pass")


def test_encode_secret_password_requires_both():
    with pytest.raises(BizException):
        encode_secret(cred_type="password", username="a", password="")
    with pytest.raises(BizException):
        encode_secret(cred_type="password", username="", password="x")


def test_docker_login_legacy_plain():
    """改版前账号密码只填了一框，整段当密码。"""
    assert docker_login("old-secret", "password") == ("", "old-secret")


def test_docker_login_token_keeps_password_only():
    assert docker_login("glpat-xxx", "token") == ("", "glpat-xxx")


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Credential.__table__])
    return Session(engine)


def _cred(db: Session, *, name: str, cred_type: str, plain: str, project_id: int | None) -> Credential:
    cipher, iv = encrypt(plain)
    row = Credential(
        name=name,
        type=cred_type,
        ciphertext=cipher,
        iv=iv,
        project_id=project_id,
        description="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_inject_password_credential_into_step():
    db = _db()
    plain = encode_secret(cred_type="password", username="acr_user", password="acr_pwd")
    cred = _cred(db, name="阿里云 ACR", cred_type="password", plain=plain, project_id=7)
    step = {"image": "registry.cn-hangzhou.aliyuncs.com/app", "registryCredentialId": cred.id}
    inject_registry_login(db, step, project_id=7)
    assert step["registryUser"] == "acr_user"
    assert step["registryPassword"] == "acr_pwd"


def test_inject_update_keeps_secret_when_empty():
    """未选凭证时不改步骤里手填的用户名密码。"""
    db = _db()
    step = {"registryUser": "hand", "registryPassword": "filled"}
    inject_registry_login(db, step, project_id=1)
    assert step["registryUser"] == "hand"
    assert step["registryPassword"] == "filled"


def test_inject_rejects_other_project_credential():
    db = _db()
    plain = encode_secret(cred_type="password", username="u", password="p")
    cred = _cred(db, name="别的项目", cred_type="password", plain=plain, project_id=2)
    step = {"registryCredentialId": cred.id}
    with pytest.raises(BizException) as exc:
        inject_registry_login(db, step, project_id=1)
    assert "不属于当前项目" in str(exc.value)


def test_inject_global_credential_allowed():
    db = _db()
    plain = encode_secret(cred_type="password", username="global_u", password="global_p")
    cred = _cred(db, name="华为云 SWR", cred_type="password", plain=plain, project_id=None)
    step = {"registryCredentialId": str(cred.id)}
    inject_registry_login(db, step, project_id=9)
    assert step["registryUser"] == "global_u"
    assert step["registryPassword"] == "global_p"


def test_inject_rejects_ssh():
    db = _db()
    cred = _cred(db, name="部署密钥", cred_type="ssh", plain="-----BEGIN OPENSSH PRIVATE KEY-----", project_id=None)
    step = {"registryCredentialId": cred.id}
    with pytest.raises(BizException) as exc:
        inject_registry_login(db, step, project_id=1)
    assert "SSH" in str(exc.value)


def test_docker_plugins_expose_credential_field():
    for name in ("docker-build", "docker-compile", "docker-deploy"):
        meta = json.loads((PLUGINS / name / "task.json").read_text(encoding="utf-8"))
        keys = [f["key"] for f in meta["config_schema"]["fields"]]
        assert "registryCredentialId" in keys, name
        field = next(f for f in meta["config_schema"]["fields"] if f["key"] == "registryCredentialId")
        assert field["source"] == "credential"
        assert keys.index("registryCredentialId") < keys.index("registryUser")


def test_inject_ssh_private_key():
    db = _db()
    cred = _cred(db, name="发布机密钥", cred_type="ssh", plain="-----BEGIN OPENSSH PRIVATE KEY-----\nabc", project_id=3)
    step = {"host": "192.0.2.10", "credentialId": cred.id, "user": "root"}
    inject_ssh_login(db, step, project_id=3)
    assert "BEGIN OPENSSH" in step["sshPrivateKey"]
    assert "sshPassword" not in step


def test_inject_ssh_password_fills_username():
    db = _db()
    plain = encode_secret(cred_type="password", username="deploy", password="s3cret")
    cred = _cred(db, name="发布机密码", cred_type="password", plain=plain, project_id=None)
    step = {"credentialId": cred.id}
    inject_ssh_login(db, step, project_id=1)
    assert step["sshUsername"] == "deploy"
    assert step["sshPassword"] == "s3cret"


def test_inject_ssh_rejects_token():
    db = _db()
    cred = _cred(db, name="GitLab", cred_type="token", plain="glpat-xxx", project_id=None)
    step = {"credentialId": cred.id}
    with pytest.raises(BizException) as exc:
        inject_ssh_login(db, step, project_id=1)
    assert "Token" in str(exc.value)


def test_ssh_deploy_schema_uses_ssh_credential():
    meta = json.loads((PLUGINS / "ssh-deploy" / "task.json").read_text(encoding="utf-8"))
    field = next(f for f in meta["config_schema"]["fields"] if f["key"] == "credentialId")
    assert field["source"] == "ssh-credential"

