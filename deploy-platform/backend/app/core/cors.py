"""浏览器跨域白名单。

生产前端经 Nginx 同源反代，多数请求不走 CORS；开发机或直连 :8080 时才需要。
来源：环境变量 CORS_ORIGINS（逗号分隔）+ 平台设置 public_app_base。
不再使用 * 配 credentials。
"""
from __future__ import annotations

import os


def cors_allow_origins() -> list[str]:
    """当前允许的 Origin 列表，去尾斜杠、去重、保持顺序。"""
    seen: list[str] = []

    def add(raw: object) -> None:
        url = str(raw or "").strip().rstrip("/")
        if url and url not in seen:
            seen.append(url)

    for part in (os.environ.get("CORS_ORIGINS") or "").split(","):
        add(part)
    try:
        from app.db.session import SessionLocal
        from app.modules.settings import get_setting

        with SessionLocal() as db:
            add(get_setting(db, "public_app_base"))
    except Exception:  # noqa: BLE001
        pass
    return seen


def origin_allowed(origin: str) -> bool:
    """请求 Origin 是否在白名单。比较时去掉尾斜杠。"""
    if not origin:
        return False
    needle = origin.strip().rstrip("/")
    return needle in cors_allow_origins()


class AllowlistCORSMiddleware:
    """按白名单回 CORS 头。预检 OPTIONS 直接 200，不进业务路由。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        origin = headers.get("origin", "")
        allowed = origin_allowed(origin)

        if scope.get("method") == "OPTIONS" and origin and allowed:
            req_headers = headers.get("access-control-request-headers") or "*"
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"access-control-allow-origin", origin.encode("latin-1")),
                        (b"access-control-allow-credentials", b"true"),
                        (
                            b"access-control-allow-methods",
                            b"GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD",
                        ),
                        (b"access-control-allow-headers", req_headers.encode("latin-1")),
                        (b"access-control-max-age", b"600"),
                        (b"vary", b"Origin"),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and origin and allowed:
                extra = [
                    (b"access-control-allow-origin", origin.encode("latin-1")),
                    (b"access-control-allow-credentials", b"true"),
                    (b"vary", b"Origin"),
                ]
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_wrapper)
