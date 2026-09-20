"""构建机与构建任务路由。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Body, Depends, File, Header, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    assert_visible_menu,
    get_current_admin,
    get_current_user,
    require_pipeline_visible,
    sees_menu,
)
from app.core.response import BizException, R
from app.db.session import SessionLocal, get_db
from app.modules.store import plugin_service
from app.modules.agent import task_service, task_token
from app.modules.agent.models import BuildAgent, BuildTask, NodeGroup, NodeGroupMember
from app.modules.agent.presence import HEARTBEAT_TIMEOUT_SECONDS, agent_is_online
from app.modules.agent.tokens import find_agent_by_presented_token, hash_agent_token, token_matches

router = APIRouter(tags=["构建机管理"])

# 心跳超时见 presence.HEARTBEAT_TIMEOUT_SECONDS。巡检间隔（秒）
CHECK_INTERVAL_SECONDS = 30
# 升级指令多久没落地就算卡住，允许重新下发
UPGRADE_STALE_MINUTES = 15
# Agent 鉴权 key 自动轮换周期（天）：到期后下一次心跳换发新 key，Agent 无感知更新
AGENT_TOKEN_ROTATE_DAYS = 7


def _authenticate_agent(db: Session, agent_id: int, token: str) -> BuildAgent:
    """校验 Agent 直连接口的 token：只有持正确 token 的 Agent 才能心跳/拉任务/上报。

    不存在和 token 错走同一句 401，避免用 404 枚举机器 id。
    轮换宽限期内新旧 token 都放行：换发了新 key 但 Agent 还没切过来时不至于被锁死。
    """
    a = db.get(BuildAgent, agent_id)
    if a is None or not token or not (
        token_matches(a.token, token) or token_matches(a.token_prev, token)
    ):
        raise BizException.unauthorized("Agent 鉴权失败")
    return a


def _rotate_agent_token(a: BuildAgent, presented_token: str) -> str | None:
    """定期轮换 Agent 鉴权 key，返回需要下发给 Agent 的新明文（无则 None）。

    库里 token / token_prev 只存哈希。新明文暂放 token_issued，直到心跳带着
    新 token 回来。这样「发了新 key 但 Agent 那一跳没收到」还能再补发一次。
    """
    from datetime import timedelta

    now = datetime.now()
    if a.token_rotated_at is None:
        # 老 Agent 首次心跳：先立个基线，本次不轮换，一个周期后再换
        a.token_rotated_at = now
        return None

    if a.token_prev:
        if token_matches(a.token, presented_token):
            a.token_prev = ""
            a.token_issued = ""
            return None
        return a.token_issued or None

    if a.token and (now - a.token_rotated_at) >= timedelta(days=AGENT_TOKEN_ROTATE_DAYS):
        new_plain = secrets.token_hex(32)
        a.token_prev = a.token
        a.token = hash_agent_token(new_plain)
        a.token_issued = new_plain
        a.token_rotated_at = now
        return new_plain
    return None


def _authenticate_agent_task(db: Session, agent_id: int, task_id: int) -> BuildTask:
    """确认这个任务确实是派给这台 Agent 的。

    只验 Agent 身份不够：任务号是连续的，一台被攻陷的构建机可以拿别人的 task_id
    上报「失败」，而 complete 会级联取消同一次发布的其余任务，等于能随手搞挂
    其它项目正在跑的发布。领取任务时就写了 agent_id，这里对上即可。
    """
    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")
    if task.agent_id != agent_id:
        raise BizException.forbidden("该任务不属于本构建机")
    return task


def _effective_status(agent: BuildAgent) -> str:
    """列表和接口用的展示状态，与派发任务时的在线判定同一套规则。

    DB 里只存最后一次心跳的状态，避免每次 list 都写库；后台巡检会定期把超时的写回 offline。
    """
    return "online" if agent_is_online(agent) else "offline"


def _stored_str_list(raw: str | None) -> list[str]:
    """把库里的 JSON 数组列读成字符串列表。坏数据当空，不抛。"""
    try:
        v = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _validate_node_allow_paths(raw) -> list[str]:
    """管理员改允许目录时的校验。节点还会再消一次毒，这里挡明显错误。

    空名单、盘符根、系统目录都不能过：空名单等于拆掉写盘闸门；
    盘符根等于把整台机器交出去。
    """
    if not isinstance(raw, list):
        raise BizException.bad_request("允许目录必须是字符串数组")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        p = str(item or "").strip()
        if not p:
            continue
        if ".." in p.replace("\\", "/").split("/"):
            raise BizException.bad_request(f"目录「{p}」不能包含 ..")
        looks_abs = ":" in p[:3] or p.startswith("/") or p.startswith("\\\\")
        if not looks_abs:
            raise BizException.bad_request(f"目录要写绝对路径，收到的是「{p}」")
        n = p.replace("\\", "/").rstrip("/")
        if n in ("", "/") or (len(n) == 2 and n[1] == ":"):
            raise BizException.bad_request(f"「{p}」是根目录，请指到具体站点目录")
        lowered = n.lower()
        for bad in (
            "c:/windows",
            "c:/program files",
            "c:/program files (x86)",
            "/etc",
            "/bin",
            "/sbin",
            "/usr",
            "/boot",
            "/sys",
            "/proc",
            "/dev",
            "/lib",
            "/lib64",
            "/root",
            "/var/run",
            "/tmp",
            "/var/tmp",
            "/dev/shm",
        ):
            if lowered == bad or lowered.startswith(bad + "/"):
                raise BizException.bad_request(f"「{p}」是系统目录，不能作为允许目录")
        # Linux 一层路径（/data、/mnt）等于把整块盘交出去；Windows 盘符根上面已经拒了
        if n.startswith("/") and n.count("/") == 1:
            raise BizException.bad_request(f"「{p}」是整块盘或系统第一层，请指到具体站点目录")
        if lowered not in seen:
            seen.add(lowered)
            out.append(p)
    if not out:
        raise BizException.bad_request("至少保留一条允许目录，空名单会让这台节点拒绝全部写操作")
    return out


def _to_dict(a: BuildAgent) -> dict:
    """把 BuildAgent 转成 dict，附加计算后的 effective_status（不写库）。"""
    d = {
        c.key: getattr(a, c.key) for c in a.__table__.columns
    }
    # token / token_prev 是这台机器的长期凭证，拿到就能冒充它领任务（任务体里带着
    # 仓库凭证）。列表页从来不需要它，别顺手把整行都倒出去
    d.pop("token", None)
    d.pop("token_prev", None)
    d.pop("token_issued", None)
    d["effective_status"] = _effective_status(a)
    # tags 沿用原来的 JSON 字符串（构建机页面按字符串解析，别改坏它）；
    # allow_paths 是新字段，直接给数组更好用
    d["allow_paths"] = _stored_str_list(a.allow_paths)
    d["allow_services"] = _stored_str_list(a.allow_services)
    d["latest_version"] = current_jar_version()
    d["outdated"] = bool(d["latest_version"] and a.agent_version and a.agent_version != d["latest_version"])
    d["upgrade_error"] = a.upgrade_error or ""
    # 版本落后的机器都在自动升级的路上，Agent 会等手上的活干完再换版本，所以
    # 「落后 + 在线 + 没报错」就是正常的升级中；报了错或掉线了才是真卡住
    d["upgrading"] = (
        d["outdated"] and d["effective_status"] == "online" and not d["upgrade_error"]
    )
    d["upgrade_stalled"] = d["outdated"] and not d["upgrading"]
    return d


# jar 指纹按 (路径, mtime, 大小) 缓存：每次 list agent 都重算一遍几十 MB 的 sha256 太浪费
_jar_sha_cache: tuple[tuple, str] | None = None
_jar_version_lock = threading.Lock()


def current_jar_sha256() -> str:
    """平台当前 deploy-agent.jar 的完整 sha256 hex；jar 不存在返回空串。"""
    global _jar_sha_cache
    path = _agent_asset_path("deploy-agent.jar")
    if path is None:
        return ""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path, st.st_mtime_ns, st.st_size)
    cached = _jar_sha_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    with _jar_version_lock:
        cached = _jar_sha_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        _jar_sha_cache = (key, sha)
        return sha


def current_jar_version() -> str:
    """平台当前 deploy-agent.jar 的指纹（sha256 前 12 位）；jar 不存在返回空串。"""
    sha = current_jar_sha256()
    return sha[:12] if sha else ""


def _heartbeat_check_loop() -> None:
    """后台巡检：每 30s 把超时没心跳的 online agent 写回 offline，
    并回收那些「机器已经没了、任务还挂着」和「压根没生成任务」的卡死执行。
    """
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        try:
            with SessionLocal() as db:
                threshold = datetime.now() - _timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
                stale = db.scalars(
                    select(BuildAgent).where(
                        BuildAgent.status == "online",
                        BuildAgent.last_heartbeat != None,  # noqa: E711
                        BuildAgent.last_heartbeat < threshold,
                    )
                ).all()
                if stale:
                    for a in stale:
                        a.status = "offline"
                    db.commit()
                    print(f"[agent-check] {len(stale)} agents → offline (心跳超时)")
        except Exception as e:  # noqa: BLE001
            print(f"[agent-check] 巡检异常: {e}")

        # 光把 agent 标成 offline 不够：它手上的任务没人会来上报结果，
        # 不收掉就永远停在 running，那条流水线也一直被占着
        try:
            n = task_service.reap_dead_agent_tasks(SessionLocal)
            if n:
                print(f"[agent-check] 回收 {n} 个机器已离线的任务")
        except Exception as e:  # noqa: BLE001
            print(f"[agent-check] 任务回收异常: {e}")

        # 拆任务时抛异常留下的「零任务却占着流水线」的发布，同样要收
        try:
            from app.modules.pipeline.service import rescue_stranded_releases

            with SessionLocal() as db:
                n = rescue_stranded_releases(db)
            if n:
                print(f"[agent-check] 清理 {n} 条没有构建任务、卡住不动的发布")
        except Exception as e:  # noqa: BLE001
            print(f"[agent-check] 卡死发布清理异常: {e}")


def _timedelta(**kw):
    from datetime import timedelta
    return timedelta(**kw)


def start_agent_heartbeat_check() -> None:
    """启动心跳巡检线程（main.py lifespan 调用）。"""
    t = threading.Thread(target=_heartbeat_check_loop, name="agent-heartbeat-check", daemon=True)
    t.start()


# ============================================================
# 构建机 CRUD
# ============================================================
@router.get("/agents", summary="构建机 / 节点列表")
def list_agents(
    role: str = "",
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    """role=builder 只看构建机，role=node 只看部署节点，不传则全都返回。"""
    stmt = select(BuildAgent).order_by(BuildAgent.id)
    if role in ("builder", "node"):
        stmt = stmt.where(BuildAgent.role == role)
    agents = db.scalars(stmt).all()

    # 所属分组一次查完。几百台节点时逐台查一遍，这个列表接口就废了
    groups = {g.id: g.name for g in db.scalars(select(NodeGroup)).all()}
    belongs: dict[int, list[dict]] = {}
    for gid, aid in db.query(NodeGroupMember.group_id, NodeGroupMember.agent_id).all():
        if gid in groups:
            belongs.setdefault(aid, []).append({"id": gid, "name": groups[gid]})

    out = []
    for a in agents:
        d = _to_dict(a)
        d["groups"] = sorted(belongs.get(a.id, []), key=lambda x: x["id"])
        out.append(d)
    return R.ok(out)


def _agent_asset_path(*parts: str) -> str | None:
    """在几种部署布局里找 agent 资源：源码运行 / Docker（WORKDIR=/app，代码在 /app/app）。"""
    here = os.path.abspath(__file__)
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    for base in (backend_dir, "/app", os.getcwd()):
        p = os.path.join(base, "agent-java", *parts)
        if os.path.isfile(p):
            return p
    return None


def _jdk_linux_tarball() -> str | None:
    """Linux 节点安装用的 JDK 包：数据卷优先，没有再从镜像里固化一份。"""
    from app.modules.agent import jdk_linux

    path = jdk_linux.find()
    return str(path) if path else None


def _authorize_agent_bootstrap(
    db: Session, enroll_token: str, authorization: str, agent_token: str = ""
) -> None:
    """下载 jar 的放行条件：能看见构建机/节点菜单的登录用户、持有接入凭证、或已登记的 Agent。

    接入凭证这条路是给目标机器上的安装脚本自己把 jar 拉下来——不必在生产机上敲平台密码。
    Agent token 这条路是给自升级用的：凭证会轮换，已登记的机器不该因此升不了级。
    """
    import hmac

    from app.core.deps import verify_token
    from app.modules.settings import get_setting

    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    user = verify_token(token, db) if token else None
    if user is not None and sees_menu(db, user, "agents", "nodes"):
        return

    expected = get_setting(db, "agent_enroll_token").strip()
    if expected and hmac.compare_digest((enroll_token or "").strip(), expected):
        return

    if agent_token and find_agent_by_presented_token(db, agent_token) is not None:
        return

    raise BizException.unauthorized(
        "需要登录账号或接入凭证。请在平台「构建机 / 节点管理」页面复制安装命令后重试"
    )


@router.get("/agents/download", summary="下载统一的 Agent jar 包")
def download_agent(
    db: Session = Depends(get_db),
    x_enroll_token: str = Header(default=""),
    x_agent_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """一个 jar 包到处部署，靠 --name/--role/--server 参数实例化为构建机或部署节点。"""
    _authorize_agent_bootstrap(db, x_enroll_token, authorization, x_agent_token)
    jar_path = _agent_asset_path("deploy-agent.jar")
    if jar_path is None:
        raise BizException.not_found(
            "Agent jar 包未构建或未打进镜像。请在 backend/agent-java 执行 build 后，"
            "确认 Dockerfile 已 COPY agent-java/deploy-agent.jar 并重建 backend 镜像"
        )
    sha = current_jar_sha256()
    headers = {"X-Checksum-SHA256": sha} if sha else None
    return FileResponse(
        jar_path,
        filename="deploy-agent.jar",
        media_type="application/java-archive",
        headers=headers,
    )


@router.get("/agents/jar-sha256", summary="当前 Agent jar 的 sha256")
def agent_jar_sha256(
    db: Session = Depends(get_db),
    x_enroll_token: str = Header(default=""),
    x_agent_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """安装脚本下载 jar 后用来对账。鉴权和下载接口相同，不能匿名。

    返回纯 hex，方便 curl | tr 直接比对，不必在生产机上解析 JSON。
    """
    _authorize_agent_bootstrap(db, x_enroll_token, authorization, x_agent_token)
    sha = current_jar_sha256()
    if not sha:
        raise BizException.not_found(
            "Agent jar 包未构建或未打进镜像。请在 backend/agent-java 执行 build 后重建 backend"
        )
    return PlainTextResponse(sha + "\n", media_type="text/plain")


@router.get("/agents/jdk-linux", summary="下载 Linux 节点 JDK 8 安装包")
def download_jdk_linux(
    db: Session = Depends(get_db),
    x_enroll_token: str = Header(default=""),
    x_agent_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """给安装脚本在目标机没有可用 Java 时拉 JDK。鉴权和 jar 相同，不能匿名下。"""
    _authorize_agent_bootstrap(db, x_enroll_token, authorization, x_agent_token)
    path = _jdk_linux_tarball()
    if path is None:
        raise BizException.not_found(
            "平台还没有 Linux JDK 包。请到「节点管理」上传 jdk-8u271-linux-x64.tar.gz，"
            "一次即可，重建平台也不会丢"
        )
    return FileResponse(
        path,
        filename=os.path.basename(path),
        media_type="application/gzip",
    )


@router.get("/agents/jdk-linux/status", summary="Linux 节点 JDK 包是否已就绪")
def jdk_linux_status(_: CurrentUser = Depends(get_current_user)):
    """节点管理页用来提示：有没有包、是不是已经落在数据卷里。"""
    from app.modules.agent import jdk_linux

    return R.ok(jdk_linux.status())


@router.post("/agents/jdk-linux", summary="上传 Linux 节点 JDK 包（写入数据卷）")
async def upload_jdk_linux(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """包进 /app/data/shared-packs/jdk-linux，跟制品同一份卷。重建镜像不会丢。"""
    assert_visible_menu(db, current, "nodes")
    from app.modules.agent import jdk_linux

    return R.ok(await jdk_linux.save_upload(file), message="JDK 包已保存，重建平台也不会丢")


INSTALL_SCRIPTS = {
    "node": "install-node.ps1",
    "node-linux": "install-node.sh",
    "builder": "install-agent.ps1",
    "builder-linux": "install-agent.sh",
}


def public_platform_url(request: Request, db: Session) -> str:
    """安装脚本里回填的平台地址。

    只信平台设置 public_app_base。没配才用本次请求的 Host，并且忽略
    X-Forwarded-Host——那个头客户端能伪造，写进脚本就是让机器连到攻击者。
    """
    from app.modules.settings import get_setting

    base = (get_setting(db, "public_app_base") or "").strip().rstrip("/")
    if base:
        return base
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
    proto = proto.split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme or "http"
    host = (request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        return ""
    return f"{proto}://{host}".rstrip("/")


@router.get("/agents/install-script", summary="下载 Agent 一键安装脚本")
def download_install_script(
    request: Request,
    role: str = "node",
    db: Session = Depends(get_db),
):
    """目标机器直接 `iwr 平台/install-script | 执行`，不用有人先把脚本传过去。

    脚本本身不含任何凭证，所以不设鉴权；真正的能力（下载 jar、注册）仍要接入凭证。
    平台地址只信 public_app_base，为空才用请求 Host，忽略 X-Forwarded-Host。
    """
    filename = INSTALL_SCRIPTS.get(role)
    if filename is None:
        raise BizException.bad_request(f"未知的安装脚本类型：{role}")
    path = _agent_asset_path("scripts", filename)
    if path is None:
        raise BizException.not_found(
            f"安装脚本 {filename} 不存在。请确认 Dockerfile 已 COPY agent-java/scripts 并重建镜像"
        )

    with open(path, encoding="utf-8-sig") as f:
        content = f.read()

    server = public_platform_url(request, db)
    if server:
        content = content.replace("__RELEASE_SERVER__", server)

    if filename.endswith(".ps1"):
        # PowerShell 5.1 不认无 BOM 的 UTF-8，中文注释会被按 GBK 解析成语法错误
        body = ("\ufeff" + content).encode("utf-8")
    else:
        # 强制 LF：仓库是在 Windows 上开发的，core.autocrlf 会把 .sh 检出成 CRLF，
        # 原样发到 Linux 上 bash 会对着每一行报 $'\r': command not found，
        # 而且报错位置和真正的问题毫不相干，查起来极其费劲
        body = content.replace("\r\n", "\n").encode()
    return Response(
        content=body,
        # 用 octet-stream 而不是 text/plain：老版本 PowerShell 的 Invoke-WebRequest
        # 遇到文本类型会按 charset 解码再用本地编码写盘，中文和 BOM 都可能被改掉
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/agents/enroll-token", summary="查看构建机接入凭证")
def get_enroll_token(
    db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)
):
    """生成安装命令需要明文凭证。能看见构建机或节点菜单即可查看；轮换仍仅管理员。"""
    assert_visible_menu(db, current, "agents", "nodes")
    from app.modules.settings import get_setting

    return R.ok({"token": get_setting(db, "agent_enroll_token")})


@router.post("/agents/enroll-token/rotate", summary="轮换构建机接入凭证")
def rotate_enroll_token(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_admin)):
    """轮换后旧凭证立刻失效，但已登记的构建机不受影响（它们用自己的 token 续期）。"""
    from app.modules.settings import update_settings

    token = secrets.token_hex(24)
    update_settings(db, {"agent_enroll_token": token})
    return R.ok({"token": token}, message="已轮换，新构建机接入需使用新凭证")


@router.post("/agents/register", summary="Agent 注册（Agent 启动时调用）")
def register_agent(
    body: dict,
    db: Session = Depends(get_db),
    x_enroll_token: str = Header(default=""),
    x_agent_token: str = Header(default=""),
):
    """Agent 自注册：按 name 查找或新建，返回 agent_id + 明文 token。

    注册相当于往任务队列里插一台能领取真实构建任务的机器，任务体里带着仓库凭证，
    所以必须持接入凭证。已登记过的构建机重启时带着自己的 token 回来，可直接续期，
    这样升级 Agent 不用每次都翻凭证。
    续期不得改 role：节点不能靠重装把自己升成构建机。改角色走管理员 PATCH。
    """
    import hmac

    from app.modules.settings import get_setting

    name = body.get("name", "")
    existing = db.scalar(select(BuildAgent).where(BuildAgent.name == name))

    renewing = (
        existing is not None
        and bool(x_agent_token)
        and (
            token_matches(existing.token, x_agent_token)
            or token_matches(existing.token_prev, x_agent_token)
        )
    )
    if not renewing:
        expected = get_setting(db, "agent_enroll_token").strip()
        if not expected or not hmac.compare_digest(x_enroll_token.strip(), expected):
            raise BizException.forbidden(
                "构建机接入凭证无效。请在平台「构建机」页面复制启动命令（含 --enroll-token）后重试"
            )

    role = "node" if str(body.get("role", "")).lower() == "node" else "builder"
    allow_paths = json.dumps(body.get("allow_paths") or [], ensure_ascii=False)
    allow_services = json.dumps(body.get("allow_services") or [], ensure_ascii=False)
    version = str(body.get("version") or "").strip()
    # 安装时可以带上环境，省得装完还要再去页面上标一次。默认 prod：
    # 空值按生产（多审一道）；写了非法码就拒绝，不再悄悄改成 prod——
    # 否则 ENV=uat 会被吃掉，这台 uat 机构建/下发都会跑到生产隔离域里
    from app.core.env import normalize_env

    reported_env = normalize_env(body.get("env"), default="prod", field="--env")

    issued_plain = ""
    if existing is not None:
        existing.host = body.get("host", existing.host)
        existing.os = body.get("os", existing.os)
        # 续期冻结 role：首次 enroll 仍可设。改角色只走管理员 PATCH。
        if not renewing:
            existing.role = role
        # 允许目录以页面为准。启动参数只在库里还是空的时候补上
        # （老节点升级后第一次带上这个字段）。已经保存过的，续期和重装都不能覆盖。
        if not _stored_str_list(existing.allow_paths):
            existing.allow_paths = allow_paths
        existing.allow_services = allow_services
        existing.tags = json.dumps(body.get("tags", []), ensure_ascii=False)
        existing.status = "online"
        existing.last_heartbeat = datetime.now()
        existing.agent_version = version
        # env 刻意不跟着重新注册更新：管理员在页面上把某台标成 test 之后，
        # Agent 重启或重装不该把它悄悄改回 prod（反过来更糟——把生产机改成免审批）。
        # 装的时候写错了就在页面上改，那里改完会进审计
        # 重新注册意味着刚换过 jar（或人工重装），升级指令使命已尽
        existing.upgrade_requested = False
        existing.upgrade_requested_at = None
        if not existing.token:
            issued_plain = secrets.token_hex(32)
            existing.token = hash_agent_token(issued_plain)
            existing.token_rotated_at = datetime.now()
        existing.token_prev = ""
        existing.token_issued = ""
        db.commit()
        db.refresh(existing)
        agent = existing
        return_token = issued_plain or x_agent_token
    else:
        issued_plain = secrets.token_hex(32)
        agent = BuildAgent(
            name=name,
            host=body.get("host", ""),
            os=body.get("os", "linux"),
            role=role,
            allow_paths=allow_paths,
            allow_services=allow_services,
            tags=json.dumps(body.get("tags", []), ensure_ascii=False),
            env=reported_env,
            status="online",
            last_heartbeat=datetime.now(),
            agent_version=version,
            token=hash_agent_token(issued_plain),
            token_rotated_at=datetime.now(),
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        return_token = issued_plain
    return R.ok({"agent_id": agent.id, "name": agent.name, "token": return_token})


@router.post("/agents/{agent_id}/heartbeat", summary="Agent 心跳上报")
def heartbeat(
    agent_id: int,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
    payload: dict | None = Body(default=None),
):
    """Agent 心跳；顺带回传是否需要自升级。

    body 可含 running_count / concurrency / version。
    """
    body = payload or {}
    a = _authenticate_agent(db, agent_id, x_agent_token)
    a.status = "online"
    a.last_heartbeat = datetime.now()
    # 定期轮换鉴权 key：到期换发新 token，随心跳回给 Agent，它落盘后无感知切换
    rotated = _rotate_agent_token(a, x_agent_token)
    reported = str(body.get("version") or "").strip()
    if reported and a.agent_version != reported:
        a.agent_version = reported
        # 版本变了说明升级已经落地，手动开关和上一轮的失败原因都该复位
        a.upgrade_requested = False
        a.upgrade_requested_at = None
        a.upgrade_error = ""
    # Agent 报上来的升级失败原因（下载失败、守护进程没换上包……）
    err = str(body.get("upgrade_error") or "").strip()[:500]
    if err != (a.upgrade_error or ""):
        a.upgrade_error = err
    db.commit()

    latest = current_jar_version()
    # 管理员手动点过「重试」：让 Agent 越过它自己设的失败熔断再试一次
    forced = _upgrade_in_flight(a)
    return R.ok(
        {
            "running_count": body.get("running_count"),
            "concurrency": body.get("concurrency"),
            "latest_version": latest,
            "should_upgrade": _should_upgrade(a, latest, forced),
            "force_upgrade": forced,
            "new_token": rotated,  # 非空表示 key 轮换了，Agent 需更新本地凭据
            # 节点允许目录以平台为准，随心跳推给正在跑的 Agent，不必重装
            "allow_paths": _stored_str_list(a.allow_paths),
        }
    )


def _upgrade_in_flight(a: BuildAgent) -> bool:
    """升级指令是否还在生效期内。

    过了这个时间还没换上版本，多半是 Agent 离线或下载失败卡住了，
    此时得放开按钮让管理员重发，否则开关一直挂着谁也动不了。
    """
    if not a.upgrade_requested:
        return False
    if a.upgrade_requested_at is None:
        return True
    return (datetime.now() - a.upgrade_requested_at).total_seconds() < UPGRADE_STALE_MINUTES * 60


# 同一台机器、同一个旧版本，两次自动升级指令之间至少隔这么久
UPGRADE_SIGNAL_COOLDOWN_SECONDS = 600
# {agent_id: (agent_version, 上次下发时刻)}。只为限流，丢了最多是多下发一次，
# 所以不值得为它加一列、也不必跨副本共享
_last_upgrade_signal: dict[int, tuple[str, float]] = {}
_upgrade_signal_lock = threading.Lock()


def _should_upgrade(a: BuildAgent, latest: str, forced: bool = False) -> bool:
    """版本落后就升级，构建机和节点一视同仁。

    节点跑在生产机上，早先设计成必须管理员点一次。实际用下来这个门槛只带来麻烦：
    一批节点要一台台点，点完还得盯着；而 Agent 本来就会等手上的发布跑完才换版本，
    换包又是守护进程在 JVM 起来之前做的，对正在进行的发布没有影响。

    真正的风险不在「自动」，而在换包失败后的循环：Agent 下载完就退出，守护进程换不上
    包，旧版本被拉起来，下个心跳又收到同样的指令……生产机上就是反复重启。新版 Agent
    靠换包回执自我熔断，但升级窗口期里跑的还是不认识回执的旧版，所以平台这边也限一下流。
    """
    if not latest or not a.agent_version or a.agent_version == latest:
        return False
    # 开发态跑的 agent（不是从 jar 启动）没法自我替换，别给它下指令
    if a.agent_version in ("dev", "unknown"):
        return False
    if forced:  # 管理员手动催的，绕过冷却
        return True
    now = time.time()
    with _upgrade_signal_lock:
        last = _last_upgrade_signal.get(a.id)
        # 版本变了说明上一轮成功了，重新计时；没变则说明还没换上，等冷却结束再催
        if last and last[0] == a.agent_version and now - last[1] < UPGRADE_SIGNAL_COOLDOWN_SECONDS:
            return False
        _last_upgrade_signal[a.id] = (a.agent_version, now)
    return True


@router.post("/agents/{agent_id}/upgrade", summary="催一下升级（版本落后时本来就会自动升）")
def request_upgrade(agent_id: int, db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_admin)):
    """手动重试。

    版本落后的 Agent 本来就会自己升级，这个接口是给「自动升级失败了想再推一把」用的：
    Agent 在换包失败后会自我熔断（避免反复重启），这里下发的标记能让它越过熔断再试一次。
    """
    a = db.get(BuildAgent, agent_id)
    if a is None:
        raise BizException.not_found("构建机")
    latest = current_jar_version()
    if not latest:
        raise BizException.bad_request("平台没有可用的 Agent jar，无法升级")
    if a.agent_version == latest:
        raise BizException.bad_request("已经是最新版本")
    if a.agent_version in ("dev", "unknown", ""):
        raise BizException.bad_request(
            "这台 Agent 没上报有效版本（可能还没升级到支持自升级的版本），请到机器上重跑安装脚本"
        )
    if _effective_status(a) != "online":
        raise BizException.bad_request(
            "这台机器当前离线，收不到升级指令。它多半是换包失败后没起来，"
            "请到机器上重跑安装脚本"
        )
    if _upgrade_in_flight(a):
        raise BizException.bad_request(
            f"刚催过了，Agent 会在手上的任务跑完后换版本。超过 {UPGRADE_STALE_MINUTES} 分钟还没换上再来重试"
        )
    a.upgrade_requested = True
    a.upgrade_requested_at = datetime.now()
    a.upgrade_error = ""  # 清掉上一轮的报错，好分辨这次重试的结果
    db.commit()
    # Agent 每 10s 心跳一次，会在下一次心跳时收到指令，等手上任务跑完再换版本
    return R.ok(message="已催促升级，Agent 会在当前任务跑完后重试")


@router.patch("/agents/{agent_id}", summary="修改构建机 / 节点")
def update_agent(
    agent_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    """目前开放环境标记、角色和节点允许目录。
    环境决定往这台机器传文件要不要审批；角色决定它是构建机还是部署节点。
    允许目录改完随心跳下发给正在跑的 Agent，不用重装。
    JDK / Maven 等安装目录写在对应构建插件步骤里，不配在构建机上。
    Agent 自己续期改不了 role，也改不了已经保存的允许目录。
    """
    a = db.get(BuildAgent, agent_id)
    if a is None:
        raise BizException.not_found("构建机")
    if "role" in body:
        role = "node" if str(body.get("role") or "").lower() == "node" else "builder"
        if a.role != role:
            from app.modules.audit.service import write as write_audit

            old = a.role
            a.role = role
            write_audit(
                db,
                "node.role.update",
                "node",
                a.id,
                f"节点 {a.name} 角色 {old} → {role}",
                user_id=current.id,
            )
    if "env" in body:
        from app.core.env import env_label, normalize_env

        env = normalize_env(body.get("env"), default="", field="环境")
        if not env:
            raise BizException.bad_request("环境不能为空")
        if a.env != env:
            from app.modules.audit.service import write as write_audit

            old = a.env
            a.env = env
            write_audit(
                db,
                "node.env.update",
                "node",
                a.id,
                f"节点 {a.name} 环境 {old}（{env_label(old)}） → {env}（{env_label(env)}）",
                user_id=current.id,
            )
    if "allow_paths" in body:
        paths = _validate_node_allow_paths(body.get("allow_paths"))
        dumped = json.dumps(paths, ensure_ascii=False)
        if a.allow_paths != dumped:
            from app.modules.audit.service import write as write_audit

            old = ", ".join(_stored_str_list(a.allow_paths)) or "（空）"
            a.allow_paths = dumped
            write_audit(
                db,
                "node.allow_paths.update",
                "node",
                a.id,
                f"节点 {a.name} 允许目录 {old} → {', '.join(paths)}",
                user_id=current.id,
                username=current.username,
            )
    db.commit()
    db.refresh(a)
    return R.ok(_to_dict(a))


@router.delete("/agents/{agent_id}", summary="删除构建机")
def delete_agent(agent_id: int, db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_admin)):
    from app.modules.auth.models import Permission

    a = db.get(BuildAgent, agent_id)
    if a is None:
        raise BizException.not_found("构建机")
    # 机器没了，挂在它身上的分组关系和授权也得跟着走。留着的话，下一台机器
    # 复用到同一个自增 id 时会凭空继承上一台的授权
    db.query(NodeGroupMember).filter(NodeGroupMember.agent_id == agent_id).delete()
    db.query(Permission).filter(
        Permission.resource_type == "node", Permission.resource_id == agent_id
    ).delete()
    db.delete(a)
    db.commit()
    return R.ok()


# ============================================================
# 节点分组（权限授在组上，机器进出组自动跟着变）
# ============================================================
def _group_to_dict(g: NodeGroup, member_ids: list[int]) -> dict:
    return {
        "id": g.id,
        "name": g.name,
        "description": g.description or "",
        "agent_ids": member_ids,
        "member_count": len(member_ids),
    }


def _members_by_group(db: Session, group_ids: list[int] | None = None) -> dict[int, list[int]]:
    """一次查出各组的成员，避免每组查一遍。"""
    q = db.query(NodeGroupMember.group_id, NodeGroupMember.agent_id)
    if group_ids is not None:
        if not group_ids:
            return {}
        q = q.filter(NodeGroupMember.group_id.in_(group_ids))
    out: dict[int, list[int]] = {}
    for gid, aid in q.all():
        out.setdefault(gid, []).append(aid)
    return out


@router.get("/node-groups", summary="节点分组列表")
def list_node_groups(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    groups = db.scalars(select(NodeGroup).order_by(NodeGroup.id)).all()
    members = _members_by_group(db)
    return R.ok([_group_to_dict(g, sorted(members.get(g.id, []))) for g in groups])


def _validate_members(db: Session, agent_ids: list[int]) -> list[int]:
    """成员必须是已登记的部署节点。构建机放进来没有意义——组权限只管下发文件。"""
    ids = sorted({int(x) for x in agent_ids or []})
    if not ids:
        return []
    rows = db.scalars(select(BuildAgent).where(BuildAgent.id.in_(ids))).all()
    found = {a.id: a for a in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise BizException.bad_request(f"节点 {missing} 不存在")
    not_node = [found[i].name for i in ids if (found[i].role or "builder") != "node"]
    if not_node:
        raise BizException.bad_request(f"{not_node} 不是部署节点，不能加入节点组")
    return ids


def _set_members(db: Session, group_id: int, agent_ids: list[int]) -> None:
    """把组成员设成给定集合（增量改，不是先清空再插，省得中途失败丢干净）。"""
    current = {
        aid
        for (aid,) in db.query(NodeGroupMember.agent_id)
        .filter(NodeGroupMember.group_id == group_id)
        .all()
    }
    target = set(agent_ids)
    for aid in target - current:
        db.add(NodeGroupMember(group_id=group_id, agent_id=aid))
    gone = current - target
    if gone:
        db.query(NodeGroupMember).filter(
            NodeGroupMember.group_id == group_id, NodeGroupMember.agent_id.in_(gone)
        ).delete(synchronize_session=False)


@router.post("/node-groups", summary="新建节点分组")
def create_node_group(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    from app.modules.audit.service import write as write_audit

    name = str(body.get("name") or "").strip()
    if not name:
        raise BizException.bad_request("请填写分组名称")
    if db.scalar(select(NodeGroup).where(NodeGroup.name == name)) is not None:
        raise BizException.bad_request(f"分组「{name}」已存在")
    ids = _validate_members(db, body.get("agent_ids") or [])

    g = NodeGroup(name=name, description=str(body.get("description") or "").strip()[:255])
    db.add(g)
    db.flush()
    _set_members(db, g.id, ids)
    write_audit(
        db, "node_group.create", "node_group", g.id,
        f"新建节点组 {name}，成员 {ids}", user_id=current.id,
    )
    db.commit()
    db.refresh(g)
    return R.ok(_group_to_dict(g, ids))


@router.patch("/node-groups/{group_id}", summary="修改节点分组")
def update_node_group(
    group_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    """改名、改描述、改成员。成员变了等于一批人的下发范围跟着变，所以要留痕。"""
    from app.modules.audit.service import write as write_audit

    g = db.get(NodeGroup, group_id)
    if g is None:
        raise BizException.not_found("节点组")

    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise BizException.bad_request("请填写分组名称")
        dup = db.scalar(select(NodeGroup).where(NodeGroup.name == name, NodeGroup.id != group_id))
        if dup is not None:
            raise BizException.bad_request(f"分组「{name}」已存在")
        g.name = name
    if "description" in body:
        g.description = str(body.get("description") or "").strip()[:255]

    members = sorted({
        aid
        for (aid,) in db.query(NodeGroupMember.agent_id)
        .filter(NodeGroupMember.group_id == group_id)
        .all()
    })
    if "agent_ids" in body:
        ids = _validate_members(db, body.get("agent_ids") or [])
        if set(ids) != set(members):
            added = sorted(set(ids) - set(members))
            removed = sorted(set(members) - set(ids))
            _set_members(db, group_id, ids)
            write_audit(
                db, "node_group.members", "node_group", group_id,
                f"节点组 {g.name} 成员变更：加入 {added}，移出 {removed}", user_id=current.id,
            )
        members = ids

    db.commit()
    db.refresh(g)
    return R.ok(_group_to_dict(g, members))


@router.delete("/node-groups/{group_id}", summary="删除节点分组")
def delete_node_group(
    group_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    """连同挂在这个组上的授权一起删。

    留着授权行的话，页面上会显示成「节点组#7」这种查无此物的记录，而且一旦
    新组复用到同一个 id，那批人就凭空拿到了新组的下发权。
    """
    from app.modules.audit.service import write as write_audit
    from app.modules.auth.models import Permission

    g = db.get(NodeGroup, group_id)
    if g is None:
        raise BizException.not_found("节点组")
    revoked = (
        db.query(Permission)
        .filter(Permission.resource_type == "node_group", Permission.resource_id == group_id)
        .delete()
    )
    db.query(NodeGroupMember).filter(NodeGroupMember.group_id == group_id).delete()
    name = g.name
    db.delete(g)
    write_audit(
        db, "node_group.delete", "node_group", group_id,
        f"删除节点组 {name}，同时撤销 {revoked} 条授权", user_id=current.id,
    )
    db.commit()
    return R.ok(
        {"revoked": revoked},
        message=f"已删除节点组「{name}」" + (f"，并撤销 {revoked} 条相关授权" if revoked else ""),
    )


# ============================================================
# 构建任务（Agent 拉模式）
# ============================================================
@router.get("/agents/{agent_id}/tasks", summary="Agent 拉取待执行任务")
def fetch_task(
    agent_id: int,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    a = _authenticate_agent(db, agent_id, x_agent_token)
    task = task_service.fetch_task(db, agent_id)
    if task is None:
        return R.ok(None)
    steps = plugin_service.attach_package_meta(db, json.loads(task.steps_json or "[]"))
    return R.ok({
        "task_id": task.id,
        "release_id": task.release_id,
        "pipeline_id": task.pipeline_id,
        "workspace": task.workspace,  # p-{pipeline.workspace_uuid}
        "stage_name": task.stage_name,
        "job_id": task.job_id,
        "job_name": task.job_name,
        "steps": steps,
        "variables": json.loads(task.variables_json or "{}"),
        # 给插件子进程用的凭证，只能代表这一个任务，别把 Agent token 给插件
        "task_token": task_token.issue(task.id),
    })


@router.post("/agents/{agent_id}/tasks/{task_id}/wait-cancel", summary="Agent 长轮询等待取消信号")
def wait_cancel(
    agent_id: int,
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    """Agent 在执行 task 时持续轮询此接口，最多 hold `timeout` 秒。

    - 若 release 状态变为 cancelled / failed → 立即返回 {cancelled:true, reason}
    - 若 task 本身被 mark 为 cancelled → 也立即返回
    - 否则 sleep 至 timeout，返回 {cancelled:false}（Agent 继续跑下一次轮询）

    这是 JDK 8 无依赖约束下能做的"主动推送"——比 SSE / WebSocket 更简单，agent 后台
    守护线程持有长轮询，主进程跑 step，主进程被 kill 后守护线程也死掉，自动清理。
    """
    from app.modules.pipeline.models import Release

    _authenticate_agent(db, agent_id, x_agent_token)
    task = db.get(BuildTask, task_id)
    if task is None:
        return R.ok({"cancelled": True, "reason": "task_not_found"})
    if task.agent_id != agent_id:
        # 不是派给你的任务，就当它已取消，让这台机器停手；比 403 温和，
        # Agent 侧不用为这种情况单独写分支
        return R.ok({"cancelled": True, "reason": "not_owned"})
    if task.status == "cancelled":
        return R.ok({"cancelled": True, "reason": "task_cancelled"})

    release = db.get(Release, task.release_id)
    if release is None or release.status in ("cancelled", "failed", "rolled_back"):
        return R.ok({"cancelled": True, "reason": release.status or "release_missing"})

    timeout = int(body.get("timeout", 30)) if body else 30
    timeout = max(1, min(timeout, 60))  # 上限 60s，避免占连接

    # 短轮询循环：每 1s 查一次，发现终止立即返回
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 重新查 DB（短会话 + 短间隔，开销可接受）
        db.refresh(task)
        if task.status == "cancelled":
            return R.ok({"cancelled": True, "reason": "task_cancelled"})
        if release is not None:
            db.refresh(release)
        if release is None or release.status in ("cancelled", "failed", "rolled_back"):
            return R.ok({"cancelled": True, "reason": release.status or "release_missing"})
        time.sleep(1.0)
    return R.ok({"cancelled": False})


@router.post("/agents/{agent_id}/tasks/{task_id}/log", summary="Agent 上报日志（单行，兼容）")
def report_log(
    agent_id: int,
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    _authenticate_agent(db, agent_id, x_agent_token)
    _authenticate_agent_task(db, agent_id, task_id)
    line = body.get("line", "") or body.get("log", "")
    task_service.report_log(db, SessionLocal, task_id, line)
    return R.ok()


@router.post("/agents/{agent_id}/tasks/{task_id}/log/batch", summary="Agent 批量上报日志")
def report_log_batch(
    agent_id: int,
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    _authenticate_agent(db, agent_id, x_agent_token)
    _authenticate_agent_task(db, agent_id, task_id)
    clipped = task_service.clip_log_batch(body.get("lines") or [])
    task_service.report_log_batch(db, SessionLocal, task_id, clipped)
    return R.ok({"count": len(clipped)})


@router.post("/agents/{agent_id}/tasks/{task_id}/step", summary="Agent 上报单个步骤的状态与耗时")
def report_step(
    agent_id: int,
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    """每个步骤开始和结束各报一次，页面据此显示逐步耗时而不是 Job 总耗时。"""
    _authenticate_agent(db, agent_id, x_agent_token)
    _authenticate_agent_task(db, agent_id, task_id)
    task_service.report_step_result(
        db,
        task_id,
        index=int(body.get("index", 0)),
        plugin=str(body.get("plugin") or ""),
        status=str(body.get("status") or ""),
        started_at_ms=int(body.get("started_at_ms") or 0),
        duration_ms=int(body.get("duration_ms") or 0),
    )
    return R.ok()


@router.post("/agents/{agent_id}/tasks/{task_id}/deployment", summary="Agent 上报部署痕迹（回滚凭据）")
def report_deployment(
    agent_id: int,
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    """部署步骤成功后上报「怎么撤销我」。

    没有这条记录，回滚就只能靠重新构建再发一遍，慢且不一定还构建得出来。
    有了它，回滚就是拿着备份目录 / 上一个镜像 tag 反向执行一次，秒级完成。
    """
    from app.modules.deployment import service as deployment_service

    _authenticate_agent(db, agent_id, x_agent_token)
    task = _authenticate_agent_task(db, agent_id, task_id)
    row = deployment_service.record_deployment(
        db,
        release_id=task.release_id,
        pipeline_id=task.pipeline_id,
        task_id=task_id,
        agent_id=agent_id,
        step_index=int(body.get("step_index") or 0),
        kind=str(body.get("kind") or ""),
        payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
        target=str(body.get("target") or ""),
        summary=str(body.get("summary") or ""),
    )
    return R.ok({"id": row.id})


@router.post(
    "/agents/{agent_id}/tasks/{task_id}/deployment/{record_id}/undone",
    summary="Agent 上报某条部署已被撤销",
)
def report_deployment_undone(
    agent_id: int,
    task_id: int,
    record_id: int,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    """回滚步骤跑完后销账，防止同一份备份被撤销两次把现场搞乱。"""
    from app.modules.deployment import service as deployment_service

    _authenticate_agent(db, agent_id, x_agent_token)
    task = _authenticate_agent_task(db, agent_id, task_id)
    deployment_service.mark_undone(db, record_id, task.release_id)
    return R.ok()


@router.get(
    "/agents/{agent_id}/tasks/{task_id}/deploy-package", summary="Agent 下载本次发布的增量包"
)
def download_deploy_package(
    agent_id: int,
    task_id: int,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    """只能下载自己正在执行的那个任务对应的包，拿 ID 遍历不到别人的制品。"""
    from fastapi.responses import FileResponse

    from app.modules.artifact import storage
    from app.modules.artifact.service import find_release_package

    _authenticate_agent(db, agent_id, x_agent_token)
    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")
    if task.agent_id != agent_id:
        raise BizException.forbidden("该任务不属于本构建机")
    pkg = find_release_package(db, task.release_id)
    if pkg is None:
        raise BizException.bad_request("本次发布没有可部署的增量包，请检查打包步骤是否执行成功")
    return FileResponse(
        storage.resolve(pkg.storage_key), filename=pkg.name, media_type="application/zip"
    )


@router.get("/agents/{agent_id}/tasks/{task_id}/status", summary="Agent 查询任务取消状态（短轮询）")
def task_cancel_status(
    agent_id: int,
    task_id: int,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    """替代 wait-cancel 长轮询：Agent 每 1～2s GET 一次，降低占连接。"""
    from app.modules.pipeline.models import Release

    _authenticate_agent(db, agent_id, x_agent_token)
    task = db.get(BuildTask, task_id)
    if task is None:
        return R.ok({"cancelled": True, "reason": "task_not_found", "status": "missing"})
    # 只能查自己领到的任务。不校验的话，任意一台构建机的 token 就能拿来遍历
    # 全平台的任务进度——别的项目在发什么、发到哪一步，一问便知
    if task.agent_id is not None and task.agent_id != agent_id:
        raise BizException.forbidden("该任务不属于本构建机")
    if task.status == "cancelled":
        return R.ok({"cancelled": True, "reason": "task_cancelled", "status": task.status})
    release = db.get(Release, task.release_id)
    if release is None or release.status in ("cancelled", "failed", "rolled_back"):
        return R.ok({
            "cancelled": True,
            "reason": (release.status if release else "release_missing"),
            "status": task.status,
        })
    return R.ok({"cancelled": False, "status": task.status})


@router.post("/agents/{agent_id}/tasks/{task_id}/complete", summary="Agent 上报任务完成")
def complete_task(
    agent_id: int,
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    _authenticate_agent(db, agent_id, x_agent_token)
    _authenticate_agent_task(db, agent_id, task_id)
    success = bool(body.get("success", True))
    source_ref = (body.get("source_ref") or "").strip() or None
    task = task_service.complete_task(db, task_id, success, source_ref=source_ref)
    # 同步更新对应 release 的任务聚合状态（由 pipeline 模块处理）
    _maybe_sync_release(db, task)
    return R.ok({"task_id": task_id, "status": task.status})


@router.get("/tasks", summary="构建任务列表")
def list_tasks(
    release_id: int | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """按发布查任务。必须指名 release_id：不限定就是「把全平台构建任务倒给你」，
    而任务体里带着仓库凭证和内网地址，没有哪个页面需要这种查询。
    """
    from app.core.deps import visible_pipeline_ids
    from app.modules.pipeline.models import Release

    if not release_id:
        raise BizException.bad_request("请指定 release_id")

    release = db.get(Release, release_id)
    if release is None:
        raise BizException.not_found("发布记录")
    require_pipeline_visible(db, current, release.pipeline_id)

    tasks = list(
        db.scalars(
            select(BuildTask).where(BuildTask.release_id == release_id).order_by(BuildTask.id)
        ).all()
    )
    visible = visible_pipeline_ids(db, current)

    out = []
    for t in tasks:
        # 同一次发布里可能有子流水线派生的任务，逐条再确认一次可见性
        if visible is not None and t.pipeline_id not in visible:
            continue
        d = {c.key: getattr(t, c.key) for c in t.__table__.columns}
        d["steps_json"] = json.dumps(
            task_service.mask_secrets(json.loads(t.steps_json or "[]")), ensure_ascii=False
        )
        # 变量里一样会有密码、API Key（流水线自定义变量），跟 steps 一个待遇。
        # 只抹 steps 不抹 variables，等于把密钥从前门挡了、后门敞着
        try:
            d["variables_json"] = json.dumps(
                task_service.mask_secrets(json.loads(t.variables_json or "{}")),
                ensure_ascii=False,
            )
        except (TypeError, json.JSONDecodeError):
            d["variables_json"] = "{}"
        # 日志不塞在列表里：调用方每次只看一个步骤，内嵌就是把整次发布所有任务的
        # 完整日志都传一遍再扔掉，后端还要为每个任务各查一次 ES。
        # 要日志走 /tasks/{id}/logs（单次）或 /tasks/{id}/logs/stream（增量）
        d.pop("logs", None)
        out.append(d)
    return R.ok(out)


@router.get("/tasks/{task_id}/logs", summary="获取任务日志")
def get_task_logs(
    task_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")
    require_pipeline_visible(db, current, task.pipeline_id)
    logs = task_service.get_task_logs(db, SessionLocal, task_id)
    return R.ok({"task_id": task_id, "logs": logs, "storage": _current_storage(db)})


# 历史日志回放的分块大小：太小起不到合并效果，太大则首屏要等整块拼完才显示
_REPLAY_CHUNK = 500


@router.post("/tasks/{task_id}/logs/stream-token", summary="换取任务日志 SSE 短时票")
def issue_task_log_stream_token(
    task_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """用登录 JWT 换 60 秒 stream_token。EventSource 只能把票放在 URL 上。"""
    from app.modules.agent.stream_token import STREAM_TTL_SECONDS, issue_stream_token

    task = db.get(BuildTask, task_id)
    if task is None:
        raise BizException.not_found("构建任务")
    require_pipeline_visible(db, current, task.pipeline_id)
    return R.ok({"stream_token": issue_stream_token(current, task_id=task_id), "expires_in": STREAM_TTL_SECONDS})


@router.get("/tasks/{task_id}/logs/stream", summary="任务日志实时流（SSE）")
def stream_task_logs(
    task_id: int,
    stream_token: str = Query("", description="短时票，由 /logs/stream-token 签发"),
    last_id: str = Query(default="0-0", alias="from"),
):
    """SSE：先回放历史，再 XREAD BLOCK 推增量（目标 100～300ms）。只接受 stream_token。"""
    from app.core.deps import check_permission
    from app.modules.agent.log_store import RedisStreamLogStore, STREAM_KEY, create_log_store
    from app.modules.agent.stream_token import user_from_stream_token
    from app.modules.settings import get_all_settings

    user = user_from_stream_token(stream_token, task_id=task_id)

    with SessionLocal() as db:
        task = db.get(BuildTask, task_id)
        if task is None:
            raise BizException.not_found("构建任务")
        if not check_permission(db, user, "pipeline", task.pipeline_id, "read"):
            raise BizException.forbidden(f"无权限：查看流水线 #{task.pipeline_id} 日志")
        store = create_log_store(SessionLocal, lambda: get_all_settings(db))

    stream_key = STREAM_KEY.format(task_id=task_id)

    def event_gen():
        cursor = last_id or "0-0"
        sent = 0  # 已回放的行数，非 Redis 模式靠它判断哪些是新增的

        def emit_lines(rows: list[str], event_id: str = "") -> None:
            # 历史日志一次几千行，一行一个事件的话浏览器要处理几千次 onmessage
            for i in range(0, len(rows), _REPLAY_CHUNK):
                chunk = rows[i:i + _REPLAY_CHUNK]
                payload = {"lines": chunk}
                if event_id:
                    payload["id"] = event_id
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def snapshot() -> list[str]:
            """读当前可见日志。ES 刚归档时 count 已涨、search 还没刷新，少的那截
            在任务结束后不会再进 Redis，必须多读一遍，否则 SSE 直接 done，前端永远缺页。"""
            rows = store.get(task_id) or []
            try:
                total = int(store.count(task_id) or 0)
            except Exception:  # noqa: BLE001
                return rows
            visible = sum(1 for x in rows if not str(x).startswith("…"))
            if visible >= total:
                return rows
            time.sleep(0.35)
            return store.get(task_id) or rows

        def catchup() -> list[str]:
            extra: list[str] = []
            for attempt in range(4):
                rows = snapshot()
                if len(rows) > sent:
                    extra = rows[sent:]
                    break
                try:
                    total = int(store.count(task_id) or 0)
                except Exception:  # noqa: BLE001
                    break
                if total <= sent:
                    break
                if attempt < 3:
                    time.sleep(0.3)
            return extra

        # 1) 历史回放：ES 已归档部分 + Redis 未删尾巴。实时增量再 XREAD。
        try:
            lines: list[str] = []
            if isinstance(store, RedisStreamLogStore):
                if not last_id or last_id == "0-0":
                    lines = snapshot()
                    cursor = store.latest_id(task_id)
                else:
                    cursor, lines = store.read_since(task_id, last_id=cursor, count=2000)
            else:
                lines = snapshot()
            yield from emit_lines(lines, cursor)
            sent = len(lines)
        except Exception as e:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"

        # 2) 实时增量
        while True:
            try:
                from app.core.redis_client import get_redis_blocking, redis_available

                if redis_available() and isinstance(store, RedisStreamLogStore):
                    r = get_redis_blocking(3)
                    resp = r.xread({stream_key: cursor if cursor != "0-0" else "0-0"}, block=2500, count=100)
                    if not resp:
                        yield ": keepalive\n\n"
                        with SessionLocal() as sdb:
                            t = sdb.get(BuildTask, task_id)
                            if t and t.status in ("success", "failed", "cancelled", "timeout"):
                                leftover = catchup()
                                if leftover:
                                    yield from emit_lines(leftover, cursor)
                                    sent += len(leftover)
                                yield f"event: done\ndata: {json.dumps({'status': t.status})}\n\n"
                                return
                        continue
                    for _key, entries in resp:
                        for eid, fields in entries:
                            cursor = eid
                            line = fields.get("line", "")
                            sent += 1
                            yield f"id: {eid}\ndata: {json.dumps({'line': line, 'id': eid}, ensure_ascii=False)}\n\n"
                else:
                    time.sleep(1.0)
                    with SessionLocal() as sdb:
                        t = sdb.get(BuildTask, task_id)
                        # 只发比上次多出来的部分。原先固定重发末尾 50 行，
                        # 前端是追加语义，等于每秒往日志尾巴上再抄一遍
                        current = store.get(task_id)
                        if len(current) > sent:
                            fresh = current[sent:]
                            sent = len(current)
                            yield (
                                "data: "
                                + json.dumps({"lines": fresh}, ensure_ascii=False)
                                + "\n\n"
                            )
                        if t and t.status in ("success", "failed", "cancelled", "timeout"):
                            leftover = catchup()
                            if leftover:
                                yield from emit_lines(leftover)
                                sent += len(leftover)
                            yield f"event: done\ndata: {json.dumps({'status': t.status})}\n\n"
                            return
                    yield ": keepalive\n\n"
            except GeneratorExit:
                return
            except Exception as e:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
                time.sleep(1.0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _current_storage(db: Session) -> str:
    from app.modules.settings import get_all_settings
    return get_all_settings(db).get("log_storage", "es")


def _persist_release_outputs(db: Session, release) -> None:
    """发布结束时把启动参数（含默认值）写入 outputs_json，供父流水线命名空间读取。"""
    try:
        from app.modules.pipeline.sub_pipeline import collect_release_outputs

        outputs = collect_release_outputs(db, release)
        release.outputs_json = json.dumps(outputs, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _mark_rollback_target(db: Session, release) -> None:
    """回滚跑完了，把被撤销的那次发布标成「已回滚」。

    否则历史列表里它还挂着绿色的「成功」，看不出线上已经不是这个版本了，
    下次有人照着它排查问题会被带偏。
    """
    from app.modules.pipeline.models import Release

    target_id = getattr(release, "rollback_of_release_id", None)
    if not target_id:
        return
    target = db.get(Release, target_id)
    if target is not None and target.status in ("success", "failed"):
        target.status = "rolled_back"
    # 节点销账失败时记录仍是未撤销，这里再盖一次，防止同一份备份被再撤一次
    from app.modules.deployment import service as deployment_service

    deployment_service.mark_release_records_undone(db, target_id, release.id)


def _maybe_sync_release(db: Session, task: BuildTask) -> None:
    """当 release 下所有任务都完成后，更新 release 状态。

    守卫：cancelled / rolled_back 是用户主动操作的结果，不能被自动聚合覆盖。
    即"步骤失败 → 整个流水线 cancelled"流程中，后失败的任务也允许上报 failed，
    但 release 状态保持 cancelled（尊重用户的取消意图），仅补 finished_at。
    """
    from app.modules.pipeline.models import Release

    release = db.get(Release, task.release_id)
    if release is None:
        return
    tasks = db.scalars(
        select(BuildTask).where(BuildTask.release_id == task.release_id)
    ).all()
    if not tasks:
        return

    # 取消状态保护：已经 cancelled / rolled_back 的 release 不再被自动聚合
    if release.status in ("cancelled", "rolled_back"):
        # 仅在没有 finished_at 时补一个（让审计时间完整）
        if release.finished_at is None and all(
            t.status in ("success", "failed", "cancelled") for t in tasks
        ):
            release.finished_at = datetime.now()
            db.commit()
        return

    statuses = {t.status for t in tasks}
    if all(s in ("success",) for s in statuses):
        release.status = "success"
        release.finished_at = datetime.now()
        _persist_release_outputs(db, release)
        _mark_rollback_target(db, release)
        db.commit()
        from app.modules.notify import on_release_finished

        on_release_finished(release.id)
        return
    elif any(s in ("failed", "timeout") for s in statuses):
        release.status = "failed"
        release.finished_at = datetime.now()
        _persist_release_outputs(db, release)
        if not (release.error_message or "").strip():
            failed = next(
                (t for t in tasks if t.status in ("failed", "timeout")),
                None,
            )
            if failed is not None:
                snippet = task_service.failed_task_error_snippet(db, failed.id)
                if snippet:
                    release.error_message = snippet[:512]
        db.commit()
        from app.modules.notify import on_release_finished

        on_release_finished(release.id)
        return
    elif any(s == "running" for s in statuses):
        release.status = "running"
    db.commit()


@router.post("/agents/{agent_id}/sub-pipelines/run", summary="Agent 启动子流水线")
def agent_run_sub_pipeline(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    """插件在构建机上调用：权限取父发布操作人，并做循环检测。"""
    _authenticate_agent(db, agent_id, x_agent_token)
    from app.modules.pipeline.models import Release
    from app.modules.pipeline.sub_pipeline import (
        release_status_payload,
        start_sub_pipeline,
        user_from_operator,
    )

    parent_release_id = body.get("parent_release_id") or body.get("parentReleaseId")
    operator_id = None
    if parent_release_id:
        parent = db.get(Release, int(parent_release_id))
        if parent is None:
            raise BizException.not_found("父发布")
        # Agent token 只能证明「我是某台构建机」，证明不了「这条父发布是我在跑」。
        # 不核实的话，拿任意一台机器的 token 挑一条历史 release 当父级，就能以那条
        # 发布操作人的身份、跳过生产审批把子流水线发出去。要求本机确实有一个
        # 正在执行的、属于这条父发布的任务——和插件走任务级 token 是同一个强度
        owns_parent = db.scalar(
            select(BuildTask.id)
            .where(
                BuildTask.release_id == int(parent_release_id),
                BuildTask.agent_id == agent_id,
                BuildTask.status == "running",
            )
            .limit(1)
        )
        if owns_parent is None:
            raise BizException.forbidden("本构建机没有在执行该父发布的任务，不能以它的名义启动子流水线")
        operator_id = parent.operator_id
    user = user_from_operator(db, operator_id)
    pipeline_id = body.get("pipeline_id") or body.get("pipelineId")
    if not pipeline_id:
        raise BizException.bad_request("缺少 pipeline_id")
    release = start_sub_pipeline(
        db,
        user=user,
        pipeline_id=int(pipeline_id),
        project_id=body.get("project_id") or body.get("projectId"),
        params=body.get("params") or {},
        parent_pipeline_id=body.get("parent_pipeline_id") or body.get("parentPipelineId"),
        parent_release_id=parent_release_id,
        # 上面核实过本机确实在跑这条父发布的任务了
        trusted_nested=bool(parent_release_id),
    )
    return R.ok(release_status_payload(db, release))


# ============================================================
# 插件回调（只认任务级 token，能做的事严格限定在本任务范围内）
# ============================================================
def _task_from_token(db: Session, token: str) -> BuildTask:
    task = db.get(BuildTask, task_token.verify(token))
    if task is None:
        raise BizException.forbidden("任务凭证无效")
    if task.status not in ("running", "pending"):
        raise BizException.forbidden("任务已结束，凭证不再可用")
    return task


@router.post("/plugin-api/deployments", summary="插件上报部署痕迹（任务级凭证）")
def plugin_report_deployment(
    body: dict,
    db: Session = Depends(get_db),
    x_task_token: str = Header(default=""),
):
    """跑在构建机上的部署插件（K8s、容器）用它登记回滚凭据。

    和节点走的那个接口是同一件事，只是身份来源不同：节点用自己的 token，
    插件是 Agent 拉起的子进程，只能拿到绑定本任务的一次性凭证。
    """
    from app.modules.deployment import service as deployment_service

    task = _task_from_token(db, x_task_token)
    row = deployment_service.record_deployment(
        db,
        release_id=task.release_id,
        pipeline_id=task.pipeline_id,
        task_id=task.id,
        agent_id=task.agent_id,
        step_index=int(body.get("step_index") or 0),
        kind=str(body.get("kind") or ""),
        payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
        target=str(body.get("target") or ""),
        summary=str(body.get("summary") or ""),
    )
    return R.ok({"id": row.id})


@router.post("/plugin-api/deployments/{record_id}/undone", summary="插件上报某条部署已撤销")
def plugin_report_deployment_undone(
    record_id: int,
    db: Session = Depends(get_db),
    x_task_token: str = Header(default=""),
):
    from app.modules.deployment import service as deployment_service

    task = _task_from_token(db, x_task_token)
    deployment_service.mark_undone(db, record_id, task.release_id)
    return R.ok()


@router.post("/plugin-api/sub-pipelines/run", summary="插件启动子流水线（任务级凭证）")
def plugin_run_sub_pipeline(
    body: dict,
    db: Session = Depends(get_db),
    x_task_token: str = Header(default=""),
):
    """权限取本任务父发布的操作人，父发布由凭证推出来，插件传什么都改不了。"""
    from app.modules.pipeline.models import Release
    from app.modules.pipeline.sub_pipeline import (
        release_status_payload,
        start_sub_pipeline,
        user_from_operator,
    )

    task = _task_from_token(db, x_task_token)
    parent = db.get(Release, task.release_id)
    if parent is None:
        raise BizException.not_found("父发布")
    pipeline_id = body.get("pipeline_id") or body.get("pipelineId")
    if not pipeline_id:
        raise BizException.bad_request("缺少 pipeline_id")
    release = start_sub_pipeline(
        db,
        user=user_from_operator(db, parent.operator_id),
        pipeline_id=int(pipeline_id),
        project_id=body.get("project_id") or body.get("projectId"),
        params=body.get("params") or {},
        parent_pipeline_id=task.pipeline_id,
        parent_release_id=task.release_id,
        # 父发布是任务级 token 绑定的，不是调用方自己报的，可以继承审批闸门
        trusted_nested=True,
    )
    return R.ok(release_status_payload(db, release))


@router.get("/plugin-api/releases/{release_id}/status", summary="插件查询子流水线状态（任务级凭证）")
def plugin_sub_pipeline_status(
    release_id: int,
    db: Session = Depends(get_db),
    x_task_token: str = Header(default=""),
):
    from app.modules.pipeline.models import Release

    from app.modules.pipeline.sub_pipeline import release_status_payload

    task = _task_from_token(db, x_task_token)
    release = db.get(Release, release_id)
    if release is None:
        raise BizException.not_found("发布任务")
    # 只能查自己拉起来的子发布，不能拿凭证遍历全平台
    if release.id != task.release_id and release.parent_release_id != task.release_id:
        raise BizException.forbidden("该发布不属于本次任务")
    return R.ok(release_status_payload(db, release))


@router.get("/agents/{agent_id}/releases/{release_id}/status", summary="Agent 查询子流水线状态")
def agent_sub_pipeline_status(
    agent_id: int,
    release_id: int,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
):
    _authenticate_agent(db, agent_id, x_agent_token)
    from app.modules.pipeline.models import Release
    from app.modules.pipeline.sub_pipeline import release_status_payload

    release = db.get(Release, release_id)
    if release is None:
        raise BizException.not_found("发布任务")
    return R.ok(release_status_payload(db, release))
