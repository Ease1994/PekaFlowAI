"""制品查询、批量清理与保留策略。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.artifact import storage
from app.modules.artifact.models import Artifact

# 「提取增量发布包」插件上传时打的类型标记
DEPLOY_PACKAGE_TYPE = "iis-package"

# 生产 / UAT / 预发：每条流水线保留最近多少个自然日的包；测试环境仍只留当天
PROD_RETENTION_DAYS = 10
PROD_RETENTION_DAYS_MIN = 1
PROD_RETENTION_DAYS_MAX = 365
# 窗口之外仍留下每条生产流水线最新的几个包。一条线就是一个服务，
# 十天没发不等于线上不用回滚；只留最新 1 个等于「当前这份」，回滚还要上一份。
PROD_KEEP_LATEST_PER_PIPELINE = 2
# 测试分组的制品留几天：0 表示只留当天产出的，隔夜就清
TEST_RETENTION_DAYS = 0
# 兼容旧调用方参数名
PROD_KEEP_RELEASES = PROD_RETENTION_DAYS


def clamp_prod_retention_days(raw: object) -> int:
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        n = PROD_RETENTION_DAYS
    return max(PROD_RETENTION_DAYS_MIN, min(PROD_RETENTION_DAYS_MAX, n))


def prod_retention_days_from_settings(db: Session) -> int:
    from app.modules.settings import get_setting

    return clamp_prod_retention_days(
        get_setting(db, "artifact_prod_retention_days", str(PROD_RETENTION_DAYS))
    )


def _day_cutoff(days: int) -> datetime:
    """按自然日算：1=今天零点起，10=含今天在内的最近 10 个自然日。"""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=max(int(days), 1) - 1)


def find_release_package(
    db: Session, release_id: int | None, package_type: str = DEPLOY_PACKAGE_TYPE
) -> Artifact | None:
    """找出本次发布要发到节点上的增量包。

    同一次发布可能打过多次包（重试、Rebuild），取最新的那个：
    节点要发的永远是这次执行最后产出的结果。
    """
    if not release_id:
        return None
    return db.scalars(
        select(Artifact)
        .where(Artifact.release_id == release_id, Artifact.type == package_type)
        .order_by(Artifact.id.desc())
        .limit(1)
    ).first()


def delete_artifacts(db: Session, artifacts: list[Artifact]) -> int:
    """删制品记录，同时清掉磁盘文件。

    先删文件再删记录：反过来的话，记录没了文件还在，那份文件就再也没人认领了。
    单个文件删不掉（被占用、已经不在）不该拖累整批，记录必须删干净，
    否则页面上点了删除却还列着。
    """
    removed = 0
    for a in artifacts:
        storage.remove(a.storage_key)
        db.delete(a)
        removed += 1
    db.commit()
    return removed


def _expired_prod_for_pipeline(arts: list[Artifact], cutoff: datetime) -> list[Artifact]:
    """一条生产流水线该清哪些包。

    窗口内的全留；窗口外按创建时间从新到旧，仍留下最新若干个。
    这样十天没发的服务至少还留着当前包和上一包，回滚时包还在。
    """
    ranked = sorted(arts, key=lambda a: (a.created_at or datetime.min, a.id), reverse=True)
    keep_ids: set[int] = set()
    for i, a in enumerate(ranked):
        in_window = bool(a.created_at) and a.created_at >= cutoff
        if in_window or i < PROD_KEEP_LATEST_PER_PIPELINE:
            keep_ids.add(a.id)
    return [a for a in ranked if a.id not in keep_ids]


def purge_expired_artifacts(
    db: Session,
    *,
    prod_retention_days: int | None = None,
    test_retention_days: int = TEST_RETENTION_DAYS,
    prod_keep: int | None = None,
) -> dict:
    """按分组环境清理过期制品。

    测试 / 开发：隔夜即清，包发完就没用了。
    生产 / UAT / 预发 / 自定义：按流水线保留最近若干自然日，窗口外仍留该线最新两份，
    给回滚留窗口。一刀切「全库早于某天」会把久未发布的服务包清光。

    环境判定取反：不在「随手可丢」名单里的一律当生产策略。只认 prod 的话，
    用户新建的 UAT 会被当成测试，包隔夜清掉。

    流水线已经被彻底删除的制品按测试环境处理——没有流水线就没人会再去回滚它。
    """
    from app.core.env import is_disposable
    from app.modules.pipeline.models import Pipeline
    from app.modules.project.models import Group

    if prod_retention_days is None:
        prod_retention_days = (
            clamp_prod_retention_days(prod_keep)
            if prod_keep is not None
            else prod_retention_days_from_settings(db)
        )
    else:
        prod_retention_days = clamp_prod_retention_days(prod_retention_days)

    prod_group_ids = {
        g.id for g in db.scalars(select(Group)).all() if not is_disposable(g.type)
    }
    pipelines = {
        pid: gid
        for pid, gid in db.execute(select(Pipeline.id, Pipeline.group_id)).all()
    }

    test_cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if test_retention_days > 0:
        test_cutoff -= timedelta(days=test_retention_days)
    prod_cutoff = _day_cutoff(prod_retention_days)

    expired_test: list[Artifact] = []
    prod_by_pipeline: dict[int, list[Artifact]] = {}

    for a in db.scalars(select(Artifact)).all():
        group_id = pipelines.get(a.pipeline_id)
        if group_id is not None and group_id in prod_group_ids:
            prod_by_pipeline.setdefault(a.pipeline_id, []).append(a)
        elif not a.created_at or a.created_at < test_cutoff:
            expired_test.append(a)

    expired_prod: list[Artifact] = []
    for arts in prod_by_pipeline.values():
        expired_prod.extend(_expired_prod_for_pipeline(arts, prod_cutoff))

    doomed = expired_test + expired_prod
    freed = sum(a.size_bytes or 0 for a in doomed)
    removed = delete_artifacts(db, doomed) if doomed else 0
    return {
        "removed": removed,
        "test_removed": len(expired_test),
        "prod_removed": len(expired_prod),
        "freed_bytes": freed,
    }


def storage_summary(db: Session, pipeline_ids: set[int] | None) -> dict:
    """制品占用概览，pipeline_ids 为 None 表示不限（管理员）。"""
    stmt = select(func.count(Artifact.id), func.sum(Artifact.size_bytes))
    if pipeline_ids is not None:
        if not pipeline_ids:
            return {"count": 0, "total_bytes": 0}
        stmt = stmt.where(Artifact.pipeline_id.in_(pipeline_ids))
    count, total = db.execute(stmt).one()
    return {"count": int(count or 0), "total_bytes": int(total or 0)}
