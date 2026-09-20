# -*- coding: utf-8 -*-
"""在 Windows 构建机上用 MSBuild 编译 .NET Framework 项目。

老 ASP.NET 项目没有 dotnet publish，能产出「可部署站点目录」的是 MSBuild 的
_CopyWebApplication 目标：它把 bin 和 Views/Content 这些内容文件按站点结构拷到
一个目录里，结构和生产站点根目录一一对应。增量清单正是相对这个目录来写的。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402

# 从新到旧探测：优先用机器上最新的 MSBuild，最后兜底到 .NET Framework 自带的
VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
FALLBACK_MSBUILD = [
    r"C:\Program Files\Microsoft Visual Studio\2022\*\MSBuild\Current\Bin\MSBuild.exe",
    r"C:\Program Files (x86)\Microsoft Visual Studio\2019\*\MSBuild\Current\Bin\MSBuild.exe",
    r"C:\Program Files (x86)\Microsoft Visual Studio\2017\*\MSBuild\15.0\Bin\MSBuild.exe",
    r"C:\Program Files (x86)\MSBuild\14.0\Bin\MSBuild.exe",
    r"C:\Program Files (x86)\MSBuild\12.0\Bin\MSBuild.exe",
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\MSBuild.exe",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\MSBuild.exe",
]


class BuildError(Exception):
    pass


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _lines(raw) -> list[str]:
    """多行文本输入统一按行拆；顺手兼容 nuget restore -source 那种分号/逗号写在一行。"""
    text = str(raw or "").strip()
    if not text:
        return []
    out: list[str] = []
    for line in text.replace(";", "\n").replace(",", "\n").splitlines():
        item = line.strip().strip('"').strip("'")
        if item and not item.startswith("#"):
            out.append(item)
    return out


DEFAULT_NUGET_SOURCES = (
    "https://api.nuget.org/v3/index.json",
    "https://nuget.org/api/v2/",
)


def _nuget_sources(raw) -> list[str]:
    """步骤没填时用 nuget.org 官方源，避免只靠构建机 NuGet.config。"""
    return _lines(raw) or list(DEFAULT_NUGET_SOURCES)


# .sln 里只有这些后缀才是 MSBuild / nuget restore 能打开的项目。
_RESTORE_PROJ_EXT = {".csproj", ".vbproj", ".fsproj", ".vcxproj"}
# 匹配 sln 的 Project 行，取出相对路径和 GUID。
_SLN_PROJECT = re.compile(
    r'^Project\("[^"]+"\)\s*=\s*"[^"]+"\s*,\s*"([^"]+)"\s*,',
    re.I | re.M,
)
_SLN_PROJECT_LINE = re.compile(
    r'^Project\("([^"]+)"\)\s*=\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"\{([^}]+)\}"\s*$',
    re.I,
)


def _nuget_restore_cmd(
    nuget: str,
    target: Path,
    sources: list[str],
    solution_dir: Path | None = None,
) -> list[str]:
    cmd = [nuget, "restore", str(target), "-NonInteractive"]
    # 对着 csproj 还原时必须告诉 nuget 解决方案目录，否则 packages 会落到项目旁，
    # HintPath 里的 ..\..\packages\ 就对不上。
    if solution_dir is not None and target.suffix.lower() != ".sln":
        cmd += ["-SolutionDirectory", str(solution_dir)]
    if sources:
        # 一条 -Source 里用分号拼接，和现场 nuget restore -source "a;b;c" 一致
        cmd += ["-Source", ";".join(sources)]
    return cmd


def _is_build_project_path(rel: str) -> bool:
    """解决方案条目是否为可还原的工程文件（不是 NuGet.exe / 解决方案文件夹）。"""
    suffix = Path(str(rel).replace("\\", "/")).suffix.lower()
    return suffix in _RESTORE_PROJ_EXT


def _sln_projects(sln: Path) -> list[Path]:
    """读 .sln 里真正的编译项目，丢掉解决方案杂项（.nuget\\NuGet.exe、readme 等）。"""
    text = sln.read_text(encoding="utf-8-sig", errors="replace")
    root = sln.parent
    out: list[Path] = []
    seen: set[Path] = set()
    for match in _SLN_PROJECT.finditer(text):
        rel = match.group(1).strip().replace("/", os.sep)
        p = Path(rel)
        full = p if p.is_absolute() else (root / p)
        if not _is_build_project_path(rel):
            continue
        key = full.resolve() if full.exists() else full
        if key in seen:
            continue
        seen.add(key)
        out.append(full)
    return out


def _filtered_sln_text(text: str) -> str | None:
    """从 sln 文本去掉不能当项目打开的条目，避免 nuget restore 触发 MSB4025。

    必须仍是一份完整 .sln（同目录相对路径才有效），而不是改成逐个 csproj 还原。
    没有需要剔除的项时返回 None，调用方直接还原原文件。
    """
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    dropped: set[str] = set()
    i = 0
    while i < len(lines):
        m = _SLN_PROJECT_LINE.match(lines[i].strip())
        if not m:
            kept.append(lines[i])
            i += 1
            continue
        rel, guid = m.group(3), m.group(4).lower()
        block = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].strip() != "EndProject":
            block.append(lines[i])
            i += 1
        if i < len(lines):
            block.append(lines[i])
            i += 1
        if _is_build_project_path(rel):
            kept.extend(block)
        else:
            dropped.add(guid)
    if not dropped:
        return None
    out: list[str] = []
    for line in kept:
        low = line.lower()
        if any("{" + guid + "}" in low for guid in dropped):
            continue
        out.append(line)
    return "".join(out)


def _sln_for_nuget_restore(solution: Path) -> tuple[Path, Path | None]:
    """选出 nuget restore 要用的解决方案。

    返回 (还原路径, 用完须删除的临时文件)。临时 sln 必须和原文件同目录，
    否则 Project 里的相对路径会全部失效。
    """
    if solution.suffix.lower() != ".sln":
        return solution, None
    try:
        text = solution.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return solution, None
    filtered = _filtered_sln_text(text)
    if not filtered:
        return solution, None
    tmp = solution.with_name(solution.stem + ".release-restore.sln")
    tmp.write_text(filtered, encoding="utf-8")
    return tmp, tmp


def _is_sdk_style_project(project: Path) -> bool:
    """是否需要 MSBuild 生成 project.assets.json（SDK 或 PackageReference）。

    packages.config 的老 csproj 靠 nuget.exe restore 即可；对它们再 /restore
    会顺着 ProjectReference 去还原 JsonRpc 等项目，容易报「路径的形式不合法」。
    """
    if project.suffix.lower() not in _RESTORE_PROJ_EXT:
        return False
    try:
        text = project.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    if re.search(r"<Project\b[^>]*\bSdk\s*=", text, re.I):
        return True
    return bool(re.search(r"<PackageReference\b", text, re.I))


def _msbuild_restore_cmd(msbuild: str, solution: Path, sources: list[str]) -> list[str]:
    """拼 MSBuild /t:Restore。分号必须写成 %3B，否则 MSB1006。编译前主路径已不再调用。"""
    cmd = [msbuild, str(solution), "/t:Restore", "/nologo", "/v:m"]
    if sources:
        # MSBuild 会把命令行里未加引号的分号当成下一条开关，MSB1006「属性无效」。
        # 属性值里的分号必须写成 %3B。
        cmd.append("/p:RestoreSources=" + "%3B".join(sources))
    return cmd


KNOWN_NUGET = (
    r"D:\b2ccode\nuget.exe",
    r"D:\b2ccode\nuget",
)


def _as_nuget_exe(raw: str | Path) -> str | None:
    """接受 nuget.exe、不带后缀的 nuget、或装着 nuget.exe 的目录。"""
    text = str(raw or "").strip()
    if not text:
        return None
    p = Path(text)
    candidates = [p]
    if p.suffix.lower() != ".exe":
        candidates.append(p.with_suffix(".exe"))
    if p.is_dir():
        candidates.extend([p / "nuget.exe", p / "nuget"])
    for hit in candidates:
        if hit.is_file():
            return str(hit)
    return None


def _find_nuget(configured: str = "", *starts: Path) -> str | None:
    if configured.strip():
        hit = _as_nuget_exe(configured)
        if hit:
            return hit
        raise BuildError(f"指定的 NuGet 不存在：{configured.strip()}")
    found = shutil.which("nuget") or shutil.which("nuget.exe")
    if found:
        return found
    seen: set[Path] = set()
    for start in starts:
        folder = start.resolve() if start.exists() else start
        if folder.is_file():
            folder = folder.parent
        for _ in range(8):
            if folder in seen:
                break
            seen.add(folder)
            hit = _as_nuget_exe(folder / "nuget") or _as_nuget_exe(folder / "nuget.exe")
            if hit:
                return hit
            if folder.parent == folder:
                break
            folder = folder.parent
    for known in KNOWN_NUGET:
        hit = _as_nuget_exe(known)
        if hit:
            return hit
    return None


def _find_msbuild(configured: str) -> str:
    if configured.strip():
        p = Path(configured.strip())
        if not p.is_file():
            raise BuildError(f"指定的 MSBuild 不存在：{p}")
        return str(p)

    if Path(VSWHERE).is_file():
        try:
            _, out = sdk.capture(
                [VSWHERE, "-latest", "-requires", "Microsoft.Component.MSBuild",
                 "-find", r"MSBuild\**\Bin\MSBuild.exe"],
                timeout=60,
            )
            for line in out.strip().splitlines():
                if line.strip() and Path(line.strip()).is_file():
                    return line.strip()
        except Exception:  # noqa: BLE001
            pass

    import glob
    for pattern in FALLBACK_MSBUILD:
        for hit in sorted(glob.glob(pattern), reverse=True):
            if Path(hit).is_file():
                return hit

    raise BuildError(
        "构建机上找不到 MSBuild。请安装 Visual Studio Build Tools，"
        "或在步骤里手动填写 MSBuild 路径"
    )


def _under_workspace(workspace: Path, path: Path, *, what: str) -> Path:
    """输出目录必须在本任务工作区内。

    编译前会清空 outputDir。填成站点路径时，clean 会把正在跑的站点删掉。
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


def _resolve_project(src: Path, raw: str, *, required: bool, what: str) -> Path | None:
    """工程文件必须在代码目录内。"""
    value = (raw or "").strip().replace("/", os.sep)
    if not value:
        if required:
            raise BuildError(f"必须填写{what}")
        return None
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise BuildError(f"{what}必须是代码目录内的相对路径")
    full = (src / p).resolve()
    try:
        full.relative_to(src.resolve())
    except ValueError as exc:
        raise BuildError(f"{what}「{raw}」不在代码目录内") from exc
    if not full.is_file():
        raise BuildError(f"找不到{what}：{full}")
    return full


def _slash_dir(p: Path) -> str:
    s = str(p)
    return s if s.endswith(("\\", "/")) else s + "\\"


def _solution_dir(restore_from: Path, project: Path) -> Path:
    """VS 编单个项目时会带上解决方案目录，$(SolutionDir) 和项目引用靠它。"""
    if restore_from.suffix.lower() == ".sln":
        return restore_from.parent
    folder = project.parent
    for _ in range(8):
        if any(folder.glob("*.sln")):
            return folder
        if folder.parent == folder:
            break
        folder = folder.parent
    return project.parent


def _platform_arg(project: Path, platform: str) -> str | None:
    plat = (platform or "").strip()
    if not plat:
        return None
    # sln 用「Any CPU」，csproj 里是 AnyCPU。对着 csproj 传带空格的会编错配置，依赖 dll 就像失踪了
    if project.suffix.lower() == ".csproj" and plat.lower() == "any cpu":
        return "AnyCPU"
    return plat


def _msbuild_props(
    configuration: str,
    platform: str | None,
    solution_dir: Path,
    restore_sources: list[str] | None = None,
    target_framework: str | None = None,
) -> list[str]:
    props = [
        f"/p:Configuration={configuration}",
        "/p:BuildProjectReferences=true",
        f"/p:SolutionDir={_slash_dir(solution_dir)}",
        "/nologo",
        "/v:m",
        "/clp:ErrorsOnly;WarningsOnly;Summary",
    ]
    if platform:
        props.insert(1, f"/p:Platform={platform}")
    if restore_sources:
        # 只应传给需要 /restore 的 SDK 项目；老 csproj 带上会走 NuGet.targets 去还原引用图。
        props.append("/p:RestoreSources=" + "%3B".join(restore_sources))
    if target_framework:
        props.append(f"/p:TargetFramework={target_framework}")
    return props


def _target_framework(raw: str, output_path: Path | None = None) -> str | None:
    """只认步骤里显式填写的 TargetFramework。

    OutputPath 的最后一级（netstandard2.0）只是 dll 落盘目录，和编哪个 TFM 不是一回事。
    IApplications 的 bat 虽然写了 netstandard2.0，后面又写了 net4.5.2，以后者为准；
    若只编 netstandard2.0，依赖库里的类型会 CS0246。
    """
    text = (raw or "").strip()
    return text or None


def _append_extra(cmd: list[str], extra: str) -> list[str]:
    """把 extraArgs 接到 MSBuild 命令上。拒绝改输出目录和加任意 Target。

    `/t:Exec` 配 `/p:Command=` 等于在构建机上跑任意命令，不能从「附加参数」进来。
    输出目录已经由步骤字段锁在工作区。
    """
    extra = extra.strip()
    if extra:
        cmd += parse_extra_args(extra)
    return cmd


_DENIED_MSBUILD_FLAGS = {"/t", "/target", "-t", "-target", "/c", "/command"}
_DENIED_MSBUILD_PROPS = {
    "outdir", "outputpath", "webprojectoutputdir", "intermediateoutputpath",
    "baseoutputpath", "publishdir", "command",
}


def parse_extra_args(extra: str) -> list[str]:
    """拆 MSBuild extraArgs。允许 /m、/p:DefineConstants，拒绝 /t:Exec 和 OutDir。"""
    text = (extra or "").strip()
    if not text:
        return []
    tokens = text.split()
    out: list[str] = []
    for tok in tokens:
        head = tok.split(":", 1)[0].split("=", 1)[0].lower()
        if head in _DENIED_MSBUILD_FLAGS:
            raise BuildError("附加参数不能指定 Target（例如 /t:Exec），编译目标由插件决定")
        prop = ""
        low = tok.lower()
        if low.startswith("/p:") or low.startswith("-p:"):
            prop = tok.split(":", 1)[1].split("=", 1)[0].strip().lower()
        if prop in _DENIED_MSBUILD_PROPS:
            raise BuildError(f"附加参数不能设置 {prop}，输出目录由步骤字段控制")
        out.append(tok)
    return out


def _assert_compile_target(project: Path, mode: str) -> None:
    if mode == "web" and project.suffix.lower() == ".sln":
        raise BuildError(
            "「Web 项目」不能填 .sln。MSBuild 会把网站目标套到解决方案里每一个项目，"
            "类库（例如 IApplications）没有 ResolveReferences，就会报 MSB4057。"
            "请把「要编译的项目」改成网站启动项目的 .csproj，还原仍填 .sln。"
        )


def _resolve_output_path(workspace: Path, workdir: Path, raw: str) -> Path | None:
    """普通编译可选的 OutputPath，必须落在工作区内。"""
    value = (raw or "").strip().replace("/", os.sep)
    if not value:
        return None
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise BuildError("OutputPath 必须是工作区内的相对路径")
    full = (workdir / p).resolve()
    return _under_workspace(workspace, full, what="OutputPath")


def _count_files(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for _ in folder.rglob("*") if _.is_file())


def _run(cmd: list[str], cwd: Path) -> int:
    return sdk.stream(cmd, cwd=cwd)


def _restore(
    solution: Path,
    src: Path,
    sources: list[str],
    nuget_path: str = "",
) -> None:
    """编译前只做一次 nuget.exe restore，对齐现场 bat。

    packages.config 靠这一步拉包。SDK 项目的 project.assets.json 不在这里对
    每个 csproj 跑 MSBuild /t:Restore，而在真正编译时对当前项目加 /restore。
    sln 若登记了 .nuget\\NuGet.exe，先写成一份只含 csproj 的临时 sln 再还原，
    避免 MSB4025，同时保持「整份解决方案还原一次」而不是 N 次。
    """
    if sources:
        sdk.log.info("NuGet 源：" + " ; ".join(sources))
    nuget = _find_nuget(nuget_path, src, solution)
    if not nuget:
        sdk.log.info(
            "未找到 nuget.exe。packages.config 不还原；"
            "若当前要编的是 SDK 项目，编译时会 /restore"
        )
        return
    sdk.log.info(f"NuGet：{nuget}")
    restore_target, tmp = _sln_for_nuget_restore(solution)
    try:
        if tmp is not None:
            sdk.log.info(
                "解决方案含 .nuget\\NuGet.exe 等非编译项，已生成临时 sln 再还原一次"
                f"（{tmp.name}）。这和 NuGet 源无关，是为了避开 MSB4025。"
            )
        sln_dir = solution.parent if solution.suffix.lower() == ".sln" else None
        sol_dir_arg = sln_dir if restore_target.suffix.lower() != ".sln" else None
        code = _run(_nuget_restore_cmd(nuget, restore_target, sources, sol_dir_arg), src)
        if code != 0:
            sdk.log.warning(
                "nuget restore 未成功。SDK 项目编译时仍会 /restore；"
                "packages.config 若缺包，会在编译时报错。"
            )
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def main() -> int:
    inp = sdk.get_input()
    workspace = Path(sdk.get_workspace())
    src = workspace / "src"
    if not src.is_dir():
        src = workspace

    try:
        msbuild = _find_msbuild(str(inp.get("msbuildPath") or ""))
        project = _resolve_project(
            src, str(inp.get("solution") or ""), required=True, what="要编译的项目"
        )
        assert project is not None
        restore_from = _resolve_project(
            src,
            str(inp.get("restoreSolution") or ""),
            required=False,
            what="还原用的解决方案",
        ) or project
        mode = str(inp.get("mode") or "web").strip()
        _assert_compile_target(project, mode)
    except BuildError as e:
        sdk.log.error(str(e))
        return 1

    configuration = str(inp.get("configuration") or "Release").strip()
    platform = str(inp.get("platform") or "Any CPU").strip()
    out_raw = str(inp.get("outputDir") or "_publish").strip().replace("/", os.sep)
    try:
        out_dir = _under_workspace(
            workspace,
            Path(out_raw) if Path(out_raw).is_absolute() else src / out_raw,
            what="输出目录",
        )
    except BuildError as e:
        sdk.log.error(str(e))
        return 1

    sdk.log.info(f"MSBuild：{msbuild}")
    sdk.log.info(f"编译：{project}")
    if restore_from != project:
        sdk.log.info(f"还原：{restore_from}")
    plat = _platform_arg(project, platform)
    sln_dir = _solution_dir(restore_from, project)
    sdk.log.info(f"配置：{configuration} | {plat or platform} | 方式：{mode}")
    sdk.log.info(f"SolutionDir：{sln_dir}")

    if _truthy(inp.get("clean", True)) and out_dir.is_dir():
        sdk.log.info(f"清空输出目录：{out_dir}")
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    if _truthy(inp.get("restore", True)):
        _restore(
            restore_from,
            src,
            _nuget_sources(inp.get("nugetSources")),
            str(inp.get("nugetPath") or ""),
        )

    extra = str(inp.get("extraArgs") or "")
    sources = _nuget_sources(inp.get("nugetSources"))
    workdir = project.parent if project.suffix.lower() == ".csproj" else src
    output_path = (
        _resolve_output_path(workspace, workdir, str(inp.get("outputPath") or "")) if mode != "web" else None
    )
    tfm = _target_framework(str(inp.get("targetFramework") or ""), output_path)
    sdk_restore = _is_sdk_style_project(project)
    props = _msbuild_props(
        configuration, plat, sln_dir, sources if sdk_restore else None, tfm
    )
    if tfm:
        sdk.log.info(f"TargetFramework：{tfm}")
    if sdk_restore:
        sdk.log.info("当前是 SDK / PackageReference 项目，编译带 /restore 以生成 project.assets.json")

    if mode == "web":
        # 对齐 VS「生成当前项目」：先 Build，这样 ProjectReference 的类库会编到各自 bin。
        # 不要设 OutDir——会套到依赖项目上，dll 对不上。拷站点用 WebProjectOutputDir。
        build_cmd = [msbuild, str(project)]
        if sdk_restore:
            build_cmd.append("/restore")
        build_cmd = _append_extra(build_cmd + ["/t:Build", *props], extra)
        sdk.log.info("先 Build（含项目引用），与 VS 单独生成当前项目一致")
        code = _run(build_cmd, workdir)
        if code != 0:
            sdk.log.error(f"MSBuild Build 失败，退出码 {code}")
            return code
        copied = False
        for target in ("_WPPCopyWebApplication", "_CopyWebApplication"):
            copy_cmd = _append_extra(
                [
                    msbuild,
                    str(project),
                    f"/t:{target}",
                    *props,
                    f"/p:WebProjectOutputDir={out_dir}\\",
                ],
                extra,
            )
            sdk.log.info(f"拷贝站点：{target} → {out_dir}")
            if _run(copy_cmd, workdir) == 0:
                copied = True
                break
            sdk.log.warning(f"{target} 失败，尝试下一个拷贝目标")
        if not copied:
            sdk.log.error("无法把站点拷到输出目录。请确认这是 Web Application 项目")
            return 1
        artifact_dir = out_dir
    else:
        build_props = list(props)
        if output_path is not None:
            build_props.append(f"/p:OutputPath={_slash_dir(output_path)}")
            artifact_dir = output_path
            sdk.log.info(f"OutputPath：{artifact_dir}")
        else:
            artifact_dir = project.parent / "bin"
            sdk.log.info(f"产物目录：{artifact_dir}（项目默认 bin，未改 OutDir）")
        cmd = [msbuild, str(project)]
        if sdk_restore:
            cmd.append("/restore")
        cmd = _append_extra(cmd + ["/t:Build", *build_props], extra)
        code = _run(cmd, workdir)
        if code != 0:
            sdk.log.error(f"MSBuild 编译失败，退出码 {code}")
            return code

    produced = _count_files(artifact_dir)
    if produced == 0:
        if mode == "web":
            sdk.log.error(
                f"编译成功但输出目录 {artifact_dir} 是空的。"
                "若项目不是 Web 项目，请把「编译方式」改成「普通编译」"
            )
            return 1
        sdk.log.warning(
            f"编译成功，但未在 {artifact_dir} 下找到文件。"
            "多目标项目请在「类库输出路径」填 bin\\Debug\\netstandard2.0\\，"
            "多目标项目请填「类库输出路径」；目标框架对齐现场 bat 填 net4.5.2，不要只填 netstandard2.0"
        )

    sdk.log.info(f"编译完成，输出目录 {artifact_dir} 共 {produced} 个文件")
    sdk.set_output(
        {
            "build_output_dir": {"type": "string", "value": str(artifact_dir)},
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
