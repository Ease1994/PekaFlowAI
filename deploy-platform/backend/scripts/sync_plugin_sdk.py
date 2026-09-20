"""把 plugins/sdk/python/release_atom_sdk 同步到各个 Python 插件目录。

插件包是独立下发到构建机的，运行时没有共享依赖，所以每个插件目录都得自带一份 SDK。
手工复制迟早漏掉某个插件，改完 SDK 后一半插件是新的一半是旧的，排查起来极费劲。

用法：python scripts/sync_plugin_sdk.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "plugins"
SRC = ROOT / "sdk" / "python" / "release_atom_sdk"
SKIP = {"sdk", "examples"}


def main() -> int:
    if not SRC.is_dir():
        print(f"找不到 SDK 源目录：{SRC}")
        return 1

    synced, skipped = [], []
    for plugin in sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name not in SKIP):
        meta = plugin / "task.json"
        if not meta.is_file():
            continue
        # 只有 Python 插件需要；Java 内置动作（file-transfer 等）没有 task.py
        if not (plugin / "task.py").is_file():
            skipped.append(plugin.name)
            continue
        dest = plugin / "release_atom_sdk"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        synced.append(plugin.name)

    print(f"已同步 SDK 到 {len(synced)} 个插件：{', '.join(synced)}")
    if skipped:
        print(f"跳过（无 task.py，由 Agent 内置执行）：{', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
