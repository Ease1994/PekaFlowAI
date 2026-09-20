# -*- coding: utf-8 -*-
"""调用手册按 OpenAPI tag 分类，名称必须和路由上一字不差。"""
from __future__ import annotations

import ast
from pathlib import Path

from app.core.openapi_tags import OPENAPI_TAGS

MODULES = Path(__file__).resolve().parents[1] / "app" / "modules"


def test_openapi_tags_cover_router_names() -> None:
    """侧栏分类靠 tag；漏登记的路由会掉进 default，手册里找不到。"""
    declared = {row["name"] for row in OPENAPI_TAGS}
    found: set[str] = set()
    for path in MODULES.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "APIRouter":
                continue
            for kw in node.keywords:
                if kw.arg != "tags" or not isinstance(kw.value, ast.List):
                    continue
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        found.add(elt.value)
    assert found, "没有扫到任何路由 tag"
    missing = sorted(found - declared)
    extra = sorted(declared - found)
    assert not missing, f"OPENAPI_TAGS 漏了：{missing}"
    assert not extra, f"OPENAPI_TAGS 多了已不存在的分类：{extra}"
