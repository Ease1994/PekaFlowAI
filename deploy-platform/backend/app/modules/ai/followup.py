"""助手发起的发布跟进：完成后把状态写回会话。失败附错误摘要和建议。"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ai.history import append_message
from app.modules.ai.models import AiWatch
from app.modules.agent.models import BuildTask
from app.modules.pipeline.models import Pipeline, Release

logger = logging.getLogger(__name__)

TERMINAL = {"success", "failed", "cancelled", "rolled_back"}
# 候选行：工具原文常打成 [INFO]，不能只认 [ERROR]。npm 写 ERR 不写 error。
ERROR_RE = re.compile(
    r"(error|exception|fatal|failed|failure|denied|refused|timeout|"
    r"unauthorized|forbidden|traceback|panic|oom|killed|"
    r"exit\s*[1-9]|status\s*[1-9]|http\s*[45]\d\d|"
    r"npm\s*err|no such file|not found|cannot find|could not find|"
    r"异常|错误|失败|拒绝|超时|无法|不存在|找不到|权限)",
    re.I,
)
# 两类没有信息量的行：Agent 划分步骤用的控制标记，以及平台自己打的收尾语。
# 它们混进摘要的后果不只是啰嗦——##[step]end:0:failed:11 里的 failed 会被当成
# 错误关键词命中，末尾的 11 其实是耗时毫秒数，模型却照着它建议「检查第 11 步」。
NOISE_RE = re.compile(
    r"^(##\[step\]|步骤失败|步骤已取消|已取消（|step failed|job failed|"
    r"可能是文件名写错了)",
)
_LEVEL_PREFIX = re.compile(r"^\[(?:ERROR|CRITICAL|INFO|WARN(?:ING)?|DEBUG)\]:\s*", re.I)
_EXIT_LINE = re.compile(r"^\(exit\s+-?\d+\)$")
_TAGGED_ERROR = re.compile(r"^\[(?:ERROR|CRITICAL)\]", re.I)
# 模型把「怎么写建议」的思考过程写进正文时会出现这些字
_SUGGEST_META = re.compile(
    r"(我们被要求|根据失败日志|给出最多|不要标题|不要客套|"
    r"看起来是|所以，现在|建议应该围绕|可执行建议|"
    r"we are required|according to the requirements)",
    re.I,
)
# 主因旁边常跟着的第二句：清单同名文件、COPY 上下文、编译器编号。
_HINT_FOLLOW = (
    "同名文件",
    "请把清单",
    "路径相对于",
    "error CS",
    "error MSB",
    "COPY ",
    "no such file",
    "failed to walk",
    "failed to solve",
)
# 通知/诊断一条摘要的上限。buildkit 路径很长，180 会把真正缺的目录截掉。
_SNIPPET_CHARS = 280
# 能让人定位到「改哪」：路径、文件、编译器编号、明确的找不到。
# 不能只认斜杠，镜像名 registry.example.com/app 也有斜杠，那是收尾不是原因。
_LOCATOR_RE = re.compile(
    r"("
    r"(?:^|[\s'\"=])(?:\.\.?[\\/]|[A-Za-z]:\\)|"
    r"\b[\w.\-]+\.(?:java|cs|dll|jar|xml|tsx?|jsx|py|go|json|yml|yaml)(?::\d+)?|"
    r"\berror\s+[A-Z]{2,}\d+|"
    r"\b[A-Z][\w.]+Exception\b|"
    r"no such file|not found|cannot find|could not find|"
    r"connection refused|unknown host|name or service not known|"
    r"denied|unauthorized|"
    r"找不到|不存在|"
    r"「[^」]{1,80}」|"
    r"COPY\s+\S|"
    r"failed to walk|failed to solve|lstat\s"
    r")",
    re.I,
)
# 插件在 sdk.stream 之后补的「退出码」收尾。
_WRAP_EXIT = re.compile(
    r"(失败[（(]退出码\s*\d+[）)]|失败，退出码\s*\d+|脚本退出码\s*\d+)$",
    re.I,
)
STATUS_LABEL = {
    "success": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "rolled_back": "已回滚",
    "pending": "待审批",
    "queued": "排队中",
    "running": "执行中",
}


_APPLY_TOOLS = frozenset(
    {"apply_project_execute", "apply_group_execute", "apply_pipeline_execute", "apply_project_role"}
)
ACCESS_DONE = {"approved", "rejected", "cancelled"}


def add_watch(db: Session, *, conversation_id: int, user_id: int, release: Release) -> AiWatch:
    exist = db.scalar(
        select(AiWatch).where(
            AiWatch.conversation_id == conversation_id,
            AiWatch.release_id == release.id,
            AiWatch.status == "watching",
        )
    )
    if exist is not None:
        return exist
    w = AiWatch(
        conversation_id=conversation_id,
        user_id=user_id,
        release_id=release.id,
        pipeline_id=release.pipeline_id,
        application_id=0,
        status="watching",
    )
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def add_access_watch(db: Session, *, conversation_id: int, user_id: int, application_id: int) -> AiWatch:
    """权限申请提交后跟进审批结果，通过/驳回写回同一会话。"""
    exist = db.scalar(
        select(AiWatch).where(
            AiWatch.conversation_id == conversation_id,
            AiWatch.application_id == application_id,
            AiWatch.status == "watching",
        )
    )
    if exist is not None:
        return exist
    w = AiWatch(
        conversation_id=conversation_id,
        user_id=user_id,
        release_id=0,
        pipeline_id=0,
        application_id=int(application_id),
        status="watching",
    )
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def watch_access_from_traces(db: Session, conversation_id: int, user_id: int, traces: list | None) -> bool:
    """本轮技能里成功提交了申请单，就开始跟进。失败或未提交不跟。"""
    added = False
    for item in traces or []:
        name = str(item.get("name") or "")
        if name not in _APPLY_TOOLS:
            continue
        result = item.get("result")
        if not isinstance(result, dict) or result.get("error"):
            continue
        aid = int(result.get("application_id") or 0)
        if not aid:
            continue
        add_access_watch(db, conversation_id=conversation_id, user_id=user_id, application_id=aid)
        added = True
    return added


def format_access_followup(db: Session, application) -> str:
    """审批终态写进对话的正文。"""
    from app.modules.auth.models import User

    reviewer = db.get(User, application.reviewer_id) if application.reviewer_id else None
    who = (reviewer.display_name or reviewer.username) if reviewer else ""
    scope = f"{application.project_name} / {application.group_name} / {application.pipeline_name}"
    if application.status == "approved":
        from app.modules.access.service import format_action_labels

        granted_raw = getattr(application, "granted_actions", "") or ""
        granted = format_action_labels([x for x in granted_raw.split(",") if x])
        return (
            f"【权限申请】已通过 #{application.id}\n"
            f"范围：{scope}\n"
            f"结果：已授予 {granted or '查看、执行'}\n"
            f"审批人：{who or '—'}\n"
            f"意见：{application.review_comment or '（无）'}"
        )
    if application.status == "rejected":
        return (
            f"【权限申请】已驳回 #{application.id}\n"
            f"范围：{scope}\n"
            f"审批人：{who or '—'}\n"
            f"意见：{application.review_comment or '（无）'}\n"
            "说明：可调整范围后重新申请。"
        )
    if application.status == "cancelled":
        return f"【权限申请】已撤销 #{application.id}\n范围：{scope}"
    return f"【权限申请】#{application.id} 当前状态 {application.status}"


def complete_access_watches(db: Session, application) -> int:
    """审批或撤销后立刻写回跟进中的会话，不等 8 秒轮询。"""
    watches = db.scalars(
        select(AiWatch).where(
            AiWatch.application_id == int(application.id),
            AiWatch.status == "watching",
        )
    ).all()
    if not watches:
        return 0
    text = format_access_followup(db, application)
    for w in watches:
        w.status = "done"
    db.commit()
    for w in watches:
        append_message(db, w.conversation_id, "assistant", text, kind="followup")
    return len(watches)


def _duration(r: Release) -> str:
    start = r.started_at or r.created_at
    end = r.finished_at or datetime.now()
    if not start:
        return "-"
    sec = max(0, int((end - start).total_seconds()))
    if sec < 60:
        return f"{sec} 秒"
    return f"{sec // 60} 分 {sec % 60} 秒"


def _bare(line: str) -> str:
    """去掉日志级别前缀，通知里只留人能读的句子。"""
    return _LEVEL_PREFIX.sub("", (line or "").strip())


def _has_locator(bare: str) -> bool:
    """这句话有没有指出文件、路径或明确的找不到，人看了能去改。"""
    return bool(_LOCATOR_RE.search(bare or ""))


def _is_wrapper_error(bare: str) -> bool:
    """命令已经失败后插件补的收尾，或只有标题没有内容的行。

    插件惯例：sdk.stream 把工具原文打成 [INFO]，最后 sdk.log.error 一句没有路径
    的「镜像构建失败 / Maven 构建失败（退出码 1）」。摘要若信这句，COPY、javac、
    CS 会被丢掉。插件自己写清原因的 [ERROR]（清单找不到某个 dll、非法分支名）
    带定位信息，不是收尾。k8s 的「当前工作负载状态：」只是下一行 kubectl 的标题。
    """
    s = (bare or "").strip()
    if not s:
        return True
    if _has_locator(s):
        return False
    if s.endswith(("：", ":")):
        return True
    if _WRAP_EXIT.search(s):
        return True
    if re.search(r"失败$", s) and "。" not in s:
        return True
    if re.search(r"\bfailed$", s, re.I) and ":" not in s[1:]:
        return True
    return False


def _clip(bare: str) -> str:
    """截一条摘要，尽量保住文件路径。"""
    return (bare or "")[:_SNIPPET_CHARS]


def _score_line(ln: str) -> int:
    """给候选行打分。定位信息远比「带 [ERROR] 级别」重要；同分取更靠后的行。

    同是 [ERROR] 时，带「超时/失败/找不到」的句子压过「可在平台上回滚」这类后续说明。
    """
    bare = _bare(ln)
    score = 0
    if _TAGGED_ERROR.match(ln):
        score += 2
    if _has_locator(bare):
        score += 8
    if re.search(
        r"(超时|失败|异常|不存在|找不到|exception|denied|unauthorized|"
        r"no such file|not found|cannot find|could not find)",
        bare,
        re.I,
    ):
        score += 3
    if _is_wrapper_error(bare):
        score -= 10
    return score


def pick_error_snippet(log_lines: list[str], *, max_n: int = 2) -> list[str]:
    """从失败步骤日志抽出给人看的一两句。

    规则：丢掉 Agent 控制行，再在带失败字样的行里按「能不能定位」打分。
    清单写错 dll、IIS 应用池名字、javac 路径、Docker COPY 缺目录，都应压过
    「构建失败」这种收尾。没有定位信息时才退回收尾句，总好过空白。
    """
    cleaned: list[str] = []
    for raw in log_lines or []:
        s = (raw or "").strip()
        if not s or NOISE_RE.match(s) or _EXIT_LINE.match(s):
            continue
        cleaned.append(s)
    if not cleaned:
        return []

    candidates = [ln for ln in cleaned if _TAGGED_ERROR.match(ln) or ERROR_RE.search(ln)]
    if not candidates:
        return [_clip(_bare(ln)) for ln in cleaned[-max_n:]]

    best = candidates[0]
    best_score = _score_line(best)
    for ln in candidates[1:]:
        score = _score_line(ln)
        if score >= best_score:
            best, best_score = ln, score

    out = [_clip(_bare(best))]
    for ln in cleaned:
        if _bare(ln) == out[0] or _is_wrapper_error(_bare(ln)):
            continue
        if any(key in ln for key in _HINT_FOLLOW):
            out.append(_clip(_bare(ln)))
            break
    return out[:max_n]


def parse_suggestions(text: str) -> list[str]:
    """只留可执行的建议行，丢掉模型复述提示词或把思路写进正文的段落。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        s = raw.strip(" \t-•*")
        s = re.sub(r"^\d+[\.、)\]］]\s*", "", s)
        if not s or _SUGGEST_META.search(s):
            continue
        if len(s) > 160:
            continue
        if s in out:
            continue
        out.append(s)
        if len(out) >= 3:
            break
    return out


def _error_briefs(db: Session, release: Release) -> list[str]:
    """按失败任务拼「步骤名：关键句」，供邮件和站内通知的错误摘要使用。"""
    from app.db.session import SessionLocal
    from app.modules.agent.task_service import get_task_logs

    tasks = (
        db.query(BuildTask)
        .filter(BuildTask.release_id == release.id, BuildTask.status.in_(("failed", "timeout")))
        .order_by(BuildTask.id)
        .all()
    )
    lines: list[str] = []
    for t in tasks[:4]:
        label = f"{t.stage_name}/{t.job_name}".strip("/") or t.job_name or f"task-{t.id}"
        logs = get_task_logs(db, SessionLocal, t.id)
        snippet = pick_error_snippet(logs or [])
        if snippet:
            lines.append(f"· {label}：{' | '.join(snippet)}")
        else:
            lines.append(f"· {label}：无日志（状态 {t.status}）")
    return lines or ["· 未取到失败步骤日志，请打开执行详情查看"]


def _suggest(db: Session, briefs: list[str]) -> list[str]:
    """根据错误摘要生成最多 3 条建议；模型输出脏了就退回规则提示。"""
    blob = "\n".join(briefs).lower()
    hints: list[str] = []
    if any(k in blob for k in ("发布清单", "编译产物目录里找不到", "同名文件")):
        hints.append(
            "清单路径必须相对编译产物根目录。若摘要里写了同名文件路径，把清单改成那条后再发布。"
        )
    if any(k in blob for k in ("401", "403", "unauthorized", "denied", "认证", "凭证", "token", "permission")):
        hints.append("检查流水线绑定的仓库凭证是否过期，到「凭证管理」更新密文后重跑。")
    if any(k in blob for k in ("clone", "git", "repository", "could not read", "分支")):
        hints.append("核对仓库地址、分支/Tag 是否存在，以及构建机能否访问该 Git 地址。")
    if any(k in blob for k in ("timeout", "timed out", "心跳")):
        hints.append("看构建机是否在线、任务是否卡住；必要时取消后换一台 Agent 重试。")
    if any(k in blob for k in ("failed to walk", "failed to solve", "copy ./", "copy .\\")):
        hints.append(
            "Dockerfile 的 COPY 找不到目录或文件。Maven 产物一般在模块的 target/ 下，"
            "把 docker-build 的构建上下文指到含 target 的目录，COPY 路径相对该上下文。"
        )
    if not hints and any(k in blob for k in ("command not found", "npm", "mvn")):
        hints.append("检查 Agent 机器是否安装对应构建工具，以及脚本路径/工作目录是否正确。")
    if not hints:
        hints.append("打开该发布的执行详情，从失败步骤日志定位命令；修好后用 Rebuild 按同一 commit 重跑。")
    try:
        from app.modules.llm.service import build_client

        client, _ = build_client(db)
        text = client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是发布失败通知的建议生成器。"
                        "只输出最多 3 行可执行建议，一行一条，不要编号以外的任何说明。"
                        "禁止复述本要求，禁止解释思考过程，禁止客套。"
                        "建议必须点名摘要里出现的文件、命令或步骤，不要写空泛的 DLL/构建脚本套话。"
                    ),
                },
                {"role": "user", "content": "\n".join(briefs)[:2500]},
            ],
            timeout_sec=25,
        )
        llm_lines = parse_suggestions(text or "")
        if llm_lines:
            return llm_lines
    except Exception as e:  # noqa: BLE001
        logger.info("跟进建议未走模型，用规则：%s", e)
    return hints[:3]


def format_one(db: Session, release: Release) -> str:
    p = db.get(Pipeline, release.pipeline_id)
    name = p.name if p else f"流水线 #{release.pipeline_id}"
    st = release.status
    label = STATUS_LABEL.get(st, st)
    head = [
        f"【发布结果】{label}",
        f"流水线：{name}",
        f"构建号：#{release.build_number or release.id}",
        f"版本：{release.version or '-'}",
        f"耗时：{_duration(release)}",
    ]
    if st == "success":
        head.append("说明：已成功结束，可在执行历史查看产物与日志。")
        return "\n".join(head)
    if st in ("cancelled", "rolled_back"):
        head.append("说明：任务已结束，如需再跑请重新发布或 Rebuild。")
        return "\n".join(head)
    if st == "failed":
        briefs = _error_briefs(db, release)
        tips = _suggest(db, briefs)
        head.append("")
        head.append("【错误摘要】")
        head.extend(briefs)
        head.append("")
        head.append("【建议】")
        head.extend(f"{i}. {t}" for i, t in enumerate(tips, 1))
        text = "\n".join(head)
        release.diagnosis_text = text
        return text
    return "\n".join(head + [f"说明：当前仍是 {label}，会继续跟进。"])


def cached_diagnosis_covers_logs(db: Session, release: Release, cached: str) -> bool:
    """缓存摘要是否已经包含当前日志抽出的关键句。

    旧通知只写了「镜像构建失败」时返回 False，打开诊断会按新规则重抽，不必点重新诊断。
    """
    text = cached or ""
    keys: list[str] = []
    for brief in _error_briefs(db, release):
        part = brief.split("：", 1)[-1].strip()
        if _is_wrapper_error(part):
            continue
        keys.append(part[:48])
    if not keys:
        return True
    return all(k in text for k in keys)


def load_cached_diagnosis(db: Session, release: Release) -> str:
    """读这次失败已经生成过的摘要+建议。

    优先 release.diagnosis_text（执行页、通知共用）；没有则回填站内失败通知正文，
    已经发过通知的历史执行打开 AI 诊断时不再请求模型。
    """
    stored = (getattr(release, "diagnosis_text", None) or "").strip()
    if stored:
        return stored
    from app.modules.notify.models import InAppNotice

    content = db.scalar(
        select(InAppNotice.content)
        .where(
            InAppNotice.kind == "release.failed",
            InAppNotice.related_id == release.id,
        )
        .order_by(InAppNotice.id.desc())
        .limit(1)
    )
    text = (content or "").strip()
    if text:
        release.diagnosis_text = text
    return text


def _tick_access_watch(db: Session, watch: AiWatch, application_id: int) -> int:
    """权限跟进：终态写回会话；超过 7 天仍待审则停止跟进。"""
    from app.modules.auth.models import PermissionApplication

    db.refresh(watch)
    if watch.status != "watching":
        return 0
    a = db.get(PermissionApplication, application_id)
    if a is None:
        watch.status = "done"
        db.commit()
        return 0
    created = watch.created_at or datetime.now()
    stale = created < datetime.now() - timedelta(days=7)
    if a.status in ACCESS_DONE:
        complete_access_watches(db, a)
        return 1
    if stale:
        append_message(
            db,
            watch.conversation_id,
            "assistant",
            f"【权限申请】跟进超时 #{a.id}\n"
            f"范围：{a.project_name} / {a.group_name} / {a.pipeline_name}\n"
            "说明：仍待审批，已停止自动跟进。可到权限申请页查看，或在对话里问「我的申请怎么样了」。",
            kind="followup",
        )
        watch.status = "done"
        db.commit()
        return 1
    return 0


def tick() -> int:
    from app.db.session import SessionLocal

    done = 0
    with SessionLocal() as db:
        watches = db.scalars(select(AiWatch).where(AiWatch.status == "watching")).all()
        by_conv: dict[int, list[tuple[AiWatch, Release, str]]] = {}
        for w in watches:
            aid = int(getattr(w, "application_id", 0) or 0)
            if aid:
                done += _tick_access_watch(db, w, aid)
                continue
            r = db.get(Release, w.release_id)
            if r is None:
                w.status = "done"
                db.commit()
                continue
            cutoff = datetime.now() - timedelta(hours=6)
            created = w.created_at or datetime.now()
            if r.status in TERMINAL or created < cutoff:
                kind = "timeout" if r.status not in TERMINAL else r.status
                by_conv.setdefault(w.conversation_id, []).append((w, r, kind))
            elif r.status == "pending" and not w.pending_hinted:
                if created < datetime.now() - timedelta(seconds=20):
                    by_conv.setdefault(w.conversation_id, []).append((w, r, "pending"))
        for conv_id, items in by_conv.items():
            terminal_items = [x for x in items if x[2] not in ("pending",)]
            pending_items = [x for x in items if x[2] == "pending"]
            if terminal_items:
                blocks = []
                if len(terminal_items) > 1:
                    blocks.append(f"共跟进 {len(terminal_items)} 条发布，结果如下。")
                for w, r, kind in terminal_items:
                    if kind == "timeout":
                        blocks.append(
                            f"【发布结果】超时未结束\n流水线：#{r.pipeline_id}\n"
                            f"构建号：#{r.build_number or r.id}\n"
                            f"当前状态：{STATUS_LABEL.get(r.status, r.status)}\n说明：已停止自动跟进，请到执行历史查看。"
                        )
                    else:
                        blocks.append(format_one(db, r))
                    w.status = "done"
                append_message(db, conv_id, "assistant", "\n\n--------\n\n".join(blocks), kind="followup")
                done += len(terminal_items)
            for w, r, _kind in pending_items:
                p = db.get(Pipeline, r.pipeline_id)
                append_message(
                    db,
                    conv_id,
                    "assistant",
                    f"【发布结果】待审批\n流水线：{p.name if p else r.pipeline_id}\n"
                    f"构建号：#{r.build_number or r.id}\n"
                    "说明：已提交生产审批，通过后会继续跟进最终结果。",
                    kind="followup",
                )
                w.pending_hinted = 1
        db.commit()
    return done


def followup_loop() -> None:
    while True:
        try:
            tick()
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 发布跟进循环异常：%s", e)
        time.sleep(8)


def start_followup_loop() -> None:
    threading.Thread(target=followup_loop, name="ai-followup", daemon=True).start()
