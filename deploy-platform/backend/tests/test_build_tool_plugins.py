"""构建类插件：能定位工具、拒绝危险参数，不依赖本机真的装着 Maven。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_plugin_deploy_safety import _load


def test_maven_finds_install_under_data_soft(tmp_path: Path, monkeypatch):
    """步骤没填 Maven 目录时才扫 /data/soft；仓库里即便有 mvnw 也不抢先。"""
    task = _load("maven-build")
    maven_bin = tmp_path / "data" / "soft" / "maven" / "bin"
    maven_bin.mkdir(parents=True)
    mvn = maven_bin / ("mvn.cmd" if __import__("os").name == "nt" else "mvn")
    mvn.write_text("@echo off\n" if mvn.suffix == ".cmd" else "#!/bin/sh\n", encoding="utf-8")
    if mvn.suffix != ".cmd":
        mvn.chmod(0o755)
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(task, "_tool_install_prefixes", lambda: [tmp_path / "data" / "soft"])
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    found = task.find_mvn(tmp_path)
    assert Path(found).resolve() == mvn.resolve()


def test_maven_uses_configured_home(tmp_path: Path, monkeypatch):
    """步骤里填了安装目录就用这一份，不再扫、不用 mvnw。"""
    task = _load("maven-build")
    home = tmp_path / "custom-maven"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    mvn = bin_dir / ("mvn.cmd" if __import__("os").name == "nt" else "mvn")
    mvn.write_text("@echo off\n" if mvn.suffix == ".cmd" else "#!/bin/sh\n", encoding="utf-8")
    if mvn.suffix != ".cmd":
        mvn.chmod(0o755)
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(task, "_tool_install_prefixes", lambda: [])
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    found = task.find_mvn(tmp_path, configured=str(home))
    assert Path(found).resolve() == mvn.resolve()


def test_maven_bad_configured_home_fails(tmp_path: Path, monkeypatch):
    """填了错误目录就失败，不能 silently 改去扫描。"""
    task = _load("maven-build")
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    try:
        task.find_mvn(tmp_path, configured=str(tmp_path / "no-maven"))
        raise AssertionError("should fail")
    except task.BuildError as exc:
        assert "找不到 mvn" in str(exc)


def test_maven_discovers_jdk_under_data_soft(tmp_path: Path, monkeypatch):
    """Agent 服务没有 JAVA_HOME 时，应能从 /data/soft/jdk8 认出 JDK。"""
    task = _load("maven-build")
    jdk = tmp_path / "data" / "soft" / "jdk8"
    java = jdk / "bin" / ("java.exe" if __import__("os").name == "nt" else "java")
    java.parent.mkdir(parents=True)
    java.write_text("", encoding="utf-8")
    monkeypatch.setattr(task, "_tool_install_prefixes", lambda: [tmp_path / "data" / "soft"])
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.delenv("JAVA_HOME_8", raising=False)
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    assert task._discover_java_home() == str(jdk)
    assert task._java_home_for("8") == str(jdk)


def test_maven_missing_selected_jdk_fails(tmp_path: Path, monkeypatch):
    """选了 JDK 21 但机器上没有时必须失败，不能悄悄用 JDK 8。"""
    task = _load("maven-build")
    monkeypatch.setattr(task, "_tool_install_prefixes", lambda: [tmp_path / "empty"])
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.delenv("JAVA_HOME_21", raising=False)
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    try:
        task.resolve_java_home("21", "")
        raise AssertionError("should fail")
    except task.BuildError as exc:
        assert "JDK 21" in str(exc)
        assert "本步骤" in str(exc)


def test_maven_bad_java_home_fails(tmp_path: Path):
    """步骤填了错误的 JDK 目录就失败，不能改去扫另一套。"""
    task = _load("maven-build")
    try:
        task.resolve_java_home("", str(tmp_path / "no-jdk"))
        raise AssertionError("should fail")
    except task.BuildError as exc:
        assert "找不到 java" in str(exc)


def test_maven_wrapper_is_last_resort(tmp_path: Path, monkeypatch):
    """本机没有 mvn 时才用仓库 mvnw。"""
    task = _load("maven-build")
    wrapper = tmp_path / "mvnw"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(task, "_tool_install_prefixes", lambda: [])
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    found = task.find_mvn(tmp_path)
    assert Path(found).name == "mvnw"


def test_maven_rejects_shell_in_goals():
    """goal 走参数列表，管道和分号必须被拒。"""
    task = _load("maven-build")
    assert task.parse_goals("clean package") == ["clean", "package"]
    try:
        task.parse_goals("package; rm -rf /")
        raise AssertionError("should reject")
    except task.BuildError as exc:
        assert "非法" in str(exc)


def test_maven_skip_tests_flag():
    """跳过测试的开关要认 bool 和字符串两种表单值。"""
    task = _load("maven-build")
    assert task._truthy(True)
    assert task._truthy("true")
    assert not task._truthy(False)


def test_maven_workdir_subdir_or_root(tmp_path: Path):
    """执行目录留空用代码根；填子目录就在那里跑 mvn，不能逃出仓库。"""
    task = _load("maven-build")
    src = tmp_path / "src"
    sub = src / "order-service"
    sub.mkdir(parents=True)
    assert task.resolve_workdir(src, "") == src
    assert task.resolve_workdir(src, "order-service") == sub.resolve()
    try:
        task.resolve_workdir(src, "..")
        raise AssertionError("should reject")
    except task.BuildError:
        pass
    try:
        task.resolve_workdir(src, "/etc")
        raise AssertionError("should reject")
    except task.BuildError:
        pass
    try:
        task.resolve_workdir(src, "missing-module")
        raise AssertionError("should reject")
    except task.BuildError:
        pass


def test_gradle_prefers_wrapper(tmp_path: Path):
    """有 gradlew 时用 wrapper，和 Maven 同一套策略。"""
    task = _load("gradle-build")
    name = "gradlew.bat" if __import__("os").name == "nt" else "gradlew"
    (tmp_path / name).write_text("echo", encoding="utf-8")
    found = task.find_gradle(tmp_path)
    assert Path(found).name == name


def test_gradle_rejects_shell_in_tasks():
    """Gradle 任务名同样禁止 shell 拼接。"""
    task = _load("gradle-build")
    assert task.parse_tasks("clean build") == ["clean", "build"]
    try:
        task.parse_tasks("build && reboot")
        raise AssertionError("should reject")
    except task.BuildError:
        pass


def test_npm_finds_install_under_nodejs_home(tmp_path: Path, monkeypatch):
    """登录 shell 的 NODEJS_HOME=/usr/local/nodejs，systemd PATH 里没有。"""
    task = _load("npm-build")
    root = tmp_path / "usr" / "local" / "nodejs"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    npm = bin_dir / ("npm.cmd" if __import__("os").name == "nt" else "npm")
    npm.write_text("", encoding="utf-8")
    if npm.suffix != ".cmd":
        npm.chmod(0o755)
    monkeypatch.setattr(task, "_tool_install_prefixes", lambda: [])
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    found = task.find_npm(configured=str(root))
    assert Path(found).resolve() == npm.resolve()


def test_npm_bad_node_home_fails(tmp_path: Path, monkeypatch):
    """步骤填了错误的 Node 目录就失败，不能改去扫 PATH。"""
    task = _load("npm-build")
    monkeypatch.setattr(task.shutil, "which", lambda *a, **k: None)
    try:
        task.find_npm(configured=str(tmp_path / "no-node"))
        raise AssertionError("should fail")
    except task.BuildError as exc:
        assert "找不到 npm" in str(exc)


def test_npm_rejects_bad_script_name():
    """npm script 只允许包名形态，避免 run 后面跟额外命令。"""
    task = _load("npm-build")
    assert task._SCRIPT_NAME.fullmatch("build")
    assert not task._SCRIPT_NAME.fullmatch("build; rm")


def test_maven_settings_machine_or_src(tmp_path: Path):
    """带密码的 settings 在构建机上：绝对路径指向 settings.xml 即可，不必进仓库。"""
    task = _load("maven-build")
    src = tmp_path / "src"
    src.mkdir()
    repo = src / ".mvn"
    repo.mkdir()
    repo_xml = repo / "settings.xml"
    repo_xml.write_text("<settings/>", encoding="utf-8")
    assert task.resolve_settings(src, ".mvn/settings.xml") == repo_xml.resolve()
    assert task.resolve_settings(src, "") is None

    machine = tmp_path / "maven" / "conf"
    machine.mkdir(parents=True)
    machine_xml = machine / "settings.xml"
    machine_xml.write_text("<settings/>", encoding="utf-8")
    assert task.resolve_settings(src, str(machine_xml)) == machine_xml

    try:
        task.resolve_settings(src, str(tmp_path / "secret.xml"))
        raise AssertionError("should reject non-settings name")
    except task.BuildError:
        pass
    try:
        task.resolve_settings(src, "../outside.xml")
        raise AssertionError("should reject")
    except task.BuildError:
        pass


def test_dotnet_extra_args_cannot_override_output():
    """-o / --output 会覆盖步骤里锁在工作区的输出目录。"""
    task = _load("dotnet-publish")
    assert "-p:PublishSingleFile=true" in " ".join(task.parse_extra_args("-p:PublishSingleFile=true"))
    try:
        task.parse_extra_args("-o /tmp/evil")
        raise AssertionError("should reject")
    except task.BuildError:
        pass
    try:
        task.parse_extra_args("/p:OutputPath=C:\\wwwroot")
        raise AssertionError("should reject")
    except task.BuildError:
        pass


def test_docker_compile_argv_follows_toolchain_image(tmp_path: Path):
    """不同基础镜像、不同编译命令，拼出的 docker run 必须跟着变。"""
    task = _load("docker-compile")
    src = tmp_path / "src"
    src.mkdir(parents=True)

    npm_image = "node:14-alpine"
    npm_cmd = "npm i && npm run build-master"
    argv = task.build_run_argv(
        docker="docker",
        image=task._assert_image(npm_image),
        command=task._assert_command(npm_cmd),
        host_source=src,
        workdir=task._assert_container_path("/code", what="容器工作目录"),
        cache_volumes=[task._assert_named_volume("npm_cache:/root/.npm")],
        file_binds=[],
        envs=[],
        extra_args=[],
        shell=task._assert_shell("sh"),
        invoke="shell",
    )
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--entrypoint") + 1] == "sh"
    assert "npm_cache:/root/.npm" in argv
    assert "-w" in argv and argv[argv.index("-w") + 1] == "/code"
    assert argv[-3:] == [npm_image, "-c", npm_cmd]

    maven_image = "registry.example.com/base/maven:3.8-jdk8"
    maven_cmd = "mvn -B clean package -DskipTests"
    argv_java = task.build_run_argv(
        docker="docker",
        image=task._assert_image(maven_image),
        command=task._assert_command(maven_cmd),
        host_source=src,
        workdir="/code",
        cache_volumes=task.resolve_cache_volumes("maven", ""),
        file_binds=[],
        envs=[],
        extra_args=[],
        shell="sh",
        invoke="shell",
    )
    assert maven_image in argv_java
    assert "maven_cache:/root/.m2" in argv_java
    assert argv_java[-1] == maven_cmd
    assert "--entrypoint" in argv_java

    direct = task.build_run_argv(
        docker="docker",
        image=maven_image,
        command="mvn -B package",
        host_source=src,
        workdir="/code",
        cache_volumes=[],
        file_binds=[],
        envs=[],
        extra_args=[],
        shell="sh",
        invoke="direct",
    )
    assert "--entrypoint" not in direct
    assert direct[-3:] == ["mvn", "-B", "package"]

    pnpm_image = "node:18-alpine"
    pnpm_cmd = "pnpm install && npm run build:pcmall:uat"
    argv2 = task.build_run_argv(
        docker="docker",
        image=task._assert_image(pnpm_image),
        command=task._assert_command(pnpm_cmd),
        host_source=src,
        workdir="/code",
        cache_volumes=["npm_cache:/root/.npm"],
        file_binds=[],
        envs=[],
        extra_args=[],
        shell="sh",
        invoke="shell",
    )
    assert pnpm_image in argv2
    assert argv2[-1] == pnpm_cmd
    assert npm_image not in argv2

    with pytest.raises(task.CompileError):
        task._assert_image("")
    with pytest.raises(task.CompileError):
        task._assert_command("")
    with pytest.raises(task.CompileError):
        task._assert_image("nodebuild:v14; rm -rf /")


def test_docker_compile_toolchain_cache_and_settings(tmp_path: Path):
    """缓存卷留空按工具链预置；Maven settings 可以挂构建机上的文件。"""
    task = _load("docker-compile")
    assert task.resolve_cache_volumes("maven", "") == ["maven_cache:/root/.m2"]
    assert task.resolve_cache_volumes("node", "") == ["npm_cache:/root/.npm"]
    assert task.resolve_cache_volumes("gradle", "") == ["gradle_cache:/root/.gradle"]
    assert task.resolve_cache_volumes("custom", "") == []
    assert task.resolve_cache_volumes("go", "go_mod_cache:/go/pkg/mod") == ["go_mod_cache:/go/pkg/mod"]

    ws = tmp_path / "job"
    src = ws / "src"
    src.mkdir(parents=True)
    settings = tmp_path / "maven" / "conf"
    settings.mkdir(parents=True)
    xml = settings / "settings.xml"
    xml.write_text("<settings/>", encoding="utf-8")
    spec = task.resolve_file_bind(ws, src, f"{xml}:/root/.m2/settings.xml")
    assert spec.endswith("/root/.m2/settings.xml")
    with pytest.raises(task.CompileError):
        task.resolve_file_bind(ws, src, "maven_cache:/root/.m2")
    with pytest.raises(task.CompileError):
        task.resolve_file_bind(ws, src, "/var/run/docker.sock:/var/run/docker.sock")
    with pytest.raises(task.CompileError):
        task._command_argv("mvn package && rm -rf /")


def test_msbuild_extra_args_reject_exec_target():
    """/t:Exec 配 /p:Command 等于任意命令，不能从附加参数进来。"""
    task = _load("msbuild-build")
    assert "/m" in task.parse_extra_args("/p:DefineConstants=PROD /m")
    try:
        task.parse_extra_args("/t:Exec /p:Command=calc")
        raise AssertionError("should reject")
    except task.BuildError:
        pass
    try:
        task.parse_extra_args("/p:OutDir=C:\\wwwroot")
        raise AssertionError("should reject")
    except task.BuildError:
        pass
