# -*- coding: utf-8 -*-
"""更新 K8s 工作负载的镜像，并登记回滚点。

K8s 自己就记着每次变更的 revision，回滚不需要重新构建也不需要旧镜像地址——
rollout undo 到部署前的那个 revision 即可。所以部署前先把当前 revision 记下来。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_atom_sdk as sdk  # noqa: E402


class DeployError(Exception):
    pass


# kubeconfig 是整个集群的凭证，绝不能进部署记录：记录的 payload 是明文存进平台
# 数据库的，一次库备份泄露就等于集群被接管。image 也去掉——回滚靠的是
# rollout undo 到旧 revision，不需要镜像地址。
#
# 代价是回滚时拿不到 kubeconfig，只能用构建机上的默认配置。这比把集群凭证
# 铺在库里可接受得多，而且下面会把这件事在日志和摘要里说清楚。
_SECRET_INPUT_KEYS = ("kubeconfig",)


def _undo_with(inp: dict, previous: str) -> tuple[dict, bool]:
    """回滚时原样回填的参数，以及是否因为去掉凭证而需要提醒。"""
    dropped = bool(str(inp.get("kubeconfig") or "").strip())
    out = {
        k: v
        for k, v in inp.items()
        if not str(k).startswith("_") and k != "image" and k not in _SECRET_INPUT_KEYS
    }
    out["toRevision"] = previous
    return out, dropped


def _timeout_input(inp: dict) -> int:
    """滚动等待时长，封顶 1 小时。

    不封的话填个 999999 就变成 kubectl --timeout=999999s，这台构建机的执行槽
    会被占上十几天，等于从队列里消失。
    """
    raw = inp.get("timeout")
    if raw is None or str(raw).strip() == "":
        return 300
    try:
        value = int(str(raw).strip())
    except ValueError:
        sdk.log.warning(f"timeout 的值「{raw}」不是整数，按默认 300 秒处理")
        return 300
    if value <= 0:
        return 300
    if value > 3600:
        sdk.log.warning(f"timeout={value} 过大，按上限 3600 秒处理")
        return 3600
    return value


def _kubectl_bin_dirs() -> list[Path]:
    """本机 kubectl 可能在的目录。

    官方 apt 包在 /usr/bin。snap、Docker Desktop 自带的 kubectl 在 /snap/bin
    或 Program Files 下，服务进程 PATH 里没有，shutil.which 会判成没装。
    """
    home = Path.home() if hasattr(Path, "home") else Path(os.path.expanduser("~"))
    dirs = [
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/snap/bin"),
        Path("/opt/homebrew/bin"),
        home / ".local/bin",
        Path(r"C:\Program Files\Kubernetes\Client\bin"),
        Path(r"C:\Program Files\Docker\Docker\resources\bin"),
    ]
    raw = (os.environ.get("KUBECTL_BIN") or "").strip()
    if raw:
        p = Path(raw)
        dirs.insert(0, p.parent if p.is_file() else p)
    return dirs


def _which(names: list[str], extra_dirs: list[Path]) -> str | None:
    """先 PATH，再额外目录。Windows 上 .exe 不要求 Unix 执行位。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for folder in extra_dirs:
        if not folder.is_dir():
            continue
        for name in names:
            candidate = folder / name
            if not candidate.is_file():
                continue
            if os.name == "nt" or os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _find_kubectl(configured: str) -> str:
    """定位 kubectl。步骤里填了路径就用它，否则 PATH + 常见安装目录。"""
    if configured.strip():
        if not os.path.isfile(configured.strip()):
            raise DeployError(f"指定的 kubectl 不存在：{configured}")
        return configured.strip()
    names = ["kubectl.exe", "kubectl"] if os.name == "nt" else ["kubectl"]
    found = _which(names, _kubectl_bin_dirs())
    if found:
        return found
    raise DeployError(
        "构建机上找不到 kubectl。请安装 kubectl，或把可执行文件路径填进步骤的「kubectl 路径」。"
        "snap 安装常见位置是 /snap/bin，Docker Desktop 会带一份在 Program Files 下。"
    )


def _base_cmd(kubectl: str, kubeconfig_path: str, context: str, namespace: str) -> list[str]:
    cmd = [kubectl]
    if kubeconfig_path:
        cmd += ["--kubeconfig", kubeconfig_path]
    if context:
        cmd += ["--context", context]
    if namespace:
        cmd += ["-n", namespace]
    return cmd


def _current_revision(base: list[str], ref: str) -> str:
    """工作负载当前的 revision，回滚就是回到它。"""
    code, out = sdk.capture(
        base + ["get", ref, "-o", "jsonpath={.metadata.annotations.deployment\\.kubernetes\\.io/revision}"],
        timeout=60,
    )
    if code != 0:
        return ""
    return out.strip().splitlines()[-1].strip() if out.strip() else ""


def _dump_pods(base: list[str], ref: str) -> None:
    sdk.log.error("当前工作负载状态：")
    sdk.stream(base + ["describe", ref], echo_command=False)


def require_namespace(raw: str) -> str:
    """命名空间必须由步骤写明。空值不会落到 default，避免发错命名空间。"""
    text = (raw or "").strip()
    if not text:
        raise DeployError("必须填写 Kubernetes 命名空间，不会默认落到 default")
    return text


def main() -> int:
    inp = sdk.get_input()
    namespace = require_namespace(str(inp.get("namespace") or ""))
    kind = str(inp.get("workloadKind") or "deployment").strip()
    workload = str(inp.get("workload") or "").strip()
    if not workload:
        raise DeployError("必须填写工作负载名称")
    ref = f"{kind}/{workload}"

    kubectl = _find_kubectl(str(inp.get("kubectlPath") or ""))
    kubeconfig = str(inp.get("kubeconfig") or "")
    timeout = _timeout_input(inp)

    kube_path = ""
    tmp_dir = ""
    try:
        if kubeconfig.strip():
            # 落成临时文件而不是走环境变量：kubectl 只认文件路径。
            # 权限收到 600，跑完立刻删，别让集群凭证留在构建机上
            tmp_dir = tempfile.mkdtemp(prefix="rp-kube-")
            kube_path = os.path.join(tmp_dir, "config")
            with open(kube_path, "w", encoding="utf-8") as f:
                f.write(kubeconfig)
            try:
                os.chmod(kube_path, 0o600)
            except OSError:
                pass

        base = _base_cmd(kubectl, kube_path, str(inp.get("context") or "").strip(), namespace)

        record_id = inp.get("_deployment_record_id")
        to_revision = str(inp.get("toRevision") or "").strip()
        previous = ""
        if record_id and to_revision:
            # 本次执行是回滚
            sdk.log.info(f"回滚 {ref} 到 revision {to_revision}")
            cmd = base + ["rollout", "undo", ref, f"--to-revision={to_revision}"]
            if sdk.stream(cmd) != 0:
                raise DeployError(
                    "回滚失败。可能该 revision 已被 K8s 的历史上限（revisionHistoryLimit）清理，"
                    "这种情况只能手动指定一个历史镜像重新部署"
                )
        else:
            image = str(inp.get("image") or "").strip()
            if not image:
                raise DeployError("必须填写镜像")
            if "${{" in image or "${" in image:
                raise DeployError(f"镜像地址里的变量没有被替换（{image}），请检查流水线变量")
            container = str(inp.get("container") or "").strip() or "*"
            previous = _current_revision(base, ref)
            sdk.log.info(f"{ref} 当前 revision：{previous or '未知（可能是首次部署）'}")

            # 回滚点在动集群之前登记。set image 一执行集群就开始滚新版本，之后
            # rollout status 超时是最常见的失败，而那正是最需要一键回滚的时刻——
            # 把登记放在函数末尾，等于最需要它的时候它一定是空的。
            # 平台按 (发布, 任务, 步骤) 去重覆盖，提前登记不会留下重复记录
            if previous:
                undo_with, dropped_kubeconfig = _undo_with(inp, previous)
                if dropped_kubeconfig:
                    sdk.log.info(
                        "回滚参数里不保存 kubeconfig（集群凭证不入库），"
                        "回滚时会使用构建机上的默认 kubeconfig"
                    )
                sdk.report_deployment(
                    kind=sdk.KIND_K8S,
                    target=f"{namespace}/{ref}",
                    payload={"undo_with": undo_with, "previous_revision": previous},
                    summary=f"回滚到 revision {previous}",
                )
            else:
                sdk.log.warning(
                    "没读到部署前的 revision（多半是首次部署），无法登记回滚点"
                )

            sdk.log.info(f"更新镜像：{container}={image}")
            if sdk.stream(base + ["set", "image", ref, f"{container}={image}"]) != 0:
                raise DeployError("更新镜像失败，请确认工作负载和容器名是否正确")

        sdk.log.info(f"等待滚动完成（最长 {timeout} 秒）...")
        if sdk.stream(base + ["rollout", "status", ref, f"--timeout={timeout}s"]) != 0:
            sdk.log.error("滚动更新没有在超时时间内完成，新版本可能起不来")
            _dump_pods(base, ref)
            if previous:
                sdk.log.error(f"可在平台上对本次发布执行回滚，回到 revision {previous}")
            return 1

        if record_id:
            sdk.report_undone(int(record_id))

        sdk.log.info(f"{ref} 已更新完成")
        return 0
    finally:
        # 整棵删掉，不要只在「文件确实建成了」时才清：写 kubeconfig 中途失败的话
        # 原来的写法会把临时目录留在构建机上，里面可能已经有半份集群凭证了
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeployError as e:
        sdk.log.error(str(e))
        sys.exit(1)
