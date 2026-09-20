"""数据卷上的公共安装包。

制品挂在发布上、会过期；这里是平台自己用的包（Linux JDK、以后的 Windows JRE 等），
长期留着，跟制品同一份 backend-data 卷。

根目录：data/shared-packs/
下面按用途分子目录，例如 jdk-linux/。不要把某种包直接摊在 data/ 根下。
"""
from __future__ import annotations

from pathlib import Path

# app/modules/agent/shared_packs.py → backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
SHARED_PACKS_DIR = _BACKEND_ROOT / "data" / "shared-packs"


def pack_dir(*parts: str) -> Path:
    """公共包根下的子目录。parts: 相对路径段，例如 ("jdk-linux",)。"""
    if not parts:
        raise ValueError("公共包必须落在 shared-packs 的子目录里")
    rel = Path(*parts)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("公共包子目录不能越界")
    return SHARED_PACKS_DIR / rel
