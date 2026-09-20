"""企业微信登录路由（OAuth2 网页授权）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.response import BizException, R
from app.core.security import create_access_token
from app.db.session import get_db
from app.modules.settings import get_all_settings
from app.modules.settings.branding import session_expire_seconds
from app.modules.auth import wecom_service
from app.modules.pm.wecom import _app_base

router = APIRouter(prefix="/auth/wecom", tags=["企业微信登录"])


@router.get("/status", summary="企微登录是否已配置")
def wecom_status(db: Session = Depends(get_db)):
    cfg = get_all_settings(db)
    return R.ok({"enabled": wecom_service.wecom_ready(cfg)})


@router.get("/authorize-url", summary="生成企微授权链接")
def wecom_authorize_url(
    scope: str = Query("snsapi_base"),
    db: Session = Depends(get_db),
):
    cfg = get_all_settings(db)
    if not wecom_service.wecom_ready(cfg):
        raise BizException.service_unavailable("企业微信登录未启用或未配置完整")
    if scope not in ("snsapi_base", "snsapi_privateinfo"):
        raise BizException.bad_request("scope 仅支持 snsapi_base 或 snsapi_privateinfo")

    state = wecom_service.make_state(app_settings.jwt_secret)
    url = wecom_service.build_authorize_url(cfg, state, scope)
    return R.ok({"authorize_url": url, "state": state, "scope": scope})


@router.get("/callback", response_class=HTMLResponse, summary="企微授权回调")
def wecom_callback(
    code: str = Query(...),
    state: str | None = Query(None),
    db: Session = Depends(get_db),
):
    cfg = get_all_settings(db)
    if not wecom_service.wecom_ready(cfg):
        raise BizException.service_unavailable("企业微信登录未启用")

    # 校验 state 防 CSRF
    if not wecom_service.verify_state(state, app_settings.jwt_secret):
        raise BizException.bad_request("state 校验失败（可能已过期或被篡改）")

    try:
        user = wecom_service.login_user_via_wecom_code(db, code, cfg)
    except RuntimeError as e:
        raise BizException.bad_request(str(e))

    token = create_access_token(
        user.id, user.username, user.is_admin, expire_seconds=session_expire_seconds(db)
    )
    # 桌面登录用弹窗 + postMessage；企微 H5 没有 opener，回跳平台登录页带上 token。
    app_base = json.dumps(_app_base(cfg) or "")
    token_js = json.dumps(token)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>登录成功</title></head><body>
    <p>企业微信登录成功，请关闭本页或返回应用。</p>
    <script>
      (function () {{
        var token = {token_js};
        var base = {app_base};
        if (window.opener) {{
          try {{
            window.opener.postMessage({{ type: "wecom_token", access_token: token }}, "*");
          }} catch (e) {{}}
          try {{ window.close(); }} catch (e) {{}}
          return;
        }}
        if (base) {{
          location.replace(base + "/login#wecom_token=" + encodeURIComponent(token));
        }}
      }})();
    </script>
    </body></html>"""
    return HTMLResponse(content=html)
