# -*- coding: utf-8 -*-
"""插件输出：写入工作空间 .release_atom_output.json，供 Agent 解析（如 source_ref）。"""
from __future__ import annotations

import json
import os
from typing import Any

from .context import get_workspace


def set_output(output: dict[str, Any]) -> None:
    """设置插件执行结果。

    示例::
        set_output({
            "status": status.SUCCESS,
            "message": "ok",
            "type": output_template_type.DEFAULT,
            "data": {
                "source_ref": {"type": "string", "value": "abc123"}
            }
        })
    """
    ws = get_workspace()
    path = os.path.join(ws, ".release_atom_output.json")
    # 也写插件 cwd，兼容 Agent 在插件目录启动的场景
    cwd_path = os.path.join(os.getcwd(), ".release_atom_output.json")
    text = json.dumps(output, ensure_ascii=False, indent=2)
    for p in {path, cwd_path}:
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
    # 约定行：方便 Agent 从日志兜底解析
    data = output.get("data") or {}
    for key, meta in data.items():
        if isinstance(meta, dict) and "value" in meta:
            print(f"##[set-output]{key}={meta['value']}", flush=True)
