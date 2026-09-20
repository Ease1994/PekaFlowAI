"""构建日志与业务库硬隔离：ES 挂了不能写库，也不能攒内存。"""
from __future__ import annotations

import threading
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.modules.agent.log_store import (
    MemoryLogStore,
    NullLogStore,
    RedisStreamLogStore,
    create_log_store,
    reset_log_store,
)
from app.modules.agent.models import BuildTask, BuildTaskLog
from app.modules.pipeline.models import Pipeline, Release
from app.modules.pipeline.service import get_release_logs, set_release_error
from app.modules.project.models import Group, Project


def test_create_log_store_never_uses_mysql_or_memory() -> None:
    reset_log_store()
    store = create_log_store(
        None,
        lambda: {
            "log_storage": "mysql",
            "es_hosts": "http://127.0.0.1:1",
            "es_index": "rp-exec-logs",
        },
    )
    name = store.name().lower()
    assert "mysql" not in name
    assert "memory" not in name
    if hasattr(store, "_skip_redis_until"):
        store._skip_redis_until = 10**12
    store.append_batch(1, ["should-not-hit-db-or-ram"])


def test_es_build_never_raises_or_writes_mysql() -> None:
    from app.modules.agent.log_store import _build_es_store

    store = _build_es_store({"es_hosts": "http://127.0.0.1:1", "es_index": "rp-exec-logs"})
    store.append_batch(9, ["es-down-must-not-raise"])
    assert store.get(9) == [] or store.name() == "none"
    assert store.name() != "memory"


def test_redis_down_writes_archive_not_mysql() -> None:
    archive = MemoryLogStore()
    store = RedisStreamLogStore(archive)
    store._skip_redis_until = 10**12
    store.append_batch(3, ["only-archive"])
    assert archive.get(3) == ["only-archive"]


def test_dead_archive_does_not_raise() -> None:
    class Boom(NullLogStore):
        def try_append_batch(self, task_id: int, lines: list[str]) -> bool:
            raise RuntimeError("es exploded")

    store = RedisStreamLogStore(Boom())
    store._skip_redis_until = 10**12
    store.append_batch(4, ["drop-me"])


class _FakePipe:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def xadd(self, key, fields, maxlen=None, approximate=True):  # noqa: ANN001
        self._ops.append(("xadd", key, fields))
        return self

    def expire(self, key, ttl):  # noqa: ANN001
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "xadd":
                out.append(self._redis.xadd(op[1], op[2]))
            elif op[0] == "expire":
                out.append(self._redis.expire(op[1], op[2]))
        self._ops = []
        return out


class _FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.expires: dict[str, int] = {}
        self._seq = 0

    def pipeline(self, transaction=False):  # noqa: ANN001
        return _FakePipe(self)

    def xadd(self, key, fields, maxlen=None, approximate=True):  # noqa: ANN001
        self._seq += 1
        eid = f"{self._seq}-0"
        self.streams.setdefault(key, []).append((eid, dict(fields)))
        if maxlen and len(self.streams[key]) > maxlen:
            self.streams[key] = self.streams[key][-maxlen:]
        return eid

    def expire(self, key, ttl):  # noqa: ANN001
        self.expires[key] = int(ttl)
        return True

    def xtrim(self, key, minid=None, approximate=True, maxlen=None):  # noqa: ANN001
        return 0

    def xrange(self, key, min="-", max="+", count=None):  # noqa: ANN001
        rows = list(self.streams.get(key, []))
        if count is not None:
            rows = rows[: int(count)]
        return rows

    def xrevrange(self, key, max="+", min="-", count=None):  # noqa: ANN001
        rows = list(reversed(self.streams.get(key, [])))
        if count is not None:
            rows = rows[: int(count)]
        return rows

    def xlen(self, key):  # noqa: ANN001
        return len(self.streams.get(key, []))

    def xdel(self, key, *ids):  # noqa: ANN001
        want = set(ids)
        rows = self.streams.get(key, [])
        kept = [row for row in rows if row[0] not in want]
        n = len(rows) - len(kept)
        self.streams[key] = kept
        return n


def test_es_success_deletes_redis_and_sets_ttl() -> None:
    from app.modules.agent.log_store import STREAM_TTL_SECONDS

    archive = MemoryLogStore()
    fake = _FakeRedis()
    store = RedisStreamLogStore(archive)
    store._try_redis = lambda: fake  # type: ignore[method-assign]
    store.append_batch(1, ["a", "b"])
    for _ in range(80):
        if fake.xlen(store._key(1)) == 0 and archive.get(1) == ["a", "b"]:
            break
        time.sleep(0.02)
    assert archive.get(1) == ["a", "b"]
    assert fake.xlen(store._key(1)) == 0
    assert fake.expires[store._key(1)] == STREAM_TTL_SECONDS


def test_es_fail_keeps_redis() -> None:
    class Fail(NullLogStore):
        def try_append_batch(self, task_id: int, lines: list[str]) -> bool:
            return False

    fake = _FakeRedis()
    store = RedisStreamLogStore(Fail())
    store._try_redis = lambda: fake  # type: ignore[method-assign]
    store.append_batch(2, ["keep-me"])
    time.sleep(0.05)
    assert fake.xlen(store._key(2)) == 1


def test_get_merges_archive_and_redis_tail() -> None:
    archive = MemoryLogStore()
    archive.append_batch(8, ["old-es"])
    fake = _FakeRedis()
    fake.xadd("release:task:8:logs", {"line": "new-redis"})
    store = RedisStreamLogStore(archive)
    store._try_redis = lambda: fake  # type: ignore[method-assign]
    store._skip_redis_until = 0
    assert store.get(8) == ["old-es", "new-redis"]


def test_archive_does_not_spawn_unbounded_threads() -> None:
    started = threading.Event()
    released = threading.Event()
    calls = {"n": 0}

    class Slow(NullLogStore):
        def try_append_batch(self, task_id: int, lines: list[str]) -> bool:
            calls["n"] += 1
            started.set()
            released.wait(timeout=2)
            return True

        def name(self) -> str:
            return "slow-es"

    fake = _FakeRedis()
    store = RedisStreamLogStore(Slow())
    store._try_redis = lambda: fake  # type: ignore[method-assign]
    before = threading.active_count()
    for i in range(40):
        store.append_batch(1, [f"line-{i}"])
    assert started.wait(timeout=1)
    extra = threading.active_count() - before
    released.set()
    time.sleep(0.05)
    assert extra <= 2, extra
    assert calls["n"] >= 1


def _session():
    engine = create_engine("sqlite:///:memory:")
    Project.__table__.create(engine)
    Group.__table__.create(engine)
    Pipeline.__table__.create(engine)
    Release.__table__.create(engine)
    BuildTask.__table__.create(engine)
    BuildTaskLog.__table__.create(engine)
    return Session(engine), engine


def test_get_release_logs_does_not_read_mysql_columns(monkeypatch) -> None:
    db, engine = _session()
    sql: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        sql.append(statement)

    proj = Project(name="p", code="c", description="")
    db.add(proj)
    db.flush()
    grp = Group(name="g", project_id=proj.id, type="test")
    db.add(grp)
    db.flush()
    pipe = Pipeline(name="pipe", project_id=proj.id, group_id=grp.id)
    db.add(pipe)
    db.flush()
    rel = Release(pipeline_id=pipe.id, group_id=grp.id, status="success", logs="db-release-should-hide")
    db.add(rel)
    db.flush()
    task = BuildTask(
        release_id=rel.id,
        pipeline_id=pipe.id,
        stage_name="s",
        job_name="j",
        status="success",
        logs="db-task-should-hide",
    )
    db.add(task)
    db.flush()
    db.add(BuildTaskLog(task_id=task.id, seq=0, lines=1, content="db-chunk-should-hide"))
    db.commit()
    sql.clear()

    mem = MemoryLogStore()
    mem.append_batch(task.id, ["from-es"])
    monkeypatch.setattr(
        "app.modules.agent.log_store.create_log_store",
        lambda *a, **k: mem,
    )

    data = get_release_logs(db, rel.id)
    assert "from-es" in data["logs"]
    assert "db-task-should-hide" not in data["logs"]
    assert "db-chunk-should-hide" not in data["logs"]
    assert "db-release-should-hide" not in data["logs"]
    joined = " ".join(sql).lower()
    assert "from build_task_log" not in joined
    assert "into build_task_log" not in joined
    db.close()


def test_set_release_error_does_not_write_logs() -> None:
    rel = Release(pipeline_id=1, group_id=1, status="failed", logs="must-stay")
    set_release_error(rel, "[系统] 发布未能启动：节点不存在")
    assert rel.error_message == "发布未能启动：节点不存在"
    assert rel.logs == "must-stay"


def test_daily_index_uses_yyyy_mm_dd(monkeypatch) -> None:
    from app.modules.agent.log_store import ESLogStore, _build_es_store, normalize_es_index_prefix
    from app.modules.agent import log_store as store_mod

    monkeypatch.setattr(store_mod, "_today_cn", lambda: "2026-09-16")
    assert normalize_es_index_prefix("rp-exec-logs-2026-09-16") == "rp-exec-logs"
    store = _build_es_store({"es_hosts": "http://127.0.0.1:1", "es_index": "rp-exec-logs"})
    assert isinstance(store, ESLogStore)
    assert store._daily_index() == "rp-exec-logs-2026-09-16"
