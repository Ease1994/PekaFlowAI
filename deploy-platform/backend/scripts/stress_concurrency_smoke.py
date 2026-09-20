#!/usr/bin/env python3
"""百并发 / 实时日志验收烟雾脚本。

用法:
  # 仅静态检查（不连 Redis）
  python scripts/stress_concurrency_smoke.py --static-only

  # Redis Stream 延迟（需在 backend 目录且已装 redis + 可连 Redis）
  python scripts/stress_concurrency_smoke.py --rounds 50

验收目标:
  - Redis Stream 往返 p95 约 <= 300ms（内网）
  - Agent: --concurrency N，多机合计约 100
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import uuid


def static_checks() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    checks = [
        ("agent jar", os.path.join(root, "agent-java", "deploy-agent.jar")),
        ("LogBatcher.java", os.path.join(root, "agent-java", "src", "com", "pekaflow", "agent", "LogBatcher.java")),
        ("log_store.py", os.path.join(root, "app", "modules", "agent", "log_store.py")),
        ("ExecutionDetail SSE", os.path.join(root, "..", "frontend", "src", "pages", "ExecutionDetail.tsx")),
    ]
    for name, path in checks:
        if not os.path.isfile(path):
            print(f"[fail] missing {name}: {path}")
            sys.exit(1)
        print(f"[ok] {name}")

    with open(checks[3][1], encoding="utf-8") as f:
        fe = f.read()
    if "logs/stream" not in fe or "EventSource" not in fe:
        print("[fail] ExecutionDetail 未改用 SSE")
        sys.exit(1)
    if "refetchInterval" in fe and "release-step-log" in fe:
        print("[fail] ExecutionDetail 仍在轮询 step log")
        sys.exit(1)
    print("[ok] ExecutionDetail 使用 EventSource /tasks/.../logs/stream")

    with open(os.path.join(root, "app", "modules", "agent", "task_service.py"), encoding="utf-8") as f:
        ts = f.read()
    if "skip_locked" not in ts:
        print("[fail] fetch_task 缺少 SKIP LOCKED")
        sys.exit(1)
    print("[ok] fetch_task FOR UPDATE SKIP LOCKED")

    with open(os.path.join(root, "agent-java", "src", "com", "pekaflow", "agent", "AgentMain.java"), encoding="utf-8") as f:
        am = f.read()
    if "/status" not in am or "concurrency" not in am.lower():
        print("[fail] Agent 缺少 concurrency / cancel 短轮询")
        sys.exit(1)
    print("[ok] Agent concurrency + cancel 短轮询")


def measure_redis_stream_latency(rounds: int = 50) -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        from app.core.redis_client import get_redis, redis_available
    except Exception as e:  # noqa: BLE001
        print(f"[skip] 无法导入 redis_client: {e}")
        return

    if not redis_available():
        print("[skip] Redis 不可用（部署环境请确认 REDIS_* / 平台设置）")
        return

    r = get_redis()
    key = f"release:stress:{uuid.uuid4().hex}:logs"
    latencies_ms: list[float] = []
    last_id = "0-0"
    try:
        for i in range(rounds):
            t0 = time.perf_counter()
            eid = r.xadd(key, {"line": f"stress-{i}"}, maxlen=1000, approximate=True)
            rows = r.xread({key: last_id}, count=10, block=500)
            last_id = eid
            if not rows:
                continue
            latencies_ms.append((time.perf_counter() - t0) * 1000)
        if not latencies_ms:
            print("[fail] Redis Stream 无读回")
            sys.exit(3)
        p50 = statistics.median(latencies_ms)
        p95 = sorted(latencies_ms)[max(0, int(len(latencies_ms) * 0.95) - 1)]
        print(f"[ok] Redis Stream 往返 rounds={rounds} p50={p50:.1f}ms p95={p95:.1f}ms")
        if p95 > 300:
            print("[warn] p95 > 300ms，未达内网延迟目标")
        else:
            print("[ok] 延迟目标约 100～300ms 内（本机 Redis 路径）")
    finally:
        try:
            r.delete(key)
        except Exception:  # noqa: BLE001
            pass


def print_agent_recipe(concurrency: int, agents: int, base: str) -> None:
    total = concurrency * agents
    print()
    print("=== Agent 压测建议 ===")
    print(f"目标合计并发 ≈ {total}（每机 --concurrency {concurrency} × {agents} 台）")
    print(
        f"  java -jar deploy-agent.jar --server {base} "
        f"--name agent-stress-1 --tags linux --concurrency {concurrency}"
    )
    print("验收: 无双派发 / 无大面积 5xx / 明细页日志延迟约 <=300ms / 刷新可回看历史")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--agents", type=int, default=13)
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()

    static_checks()
    if not args.static_only:
        measure_redis_stream_latency(args.rounds)
    print_agent_recipe(args.concurrency, args.agents, args.base)


if __name__ == "__main__":
    main()
