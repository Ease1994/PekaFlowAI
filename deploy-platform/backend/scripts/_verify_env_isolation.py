"""自定义环境分组（UAT / 预发 / 开发）与环境隔离的回归验证。

改造前的硬伤是一句「分组 type 不是 test 就当 prod」：建一个 type=uat 的分组，
任务会被派到生产构建机、发到生产节点，制品还会被当成测试包隔夜清掉。
隔离、审批、留存三条线同时失效，而页面上看不出任何异常。

所以这里钉住的核心是一条：**环境码必须全等匹配，非法码一律拒绝，绝不默默当成生产。**

覆盖：
  1. 环境码表：内置码齐全，test/dev 免下发审批，其余要审
  2. normalize_env：空值取默认；非法码拒绝而不是改写成 prod
  3. pipeline_env_of：uat 原样返回；空 type 报错而不是当 prod
  4. uat 流水线不能用 prod 构建机 / prod 节点（反向同样不行）
  5. uat 流水线用 uat 构建机、uat 节点正常放行
  6. 建分组：合法自定义码接受，非法码拒绝；同项目重名、改名撞名都拒绝
  7. 建分组的审批默认值按环境走：uat 默认要审，dev 默认免审
  8. 改分组 type 同样校验，空值拒绝；有流水线（含回收站）不能改环境码
  9. 注册构建机：env=uat 原样保留，非法码拒绝
 10. 删分组：有流水线的拒绝（含回收站），空组放行
 11. 制品清理：uat 的包不会被当成测试包清掉
 12. 发布通知：uat 走站内信+邮件，test 只在失败时发站内信
 13. 新项目自动带生产 + 测试两个分组
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 硬覆盖：这个脚本会造数据，绝不能落到测试 MySQL 上
os.environ["DATABASE_URL"] = "sqlite:///./data/verify_env_isolation.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'fail'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def field(payload, name):
    """R.ok(...) 的 data 有时是 ORM 对象、有时已被序列化成 dict，两种都取得到。"""
    data = getattr(payload, "data", payload)
    if isinstance(data, dict):
        return data.get(name)
    return getattr(data, name, None)


def rejected(fn) -> str:
    """跑一个应当被拒绝的操作，返回错误文案；没拒绝则返回空串。"""
    from app.core.response import BizException

    try:
        fn()
    except BizException as e:
        return str(getattr(e, "message", None) or e)
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return ""


def main() -> int:  # noqa: C901
    import app.main  # noqa: F401  建表前把所有模型注册进 Base

    from datetime import datetime, timedelta

    from app.core import env as envmod
    from app.core.deps import CurrentUser
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.modules.agent.models import BuildAgent
    from app.modules.agent import task_service
    from app.modules.artifact.models import Artifact
    from app.modules.auth.models import User
    from app.modules.notify import center as notify_center
    from app.modules.pipeline.models import Pipeline
    from app.modules.project import router as project_router
    from app.modules.project.models import Group, Project

    if engine.dialect.name != "sqlite":
        print(f"拒绝执行：本脚本会造数据，只能跑在 sqlite 上，当前是 {engine.dialect.name}")
        return 2
    db_file = "./data/verify_env_isolation.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    Base.metadata.create_all(engine)

    # ---- 1. 环境码表 ----
    check("1a 内置码齐全", set(envmod.KNOWN) == {"prod", "test", "uat", "staging", "dev"},
          str(sorted(envmod.KNOWN)))
    check("1b test/dev 下发免审", envmod.skip_node_push_approval("test")
          and envmod.skip_node_push_approval("dev"))
    check("1c uat/staging/prod 下发要审",
          not any(envmod.skip_node_push_approval(x) for x in ("uat", "staging", "prod")))
    check("1d 未知自定义码要审（失败关闭）", not envmod.skip_node_push_approval("train"))
    check("1e env 为空按生产要审（老数据）", not envmod.skip_node_push_approval(""))
    check("1f uat 的包要长留，不隔夜清", not envmod.is_disposable("uat"))

    # ---- 2. normalize_env ----
    check("2a 空值取默认", envmod.normalize_env("", default="test") == "test")
    check("2b 合法自定义码原样保留", envmod.normalize_env("train") == "train")
    check("2c 大写归一", envmod.normalize_env("UAT") == "uat")
    msg = rejected(lambda: envmod.normalize_env("UAT!!"))
    check("2d 非法码被拒而不是改写成 prod", bool(msg), msg)
    msg = rejected(lambda: envmod.normalize_env("9uat"))
    check("2e 数字开头被拒", bool(msg), msg)

    # ---- 3. pipeline_env_of ----
    check("3a uat 原样返回", envmod.pipeline_env_of("uat") == "uat")
    msg = rejected(lambda: envmod.pipeline_env_of(""))
    check("3b 空 type 报错而不是当 prod", bool(msg), msg)
    msg = rejected(lambda: envmod.pipeline_env_of("生产"))
    check("3c 非法 type 报错而不是当 prod", bool(msg), msg)

    # ---- 造数据 ----
    with SessionLocal() as db:
        admin = User(username="admin", password_hash="x", display_name="管理员", is_admin=True)
        db.add(admin)
        db.flush()
        proj = Project(name="demo", code="demo")
        db.add(proj)
        db.flush()
        g_prod = Group(project_id=proj.id, name="生产", type="prod", approval_required=True)
        g_uat = Group(project_id=proj.id, name="UAT", type="uat", approval_required=True)
        g_test = Group(project_id=proj.id, name="测试", type="test", approval_required=False)
        db.add_all([g_prod, g_uat, g_test])
        db.flush()
        b_prod = BuildAgent(name="build-prod", host="10.0.0.1", os="linux",
                            tags="[]", role="builder", env="prod", status="online")
        b_uat = BuildAgent(name="build-uat", host="10.0.0.2", os="linux",
                           tags="[]", role="builder", env="uat", status="online")
        n_prod = BuildAgent(name="node-prod", host="10.0.0.3", os="linux",
                            tags="[]", role="node", env="prod", status="online",
                            last_heartbeat=datetime.now())
        n_uat = BuildAgent(name="node-uat", host="10.0.0.4", os="linux",
                           tags="[]", role="node", env="uat", status="online",
                           last_heartbeat=datetime.now())
        db.add_all([b_prod, b_uat, n_prod, n_uat])
        db.flush()
        p_uat = Pipeline(project_id=proj.id, group_id=g_uat.id, name="uat-line", yaml="",
                         status="active", created_by=admin.id, workspace_uuid="ws-uat")
        db.add(p_uat)
        db.commit()
        ids = {
            "proj": proj.id, "admin": admin.id,
            "g_prod": g_prod.id, "g_uat": g_uat.id, "g_test": g_test.id,
            "b_prod": b_prod.id, "b_uat": b_uat.id,
            "n_prod": n_prod.id, "n_uat": n_uat.id,
            "p_uat": p_uat.id,
        }

    cur = CurrentUser(id=ids["admin"], username="admin", is_admin=True)

    # ---- 4/5. 构建机与节点的环境隔离 ----
    with SessionLocal() as db:
        msg = rejected(lambda: task_service._resolve_builder(db, f"agent:{ids['b_prod']}", "uat"))
        check("4a uat 流水线不能用生产构建机", bool(msg), msg)
        check("4b 报错文案点名 UAT 和生产", "UAT" in msg and "生产" in msg, msg)

        msg = rejected(lambda: task_service._resolve_builder(db, f"agent:{ids['b_uat']}", "prod"))
        check("4c 生产流水线不能用 UAT 构建机", bool(msg), msg)

        steps = [{"plugin": "file-transfer", "with": {"node": ids["n_prod"], "targetDir": "/opt/app"}}]
        msg = rejected(lambda: task_service._assert_node_usable(db, ids["n_prod"], steps, "uat"))
        check("4d uat 流水线不能发到生产节点", bool(msg), msg)

        ok_builder = task_service._resolve_builder(db, f"agent:{ids['b_uat']}", "uat")
        check("5a uat 流水线用 uat 构建机放行", ok_builder == ids["b_uat"])

        steps = [{"plugin": "file-transfer", "with": {"node": ids["n_uat"], "targetDir": "/opt/app"}}]
        msg = rejected(lambda: task_service._assert_node_usable(db, ids["n_uat"], steps, "uat"))
        check("5b uat 流水线发到 uat 节点放行", msg == "", msg)

        msg = rejected(lambda: task_service._assert_env_has_builder(db, "staging"))
        check("5c 没有该环境构建机时明确报错", bool(msg), msg)

        # 老数据 env 为空：不能再当生产用，必须先在页面标明环境
        b_legacy = BuildAgent(name="build-legacy", host="10.0.0.9", os="linux",
                              tags="[]", role="builder", env="", status="online")
        db.add(b_legacy)
        db.flush()
        msg = rejected(lambda: task_service._assert_env_match(
            "构建机", "build-legacy", "", "prod"))
        check("5d 空 env 构建机不能跑生产任务", bool(msg), msg)
        msg = rejected(lambda: task_service._assert_env_match(
            "构建机", "build-legacy", "", "test"))
        check("5e 空 env 构建机不能跑测试任务", bool(msg), msg)
        msg = rejected(lambda: task_service._resolve_builder(db, f"agent:{b_legacy.id}", "prod"))
        check("5f 指名空 env 构建机不能跑生产任务", bool(msg), msg)
        msg = rejected(lambda: task_service._resolve_builder(db, f"agent:{b_legacy.id}", "test"))
        check("5f2 指名空 env 构建机不能跑测试任务", bool(msg), msg)

        # 平台节点下发：内置流水线分组是 prod，但不能因此卡死测试节点
        msg = rejected(lambda: task_service._assert_node_usable(
            db, ids["n_uat"], [{"plugin": "file-transfer", "with": {"targetDir": "/opt/app"}}],
            pipeline_env=None))
        check("5g 平台下发不拿分组 type 卡测试节点", msg == "", msg)
        msg = rejected(lambda: task_service._assert_node_usable(
            db, ids["n_uat"], [{"plugin": "file-transfer", "with": {"targetDir": "/opt/app"}}], "prod"))
        check("5h 用户生产流水线仍不能发到 UAT 节点", bool(msg), msg)

    # ---- 6/7. 建分组 ----
    with SessionLocal() as db:
        body = {"project_id": ids["proj"], "name": "预发", "type": "staging"}
        r = project_router.create_group(body, db, cur)
        check("6a 内置码 staging 可建", field(r, "type") == "staging")

        body = {"project_id": ids["proj"], "name": "沙箱", "type": "sandbox"}
        r = project_router.create_group(body, db, cur)
        check("6b 自定义码 sandbox 可建", field(r, "type") == "sandbox")

        msg = rejected(lambda: project_router.create_group(
            {"project_id": ids["proj"], "name": "坏的", "type": "UAT 环境"}, db, cur))
        check("6c 非法环境码被拒", bool(msg), msg)

        msg = rejected(lambda: project_router.create_group(
            {"project_id": ids["proj"], "name": "预发", "type": "dev"}, db, cur))
        check("6d 同项目重名分组被拒", bool(msg), msg)

        msg = rejected(lambda: project_router.update_group(
            ids["g_test"], {"name": "生产"}, db, cur))
        check("6e 改名撞上同项目已有分组被拒", bool(msg), msg)

        r = project_router.create_group(
            {"project_id": ids["proj"], "name": "验收", "type": "uat"}, db, cur)
        check("7a uat 分组默认强制审批", field(r, "approval_required") is True)
        r = project_router.create_group(
            {"project_id": ids["proj"], "name": "开发环境", "type": "dev"}, db, cur)
        check("7b dev 分组默认免审批", field(r, "approval_required") is False)

    # ---- 8. 改分组 type ----
    with SessionLocal() as db:
        r = project_router.update_group(ids["g_test"], {"type": "dev"}, db, cur)
        check("8a 改成 dev 生效", field(r, "type") == "dev")
        msg = rejected(lambda: project_router.update_group(ids["g_test"], {"type": ""}, db, cur))
        check("8b 改成空值被拒", bool(msg), msg)
        msg = rejected(lambda: project_router.update_group(
            ids["g_test"], {"type": "PROD!"}, db, cur))
        check("8c 改成非法码被拒", bool(msg), msg)
        msg = rejected(lambda: project_router.update_group(
            ids["g_uat"], {"type": "prod"}, db, cur))
        check("8d 有流水线的分组不能改环境码", bool(msg), msg)
        # 复原，免得影响后面的用例
        project_router.update_group(ids["g_test"], {"type": "test"}, db, cur)

    # ---- 9. 注册构建机带环境码 ----
    with SessionLocal() as db:
        check("9a 注册 env=uat 原样保留", envmod.normalize_env("uat", default="prod") == "uat")
        check("9b 注册不带 env 按 prod", envmod.normalize_env(None, default="prod") == "prod")
        msg = rejected(lambda: envmod.normalize_env("uat 环境", default="prod", field="--env"))
        check("9c 注册非法 env 被拒", bool(msg), msg)

    # ---- 10. 删分组 ----
    with SessionLocal() as db:
        msg = rejected(lambda: project_router.delete_group(ids["g_uat"], db, cur))
        check("10a 有流水线的分组不能删", bool(msg), msg)
        check("10b 报错提示怎么办", "挪到" in msg or "流水线" in msg, msg)

        empty = project_router.create_group(
            {"project_id": ids["proj"], "name": "待删", "type": "dev"}, db, cur)
        empty_id = field(empty, "id")
        project_router.delete_group(empty_id, db, cur)
        check("10c 空分组可以删", db.get(Group, empty_id) is None)

        from app.modules.pipeline import service as pipeline_service

        pipeline_service.delete_pipeline(db, ids["p_uat"], operator_id=ids["admin"])
        msg = rejected(lambda: project_router.delete_group(ids["g_uat"], db, cur))
        check("10d 回收站里的流水线也挡住删组", bool(msg), msg)

    # ---- 11. 制品清理不误伤 uat ----
    with SessionLocal() as db:
        from datetime import datetime, timedelta

        from app.modules.artifact import service as artifact_service

        old = datetime.now() - timedelta(days=1)
        a_uat = Artifact(pipeline_id=ids["p_uat"], name="uat.zip", storage_key="",
                         size_bytes=1, release_id=1, created_at=old)
        db.add(a_uat)
        db.commit()
        uat_artifact_id = a_uat.id
        artifact_service.purge_expired_artifacts(db, prod_retention_days=10, test_retention_days=0)
        check("11a uat 的包没被当成测试包隔夜清掉",
              db.get(Artifact, uat_artifact_id) is not None)

    # ---- 12. 发布通知渠道 ----
    check("12a uat 成功也发站内信+邮件",
          notify_center._release_channels("uat", "success") == ["in_app", "email"])
    check("12b staging 成功也发站内信+邮件",
          notify_center._release_channels("staging", "success") == ["in_app", "email"])
    check("12c test 成功不打扰", notify_center._release_channels("test", "success") == [])
    check("12d test 失败发站内信",
          notify_center._release_channels("test", "failed") == ["in_app"])

    # ---- 13. 新项目自带生产 + 测试 ----
    with SessionLocal() as db:
        from sqlalchemy import select

        r = project_router.create_project({"name": "新项目", "code": "newp"}, db, cur)
        groups = db.scalars(select(Group).where(Group.project_id == field(r, "id"))).all()
        types = sorted(g.type for g in groups)
        check("13a 新项目自动建生产+测试两组", types == ["prod", "test"], str(types))
        prod = next((g for g in groups if g.type == "prod"), None)
        check("13b 生产组默认强制审批", bool(prod) and prod.approval_required is True)

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
