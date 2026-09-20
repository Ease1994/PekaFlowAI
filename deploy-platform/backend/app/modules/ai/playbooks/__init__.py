"""内置 Agent 技能：仓库里每份 rp-*.md 就是一份 SKILL.md。

YAML frontmatter 提供 name + description（做什么、何时用、不是什么），
正文写流程、反例和例子。系统提示只放摘要，正文由 `skill` 工具按需加载。

改领域规则改这些 md，不要再往 prompt.py 里拼 if。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_DIR = Path(__file__).resolve().parent
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.S)


@dataclass(frozen=True)
class Playbook:
    """一份内置技能：目录用 name/description，模型加载 body。"""

    name: str
    display_name: str
    description: str
    filename: str
    body: str


def _from_file(path: Path) -> Playbook:
    """读一份带 YAML frontmatter 的 SKILL.md。description 必须能独立决定要不要加载。"""
    raw = path.read_text(encoding="utf-8")
    matched = _FRONTMATTER.match(raw)
    if not matched:
        raise ValueError(f"{path.name} 缺少 YAML frontmatter（--- name / description ---）")
    meta = yaml.safe_load(matched.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name} frontmatter 必须是映射")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        raise ValueError(f"{path.name} 必须有 name 和 description")
    display = str(meta.get("display_name") or "").strip() or name
    body = matched.group(2).strip()
    if not body:
        raise ValueError(f"{path.name} 正文不能为空")
    return Playbook(
        name=name,
        display_name=display,
        description=description,
        filename=path.name,
        body=body,
    )


def _load() -> tuple[Playbook, ...]:
    """加载本目录全部 rp-*.md，按 name 排序，保证目录稳定。"""
    items = [_from_file(path) for path in sorted(_DIR.glob("rp-*.md"))]
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError("内置技能 name 重复：" + "、".join(names))
    return tuple(items)


PLAYBOOKS: tuple[Playbook, ...] = _load()


def all_playbooks() -> tuple[Playbook, ...]:
    return PLAYBOOKS


def get(name: str) -> Playbook | None:
    key = (name or "").strip()
    return next((item for item in PLAYBOOKS if item.name == key), None)
