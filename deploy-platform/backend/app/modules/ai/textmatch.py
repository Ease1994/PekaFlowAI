"""短文本重叠打分。

对话里要在几十个工具、若干技能包里挑最像的那条。这里不做意图分类，
只比较用户原话和候选的名称/说明/例句有多少字重叠。模型仍然自己决定调哪个；
本模块只决定「先把哪些候选放到模型眼前」。
"""
from __future__ import annotations

import re


def grams(text: str) -> list[str]:
    """英文按单词、中文按二字切。空串返回空列表。"""
    raw = (text or "").strip().lower()
    if not raw:
        return []
    out: list[str] = []
    out.extend(re.findall(r"[a-z0-9-]{2,}", raw))
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", raw)
    if len(cjk) >= 2:
        out.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    elif cjk:
        out.append(cjk)
    return out


def overlap_score(query: str, blob: str) -> int:
    """query 在 blob 里的重叠分。整句命中最高，其次按 gram 计数。"""
    q = (query or "").strip().lower()
    hay = (blob or "").lower()
    if not q or not hay:
        return 0
    if q == hay:
        return 100
    if q in hay:
        return 80
    tokens = grams(q)
    if not tokens:
        return 0
    return sum(1 for item in tokens if item in hay)
