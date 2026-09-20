"""Agent 目录契约：工具/技能/插件说明按统一文案，说明书里点名的工具必须存在。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.modules.ai.playbooks import all_playbooks
from app.modules.ai.registry import all_skills
from app.modules.ai.skills import load_all
from app.modules.harness.tools import _TOOL_DESC_MAX, _clip_text

_TOOL_TOKEN = re.compile(r"`([a-z][a-z0-9_]{2,40})`")
# 「是不是」里的「不是」不算易混对比，必须是独立的不是 / 不要 / 改用。
_CONFUSABLE = re.compile(r"(?<!是)不是|不要|改用")
_KNOWN_FIELDS = {
    "project",
    "env",
    "pipeline",
    "query",
    "keyword",
    "status",
    "name",
    "display_name",
    "description",
    "skill_md",
    "notice_id",
    "node_id",
    "node_ids",
    "target_dir",
    "attachment_ids",
    "allow_paths",
    "pipeline_id",
    "release_id",
    "unread_only",
    "intent",
    "action",
    "files",
    "plain",
    "blocker",
    "all",
    "running",
    "success",
    "failed",
    "cancelled",
    "rejected",
    "enable",
    "disable",
    "uninstall",
    "comment",
    "approved",
}
_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"


def _has_when(text: str) -> bool:
    """何时调用：中文写成「在…时调用/时使用」。"""
    return "时调用" in text or "时使用" in text


def _has_not(text: str) -> bool:
    """何时不用：和易混工具/步骤划界，排除「是不是」误伤。"""
    return bool(_CONFUSABLE.search(text or ""))


def test_visible_tools_have_when_and_not() -> None:
    """发给模型的 140 字摘要必须同时有何时调用、和易混工具的差别。"""
    load_all()
    missing: list[str] = []
    for item in all_skills():
        if not item.llm_visible:
            continue
        clip = _clip_text(item.description or "", _TOOL_DESC_MAX)
        if not _has_when(clip):
            missing.append(f"{item.name}: 缺少「何时调用」")
        if not _has_not(clip):
            missing.append(f"{item.name}: 缺少易混对比")
    assert not missing, "\n".join(missing)


def test_playbook_descriptions_have_what_and_when() -> None:
    """技能目录摘要：做什么 + 何时用 + 不是什么。"""
    missing: list[str] = []
    for item in all_playbooks():
        text = item.description or ""
        if not _has_when(text):
            missing.append(f"{item.name}: 缺少何时用")
        if not _has_not(text):
            missing.append(f"{item.name}: 缺少易混对比")
        if "Workflow" not in item.body or "Do not use" not in item.body:
            missing.append(f"{item.name}: 正文缺少 Workflow / Do not use")
    assert not missing, "\n".join(missing)


def test_playbook_named_tools_exist() -> None:
    """说明书里反引号点名的工具必须在注册表里，避免教模型调不存在的接口。"""
    load_all()
    known = {item.name for item in all_skills()} | _KNOWN_FIELDS | {item.name for item in all_playbooks()}
    invented: list[str] = []
    for playbook in all_playbooks():
        for token in _TOOL_TOKEN.findall(playbook.body):
            if token in known:
                continue
            if token.startswith(("rp-", "list_", "get_", "propose_", "apply_", "lint_", "cancel_", "pm_", "diagnose_")):
                invented.append(f"{playbook.name}: `{token}`")
    assert not invented, "说明书点名了不存在的工具：\n" + "\n".join(invented)


def test_pipeline_plugins_describe_what_and_not() -> None:
    """流水线插件说明写这个步骤干什么、别和相近插件搞混。"""
    weak: list[str] = []
    for path in sorted(_PLUGIN_ROOT.glob("*/task.json")):
        if "examples" in path.parts:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        desc = str(data.get("description") or "").strip()
        name = data.get("name") or path.parent.name
        if len(desc) < 12:
            weak.append(f"{name}: 说明过短")
        if "在" not in desc and "时" not in desc and "用" not in desc:
            weak.append(f"{name}: 看不出何时用这个步骤")
        if "不是" not in desc and "请用" not in desc and "不要" not in desc:
            weak.append(f"{name}: 缺少与相近步骤的差别")
    assert not weak, "\n".join(weak)
