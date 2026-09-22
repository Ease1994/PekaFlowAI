"""构建任务服务（Agent 拉模式的任务队列）。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime


from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.agent.log_store import LogStore, create_log_store
from app.modules.agent.models import BuildAgent, BuildTask
from app.modules.agent.presence import agent_is_online
from app.modules.settings import get_all_settings

logger = logging.getLogger(__name__)

# 步骤参数里会被注入解密后的凭证（git repoToken 等）。那份明文只该出现在
# Agent 拉取任务的响应里，任何面向浏览器的接口都必须先抹掉，否则只要有一条
# 流水线的查看权，就能把它用到的仓库凭证读走
_SECRET_HINTS = (
    "token", "password", "passwd", "pwd", "secret", "credential",
    "apikey", "accesskey", "privatekey", "signkey",
)
_MASKED = "******"
# 变量名千奇百怪：API_KEY、access-key、private.key 都是同一个东西。
# 不把分隔符抹平，"apikey" 这条规则就只认 APIKEY、认不出 API_KEY——
# 而真实世界里几乎没人写不带下划线的那种
_SEP = re.compile(r"[^a-z0-9]+")


def _looks_secret(key: str) -> bool:
    flat = _SEP.sub("", key.lower())
    return any(h in flat for h in _SECRET_HINTS)


def mask_secrets(value):
    """递归抹掉参数里的密文字段，保留结构以免前端渲染出错。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _looks_secret(k):
                out[k] = _MASKED if v else v
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


# ============================================================
# 任务生成（发布执行时调用）
# ============================================================
# 渲染完还留在参数里的 ${{...}}。双花括号是平台自己的语法，shell 的变量是单花括号
# ${VAR}，所以这里剩下的一定是「引用了一个不存在的变量」，不会误伤脚本
_LEFTOVER_VAR = re.compile(r"\$\{\{\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*\}\}")


def _collect_leftover(value) -> list[str]:
    if isinstance(value, str):
        return _LEFTOVER_VAR.findall(value)
    if isinstance(value, dict):
        return [n for v in value.values() for n in _collect_leftover(v)]
    if isinstance(value, list):
        return [n for v in value for n in _collect_leftover(v)]
    return []


# 这几个变量来自发布清单。手动执行可在弹窗填写 DEPLOY_MANIFEST；
# 更新日志和提交单号仍只有「发布提交」页会带。单独给指引，避免被引到「去定义变量」
_DEPLOY_REQUEST_VARS = {"DEPLOY_MANIFEST", "DEPLOY_CHANGELOG", "DEPLOY_REQUEST_ID"}


def _assert_no_leftover_vars(pipeline_name: str, step_label: str, step_with: dict) -> None:
    """步骤参数里引用了不存在的变量就别往下发了。

    原来这种参数会原样发给插件：msbuild 会去编译一个叫 `${{FOO}}/App.sln` 的文件、
    file-transfer 会往一个带花括号的目录发文件。只有四个插件自己查了占位符，其余的
    要么跑出莫名其妙的错，要么干脆「成功」了但产物是错的。
    """
    missing = sorted(set(_collect_leftover(step_with)))
    if not missing:
        return
    names = "、".join(f"${{{{{n}}}}}" for n in missing)
    if set(missing) & _DEPLOY_REQUEST_VARS:
        raise BizException.bad_request(
            f"流水线「{pipeline_name}」的步骤「{step_label}」用到了 {names}，"
            "这些值来自发布清单。请在发起发布时填写，或从「发布提交」页面发起，"
            "或把参数值直接写死在步骤里。"
        )
    raise BizException.bad_request(
        f"流水线「{pipeline_name}」的步骤「{step_label}」引用了未定义的变量 {names}。"
        "请在流水线的「变量」里定义它，或在执行时通过启动参数传入；"
        "若本意就是 shell 变量，请改成单花括号写法 ${VAR}。"
    )


def create_tasks_for_release(
    db: Session, release_id: int, pipeline_id: int, stages: list, variables: list = None
) -> list[BuildTask]:
    """把流水线的每个 Job 拆成一个构建任务（Agent 执行单元）。

    variables 为流水线级全局变量，注入到每个任务（供 Job 内步骤引用）。
    对 git-checkout 步骤，把 repo 名字解析成完整 URL（查 Repository 表），注入 with.repoUrl；
    若仓库关联了凭证（credential_id），解密后注入 with.repoToken（供 Agent 带认证拉代码）。
    **若 release 有 source_ref，注入 with.ref**（Rebuild 关键：用发布时 commit/tag 重建）。
    """
    from app.core.security import decrypt
    from app.modules.credential.models import Credential
    from app.modules.credential.service import docker_login, inject_registry_login, inject_ssh_login
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.pipeline.service import ensure_pipeline_workspace_uuid
    from app.modules.pipeline.variables import build_context as build_variable_context
    from app.modules.pipeline.variables import render_any as render_variables
    from app.modules.repository.models import Repository

    # 查 release 的 source_ref（Rebuild 用发布时的 commit/tag）
    release = db.get(Release, release_id)
    pinned_ref = release.source_ref if release else None

    # 工作空间：p-{pipeline.workspace_uuid}，创建流水线时生成并持久化，多次执行复用
    pipeline = db.get(Pipeline, pipeline_id)
    if pipeline is None:
        raise BizException.not_found("流水线")
    ws_key = ensure_pipeline_workspace_uuid(db, pipeline)
    workspace = f"p-{ws_key}"

    # 环境隔离：流水线跑哪套环境，就只能用哪套环境的构建机和节点。
    # 必须全等，绝不能「不是 test 就当 prod」——否则 uat 分组会把包发到生产机上。
    from app.core.env import PLATFORM_PROJECT_CODE, pipeline_env_of
    from app.modules.project.models import Group, Project

    grp = db.get(Group, pipeline.group_id)
    if grp is None:
        raise BizException.bad_request(
            f"流水线「{pipeline.name}」所属环境分组不存在，无法确定该用哪套构建机/节点"
        )
    pipeline_env = pipeline_env_of(grp.type)
    proj = db.get(Project, pipeline.project_id)
    # 平台内置「节点文件下发」跨所有环境，不能拿它的分组 type（固定 prod）去卡测试节点
    platform_push = bool(proj and proj.code == PLATFORM_PROJECT_CODE)

    # 预加载仓库对象（alias/name 都作 key，兼容 seed 旧流水线的 name 引用）
    # 新流水线的 git-checkout 用 alias（group/project），但 seed 老流水线可能用 name
    repo_map: dict[str, Repository] = {}
    for r in db.scalars(select(Repository)).all():
        if r.alias:
            repo_map[r.alias] = r
        if r.name and r.name != r.alias:
            repo_map[r.name] = r

    tasks: list[BuildTask] = []
    vars_payload = [v.model_dump() for v in (variables or [])]
    # 执行时填写的参数覆盖默认值（手动执行的参数面板、子流水线启动参数走同一条路）
    try:
        overrides = json.loads((release.run_params_json if release else None) or "{}")
    except json.JSONDecodeError:
        overrides = {}
    if not isinstance(overrides, dict):
        overrides = {}

    # 变量上下文：自定义变量 + 系统变量，供下面渲染步骤参数
    var_values = build_variable_context(
        db, release, pipeline, variables or [], overrides
    )
    for item in vars_payload:
        name = item.get("name")
        if name in var_values:
            item["default_value"] = var_values[name]
            item["value"] = var_values[name]
    for stage in stages:
        for job in (stage.jobs or []):
            steps_payload = []
            for s in (job.steps or []):
                step_with = dict(s.with_ or {})
                # git-checkout：按别名查仓库，注入 repoUrl + repoToken（凭证）
                if s.plugin == "git-checkout":
                    # repoName 优先：新版 UI 的下拉把选中值存到 repoName（alias）
                    # 老 seed 流水线可能残留 repo 字段，导致取到旧的 order-service 而非用户新选的仓库
                    repo_alias = step_with.get("repoName") or step_with.get("repo")
                    repo = repo_map.get(repo_alias or "")
                    if repo is None and not step_with.get("repoUrl"):
                        # 解析不到就必须停在这里。放过去的话插件拿不到 URL 会建个空 src 目录
                        # 然后报成功，后面照常编译打包，最后把一个空包发到线上——全程没有一处报错
                        raise BizException.bad_request(
                            f"流水线「{pipeline.name}」的「拉取代码」步骤找不到代码库"
                            f"「{repo_alias or '（未选择）'}」：可能是代码库被删了、改了别名，"
                            "或这条流水线还没选代码库。请到流水线编辑页重新选择代码库后再执行。"
                        )
                    if repo is not None:
                        step_with.setdefault("repoUrl", repo.url)
                        step_with.setdefault("repoAlias", repo.alias)
                        step_with.setdefault("repoName", repo.name)
                        # Rebuild 关键：若 release 有 source_ref，覆盖 yaml 里的 ref
                        # 这样 Agent 拉的就是那一次提交的代码，不是最新
                        if pinned_ref:
                            step_with["ref"] = pinned_ref
                            step_with["branch"] = pinned_ref
                        # 关联凭证 → 解密 token 注入（供 Agent 认证）
                        if repo.credential_id:
                            cred = db.get(Credential, repo.credential_id)
                            if cred is not None:
                                try:
                                    plain = decrypt(cred.ciphertext, cred.iv)
                                except Exception as exc:  # noqa: BLE001
                                    # 塞空 token 只会让私有库在 Agent 上报一句看不懂的 git 认证错误，
                                    # 真正的原因（密钥换了、密文坏了）反而被埋掉
                                    raise BizException.bad_request(
                                        f"代码库「{repo.alias or repo.name}」关联的凭证解密失败，"
                                        "无法拉取私有仓库。请到「凭证管理」重新录入该凭证。"
                                    ) from exc
                                user, secret = docker_login(plain, cred.type or "token")
                                if (cred.type or "token").strip() == "token":
                                    secret = secret or plain
                                    user = user or "oauth2"
                                if not secret:
                                    raise BizException.bad_request(
                                        f"代码库「{repo.alias or repo.name}」关联的凭证没有密码或 Token，"
                                        "无法拉取私有仓库。请到「凭证管理」重新录入。"
                                    )
                                step_with["repoUser"] = user or "oauth2"
                                step_with["repoToken"] = secret
                # 变量替换：仓库地址注入完再统一渲染
                step_with = render_variables(step_with, var_values)
                _assert_no_leftover_vars(pipeline.name, s.name or s.plugin, step_with)
                # 镜像仓库账号在变量检查之后注入，避免密码里的 ${{ 被当成未替换变量
                inject_registry_login(db, step_with, project_id=pipeline.project_id)
                if s.plugin == "ssh-deploy":
                    inject_ssh_login(db, step_with, project_id=pipeline.project_id)
                # 老编排没写 keepBackups 时 Java 会当成 0 而永不清理；缺省按插件默认 20 注入
                if s.plugin == "file-transfer":
                    step_with["keepBackups"] = resolve_keep_backups(step_with.get("keepBackups"))
                trial_pkg = step_with.pop("_trial_package", None)
                # 带上用户起的步骤名：同一条流水线里 iis-control 可能出现好几次，
                # 执行页只显示插件名的话，根本分不清停的是哪个池、起的是哪个站点
                item = {"name": s.name or "", "plugin": s.plugin, "with": step_with}
                if isinstance(trial_pkg, dict):
                    from app.modules.store import plugin_service
                    pkg = plugin_service.sanitize_package_meta(trial_pkg)
                    if pkg is not None:
                        item["package"] = pkg
                steps_payload.append(item)
            # 平台步骤（run-pipeline）与构建机步骤拆成串行任务，避免占用 Agent 槽位轮询；
            # 部署步骤按它选的节点再拆一层，各自派给对应节点的 Agent
            prev_task_id = None
            for kind, seg_steps, node_id in _split_job_segments(steps_payload):
                if not seg_steps:
                    continue
                target_id = node_id
                task_env = pipeline_env
                if kind == "platform":
                    agent_tag = "platform"
                elif kind == "node":
                    agent_tag = "node"
                    if platform_push:
                        # 指名节点自己的环境；审批闸门已经按节点 env 算过了
                        _assert_node_usable(db, node_id, seg_steps, pipeline_env=None)
                        node = db.get(BuildAgent, node_id)
                        if node is None or not (node.env or "").strip():
                            raise BizException.bad_request(
                                "这台节点没有标明环境，不能接收下发。请到节点管理页标成生产或测试"
                            )
                        task_env = _machine_env(node.env)
                    else:
                        _assert_node_usable(db, node_id, seg_steps, pipeline_env)
                else:
                    agent_tag = job.agent or "linux"
                    # Job 选了具体构建机时，YAML 里记成 agent:<id>。直接落到
                    # target_agent_id，不必再借道标签——标签只是"哪一类机器"，
                    # 表达不了"就这一台"
                    if agent_tag.startswith("agent:"):
                        target_id = _resolve_builder(db, agent_tag, pipeline_env)
                    else:
                        # any/os 标签的构建：建任务时锁不定具体机器，先确认这套环境
                        # 有构建机存在，免得任务默默卡死等一台永远不来的机器
                        _assert_env_has_builder(db, pipeline_env)
                task = BuildTask(
                    release_id=release_id,
                    pipeline_id=pipeline_id,
                    stage_name=stage.name,
                    job_id=job.id or "",
                    job_name=job.name or job.agent,
                    agent_tag=agent_tag,
                    # 平台编排步骤（run-pipeline）不落具体机器，env 无意义；构建/部署段
                    # 带上流水线环境，领取时据此把生产任务挡在测试机之外
                    env=task_env,
                    target_agent_id=target_id,
                    depends_on_task_id=prev_task_id,
                    steps_json=json.dumps(seg_steps, ensure_ascii=False),
                    # 下发完整的变量表（自定义 + BK_CI_* 系统变量）而不是变量定义列表：
                    # 步骤参数在上面已经渲染过了，Agent 拿这个是为了另一类用途——
                    # 比如节点要按项目名/流水线名分层建备份目录，而那些名字只在系统变量里
                    variables_json=json.dumps(var_values, ensure_ascii=False),
                    workspace=workspace,
                    status="pending",
                )
                db.add(task)
                db.flush()
                prev_task_id = task.id
                tasks.append(task)
    db.commit()
    for t in tasks:
        db.refresh(t)
    return tasks


PLATFORM_PLUGINS = {"run-pipeline"}
# 在部署节点上执行的插件：节点由步骤参数里的 node 指定，而不是 Job 的 agent 标签。
# 服务启停按平台分两个：Windows 用 iis-control，Linux 用 service-control
NODE_PLUGINS = {"file-transfer", "iis-control", "service-control", "rollback-files"}
# 与 file-transfer 插件 schema 默认值一致。YAML 没写时注入这个数，避免 Java 把空值当成 0 永不清理。
DEFAULT_KEEP_BACKUPS = 20
# 单条流水线在生产机上允许保留的备份份数上限，防止编排里填 99999 把盘写满。
MAX_KEEP_BACKUPS = 50


def resolve_keep_backups(raw) -> int:
    """节点备份保留份数：缺省/非法用 20；0 表示只留本次，按 1 处理；超过上限按上限。"""
    if raw is None or raw == "":
        return DEFAULT_KEEP_BACKUPS
    try:
        keep = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_KEEP_BACKUPS
    if keep <= 0:
        return 1
    if keep > MAX_KEEP_BACKUPS:
        return MAX_KEEP_BACKUPS
    return keep


def _env_label(env: str) -> str:
    from app.core.env import env_label

    return env_label(env)


def _machine_env(raw: str | None) -> str:
    from app.core.env import machine_env

    return machine_env(raw)


def _builder_accept_tags(agent: BuildAgent, agent_tags: set[str]) -> list[str]:
    """这台构建机认领的任务标签：自身 tags、操作系统，以及 any。

    领取必须在 SQL 里按这些标签筛。只按环境 LIMIT 50 时，另一种操作系统上
    积压的任务会占满窗口，这台机器领不到自己能跑的活。
    """
    tags = {t for t in agent_tags if t}
    tags.add("any")
    os_name = (agent.os or "").strip()
    if os_name:
        tags.add(os_name)
    return list(tags)


def _assert_env_match(kind: str, name: str, resource_env: str, pipeline_env: str) -> None:
    """环境隔离：流水线只能落到同环境码的构建机/节点。

    机器没标环境不能匹配任何流水线。宁可在建任务时就报错，也不要把测试包发到生产机。
    """
    r = _machine_env(resource_env)
    p = (pipeline_env or "").strip().lower()
    if not r:
        raise BizException.bad_request(
            f"{kind}「{name}」没有标明环境，不能用于任何流水线。"
            f"请到{kind}管理页把它标成{_env_label(p) if p else '生产或测试'}"
        )
    if not p:
        raise BizException.bad_request("流水线所属环境分组没有环境码，无法隔离构建机和节点")
    if r != p:
        raise BizException.bad_request(
            f"环境不匹配：{_env_label(p)}流水线不能用{_env_label(r)}{kind}「{name}」。"
            f"请改选{_env_label(p)}{kind}，或在{kind}管理页把它标成{_env_label(p)}环境"
        )


def _assert_node_usable(
    db: Session, node_id: int | None, steps: list[dict], pipeline_env: str | None = "prod"
) -> None:
    """提交前确认这条部署段现在就能跑。

    节点必须真实存在、角色是 node、此刻在线（心跳未超时），环境和流水线一致，
    启停插件对得上操作系统，站点目录落在节点上报的白名单里。
    部署任务指名具体节点，离线就永远没人领，页面只会停在「正在加载日志」。
    这些如果拖到节点已经停了服务才失败，生产会停在半路，比一开始拒绝难收拾得多。
    """
    plugins = "、".join(dict.fromkeys(s.get("plugin", "") for s in steps))
    if not node_id:
        raise BizException.bad_request(
            f"步骤「{plugins}」没有选择部署节点，请在步骤配置里选择目标节点后重试"
        )
    node = db.get(BuildAgent, node_id)
    if node is None:
        raise BizException.bad_request(f"步骤「{plugins}」选择的部署节点不存在（可能已被删除）")
    if (node.role or "builder") != "node":
        raise BizException.bad_request(
            f"步骤「{plugins}」选择的「{node.name}」是构建机而不是部署节点"
        )
    # 指名节点离线就永远领不走任务。构建机可以等领取重试，节点不行。
    if not agent_is_online(node):
        action = plugins or "部署步骤"
        raise BizException.bad_request(
            f"部署节点「{node.name}」当前离线，无法执行「{action}」。"
            "请先在节点管理确认 Agent 已启动并心跳正常"
        )
    # 平台节点下发不拿流水线分组去卡节点环境；用户流水线必须全等
    if pipeline_env is not None:
        _assert_env_match("部署节点", node.name, node.env, pipeline_env)
    from app.modules.ai.nodepush import check_target_dir

    is_windows = (node.os or "").lower() == "windows"
    for s in steps:
        plugin = s.get("plugin", "")
        if plugin == "iis-control" and not is_windows:
            raise BizException.bad_request(
                f"「{node.name}」是 {node.os or '非 Windows'} 节点，用不了「IIS 启停控制」，"
                "请改用「服务启停控制（Linux）」"
            )
        if plugin == "service-control" and is_windows:
            raise BizException.bad_request(
                f"「{node.name}」是 Windows 节点，用不了「服务启停控制（Linux）」，"
                "请改用「IIS 启停控制」"
            )
        if plugin == "file-transfer":
            check_target_dir(node, str((s.get("with") or {}).get("targetDir") or ""))


def _resolve_builder(db: Session, agent_tag: str, pipeline_env: str = "prod") -> int:
    """把 Job 的 agent:<id> 解析成构建机 ID，顺便挡住选了机器又把机器删了的情况。

    宁可在这里报错，也不要建出一个谁都领不走、永远卡在 pending 的任务。
    """
    try:
        builder_id = int(agent_tag.split(":", 1)[1])
    except (IndexError, ValueError):
        raise BizException.bad_request(f"Job 的构建资源「{agent_tag}」格式不对") from None
    builder = db.get(BuildAgent, builder_id)
    if builder is None:
        raise BizException.bad_request(
            "Job 指定的构建机不存在（可能已被删除），请重新编辑流水线选择构建资源"
        )
    if (builder.role or "builder") != "builder":
        raise BizException.bad_request(f"Job 指定的「{builder.name}」是部署节点，不能用来跑构建")
    _assert_env_match("构建机", builder.name, builder.env, pipeline_env)
    return builder_id


def _assert_env_has_builder(db: Session, pipeline_env: str) -> None:
    """`any`/标签构建在建任务时无法锁定具体机器，但至少确认这套环境有构建机存在。

    否则任务会默默卡在 pending 等一台永远不会出现的机器，用户完全看不出原因。
    只查「存不存在」，离线与否交给正常的领取重试。
    """
    env = (pipeline_env or "").strip().lower()
    role_ok = (BuildAgent.role == "builder") | (BuildAgent.role.is_(None))
    env_ok = BuildAgent.env == env
    exists = db.scalar(
        select(BuildAgent.id).where(role_ok, env_ok).limit(1)
    )
    if not exists:
        raise BizException.bad_request(
            f"没有{_env_label(env)}环境的构建机，{_env_label(env)}流水线无法构建。"
            f"请先在构建机管理页把一台构建机标成{_env_label(env)}环境"
        )


def _step_node_id(step: dict) -> int | None:
    raw = (step.get("with") or {}).get("node")
    try:
        return int(raw) or None
    except (TypeError, ValueError):
        return None


def _split_job_segments(steps: list[dict]) -> list[tuple[str, list[dict], int | None]]:
    """把 Job 内步骤切成串行执行的段：(段类型, 步骤, 指定节点)。

    构建机步骤连续的合成一段；每个平台插件单独一段；部署步骤按节点分段，
    连着发往同一节点的步骤合成一段（停应用池、发文件、启应用池就是同一段）。
    """
    segments: list[tuple[str, list[dict], int | None]] = []
    buf: list[dict] = []
    buf_kind = "agent"
    buf_node: int | None = None

    def flush() -> None:
        nonlocal buf
        if buf:
            segments.append((buf_kind, buf, buf_node))
            buf = []

    for step in steps:
        plugin = (step.get("plugin") or "") if isinstance(step, dict) else ""
        if plugin in PLATFORM_PLUGINS:
            flush()
            segments.append(("platform", [step], None))
            buf_kind, buf_node = "agent", None
            continue
        if plugin in NODE_PLUGINS:
            node_id = _step_node_id(step)
            if buf_kind != "node" or buf_node != node_id:
                flush()
                buf_kind, buf_node = "node", node_id
            buf.append(step)
            continue
        if buf_kind != "agent":
            flush()
            buf_kind, buf_node = "agent", None
        buf.append(step)
    flush()
    return segments


# ============================================================
# Agent 拉取 / 上报
# ============================================================
def fetch_task(db: Session, agent_id: int) -> BuildTask | None:
    """Agent 拉取一个待执行任务（按标签匹配），标记为 assigned。

    关键：
    - 跳过已终止 release 的任务
    - 按角色/节点 ID/环境/构建机标签在 SQL 里先筛，再 LIMIT。只取最老 50 条再 Python 过滤时，
      积压的构建任务会把指名给这台节点的部署任务挤出窗口，异构构建机也会互相饿死。
    - MySQL 8：FOR UPDATE SKIP LOCKED 原子领取，避免多 Agent 双派发
    """
    from app.modules.pipeline.models import Release as ReleaseModel

    agent = db.get(BuildAgent, agent_id)
    if agent is None:
        raise BizException.not_found("构建机")

    try:
        agent_tags = set(json.loads(agent.tags or "[]"))
    except json.JSONDecodeError:
        agent_tags = set()

    TERMINAL = ("cancelled", "failed", "success", "rolled_back")
    dialect = db.get_bind().dialect.name
    is_node = (agent.role or "builder") == "node"
    agent_env = _machine_env(agent.env)

    base_stmt = (
        select(BuildTask)
        .join(ReleaseModel, BuildTask.release_id == ReleaseModel.id)
        .where(
            BuildTask.status == "pending",
            ReleaseModel.status.not_in(TERMINAL),
        )
    )
    if is_node:
        # 生产节点只领指名给自己的部署任务，绝不碰构建任务（构建任务带着仓库凭证）
        base_stmt = base_stmt.where(
            BuildTask.target_agent_id == agent_id,
            BuildTask.agent_tag == "node",
        )
    else:
        if not agent_env:
            db.rollback()
            _cancel_orphan_pending_tasks()
            return None
        accept_tags = _builder_accept_tags(agent, agent_tags)
        base_stmt = base_stmt.where(
            BuildTask.agent_tag.notin_(("node", "platform")),
            BuildTask.env == agent_env,
            or_(
                BuildTask.target_agent_id == agent_id,
                and_(
                    BuildTask.target_agent_id.is_(None),
                    BuildTask.agent_tag.in_(accept_tags),
                ),
            ),
        )
    base_stmt = base_stmt.order_by(BuildTask.id).limit(50)
    if dialect == "mysql":
        base_stmt = base_stmt.with_for_update(skip_locked=True)

    rows = list(db.scalars(base_stmt).all())

    for task in rows:
        if not _deps_ready(db, task):
            continue
        if task.target_agent_id:
            matched = task.target_agent_id == agent_id
        elif is_node:
            matched = False
        else:
            matched = task.agent_tag in agent_tags or task.agent_tag in ("any", agent.os)
        if matched:
            # 同一事务内更新，提交后才释放 SKIP LOCKED 行锁
            task.status = "running"
            task.agent_id = agent_id
            task.started_at = datetime.now()
            db.commit()
            db.refresh(task)
            # 孤儿清理放独立会话，避免打断领取事务的行锁
            _cancel_orphan_pending_tasks()
            return task

    db.rollback()
    _cancel_orphan_pending_tasks()
    return None


def _deps_ready(db: Session, task: BuildTask) -> bool:
    if not task.depends_on_task_id:
        return True
    dep = db.get(BuildTask, task.depends_on_task_id)
    return dep is not None and dep.status == "success"


def _cancel_orphan_pending_tasks() -> None:
    """清理已终止 release 上仍 pending 的孤儿任务（独立会话）。"""
    from app.db.session import SessionLocal
    from app.modules.pipeline.models import Release as ReleaseModel

    terminal = ("cancelled", "failed", "success", "rolled_back")
    try:
        with SessionLocal() as sdb:
            orphans = sdb.execute(
                select(BuildTask)
                .join(ReleaseModel, BuildTask.release_id == ReleaseModel.id)
                .where(
                    BuildTask.status == "pending",
                    ReleaseModel.status.in_(terminal),
                )
                .limit(20)
            ).scalars().all()
            if not orphans:
                return
            now = datetime.now()
            for t in orphans:
                t.status = "cancelled"
                t.finished_at = now
            sdb.commit()
    except Exception:  # noqa: BLE001
        pass


MAX_LOG_BATCH = 500
MAX_LOG_LINE_CHARS = 4000
MAX_LOG_BATCH_BYTES = 512 * 1024


def clip_log_batch(lines) -> list[str]:
    """Agent 上报入口的体积闸。行数和总字节都封顶，避免单次请求把 API、SSE 和检索拖死。"""
    if isinstance(lines, str):
        rows = [lines]
    else:
        rows = list(lines or [])
    out: list[str] = []
    total = 0
    for item in rows:
        if len(out) >= MAX_LOG_BATCH:
            break
        text = str(item)
        if len(text) > MAX_LOG_LINE_CHARS:
            text = text[:MAX_LOG_LINE_CHARS] + "…（单行过长已截断）"
        nbytes = len(text.encode("utf-8"))
        if out and total + nbytes > MAX_LOG_BATCH_BYTES:
            break
        total += nbytes
        out.append(text)
    return out


def report_log(db: Session, session_factory, task_id: int, line: str) -> None:
    """Agent 上报一条日志。存储故障不影响发布。"""
    report_log_batch(db, session_factory, task_id, [line])


def report_log_batch(db: Session, session_factory, task_id: int, lines: list[str]) -> None:
    """Agent 批量上报日志。ES/Redis 挂了只丢日志，不写数据库、不让任务失败。"""
    lines = clip_log_batch(lines)
    if not lines:
        return
    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")
    try:
        store: LogStore = create_log_store(session_factory, lambda: get_all_settings(db))
        store.append_batch(task_id, lines)
    except Exception:  # noqa: BLE001
        logger.warning("任务 #%s 日志写入失败，已丢弃本批", task_id, exc_info=True)


def report_step_result(
    db: Session,
    task_id: int,
    *,
    index: int,
    plugin: str,
    status: str,
    started_at_ms: int,
    duration_ms: int,
) -> None:
    """记录单个步骤的状态与耗时（同一步骤会先报 running 再报最终态，按 index 覆盖）。"""
    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")

    try:
        results = json.loads(task.step_results_json or "[]")
        if not isinstance(results, list):
            results = []
    except (TypeError, ValueError):
        results = []

    started_at = (
        datetime.fromtimestamp(started_at_ms / 1000).isoformat() if started_at_ms else None
    )
    item = {
        "index": index,
        "plugin": plugin,
        "status": status,
        "started_at": started_at,
        # running 时 Agent 报的 duration 是 0，页面按「进行中」处理，别显示成 0 秒
        "duration": round(duration_ms / 1000, 1) if duration_ms > 0 else None,
    }
    results = [r for r in results if isinstance(r, dict) and r.get("index") != index]
    results.append(item)
    results.sort(key=lambda r: r.get("index", 0))
    task.step_results_json = json.dumps(results, ensure_ascii=False)
    db.commit()


def complete_task(
    db: Session, task_id: int, success: bool, source_ref: str | None = None
) -> BuildTask:
    """Agent 上报任务完成。

    已经是终态（success/failed/cancelled/timeout）的不再覆盖：平台取消后
    Agent 仍可能跑完补偿启服再报 success/failed，不能把「用户取消」改写成成功。

    失败时级联取消同 release 下所有未完成的任务（步骤失败即终止流水线）：
    - pending/assigned 任务：直接 cancelled（Agent 拉不到）
    - running 任务：状态置 cancelled，节点 Agent 下一轮短轮询会停后续步骤并补偿启服
    - release 状态置 failed，写入 finished_at（审计需要）

    已经 cancelled 的 release 不允许被这次失败再次覆盖（避免状态抖动）。

    source_ref：Agent 在 git-checkout 后上报的真实 commit SHA。
    写入 release.source_ref（空则补写；已有则不覆盖，保留创建发布时 API 解析值 / 先到先得）。
    Rebuild 与「代码变更」区间都依赖该字段。
    """
    from app.modules.pipeline.models import Release

    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")
    if task.status in ("success", "failed", "cancelled", "timeout"):
        logger.info(
            "任务 #%s 已是终态 %s，忽略 Agent 完成上报 success=%s",
            task_id,
            task.status,
            success,
        )
        return task
    task.status = "success" if success else "failed"
    task.finished_at = datetime.now()
    db.flush()  # 先把 task 状态写下去再级联查

    # 回写 commit：拉代码成功后由 Agent 上报，比后端调 GitLab API 更可靠
    if source_ref and success:
        release_for_ref = db.get(Release, task.release_id)
        if release_for_ref is not None and not release_for_ref.source_ref:
            release_for_ref.source_ref = source_ref
            # 同步 snapshot，便于审计
            try:
                snap = json.loads(release_for_ref.snapshot or "{}")
            except Exception:  # noqa: BLE001
                snap = {}
            snap["source_ref"] = source_ref
            release_for_ref.snapshot = json.dumps(snap, ensure_ascii=False)

    if not success and cascade_fail_release(db, task):
        db.commit()
        db.refresh(task)
        from app.modules.notify import on_release_finished

        on_release_finished(task.release_id)
        _kick_log_archive(db, task_id)
        return task
    db.commit()
    db.refresh(task)
    _kick_log_archive(db, task_id)
    return task


def _kick_log_archive(db: Session, task_id: int) -> None:
    try:
        from app.modules.agent.log_store import create_log_store
        from app.modules.settings import get_all_settings

        store = create_log_store(None, lambda: get_all_settings(db))
        flush = getattr(store, "flush_archive", None)
        if callable(flush):
            flush(task_id)
    except Exception:  # noqa: BLE001
        logger.warning("任务 #%s 结束时刷归档失败，Redis 条目靠 TTL 清理", task_id, exc_info=True)


def cascade_fail_release(db: Session, task: BuildTask) -> bool:
    """一个任务失败/超时 → 同 release 下其余未完成任务全部取消，release 置 failed。

    不提交，由调用方决定事务边界。返回是否真的改了 release
    （已经是终态的不动，避免用户主动取消后又被抖回 failed）。
    """
    from app.modules.pipeline.models import Release

    release = db.get(Release, task.release_id)
    if release is None or release.status in ("cancelled", "success", "failed", "rolled_back"):
        return False
    # 含 running：agent 长轮询取消接口能感知到，会把子进程杀掉
    unfinished = db.scalars(
        select(BuildTask).where(
            BuildTask.release_id == task.release_id,
            BuildTask.id != task.id,
            BuildTask.status.in_(["pending", "assigned", "running"]),
        )
    ).all()
    now = datetime.now()
    for t in unfinished:
        t.status = "cancelled"
        t.finished_at = now
    release.status = "failed"
    release.finished_at = now
    # 列表和明细的「失败原因」读的是 error_message。步骤失败时原因只在任务日志里，
    # 这里抽一句写回去，否则页面会显示「没有记录到失败原因」。
    if not (release.error_message or "").strip():
        snippet = failed_task_error_snippet(db, task.id)
        if snippet:
            release.error_message = snippet[:512]
    return True


def failed_task_error_snippet(db: Session, task_id: int) -> str:
    """从失败任务日志抽出一句给人看的原因，供发布记录和列表摘要使用。"""
    from app.db.session import SessionLocal
    from app.modules.ai.followup import pick_error_snippet

    try:
        logs = get_task_logs(db, SessionLocal, task_id)
    except Exception:  # noqa: BLE001
        return ""
    hits = pick_error_snippet(logs or [], max_n=1)
    return (hits[0] if hits else "")[:512]


# Agent 判死阈值：心跳是 Agent 里的独立守护线程，10s 一次，执行长构建期间照常发，
# 所以「连续这么久没心跳」只可能是机器/进程真的没了，不会误伤跑得久的任务
DEAD_AGENT_SECONDS = 600


def _abort_stuck_task(
    db: Session,
    session_factory,
    task: BuildTask,
    log_line: str,
    *,
    status: str,
) -> bool:
    """把一条没人交差的任务收成终态，并级联失败整次发布。

    先写日志再改状态：页面上的失败原因从日志抽，没有这句就只剩转圈。
    日志没写上时直接把本句落到 release.error_message，避免列表空白。
    返回是否改了发布状态（已经是终态的不动）。
    """
    try:
        report_log(db, session_factory, task.id, log_line)
    except Exception:  # noqa: BLE001
        pass
    reason = log_line.replace("[平台] ", "", 1)[:512]
    task.status = status
    task.finished_at = datetime.now()
    db.flush()
    from app.modules.pipeline.models import Release

    release = db.get(Release, task.release_id)
    # 先写下原因：级联时会从日志再抽一句，日志没写上也不能让列表空白
    if release is not None and not (release.error_message or "").strip():
        release.error_message = reason
    changed = cascade_fail_release(db, task)
    db.commit()
    if changed:
        try:
            from app.modules.notify import on_release_finished

            on_release_finished(task.release_id)
        except Exception:  # noqa: BLE001
            pass
    return changed


def reap_dead_agent_tasks(session_factory) -> int:
    """回收没人能交差的任务：失联构建机手上的 running，以及目标节点已离线的部署。

    构建机掉电后手上的 assigned/running 不会有人上报结果；部署任务还指名
    target_agent_id，节点离线时连 pending 都永远领不走。没有这道回收，发布
    一直「执行中」，流水线被占着，页面不报错只转圈。
    """
    from datetime import timedelta

    from sqlalchemy import or_

    reaped = 0
    with session_factory() as db:
        threshold = datetime.now() - timedelta(seconds=DEAD_AGENT_SECONDS)
        builder_tasks = list(
            db.scalars(
                select(BuildTask)
                .join(BuildAgent, BuildTask.agent_id == BuildAgent.id)
                .where(
                    BuildTask.status.in_(("assigned", "running")),
                    BuildTask.agent_tag != "node",
                    or_(
                        BuildAgent.last_heartbeat.is_(None),
                        BuildAgent.last_heartbeat < threshold,
                    ),
                )
                .limit(50)
            ).all()
        )
        for task in builder_tasks:
            if task.status not in ("assigned", "running"):
                continue
            agent = db.get(BuildAgent, task.agent_id)
            name = agent.name if agent is not None else f"#{task.agent_id}"
            _abort_stuck_task(
                db,
                session_factory,
                task,
                f"[平台] 构建机「{name}」已超过 {DEAD_AGENT_SECONDS // 60} 分钟没有心跳，"
                "判定本次执行已中断",
                status="timeout",
            )
            reaped += 1

        # 部署任务指名节点：离线就永远没人领，不能等十分钟心跳判死。
        node_tasks = list(
            db.scalars(
                select(BuildTask).where(
                    BuildTask.agent_tag == "node",
                    BuildTask.status.in_(("pending", "assigned", "running")),
                ).limit(50)
            ).all()
        )
        for task in node_tasks:
            if task.status not in ("pending", "assigned", "running"):
                continue
            node_id = task.target_agent_id or task.agent_id
            node = db.get(BuildAgent, node_id) if node_id else None
            if agent_is_online(node):
                continue
            name = node.name if node is not None else (f"#{node_id}" if node_id else "未指定")
            _abort_stuck_task(
                db,
                session_factory,
                task,
                f"[平台] 部署节点「{name}」当前离线，无法发送文件，本次执行中止",
                status="failed",
            )
            reaped += 1
    return reaped


def get_task_logs(db: Session, session_factory, task_id: int) -> list[str]:
    """读取任务日志（从配置的存储后端）。"""
    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")
    store: LogStore = create_log_store(session_factory, lambda: get_all_settings(db))
    try:
        return store.get(task_id)
    except Exception:  # noqa: BLE001
        logger.warning("读取任务 #%s 日志失败", task_id, exc_info=True)
        return []
