"""失败执行的诊断正文：与站内通知同一份，默认不重新请求模型。"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, check_permission
from app.core.response import BizException
from app.db.session import SessionLocal
from app.modules.agent.models import BuildTask
from app.modules.agent.task_service import get_task_logs
from app.modules.pipeline.models import Pipeline, Release

MAX_CHARS = 12000
TAIL_LINES = 80
ERROR_RE = re.compile(
    r"(error|exception|fatal|failed|failure|denied|refused|timeout|"
    r"unauthorized|forbidden|traceback|panic|oom|killed|"
    r"exit\s*[1-9]|status\s*[1-9]|http\s*[45]\d\d|"
    r"异常|错误|失败|拒绝|超时|无法|不存在|找不到)",
    re.I,
)
# Agent 用来切分步骤的机器标记。原样喂给模型会被当成日志内容读：
# ##[step]end:0:failed:11 末尾是耗时毫秒数，模型却把它当成「第 11 步出错」。
# 边界信息本身有用（能看出错在哪一步），所以翻译成人话而不是删掉。
STEP_BEGIN_RE = re.compile(r"^##\[step\]begin:(\d+):(.*)$")
STEP_END_RE = re.compile(r"^##\[step\]end:(\d+):(\w+):(\d+)$")
STEP_STATUS_CN = {"success": "成功", "failed": "失败", "timeout": "超时", "skipped": "跳过"}


def _humanize_markers(lines: list[str]) -> list[str]:
    """把 Agent 的 ##[step] 标记翻成中文分隔行，避免模型把耗时毫秒当成步骤号。"""
    out: list[str] = []
    for ln in lines:
        raw = ln.rstrip("\n")
        m = STEP_BEGIN_RE.match(raw)
        if m:
            out.append(f"===== 步骤 {int(m.group(1)) + 1}（{m.group(2)}）开始 =====")
            continue
        m = STEP_END_RE.match(raw)
        if m:
            status = STEP_STATUS_CN.get(m.group(2), m.group(2))
            out.append(
                f"===== 步骤 {int(m.group(1)) + 1} 结束：{status}，"
                f"耗时 {int(m.group(3))} 毫秒 ====="
            )
            continue
        out.append(raw)
    return out


def _trim_failed_log(lines: list[str]) -> tuple[str, str]:
    """返回 (正文, 精简说明)。优先错误行 + 末尾若干行。"""
    cleaned = _humanize_markers([ln for ln in (lines or []) if ln is not None])
    if not cleaned:
        return "（该失败步骤无日志）", "empty"
    if sum(len(x) + 1 for x in cleaned) <= MAX_CHARS:
        return "\n".join(cleaned), "full"

    err_idx = [i for i, ln in enumerate(cleaned) if ERROR_RE.search(ln)]
    keep: dict[int, str] = {}
    for i in err_idx[-120:]:
        for j in range(max(0, i - 1), min(len(cleaned), i + 2)):
            keep[j] = cleaned[j]
    start = max(0, len(cleaned) - TAIL_LINES)
    for i in range(start, len(cleaned)):
        keep[i] = cleaned[i]
    ordered = [keep[i] for i in sorted(keep)]
    text = "\n".join(ordered)
    if len(text) > MAX_CHARS:
        text = text[-MAX_CHARS:]
        text = "…(截断)\n" + text
    note = f"trimmed: raw_lines={len(cleaned)} kept={len(ordered)} err_hits={len(err_idx)}"
    return text, note


def collect_failure_context(db: Session, release: Release) -> dict[str, Any]:
    pipeline = db.get(Pipeline, release.pipeline_id)
    tasks = (
        db.query(BuildTask)
        .filter(BuildTask.release_id == release.id)
        .order_by(BuildTask.id)
        .all()
    )
    ok_names: list[str] = []
    failed_blocks: list[dict[str, Any]] = []
    for t in tasks:
        label = f"{t.stage_name}/{t.job_name}" if t.stage_name else (t.job_name or f"task-{t.id}")
        if t.status == "success":
            ok_names.append(label)
            continue
        if t.status not in ("failed", "timeout"):
            continue
        logs = get_task_logs(db, SessionLocal, t.id)
        body, note = _trim_failed_log(logs)
        plugins = []
        try:
            import json

            for s in json.loads(t.steps_json or "[]"):
                if isinstance(s, dict) and s.get("plugin"):
                    plugins.append(str(s["plugin"]))
        except Exception:  # noqa: BLE001
            pass
        failed_blocks.append(
            {
                "task_id": t.id,
                "label": label,
                "status": t.status,
                "plugins": plugins,
                "trim": note,
                "log": body,
            }
        )
    if not failed_blocks:
        from app.modules.pipeline.service import release_error_summary

        startup = release_error_summary(release, max_len=4000, db=db)
        if startup:
            failed_blocks.append(
                {
                    "task_id": 0,
                    "label": "发布启动",
                    "status": release.status,
                    "plugins": [],
                    "trim": "startup",
                    "log": startup,
                }
            )
        else:
            raise BizException.bad_request("未找到失败步骤日志，无法诊断（可能尚未上报完成）")
    return {
        "pipeline_name": pipeline.name if pipeline else f"#{release.pipeline_id}",
        "pipeline_id": release.pipeline_id,
        "release_id": release.id,
        "version": release.version,
        "status": release.status,
        "source_ref": release.source_ref or "",
        "success_steps": ok_names,
        "failed_steps": failed_blocks,
    }


def build_user_prompt(ctx: dict[str, Any]) -> str:
    parts = [
        f"流水线：{ctx['pipeline_name']} (id={ctx['pipeline_id']})",
        f"构建号 / 发布：#{ctx['release_id']}  version={ctx['version']}  status={ctx['status']}",
        f"代码版本：{ctx['source_ref'] or '（无）'}",
        "",
        "已成功的步骤（仅名称，日志未附）：",
        ("、".join(ctx["success_steps"]) if ctx["success_steps"] else "（无）"),
        "",
        "以下仅为失败步骤日志：",
    ]
    for i, block in enumerate(ctx["failed_steps"], 1):
        plugins = ",".join(block["plugins"]) or "unknown"
        parts.append(
            f"\n----- 失败步骤 {i}: {block['label']}  plugins={plugins}  status={block['status']} -----"
        )
        parts.append(block["log"])
    return "\n".join(parts)


def diagnose_release(
    db: Session,
    release_id: int,
    current: CurrentUser,
    model_id: int | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """默认返回通知里那份摘要+建议；缓存没捕到真实日志，或 force，才重新 format_one。"""
    from app.modules.ai.followup import (
        cached_diagnosis_covers_logs,
        format_one,
        load_cached_diagnosis,
    )
    from app.modules.pipeline.service import get_release

    _ = model_id

    release = get_release(db, release_id)
    if not check_permission(db, current, "pipeline", release.pipeline_id, "read"):
        raise BizException.forbidden(f"无权限：查看流水线 #{release.pipeline_id}")
    if release.status not in ("failed", "cancelled"):
        raise BizException.bad_request("仅失败（或已取消）的执行可做 AI 诊断")

    source = "generated"
    if not force:
        cached = load_cached_diagnosis(db, release)
        if cached and cached_diagnosis_covers_logs(db, release, cached):
            db.commit()
            return _present(release, cached, "notice")
    text = format_one(db, release)
    db.commit()
    return _present(release, text, source)


def _present(release: Release, text: str, source: str) -> dict[str, Any]:
    """执行页抽屉和通知展示同一份正文。"""
    reused = source == "notice"
    return {
        "release_id": release.id,
        "pipeline_id": release.pipeline_id,
        "model": {
            "name": "发布失败通知" if reused else "失败摘要",
            "model_id": source,
            "provider_name": "复用" if reused else "生成",
        },
        "failed_steps": [],
        "success_steps": [],
        "diagnosis": text,
        "source": source,
    }
