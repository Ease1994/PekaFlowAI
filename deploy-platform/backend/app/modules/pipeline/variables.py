"""执行期变量：把流水线变量和系统变量渲染进步骤参数。

占位符两种写法都认（对齐蓝盾）：`${VAR}` 和 `${{VAR}}`。

只替换**已知**变量名，认不出来的原样留着 —— 这样 shell 脚本里的 `${HOME}`、
`${PATH}` 不会被平台吃掉，仍旧交给 bash 自己展开。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

# ${{VAR}} 要先于 ${VAR} 匹配，否则会把外层花括号剩下来
_DOUBLE = re.compile(r"\$\{\{\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*\}\}")
_SINGLE = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*\}")

# 变量互相引用的展开轮数上限（version = v1.0.${{BK_CI_BUILD_NUM}} 这类）
_MAX_PASSES = 5


def render_text(text: str, values: dict[str, str]) -> str:
    """把一段文本里的占位符替换成变量值，未知变量原样保留。"""
    if not text or "${" not in text:
        return text

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        return values[name] if name in values else m.group(0)

    out = text
    for _ in range(_MAX_PASSES):
        replaced = _SINGLE.sub(sub, _DOUBLE.sub(sub, out))
        if replaced == out:
            break
        out = replaced
    return out


def render_any(value: Any, values: dict[str, str]) -> Any:
    """递归渲染任意结构里的字符串（步骤参数可能是嵌套 dict/list）。"""
    if isinstance(value, str):
        return render_text(value, values)
    if isinstance(value, dict):
        return {k: render_any(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [render_any(v, values) for v in value]
    return value


def pipeline_version_stamp(pipeline) -> str:
    """BK_CI_PIPELINE_VERSION：按流水线最近一次保存时刻生成，形如 V20260826160732。

    比自增的 1、2、31 好认，也避免制品/目录用 v1 这种短号互相覆盖。
    用到秒是为了同一分钟内连点保存也不撞；同一秒内两次保存仍可能相同，极罕见。
    """
    t = getattr(pipeline, "updated_at", None) or getattr(pipeline, "created_at", None)
    if t is None:
        t = datetime.now()
    elif isinstance(t, str):
        try:
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            t = datetime.now()
    return "V" + t.strftime("%Y%m%d%H%M%S")


def _start_type(trigger_by: str | None) -> str:
    return {
        "manual": "MANUAL",
        "rebuild": "MANUAL",
        "ai": "SERVICE",
        "cron": "TIME_TRIGGER",
        "schedule": "TIME_TRIGGER",
        "webhook": "WEB_HOOK",
        "sub_pipeline": "PIPELINE",
    }.get((trigger_by or "manual").lower(), "MANUAL")


def system_variables(db: Session, release, pipeline) -> dict[str, str]:
    """本次构建可确定的系统变量。

    只放我们真的填得出来的：像 commit id 这类要等 git-checkout 跑完才知道，
    不在这里假装支持，免得脚本里拿到空串还以为是自己写错了。
    """
    from app.modules.auth.models import User
    from app.modules.project.models import Project

    values: dict[str, str] = {
        "BK_CI_PIPELINE_ID": str(pipeline.id),
        "BK_CI_PIPELINE_NAME": pipeline.name or "",
        "BK_CI_PIPELINE_VERSION": pipeline_version_stamp(pipeline),
    }
    project = db.get(Project, pipeline.project_id)
    values["BK_CI_PROJECT_NAME"] = project.name if project else ""
    values["BK_CI_PROJECT_NAME_CN"] = project.name if project else ""
    if release is None:
        return values

    user = db.get(User, release.operator_id) if release.operator_id else None
    started = release.started_at or release.created_at
    build_num = release.build_number or release.id
    values.update({
        "BK_CI_BUILD_ID": str(release.id),
        "BK_CI_BUILD_NUM": str(build_num),
        "BK_CI_BUILD_NO": str(build_num),
        "BK_CI_BUILD_START_TIME": (
            str(int(started.timestamp() * 1000)) if started else ""
        ),
        "BK_CI_START_TYPE": _start_type(release.trigger_by),
        "BK_CI_START_USER_ID": str(release.operator_id or ""),
        "BK_CI_START_USER_NAME": (user.username if user else ""),
        "BK_CI_VERSION": release.version or "",
    })
    if release.source_ref:
        values["BK_CI_GIT_REPO_HEAD_COMMIT_ID"] = release.source_ref
    return values


def build_context(
    db: Session, release, pipeline, variables: list, overrides: dict | None = None
) -> dict[str, str]:
    """系统变量 + 自定义变量（执行时填的值优先于默认值），彼此可互相引用。"""
    values = system_variables(db, release, pipeline)

    raw: dict[str, str] = {}
    for v in variables or []:
        name = getattr(v, "name", None) or (v.get("name") if isinstance(v, dict) else None)
        if not name:
            continue
        default = getattr(v, "default_value", None)
        if default is None and isinstance(v, dict):
            default = v.get("default_value")
        raw[str(name)] = "" if default is None else str(default)

    for name, val in (overrides or {}).items():
        if val is None:
            continue
        if isinstance(val, list):
            raw[str(name)] = ",".join(str(x) for x in val)
        elif isinstance(val, bool):
            raw[str(name)] = "true" if val else "false"
        else:
            raw[str(name)] = str(val)

    # 自定义变量的值本身可以引用系统变量或别的自定义变量
    for _ in range(_MAX_PASSES):
        rendered = {k: render_text(v, {**values, **raw}) for k, v in raw.items()}
        if rendered == raw:
            break
        raw = rendered

    values.update(raw)
    return values
