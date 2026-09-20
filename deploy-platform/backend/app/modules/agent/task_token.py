"""插件进程用的任务级 token。

构建机上的插件是 Agent 起的子进程，此前 Agent 把自己的长期 token 注入进去，
插件因此能以构建机身份调任何 Agent 接口 —— 领走别人的任务（连同 variables 里的密钥）、
把任意任务标成成功、下载任意插件包。插件代码来源不可控（尤其后面还要让 AI 生成），
这个口子必须收掉。

这里签发的 token 只绑定一个 task_id，随任务下发、跟着任务过期，
能做的事只有「以本任务的名义启动子流水线并查它的状态」。
无状态签名，不落库，任务本身结束后即便未过期也会被接口按任务状态挡下。
"""
from __future__ import annotations

import hashlib
import hmac
import time

from app.core.config import settings
from app.core.response import BizException

# 单个 Job 的执行上限是 600s，留足重试余量即可
TOKEN_TTL_SECONDS = 6 * 60 * 60


def _sign(expire_at: int, task_id: int) -> str:
    msg = f"{expire_at}.{task_id}".encode()
    return hmac.new(settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()


def issue(task_id: int) -> str:
    expire_at = int(time.time()) + TOKEN_TTL_SECONDS
    return f"t1.{expire_at}.{task_id}.{_sign(expire_at, task_id)}"


def verify(token: str) -> int:
    """校验并返回 task_id；不通过一律抛错（fail closed）。"""
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != "t1":
        raise BizException.forbidden("任务凭证无效")
    _, raw_expire, raw_task, signature = parts
    try:
        expire_at = int(raw_expire)
        task_id = int(raw_task)
    except ValueError:
        raise BizException.forbidden("任务凭证无效") from None

    if expire_at < time.time():
        raise BizException.forbidden("任务凭证已过期")
    if not hmac.compare_digest(signature, _sign(expire_at, task_id)):
        raise BizException.forbidden("任务凭证无效")
    return task_id
