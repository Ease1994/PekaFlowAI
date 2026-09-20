"""按本轮话挑选发给模型的工具。

检索只决定把哪些工具放到眼前；最终由模型选。
不再用正则把本轮锁死成一个工具：话术对不上或对错，真正该调的工具会被藏起来。
"""
from __future__ import annotations

import re

from app.modules.ai.intent import is_skill_lifecycle_utterance
from app.modules.ai.textmatch import overlap_score

# access 始终在目录里：申请执行权和发布常被说成同一句话，不能靠正则先把权限工具拿掉。
CORE_CATEGORIES = frozenset({"meta", "catalog", "observe", "diagnose", "delivery", "access"})
# 检索命中后最多发给模型的工具数。太多又会退回 list_*。
_RETRIEVE_LIMIT = 8
# 重叠太弱时当作没检索到，回退成该分类下全量工具，避免漏发。
_RETRIEVE_MIN = 2
# 说明书加载和向用户提问必须随时能调，不能被检索子集丢掉。
_ALWAYS_TOOLS = frozenset({"skill", "ask_user"})

# 只决定要不要把写作类工具放进本轮候选，不是整轮路由。
_AUTHORING = re.compile(
    r"(技能|插件|skill|plugin|起草|实现一个|写一个|SKILL\.md|task\.py|助手技能)",
    re.I,
)


def _tool_blob(skill) -> str:
    """检索用的工具文本：名称、完整说明、例句。发给模型的 140 字摘要不参与打分。"""
    return " ".join(
        [skill.name.replace("_", " "), skill.description or "", *(skill.examples or ())]
    )


def rank_tools_for_message(message: str, *, categories: frozenset[str] | set[str]) -> list[tuple[int, str]]:
    """按与用户原话的重叠给当前分类下的工具排序，分高在前。"""
    from app.modules.ai.registry import all_skills
    from app.modules.ai.skills import load_all

    load_all()
    query = (message or "").strip()
    ranked: list[tuple[int, str]] = []
    for skill in all_skills():
        if not skill.llm_visible:
            continue
        if skill.category not in categories and skill.name != "skill":
            continue
        examples = skill.examples or []
        desc_score = overlap_score(query, _tool_blob(skill))
        example_score = max((overlap_score(query, item) for item in examples), default=0)
        score = desc_score + example_score
        if score > 0:
            ranked.append((score, skill.name))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def retrieve_tool_names(message: str, *, categories: frozenset[str] | set[str]) -> frozenset[str] | None:
    """取出本轮最像的几个工具。分太低返回 None，让调用方发该分类全量。"""
    ranked = rank_tools_for_message(message, categories=categories)
    if not ranked or ranked[0][0] < _RETRIEVE_MIN:
        return None
    best = ranked[0][0]
    # 只留和第一名同一量级的，避免「卸载技能」还带上查通知、列流水线。
    floor = max(_RETRIEVE_MIN, best // 2)
    picked = [name for score, name in ranked if score >= floor][:_RETRIEVE_LIMIT]
    return frozenset(picked)


def names_for_turn(message: str, *, categories: frozenset[str] | set[str]) -> frozenset[str] | None:
    """按说明/例句重叠收窄候选。对不上则发该分类全量，由模型选。"""
    picked = retrieve_tool_names(message, categories=categories)
    if picked is None:
        return None
    return picked | _ALWAYS_TOOLS


def categories_for_turn(
    message: str,
    *,
    selected_skills: list[str] | None = None,
    history: list[dict] | None = None,
) -> frozenset[str]:
    """本轮要带上哪些工具分类。写作类工具只在话里沾到时才打开，省 schema。"""
    cats = set(CORE_CATEGORIES)
    blob = message or ""
    for name in selected_skills or []:
        blob += " " + str(name)
    seen = 0
    for turn in reversed(history or []):
        if turn.get("role") != "user":
            continue
        content = turn.get("content")
        if isinstance(content, str) and content.strip():
            blob += "\n" + content
            seen += 1
            if seen >= 2:
                break
    if _AUTHORING.search(blob) or is_skill_lifecycle_utterance(blob):
        cats.add("authoring")
    return frozenset(cats)
