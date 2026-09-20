"""定时触发器（Redis 延迟队列实现）。

设计：
- ``cron:queue``  ZSET：member=pipeline_id, score=next_run_ts（下次执行时间戳）
- ``cron:expr``   HASH：pipeline_id -> cron 表达式（触发后重算下一次）

**API 保存时只同步这一条**：触发类型已经落在 pipeline.trigger_type 列上。
**scheduler 后台兜底**：
- ``tick()`` 每 15s 扫描 ZSET 到期项 → 原子 ZREM（防重复）→ 触发执行
- ``scan_cron_pipelines_and_sync()`` 每 30s 只扫 trigger_type=cron 的流水线，按列写入 Redis
Redis 不可用时自动降级（定时触发不生效），配置好 Redis 后下一个周期自动恢复。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import croniter

REDIS_QUEUE = "cron:queue"
REDIS_EXPR = "cron:expr"


def _get_redis():
    from app.core.redis_client import get_redis

    return get_redis()


def get_next_run_time(pipeline_id: int) -> datetime | None:
    """查询某流水线的下次触发时间（供前端展示）。"""
    try:
        r = _get_redis()
        score = r.zscore(REDIS_QUEUE, str(pipeline_id))
        if score is None:
            return None
        return datetime.fromtimestamp(score)
    except Exception:
        return None


def tick() -> int:
    """扫描到期触发器并执行，返回触发数量。Redis 不可用时降级返回 0。"""
    from app.db.session import SessionLocal
    from app.modules.pipeline import service

    try:
        r = _get_redis()
        now = time.time()
        due = r.zrangebyscore(REDIS_QUEUE, 0, now)
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] Redis 不可用，跳过本轮：{e}")
        return 0

    fired = 0
    for pid in due:
        # 原子移除：只有移除成功的实例才执行（多 worker / 重启防重复）
        if not r.zrem(REDIS_QUEUE, pid):
            continue
        pid_int = int(pid)
        with SessionLocal() as db:
            try:
                from app.modules.audit.context import use_source

                with use_source("cron", user_id=1, username="admin"):
                    release = service.create_release(
                        db,
                        pipeline_id=pid_int,
                        version="",
                        strategy="rolling",
                        trigger_by="cron",
                        operator_id=1,  # 系统触发
                    )
                    # 生产分组 → pending 等审批；测试分组 → queued 直接执行
                    if release.status == "queued":
                        service.execute_release(db, release.id)
                fired += 1
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] 触发 pipeline #{pid_int} 失败: {e}")
            # 重排下一次
            try:
                cron_expr = r.hget(REDIS_EXPR, pid)
                if cron_expr:
                    it = croniter.croniter(cron_expr, datetime.now())
                    next_dt = it.get_next(datetime)
                    r.zadd(REDIS_QUEUE, {pid: next_dt.timestamp()})
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] 重排 pipeline #{pid_int} 失败: {e}")
    return fired


def drop_cron_schedule(pipeline_id: int) -> None:
    """从 Redis 摘掉这一条定时。删除 / 改回手动时用。"""
    try:
        r = _get_redis()
        r.zrem(REDIS_QUEUE, pipeline_id)
        r.hdel(REDIS_EXPR, pipeline_id)
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 清理 #{pipeline_id} 的定时条目失败: {e}")


def upsert_cron_schedule(pipeline_id: int, cron_expr: str) -> None:
    """把这一条定时写进 Redis。表达式没变且队列里已有下次时间，就不动。

    保存接口会调这里；Redis 挂了只打日志，不挡保存。后台 30 秒兜底会再补。
    """
    expr = (cron_expr or "").strip()
    if not expr:
        drop_cron_schedule(pipeline_id)
        return
    try:
        r = _get_redis()
        old = r.hget(REDIS_EXPR, str(pipeline_id))
        if isinstance(old, bytes):
            old = old.decode()
        if old == expr and r.zscore(REDIS_QUEUE, str(pipeline_id)) is not None:
            return
        it = croniter.croniter(expr, datetime.now())
        next_dt = it.get_next(datetime)
        r.hset(REDIS_EXPR, str(pipeline_id), expr)
        r.zadd(REDIS_QUEUE, {str(pipeline_id): next_dt.timestamp()})
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 写入 #{pipeline_id} 定时队列失败: {e}")


def scan_cron_pipelines_and_sync() -> int:
    """只同步 trigger_type=cron 的流水线，按列上的表达式写 Redis，不解析 YAML。"""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.modules.pipeline.models import Pipeline

    try:
        with SessionLocal() as db:
            rows = list(
                db.scalars(
                    select(Pipeline).where(
                        Pipeline.status == "active",
                        Pipeline.trigger_type == "cron",
                    )
                ).all()
            )
        keep: set[str] = set()
        synced = 0
        for p in rows:
            expr = (p.cron_expr or "").strip()
            if not expr:
                continue
            upsert_cron_schedule(p.id, expr)
            keep.add(str(p.id))
            synced += 1
        try:
            r = _get_redis()
            stored = r.hkeys(REDIS_EXPR) or []
            for raw in stored:
                pid = raw.decode() if isinstance(raw, bytes) else str(raw)
                if pid not in keep:
                    drop_cron_schedule(int(pid))
        except Exception as e:  # noqa: BLE001
            print(f"[scheduler] 清理过期定时条目失败: {e}")
        return synced
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 扫描同步异常: {e}")
        return 0


def scan_all_pipelines_and_sync() -> int:
    """兼容旧名：现在只扫 cron 列，不再全量解析 YAML。"""
    return scan_cron_pipelines_and_sync()


def purge_recycle_bin() -> int:
    """清理回收站里超过保留期的流水线（每小时一次）。"""
    from app.db.session import SessionLocal
    from app.modules.pipeline import service

    try:
        with SessionLocal() as db:
            n = service.purge_expired_pipelines(db)
            if n:
                print(f"[scheduler] 回收站清理 {n} 条过期流水线")
            return n
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 回收站清理异常: {e}")
        return 0


def withdraw_orphan_approvals() -> int:
    """撤回已结束发布遗留的待审批单（每小时一次）。

    正常路径（取消 / 通过 / 驳回）都会连带撤回，但只要有一条漏了，审批人那里就会
    一直挂着一条点不动的单子——点下去只会得到「当前状态 xx 不允许审批」。
    启动时已经清过一遍，这里兜住进程长期不重启的情况。
    """
    from sqlalchemy import select, update

    from app.db.session import SessionLocal
    from app.modules.approval.models import Approval
    from app.modules.pipeline.models import Release

    try:
        with SessionLocal() as db:
            ended = select(Release.id).where(Release.status != "pending")
            n = db.execute(
                update(Approval)
                .where(Approval.status == "pending", Approval.release_id.in_(ended))
                .values(status="cancelled", comment="发布已结束，审批自动撤回"),
                execution_options={"synchronize_session": False},
            ).rowcount
            db.commit()
        if n:
            print(f"[scheduler] 撤回 {n} 条已结束发布遗留的待审批单")
        return n or 0
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 撤回遗留审批异常: {e}")
        return 0


def purge_audit_logs() -> int:
    """清理超过保留期的审计日志（每小时一次）。"""
    from app.db.session import SessionLocal
    from app.modules.audit import service as audit_service

    try:
        with SessionLocal() as db:
            n = audit_service.purge_expired(db)
            if n:
                print(f"[scheduler] 审计日志清理 {n} 条过期记录")
            return n
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 审计日志清理异常: {e}")
        return 0


# 制品清理放在凌晨：测试环境的包「当天晚上清掉」，白天跑会把人正在用的包删了
ARTIFACT_PURGE_HOUR = 3
_last_artifact_purge_date = None


def purge_artifacts_daily() -> dict | None:
    """每天清一次制品：测试隔夜即清；生产按流水线留最近若干天，并至少留最新两份。

    重启会丢掉「今天已经清过」的记录，最多当天多跑一次；清理本身是幂等的，
    为这点重复去引入一张状态表不划算。
    """
    global _last_artifact_purge_date
    from app.db.session import SessionLocal
    from app.modules.artifact import service as artifact_service

    now = datetime.now()
    if now.hour < ARTIFACT_PURGE_HOUR or _last_artifact_purge_date == now.date():
        return None
    _last_artifact_purge_date = now.date()

    try:
        with SessionLocal() as db:
            days = artifact_service.prod_retention_days_from_settings(db)
            stat = artifact_service.purge_expired_artifacts(db, prod_retention_days=days)
        if stat["removed"]:
            print(
                f"[scheduler] 制品清理：测试 {stat['test_removed']} 个、"
                f"生产过期 {stat['prod_removed']} 个，释放 "
                f"{stat['freed_bytes'] / 1024 / 1024:.1f} MB"
            )
        return stat
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 制品清理异常: {e}")
        return None


def scheduler_loop() -> None:
    """后台线程：
    - 每 15s tick 一次（扫描到期任务并执行）
    - 每 30s scan_cron_pipelines_and_sync 一次（只同步 cron 列，不解析 YAML）
    - 每 1h 清一次回收站（超过保留期的流水线物理删除）与审计日志
    - 每天凌晨清一次制品（测试隔夜清；生产按流水线留最近若干天，并至少留最新两份）
    """
    last_scan = 0.0
    last_purge = 0.0
    while True:
        try:
            tick()
            if time.time() - last_scan > 30:
                last_scan = time.time()
                scan_cron_pipelines_and_sync()
            if time.time() - last_purge > 3600:
                last_purge = time.time()
                purge_recycle_bin()
                purge_audit_logs()
                purge_artifacts_daily()
                withdraw_orphan_approvals()
        except Exception as e:  # noqa: BLE001
            print(f"[scheduler] 主循环异常: {e}")
        time.sleep(15)


def start_scheduler() -> None:
    """启动调度线程（main.py lifespan 调用）。"""
    threading.Thread(target=scheduler_loop, name="cron-scheduler", daemon=True).start()
    from app.modules.pipeline.platform_worker import start_platform_worker

    start_platform_worker()
