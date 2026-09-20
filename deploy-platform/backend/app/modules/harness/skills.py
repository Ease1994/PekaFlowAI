"""Agent Skill：按需加载的指令包，不含可执行代码。

Skill 和 Tool 是两件事，混在一起是过去这套助手最大的概念债：
Tool 会真的改动平台状态，所以要签名、要隔离、要审批；Skill 只是写给模型看的
指令和资源，最坏情况是模型被引导得不好，因此它不进 Runner、也不需要签名，
但必须能被禁用——被禁用的 Skill 一个字都不能进模型上下文。

对齐 DeepSeek Harness：系统提示只给 name + description；正文由 `skill` 工具
以 <skill_content> 返回。always / 用户点选的技能才内联正文。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.harness.models import HarnessComponent, HarnessVersion
from app.modules.harness.packages import Manifest

_selected_skills: ContextVar[tuple[str, ...]] = ContextVar("selected_skills", default=())

MANIFEST_NAME = "manifest.yaml"
BODY_NAME = "SKILL.md"
MAX_BODY_CHARS = 20000
# model：模型自己按目录判断是否加载；user：只有用户显式点选才加载；always：每轮都注入
INVOCATION_POLICIES = {"model", "user", "always"}
# 普通用户从助手安装的技能只挂在自己名下，source_ref = user:{user_id}
USER_SKILL_SOURCE = "user"
USER_SKILL_REF_PREFIX = "user:"


@dataclass(frozen=True)
class SkillPackage:
    manifest: Manifest
    body: str
    resources: dict[str, int]


def parse_package(data: bytes) -> SkillPackage:
    """解析 Skill zip：manifest.yaml 声明元信息，SKILL.md 是给模型看的正文。"""
    if not data:
        raise BizException.bad_request("空文件")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BizException.bad_request("不是合法的 zip 包") from exc
    with archive:
        names = {name.replace("\\", "/"): name for name in archive.namelist() if not name.endswith("/")}
        root = _common_root(names)
        manifest_name = names.get(f"{root}{MANIFEST_NAME}")
        body_name = names.get(f"{root}{BODY_NAME}")
        if not manifest_name or not body_name:
            raise BizException.bad_request(f"Skill 包必须包含 {MANIFEST_NAME} 和 {BODY_NAME}")
        try:
            raw = yaml.safe_load(archive.read(manifest_name).decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise BizException.bad_request(f"{MANIFEST_NAME} 解析失败: {exc}") from exc
        if not isinstance(raw, dict):
            raise BizException.bad_request(f"{MANIFEST_NAME} 必须是映射")
        raw.setdefault("kind", "agent-skill")
        try:
            manifest = Manifest.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - pydantic 的报错直接给用户看
            raise BizException.bad_request(f"{MANIFEST_NAME} 不合法: {exc}") from exc
        if manifest.kind != "agent-skill":
            raise BizException.bad_request("Skill 包的 kind 必须是 agent-skill")
        policy = str((manifest.metadata or {}).get("invocation_policy") or "model")
        if policy not in INVOCATION_POLICIES:
            raise BizException.bad_request(
                "invocation_policy 只能是 " + "、".join(sorted(INVOCATION_POLICIES))
            )
        body = archive.read(body_name).decode("utf-8", errors="replace").strip()
        if not body:
            raise BizException.bad_request(f"{BODY_NAME} 不能为空")
        if len(body) > MAX_BODY_CHARS:
            raise BizException.bad_request(f"{BODY_NAME} 超过 {MAX_BODY_CHARS} 字符上限")
        resources = {
            name[len(root):]: archive.getinfo(actual).file_size
            for name, actual in sorted(names.items())
            if name not in {f"{root}{MANIFEST_NAME}", f"{root}{BODY_NAME}"}
        }
    return SkillPackage(manifest=manifest, body=body, resources=resources)


def _common_root(names: dict[str, str]) -> str:
    """允许 zip 里多包一层目录，取公共前缀。"""
    if any("/" not in name for name in names):
        return ""
    roots = {name.split("/", 1)[0] for name in names}
    return f"{next(iter(roots))}/" if len(roots) == 1 else ""


def personal_component_name(user_id: int, public_name: str) -> str:
    """个人技能在组件表里的唯一名：`u{用户id}-{对外名}`，避免和别人或全局技能撞车。"""
    return f"u{int(user_id)}-{public_name}"


def personal_owner_id(component: HarnessComponent, metadata: dict | None = None) -> int | None:
    """读出个人技能的所有者。全局技能返回 None。"""
    if (component.source or "") == USER_SKILL_SOURCE:
        ref = (component.source_ref or "").strip()
        if ref.startswith(USER_SKILL_REF_PREFIX):
            try:
                return int(ref[len(USER_SKILL_REF_PREFIX) :])
            except ValueError:
                return None
    raw = (metadata or {}).get("owner_user_id")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def catalog_public_name(manifest: Manifest, metadata: dict | None = None) -> str:
    """模型目录和确认卡用对外名；库内可能带 u{id}- 前缀。"""
    public = str((metadata or {}).get("public_name") or "").strip()
    return public or manifest.name


def viewer_can_see(
    component: HarnessComponent,
    *,
    viewer_id: int | None,
    is_admin: bool = False,
    metadata: dict | None = None,
) -> bool:
    """别人的个人技能不能出现在列表、详情和下载里。全局技能和自己装的可以看。"""
    if component.kind != "agent-skill":
        return True
    owner = personal_owner_id(component, metadata)
    if owner is None or is_admin:
        return True
    return viewer_id is not None and owner == viewer_id


def pack_skill_archive(
    *,
    name: str,
    version: str,
    display_name: str,
    description: str,
    body: str,
    examples: str = "",
    invocation_policy: str = "model",
    extra_files: dict[str, str] | None = None,
    public_name: str = "",
    owner_user_id: int | None = None,
) -> bytes:
    """把 SKILL.md 打成和上传模板同一形状的 zip。

    extra_files 用于附带 README 等给人看的说明，解析器会忽略它们。
    public_name / owner_user_id 只在个人安装时写入 metadata：目录对外仍用友好名。
    """
    text = (body or "").strip()
    if not text:
        raise BizException.bad_request("SKILL.md 不能为空")
    if len(text) > MAX_BODY_CHARS:
        text = text[: MAX_BODY_CHARS - 20].rstrip() + "\n\n…（已截断）\n"
    metadata: dict[str, Any] = {"invocation_policy": invocation_policy or "model"}
    if public_name:
        metadata["public_name"] = public_name
    if owner_user_id:
        metadata["owner_user_id"] = int(owner_user_id)
    manifest = {
        "schema_version": "1",
        "kind": "agent-skill",
        "name": name,
        "version": version,
        "display_name": display_name,
        "description": description,
        "entrypoint": "",
        "capabilities": [],
        "dependencies": [],
        "metadata": metadata,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
        archive.writestr(BODY_NAME, text + "\n")
        if (examples or "").strip():
            archive.writestr("examples.md", examples.strip() + "\n")
        for relative, content in (extra_files or {}).items():
            archive.writestr(relative, content if content.endswith("\n") else content + "\n")
    return buf.getvalue()


def export_installed_skill(component: HarnessComponent, version: HarnessVersion) -> tuple[str, bytes]:
    """把已安装技能还原成可二次开发的 zip。正文在 version.manifest 的 metadata.body。"""
    manifest = Manifest.model_validate_json(version.manifest_json)
    metadata = manifest.metadata or {}
    body = str(metadata.get("body") or "").strip()
    if not body:
        raise BizException.bad_request("该技能没有可导出的说明书正文")
    data = pack_skill_archive(
        name=manifest.name,
        version=version.version,
        display_name=manifest.display_name or manifest.name,
        description=manifest.description,
        body=body,
        invocation_policy=str(metadata.get("invocation_policy") or "model"),
    )
    return f"{manifest.name}-{version.version}.zip", data


def export_builtin_tool_as_skill(spec: Any) -> tuple[str, bytes]:
    """把进程内内置工具导出成技能包，方便改 name 后当第三方技能上传。

    内置工具的实现仍在 API 进程里，这份 zip 只是说明书，不能替换原工具。
    导出 name 加 -skill 后缀，避免和工具标识撞车。
    """
    fork_name = f"{spec.name}-skill"
    properties = (spec.parameters or {}).get("properties") or {}
    param_lines = []
    if isinstance(properties, dict) and properties:
        for key, schema in properties.items():
            if not isinstance(schema, dict):
                param_lines.append(f"- `{key}`")
                continue
            desc = str(schema.get("description") or schema.get("type") or "").strip()
            param_lines.append(f"- `{key}`：{desc}" if desc else f"- `{key}`")
    else:
        param_lines.append("无参数。")
    examples = "\n".join(f"- {item}" for item in (spec.examples or ()) if str(item).strip())
    body = "\n".join(
        [
            f"# {spec.display_name or spec.name}",
            "",
            f"这是从内置工具 `{spec.name}` 导出的说明书，用于二次开发。",
            f"上传前请把 `manifest.yaml` 的 `name` 改成你自己的标识，不要再用 `{fork_name}` 以外的内置工具名。",
            "本包不含可执行代码，不会替换平台进程内的原工具。",
            "",
            "## 何时使用",
            spec.description or spec.name,
            "",
            "## 参数",
            *param_lines,
            "",
            "## 调用示例",
            examples or "按用户原话填写参数后调用该工具。",
            "",
            "## 参数 Schema",
            "```json",
            json.dumps(spec.parameters or {}, ensure_ascii=False, indent=2),
            "```",
        ]
    )
    data = pack_skill_archive(
        name=fork_name,
        version="1.0.0",
        display_name=f"{spec.display_name or spec.name}（技能说明书）",
        description=spec.description or spec.name,
        body=body,
        examples="\n".join(str(item) for item in (spec.examples or ()) if str(item).strip()),
        invocation_policy="model",
        extra_files={
            "README.md": (
                "内置工具导出的技能包。改掉 manifest.yaml 的 name 后再上传。"
                "这不会替换平台自带工具，只是给模型一份说明书。"
            )
        },
    )
    return f"{fork_name}-1.0.0.zip", data


def install_package(
    db: Session,
    data: bytes,
    *,
    actor_id: int | None = None,
    actor_name: str = "",
    source: str | None = None,
    source_ref: str = "",
) -> HarnessComponent:
    from app.modules.harness import lifecycle

    package = parse_package(data)
    metadata = dict(package.manifest.metadata or {})
    raw_owner = metadata.get("owner_user_id")
    try:
        owner = int(raw_owner) if raw_owner is not None and str(raw_owner).strip() != "" else None
    except (TypeError, ValueError):
        owner = None
    resolved_source = source or (USER_SKILL_SOURCE if owner else "upload")
    resolved_ref = source_ref or (
        f"{USER_SKILL_REF_PREFIX}{owner}" if owner else f"skill:{package.manifest.name}"
    )
    manifest = package.manifest.model_copy(
        update={
            "metadata": {
                **metadata,
                "body": package.body,
                "resources": package.resources,
                "invocation_policy": str(metadata.get("invocation_policy") or "model"),
            }
        }
    )
    return lifecycle.install(
        db,
        manifest,
        enable=True,
        source=resolved_source,
        source_ref=resolved_ref,
        actor_id=actor_id,
        actor_name=actor_name,
    )


def catalog(
    db: Session,
    *,
    enabled_only: bool = True,
    viewer_id: int | None = None,
    include_all_personal: bool = False,
) -> list[dict[str, Any]]:
    """已安装技能目录。

    不传 viewer_id 时只返回全局技能（管理员上传 / 管理员从助手安装）。
    传入 viewer_id 时附带该用户自己装的个人技能。
    include_all_personal 给技能库管理视图：管理员能看到所有人的个人技能。
    """
    stmt = (
        select(HarnessComponent, HarnessVersion)
        .join(HarnessVersion, HarnessVersion.id == HarnessComponent.current_version_id)
        .where(HarnessComponent.kind == "agent-skill")
        .order_by(HarnessComponent.name)
    )
    if enabled_only:
        stmt = stmt.where(HarnessComponent.enabled.is_(True))
    personal: list[dict[str, Any]] = []
    shared: list[dict[str, Any]] = []
    for component, version in db.execute(stmt).all():
        manifest = Manifest.model_validate_json(version.manifest_json)
        metadata = manifest.metadata or {}
        owner = personal_owner_id(component, metadata)
        item = {
            "component_id": component.id,
            "name": catalog_public_name(manifest, metadata),
            "component_name": component.name,
            "display_name": manifest.display_name or manifest.name,
            "description": manifest.description,
            "version": version.version,
            "source": component.source,
            "status": component.status,
            "enabled": component.enabled,
            "owner_user_id": owner,
            "publisher_id": version.created_by,
            "invocation_policy": str(metadata.get("invocation_policy") or "model"),
            "dependencies": [item.model_dump() for item in manifest.dependencies],
            "resources": metadata.get("resources") or {},
            "body": metadata.get("body") or "",
        }
        if owner is not None:
            if include_all_personal or (viewer_id is not None and owner == viewer_id):
                personal.append(item)
            continue
        shared.append(item)
    # 个人技能排在前面：同名时 visible_entries 以个人说明书为准
    return [*personal, *shared]


def bind_selected(names: list[str] | None) -> Token:
    return _selected_skills.set(tuple(str(n).strip() for n in (names or []) if str(n).strip()))


def reset_selected(token: Token) -> None:
    _selected_skills.reset(token)


def selected_names() -> tuple[str, ...]:
    return _selected_skills.get()


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;")


_STATUS_SKILL_NAMES = frozenset({"query-pipeline-status", "query-release-status"})
_STATUS_SKILL_OVERRIDE = (
    "默认只查用户点名流水线的最近一次执行。"
    "没说历史、最近几次、全部记录时，禁止把执行历史全拉出来，"
    "禁止 list_pipelines / get_pipeline 连环查询，只调用一次 get_release_status。"
    "status 只表示成败/进行中筛法；不传 status 就是最近一次的实际状态。"
)


def body_for_model(name: str, body: str) -> str:
    """已安装的查状态技能若仍写「没说就 all」，运行时改成最近一次，避免翻历史循环。"""
    text = body or ""
    if name not in _STATUS_SKILL_NAMES or "运行时修订" in text:
        return text
    return "## 运行时修订\n" + _STATUS_SKILL_OVERRIDE + "\n\n" + text


def render_skill_content(
    name: str,
    body: str,
    *,
    provider: str,
    resources: dict | None = None,
) -> str:
    """两条加载路径共用这一种形态：skill 工具结果，以及 always/点选内联。"""
    body = body_for_model(name, body)
    hints = [
        f'Resources for this skill are managed by provider "{_escape_text(provider)}".',
        "Load referenced resources only as needed.",
    ]
    if resources:
        listed = ", ".join(sorted(resources)[:12])
        hints.append(f"Packaged files: {listed}.")
    return "\n".join(
        [
            f'<skill_content name="{_escape_attr(name)}">',
            "<skill_resources>",
            *hints,
            "</skill_resources>",
            "",
            "<skill_instructions>",
            body.strip(),
            "</skill_instructions>",
            "</skill_content>",
        ]
    )


def _playbook_entries() -> list[dict[str, Any]]:
    from app.modules.ai.playbooks import all_playbooks

    return [
        {
            "name": item.name,
            "display_name": item.display_name,
            "description": item.description,
            "invocation_policy": "model",
            "body": item.body,
            "resources": {},
            "provider": "builtin",
            "source": "builtin",
        }
        for item in all_playbooks()
    ]


# 装了很多个人技能时，目录不能整表塞进每一轮。
_DIR_LIMIT = 12
# 和用户话重叠到这个分，就内联 SKILL.md（相关技能先读再调工具）。
_MATCH_SCORE = 2
# 一轮最多内联几份说明书，避免把全部正文都塞进上下文。
_INLINE_LIMIT = 2
_QUERY_STOP = {
    "一下",
    "帮我",
    "请",
    "的",
    "我的",
    "有哪些",
    "有没有",
    "what",
    "the",
    "and",
    "for",
    "how",
}
_INBOX_HINT = re.compile(r"(未读|通知|消息|铃铛|收件箱|inbox|notification|unread)", re.I)


def _query_tokens(text: str) -> set[str]:
    """把用户话和技能摘要切成可重叠的词。中文用二字串，英文按标识符切。"""
    raw = (text or "").casefold()
    latin = set(re.findall(r"[a-z0-9][a-z0-9\-_]{1,}", raw))
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(run) == 1:
            grams.add(run)
            continue
        grams.add(run)
        grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return {tok for tok in latin | grams if tok and tok not in _QUERY_STOP}


def score_skill(message: str, item: dict[str, Any]) -> int:
    """用户这一句和某条技能有多贴。只看 name / 显示名 / 描述 / 正文，不跑模型。

    正文要算进去：description 写「发布」，用户常说「发到」，例子在 SKILL.md 后半段。
    """
    query = (message or "").strip()
    if not query:
        return 0
    name = str(item.get("name") or "")
    display = str(item.get("display_name") or "")
    desc = str(item.get("description") or "")
    body = str(item.get("body") or "")[:3000]
    blob = f"{name} {display} {desc} {body}"
    q = query.casefold()
    score = 0
    if name and name.casefold() in q:
        score += 4
    if display and len(display) >= 2 and display.casefold() in q:
        score += 3
    score += min(6, len(_query_tokens(query) & _query_tokens(blob)))
    if _INBOX_HINT.search(query) and _INBOX_HINT.search(blob):
        score += 3
    return score


def entries_for_turn(
    db: Session,
    *,
    message: str = "",
    selected: list[str] | None = None,
    viewer_id: int | None = None,
) -> list[dict[str, Any]]:
    """本轮真正塞进模型的技能。

    目录里保留全部内置技能的摘要（name + description）。
    只把本轮最相关的 1～2 份 SKILL.md 正文内联，视为已经读过，然后直接调工具。
    已安装的个人技能同样按这句话检索，高分内联，其余最多留 12 条摘要。
    """
    visible = visible_entries(db, selected=selected, viewer_id=viewer_id)
    if not (message or "").strip():
        return visible
    builtins: list[dict[str, Any]] = []
    installed: list[dict[str, Any]] = []
    for item in visible:
        if item.get("source") == "builtin":
            builtins.append(item)
        else:
            installed.append(item)
    out: list[dict[str, Any]] = []
    for item in installed:
        score = score_skill(message, item)
        entry = dict(item)
        if entry.get("inlined") or score >= _MATCH_SCORE:
            entry["inlined"] = True
            entry["matched"] = True
            out.append(entry)
            continue
        if score > 0:
            entry["matched"] = False
            entry["score"] = score
            out.append(entry)
    rest = [item for item in out if not item.get("inlined")]
    rest.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    kept_rest = rest[:_DIR_LIMIT]
    forced = [item for item in out if item.get("inlined")]
    playbooks: list[dict[str, Any]] = []
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in builtins:
        ranked.append((score_skill(message, item), dict(item)))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    inlined = 0
    for score, entry in ranked:
        if entry.get("inlined") or (score >= _MATCH_SCORE and inlined < _INLINE_LIMIT):
            entry["inlined"] = True
            entry["matched"] = True
            inlined += 1
        playbooks.append(entry)
    return [*forced, *kept_rest, *playbooks]


def visible_entries(
    db: Session, *, selected: list[str] | None = None, viewer_id: int | None = None
) -> list[dict[str, Any]]:
    """模型能看见的技能：内置 playbook + 已启用的全局包 + 当前用户自己装的。

    user 策略不进目录，除非用户点选；点选后正文内联，目录里标注已加载。
    """
    chosen = set(selected if selected is not None else selected_names())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (*_playbook_entries(), *catalog(db, viewer_id=viewer_id)):
        name = item["name"]
        if name in seen:
            continue
        seen.add(name)
        policy = str(item.get("invocation_policy") or "model")
        if policy == "user" and name not in chosen:
            continue
        entry = dict(item)
        entry.setdefault("provider", "installed" if item.get("source") != "builtin" else "builtin")
        entry["inlined"] = policy == "always" or name in chosen
        out.append(entry)
    return out


def resolve_for_model(
    db: Session,
    name: str,
    *,
    selected: list[str] | None = None,
    viewer_id: int | None = None,
) -> dict[str, Any] | None:
    key = (name or "").strip()
    if not key:
        return None
    for item in visible_entries(db, selected=selected, viewer_id=viewer_id):
        if item["name"] == key:
            return item
    return None


def load_for_model(
    db: Session,
    name: str,
    *,
    selected: list[str] | None = None,
    viewer_id: int | None = None,
) -> dict[str, Any]:
    item = resolve_for_model(db, name, selected=selected, viewer_id=viewer_id)
    if item is None:
        return {"error": f"未知或不可用的技能 {name}", "code": "SKILL_NOT_FOUND"}
    body = body_for_model(item["name"], item.get("body") or "")
    rendered = render_skill_content(
        item["name"],
        body,
        provider=str(item.get("provider") or "installed"),
        resources=item.get("resources") or {},
    )
    return {
        "name": item["name"],
        "provider": item.get("provider"),
        "content": body,
        "skill_content": rendered,
    }


def system_prompt(
    db: Session,
    *,
    selected: list[str] | None = None,
    viewer_id: int | None = None,
    message: str = "",
) -> str:
    """本轮注入模型的技能材料：按这句话检索后的目录摘要 + 已匹配的正文。

    目录只有 name 和 description。匹配到的个人技能会内联 SKILL.md，
    否则模型会跳过说明书、直接调描述更硬的内置工具。
    """
    entries = entries_for_turn(db, message=message, selected=selected, viewer_id=viewer_id)
    if not entries:
        return ""
    lines = [
        "<available_skills>",
        *[
            f"- `{item['name']}`: {_escape_text(item.get('description') or item['display_name'])}"
            + (" （已加载，不要再调用 skill）" if item.get("inlined") else "")
            for item in entries
        ],
        "</available_skills>",
        "技能是 SKILL.md 说明书，工具是可执行函数。"
        "目录只有摘要。已标注「已加载」的正文在下方，立即按 Workflow 调用工具，不要再 skill。"
        "未加载时才用 skill 工具传入精确 name。",
    ]
    loaded = [item["name"] for item in entries if item.get("inlined")]
    if loaded:
        names = "、".join(f"`{name}`" for name in loaded)
        lines.append(
            f"本轮已加载：{names}。视为已经读过这些 SKILL.md，按 Workflow 调用工具，"
            "不要用工具 schema 的短描述覆盖说明书里的流程。"
        )
    inlined = [item for item in entries if item.get("inlined")]
    if inlined:
        lines.append("")
        lines.extend(
            render_skill_content(
                item["name"],
                body_for_model(item["name"], item.get("body") or ""),
                provider=str(item.get("provider") or "installed"),
                resources=item.get("resources") or {},
            )
            for item in inlined
        )
    return "\n".join(lines)
