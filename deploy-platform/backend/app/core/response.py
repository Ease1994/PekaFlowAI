"""统一响应结构与业务异常。

响应格式（与文档 §9 约定一致）：
    {"code": 0, "message": "ok", "data": {...}}

R.ok 会自动将 SQLAlchemy ORM 对象 / datetime / Pydantic 模型
序列化为可 JSON 编码的 dict，避免路由层手工转换。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


def _serialize(obj: Any, *, omit: frozenset[str] | set[str] | None = None) -> Any:
    """递归序列化：ORM 对象 → dict，datetime → ISO 字符串。

    omit 跳过这些列，既不进 JSON，也不触发 SQLAlchemy 对 defer 列的懒加载。
    列表接口用它丢掉 YAML / 发布快照，避免「响应里 pop 了、查询时已经搬进内存」。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_serialize(i) for i in obj]
    if isinstance(obj, BaseModel):
        return obj.model_dump(by_alias=True)
    # SQLAlchemy ORM 对象
    if hasattr(obj, "__table__"):
        skip = omit or set()
        return {
            c.key: _serialize(getattr(obj, c.key))
            for c in obj.__table__.columns
            if c.key not in skip
        }
    return str(obj)


class R(BaseModel, Generic[T]):
    """统一响应结构。"""

    code: int = 0
    message: str = "ok"
    data: T | None = None

    @classmethod
    def ok(cls, data: Any = None, message: str = "ok") -> "R":
        return cls(code=0, message=message, data=_serialize(data))

    @classmethod
    def fail(cls, message: str, code: int = 500) -> "R":
        return cls(code=code, message=message, data=None)


class BizException(Exception):
    """业务异常。"""

    def __init__(self, message: str, code: int = 500):
        self.message = message
        self.code = code
        super().__init__(message)

    @classmethod
    def not_found(cls, resource: str) -> "BizException":
        return cls(f"{resource} 不存在", code=404)

    @classmethod
    def forbidden(cls, message: str = "无权限执行此操作") -> "BizException":
        return cls(message, code=403)

    @classmethod
    def bad_request(cls, message: str) -> "BizException":
        return cls(message, code=400)

    @classmethod
    def unauthorized(cls, message: str = "未登录或登录已过期") -> "BizException":
        return cls(message, code=401)

    @classmethod
    def service_unavailable(cls, message: str = "服务不可用") -> "BizException":
        return cls(message, code=503)
