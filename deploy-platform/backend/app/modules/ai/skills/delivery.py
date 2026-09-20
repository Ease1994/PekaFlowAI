"""交付类技能：发布 / 回滚 / Rebuild / 取消 / 审批。高风险只出确认卡。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.env import env_label
from app.core.response import BizException
from app.modules.ai.registry import Skill, register
from app.modules.ai.skills.base import card
from app.modules.pipeline.models import Pipeline
from app.modules.pipeline.service import approval_required_for, create_release, get_pipeline
from app.modules.project.models import Group


def _approval_note(g: Group | None, p: Pipeline | None, action: str) -> str:
    """确认卡上那句「要不要审批」的说明，顺带讲清楚是谁定的。

    审批要求可能来自环境，也可能来自流水线自己的覆盖。只说「生产分组需要审批」
    的话，一条设了强制审批的测试流水线就会给出自相矛盾的提示。
    """
    if g is None:
        return ""
    mode = (getattr(p, "approval_mode", "") or "inherit") if p is not None else "inherit"
    if approval_required_for(g, p):
        why = "该流水线被单独设为强制审批" if mode == "force" else f"「{g.name}」需要审批"
        return f"{why}，{action}会先进入待审批。"
    why = "该流水线被单独设为豁免审批" if mode == "exempt" else f"「{g.name}」免审批"
    return f"{why}，确认后立即执行。"


def _as_pipeline_id(raw) -> int:
    """只接受真正的数字 id。模型把「AI陪练项目生产环境」塞进 pipeline_id 时当 0。"""
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def _normalize_manifest_text(raw) -> str:
    """把模型塞来的清单收成一行一条。列表、逗号、换行都能认。"""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        return "\n".join(str(x).strip() for x in raw if str(x).strip())
    text = str(raw).replace(",", "\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _manifest_note(pipeline, user_text: str) -> str:
    """确认卡上说明这次清单从哪来：对话指定，或沿用步骤里写死的列表。"""
    from app.modules.deploy.service import pack_incremental_manifest_of

    configured = pack_incremental_manifest_of(pipeline)
    if configured is None:
        return ""
    text = (user_text or "").strip()
    if text:
        preview = text if len(text) <= 240 else text[:240] + "…"
        return f"本次发布清单：\n{preview}\n"
    return "对话里没另指定文件，将沿用「提取增量发布包」步骤里已经写死的清单。\n"


def _release_run_params(pipeline, user_text: str | None) -> dict:
    """AI 发布和页面执行同一套：变量默认值 + 发布清单规则。"""
    from app.modules.deploy.service import apply_execute_manifest
    from app.modules.pipeline.sub_pipeline import merge_params

    return apply_execute_manifest(pipeline, merge_params(pipeline, None), user_text)


def _propose_release(db: Session, current, params: dict) -> dict:
    """生成发布确认卡。id、名称、项目+环境或用户原话都可以，由解析器落到可见流水线。"""
    from app.core.deps import check_permission
    from app.modules.ai.context import resolve_release_target

    params = dict(params or {})
    raw_id = params.get("pipeline_id")
    pipeline_id = _as_pipeline_id(raw_id)
    if raw_id not in (None, "") and pipeline_id == 0:
        # 非数字 id 改当流水线名，避免 int() 炸掉整轮、模型再自己编一张确认卡。
        if not str(params.get("pipeline") or "").strip():
            params["pipeline"] = str(raw_id).strip()
        if not str(params.get("query") or "").strip():
            params["query"] = str(raw_id).strip()
    found = resolve_release_target(
        db,
        current,
        pipeline_id=pipeline_id,
        pipeline=str(params.get("pipeline") or ""),
        project=str(params.get("project") or ""),
        env=str(params.get("env") or ""),
        query=str(params.get("query") or ""),
    )
    p = found.get("match")
    if p is None:
        if found.get("candidates"):
            rows = [
                {
                    "id": pipe.id,
                    "name": pipe.name,
                    "project": proj.name if proj else "",
                    "env": group.type if group else "",
                }
                for pipe, proj, group in found["candidates"][:10]
            ]
            return {
                "error": "未唯一对应到一条流水线",
                "reply": found.get("reply") or "",
                "candidates": rows,
            }
        if found.get("projects"):
            return {
                "error": "未唯一对应到一个项目",
                "reply": found.get("reply") or "",
                "projects": [
                    {"id": item.id, "name": item.name, "code": item.code}
                    for item in found["projects"][:10]
                ],
            }
        return {"error": found.get("error") or "找不到对应流水线"}
    if _as_pipeline_id(getattr(p, "id", None)) == 0:
        return {"error": "解析结果没有合法的流水线 id"}
    if not check_permission(db, current, "pipeline", p.id, "execute"):
        return {
            "error": f"你没有流水线「{p.name}」的执行权限，发不了。"
            f"可以说「申请 {p.name} 的执行权限」提交申请单，管理员通过后再发。"
        }
    item = found.get("item")
    g = item[2] if item else db.get(Group, p.group_id)
    version = str(params.get("version") or "").strip() or f"ai-{datetime.now():%Y%m%d%H%M%S}"
    group_label = env_label(g.type) if g else "未知"
    need_approval = bool(g) and approval_required_for(g, p)
    manifest = _normalize_manifest_text(
        params.get("deploy_manifest") or params.get("manifest") or params.get("files")
    )
    from app.modules.deploy.service import (
        is_manifest_placeholder,
        pack_incremental_manifest_of,
    )

    configured = pack_incremental_manifest_of(p)
    if configured is not None and is_manifest_placeholder(configured) and not manifest:
        return {
            "error": (
                "这条流水线按发布清单打增量包，必须先指定要发哪些文件"
                "（例如 bin/*.dll、Areas/）。未指定不会按编译产物全量打包，也不会发出去。"
            )
        }
    payload = {
        "pipeline_id": int(p.id),
        "version": version,
        "strategy": params.get("strategy") or "rolling",
    }
    if manifest:
        payload["deploy_manifest"] = manifest
    return card(
        "confirm_release",
        "确认发起发布申请" if need_approval else "确认发布",
        payload,
        f"已识别 流水线=#{p.id} {p.name} · 分组={group_label}。\n"
        "不是这条就别点确认，直接把流水线 id 或完整名称告诉我。\n"
        "将执行这条流水线配置好的全部步骤。\n"
        + _manifest_note(p, manifest)
        + _approval_note(g, p, "发布"),
    )


def _propose_rollback(db: Session, current, params: dict) -> dict:
    from app.core.deps import check_permission
    from app.modules.pipeline.service import get_release

    release_id = int(params.get("release_id") or 0)
    if not release_id:
        return {"error": "缺少 release_id"}
    r = get_release(db, release_id)
    if not check_permission(db, current, "pipeline", r.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：对流水线 #{r.pipeline_id} 回滚")
    p = db.get(Pipeline, r.pipeline_id)
    g = db.get(Group, r.group_id)
    return card(
        "confirm_rollback",
        f"确认回滚 #{r.id}",
        {"release_id": r.id},
        f"回滚属于高风险操作。确认回滚发布 #{r.id}（流水线 {p.name if p else r.pipeline_id}，状态 {r.status}）？\n"
        + _approval_note(g, p, "回滚单"),
    )


def _propose_rebuild(db: Session, current, params: dict) -> dict:
    from app.core.deps import check_permission
    from app.modules.pipeline.service import get_release

    release_id = int(params.get("release_id") or 0)
    r = get_release(db, release_id)
    if not check_permission(db, current, "pipeline", r.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：Rebuild 流水线 #{r.pipeline_id}")
    g = db.get(Group, r.group_id)
    p = db.get(Pipeline, r.pipeline_id)
    return card(
        "confirm_rebuild",
        f"确认 Rebuild #{r.id}",
        {"release_id": r.id},
        f"将用原代码版本 {r.source_ref or '（未记录）'} 重新跑发布 #{r.id}，确认？\n"
        + _approval_note(g, p, "Rebuild 单"),
    )


def _propose_cancel(db: Session, current, params: dict) -> dict:
    from app.core.deps import check_permission
    from app.modules.pipeline.service import get_release

    release_id = int(params.get("release_id") or 0)
    r = get_release(db, release_id)
    if not check_permission(db, current, "pipeline", r.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：取消流水线 #{r.pipeline_id} 的发布")
    return card(
        "confirm_cancel",
        f"确认取消 #{r.id}",
        {"release_id": r.id},
        f"确认取消正在执行的发布 #{r.id}（状态 {r.status}）？",
    )


def _propose_approve(db: Session, current, params: dict) -> dict:
    from app.core.deps import check_permission
    from app.modules.approval.service import pending_approval_for_reviewer
    from app.modules.pipeline.service import RELEASE_PENDING, get_release

    release_id = int(params.get("release_id") or 0)
    approved = bool(params.get("approved", True))
    comment = str(params.get("comment") or "").strip()
    r = get_release(db, release_id)
    # propose 阶段就按 confirm 阶段的口径校验，避免弹出一张点了必然失败的卡片
    if not check_permission(db, current, "group", r.group_id, "approve"):
        raise BizException.forbidden(f"无权限：审批分组 #{r.group_id} 下的发布")
    if r.status != RELEASE_PENDING:
        return {"error": f"发布 #{r.id} 当前状态 {r.status}，不在待审批中"}
    mine = pending_approval_for_reviewer(
        db,
        release_id=r.id,
        reviewer_id=current.id,
        is_admin=bool(getattr(current, "is_admin", False)),
    )
    if mine is None:
        return {"error": f"发布 #{r.id} 没有你可以处理的待审批，或已被其他审批人处理"}
    if not approved and not comment:
        return {"error": "驳回必须填写原因，请先问用户为什么驳回，再带上 comment 重新调用"}

    p = db.get(Pipeline, r.pipeline_id)
    detail = f"流水线 {p.name if p else r.pipeline_id} · 版本 {r.version or 'latest'} · 发起人 {r.trigger_by or '未知'}"
    return card(
        "confirm_approve",
        "确认通过审批" if approved else "确认驳回",
        {"release_id": r.id, "approved": approved, "comment": comment},
        f"{'通过' if approved else '驳回'}发布 #{r.id} 的审批（{detail}）。"
        + ("通过后立即进入执行队列，请确认。" if approved else f"\n驳回原因：{comment}"),
    )


def _create_release(db: Session, current, params: dict) -> dict:
    from app.core.deps import check_permission

    pipeline_id = params["pipeline_id"]
    if not check_permission(db, current, "pipeline", pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：对流水线 #{pipeline_id} 发起发布")

    pipe = get_pipeline(db, pipeline_id)
    release = create_release(
        db,
        pipeline_id=pipeline_id,
        version=params.get("version", ""),
        strategy=params.get("strategy", "rolling"),
        trigger_by="ai",
        operator_id=current.id,
        run_params=_release_run_params(pipe, params.get("deploy_manifest")),
    )
    # queued 表示不用审批，得自己把它拉起来。少了这一步，发布就一直停在「排队中」、
    # 一个任务都不会生成，而 queued 是算占着流水线的——这条流水线从此发不出新的东西，
    # 页面上还看不出任何异常。其它入口（页面、AI 确认卡片）都做了这一步，
    # 只有这个 skill 漏了
    err = ""
    if release.status == "queued":
        from app.modules.pipeline.service import try_execute_release

        release, err = try_execute_release(db, release.id)
    out = {"release_id": release.id, "status": release.status}
    if err:
        out["error"] = f"发布已创建但未能启动：{err}"
    return out


def _rollback(db: Session, current, params: dict) -> dict:
    from app.core.deps import check_permission
    from app.modules.pipeline.service import rollback_release, get_release

    release = get_release(db, params["release_id"])
    if not check_permission(db, current, "pipeline", release.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：对流水线 #{release.pipeline_id} 回滚")
    rb = rollback_release(db, params["release_id"], current.id)
    return {"release_id": rb.id, "status": rb.status}


def _run_pipeline(db: Session, current, params: dict) -> dict:
    from app.modules.pipeline.sub_pipeline import start_sub_pipeline

    pipeline_id = params.get("pipeline_id") or params.get("pipelineId")
    if not pipeline_id:
        raise BizException.bad_request("缺少 pipeline_id")
    release = start_sub_pipeline(
        db,
        user=current,
        pipeline_id=int(pipeline_id),
        project_id=params.get("project_id") or params.get("projectId"),
        params=params.get("params") or {},
        parent_pipeline_id=params.get("parent_pipeline_id"),
        parent_release_id=params.get("parent_release_id"),
    )
    return {"release_id": release.id, "status": release.status, "pipeline_id": release.pipeline_id}


# 一次最多摆给模型多少台。节点上百之后全量塞进上下文，又贵又容易让模型看花眼，
# 超出就让它拿关键字或组名再问一次
_MAX_LISTED_NODES = 40


def _node_matches(kw: str, node, groups: list[str]) -> bool:
    """关键字是否命中这台节点。kw 已转小写。"""
    if kw in (node.name or "").lower():
        return True
    if kw.isdigit():
        # 纯数字只认节点 id。否则 17 会命中 192.0.2.10，模型把 node_id 当 keyword
        # 时就会指错机器，或者反过来查空后宣称节点不存在。
        return int(kw) == int(getattr(node, "id", 0) or 0)
    if kw in (node.host or "").lower():
        return True
    return any(kw in g.lower() for g in groups)


def _explain_miss(db: Session, kw: str, allowed: set[int] | None, current) -> str:
    """查不到时说清到底是哪种「查不到」。

    「没权限」「没登记」「登记成了构建机」是三件完全不同的事，处理方式也不同，
    但对用户都长成「找不到这台机器」。之前一律按没权限报，管理员看到「请联系
    管理员授权」只会更糊涂——他就是管理员，这条路根本走不通。
    """
    from app.modules.agent.models import BuildAgent

    hit = list(
        db.scalars(
            select(BuildAgent).where(
                (BuildAgent.host.ilike(f"%{kw}%")) | (BuildAgent.name.ilike(f"%{kw}%"))
            )
        ).all()
    )
    if kw.isdigit():
        by_id = db.get(BuildAgent, int(kw))
        if by_id is not None and all(a.id != by_id.id for a in hit):
            hit.insert(0, by_id)

    if not hit:
        return (
            f"平台上没有登记过匹配「{kw}」的机器（机器名、IP 都比对过了）。"
            "它可能还没装 Agent，或者装了但没注册成功。"
            "请到「节点管理」页确认，别说这台机器不存在，也不要改推荐别的机器——"
            "目录对得上不等于就是用户要发的那台。"
        )

    builders = [a for a in hit if (a.role or "builder") != "node"]
    nodes = [a for a in hit if (a.role or "builder") == "node"]

    if nodes and allowed is not None:
        # 登记了、是节点、但不在这个人的授权范围内
        which = "、".join(f"{a.name}（{a.host or '无IP'}）" for a in nodes[:5])
        return (
            f"机器 {which} 已登记为部署节点，但不在你的下发权限范围内。"
            "往生产机写文件要单独授权，请让管理员在「权限管理 → 用户授权」里，"
            "授权范围选「节点组」或「指定节点」授给你。"
        )

    if builders:
        which = "、".join(f"{a.name}（{a.host or '无IP'}）" for a in builders[:5])
        return (
            f"匹配到 {which}，但它登记的是**构建机**，不是部署节点，不能往上面下发文件。"
            "构建机是跑编译的，部署节点才是发布目标。"
            "如果这台确实要当发布目标，需要在上面按「节点」角色重装 Agent"
            "（节点管理页有一键安装命令），装好后才能下发。"
        )

    # 是管理员（allowed is None）且匹配到了节点，却没进结果集：只可能是上面的
    # 精确 IP 收窄把它筛掉了，正常不会走到这里
    return f"没有匹配「{kw}」的节点，换个关键字或去掉关键字看全部。"


def _list_push_nodes(db: Session, current, params: dict) -> dict:
    """列出这个人能往上面下发文件的节点，可按名字或所属分组过滤。"""
    from app.core.deps import deployable_node_ids
    from app.modules.agent.models import BuildAgent, NodeGroup, NodeGroupMember
    from app.modules.ai.nodepush import node_allow_paths

    kw = str(params.get("keyword") or "").strip().lower()
    # 逐台 check_permission 在几百台时是几百次全表扫权限，这里一次算完
    allowed = deployable_node_ids(db, current)
    if allowed is not None and not allowed:
        # 指名道姓找某台时，先分清是「没权限」还是「这台压根没登记」——
        # 一句「你没有权限」会让人去找管理员要权限，而真相可能是 Agent 没装上
        if kw:
            return {"nodes": [], "hint": _explain_miss(db, kw, allowed, current)}
        return {
            "nodes": [],
            "hint": "你还没有任何节点的下发权限。往生产机写文件要单独授权，"
            "请让管理员在「权限管理 → 用户授权」里，授权范围选「节点组」或「指定节点」授给你。",
        }

    stmt = select(BuildAgent).where(BuildAgent.role == "node").order_by(BuildAgent.id)
    if allowed is not None:
        stmt = stmt.where(BuildAgent.id.in_(allowed))
    rows = db.scalars(stmt).all()

    names = {g.id: g.name for g in db.scalars(select(NodeGroup)).all()}
    belongs: dict[int, list[str]] = {}
    for gid, aid in db.query(NodeGroupMember.group_id, NodeGroupMember.agent_id).all():
        if gid in names:
            belongs.setdefault(aid, []).append(names[gid])

    out = []
    for n in rows:
        groups = sorted(belongs.get(n.id, []))
        # 关键字同时匹配机器名、IP 和组名。运维记 IP 比记机器名多，
        # 「传到 192.0.2.10」是很自然的说法；而「发到 O2O 生产那批机器」
        # 本来就是按组说的。少认一种，用户就会得到「没有这台节点」的假否定
        if kw and not _node_matches(kw, n, groups):
            continue
        out.append(
            {
                "id": n.id,
                "name": n.name,
                # 带上 IP：模型看不到的字段就等于不存在，它会据此断言「没有这个 IP 的节点」
                "host": n.host or "",
                "env": n.env or "prod",
                "status": n.status,
                "groups": groups,
                "allow_paths": node_allow_paths(n),
            }
        )
    # IP 完全命中时，把「前缀相同的邻居」剔掉：用户说 192.0.2.10，
    # 顺带列出 192.0.2.20 只会让人以为自己选错了机器
    if kw and out:
        exact = [d for d in out if (d["host"] or "").lower() == kw]
        if exact:
            out = exact

    if not out:
        if kw:
            return {"nodes": [], "hint": _explain_miss(db, kw, allowed, current)}
        return {
            "nodes": [],
            "hint": "你还没有任何节点的下发权限。往生产机写文件要单独授权，"
            "请让管理员在「权限管理 → 用户授权」里，授权范围选「节点组」或「指定节点」授给你。",
        }

    total = len(out)
    hint = "目标目录必须落在节点的 allow_paths 之内。"
    if total > _MAX_LISTED_NODES:
        out = out[:_MAX_LISTED_NODES]
        hint += (
            f" 共 {total} 台，这里只列了前 {_MAX_LISTED_NODES} 台。"
            "指代的是哪一批就用 keyword 再查一次（组名也能匹配），别在没列全的情况下替用户挑机器。"
        )
    return {"nodes": out, "total": total, "hint": hint}


def _propose_node_push(db: Session, current, params: dict) -> dict:
    from app.modules.ai.nodepush import build_card

    return build_card(db, current, params)


def load() -> None:
    register(Skill(
        name="list_push_nodes",
        description=(
            "列出有下发权限的部署节点（IP、可写目录、分组）。"
            "在问能往哪些机器传文件、部署节点、按 IP 找机器时调用。"
            "不是构建机（用 list_agents），也不是发布流水线。传文件前必须用它确认真实 node_id。"
        ),
        category="delivery",
        risk="read",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "按机器名、IP 或分组名过滤，如 o2o、生产、web、192.0.2.10",
                }
            },
        },
        handler=_list_push_nodes,
        examples=["查询全部节点", "我能往哪些节点传文件", "有哪些部署节点", "传到 192.0.2.10"],
    ))
    register(Skill(
        name="propose_node_push",
        description=(
            "把对话框已上传的文件下发到一台或多台节点的同一目录，出确认卡。"
            "在要传到某台机器、拷到节点、按 IP 下发时调用。"
            "不是发布流水线，也不是申请执行权。没有附件不可调用。"
        ),
        category="delivery",
        risk="destructive",
        confirm=True,
        parameters={
            "type": "object",
            "properties": {
                "node_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "目标节点 id，可多台，每台都发同一份",
                },
                "target_dir": {
                    "type": "string",
                    "description": (
                        "目标绝对目录，原样照抄用户给出的路径，如 D:\\wwwroot\\site。"
                        "严禁改写：不许截短成节点 allow_paths 的根目录，不许补子目录，"
                        "不许因为换了台机器就改目录。没说目录就问，别猜"
                    ),
                },
                "attachment_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "本次要下发的附件 id",
                },
            },
            "required": ["node_ids", "target_dir", "attachment_ids"],
        },
        handler=_propose_node_push,
        examples=["把这些文件传到 srvt4 的 D:\\wwwroot\\site 下", "拷到那台机器", "下发到节点"],
    ))
    register(Skill(
        name="propose_release",
        description=(
            "发起一次流水线发布，出确认卡，不会立刻执行。"
            "在要发布、部署、上线、发到某环境、跑流水线时调用。"
            "用户点名了要发哪些文件时把路径写入 deploy_manifest。"
            "不是申请执行权，也不是往节点拷文件。禁止编造 pipeline_id。"
        ),
        category="delivery",
        risk="write",
        confirm=True,
        parameters={
            "type": "object",
            "properties": {
                "pipeline_id": {
                    "type": "integer",
                    "description": "仅填用户可见且已确认的 id，不要猜测",
                },
                "pipeline": {"type": "string", "description": "流水线名称"},
                "project": {"type": "string", "description": "项目名称、代号或 id"},
                "env": {
                    "type": "string",
                    "description": "环境：prod/test/uat/staging/dev，或生产/测试/线上",
                },
                "query": {"type": "string", "description": "用户原话，抽槽不全时传入"},
                "version": {"type": "string"},
                "strategy": {"type": "string"},
                "deploy_manifest": {
                    "type": "string",
                    "description": (
                        "发布清单，一行一个相对路径或通配符，如 bin/*.dll、Areas/。"
                        "用户点名了要发哪些文件时填入。"
                        "增量包流水线步骤仍是占位时必须填写，未指定会拒绝发布，不会全量打包。"
                    ),
                },
            },
        },
        handler=_propose_release,
        examples=[
            "把订单服务发到测试",
            "帮我跑一下陪练的生产",
            "发布到生产 v1.2.3",
            "上线一下",
            "部署到测试",
            "把 bin/*.dll 和 Areas/ 发到生产",
        ],
    ))
    register(Skill(
        name="propose_rollback",
        description="按 release_id 生成回滚确认卡，把已成功的发布退回上一版。在要回滚、还原上一版时调用。不是停掉正在跑的发布（用 propose_cancel），也不是作废权限申请。",
        category="delivery",
        risk="destructive",
        confirm=True,
        parameters={"type": "object", "properties": {"release_id": {"type": "integer"}}, "required": ["release_id"]},
        handler=_propose_rollback,
        examples=["回滚上一笔发布", "还原上一版", "这次发错了撤回去"],
    ))
    register(Skill(
        name="propose_rebuild",
        description="按原 commit 再跑一次同一条发布，出确认卡。在失败后要 Rebuild、按原提交重跑时调用。不是回滚，也不是新开一条不同版本的发布。",
        category="delivery",
        risk="write",
        confirm=True,
        parameters={"type": "object", "properties": {"release_id": {"type": "integer"}}, "required": ["release_id"]},
        handler=_propose_rebuild,
        examples=["把失败的那次 Rebuild", "按原提交重跑", "再编一次"],
    ))
    register(Skill(
        name="propose_cancel",
        description=(
            "停掉正在跑的发布（收回、取消、中止都是同一件事），出确认卡。"
            "在要取消这次发布、停掉正在跑的流水线时调用。"
            "不是作废权限申请（那个用 cancel_access_application），也不是回滚已成功的版本。"
        ),
        category="delivery",
        risk="destructive",
        confirm=True,
        parameters={"type": "object", "properties": {"release_id": {"type": "integer"}}, "required": ["release_id"]},
        handler=_propose_cancel,
        examples=["取消这次发布", "停掉正在跑的流水线", "中止这次构建", "发布不要跑了"],
    ))
    register(Skill(
        name="propose_approve",
        description="对停在 pending 的生产发布通过或驳回，出确认卡。在要同意或驳回这次上线时调用。不是审批权限申请（那个用 propose_review_access）。驳回必须带 comment。",
        category="delivery",
        risk="write",
        confirm=True,
        parameters={
            "type": "object",
            "properties": {
                "release_id": {"type": "integer"},
                "approved": {"type": "boolean"},
                "comment": {"type": "string", "description": "审批意见；驳回时必填"},
            },
            "required": ["release_id"],
        },
        handler=_propose_approve,
        examples=["同意这次生产发布", "通过这条发布", "驳回这次上线"],
    ))
    register(Skill(
        name="create_release",
        description="创建发布（MCP 直调；对话走 propose_release）",
        category="delivery",
        risk="write",
        confirm=True,
        llm_visible=False,
        parameters={
            "type": "object",
            "properties": {"pipeline_id": {"type": "integer"}, "version": {"type": "string"}},
            "required": ["pipeline_id"],
        },
        handler=_create_release,
    ))
    register(Skill(
        name="rollback",
        description="执行回滚（MCP 直调，需 user_confirmed；对话走 propose_rollback）",
        category="delivery",
        risk="destructive",
        confirm=True,
        llm_visible=False,
        parameters={"type": "object", "properties": {"release_id": {"type": "integer"}}, "required": ["release_id"]},
        handler=_rollback,
    ))
    register(Skill(
        name="run_pipeline",
        description="启动一条流水线（MCP；可作子流水线。对话走 propose_release）",
        category="delivery",
        risk="write",
        confirm=True,
        llm_visible=False,
        parameters={"type": "object", "properties": {"pipeline_id": {"type": "integer"}}, "required": ["pipeline_id"]},
        handler=_run_pipeline,
    ))
