"""插件执行引擎（本地演示版）。

生产环境由构建机 Agent（Go 单二进制，拉模式）领取任务执行；
这里提供一个本地执行器，让插件「真的能跑起来」用于演示：
  - shell-exec / bat-exec：真用 subprocess 执行命令
  - 其余插件：模拟执行并输出结构化日志

每个执行器返回 (success: bool, logs: list[str])。
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable

# 执行器签名：接收 step 的 with 参数，返回 (成功, 日志列表)
Executor = Callable[[dict[str, Any]], tuple[bool, list[str]]]


def _run_command(cmd: str, timeout: int = 30) -> tuple[bool, list[str]]:
    """真执行 shell 命令（Linux/Mac 用 bash，Windows 用 cmd）。"""
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",  # Windows 上 GBK/UTF-8 混合输出避免解码崩溃
        )
        logs = [f"$ {cmd}"]
        if (r.stdout or "").strip():
            logs.append((r.stdout or "").rstrip())
        if (r.stderr or "").strip():
            logs.append((r.stderr or "").rstrip())
        logs.append(f"(退出码 {r.returncode})")
        return r.returncode == 0, logs
    except subprocess.TimeoutExpired:
        return False, [f"$ {cmd}", f"❌ 执行超时（>{timeout}s）"]
    except Exception as e:  # noqa: BLE001
        return False, [f"$ {cmd}", f"❌ 执行异常：{e}"]


def exec_git_checkout(params: dict) -> tuple[bool, list[str]]:
    repo = params.get("repoName") or params.get("repo") or "unknown-repo"
    ref = params.get("ref") or params.get("branch") or "master"
    strategy = params.get("strategy") or "REVERT_UPDATE"
    logs = [
        f"→ 代码库：{repo}",
        f"→ 分支/TAG/COMMIT：{ref}",
        f"→ 拉取策略：{strategy}",
        f"✓ 已检出 {repo}@{ref} 到工作空间",
    ]
    return True, logs


def exec_maven_build(params: dict) -> tuple[bool, list[str]]:
    goal = params.get("goal") or "package"
    skip = params.get("skipTests")
    cmd = f"mvn {goal}" + (" -DskipTests" if skip else "")
    logs = [f"$ {cmd}"]
    logs.append("✓ 编译打包完成，产出 target/*.jar")
    return True, logs


def exec_docker_build(params: dict) -> tuple[bool, list[str]]:
    image = params.get("imageName") or "app"
    tag = params.get("imageTag") or "latest"
    logs = [f"$ docker build -t {image}:{tag} .", f"✓ 镜像 {image}:{tag} 构建完成"]
    return True, logs


def exec_docker_compile(params: dict) -> tuple[bool, list[str]]:
    """本地演示：只打印将要 docker run 的基础镜像和编译命令。"""
    image = params.get("image") or "toolchain"
    command = params.get("command") or "build"
    toolchain = params.get("toolchain") or "custom"
    logs = [
        f"→ 工具链：{toolchain}",
        f"$ docker run --rm -v $RELEASE_SRC:/code -w /code {image} {command!r}",
        "✓ 容器内编译完成，产物已写回工作区",
    ]
    return True, logs


def exec_archive_artifact(params: dict) -> tuple[bool, list[str]]:
    src = params.get("sourcePath") or "target/*"
    name = params.get("artifactName") or "artifact"
    logs = [f"→ 归档 {src} → 制品 {name}", "✓ 制品已上传到对象存储"]
    return True, logs


def exec_k8s_deploy(params: dict) -> tuple[bool, list[str]]:
    ns = str(params.get("namespace") or "").strip()
    if not ns:
        return False, ["必须填写 Kubernetes 命名空间，不会默认落到 default"]
    strategy = params.get("strategy") or "rolling"
    logs = [
        f"$ kubectl apply -f manifest -n {ns}",
        f"→ 发布策略：{strategy}",
        f"✓ 已部署到命名空间 {ns}",
    ]
    return True, logs


def exec_ssh_deploy(params: dict) -> tuple[bool, list[str]]:
    """本地演示占位。真实传输由构建机上的 plugins/ssh-deploy 执行。"""
    host = params.get("host") or "unknown"
    logs = [f"→ SSH 到 {host}", "本地演示不真正 scp，请在构建机上跑 ssh-deploy 插件"]
    return True, logs


def exec_shell(params: dict) -> tuple[bool, list[str]]:
    content = params.get("content") or params.get("script") or ""
    if not content.strip():
        return True, ["(空脚本，跳过执行)"]
    return _run_command(content)


def exec_cron(params: dict) -> tuple[bool, list[str]]:
    cron = params.get("cron") or "* * * * *"
    return True, [f"→ 定时表达式：{cron}", "✓ 触发器已注册"]


def exec_notify(params: dict) -> tuple[bool, list[str]]:
    to = params.get("recipients") or "—"
    subject = params.get("subject") or "(无主题)"
    logs = [f"→ 发送通知给：{to}", f"→ 主题：{subject}", "✓ 通知已推送（IM/邮件）"]
    return True, logs


def exec_manual(params: dict) -> tuple[bool, list[str]]:
    return True, ["✓ 手动触发"]


# 插件名 → 执行器映射
PLUGIN_EXECUTORS: dict[str, Executor] = {
    "git-checkout": exec_git_checkout,
    "maven-build": exec_maven_build,
    "docker-build": exec_docker_build,
    "docker-compile": exec_docker_compile,
    "archive-artifact": exec_archive_artifact,
    "k8s-deploy": exec_k8s_deploy,
    "ssh-deploy": exec_ssh_deploy,
    "shell-exec": exec_shell,
    "bat-exec": exec_shell,          # Windows 上 bat 同样用 subprocess
    "cron-trigger": exec_cron,
    "manual-trigger": exec_manual,
    "notify-im": exec_notify,
    # 尚未注册的插件默认"模拟成功"
}


def execute_step(plugin: str, params: dict[str, Any]) -> tuple[bool, list[str]]:
    """执行单个步骤，返回 (成功, 日志)。"""
    handler = PLUGIN_EXECUTORS.get(plugin)
    if handler is None:
        return True, [f"[{plugin}] 未注册执行器（本地演示：跳过，模拟成功）"]
    try:
        return handler(params or {})
    except Exception as e:  # noqa: BLE001
        return False, [f"❌ 插件 {plugin} 执行异常：{e}"]
