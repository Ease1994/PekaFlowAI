"""部署记录的读写与回滚计划生成。"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.agent.models import BuildAgent
from app.modules.deployment.models import DeploymentRecord
from app.modules.pipeline.models import Release

# kind → 撤销时在节点/构建机上执行的动作
UNDO_PLUGIN = {
    "file-backup": "rollback-files",
    "docker-image": "docker-deploy",
    "k8s-revision": "k8s-deploy",
}

KIND_LABEL = {
    "file-backup": "增量文件发布",
    "docker-image": "容器镜像发布",
    "k8s-revision": "K8s 发布",
}
# Docker tag：字母数字下划线开头，允许点、横线。只换 tag，不改仓库路径。
_IMAGE_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def split_image_ref(image: str) -> tuple[str, str]:
    """把镜像拆成仓库地址和 tag。

    harbor:5000/app:10 里端口冒号在最后一个 / 之前，不能当成 tag。
    没有 tag 时第二段为空。
    """
    text = (image or "").strip()
    if not text:
        return "", ""
    slash = text.rfind("/")
    colon = text.rfind(":")
    if colon > slash:
        return text[:colon], text[colon + 1 :]
    return text, ""


def docker_images_of(row: DeploymentRecord) -> tuple[str, str]:
    """这次发布写上去的镜像，以及默认要回到的上一个镜像。"""
    payload = payload_of(row)
    current = str(payload.get("current_image") or "").strip()
    previous = str(payload.get("previous_image") or "").strip()
    undo = payload.get("undo_with") if isinstance(payload.get("undo_with"), dict) else {}
    if not previous:
        previous = str(undo.get("image") or "").strip()
    if " → " in (row.summary or ""):
        left, _, right = (row.summary or "").partition(" → ")
        if not previous:
            previous = left.strip()
        if not current:
            current = right.strip()
    return current, previous


def replace_image_tag(image: str, tag: str) -> str:
    """只替换镜像 tag，仓库路径保持不变。"""
    repo, _ = split_image_ref(image)
    text = (tag or "").strip()
    if not repo:
        raise BizException.bad_request("没有镜像仓库地址，无法指定回滚版本")
    if not _IMAGE_TAG.fullmatch(text):
        raise BizException.bad_request(
            f"镜像 tag「{text}」不合法。只能用字母、数字、点、下划线和横线，例如 9 或 v1.2.0"
        )
    return f"{repo}:{text}"


def normalize_image_tags(raw) -> dict[int, str]:
    """请求体里的 {记录 id: tag}。空值丢掉，非法 id 直接拒绝。"""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise BizException.bad_request("image_tags 必须是对象")
    out: dict[int, str] = {}
    for key, value in raw.items():
        try:
            record_id = int(key)
        except (TypeError, ValueError) as exc:
            raise BizException.bad_request(f"回滚版本键「{key}」不是部署记录 id") from exc
        tag = str(value or "").strip()
        if tag:
            out[record_id] = tag
    return out


def record_deployment(
    db: Session,
    *,
    release_id: int,
    pipeline_id: int,
    task_id: int,
    agent_id: int | None,
    step_index: int,
    kind: str,
    payload: dict,
    target: str,
    summary: str,
) -> DeploymentRecord:
    """登记一次部署。同一步骤重复上报时覆盖，避免重试留下两条记录。"""
    if kind not in UNDO_PLUGIN:
        raise BizException.bad_request(f"未知的部署类型：{kind}")

    row = db.scalar(
        select(DeploymentRecord).where(
            DeploymentRecord.task_id == task_id,
            DeploymentRecord.step_index == step_index,
        )
    )
    if row is None:
        row = DeploymentRecord(
            release_id=release_id,
            pipeline_id=pipeline_id,
            task_id=task_id,
            step_index=step_index,
        )
        db.add(row)
    row.agent_id = agent_id
    row.kind = kind
    row.payload_json = json.dumps(payload or {}, ensure_ascii=False)
    row.target = (target or "")[:512]
    row.summary = (summary or "")[:512]
    row.undone_at = None
    row.undone_by_release_id = None
    db.commit()
    db.refresh(row)
    return row


def records_of_release(db: Session, release_id: int) -> list[DeploymentRecord]:
    return list(
        db.scalars(
            select(DeploymentRecord)
            .where(DeploymentRecord.release_id == release_id)
            .order_by(DeploymentRecord.id)
        ).all()
    )


def payload_of(row: DeploymentRecord) -> dict:
    try:
        data = json.loads(row.payload_json or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def latest_on_target(
    db: Session,
    pipeline_id: int,
    target: str,
    *,
    agent_id: int | None = None,
) -> DeploymentRecord | None:
    """某个部署目标上最后一次仍有效的部署。

    已经撤销过的记录不能再当「之后又部署过」——否则撤掉后一次、再撤前一次时，
    会把已经不在线上的那次误报成覆盖，把人吓回去或者判断错现场。

    负载均衡后面三台机器常常写同一个站点目录。目标必须带上节点，
    否则 A 机器的记录会被 B 机器同目录的后一次发布误判成覆盖。
    """
    stmt = select(DeploymentRecord).where(
        DeploymentRecord.pipeline_id == pipeline_id,
        DeploymentRecord.target == target,
        DeploymentRecord.undone_at.is_(None),
    )
    if agent_id is not None:
        stmt = stmt.where(DeploymentRecord.agent_id == agent_id)
    return db.scalar(stmt.order_by(DeploymentRecord.id.desc()))


def row_undo_blocker(row: DeploymentRecord) -> str:
    """这条记录缺了什么就回不去。空串表示可以生成完整的撤销步骤。"""
    payload = payload_of(row)
    label = KIND_LABEL.get(row.kind, row.kind)
    where = row.target or "未标明目标"
    if row.kind == "file-backup":
        if not str(payload.get("backupDir") or "").strip():
            return f"{label}「{where}」没有备份目录，无法还原"
        if not str(payload.get("targetDir") or row.target or "").strip():
            return f"{label}「{where}」没有站点目录，无法还原"
        return ""
    if row.kind == "docker-image":
        undo = payload.get("undo_with")
        if not isinstance(undo, dict) or not str(undo.get("image") or "").strip():
            return f"{label}「{where}」没有上一个镜像，无法回退容器"
        return ""
    if row.kind == "k8s-revision":
        undo = payload.get("undo_with") if isinstance(payload.get("undo_with"), dict) else {}
        rev = str(
            undo.get("toRevision")
            or undo.get("to_revision")
            or payload.get("previous_revision")
            or ""
        ).strip()
        if not rev:
            return f"{label}「{where}」没有上一个 revision，无法回退 K8s"
        return ""
    return f"不支持的部署类型：{row.kind}"


def _node_display(db: Session, agent_id: int | None) -> str:
    """回滚提示里怎么称呼这台机器。

    节点名是业务别名，同名或多台负载均衡时对不上现场。心跳上报的 host
    （节点页「IP / 主机」）一并写出。名字本身就是地址时只写一份。
    """
    if not agent_id:
        return ""
    agent = db.get(BuildAgent, agent_id)
    if agent is None:
        return "部署节点"
    name = (agent.name or "").strip()
    host = (agent.host or "").strip()
    if name and host and name != host:
        return f"节点「{name} {host}」"
    if name:
        return f"节点「{name}」"
    if host:
        return f"节点「{host}」"
    return "部署节点"


def _build_display(db: Session, release_id: int | None) -> str:
    """回滚提示里怎么称呼一次发布。

    执行历史列表展示的是流水线自己从 1 往上数的构建号；全库发布主键会大很多。
    找不到那条发布时宁可不出数字，也不要再甩一个对不上的 id。
    """
    if not release_id:
        return ""
    rel = db.get(Release, release_id)
    if rel is None:
        return ""
    return f"构建 #{rel.build_number or rel.id}"


def later_cover_warning(
    db: Session, row: DeploymentRecord, latest: DeploymentRecord
) -> str:
    """同一节点同一目录上，这次之后又有成功部署时的当面警告。

    回滚是把这次留下的备份盖回去，目录上现在的内容如果已经是更晚那次写的，
    盖回去等于把更晚那次也撤掉。必须把「哪台机器、哪个目录、被哪次构建覆盖」
    说成执行历史上能对上的名字和构建号。
    """
    node = _node_display(db, row.agent_id)
    where = (row.target or "").strip() or "未标明目录"
    later = _build_display(db, latest.release_id)
    loc = f"{node}上的目录「{where}」" if node else f"目录「{where}」"
    if later:
        return (
            f"{loc}在这次之后又被{later}覆盖过。"
            "现在回滚会把那一次写上去的内容一起撤掉"
        )
    return (
        f"{loc}在这次之后又部署过。"
        "现在回滚会把之后写上去的内容一起撤掉"
    )


def check_undoable(
    db: Session, release_id: int
) -> tuple[list[DeploymentRecord], list[str], str]:
    """能不能撤销这次发布，以及有哪些需要当面告诉用户的风险。

    返回 (可撤销的记录, 警告列表, 不能回滚的原因)。记录为空时上层应当直接拒绝，
    并把原因原样告诉用户——「已经撤销过」和「压根没部署过」要采取的下一步完全不同。
    """
    rows = records_of_release(db, release_id)
    warnings: list[str] = []
    usable: list[DeploymentRecord] = []
    undone: list[DeploymentRecord] = []
    blockers: list[str] = []

    for row in rows:
        if row.undone_at is not None:
            undone.append(row)
            continue
        blocker = row_undo_blocker(row)
        if blocker:
            blockers.append(blocker)
            continue
        latest = latest_on_target(
            db, row.pipeline_id, row.target, agent_id=row.agent_id
        )
        if latest is not None and latest.id != row.id:
            warnings.append(later_cover_warning(db, row, latest))
        usable.append(row)

    # 有一条记不全就不能撤：只还原其中几个目标，现场会停在一半新一半旧
    if blockers:
        return [], warnings, (
            "这次发布有部署记录不完整，不能一键回滚，避免只撤一部分把环境拆成两半："
            + "；".join(blockers)
        )

    if usable:
        for row in undone:
            warnings.append(
                f"{KIND_LABEL.get(row.kind, row.kind)}「{row.target}」已经在 "
                f"{row.undone_at.strftime('%m-%d %H:%M')} 撤销过，这次跳过它"
            )
        return usable, warnings, ""

    if undone:
        when = max(r.undone_at for r in undone).strftime("%m-%d %H:%M")
        by = next((r.undone_by_release_id for r in undone if r.undone_by_release_id), None)
        by_label = _build_display(db, by)
        reason = f"这次发布已经在 {when} 撤销过" + (f"（{by_label}）" if by_label else "")
        return [], warnings, (
            reason + "，不能再撤一次：备份已经盖回生产，重复还原只会把更早的版本翻上来。"
            "要回到别的版本，请用那个版本重新发一次"
        )

    return [], warnings, (
        "这次发布没有留下部署记录，无法一键回滚。"
        "可能它没真正往目标环境写东西，或者发生在平台支持回滚之前；"
        "这种情况请用上一个正常版本重新发一次"
    )


def build_undo_jobs(
    db: Session,
    rows: list[DeploymentRecord],
    *,
    image_tags: dict[int, str] | None = None,
) -> list[dict]:
    """把部署记录翻译成回滚计划：[{agent, name, steps}]。

    步骤形状和正常发布一致（plugin + with），能直接塞进 BuildTask，
    复用现成的派发、日志、取消和状态聚合，不必为回滚另造一套执行链路。

    按机器分组是必须的：备份文件只在当时那台节点的磁盘上，kubeconfig 和
    docker 上下文也只在当时那台构建机上。派到别处去，回滚必然扑空。
    image_tags 只换容器镜像的 tag，仓库路径仍用当时登记的那条。
    """
    jobs: list[dict] = []
    tags = image_tags or {}
    for row in rows:
        if row_undo_blocker(row):
            continue
        payload = payload_of(row)
        if row.kind == "file-backup":
            # 节点步骤由 with.node 指定机器，Job 的 agent 标签用不上
            steps = _file_backup_steps(db, row, payload)
            agent = "any"
        elif row.kind == "docker-image":
            undo = dict(payload.get("undo_with") or {})
            current, previous = docker_images_of(row)
            base = str(undo.get("image") or previous or current)
            if row.id in tags:
                undo["image"] = replace_image_tag(base, tags[row.id])
            image = str(undo.get("image") or "").strip()
            steps = [{
                "name": f"回退镜像：{image or row.target}",
                "plugin": "docker-deploy",
                "with": {
                    **undo,
                    "_deployment_record_id": row.id,
                },
            }]
            agent = f"agent:{row.agent_id}" if row.agent_id else "any"
        elif row.kind == "k8s-revision":
            steps = [{
                "name": f"回退到上一个版本：{row.target}",
                "plugin": "k8s-deploy",
                "with": {
                    **payload.get("undo_with", {}),
                    "_deployment_record_id": row.id,
                },
            }]
            agent = f"agent:{row.agent_id}" if row.agent_id else "any"
        else:
            continue
        if steps:
            jobs.append({
                "id": f"undo-{row.id}",
                "name": f"撤销 {KIND_LABEL.get(row.kind, row.kind)}：{row.target}",
                "agent": agent,
                "steps": steps,
            })
    # 缺一条就不拼计划，避免只撤一部分
    if len(jobs) != len(rows):
        return []
    return stitch_rolling_undo(jobs)


def stitch_rolling_undo(jobs: list[dict]) -> list[dict]:
    """多台节点的撤销合成一个 Job，后发的先撤。

    同一 Job 里按节点拆段后，后一段会等前一段成功——和发布时「一个 Job 抄三遍」
    是同一条链路。三台 Job 放进同一 Stage 会并行，三台应用池一起停。
    """
    if len(jobs) <= 1:
        return jobs
    node_jobs = [job for job in jobs if _is_node_undo_job(job)]
    other = [job for job in jobs if not _is_node_undo_job(job)]
    if len(node_jobs) <= 1:
        return other + node_jobs
    steps: list[dict] = []
    order: list[str] = []
    for job in reversed(node_jobs):
        order.append(str(job.get("name") or ""))
        steps.extend(s for s in (job.get("steps") or []) if isinstance(s, dict))
    return other + [{
        "id": "undo-rolling",
        "name": "滚动撤销（后发先撤，一次一台）",
        "agent": "any",
        "steps": steps,
        "rolling_order": order,
    }]


def _is_node_undo_job(job: dict) -> bool:
    return any(
        isinstance(step, dict)
        and step.get("plugin") in {"rollback-files", "iis-control", "service-control"}
        for step in (job.get("steps") or [])
    )


_SERVICE_PLUGINS = {"iis-control", "service-control"}
_START_ACTIONS = {"start", "restart", "reload", "recycle"}


def _service_action(step: dict) -> str:
    raw = step.get("with") if isinstance(step.get("with"), dict) else {}
    return str(raw.get("action") or "").strip().lower()


def _parse_steps(raw: str) -> list[dict]:
    try:
        steps = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _norm_dir(path: str) -> str:
    return str(path or "").strip().rstrip("\\/").replace("\\", "/").lower()


def _is_transfer(step: dict) -> bool:
    return step.get("plugin") == "file-transfer"


def _transfer_dir(step: dict) -> str:
    raw = step.get("with") if isinstance(step.get("with"), dict) else {}
    return _norm_dir(raw.get("targetDir") or "")


def _same_node(step: dict, node: int | None) -> bool:
    """没写节点的启停跟传文件算同一台；写了就得对上，别把另一台机器的池子套过来。"""
    raw = step.get("with") if isinstance(step.get("with"), dict) else {}
    value = raw.get("node")
    if value in (None, "", 0, "0"):
        return True
    try:
        return node is None or int(value) == int(node)
    except (TypeError, ValueError):
        return True


def _release_step_seq(db: Session, release_id: int) -> list[tuple[int, int, dict]]:
    """同一次发布全部任务按 id 拼成步骤序列：(task_id, 任务内下标, 步骤)。

    流水线常把停 IIS、传文件、起 IIS 拆成三个 Job，节点侧就是三个任务。
    只看传文件那个任务，前后是空的，回滚会只还原文件、完全不停不起。
    """
    from app.modules.agent.models import BuildTask

    tasks = list(
        db.scalars(
            select(BuildTask)
            .where(BuildTask.release_id == release_id)
            .order_by(BuildTask.id)
        ).all()
    )
    out: list[tuple[int, int, dict]] = []
    for task in tasks:
        for index, step in enumerate(_parse_steps(task.steps_json)):
            out.append((task.id, index, step))
    return out


def _locate_transfer(
    seq: list[tuple[int, int, dict]], row: DeploymentRecord, payload: dict
) -> int:
    """对准这次部署对应的 file-transfer，不能见着第一个传文件就套上去。

    一次发布里两个站点各传一次时，step_index 对不上就会拿错另一套 IIS。
    """
    want = _norm_dir(payload.get("targetDir") or row.target or "")

    for pos, (task_id, index, step) in enumerate(seq):
        if task_id == row.task_id and index == row.step_index and _is_transfer(step):
            if not want or _transfer_dir(step) == want:
                return pos

    in_task = [
        pos
        for pos, (task_id, _index, step) in enumerate(seq)
        if task_id == row.task_id and _is_transfer(step)
        and (not want or _transfer_dir(step) == want)
    ]
    if in_task:
        return in_task[-1]

    if want:
        by_dir = [
            pos
            for pos, (_task_id, _index, step) in enumerate(seq)
            if _is_transfer(step) and _transfer_dir(step) == want
        ]
        if by_dir:
            return by_dir[-1]
    return -1


def _collect_neighbors(
    seq: list[tuple[int, int, dict]], loc: int, node: int | None
) -> tuple[list[dict], list[dict]]:
    """从这次传文件往两边找启停。

    中间夹着构建步骤、status 查询、另一台机器的启停都跳过，不能因此把本台
    的停池/停站丢掉。碰到另一次 file-transfer 必须停——那是另一套站点。
    recycle 算启动侧：有的流水线传完文件只回收应用池，不显式 start。
    """
    stops: list[dict] = []
    i = loc - 1
    while i >= 0:
        step = seq[i][2]
        if _is_transfer(step):
            break
        if step.get("plugin") in _SERVICE_PLUGINS:
            action = _service_action(step)
            if action == "status" or not _same_node(step, node):
                i -= 1
                continue
            if action != "stop":
                break
            stops.append(step)
        i -= 1
    stops.reverse()

    starts: list[dict] = []
    i = loc + 1
    while i < len(seq):
        step = seq[i][2]
        if _is_transfer(step):
            break
        if step.get("plugin") in _SERVICE_PLUGINS:
            action = _service_action(step)
            if action == "status" or not _same_node(step, node):
                i += 1
                continue
            if action not in _START_ACTIONS:
                break
            starts.append(step)
        i += 1
    return stops, starts


def _neighbors_from_original_task(
    db: Session, row: DeploymentRecord, payload: dict | None = None
) -> tuple[list[dict], list[dict]]:
    """发布时 file-transfer 前后的停/起，整段都要回放。

    以整次发布的任务步骤为准，不依赖 Agent 当时记全。节点上报以前只记
    「最近一条」停和「最近一条」起，停池+停站会漏掉一半。
    """
    payload = payload if isinstance(payload, dict) else payload_of(row)
    seq = _release_step_seq(db, row.release_id)
    if not seq:
        return [], []
    loc = _locate_transfer(seq, row, payload)
    if loc < 0:
        return [], []
    return _collect_neighbors(seq, loc, row.agent_id)


def _controls_from_payload(
    payload: dict, list_key: str, single_key: str, plugin_key: str, verb: str
) -> list[dict]:
    """任务步骤找不到时，退回 Agent 当时记在 payload 里的启停。"""
    raw = payload.get(list_key)
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)]
    single = payload.get(single_key)
    if isinstance(single, dict) and single:
        plugin = str(payload.get(plugin_key) or "iis-control")
        return [{
            "plugin": plugin,
            "with": single,
            "name": f"{verb} {single.get('name') or ''}".strip(),
        }]
    return []


def _control_step(item: dict, *, verb: str, plugin: str, node: int | None) -> dict:
    raw = dict(item.get("with") or {}) if isinstance(item.get("with"), dict) else dict(item)
    name = str(item.get("name") or "").strip() or f"{verb} {raw.get('name') or ''}".strip()
    return {
        "name": name,
        "plugin": str(item.get("plugin") or plugin),
        "with": {**raw, "node": node},
    }


def _file_backup_steps(db: Session, row: DeploymentRecord, payload: dict) -> list[dict]:
    """增量发布的撤销：停服务 → 还原文件 → 起服务。

    停起的参数直接沿用发布时那一步的配置。发布时需要停服务才能换文件，
    还原同样是在换文件，理由一模一样；发布时没停，回滚也不该自作主张去停。

    停起用哪个插件也照抄发布时那一步：Windows 是 iis-control，Linux 是
    service-control。旧记录里没有这个字段，按 IIS 兜底——那会儿只有 Windows 节点。
    """
    backup_dir = str(payload.get("backupDir") or "").strip()
    target_dir = str(payload.get("targetDir") or row.target or "").strip()
    if not backup_dir or not target_dir:
        return []

    steps: list[dict] = []
    node = row.agent_id
    stop_plugin = payload.get("stop_plugin") or "iis-control"
    start_plugin = payload.get("start_plugin") or "iis-control"
    stops, starts = _neighbors_from_original_task(db, row, payload)
    if not stops:
        stops = _controls_from_payload(payload, "stop_steps", "stop_with", "stop_plugin", "停止")
    if not starts:
        starts = _controls_from_payload(payload, "start_steps", "start_with", "start_plugin", "启动")

    for item in stops:
        steps.append(_control_step(item, verb="停止", plugin=stop_plugin, node=node))
    steps.append({
        "name": f"还原备份到 {target_dir}".strip(),
        "plugin": "rollback-files",
        "with": {
            "backupDir": backup_dir,
            "targetDir": target_dir,
            "node": node,
            "_deployment_record_id": row.id,
        },
    })
    for item in starts:
        steps.append(_control_step(item, verb="启动", plugin=start_plugin, node=node))
    return steps


def mark_undone(db: Session, record_id: int, release_id: int) -> None:
    row = db.get(DeploymentRecord, record_id)
    if row is None or row.undone_at is not None:
        return
    row.undone_at = datetime.now()
    row.undone_by_release_id = release_id
    db.commit()


def mark_release_records_undone(db: Session, original_release_id: int, by_release_id: int) -> int:
    """回滚单跑成功后，把原发布留下的部署记录全部销账。

    节点/插件销账失败时记录仍是「未撤销」，确认框会让人再撤一次，同一份备份
    盖两遍。平台在回滚成功这一刻再盖一次章，不依赖 Agent 那一次 HTTP 一定送到。
    """
    now = datetime.now()
    n = 0
    for row in records_of_release(db, original_release_id):
        if row.undone_at is not None:
            continue
        row.undone_at = now
        row.undone_by_release_id = by_release_id
        n += 1
    return n


def preview_item(db: Session, row: DeploymentRecord) -> dict:
    """确认框里一条部署要展示的内容和将要执行的步骤。"""
    payload = payload_of(row)
    if row.kind == "file-backup":
        steps = _file_backup_steps(db, row, payload)
    else:
        jobs = build_undo_jobs(db, [row])
        steps = jobs[0]["steps"] if jobs else []
    current, previous = docker_images_of(row) if row.kind == "docker-image" else ("", "")
    repo, current_tag = split_image_ref(current)
    prev_repo, previous_tag = split_image_ref(previous)
    return {
        "id": row.id,
        "kind": row.kind,
        "kind_label": KIND_LABEL.get(row.kind, row.kind),
        "target": row.target,
        "summary": row.summary,
        "agent_id": row.agent_id,
        "current_image": current,
        "previous_image": previous,
        "image_repo": repo or prev_repo,
        "current_tag": current_tag,
        "previous_tag": previous_tag,
        "steps": [
            {"name": str(s.get("name") or ""), "plugin": str(s.get("plugin") or "")}
            for s in steps
            if isinstance(s, dict)
        ],
    }


def describe(rows: list[DeploymentRecord]) -> str:
    """给回滚确认框和审批通知用的一句话说明。"""
    if not rows:
        return "没有可撤销的部署"
    parts = []
    for row in rows:
        label = KIND_LABEL.get(row.kind, row.kind)
        parts.append(f"{label} {row.target}：{row.summary}" if row.summary
                     else f"{label} {row.target}")
    return "；".join(parts)
