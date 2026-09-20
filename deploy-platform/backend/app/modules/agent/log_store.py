"""构建日志存储。

硬隔离：
  - 实时：Redis Stream（最多保留 1 小时）
  - 历史：只在 Elasticsearch；写入 ES 成功后立刻从 Redis 删除
  - 业务数据只进数据库
  - ES / Redis 挂了：丢这一批，可以打 warning；禁止写 MySQL，也禁止攒进程内存
  - 库挂了：不能把业务数据塞进 ES
  - 中间件故障不得拖垮发布：超时短、失败后冷却，忙就丢、不排队
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

from app.modules.settings.defaults import DEFAULT_ES_INDEX_PREFIX

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_ES_TIMEOUT = 3.0

logger = logging.getLogger(__name__)

STREAM_KEY = "release:task:{task_id}:logs"
STREAM_TTL_SECONDS = 3600
STREAM_MAXLEN = 30000  # 一小时之外的时间裁剪为主；这条是防单任务把 Redis 打满
_DRAIN_CHUNK = 500
_DRAIN_MAX_CHUNKS = 40

MAX_FETCH_LINES = 20000
TRUNCATED_MARK = "…（日志过长，此处省略 {n} 行，完整日志请到构建机工作区或 ES 查看）"

_store_lock = threading.Lock()
_cached_store: LogStore | None = None
_cached_sig = ""


def normalize_es_index_prefix(index: str | None) -> str:
    """配置项是前缀；若误填带日期的完整名则剥掉日期。"""
    name = (index or DEFAULT_ES_INDEX_PREFIX).strip()
    name = _DATE_SUFFIX.sub("", name).rstrip("-")
    return name or DEFAULT_ES_INDEX_PREFIX


def _today_cn() -> str:
    """索引日期用上海时区的 yyyy-mm-dd，跨日即建新索引。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


class LogStore:
    """日志存储接口。实现里不允许把日志写入业务库。"""

    def append(self, task_id: int, line: str) -> None:
        raise NotImplementedError

    def append_batch(self, task_id: int, lines: list[str]) -> None:
        for line in lines:
            self.append(task_id, line)

    def get(self, task_id: int, start: int = 0, limit: int = MAX_FETCH_LINES) -> list[str]:
        raise NotImplementedError

    def count(self, task_id: int) -> int:
        return len(self.get(task_id))

    def try_append_batch(self, task_id: int, lines: list[str]) -> bool:
        """写入归档。成功才返回 True，调用方据此从 Redis 删除。"""
        try:
            self.append_batch(task_id, lines)
            return True
        except Exception:  # noqa: BLE001
            return False

    def name(self) -> str:
        return self.__class__.__name__


class NullLogStore(LogStore):
    """ES / Redis 都不可用时的黑洞：写丢掉、读空列表，不碰数据库。"""

    def append(self, task_id: int, line: str) -> None:
        return

    def append_batch(self, task_id: int, lines: list[str]) -> None:
        return

    def get(self, task_id: int, start: int = 0, limit: int = MAX_FETCH_LINES) -> list[str]:
        return []

    def count(self, task_id: int) -> int:
        return 0

    def try_append_batch(self, task_id: int, lines: list[str]) -> bool:
        return False

    def name(self) -> str:
        return "none"


class MemoryLogStore(LogStore):
    """进程内日志，仅单测替身。生产路径禁止使用：ES 挂了就丢，不能把机器内存当仓库。"""

    def __init__(self) -> None:
        self._lines: dict[int, list[str]] = {}

    def append(self, task_id: int, line: str) -> None:
        self.append_batch(task_id, [line])

    def append_batch(self, task_id: int, lines: list[str]) -> None:
        if not lines:
            return
        self._lines.setdefault(task_id, []).extend(lines)

    def get(self, task_id: int, start: int = 0, limit: int = MAX_FETCH_LINES) -> list[str]:
        rows = self._lines.get(task_id, [])
        return rows[start : start + limit]

    def count(self, task_id: int) -> int:
        return len(self._lines.get(task_id, []))

    def try_append_batch(self, task_id: int, lines: list[str]) -> bool:
        self.append_batch(task_id, lines)
        return True

    def name(self) -> str:
        return "memory"


def es_client_kwargs(
    hosts: list[str],
    username: str = "",
    password: str = "",
    *,
    request_timeout: float = _ES_TIMEOUT,
) -> dict:
    """写入归档和健康探测共用的客户端参数。

    HTTPS 集群通常要账号；证书在内网自签，校验关掉以免探测和写入结果不一致。
    """
    kwargs: dict = {
        "hosts": hosts,
        "request_timeout": request_timeout,
        "max_retries": 0,
        "retry_on_timeout": False,
        "verify_certs": False,
        "ssl_show_warn": False,
    }
    if username and password:
        kwargs["basic_auth"] = (username, password)
    return kwargs


class ESLogStore(LogStore):
    """Elasticsearch 日志归档。构造时不探测集群：ES 挂了不能拖垮 API。"""

    _COOLDOWN = 15.0

    def __init__(self, hosts: list[str], index: str, username: str = "", password: str = ""):
        try:
            from elasticsearch import Elasticsearch
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("未安装 elasticsearch") from e

        self._prefix = normalize_es_index_prefix(index)
        self._ensured: set[str] = set()
        self._index_lock = threading.Lock()
        self._skip_until = 0.0
        self._es = Elasticsearch(
            **es_client_kwargs(hosts, username, password, request_timeout=_ES_TIMEOUT)
        )

    def _cooling(self) -> bool:
        import time

        return time.time() < self._skip_until

    def _mark_down(self, e: BaseException) -> None:
        import time

        self._skip_until = time.time() + self._COOLDOWN
        logger.warning("ES 不可用，%s 秒内不再请求（不回写数据库）：%s", int(self._COOLDOWN), e)

    def _daily_index(self) -> str:
        """当天写入的索引名，例如 rp-exec-logs-2026-09-16。"""
        return f"{self._prefix}-{_today_cn()}"

    def _search_index(self) -> str:
        return f"{self._prefix},{self._prefix}-*"

    def _ensure_index(self, name: str) -> None:
        if name in self._ensured or self._cooling():
            return
        with self._index_lock:
            if name in self._ensured or self._cooling():
                return
            try:
                if not self._es.indices.exists(index=name):
                    self._es.indices.create(
                        index=name,
                        mappings={
                            "properties": {
                                "task_id": {"type": "long"},
                                "line": {"type": "text"},
                                "@timestamp": {"type": "date"},
                                "seq": {"type": "integer"},
                            }
                        },
                    )
                    logger.info("已创建 ES 日志索引: %s", name)
                self._ensured.add(name)
            except Exception as e:  # noqa: BLE001
                self._mark_down(e)

    def append(self, task_id: int, line: str) -> None:
        self.append_batch(task_id, [line])

    def append_batch(self, task_id: int, lines: list[str]) -> None:
        self.try_append_batch(task_id, lines)

    def try_append_batch(self, task_id: int, lines: list[str]) -> bool:
        if not lines or self._cooling():
            return False
        try:
            from elasticsearch.helpers import bulk

            now = datetime.now(timezone.utc).isoformat()
            index = self._daily_index()
            self._ensure_index(index)
            if self._cooling():
                return False
            actions = [
                {
                    "_index": index,
                    "_source": {
                        "task_id": task_id,
                        "line": line,
                        "@timestamp": now,
                        "seq": i,
                    },
                }
                for i, line in enumerate(lines)
            ]
            success, errors = bulk(
                self._es,
                actions,
                raise_on_error=False,
                request_timeout=max(_ES_TIMEOUT, 10.0),
                refresh="wait_for",
            )
            if errors:
                self._mark_down(RuntimeError(f"ES bulk 失败 {len(errors)} 条"))
                return False
            return bool(success) or not lines
        except Exception as e:  # noqa: BLE001
            self._mark_down(e)
            return False

    _WINDOW = 10000

    def get(self, task_id: int, start: int = 0, limit: int = MAX_FETCH_LINES) -> list[str]:
        if self._cooling():
            return []
        frm = min(max(start, 0), self._WINDOW)
        size = max(0, min(limit, self._WINDOW - frm))
        if size == 0:
            total = self.count(task_id)
            return [TRUNCATED_MARK.format(n=max(total - self._WINDOW, 0))]
        try:
            resp = self._es.search(
                index=self._search_index(),
                ignore_unavailable=True,
                allow_no_indices=True,
                query={"term": {"task_id": task_id}},
                sort=[{"@timestamp": "asc"}, {"seq": "asc"}],
                from_=frm,
                size=size,
            )
            lines = [h["_source"].get("line", "") for h in resp["hits"]["hits"]]
            if len(lines) == size:
                total = self.count(task_id)
                if total > frm + size:
                    lines.append(TRUNCATED_MARK.format(n=total - frm - size))
            return lines
        except Exception as e:  # noqa: BLE001
            self._mark_down(e)
            return []

    def count(self, task_id: int) -> int:
        if self._cooling():
            return 0
        try:
            resp = self._es.count(
                index=self._search_index(),
                ignore_unavailable=True,
                allow_no_indices=True,
                query={"term": {"task_id": task_id}},
            )
            return int(resp.get("count", 0))
        except Exception as e:  # noqa: BLE001
            self._mark_down(e)
            return 0

    def name(self) -> str:
        return "elasticsearch"


class RedisStreamLogStore(LogStore):
    """Redis 只缓冲实时日志；ES 写成功后删除；key 1 小时过期。"""

    _REDIS_COOLDOWN = 15.0

    def __init__(self, archive: LogStore):
        self._archive = archive
        self._skip_redis_until = 0.0
        self._archive_lock = threading.Lock()
        self._archive_inflight = False
        self._archive_dirty: set[int] = set()
        self._retry_timer: threading.Timer | None = None

    def _key(self, task_id: int) -> str:
        return STREAM_KEY.format(task_id=task_id)

    def _redis(self):
        from app.core.redis_client import get_redis

        return get_redis()

    def _mark_redis_down(self, e: BaseException) -> None:
        self._skip_redis_until = time.time() + self._REDIS_COOLDOWN
        logger.warning("Redis 不可用，%s 秒内不再连：%s", int(self._REDIS_COOLDOWN), e)

    def _try_redis(self):
        now = time.time()
        if now < self._skip_redis_until:
            return None
        try:
            return self._redis()
        except Exception as e:  # noqa: BLE001
            self._mark_redis_down(e)
            return None

    def append(self, task_id: int, line: str) -> None:
        self.append_batch(task_id, [line])

    def flush_archive(self, task_id: int) -> None:
        """任务结束时再踢一脚归档，把 Redis 里剩下的刷进 ES。"""
        self._offer_archive(task_id)

    def _offer_archive(self, task_id: int) -> None:
        if self._archive.name() == "none":
            return
        with self._archive_lock:
            self._archive_dirty.add(task_id)
        cooling = getattr(self._archive, "_cooling", None)
        if callable(cooling) and cooling():
            self._schedule_retry()
            return
        with self._archive_lock:
            if self._archive_inflight:
                return
            self._archive_inflight = True
        threading.Thread(target=self._drain_archive, name="log-archive", daemon=True).start()

    def _schedule_retry(self) -> None:
        """ES 冷却后再踢一脚，避免任务已经结束、没人再写日志时 Redis 里剩一批。"""
        with self._archive_lock:
            if self._retry_timer is not None:
                return
            timer = threading.Timer(16.0, self._retry_drain)
            timer.daemon = True
            self._retry_timer = timer
        timer.start()

    def _retry_drain(self) -> None:
        with self._archive_lock:
            self._retry_timer = None
            pending = list(self._archive_dirty)
        for tid in pending:
            self._offer_archive(tid)

    def _drain_archive(self) -> None:
        try:
            while True:
                with self._archive_lock:
                    if not self._archive_dirty:
                        return
                    task_id = next(iter(self._archive_dirty))
                status = self._drain_task(task_id)
                with self._archive_lock:
                    if status == "ok":
                        self._archive_dirty.discard(task_id)
                    elif status == "fail":
                        self._schedule_retry()
                        return
                    # more：留在 dirty 里下一圈继续
        finally:
            with self._archive_lock:
                self._archive_inflight = False

    def _drain_task(self, task_id: int) -> str:
        """把 Redis 里未归档的行写入 ES，成功则 XDEL。ok / more / fail。"""
        r = self._try_redis()
        if r is None:
            return "fail"
        key = self._key(task_id)
        cooling = getattr(self._archive, "_cooling", None)
        for _ in range(_DRAIN_MAX_CHUNKS):
            if callable(cooling) and cooling():
                return "fail"
            try:
                rows = r.xrange(key, min="-", max="+", count=_DRAIN_CHUNK)
            except Exception as e:  # noqa: BLE001
                self._mark_redis_down(e)
                return "fail"
            if not rows:
                return "ok"
            ids = [row[0] for row in rows]
            lines = [row[1].get("line", "") for row in rows]
            try:
                ok = bool(self._archive.try_append_batch(task_id, lines))
            except Exception as ae:  # noqa: BLE001
                logger.warning("日志归档失败（%s），Redis 条目先留着：%s", self._archive.name(), ae)
                ok = False
            if not ok:
                return "fail"
            try:
                r.xdel(key, *ids)
            except Exception as e:  # noqa: BLE001
                self._mark_redis_down(e)
                return "fail"
        return "more"

    def append_batch(self, task_id: int, lines: list[str]) -> None:
        if not lines:
            return

        redis_ok = False
        r = self._try_redis()
        if r is not None:
            try:
                pipe = r.pipeline(transaction=False)
                key = self._key(task_id)
                for line in lines:
                    pipe.xadd(key, {"line": line}, maxlen=STREAM_MAXLEN, approximate=True)
                pipe.expire(key, STREAM_TTL_SECONDS)
                pipe.execute()
                try:
                    r.xtrim(
                        key,
                        minid=f"{int((time.time() - STREAM_TTL_SECONDS) * 1000)}-0",
                        approximate=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                redis_ok = True
            except Exception as e:  # noqa: BLE001
                self._mark_redis_down(e)

        if redis_ok:
            self._offer_archive(task_id)
            return

        try:
            self._archive.append_batch(task_id, lines)
        except Exception as ae:  # noqa: BLE001
            logger.warning("ES 直写也失败，丢弃本批日志：%s", ae)

    def _redis_lines(self, task_id: int, start: int, limit: int) -> list[str]:
        r = self._try_redis()
        if r is None or limit <= 0:
            return []
        try:
            rows = r.xrange(self._key(task_id), min="-", max="+", count=start + limit)
            if not rows:
                return []
            return [fields.get("line", "") for _id, fields in rows[start : start + limit]]
        except Exception as e:  # noqa: BLE001
            self._mark_redis_down(e)
            return []

    def get(self, task_id: int, start: int = 0, limit: int = MAX_FETCH_LINES) -> list[str]:
        start = max(start, 0)
        limit = max(0, min(limit, MAX_FETCH_LINES))
        if limit == 0:
            return []
        try:
            es_n = int(self._archive.count(task_id) or 0)
        except Exception:  # noqa: BLE001
            es_n = 0
        out: list[str] = []
        if start < es_n:
            try:
                out = list(self._archive.get(task_id, start=start, limit=limit) or [])
            except Exception:  # noqa: BLE001
                out = []
            if out and out[-1].startswith("…"):
                return out
        need = limit - len(out)
        if need <= 0:
            return out
        redis_start = max(0, start - es_n + (len(out) if start < es_n else 0))
        if start >= es_n:
            redis_start = start - es_n
        out.extend(self._redis_lines(task_id, redis_start, need))
        return out

    def count(self, task_id: int) -> int:
        try:
            es_n = int(self._archive.count(task_id) or 0)
        except Exception:  # noqa: BLE001
            es_n = 0
        redis_n = 0
        r = self._try_redis()
        if r is not None:
            try:
                redis_n = int(r.xlen(self._key(task_id)) or 0)
            except Exception as e:  # noqa: BLE001
                self._mark_redis_down(e)
        return es_n + redis_n

    def latest_id(self, task_id: int) -> str:
        """当前 Redis Stream 最新 ID，给 SSE 接着 XREAD；没有则 0-0。"""
        r = self._try_redis()
        if r is None:
            return "0-0"
        try:
            rows = r.xrevrange(self._key(task_id), max="+", min="-", count=1)
            if rows:
                return rows[0][0]
        except Exception as e:  # noqa: BLE001
            self._mark_redis_down(e)
        return "0-0"

    def read_since(self, task_id: int, last_id: str = "0-0", count: int = 200) -> tuple[str, list[str]]:
        """从 last_id 之后读取增量，返回 (new_last_id, lines)。"""
        try:
            r = self._try_redis()
            if r is None:
                return last_id, []
            rows = r.xrange(
                self._key(task_id),
                min=f"({last_id}" if last_id != "0-0" else "-",
                max="+",
                count=count,
            )
            if not rows:
                return last_id, []
            lines = [fields.get("line", "") for _id, fields in rows]
            return rows[-1][0], lines
        except Exception as e:  # noqa: BLE001
            self._mark_redis_down(e)
            return last_id, []

    def name(self) -> str:
        return f"redis+{self._archive.name()}"


def _build_es_store(cfg: dict, *, index: str | None = None) -> LogStore:
    hosts = cfg.get("es_hosts", "http://localhost:9200")
    host_list = [h.strip() for h in str(hosts).split(",") if h.strip()]
    if not host_list:
        logger.warning("es_hosts 为空，日志无法归档到 ES")
        return NullLogStore()
    try:
        return ESLogStore(
            host_list,
            normalize_es_index_prefix(index if index is not None else cfg.get("es_index")),
            cfg.get("es_username", "") or "",
            cfg.get("es_password", "") or "",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ES 客户端创建失败，本进程不写日志归档：%s", e)
        return NullLogStore()


def _with_redis(archive: LogStore) -> LogStore:
    # 不在创建时 ping Redis：挂了也要让发布继续。写入路径自己降级。
    return RedisStreamLogStore(archive)


def _cfg_sig(cfg: dict) -> str:
    return "|".join(
        str(cfg.get(k, ""))
        for k in ("es_hosts", "es_index", "es_username", "es_password", "redis_host", "redis_port", "redis_db")
    )


def reset_log_store() -> None:
    """配置变更后丢掉缓存的客户端。"""
    global _cached_store, _cached_sig
    with _store_lock:
        _cached_store = None
        _cached_sig = ""


def create_log_store(session_factory=None, settings_provider=None) -> LogStore:
    """创建日志存储。session_factory 已废弃，保留参数以免调用方改一堆。

    永远不写 MySQL，也永远不把日志攒在进程内存。
    log_storage 即使填了 mysql / redis 也走 Redis+ES；两边都挂就丢。
    """
    del session_factory
    cfg = settings_provider() if settings_provider else {}
    storage = (cfg.get("log_storage") or "es").strip().lower()
    if storage in {"mysql", "redis"}:
        logger.warning("已忽略 log_storage=%s：构建日志只允许 Redis+ES，禁止回写数据库或落内存", storage)

    sig = _cfg_sig(cfg)
    global _cached_store, _cached_sig
    with _store_lock:
        if _cached_store is not None and _cached_sig == sig:
            return _cached_store
        store = _with_redis(_build_es_store(cfg))
        _cached_store = store
        _cached_sig = sig
        return store
