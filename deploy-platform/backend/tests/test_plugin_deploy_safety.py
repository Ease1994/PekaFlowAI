# -*- coding: utf-8 -*-
"""部署类插件的安全行为。

这里每一条都对应一种「出事了才会知道」的走法：凭证被写进库、回滚参数被污染、
等待时长没有上限把构建机执行槽占死。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _load(plugin: str, module: str = "task"):
    """按文件路径加载插件模块。

    各插件目录里都内嵌了一份 release_atom_sdk，import 时要让插件目录在 sys.path 上；
    模块名带上插件名，避免几个插件的 task.py 在 sys.modules 里互相覆盖。
    """
    root = PLUGINS / plugin
    path = root / f"{module}.py"
    if not path.is_file():
        pytest.skip(f"插件不存在：{path}")
    sys.path.insert(0, str(root))
    try:
        name = f"release_{plugin.replace('-', '_')}_{module}"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(root))


# ============ docker-deploy ============

PASSWORD = "s3cret-registry-token"


def test_docker_undo_with_never_carries_the_registry_password():
    """部署记录会明文落库、并在回滚时回填成任务参数，凭证不能在里面。

    task.json 里 registryPassword 标的是 type: password，本来就不该落地。
    """
    task = _load("docker-deploy")
    inp = {
        "image": "repo/app:new",
        "containerName": "app",
        "registryUser": "ci",
        "registryPassword": PASSWORD,
        "registryCredentialId": 42,
        "ports": "8080:80",
        "_deployment_record_id": 7,
    }
    undo = task._undo_with(inp, "repo/app:old")

    assert "registryPassword" not in undo
    assert "registryUser" not in undo
    # 凭证 id 不是密文，留给回滚时再解密注入
    assert undo["registryCredentialId"] == 42
    # 内部字段不该回填给插件
    assert "_deployment_record_id" not in undo
    # 回滚的目标就是上一个镜像
    assert undo["image"] == "repo/app:old"
    # 非凭证的部署参数要留着，否则回滚起出来的容器少了端口映射
    assert undo["ports"] == "8080:80"

    # 最要紧的一条：整个 payload 序列化后都不该出现密码
    payload = {"undo_with": undo, "previous_image": "repo/app:old"}
    assert PASSWORD not in json.dumps(payload, ensure_ascii=False)


def test_docker_int_input_is_bounded():
    """healthTimeout 之类的等待时长必须封顶。

    不封的话填个 999999 就能让这台构建机的执行槽被占十几天，等于从队列里消失。
    """
    task = _load("docker-deploy")
    assert task._int_input({}, "healthTimeout", 0, 3600) == 0
    assert task._int_input({"healthTimeout": ""}, "healthTimeout", 0, 3600) == 0
    assert task._int_input({"healthTimeout": "30"}, "healthTimeout", 0, 3600) == 30
    assert task._int_input({"healthTimeout": 999999}, "healthTimeout", 0, 3600) == 3600
    assert task._int_input({"healthTimeout": -5}, "healthTimeout", 0, 3600) == 0
    # 填了非数字不该抛 traceback，退回默认值就行
    assert task._int_input({"healthTimeout": "abc"}, "healthTimeout", 0, 3600) == 0


# ============ k8s-deploy ============

KUBECONFIG = "apiVersion: v1\nusers:\n- name: admin\n  user:\n    token: SUPER-SECRET\n"


def test_k8s_empty_namespace_is_rejected():
    """空命名空间不能落到 default，否则可能发到错误的命名空间。"""
    task = _load("k8s-deploy")
    with pytest.raises(task.DeployError) as ei:
        task.require_namespace("")
    assert "不会默认落到 default" in str(ei.value)
    with pytest.raises(task.DeployError):
        task.require_namespace("  ")
    assert task.require_namespace("prod") == "prod"


def test_k8s_undo_with_never_carries_the_kubeconfig():
    """kubeconfig 是整个集群的凭证，一次库备份泄露就等于集群被接管。"""
    task = _load("k8s-deploy")
    inp = {
        "workload": "web",
        "workloadKind": "deployment",
        "namespace": "prod",
        "image": "repo/app:new",
        "kubeconfig": KUBECONFIG,
        "timeout": "300",
        "_deployment_record_id": 3,
    }
    undo, dropped = task._undo_with(inp, "8")

    assert "kubeconfig" not in undo
    assert dropped is True, "去掉了 kubeconfig 就要告知使用者，否则回滚失败时无从下手"
    # 回滚靠 rollout undo 到旧 revision，不需要镜像地址
    assert "image" not in undo
    assert undo["toRevision"] == "8"
    assert undo["namespace"] == "prod"
    assert "_deployment_record_id" not in undo

    payload = {"undo_with": undo, "previous_revision": "8"}
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SUPER-SECRET" not in serialized
    assert "apiVersion" not in serialized


def test_k8s_no_kubeconfig_means_nothing_to_warn_about():
    """用构建机默认配置的场景，没丢东西就不该多嘴。"""
    task = _load("k8s-deploy")
    undo, dropped = task._undo_with({"workload": "web"}, "2")
    assert dropped is False
    assert undo["toRevision"] == "2"


def test_k8s_timeout_is_bounded():
    """timeout 会变成 kubectl --timeout=Ns，无上限等于能把执行槽占死。"""
    task = _load("k8s-deploy")
    assert task._timeout_input({}) == 300
    assert task._timeout_input({"timeout": ""}) == 300
    assert task._timeout_input({"timeout": "600"}) == 600
    assert task._timeout_input({"timeout": 999999}) == 3600
    assert task._timeout_input({"timeout": 0}) == 300
    assert task._timeout_input({"timeout": "abc"}) == 300


# ============ pack-incremental ============


def test_safe_version_blocks_path_traversal_and_header_injection():
    """版本号既当文件名又进 multipart 头，两边都得净化。

    带 ../ 能让 zip 落到 workspace 之外；带引号或换行能把 multipart 的头拆开。
    """
    task = _load("pack-incremental")
    safe = task._safe_version

    # 路径穿越：切完之后不能再有分隔符和 ..
    for raw in ("../../etc/passwd", "..\\..\\windows", "a/b/c"):
        out = safe(raw)
        assert "/" not in out and "\\" not in out
        assert ".." not in out

    # 头注入：引号和换行必须没了
    out = safe('1.0"\r\nX-Evil: yes')
    assert '"' not in out and "\r" not in out and "\n" not in out

    # 正常版本号不该被改动
    assert safe("1.2.3-rc1") == "1.2.3-rc1"
    assert safe("release_2026.08") == "release_2026.08"

    # 空值要有兜底，否则文件名会变成 incremental-.zip
    assert safe("") == "latest"
    assert safe("   ") == "latest"
    assert safe("///") == "latest"

    # 过长的值会撞上文件名长度限制
    assert len(safe("v" * 500)) <= 120


def test_safe_filename_strips_quotes_and_newlines():
    task = _load("pack-incremental")
    assert '"' not in task._safe_filename('a"b.zip')
    assert "\n" not in task._safe_filename("a\nb.zip")
    assert task._safe_filename("incremental-1.2.3.zip") == "incremental-1.2.3.zip"


def test_pack_source_dir_must_stay_in_workspace(tmp_path):
    """sourceDir 越出工作区会把无关文件打进增量包，再覆盖生产。"""
    task = _load("pack-incremental")
    ws = tmp_path / "job"
    src = ws / "src"
    src.mkdir(parents=True)
    (src / "bin").mkdir()
    (src / "bin" / "a.dll").write_bytes(b"x")

    ok = task._resolve_source(ws, "bin")
    assert ok == (src / "bin").resolve()

    with pytest.raises(task.ManifestError):
        task._resolve_source(ws, str(tmp_path / "outside"))
    with pytest.raises(task.ManifestError):
        task._resolve_source(ws, "../..")
    with pytest.raises(task.ManifestError):
        task._resolve_source(ws, "")


def test_pack_double_star_manifest_includes_nested_files(tmp_path):
    """步骤或本次执行显式写出 ** 时，仍能打到编译产物子目录里的文件。"""
    man = _load("pack-incremental", "manifest")
    root = tmp_path / "_publish"
    (root / "bin").mkdir(parents=True)
    (root / "index.html").write_text("ok", encoding="utf-8")
    (root / "bin" / "app.dll").write_bytes(b"x")
    files = man.collect(root, "**")
    assert "index.html" in files
    assert "bin/app.dll" in files


def test_docker_deploy_rejects_privileged_and_docker_sock():
    task = _load("docker-deploy")
    with pytest.raises(task.DeployError):
        task._assert_safe_extra_args("--privileged")
    with pytest.raises(task.DeployError):
        task._assert_safe_extra_args("--pid=host")
    with pytest.raises(task.DeployError):
        task._assert_safe_network("host")
    with pytest.raises(task.DeployError):
        task._assert_safe_volume("/var/run/docker.sock:/var/run/docker.sock")
    with pytest.raises(task.DeployError):
        task._assert_safe_volume("/:/app")
    with pytest.raises(task.DeployError):
        task._assert_safe_volume("C:\\:C:\\app")
    with pytest.raises(task.DeployError):
        task._assert_safe_volume("/root/.ssh:/root/.ssh")
    with pytest.raises(task.DeployError):
        task._assert_safe_volume("/etc/ssh:/etc/ssh")
    assert "--label" in " ".join(task._assert_safe_extra_args('--label team=a'))


def test_docker_build_rejects_host_network_extra_args():
    task = _load("docker-build")
    with pytest.raises(task.BuildError):
        task._assert_safe_extra_args("--network=host")
    with pytest.raises(task.BuildError):
        task._assert_safe_extra_args("--privileged")
    with pytest.raises(task.BuildError):
        task._assert_safe_extra_args("-f /etc/passwd")
    with pytest.raises(task.BuildError):
        task._assert_safe_extra_args("--build-context secret=/etc")
    with pytest.raises(task.BuildError):
        task._assert_safe_extra_args("--network=container:other")
    assert "--label" in " ".join(task._assert_safe_extra_args("--label a=b"))


def test_docker_build_context_stays_in_workspace(tmp_path):
    """构建上下文不能指到工作区外，否则会把宿主机文件打进镜像。"""
    task = _load("docker-build")
    ws = tmp_path / "job"
    src = ws / "src"
    src.mkdir(parents=True)
    (src / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    ctx = task._under_workspace(ws, src / ".", what="构建上下文")
    assert ctx == src.resolve()
    with pytest.raises(task.BuildError):
        task._under_workspace(ws, tmp_path / "outside", what="构建上下文")
    with pytest.raises(task.BuildError):
        task._under_workspace(ws, src / ".." / ".." / "outside", what="Dockerfile")


def test_docker_finds_binary_outside_path(tmp_path, monkeypatch):
    """snap / Program Files 不在服务 PATH 里时，仍应找到 docker 客户端。"""
    import os

    for plugin in ("docker-build", "docker-deploy", "docker-compile"):
        task = _load(plugin)
        name = "docker.exe" if os.name == "nt" else "docker"
        fake = tmp_path / name
        fake.write_bytes(b"")
        if os.name != "nt":
            fake.chmod(0o755)
        monkeypatch.setattr(task.shutil, "which", lambda *_a, **_k: None)
        monkeypatch.setattr(task, "_docker_bin_dirs", lambda: [tmp_path])
        assert Path(task.find_docker()).name == name


def test_docker_missing_explains_install(monkeypatch):
    """本机没有 docker 时，报错要能看懂该装什么、PATH 为什么看不见。"""
    task = _load("docker-build")
    monkeypatch.setattr(task.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(task, "_docker_bin_dirs", lambda: [])
    with pytest.raises(task.BuildError) as exc:
        task.find_docker()
    assert "docker" in str(exc.value).lower()
    assert "PATH" in str(exc.value) or "snap" in str(exc.value)


def test_docker_compile_rejects_host_volumes_and_privileged():
    """容器编译只允许命名缓存卷；-v 宿主机路径和 --privileged 不能从 extraArgs 进来。"""
    task = _load("docker-compile")
    assert task._assert_named_volume("npm_cache:/root/.npm") == "npm_cache:/root/.npm"
    assert task._assert_named_volume("maven_cache:/root/.m2") == "maven_cache:/root/.m2"
    assert task._assert_named_volume("pnpm_store:/root/.local/share/pnpm/store") == (
        "pnpm_store:/root/.local/share/pnpm/store"
    )
    with pytest.raises(task.CompileError):
        task._assert_named_volume("/var/run/docker.sock:/var/run/docker.sock")
    with pytest.raises(task.CompileError):
        task._assert_named_volume("/data/cache:/root/.npm")
    with pytest.raises(task.CompileError):
        task._assert_named_volume("./npm:/root/.npm")
    with pytest.raises(task.CompileError):
        task._assert_safe_extra_args("--privileged")
    with pytest.raises(task.CompileError):
        task._assert_safe_extra_args("-v /etc:/code")
    with pytest.raises(task.CompileError):
        task._assert_safe_extra_args("--network=host")
    assert "--memory=4g" in " ".join(task._assert_safe_extra_args("--memory=4g --cpus=2"))


def test_docker_compile_default_resource_limits():
    """没写 extraArgs 时必须带上内存和 CPU 上限，已经写了则不重复。"""
    task = _load("docker-compile")
    defaults = task._with_default_resource_limits([])
    assert "--memory" in defaults and "4g" in defaults
    assert "--cpus" in defaults and "2" in defaults
    custom = task._with_default_resource_limits(["--memory=8g", "--cpus=4"])
    joined = " ".join(custom)
    assert "--memory=8g" in joined
    assert "--cpus=4" in joined
    assert "4g" not in joined
    short = task._with_default_resource_limits(["-m", "2g"])
    assert "--memory" not in short
    assert "-m" in short and "2g" in short
    assert "--cpus" in short


def test_docker_compile_source_stays_in_workspace(tmp_path):
    """代码子目录不能指到工作区外，否则会把宿主机其它目录挂进编译容器。"""
    task = _load("docker-compile")
    ws = tmp_path / "job"
    src = ws / "src"
    src.mkdir(parents=True)
    assert task._under_workspace(ws, src / ".", what="代码子目录") == src.resolve()
    with pytest.raises(task.CompileError):
        task._under_workspace(ws, tmp_path / "outside", what="代码子目录")
    with pytest.raises(task.CompileError):
        task._under_workspace(ws, src / ".." / ".." / "outside", what="代码子目录")


def test_kubectl_finds_binary_outside_path(tmp_path, monkeypatch):
    """snap / Docker Desktop 的 kubectl 不在服务 PATH 里时仍应找到。"""
    import os

    task = _load("k8s-deploy")
    name = "kubectl.exe" if os.name == "nt" else "kubectl"
    fake = tmp_path / name
    fake.write_bytes(b"")
    if os.name != "nt":
        fake.chmod(0o755)
    monkeypatch.setattr(task.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(task, "_kubectl_bin_dirs", lambda: [tmp_path])
    assert Path(task._find_kubectl("")).name == name


def test_kubectl_missing_explains_install(monkeypatch):
    """本机没有 kubectl 时，报错要提到 snap / 手动路径。"""
    task = _load("k8s-deploy")
    monkeypatch.setattr(task.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(task, "_kubectl_bin_dirs", lambda: [])
    with pytest.raises(task.DeployError) as exc:
        task._find_kubectl("")
    assert "kubectl" in str(exc.value).lower()


def test_archive_rejects_escape_and_collects_under_src(tmp_path):
    """归档 glob 不能逃出工作区；相对 src/ 的 target/*.jar 要能命中。"""
    task = _load("archive-artifact")
    ws = tmp_path / "job"
    src = ws / "src"
    target = src / "target"
    target.mkdir(parents=True)
    jar = target / "app.jar"
    jar.write_bytes(b"jar")
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"x")

    files = task.collect_files(ws, "target/*.jar")
    assert files == [jar.resolve()]

    with pytest.raises(task.ArchiveError):
        task.collect_files(ws, "../secret.bin")
    with pytest.raises(task.ArchiveError):
        task.collect_files(ws, str(outside))
    with pytest.raises(task.ArchiveError):
        task.collect_files(ws, "no-such/*.jar")


def test_python_exec_rejects_escape_and_writes_inline(tmp_path):
    """脚本文件必须在工作区内；表单内容写到工作区临时文件，不覆盖仓库。"""
    task = _load("python-exec")
    ws = tmp_path / "job"
    src = ws / "src"
    src.mkdir(parents=True)
    script = src / "ok.py"
    script.write_text("print(1)\n", encoding="utf-8")

    assert task.resolve_script(ws, "ok.py", "") == script.resolve()
    with pytest.raises(task.ExecError):
        task.resolve_script(ws, "../ok.py", "")
    with pytest.raises(task.ExecError):
        task.resolve_script(ws, str(Path("/etc/passwd")), "")

    written = task.resolve_script(ws, "", "print('hi')")
    assert written == (ws / "_release_python_exec.py").resolve()
    assert written.read_text(encoding="utf-8") == "print('hi')"


def test_python_finds_binary_outside_path(tmp_path, monkeypatch):
    """pyenv / Program Files 不在服务 PATH 里时仍应找到 python。"""
    import os

    task = _load("python-exec")
    name = "python.exe" if os.name == "nt" else "python3"
    fake = tmp_path / name
    fake.write_bytes(b"")
    if os.name != "nt":
        fake.chmod(0o755)
    monkeypatch.setattr(task.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(task, "_python_bin_dirs", lambda: [tmp_path])
    assert Path(task.find_python()).name == name
