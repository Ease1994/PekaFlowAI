"""插件创作技能：让助手写插件，但只写到「待审草稿」为止。

插件是在构建机上以 Agent 子进程身份执行的任意代码，比发布本身权限更高 ——
发布只是跑既有编排，插件是往执行层塞新代码。所以这里的边界很硬：
模型可以起草、可以体检，落地也只落成草稿，进不了插件仓库、更进不了编排器。
后面还要管理员看代码、（可选）沙箱试跑、点发布、点安装，共四道。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.ai.registry import Skill, register
from app.modules.store import draft_service, plugin_lint

LEVEL_LABEL = {
    plugin_lint.LEVEL_ERROR: "错误",
    plugin_lint.LEVEL_HIGH: "高危",
    plugin_lint.LEVEL_WARN: "提醒",
}

_DRAFT_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "插件标识，小写字母开头，只含小写字母/数字/连字符，如 notify-dingtalk",
        },
        "display_name": {"type": "string", "description": "显示名，如 钉钉通知"},
        "category": {
            "type": "string",
            "description": "分类：source/build/deploy/notify/trigger/exec/artifact/pipeline",
        },
        "version": {"type": "string", "description": "语义化版本，新插件用 1.0.0"},
        "description": {"type": "string", "description": "这个步骤干什么、何时用、和相近步骤差在哪"},
        "language": {"type": "string", "description": "python / nodejs / java，优先 python"},
        "entrypoint": {
            "type": "string",
            "description": "在插件目录下执行的命令，python 用 python3 task.py",
        },
        "config_schema": {
            "type": "object",
            "description": (
                "编排器表单 DSL：{\"fields\":[{\"key\":\"webhook\",\"label\":\"Webhook 地址\","
                "\"type\":\"text\",\"required\":true,\"default\":\"\"}]}。"
                "type 可用 text/textarea/password/number/radio/select/checkbox/switch/code/group，"
                "select 与 radio 必须给 options"
            ),
        },
        "files": {
            "type": "object",
            "description": (
                "源码，键是文件名值是完整内容，如 {\"task.py\": \"...\"}。"
                "不要放 task.json（由上面的字段生成），也不要放 SDK（平台会打包进去）。"
                "Python 插件用 import release_atom_sdk as sdk 读参数 sdk.get_input()、"
                "打日志 sdk.log.info()、写输出 sdk.set_output()，需要回调平台时用 sdk.get_task_token()"
            ),
        },
        "intent": {"type": "string", "description": "用户的原始诉求，一句话记下来便于回溯"},
    },
    "required": ["name", "display_name", "language", "entrypoint", "files"],
}


def _collect(params: dict) -> tuple[dict, dict]:
    files = params.get("files") or {}
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except json.JSONDecodeError:
            raise BizException.bad_request("files 不是合法 JSON") from None
    schema = params.get("config_schema") or {}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema) if schema.strip() else {}
        except json.JSONDecodeError:
            raise BizException.bad_request("config_schema 不是合法 JSON") from None

    meta = {
        "name": str(params.get("name") or "").strip(),
        "display_name": str(params.get("display_name") or "").strip(),
        "category": str(params.get("category") or "exec").strip(),
        "version": str(params.get("version") or "1.0.0").strip(),
        "description": str(params.get("description") or "").strip(),
        "language": str(params.get("language") or "python").strip(),
        "entrypoint": str(params.get("entrypoint") or "").strip(),
        "config_schema": schema,
    }
    return meta, files if isinstance(files, dict) else {}


def _format_findings(result: dict) -> str:
    findings = result.get("findings") or []
    if not findings:
        return "体检没有发现问题。"
    lines = []
    for f in findings[:12]:
        label = LEVEL_LABEL.get(f.get("level"), f.get("level"))
        where = f.get("where") or ""
        lines.append(f"- [{label}] {f.get('message')}" + (f"（{where}）" if where else ""))
    if len(findings) > 12:
        lines.append(f"- …另有 {len(findings) - 12} 条")
    return "\n".join(lines)


def _propose_plugin_draft(db: Session, current, params: dict) -> dict:
    meta, files = _collect(params)
    if not files:
        return {"error": "没有生成任何源码文件"}

    result = draft_service.lint_payload(db, meta, files)
    if not result["ok"]:
        # 不合法就别递到用户面前，把问题回给模型让它改
        return {
            "ok": False,
            "message": "草稿没通过体检，请按下面的问题修正后重新起草",
            "findings": [f for f in result["findings"] if f["level"] == plugin_lint.LEVEL_ERROR],
        }

    draft = draft_service.create_draft(
        db,
        meta=meta,
        files=files,
        intent=str(params.get("intent") or "").strip(),
        source="ai",
        created_by=current.id,
    )
    entry_file = next(iter(files))
    preview = files[entry_file]
    if len(preview) > 1500:
        preview = preview[:1500] + "\n…（完整源码在技能库的插件草稿里看）"

    summary = (
        f"已把插件 **{draft.display_name or draft.name}**（`{draft.name}` v{draft.version}）"
        f"存成待审草稿 **#{draft.id}**。\n\n"
        f"{meta['description']}\n\n"
        f"共 {len(files)} 个文件：{'、'.join(files)}\n\n"
        f"体检结果：错误 {result['error_count']}、高危 {result['high_count']}、提醒 {result['warn_count']}\n"
        f"{_format_findings(result)}\n\n"
        f"`{entry_file}` 预览：\n```{meta['language']}\n{preview}\n```\n\n"
        "到 **技能库 → 流水线插件 → 插件草稿** 看完整代码、试跑。"
        "现在还没安装，流水线编排器里选不到它。发布到全局仓库仍需要管理员。"
    )
    return {
        "ok": True,
        "draft_id": draft.id,
        "name": draft.name,
        "status": draft.status,
        "reply": summary,
    }


def _lint_plugin_source(db: Session, current, params: dict) -> dict:
    """只体检不落库，模型可以自查一遍再决定要不要递确认卡。"""
    meta, files = _collect(params)
    if not files:
        return {"error": "没有可体检的源码"}
    return draft_service.lint_payload(db, meta, files)


def _list_plugin_drafts(db: Session, current, params: dict) -> dict:
    status = str(params.get("status") or "").strip() or None
    created_by = None if getattr(current, "is_admin", False) else getattr(current, "id", None)
    drafts = draft_service.list_drafts(db, status, created_by=created_by)
    return {
        "drafts": [
            {
                "id": d.id,
                "name": d.name,
                "display_name": d.display_name,
                "version": d.version,
                "status": d.status,
                "trial_status": d.trial_status,
                "intent": d.intent,
            }
            for d in drafts[:50]
        ]
    }


def load() -> None:
    register(Skill(
        name="propose_plugin_draft",
        description=(
            "起草流水线步骤插件并存为待审草稿（不会安装）。有可执行代码。"
            "在要给编排器加一个步骤、写插件代码时调用。"
            "助手说明书（只有 SKILL.md）改用 propose_agent_skill。"
        ),
        category="authoring",
        risk="write",
        confirm=False,
        parameters=_DRAFT_PARAMS,
        handler=_propose_plugin_draft,
        examples=["帮我做一个把构建产物推到对象存储的插件", "写个企业微信机器人通知插件", "给流水线加一个步骤插件"],
    ))
    register(Skill(
        name="lint_plugin_source",
        description="对一份流水线插件源码做静态体检（manifest / 表单 / 危险代码），不落库。在起草前要自查合不合法时调用。不是起草，也不是安装。",
        category="authoring",
        risk="read",
        parameters=_DRAFT_PARAMS,
        handler=_lint_plugin_source,
        examples=["体检一下这份插件代码", "插件草稿合不合法"],
    ))
    register(Skill(
        name="list_plugin_drafts",
        description="查看流水线插件草稿及其状态（待审/已发布/已驳回）。在问我的插件草稿、待审插件时调用。不是已安装插件目录（用 list_plugins），也不是助手技能包。",
        category="authoring",
        risk="read",
        parameters={
            "type": "object",
            "properties": {"status": {"type": "string", "description": "pending/published/rejected"}},
        },
        handler=_list_plugin_drafts,
        examples=["我的插件草稿", "待审插件有哪些"],
    ))
