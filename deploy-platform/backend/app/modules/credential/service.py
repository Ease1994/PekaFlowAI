"""凭证明文编解码，以及流水线步骤引用镜像仓库凭证时的注入。

token / ssh 整段加密存储。账号密码存成 JSON，docker login 时再拆成用户名和密码。
旧的账号密码若还是整段密文，当作只有密码、没有用户名。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.response import BizException
from app.core.security import decrypt
from app.modules.credential.models import Credential

# 账号密码在密文里的字段名。
_USER_KEY = "username"
_PASS_KEY = "password"

# 步骤 with 里存的是凭证 id；执行时再解密成 docker login 用户名密码。
REGISTRY_CREDENTIAL_KEY = "registryCredentialId"
# ssh-deploy 步骤只存凭证 id，私钥/密码在下发任务时注入，不进流水线 YAML。
SSH_CREDENTIAL_KEY = "credentialId"


def encode_secret(
    *,
    cred_type: str,
    secret: str = "",
    username: str = "",
    password: str = "",
) -> str:
    """把表单收成待加密的明文。账号密码必须成对；token/ssh 用 secret 整段。"""
    kind = (cred_type or "token").strip() or "token"
    if kind == "password":
        user = (username or "").strip()
        pwd = password or ""
        if not user or not pwd:
            raise BizException.bad_request("账号密码类型请填写用户名和密码")
        return json.dumps({_USER_KEY: user, _PASS_KEY: pwd}, ensure_ascii=False)
    text = secret or ""
    if not str(text).strip():
        raise BizException.bad_request("缺少凭证内容")
    return text


def docker_login(plain: str, cred_type: str) -> tuple[str, str]:
    """解密后的明文拆成 docker login 的用户名和密码。

    账号密码存 JSON；对不上时把整段当密码（兼容改版前只填了一框的数据）。
    Token 类型没有单独用户名，整段当密码，用户名留给步骤里手填的。
    """
    kind = (cred_type or "token").strip()
    raw = plain or ""
    if kind == "password":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and _PASS_KEY in data:
            return str(data.get(_USER_KEY) or "").strip(), str(data.get(_PASS_KEY) or "")
        return "", raw
    return "", raw


def inject_registry_login(
    db: Session,
    step_with: dict,
    *,
    project_id: int | None,
) -> None:
    """步骤选了镜像仓库凭证时，把解密后的账号密码写入 registryUser / registryPassword。

    流水线 YAML 只存凭证 id，密码不进配置。凭证必须是本项目的或全局的。
    账号密码类型覆盖用户名和密码；Token 类型只覆盖密码，用户名保留步骤里手填的。
    """
    raw_id = step_with.get(REGISTRY_CREDENTIAL_KEY)
    if raw_id in (None, "", 0, "0"):
        return
    try:
        cred_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise BizException.bad_request("镜像仓库凭证无效，请重新选择") from exc

    cred = db.get(Credential, cred_id)
    if cred is None:
        raise BizException.bad_request("所选镜像仓库凭证不存在，请到「凭证管理」重新选择")

    if cred.project_id is not None and cred.project_id != project_id:
        raise BizException.bad_request(
            f"凭证「{cred.name}」不属于当前项目，不能用于这条流水线"
        )

    kind = (cred.type or "token").strip()
    if kind == "ssh":
        raise BizException.bad_request(
            f"凭证「{cred.name}」是 SSH 密钥，不能用于 docker login。请改选账号密码或 Token"
        )

    try:
        plain = decrypt(cred.ciphertext, cred.iv)
    except Exception as exc:  # noqa: BLE001
        raise BizException.bad_request(
            f"凭证「{cred.name}」解密失败，请到「凭证管理」重新录入"
        ) from exc

    user, password = docker_login(plain, kind)
    if user:
        step_with["registryUser"] = user
    step_with["registryPassword"] = password


def inject_ssh_login(
    db: Session,
    step_with: dict,
    *,
    project_id: int | None,
) -> None:
    """ssh-deploy 选了凭证后，把解密后的私钥或密码写进步骤参数。

    YAML 只存 credentialId。凭证必须是本项目的或全局的。
    SSH 类型注入 sshPrivateKey；账号密码注入 sshPassword，必要时补 sshUsername。
    """
    raw_id = step_with.get(SSH_CREDENTIAL_KEY)
    if raw_id in (None, "", 0, "0"):
        return
    try:
        cred_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise BizException.bad_request("SSH 凭证无效，请重新选择") from exc

    cred = db.get(Credential, cred_id)
    if cred is None:
        raise BizException.bad_request("所选 SSH 凭证不存在，请到「凭证管理」重新选择")

    if cred.project_id is not None and cred.project_id != project_id:
        raise BizException.bad_request(
            f"凭证「{cred.name}」不属于当前项目，不能用于这条流水线"
        )

    kind = (cred.type or "token").strip()
    if kind == "token":
        raise BizException.bad_request(
            f"凭证「{cred.name}」是 Token，不能用于 SSH。请改选 SSH 私钥或账号密码"
        )

    try:
        plain = decrypt(cred.ciphertext, cred.iv)
    except Exception as exc:  # noqa: BLE001
        raise BizException.bad_request(
            f"凭证「{cred.name}」解密失败，请到「凭证管理」重新录入"
        ) from exc

    if kind == "ssh":
        step_with["sshPrivateKey"] = plain
        return
    user, password = docker_login(plain, kind)
    if user and not str(step_with.get("user") or "").strip():
        step_with["sshUsername"] = user
    step_with["sshPassword"] = password
