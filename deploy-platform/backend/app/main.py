"""FastAPI 应用入口。

聚合所有业务模块路由，统一挂载到 /api/v1 前缀，
并处理全局异常、CORS、启动初始化（建表 + 种子数据）。
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.openapi_tags import OPENAPI_TAGS
from app.core.response import BizException, R
from app.modules.agent.router import router as agent_router
from app.modules.settings.router import router as settings_router
from app.modules.ai.router import router as ai_router
from app.modules.approval.router import router as approval_router
from app.modules.artifact.router import router as artifact_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.auth.permission_router import router as permission_router
from app.modules.auth.role_router import router as role_router
from app.modules.auth.token_router import router as token_router
from app.modules.auth.wecom_router import router as wecom_router
from app.modules.credential.router import router as credential_router
from app.modules.deploy.router import router as deploy_request_router
from app.modules.metric.router import router as metric_router
from app.modules.notify.router import router as notify_router
from app.modules.pipeline.router import router as pipeline_router
from app.modules.project.router import router as project_router
from app.modules.repository.router import router as repository_router
from app.modules.store.router import router as store_router
from app.modules.llm.router import router as llm_router
from app.modules.access.router import router as access_router
from app.modules.account.router import router as account_router
from app.modules.harness.router import router as harness_router
from app.modules.pm.router import router as pm_router
from app.modules.health.router import router as health_router
from app.modules.meta.router import router as meta_router

# 业务模块路由（都带 /v1 前缀）
module_routers = [
    auth_router,
    permission_router,
    role_router,
    token_router,
    wecom_router,
    project_router,
    repository_router,
    credential_router,
    deploy_request_router,
    pipeline_router,
    agent_router,
    settings_router,
    store_router,
    artifact_router,
    approval_router,
    audit_router,
    metric_router,
    notify_router,
    llm_router,
    access_router,
    account_router,
    harness_router,
    pm_router,
    health_router,
    meta_router,
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库（建表 + 种子数据）+ 启动定时触发调度线程。"""
    from app.db.base import Base
    from app.db.session import engine
    from app.modules.harness.lifecycle import initialize_builtin_providers
    import app.db.models  # noqa: F401  确保所有模型注册

    # 只注册进程内 Provider；这里严禁 ORM 查询，数据库表尚未 create_all。
    from app.db.bootstrap import ensure_runtime_ready, wait_for_database

    initialize_builtin_providers()
    wait_for_database(engine)
    Base.metadata.create_all(bind=engine)
    _ensure_schema_patches()
    try:
        _seed_if_empty()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 演示数据写入失败，继续最小初始化：{e}", file=sys.stderr)
    _retire_placeholder_plugins()
    # 演示数据里的生产分组也要能自审，补列回填发生在 seed 之前，这里再扫一遍。
    _backfill_prod_self_approval()
    try:
        _seed_llm_catalog()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 模型目录初始化失败：{e}", file=sys.stderr)
    ensure_runtime_ready()

    # 扩大默认线程池，支撑百并发下同步接口（领取/心跳/日志/SSE 阻塞读）
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = 200
    except Exception:  # noqa: BLE001
        pass

    # 启动定时触发器调度线程（15s 到期开火；30s 只同步 trigger_type=cron 的行）
    from app.modules.pipeline.scheduler import start_scheduler

    start_scheduler()

    # 启动构建机心跳巡检（每 30s 把超时 agent 标记为 offline，不删除）
    from app.modules.agent.router import start_agent_heartbeat_check

    start_agent_heartbeat_check()

    from app.modules.ai.followup import start_followup_loop

    start_followup_loop()

    _warn_if_agent_jar_stale()

    yield


def _warn_if_agent_jar_stale() -> None:
    """检查分发给各机器的 deploy-agent.jar 是不是当前 java 源码编译的。

    jar 是提交进仓库的编译产物。改了 .java 忘了重新 build，平台就会一直把旧 jar
    发下去，而现象是「新功能怎么都不生效」——从表现完全看不出问题在编译上。
    这里在启动日志里喊一嗓子，比事后从节点日志倒推便宜得多。

    整段兜底不抛：这只是条提示，容器 stdout 落到非 UTF-8 locale 时 print 中文会抛
    UnicodeEncodeError，在 lifespan 里等于整个平台起不来，代价和它的用途完全不成比例。
    """
    import subprocess
    import sys
    from pathlib import Path

    try:
        from app.modules.agent.router import _agent_asset_path

        stamp_path = _agent_asset_path("deploy-agent.jar.srcsha")
        script_path = _agent_asset_path("src_fingerprint.py")
        if not stamp_path or not script_path:
            return  # 老部署没带这两个文件，不影响运行
        recorded = Path(stamp_path).read_text(encoding="utf-8").strip()
        current = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if recorded and current and recorded != current:
            import logging

            logging.getLogger(__name__).warning(
                "deploy-agent.jar 和 agent-java/src 对不上"
                "（jar 来自 %s，源码是 %s）。"
                "现在分发给构建机和节点的是旧 jar，Java 侧的改动不会生效。"
                "请在 backend/agent-java 执行 build.sh / build.bat 后重建 backend 镜像。",
                recorded,
                current,
            )
    except Exception:  # noqa: BLE001
        return


def _ensure_schema_patches():
    """已有库先按模型把列补齐，再跑数据回填（全部走 ORM）。

    create_all 不会 ALTER。缺列时先查 ORM，MySQL 会 1054，整站起不来。
    """
    from app.db.schema_sync import sync_model_columns
    from app.db.session import SessionLocal, engine

    added = sync_model_columns(engine)
    if added:
        print(f"[startup] 已按模型补列 {len(added)} 个")

    # 下面这些不是加列：扩类型、回填业务数据。列已经齐了，可以走 ORM。
    _ensure_build_task_logs_mediumtext()
    _widen_notice_kind()
    _backfill_pipeline_workspace_uuid()
    _backfill_legacy_plugins()
    _refresh_catalog_plugin_schemas()
    _ensure_release_build_number()
    _backfill_prod_self_approval()
    _backfill_user_account_source()
    _ensure_agent_enroll_token()
    _migrate_legacy_encrypted_credentials()
    _ensure_llm_adapter_schema()
    _withdraw_orphan_approvals()
    _rescue_stranded_releases()
    _backfill_pipeline_trigger_columns()
    with SessionLocal() as db:
        from app.modules.agent.tokens import migrate_plaintext_tokens
        from app.modules.store.plugin_service import sync_builtin_plugins

        hashed = migrate_plaintext_tokens(db)
        if hashed:
            print(f"[startup] 已把 {hashed} 台构建机的明文 token 收成哈希")
        sync_builtin_plugins(db)


def _ensure_llm_adapter_schema() -> None:
    """补齐适配器/路由/观测表，并把明文 API Key 迁成凭证引用。

    必须排在凭证密钥迁移之后：迁 Key 用的是当前 AES 密钥，先跑就会用一把
    随后才被换掉的密钥去加密。
    """
    from app.db.session import engine
    from app.modules.llm.models import ensure_llm_schema

    migrated = ensure_llm_schema(engine)
    if migrated:
        print(f"[startup] 已把 {migrated} 个厂商的明文 API Key 迁移为加密凭证引用")


def _migrate_legacy_encrypted_credentials() -> None:
    """把用旧默认 AES_KEY 加密的凭证换成当前密钥重新加密。

    旧默认密钥进过 git，必须停用；但停用的那一刻，此前用它加密的凭证就全解不开了——
    页面上表现为「凭证解密失败」，拉代码直接失败。密文还在，旧密钥也还知道，
    那就自己解开再用新密钥封回去，不该让用户去重新录一遍。
    """
    from app.core.config import _LEAKED_AES
    from app.core.security import decrypt, encrypt
    from app.db.session import SessionLocal
    from app.modules.credential.models import Credential

    if settings.aes_key == _LEAKED_AES:
        return  # 还在用旧密钥（debug），没什么可迁的

    migrated, broken = 0, []
    with SessionLocal() as db:
        for c in db.query(Credential).all():
            if not c.ciphertext:
                continue
            try:
                decrypt(c.ciphertext, c.iv)
                continue  # 当前密钥能解开，已经是新的了
            except Exception:  # noqa: BLE001
                pass
            try:
                plain = decrypt(c.ciphertext, c.iv, key_override=_LEAKED_AES)
            except Exception:  # noqa: BLE001
                broken.append(f"#{c.id} {c.name}")
                continue
            c.ciphertext, c.iv = encrypt(plain)
            migrated += 1
        if migrated:
            db.commit()
    if migrated:
        print(f"[startup] 已用新密钥重新加密 {migrated} 条历史凭证")
    if broken:
        print(
            "[startup] ⚠ 以下凭证既解不开也不认识它的密钥，需要到「凭证管理」重新录入："
            + "、".join(broken),
            file=sys.stderr,
        )


def _rescue_stranded_releases() -> None:
    """启动时清理一遍卡死的发布（之后由 agent 巡检每 30s 兜底）。

    重启这一刻没有任何请求在飞，不需要保护期，可以把刚卡住的也一起收掉。
    """
    from app.db.session import SessionLocal
    from app.modules.pipeline.service import rescue_stranded_releases

    with SessionLocal() as db:
        n = rescue_stranded_releases(db, min_age_seconds=0)
    if n:
        print(f"[startup] 已清理 {n} 条没有构建任务、卡住不动的发布")


def _backfill_pipeline_trigger_columns() -> None:
    """存量流水线把 YAML 派生列写上：定时、清单标记、子流水线边。之后列表和环检测不再 parse YAML。"""
    from app.db.session import SessionLocal
    from app.modules.pipeline.service import backfill_trigger_columns

    with SessionLocal() as db:
        n = backfill_trigger_columns(db)
    if n:
        print(f"[startup] 已回填 {n} 条流水线的 YAML 派生列")


def _ensure_agent_enroll_token() -> None:
    """首次启动生成构建机接入凭证。

    注册构建机等于往任务队列里插一台能领取真实构建任务（含仓库凭证）的机器，
    不能是匿名的。凭证放平台设置里，管理员可在构建机页面查看和轮换。
    """
    import secrets

    from app.db.session import SessionLocal
    from app.modules.settings import get_setting, update_settings

    with SessionLocal() as db:
        if not get_setting(db, "agent_enroll_token").strip():
            update_settings(db, {"agent_enroll_token": secrets.token_hex(24)})
            print("[startup] 已生成构建机接入凭证，请到「构建机」页面查看")


def _withdraw_orphan_approvals() -> None:
    """撤回已结束发布遗留的待审批单。

    早期版本取消发布时没有连带撤回审批待办，审批人那里会一直挂着一条点不动的单子。
    这里做幂等修复，同时兜住其他可能漏掉的结束路径。
    """
    from sqlalchemy import select, update

    from app.db.session import SessionLocal
    from app.modules.approval.models import Approval
    from app.modules.pipeline.models import Release

    ended = select(Release.id).where(Release.status != "pending")
    with SessionLocal() as db:
        db.execute(
            update(Approval)
            .where(Approval.status == "pending", Approval.release_id.in_(ended))
            .values(status="cancelled", comment="发布已结束，审批自动撤回"),
            execution_options={"synchronize_session": False},
        )
        db.commit()


def _backfill_pipeline_workspace_uuid() -> None:
    """旧流水线补齐工作空间 UUID。列本身由 sync_model_columns 补。"""
    import uuid

    from sqlalchemy import or_, select

    from app.db.session import SessionLocal
    from app.modules.pipeline.models import Pipeline

    with SessionLocal() as db:
        rows = db.scalars(
            select(Pipeline).where(
                or_(Pipeline.workspace_uuid.is_(None), Pipeline.workspace_uuid == "")
            )
        ).all()
        for p in rows:
            p.workspace_uuid = uuid.uuid4().hex
        if rows:
            db.commit()


def _ensure_release_build_number() -> None:
    """按流水线回填历史构建号。列本身由 sync_model_columns 补。

    以前页面直接拿全局自增的 release.id 当构建号，新流水线第一次执行就显示 #34。
    回填按 id 顺序给每条流水线各自从 1 编号，历史记录的相对顺序不变。
    """
    from sqlalchemy import func, select

    from app.db.session import SessionLocal
    from app.modules.pipeline.models import Release

    with SessionLocal() as db:
        pending = db.scalars(
            select(Release)
            .where((Release.build_number == None) | (Release.build_number == 0))  # noqa: E711
            .order_by(Release.id)
        ).all()
        if not pending:
            return
        seq: dict[int, int] = {}
        for r in pending:
            if r.pipeline_id not in seq:
                seq[r.pipeline_id] = int(
                    db.scalar(
                        select(func.max(Release.build_number)).where(
                            Release.pipeline_id == r.pipeline_id
                        )
                    )
                    or 0
                )
            seq[r.pipeline_id] += 1
            r.build_number = seq[r.pipeline_id]
        db.commit()
        print(f"[startup] 已回填 {len(pending)} 条发布记录的构建号")


def _ensure_build_task_logs_mediumtext() -> None:
    """build_task.logs 从 TEXT 扩到 MEDIUMTEXT。

    TEXT 只有 64KB，构建日志超过就写不进去。而日志归档跑在后台线程里、
    异常被 warning 吞掉，症状是「日志只有开头一段」，页面上没有任何报错。
    这是改列类型，不是加列，模型同步做不了。
    """
    from sqlalchemy import text

    from app.db.session import engine

    if engine.dialect.name != "mysql":  # sqlite 的 TEXT 本来就没有长度限制
        return
    with engine.begin() as conn:
        current = conn.execute(
            text(
                "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='build_task' "
                "AND COLUMN_NAME='logs'"
            )
        ).scalar()
        if current and current.lower() == "text":
            conn.execute(text("ALTER TABLE build_task MODIFY COLUMN logs MEDIUMTEXT"))
            print("[schema] build_task.logs 已扩容为 MEDIUMTEXT")


def _backfill_legacy_plugins() -> None:
    """Agent jar 内置的 shell/bat 没有 zip，仍视为已安装。

    其余没有包的名字不能标成已安装：编排器按 installed=true 拉列表，
    选中后构建机报 plugin not installed。
    """
    from sqlalchemy import or_, select

    from app.db.session import SessionLocal
    from app.modules.store.models import Plugin
    from app.modules.store.plugin_service import JOB_INLINE_PLUGINS

    with SessionLocal() as db:
        rows = db.scalars(
            select(Plugin).where(
                Plugin.name.in_(JOB_INLINE_PLUGINS),
                or_(Plugin.package_path.is_(None), Plugin.package_path == ""),
                or_(Plugin.installed.is_(False), Plugin.installed.is_(None)),
            )
        ).all()
        for p in rows:
            p.installed = True
            p.enabled = True
            p.status = "installed"
        if rows:
            db.commit()


def _retire_placeholder_plugins() -> None:
    """启动时收回没有 zip 却被标成已安装的占位插件，编排器不再能选。"""
    from app.db.session import SessionLocal
    from app.modules.store.plugin_service import retire_unrunnable_catalog_plugins

    with SessionLocal() as db:
        n = retire_unrunnable_catalog_plugins(db)
    if n:
        print(f"[startup] 已把 {n} 个没有实现的占位插件从「已安装」收回")


def _refresh_catalog_plugin_schemas() -> None:
    """已有库刷新内置目录插件的表单 schema（seed 只在空库执行）。"""
    import json

    from sqlalchemy import select

    from app.db.seed import _SCHEMA_SHELL_EXEC
    from app.db.session import SessionLocal
    from app.modules.store.models import Plugin

    payload = json.dumps(_SCHEMA_SHELL_EXEC, ensure_ascii=False)
    with SessionLocal() as db:
        plugin = db.scalars(select(Plugin).where(Plugin.name == "shell-exec")).first()
        if plugin and plugin.config_schema != payload:
            plugin.config_schema = payload
            db.commit()


def _backfill_prod_self_approval() -> None:
    """存量生产分组打开自审，和当初补列时的默认行为一致。"""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.modules.project.models import Group

    with SessionLocal() as db:
        changed = False
        for g in db.scalars(select(Group).where(Group.type == "prod")).all():
            if not g.allow_self_approval:
                g.allow_self_approval = True
                changed = True
        if changed:
            db.commit()


def _backfill_user_account_source() -> None:
    """按企微/LDAP 痕迹回填账号来源，空状态视为启用。"""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.modules.auth.models import User

    with SessionLocal() as db:
        for u in db.scalars(select(User)).all():
            src = (u.source or "").strip()
            if src in ("", "local"):
                if u.wecom_userid:
                    u.source = "wecom"
                elif not u.password_hash:
                    u.source = "ldap"
                else:
                    u.source = "local"
            if not (u.status or "").strip():
                u.status = "active"
        db.commit()


def _widen_notice_kind() -> None:
    """通知事件名从短字段扩到 VARCHAR(64)。改类型，不是加列。"""
    from sqlalchemy import text

    from app.db.session import engine

    if engine.dialect.name != "mysql":
        return
    with engine.begin() as conn:
        current = conn.execute(
            text(
                "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='in_app_notice' "
                "AND COLUMN_NAME='kind'"
            )
        ).scalar()
        if current is not None and int(current) < 64:
            conn.execute(
                text("ALTER TABLE in_app_notice MODIFY COLUMN kind VARCHAR(64) NOT NULL DEFAULT ''")
            )


def _seed_if_empty():
    """数据库为空时写入演示数据。"""
    from sqlalchemy import select
    from app.db.session import SessionLocal
    from app.db.models import User

    with SessionLocal() as db:
        if db.scalar(select(User).limit(1)) is None:
            from app.db.seed import seed
            seed(db)


def _seed_llm_catalog():
    from app.db.session import SessionLocal
    from app.modules.llm.service import seed_llm_catalog

    with SessionLocal() as db:
        seed_llm_catalog(db)


_docs = bool(settings.debug or settings.api_docs_enabled)
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "PekaFlowAI：多形态发布部署。文档仅供内网/本地查看；"
        "调用接口请在本地工具里带自己账号的 Bearer Token（登录 JWT 或 qx_ API Token）。"
    ),
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/v3/api-docs" if _docs else None,
)

if _docs:
    from fastapi.responses import RedirectResponse

    @app.get("/swagger-ui", include_in_schema=False)
    def _swagger_ui_alias():
        return RedirectResponse("/docs")

# CORS：public_app_base + CORS_ORIGINS，不再用 * 配 credentials
from app.core.cors import AllowlistCORSMiddleware

app.add_middleware(AllowlistCORSMiddleware)


# 全局异常处理
@app.exception_handler(BizException)
async def biz_exception_handler(request: Request, exc: BizException):
    return JSONResponse(status_code=exc.code if 400 <= exc.code < 600 else 500,
                        content=R.fail(exc.message, exc.code).model_dump())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400,
                        content=R.fail("参数校验失败: " + str(exc.errors())[:300], 400).model_dump())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    import logging
    logging.getLogger(__name__).exception("未处理异常: %s", exc)
    # 细节只进日志：异常字符串里常带 SQL 片段、文件路径、内部主机名，回给客户端等于
    # 免费给攻击者递踩点信息。前端只需要知道「这不是你的参数问题，是服务端崩了」
    return JSONResponse(status_code=500,
                        content=R.fail("服务器内部错误，请联系管理员并提供操作时间", 500).model_dump())


# 挂载业务路由（统一 /api/v1 前缀，与文档 §9 接口规范一致）
for r in module_routers:
    app.include_router(r, prefix="/api/v1")

# AI 助手路由（独立挂载）
app.include_router(ai_router, prefix="/api/v1")

# MCP Server 端点（供支持 MCP 的 AI 客户端连接）
from app.modules.ai.mcp_server import router as mcp_router

app.include_router(mcp_router)


@app.get("/api/v1/health", tags=["系统"], summary="健康检查")
def health():
    return R.ok({"status": "up", "version": settings.app_version})
