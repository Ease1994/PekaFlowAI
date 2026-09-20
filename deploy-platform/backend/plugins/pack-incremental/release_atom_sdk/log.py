# -*- coding: utf-8 -*-
"""控制台日志：Agent 采集 stdout 展示到执行明细。"""
from __future__ import annotations

import sys

# Agent 固定按 UTF-8 采集插件的 stdout，但 Windows 上 Python 默认按 ANSI 代码页
# 输出（中文机器是 cp936），两边对不上，中文日志就成了乱码。更糟的是 GBK 编不出
# 的字符（比如解码外部命令输出时产生的 U+FFFD）会直接抛 UnicodeEncodeError，
# 把整个插件搞挂。这里主动把输出对齐到 Agent 约定的 UTF-8。
def _force_utf8(stream) -> None:
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 被重定向成非文本流时没有 reconfigure，忽略即可
        pass


_force_utf8(sys.stdout)
_force_utf8(sys.stderr)


class _Logger:
    def debug(self, msg: str) -> None:
        print(f"[DEBUG]: {msg}", flush=True)

    def info(self, msg: str) -> None:
        print(f"[INFO]: {msg}", flush=True)

    def warning(self, msg: str) -> None:
        print(f"[WARNING]: {msg}", flush=True)

    def error(self, msg: str) -> None:
        print(f"[ERROR]: {msg}", file=sys.stderr, flush=True)

    def critical(self, msg: str) -> None:
        print(f"[CRITICAL]: {msg}", file=sys.stderr, flush=True)


log = _Logger()
