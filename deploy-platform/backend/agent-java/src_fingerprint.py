"""Agent 源码指纹：确认仓库里的 deploy-agent.jar 是当前源码编译出来的。

jar 是提交进仓库的编译产物，Dockerfile 直接 COPY 它。改了 .java 却忘了重新
build，平台就会一直把旧 jar 发给所有机器，而现象是「新功能怎么都不生效」——
从表现根本看不出问题出在编译上，只会一路往业务逻辑里查。

build 脚本编译后写入指纹，后端启动时比对，对不上就打醒目警告。

用法：
    python src_fingerprint.py --write    # 编译后记录
    python src_fingerprint.py            # 打印当前源码指纹
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
STAMP = HERE / "deploy-agent.jar.srcsha"


def source_fingerprint() -> str:
    """所有 .java 文件内容的聚合指纹，与文件顺序、时间戳无关。"""
    digest = hashlib.sha256()
    for f in sorted(SRC.rglob("*.java")):
        digest.update(f.relative_to(SRC).as_posix().encode())
        digest.update(b"\0")
        # 统一换行再算，免得 git 的 autocrlf 让同一份源码在两台机器上指纹不同
        digest.update(f.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def recorded_fingerprint() -> str:
    try:
        return STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main() -> int:
    current = source_fingerprint()
    if "--write" in sys.argv:
        STAMP.write_text(current + "\n", encoding="utf-8")
        print(f"源码指纹已记录: {current}")
        return 0
    print(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
