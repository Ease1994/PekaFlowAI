"""审批后卡死问题的回归验证。

复现链条：AES_KEY 换了 → 仓库凭证解不开 → 拆构建任务抛异常 →
发布卡在 running 且零任务 → 审批接口报错、弹窗不关、用户反复点。

覆盖：
  1. 拆任务失败时发布收尾成 failed（不再卡在 running）
  2. 审批接口在执行失败时返回成功 + 原因（弹窗能关、不会被当成审批失败）
  3. 历史遗留的「running 但没有任务」发布，启动时被清理
  4. 旧密钥加密的凭证自动重新加密，链条恢复正常
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 必须硬覆盖：外面可能已经把 DATABASE_URL 指向测试 MySQL，
# 这个脚本会造数据、还会跑清理逻辑，绝不能落到真库上
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_approval.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


PIPELINE_YAML = """
pipeline:
  name: prd-c
  stages:
    - name: stage-1
      jobs:
        - name: build
          agent: windows
          steps:
            - name: git-checkout
              plugin: git-checkout
              with:
                repo: ssc/ssc.admin
"""

# 坏 YAML：解析就会失败，用来验证「还没来得及切 running 就抛异常」那条路径
BROKEN_YAML = """
pipeline:
  name: prd-c
  stages:
    - name: stage-1
      jobs:
        - name: build
          steps:
            - name: 没有 plugin 字段
"""


def main() -> int:  # noqa: C901
    import app.main  # noqa: F401  建表前先把所有模型都注册进 Base（通知、审计等都要）

    from app.core.config import _LEAKED_AES, settings
    from app.core.security import decrypt
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.agent.models import BuildAgent, BuildTask  # noqa: F401
    from app.modules.approval.models import Approval
    from app.modules.auth.models import Permission, User  # noqa: F401
    from app.modules.credential.models import Credential
    from app.modules.pipeline import service as pipeline_service
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.project.models import Group, Project
    from app.modules.repository.models import Repository

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：本脚本会造数据和清理数据，只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_approval.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

    # ---- 造数据：生产分组 + 需要凭证的仓库 + 引用它的流水线 ----
    with SessionLocal() as db:
        user = User(username="admin", password_hash="x", display_name="管理员", is_admin=True)
        db.add(user)
        db.flush()
        proj = Project(name="ssc", code="ssc")
        db.add(proj)
        db.flush()
        grp = Group(project_id=proj.id, name="生产", type="prod", approval_required=True)
        db.add(grp)
        db.flush()

        # 关键：凭证用「旧的、已泄露的默认密钥」加密，模拟升级前存下来的数据
        old_cipher, old_iv = _encrypt_with_legacy(_LEAKED_AES, "glpat-secret-token")
        cred = Credential(
            project_id=proj.id, name="ssc-token", type="token",
            ciphertext=old_cipher, iv=old_iv, created_by=user.id,
        )
        db.add(cred)
        db.flush()
        repo = Repository(
            project_id=proj.id, name="ssc.admin", alias="ssc/ssc.admin",
            url="https://git.example.com/ssc/ssc.admin.git",
            provider="gitlab", credential_id=cred.id,
        )
        db.add(repo)
        pipe = Pipeline(
            project_id=proj.id, group_id=grp.id, name="prd-c", yaml=PIPELINE_YAML,
            status="active", created_by=user.id, workspace_uuid="wsuuid",
        )
        db.add(pipe)
        # 第二条流水线：YAML 本身是坏的，解析阶段就抛——那时状态还是 queued
        pipe_bad = Pipeline(
            project_id=proj.id, group_id=grp.id, name="prd-bad", yaml=BROKEN_YAML,
            status="active", created_by=user.id, workspace_uuid="wsuuid2",
        )
        db.add(pipe_bad)
        db.add(BuildAgent(
            name="builder-prod", host="10.0.0.9", os="windows",
            role="builder", env="prod", status="online", token="t",
        ))
        db.commit()
        ids = {
            "user": user.id, "proj": proj.id, "grp": grp.id,
            "pipe": pipe.id, "pipe_bad": pipe_bad.id, "cred": cred.id,
        }

    # 当前进程用的是新密钥（settings 自动生成的），跟造数据用的旧密钥不同
    check(
        "前置：当前密钥确实解不开旧凭证",
        settings.aes_key != _LEAKED_AES and not _can_decrypt(db_cred(ids["cred"])),
        "复现了「凭证解密失败」",
    )

    # ---- 1. 审批通过 → 拆任务失败 → 发布不能卡在 running ----
    rid = _new_pending_release(ids)
    _add_approval(rid, ids["user"])
    with SessionLocal() as db:
        pipeline_service.approve_release(db, rid, True, "1", reviewer_id=ids["user"])
        err = ""
        try:
            pipeline_service.execute_release(db, rid)
        except Exception as e:  # noqa: BLE001
            err = str(e)
    with SessionLocal() as db:
        r = db.get(Release, rid)
        ntasks = db.query(BuildTask).filter(BuildTask.release_id == rid).count()
        check(
            "拆任务失败后发布收尾成 failed",
            r.status == "failed" and ntasks == 0,
            f"status={r.status} tasks={ntasks}",
        )
        check("失败原因写进了发布日志", "发布未能启动" in (r.logs or ""), (r.logs or "")[-80:].strip())
    check("异常照常抛给调用方", "凭证" in err or "解密" in err, err[:70])

    # ---- 1b. 更早的失败（YAML 解析）：那时状态还是 queued，同样不能留着 ----
    rid_bad = _new_pending_release(ids, pipeline_key="pipe_bad")
    _add_approval(rid_bad, ids["user"])
    with SessionLocal() as db:
        pipeline_service.approve_release(db, rid_bad, True, "1", reviewer_id=ids["user"])
        try:
            pipeline_service.execute_release(db, rid_bad)
        except Exception:  # noqa: BLE001
            pass
    with SessionLocal() as db:
        r = db.get(Release, rid_bad)
        check("YAML 解析失败的发布不会留在 queued", r.status == "failed", f"status={r.status}")
    with SessionLocal() as db:
        blocked2 = ""
        try:
            pipeline_service.ensure_pipeline_idle(db, ids["pipe_bad"])
        except Exception as e:  # noqa: BLE001
            blocked2 = str(e)
    check("坏流水线没被那条发布占住", not blocked2, blocked2[:70])

    # ---- 2. 流水线没被占住：还能再发起下一次 ----
    blocked = ""
    with SessionLocal() as db:
        try:
            pipeline_service.ensure_pipeline_idle(db, ids["pipe"])
        except Exception as e:  # noqa: BLE001
            blocked = str(e)
    check("失败的发布没有把流水线占住", not blocked, blocked[:70])

    # ---- 3. 审批接口：执行失败不报成审批失败 ----
    rid2 = _new_pending_release(ids)
    _add_approval(rid2, ids["user"])
    with SessionLocal() as db:
        payload = _decide_via_router(db, rid2, ids["user"])
    check(
        "审批接口返回成功而不是抛错",
        payload["code"] == 0,
        f"code={payload['code']}",
    )
    check(
        "接口如实说明发布没起来",
        "未能启动" in payload["message"],
        payload["message"][:70],
    )
    with SessionLocal() as db:
        a = db.query(Approval).filter(Approval.release_id == rid2).first()
        check("审批单已落成 approved（不会被反复点）", a.status == "approved", a.status)

    # ---- 4. 历史卡死的发布，启动时清理 ----
    with SessionLocal() as db:
        stranded = Release(
            pipeline_id=ids["pipe"], group_id=ids["grp"], version="stuck",
            status="running", operator_id=ids["user"],
        )
        db.add(stranded)
        db.commit()
        stranded_id = stranded.id
    from app.main import _rescue_stranded_releases

    _rescue_stranded_releases()
    with SessionLocal() as db:
        check("卡死的历史发布被清理成 failed", db.get(Release, stranded_id).status == "failed")

    # ---- 5. 凭证迁移：旧密文自动换成新密钥，链条恢复 ----
    from app.main import _migrate_legacy_encrypted_credentials

    _migrate_legacy_encrypted_credentials()
    check("旧凭证已用新密钥重新加密", _can_decrypt(db_cred(ids["cred"])))
    with SessionLocal() as db:
        c = db.get(Credential, ids["cred"])
        check("重新加密后明文没变", decrypt(c.ciphertext, c.iv) == "glpat-secret-token")

    rid3 = _new_pending_release(ids)
    _add_approval(rid3, ids["user"])
    with SessionLocal() as db:
        pipeline_service.approve_release(db, rid3, True, "1", reviewer_id=ids["user"])
        pipeline_service.execute_release(db, rid3)
    with SessionLocal() as db:
        r = db.get(Release, rid3)
        ntasks = db.query(BuildTask).filter(BuildTask.release_id == rid3).count()
        check(
            "凭证修好后审批通过能正常拆出构建任务",
            r.status == "running" and ntasks > 0,
            f"status={r.status} tasks={ntasks}",
        )
        task = db.query(BuildTask).filter(BuildTask.release_id == rid3).first()
        check("构建任务里带上了解密后的 token", "glpat-secret-token" in (task.steps_json or ""))

    # 迁移是幂等的：再跑一次不该动任何东西
    _migrate_legacy_encrypted_credentials()
    with SessionLocal() as db:
        c = db.get(Credential, ids["cred"])
        check("迁移可重复执行", decrypt(c.ciphertext, c.iv) == "glpat-secret-token")

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


# ---- 小工具 ----
def _encrypt_with_legacy(key: str, plain: str) -> tuple[str, str]:
    """用指定密钥加密，模拟换密钥之前存下的密文。"""
    import base64
    import hashlib

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    k = hashlib.sha256(key.encode()).digest()
    iv = hashlib.md5(key.encode()).digest()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(k), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode(), base64.b64encode(iv).decode()


def db_cred(cred_id: int):
    from app.db.session import SessionLocal
    from app.modules.credential.models import Credential

    with SessionLocal() as db:
        c = db.get(Credential, cred_id)
        return (c.ciphertext, c.iv)


def _can_decrypt(pair) -> bool:
    from app.core.security import decrypt

    try:
        decrypt(pair[0], pair[1])
        return True
    except Exception:  # noqa: BLE001
        return False


def _new_pending_release(ids: dict, pipeline_key: str = "pipe") -> int:
    from app.db.session import SessionLocal
    from app.modules.pipeline.models import Release

    with SessionLocal() as db:
        r = Release(
            pipeline_id=ids[pipeline_key], group_id=ids["grp"], version="v1",
            status="pending", operator_id=ids["user"],
        )
        db.add(r)
        db.commit()
        return r.id


def _add_approval(release_id: int, approver_id: int) -> None:
    from app.db.session import SessionLocal
    from app.modules.approval.models import Approval

    with SessionLocal() as db:
        db.add(Approval(release_id=release_id, approver_id=approver_id, status="pending"))
        db.commit()


def _decide_via_router(db, release_id: int, user_id: int) -> dict:
    """走真实的 /approvals/{id}/decide 处理函数，验证它的返回信封。"""
    import json

    from app.modules.approval.models import Approval
    from app.modules.approval.router import decide
    from app.modules.auth.models import User

    a = db.query(Approval).filter(
        Approval.release_id == release_id, Approval.status == "pending"
    ).first()
    user = db.get(User, user_id)
    current = type("C", (), {"id": user.id, "username": user.username, "is_admin": True})()
    resp = decide(a.id, {"approved": True, "comment": "1"}, db=db, current=current)
    if hasattr(resp, "body"):  # JSONResponse
        return json.loads(resp.body)
    return {"code": resp.code, "message": resp.message, "data": resp.data}


if __name__ == "__main__":
    raise SystemExit(main())
