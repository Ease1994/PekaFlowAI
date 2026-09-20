"""把对话框里的文件下发到节点。

不另造一套执行链路：打包成制品 → 建一条正常的发布单 → 节点照常拉包、备份、覆盖。
审批、审计、执行日志、备份回滚因此全都跟正常发布一个样，不用重写一遍。

这条路绕开了流水线，所以权限单独把关：必须持有目标节点的 deploy 权限，
生产节点还要过审批。真正的落盘边界仍在节点本地的 --allow-paths，平台越不过去。
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.env import PLATFORM_PROJECT_CODE
from app.core.response import BizException
from app.modules.agent.models import BuildAgent
from app.modules.pipeline.models import Pipeline
from app.modules.project.models import Group, Project

# 内置项目/分组/流水线的固定标识，靠它们找回来，不靠 id
SYSTEM_PROJECT_CODE = PLATFORM_PROJECT_CODE
SYSTEM_GROUP_NAME = "节点文件下发"
SYSTEM_PIPELINE_NAME = "节点文件下发"

# 真正执行时步骤由 _plan 现场生成（发几台就是几个 Job），不走这份 YAML。
# 留着它是因为流水线得有个合法定义才能在页面上打开；手动点执行会因为
# NODE_ID 为空而报「没有选择部署节点」——本来也不该手动执行，没有附件包。
_PIPELINE_YAML = """\
pipeline:
  name: 节点文件下发
  triggers:
    - type: manual
  variables:
    - name: NODE_ID
      type: text
      default_value: ""
      description: 目标节点
    - name: TARGET_DIR
      type: text
      default_value: ""
      description: 目标目录
  stages:
    - name: 下发
      jobs:
        - name: 发送文件到节点
          agent: node
          steps:
            - name: 发送文件到节点
              plugin: file-transfer
              with:
                node: ${{NODE_ID}}
                targetDir: ${{TARGET_DIR}}
"""


def ensure_pipeline(db: Session) -> Pipeline:
    """取内置下发流水线，没有就建一条。

    放在一个内置项目下，不混进用户自己的项目列表里；分组开审批，
    测试节点靠 skip_approval 直接放行，生产节点老老实实等人点头。
    """
    proj = db.scalar(select(Project).where(Project.code == SYSTEM_PROJECT_CODE))
    if proj is None:
        proj = Project(
            name="平台内置",
            code=SYSTEM_PROJECT_CODE,
            description="平台自用，不用于业务发布",
        )
        db.add(proj)
        db.flush()

    grp = db.scalar(
        select(Group).where(Group.project_id == proj.id, Group.name == SYSTEM_GROUP_NAME)
    )
    if grp is None:
        grp = Group(
            project_id=proj.id,
            name=SYSTEM_GROUP_NAME,
            type="prod",
            approval_required=True,
            # 小团队里管理员可能只有一个人，不允许自审就等于谁也发不出去
            allow_self_approval=True,
        )
        db.add(grp)
        db.flush()
    else:
        # 分组模型默认 approval_required=False。这条线如果是早期版本建的、
        # 或者有人在项目页把审批关掉了，生产节点下发就会直接落盘——
        # 卡片上写着「先进审批」，实际跳过。闸门是平台不变量，每次下发都自愈回来。
        if grp.type != "prod":
            grp.type = "prod"
        if not grp.approval_required:
            grp.approval_required = True
        if not grp.allow_self_approval:
            grp.allow_self_approval = True

    pipe = db.scalar(
        select(Pipeline).where(
            Pipeline.project_id == proj.id, Pipeline.name == SYSTEM_PIPELINE_NAME
        )
    )
    if pipe is None:
        pipe = Pipeline(
            project_id=proj.id,
            group_id=grp.id,
            name=SYSTEM_PIPELINE_NAME,
            description="AI 助手把文件下发到节点时走的内置流水线",
            yaml=_PIPELINE_YAML,
            version=1,
            created_by=0,
            workspace_uuid=uuid.uuid4().hex,
        )
        db.add(pipe)
        db.flush()
    else:
        if pipe.yaml != _PIPELINE_YAML:
            # 平台升级后改了步骤定义，内置流水线跟着走，不留旧版本在库里
            pipe.yaml = _PIPELINE_YAML
            pipe.group_id = grp.id
        # 这条线的审批闸门护着「AI 往生产节点写文件」这一整类操作，是平台不变量，
        # 不是可以按流水线调的策略——一条线被豁免，所有人往所有生产节点传文件就都不用批了。
        # 所以就算有人改过，每次下发都自愈回跟随环境
        if (pipe.approval_mode or "inherit") != "inherit":
            pipe.approval_mode = "inherit"
    db.commit()
    db.refresh(pipe)
    return pipe


def resolve_node(db: Session, current, node_id: int) -> BuildAgent:
    """找到节点并确认这个人能往上面写东西。"""
    from app.core.deps import check_permission

    node = db.get(BuildAgent, node_id)
    if node is None or (node.role or "builder") != "node":
        raise BizException.bad_request(f"节点 #{node_id} 不存在")
    if not check_permission(db, current, "node", node.id, "deploy"):
        raise BizException.forbidden(
            f"你没有节点「{node.name}」的下发权限。往生产机写文件要单独授权，"
            "请让管理员在「权限管理 → 用户授权」里，授权范围选「节点组」或「指定节点」授给你"
        )
    return node


def node_allow_paths(node: BuildAgent) -> list[str]:
    try:
        raw = json.loads(node.allow_paths or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(p) for p in raw if str(p).strip()]


def _norm_win(path: str) -> str:
    """按 Windows 语义规范化路径，只为提前拦一道，不是安全边界。

    平台跑在 Linux 容器里，摸不到节点的磁盘，做不了 canonical，
    解析符号链接、8.3 短名这些仍然只能靠节点自己。这里挡的是打错字，
    省得任务派下去跑到一半才失败。
    """
    return path.replace("\\", "/").rstrip("/").lower()


def check_target_dir(node: BuildAgent, target_dir: str) -> None:
    """目标目录的预检：空值、相对路径、明显不在白名单内的直接回绝。"""
    raw = (target_dir or "").strip()
    if not raw:
        raise BizException.bad_request("请指定要传到哪个目录")
    if ".." in raw.replace("\\", "/").split("/"):
        raise BizException.bad_request("目标目录不能包含 ..")
    looks_absolute = ":" in raw[:3] or raw.startswith("/") or raw.startswith("\\\\")
    if not looks_absolute:
        raise BizException.bad_request(f"目标目录要写绝对路径，比如 D:\\wwwroot\\site，收到的是「{raw}」")

    allow = node_allow_paths(node)
    if not allow:
        raise BizException.bad_request(
            f"节点「{node.name}」还没有配置允许目录，拒绝下发。"
            "请在节点管理里填写允许路径，节点在线心跳后生效。"
        )
    target = _norm_win(raw)
    for root in allow:
        r = _norm_win(root)
        if target == r or target.startswith(r + "/"):
            return
    raise BizException.bad_request(
        f"目录「{raw}」不在节点「{node.name}」允许的范围内：{allow}。"
        "要发到别处请在节点管理页改允许目录，在线节点心跳后生效"
    )


def resolve_nodes(db: Session, current, params: dict) -> list[BuildAgent]:
    """解析目标节点列表，逐台校验权限。

    同一批文件常常要发到负载均衡后面的好几台机器，所以收的是列表；
    node_id 单数形式也认，模型有时会按老写法给。
    """
    raw = params.get("node_ids")
    if raw is None:
        raw = [params.get("node_id")] if params.get("node_id") else []
    if not isinstance(raw, list):
        raw = [raw]
    ids: list[int] = []
    for item in raw:
        try:
            nid = int(item)
        except (TypeError, ValueError):
            continue
        if nid and nid not in ids:
            ids.append(nid)
    if not ids:
        raise BizException.bad_request("没有指定要发到哪台节点")
    return [resolve_node(db, current, nid) for nid in ids]


def build_card(db: Session, current, params: dict) -> dict:
    """生成下发确认卡片。真正落盘要等用户点确认。"""
    from app.modules.ai import attachments
    from app.modules.ai.skills.base import card

    nodes = resolve_nodes(db, current, params)
    target_dir = str(params.get("target_dir") or "").strip()
    # 各台的 allow_paths 可能不一样，挨个验，别等派下去才发现有一台不让写
    for node in nodes:
        check_target_dir(node, target_dir)

    ids = [int(x) for x in (params.get("attachment_ids") or [])]
    if not ids:
        raise BizException.bad_request("没有待下发的文件，请先把文件拖进对话框")
    rows = attachments.list_pending(db, current.id, ids)

    from app.core.env import env_label, skip_node_push_approval

    total = sum(r.size_bytes for r in rows)
    listed = "\n".join(f"  - {r.rel_path or r.name}（{_size_text(r.size_bytes)}）" for r in rows)
    # 只要有一台不能免审，整单就走审批。test/dev 免审，prod/uat/staging/自定义一律要审
    gated = [n for n in nodes if not skip_node_push_approval(n.env)]
    need_approval = bool(gated)
    node_text = "、".join(
        f"{n.name}（{env_label(n.env)}）" for n in nodes
    )

    if need_approval:
        tail = (
            f"其中 {len(gated)} 台是{env_label(gated[0].env) if len(gated) == 1 else '需审批'}节点，确认后先进审批，通过了才真正下发。"
            if len(nodes) > 1
            else f"这是{env_label(nodes[0].env)}节点，确认后先进审批，通过了才真正下发。"
        )
        tail += " 审批人是对内置分组「节点文件下发」有审批权的人（管理员始终可以批），不是某个业务项目里的「审批人」角色。"
    else:
        tail = "都是测试/开发节点，确认后立即下发。"

    # 直接发到白名单根目录时提醒一句。发到 D:\wwwroot 和发到 D:\wwwroot\testagent
    # 是两回事：前者铺在站点根上，同名文件覆盖的就是正在跑的站点。
    # 用户说的目录被改写成根目录这种事发生过，卡片上不点出来很难一眼看见
    root_hit = [
        n.name
        for n in nodes
        if any(_norm_win(target_dir) == _norm_win(p) for p in node_allow_paths(n))
    ]
    if root_hit:
        tail = (
            f"\n⚠️ 注意：{target_dir} 是 {'、'.join(root_hit)} 允许写入的**根目录**，"
            "文件会直接铺在这一层。如果你要发的是它下面的某个子目录，现在取消并说明完整目录。\n"
            + tail
        )

    return card(
        "confirm_node_push",
        "确认下发文件" if not need_approval else "确认提交下发申请",
        {
            "node_ids": [n.id for n in nodes],
            "target_dir": target_dir,
            "attachment_ids": [r.id for r in rows],
        },
        f"准备把 {len(rows)} 个文件下发到 {len(nodes)} 台节点：{node_text}\n"
        f"{listed}\n"
        f"目标目录：{target_dir}\n"
        f"合计 {_size_text(total)}"
        + ("，每台都发同一份。" if len(nodes) > 1 else "。")
        + "同名文件会被覆盖，覆盖前会自动备份，事后可以回滚。\n"
        + tail,
    )


def _size_text(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _plan(nodes: list[BuildAgent], target_dir: str) -> str:
    """本次下发的专属执行计划：一台节点一个 Job，各自派给自己的 Agent。

    内置流水线的 YAML 只有一个步骤，发多台就不够用了。回滚早就在用
    plan_json 现场生成步骤，这里走同一条路，省得为每种节点组合各存一条流水线。
    """
    jobs = []
    for n in nodes:
        jobs.append(
            {
                "id": f"push-{n.id}",
                "name": f"下发到 {n.name}",
                "agent": "node",
                "steps": [
                    {
                        "name": f"发送文件到 {n.name}",
                        "plugin": "file-transfer",
                        "with": {"node": n.id, "targetDir": target_dir},
                    }
                ],
            }
        )
    return json.dumps(
        {
            "name": "文件下发",
            "job_name": "下发到节点",
            "jobs": jobs,
            "summary": f"{len(nodes)} 台节点 → {target_dir}",
        },
        ensure_ascii=False,
    )


def execute(db: Session, current, payload: dict):
    """真正下发：建发布单 → 附件打包挂成制品 → 排队或送审。"""
    from app.modules.ai import attachments
    from app.modules.artifact import storage
    from app.modules.artifact.models import Artifact
    from app.modules.artifact.service import DEPLOY_PACKAGE_TYPE
    from app.modules.audit.service import write as write_audit
    from app.modules.pipeline import service as pipeline_service

    nodes = resolve_nodes(db, current, payload)
    target_dir = str(payload.get("target_dir") or "").strip()
    for node in nodes:
        check_target_dir(node, target_dir)
    ids = [int(x) for x in (payload.get("attachment_ids") or [])]
    rows = attachments.list_pending(db, current.id, ids)

    pipe = ensure_pipeline(db)
    from app.core.env import skip_node_push_approval

    # 只要有一台不能免审，整单就走审批。test/dev 免审，其余（含 uat/自定义）一律要审
    skip_approval = all(skip_node_push_approval(n.env) for n in nodes)
    node_names = "、".join(n.name for n in nodes)
    release = pipeline_service.create_release(
        db,
        pipeline_id=pipe.id,
        version=f"push-{nodes[0].name}" + (f"+{len(nodes) - 1}" if len(nodes) > 1 else ""),
        strategy="rolling",
        trigger_by="ai",
        operator_id=current.id,
        run_params={
            "TARGET_DIR": target_dir,
            # 备份目录按这两个变量分层，写清楚是谁下发的比 unknown-project 有用得多
            "BK_CI_PROJECT_NAME": "节点下发",
            "BK_CI_PIPELINE_NAME": node_names[:64],
        },
        plan_json=_plan(nodes, target_dir),
        skip_approval=skip_approval,
    )

    # 打包挂到这次发布上：节点执行 file-transfer 时按 release_id 找这个包
    data = attachments.pack(rows)
    name = f"node-push-{release.id}.zip"
    storage_key, sha = storage.save(release.id, name, data)
    db.add(
        Artifact(
            pipeline_id=pipe.id,
            release_id=release.id,
            name=name,
            type=DEPLOY_PACKAGE_TYPE,
            version=release.version,
            storage_key=storage_key,
            sha256=sha,
            size_bytes=len(data),
        )
    )
    attachments.mark_consumed(db, rows, release.id)
    db.commit()

    # 一台一条审计：事后按节点查「这台机器被谁动过」才查得出来
    for node in nodes:
        write_audit(
            db,
            "node.push",
            "node",
            node.id,
            f"下发 {len(rows)} 个文件到节点 {node.name}:{target_dir}，发布单 #{release.id}，"
            f"文件={[r.rel_path or r.name for r in rows]}",
            user_id=current.id,
            source="ai",
        )
    db.commit()

    if release.status == "queued":
        pipeline_service.execute_release(db, release.id)
        db.refresh(release)
    return release, nodes, len(rows)
