# -*- coding: utf-8 -*-
"""跑外部命令并把输出实时转成构建日志。"""
from __future__ import annotations

import locale
import subprocess
import sys
from typing import Callable, Optional, Sequence

from .log import log


def _native_encoding() -> str:
    if sys.platform == "win32":
        # msbuild / dotnet / git 在中文 Windows 上按 ANSI 代码页(cp936)输出本地化文案，
        # 不是 UTF-8
        return locale.getpreferredencoding(False) or "cp936"
    return "utf-8"


_NATIVE = _native_encoding()


def decode(raw: bytes) -> str:
    """把命令输出解成文本。

    逐行判断而不是整体定编码：同一个工具的输出经常混着两种编码，比如 UTF-8 的
    文件路径配上本地代码页的提示语。硬按 UTF-8 解会把中文变成 U+FFFD，既丢了
    信息，后续还可能编不回去。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(_NATIVE, errors="replace")


def redact(text: str, secrets: Optional[Sequence[str]] = None) -> str:
    """把日志里的密钥换成 ***。

    构建日志所有人都看得到，密码一旦打进去就等于泄露，事后删日志也来不及。
    """
    out = text
    for s in secrets or ():
        if s and len(s) >= 4:
            out = out.replace(s, "***")
    return out


def stream(
    cmd: Sequence[str],
    cwd=None,
    env=None,
    on_line: Optional[Callable[[str], None]] = None,
    echo_command: bool = True,
    stdin_text: Optional[str] = None,
    mask: Optional[Sequence[str]] = None,
) -> int:
    """执行命令，逐行输出到日志，返回退出码。

    stdin_text 用于把密码之类的东西从命令行挪到标准输入（docker login
    --password-stdin 就是干这个的）：写在 argv 里，同机器上 ps 一下就能看到。
    """
    argv = [str(c) for c in cmd]
    if echo_command:
        log.info("$ " + redact(subprocess.list2cmdline(argv), mask))
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
        finally:
            proc.stdin.close()
    assert proc.stdout is not None
    with proc.stdout:
        # 用 readline 而不是迭代文件对象：后者带块缓冲，长时间的编译会看不到实时进度
        for raw in iter(proc.stdout.readline, b""):
            line = redact(decode(raw).rstrip("\r\n"), mask)
            if on_line is not None:
                on_line(line)
            else:
                log.info(line)
    return proc.wait()


def capture(cmd: Sequence[str], cwd=None, timeout: Optional[float] = None) -> tuple[int, str]:
    """执行命令并拿到完整输出，用于探测类调用（不进构建日志）。"""
    argv = [str(c) for c in cmd]
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return proc.returncode, decode(proc.stdout or b"")
