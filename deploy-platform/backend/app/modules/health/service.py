"""探测 MySQL、Redis、ES 是否可达，给顶栏告警用。

发布路径里这些依赖挂了是静默降级（定时不跑、日志丢失）。
这里主动探一次，把错误原文带给运维，避免「保存成功但其实没生效」。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

_CACHE_SECONDS = 15.0
_PROBE_TIMEOUT = 2.0

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_at = 0.0


def _component(name: str, status: str, detail: str) -> dict[str, str]:
    """一条依赖的探测结果。status：ok / down。"""
    return {"name": name, "status": status, "detail": detail}


def _probe_mysql(db: Session) -> dict[str, str]:
    """用当前请求的会话跑 SELECT 1，失败说明库连不上或事务已坏。"""
    try:
        db.execute(text("SELECT 1"))
        return _component("MySQL", "ok", "查询正常")
    except Exception as e:  # noqa: BLE001
        return _component("MySQL", "down", str(e)[:400])


def _probe_redis() -> dict[str, str]:
    """独立 ping，不走业务冷却，好把真实错误带给顶栏。"""
    try:
        from app.core.redis_client import get_redis

        client = get_redis()
        client.ping()
        return _component("Redis", "ok", "PONG")
    except Exception as e:  # noqa: BLE001
        return _component("Redis", "down", str(e)[:400])


def _probe_es() -> dict[str, str]:
    """按平台设置连 ES。账号、超时和写入归档同一套，探测用 GET / 而不是 ping。

    ping() 发 HEAD /，开了安全的 HTTPS 集群常返回 401 且被客户端吞成 False，
    页面就显示「ping 失败」，而带账号的 _bulk 其实一直在写。
    """
    es = None
    try:
        from elasticsearch import Elasticsearch

        from app.db.session import SessionLocal
        from app.modules.agent.log_store import es_client_kwargs
        from app.modules.settings import get_all_settings

        with SessionLocal() as sdb:
            cfg = get_all_settings(sdb)
        hosts = cfg.get("es_hosts", "http://localhost:9200")
        host_list = [h.strip() for h in str(hosts).split(",") if h.strip()]
        if not host_list:
            return _component("Elasticsearch", "down", "未配置 es_hosts，构建日志无法归档")
        es = Elasticsearch(
            **es_client_kwargs(
                host_list,
                cfg.get("es_username") or "",
                cfg.get("es_password") or "",
                request_timeout=_PROBE_TIMEOUT,
            )
        )
        info = es.info()
        body = info if isinstance(info, dict) else getattr(info, "body", None)
        cluster = str((body or {}).get("cluster_name") or "").strip()
        detail = f"可达 {host_list[0]}"
        if cluster:
            detail += f"（{cluster}）"
        return _component("Elasticsearch", "ok", detail)
    except Exception as e:  # noqa: BLE001
        return _component("Elasticsearch", "down", str(e)[:400])
    finally:
        if es is not None:
            try:
                es.close()
            except Exception:  # noqa: BLE001
                pass


def probe_deps(db: Session, *, force: bool = False) -> dict[str, Any]:
    """探测三项依赖。结果缓存十几秒，避免每个页面轮询都打满连接。"""
    global _cached, _cached_at
    now = time.time()
    with _lock:
        if not force and _cached is not None and now - _cached_at < _CACHE_SECONDS:
            return _cached

    mysql = _probe_mysql(db)
    redis = _probe_redis()
    es = _probe_es()
    items = [mysql, redis, es]
    down = [c for c in items if c["status"] != "ok"]
    payload = {
        "status": "ok" if not down else "degraded",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "components": items,
        "alerts": [{"name": c["name"], "detail": c["detail"]} for c in down],
    }
    with _lock:
        _cached = payload
        _cached_at = now
    return payload


def reset_health_cache() -> None:
    """测试或配置变更后丢掉缓存。"""
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0
