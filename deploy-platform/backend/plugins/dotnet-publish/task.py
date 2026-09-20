# -*- coding: utf-8 -*-
"""用 dotnet CLI 编译并发布 .NET Core / .NET 5+ 项目。

和 msbuild-build 的分工：那个插件走 MSBuild 的 _CopyWebApplication，是给没有
dotnet publish 的老 .NET Framework 项目用的。本插件面向 SDK 风格项目，
dotnet publish 产出的目录本身就是可部署结构，不需要额外拼装。
"""
from __future__ import annotations

import fnmatch
import os
import shlex
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402

FALLBACK_DOTNET = [
    r"C:\Program Files\dotnet\dotnet.exe",
    r"C:\Program Files (x86)\dotnet\dotnet.exe",
    "/usr/bin/dotnet",
    "/usr/local/bin/dotnet",
    "/usr/share/dotnet/dotnet",
    "/snap/bin/dotnet",
]


class BuildError(Exception):
    pass


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _lines(raw) -> list[str]:
    """多行文本输入统一按行拆；顺手兼容用分号或逗号写在一行的情况。"""
    text = str(raw or "").strip()
    if not text:
        return []
    out: list[str] = []
    for line in text.replace(";", "\n").replace(",", "\n").splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out


# extraArgs 里不允许改输出目录：插件已经把 -o 锁在工作区，再放开会被覆盖到站点路径。
_DENIED_DOTNET_FLAGS = {"-o", "--output"}
_DENIED_DOTNET_PROPS = {
    "outputpath", "outdir", "publishdir", "baseoutputpath", "intermediateoutputpath",
}


def parse_extra_args(extra: str) -> list[str]:
    """拆 extraArgs。拒绝改输出目录，其余 -p: 属性原样保留。"""
    text = (extra or "").strip()
    if not text:
        return []
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise BuildError(f"附加参数无法解析：{exc}") from exc
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        name = tok.split("=", 1)[0].lower()
        if name in _DENIED_DOTNET_FLAGS:
            raise BuildError("附加参数不能改输出目录，请用步骤里的「输出目录」")
        prop = ""
        if tok.lower().startswith("-p:") or tok.lower().startswith("/p:"):
            prop = tok.split(":", 1)[1].split("=", 1)[0].strip().lower()
        if prop in _DENIED_DOTNET_PROPS:
            raise BuildError(f"附加参数不能设置 {prop}，输出目录由步骤字段控制")
        out.append(tok)
        i += 1
    return out


def _find_dotnet(configured: str) -> str:
    if configured.strip():
        p = Path(configured.strip())
        if not p.is_file():
            raise BuildError(f"指定的 dotnet 不存在：{p}")
        return str(p)

    found = shutil.which("dotnet")
    if found:
        return found
    home = Path.home()
    extra = [
        *FALLBACK_DOTNET,
        str(home / ".dotnet" / "dotnet"),
        str(home / ".dotnet" / "dotnet.exe"),
    ]
    for candidate in extra:
        if Path(candidate).is_file():
            return candidate
    raise BuildError(
        "构建机上找不到 dotnet。请安装 .NET SDK，或在步骤里手动填写 dotnet 路径"
    )


def _under_workspace(workspace: Path, path: Path, *, what: str) -> Path:
    """输出目录必须在本任务工作区内。

    发布前会清空 outputDir。填成站点路径或盘符下的目录，clean 就会把线上文件删掉。
    """
    root = workspace.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise BuildError(
            f"{what}「{path}」不在任务工作区 {root} 内。"
            "禁止填工作区外的绝对路径，清空输出目录会删到站点或系统目录"
        ) from exc
    return resolved


def _resolve_project(src: Path, raw: str) -> Path:
    """项目文件必须在代码目录内，避免编译构建机上别的仓库。"""
    value = (raw or "").strip().replace("/", os.sep)
    if not value:
        raise BuildError("必须填写解决方案 / 项目文件")
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise BuildError("项目文件必须是代码目录内的相对路径")
    full = (src / p).resolve()
    try:
        full.relative_to(src.resolve())
    except ValueError as exc:
        raise BuildError(f"项目文件「{raw}」不在代码目录内") from exc
    if not full.is_file():
        raise BuildError(f"找不到项目文件：{full}")
    return full


def _run(cmd: list[str], cwd: Path) -> int:
    return sdk.stream(cmd, cwd=cwd)


def _remove_after(out_dir: Path, patterns: list[str]) -> None:
    """剔除不该覆盖生产的文件，比如 Web.config、appsettings.Development.json。"""
    for pattern in patterns:
        normalized = pattern.replace("/", os.sep).lstrip(os.sep)
        hits = [
            f for f in out_dir.rglob("*")
            if f.is_file() and fnmatch.fnmatch(
                str(f.relative_to(out_dir)).lower(), normalized.lower()
            )
        ]
        if not hits:
            raise BuildError(
                f"「发布后删除」规则「{pattern}」没匹配到任何文件，已终止。"
                "可能是路径写错了。若继续发布，这些本该删掉的文件会留在产物里，"
                "打进增量包后覆盖生产配置。"
            )
        for f in hits:
            f.unlink()
            sdk.log.info(f"已删除 {f.relative_to(out_dir)}")


def main() -> int:
    inp = sdk.get_input()
    workspace = Path(sdk.get_workspace())
    src = workspace / "src"
    if not src.is_dir():
        src = workspace

    try:
        dotnet = _find_dotnet(str(inp.get("dotnetPath") or ""))
        project = _resolve_project(src, str(inp.get("project") or ""))
        out_raw = str(inp.get("outputDir") or "_publish").strip().replace("/", os.sep)
        out_dir = _under_workspace(
            workspace,
            Path(out_raw) if Path(out_raw).is_absolute() else src / out_raw,
            what="输出目录",
        )
    except BuildError as e:
        sdk.log.error(str(e))
        return 1

    configuration = str(inp.get("configuration") or "Release").strip()
    runtime = str(inp.get("runtime") or "").strip()
    sources = _lines(inp.get("nugetSources"))

    sdk.log.info(f"dotnet：{dotnet}")
    sdk.log.info(f"项目：{project}")
    sdk.log.info(f"配置：{configuration}" + (f" | 运行时：{runtime}" if runtime else " | 框架依赖"))

    if _truthy(inp.get("clean", True)) and out_dir.is_dir():
        sdk.log.info(f"清空输出目录：{out_dir}")
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 还原和发布分开跑：dotnet publish 的隐式还原不接受多个 --source，
    # 拆开才能把私有源明确传进去，失败时也能一眼看出是还原挂了还是编译挂了
    do_restore = _truthy(inp.get("restore", True))
    if do_restore:
        restore_cmd = [dotnet, "restore", str(project)]
        for s in sources:
            restore_cmd += ["--source", s]
        if runtime:
            restore_cmd += ["--runtime", runtime]
        if _run(restore_cmd, src) != 0:
            sdk.log.error("NuGet 还原失败。请确认源地址可达、私有源在构建机上可访问")
            return 1

    cmd = [
        dotnet, "publish", str(project),
        "-c", configuration,
        "-o", str(out_dir),
        "--nologo",
    ]
    if runtime:
        cmd += ["-r", runtime]
        # self-contained 只在指定了 runtime 时有意义，否则 dotnet 会直接报错
        cmd += ["--self-contained", "true" if _truthy(inp.get("selfContained")) else "false"]
    if do_restore:
        cmd.append("--no-restore")

    extra = str(inp.get("extraArgs") or "").strip()
    if extra:
        try:
            cmd += parse_extra_args(extra)
        except BuildError as e:
            sdk.log.error(str(e))
            return 1

    code = _run(cmd, src)
    if code != 0:
        sdk.log.error(f"dotnet publish 失败，退出码 {code}")
        return code

    removals = _lines(inp.get("removeAfterPublish"))
    if removals:
        _remove_after(out_dir, removals)

    produced = sum(1 for _ in out_dir.rglob("*") if _.is_file())
    if produced == 0:
        sdk.log.error(f"发布成功但输出目录 {out_dir} 是空的，请确认项目文件指向的是启动项目")
        return 1

    sdk.log.info(f"发布完成，输出目录 {out_dir} 共 {produced} 个文件")
    sdk.set_output(
        {
            "build_output_dir": {"type": "string", "value": str(out_dir)},
            "build_file_count": {"type": "string", "value": str(produced)},
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        sdk.log.error(str(e))
        sys.exit(1)
