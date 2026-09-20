# -*- coding: utf-8 -*-
"""msbuild-build 要把 nuget restore -source 写进步骤参数。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "msbuild-build"


def _load():
    sys.path.insert(0, str(PLUGIN))
    spec = importlib.util.spec_from_file_location("msbuild_build_task", PLUGIN / "task.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_parse_semicolon_sources_like_nuget_cli():
    mod = _load()
    raw = (
        'https://api.nuget.org/v3/index.json;'
        "https://nuget.org/api/v2/"
    )
    assert mod._lines(raw) == [
        "https://api.nuget.org/v3/index.json",
        "https://nuget.org/api/v2/",
    ]


def test_parse_multiline_and_quoted_sources():
    mod = _load()
    raw = '"https://api.nuget.org/v3/index.json"\nhttps://nuget.org/api/v2/\n'
    assert mod._lines(raw) == [
        "https://api.nuget.org/v3/index.json",
        "https://nuget.org/api/v2/",
    ]


def test_nuget_restore_cmd_joins_sources():
    mod = _load()
    sln = Path(r"D:\src\workspace\oms\Web.sln")
    sources = [
        "https://api.nuget.org/v3/index.json",
        "https://nuget.org/api/v2/",
    ]
    cmd = mod._nuget_restore_cmd("nuget", sln, sources)
    assert cmd[:4] == ["nuget", "restore", str(sln), "-NonInteractive"]
    assert cmd[4:6] == [
        "-Source",
        "https://api.nuget.org/v3/index.json;https://nuget.org/api/v2/",
    ]


def test_msbuild_restore_escapes_semicolons():
    mod = _load()
    sln = Path("app.sln")
    sources = [
        "https://api.nuget.org/v3/index.json",
        "https://nuget.org/api/v2/",
    ]
    cmd = mod._msbuild_restore_cmd("MSBuild.exe", sln, sources)
    prop = [x for x in cmd if x.startswith("/p:RestoreSources=")][0]
    assert ";" not in prop
    assert prop == (
        "/p:RestoreSources="
        "https://api.nuget.org/v3/index.json%3B"
        "https://nuget.org/api/v2/"
    )


def test_as_nuget_exe_accepts_bare_name_and_directory(tmp_path: Path):
    mod = _load()
    bare = tmp_path / "nuget"
    bare.write_bytes(b"fake")
    assert Path(mod._as_nuget_exe(bare)).name == "nuget"
    exe = tmp_path / "tools" / "nuget.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"fake")
    assert Path(mod._as_nuget_exe(exe.parent)).name == "nuget.exe"
    assert Path(mod._as_nuget_exe(tmp_path / "tools" / "nuget")).name == "nuget.exe"


def test_web_mode_rejects_solution_file():
    mod = _load()
    sln = Path("Web.sln")
    try:
        mod._assert_compile_target(sln, "web")
    except mod.BuildError as e:
        assert "MSB4057" in str(e)
    else:
        raise AssertionError("expected BuildError")
    mod._assert_compile_target(Path("Web.csproj"), "web")
    mod._assert_compile_target(sln, "build")


def test_csproj_platform_maps_any_cpu():
    mod = _load()
    assert mod._platform_arg(Path("a.csproj"), "Any CPU") == "AnyCPU"
    assert mod._platform_arg(Path("a.sln"), "Any CPU") == "Any CPU"


def test_solution_dir_prefers_restore_sln():
    mod = _load()
    sln = Path(r"D:\src\workspace\oms\Web.sln")
    proj = Path(r"D:\src\workspace\oms\Server\Web.csproj")
    assert mod._solution_dir(sln, proj) == sln.parent


def test_web_props_enable_project_references():
    mod = _load()
    sln_dir = Path(r"D:\src\workspace\oms")
    props = mod._msbuild_props("Release", "AnyCPU", sln_dir)
    assert "/p:BuildProjectReferences=true" in props
    assert any(p.startswith("/p:SolutionDir=") and p.endswith("\\") for p in props)
    assert "/p:Platform=AnyCPU" in props
    with_src = mod._msbuild_props(
        "Release",
        "AnyCPU",
        sln_dir,
        ["https://api.nuget.org/v3/index.json", "https://nuget.org/api/v2/"],
    )
    assert any(p.startswith("/p:RestoreSources=") and "%3B" in p for p in with_src)


def test_empty_sources_use_public_defaults():
    mod = _load()
    assert mod._nuget_sources("") == [
        "https://api.nuget.org/v3/index.json",
        "https://nuget.org/api/v2/",
    ]
    sln = Path("app.sln")
    assert mod._nuget_restore_cmd("nuget", sln, []) == [
        "nuget", "restore", "app.sln", "-NonInteractive",
    ]


def test_target_framework_only_when_explicit():
    mod = _load()
    out = Path(r"D:\src\Framework\OMS.IApplications\bin\Debug\netstandard2.0")
    assert mod._target_framework("", out) is None
    assert mod._target_framework("net4.5.2", out) == "net4.5.2"
    props = mod._msbuild_props("Release", "AnyCPU", Path(r"D:\src"), None, "net4.5.2")
    assert "/p:TargetFramework=net4.5.2" in props
    props_plain = mod._msbuild_props("Release", "AnyCPU", Path(r"D:\src"))
    assert not any(p.startswith("/p:TargetFramework=") for p in props_plain)


def test_resolve_output_path_relative_to_workdir(tmp_path: Path):
    """相对 OutputPath 拼在项目目录下，且必须仍落在工作区内。"""
    mod = _load()
    workspace = tmp_path
    workdir = tmp_path / "Framework" / "OMS.IApplications"
    workdir.mkdir(parents=True)
    hit = mod._resolve_output_path(workspace, workdir, r"bin\Debug\netstandard2.0\\")
    assert hit == (workdir / "bin" / "Debug" / "netstandard2.0").resolve()
    assert mod._resolve_output_path(workspace, workdir, "") is None
    assert mod._resolve_output_path(workspace, workdir, "   ") is None


def test_nuget_restore_csproj_passes_solution_directory():
    mod = _load()
    sln_dir = Path(r"D:\src")
    proj = sln_dir / "App" / "App.csproj"
    cmd = mod._nuget_restore_cmd("nuget", proj, [], sln_dir)
    assert cmd == [
        "nuget", "restore", str(proj), "-NonInteractive",
        "-SolutionDirectory", str(sln_dir),
    ]


def test_sln_projects_skips_nuget_exe(tmp_path: Path):
    """旧 Enable Package Restore 会把 NuGet.exe 写进 sln，还原时必须丢掉。"""
    mod = _load()
    app = tmp_path / "App" / "App.csproj"
    app.parent.mkdir()
    app.write_text("<Project/>", encoding="utf-8")
    lib = tmp_path / "Lib" / "Lib.csproj"
    lib.parent.mkdir()
    lib.write_text("<Project/>", encoding="utf-8")
    sln = tmp_path / "App.sln"
    sln.write_text(
        "\n".join(
            [
                'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "App\\App.csproj", "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"',
                "EndProject",
                'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Lib", "Lib\\Lib.csproj", "{BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB}"',
                "EndProject",
                'Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = ".nuget", ".nuget", "{CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC}"',
                "EndProject",
                'Project("{911E67C6-3D85-4FCE-B60C-F9A76D4F0012}") = "NuGet", ".nuget\\NuGet.exe", "{DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD}"',
                "EndProject",
            ]
        ),
        encoding="utf-8",
    )
    got = [p.resolve() for p in mod._sln_projects(sln)]
    assert app.resolve() in got
    assert lib.resolve() in got
    assert all(p.suffix.lower() == ".csproj" for p in got)


def test_filtered_sln_drops_nuget_exe_keeps_csproj():
    """nuget restore 仍是一次 sln，只是先去掉 NuGet.exe，不能改成逐个 csproj。"""
    mod = _load()
    raw = "\n".join(
        [
            "Microsoft Visual Studio Solution File, Format Version 12.00",
            'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "App\\App.csproj", "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"',
            "EndProject",
            'Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = ".nuget", ".nuget", "{CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC}"',
            "EndProject",
            'Project("{911E67C6-3D85-4FCE-B60C-F9A76D4F0012}") = "NuGet", ".nuget\\NuGet.exe", "{DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD}"',
            "EndProject",
            "Global",
            "\tGlobalSection(ProjectConfigurationPlatforms) = postSolution",
            "\t\t{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}.Debug|Any CPU.ActiveCfg = Debug|Any CPU",
            "\t\t{DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD}.Debug|Any CPU.ActiveCfg = Debug|Any CPU",
            "\tEndGlobalSection",
            "EndGlobal",
            "",
        ]
    )
    got = mod._filtered_sln_text(raw)
    assert got is not None
    assert "App.csproj" in got
    assert "NuGet.exe" not in got
    assert "DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD" not in got
    assert "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}" in got
    assert mod._filtered_sln_text(
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "App\\App.csproj", "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"\nEndProject\n'
    ) is None


def test_sln_for_nuget_restore_writes_sibling(tmp_path: Path):
    mod = _load()
    sln = tmp_path / "App.sln"
    sln.write_text(
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "App\\App.csproj", "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"\nEndProject\n'
        'Project("{911E67C6-3D85-4FCE-B60C-F9A76D4F0012}") = "NuGet", ".nuget\\NuGet.exe", "{DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD}"\nEndProject\n',
        encoding="utf-8",
    )
    target, tmp = mod._sln_for_nuget_restore(sln)
    assert tmp is not None
    assert target.parent == sln.parent
    assert target.name.endswith(".release-restore.sln")
    assert "NuGet.exe" not in target.read_text(encoding="utf-8")
    tmp.unlink()
    plain = tmp_path / "plain.sln"
    plain.write_text(
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "App\\App.csproj", "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"\nEndProject\n',
        encoding="utf-8",
    )
    target2, tmp2 = mod._sln_for_nuget_restore(plain)
    assert tmp2 is None
    assert target2 == plain


def test_sdk_style_detection(tmp_path: Path):
    mod = _load()
    sdk_proj = tmp_path / "Lib.csproj"
    sdk_proj.write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n<PropertyGroup></PropertyGroup>\n</Project>\n',
        encoding="utf-8",
    )
    old = tmp_path / "Web.csproj"
    old.write_text(
        '<Project ToolsVersion="15.0">\n<ItemGroup></ItemGroup>\n</Project>\n',
        encoding="utf-8",
    )
    pkg = tmp_path / "Pkg.csproj"
    pkg.write_text(
        '<Project ToolsVersion="15.0">\n<ItemGroup><PackageReference Include="Newtonsoft.Json" /></ItemGroup>\n</Project>\n',
        encoding="utf-8",
    )
    assert mod._is_sdk_style_project(sdk_proj) is True
    assert mod._is_sdk_style_project(old) is False
    assert mod._is_sdk_style_project(pkg) is True


def test_restore_does_not_msbuild_each_csproj():
    """编译前只 nuget restore 一次；不得再对每个 csproj 跑 MSBuild /t:Restore。"""
    src = (PLUGIN / "task.py").read_text(encoding="utf-8")
    assert "_restore_targets" not in src
    assert "for target in targets" not in src
    body = src.split("def _restore(")[1].split("def main(")[0]
    assert "_msbuild_restore_cmd" not in body


def test_build_mode_must_not_default_outdir_to_publish():
    """普通编译若默认 OutDir=_publish，下游 HintPath 指向 bin 就会 CS0246。"""
    src = (PLUGIN / "task.py").read_text(encoding="utf-8")
    # 锁定：build 分支不得再出现默认 OutDir=_publish 的拼装
    assert 'f"/p:OutDir={out_dir}' not in src
    assert "/p:OutputPath=" in src
    assert "outputPath" in (PLUGIN / "task.json").read_text(encoding="utf-8")
