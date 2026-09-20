"""助手上下文：用户可见资源目录。"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.modules.pipeline.models import Pipeline
from app.modules.project.models import Group, Project

SERVICE_ALIASES: dict[str, list[str]] = {
    "order-service": ["订单服务", "订单", "order"],
    "pay-service": ["支付服务", "支付", "pay"],
    "gateway": ["网关", "gateway"],
    "b2c-front": ["前端", "b2c前端", "b2c", "front"],
}


def visible_pipelines(db: Session, current) -> list[tuple[Pipeline, Project | None, Group | None]]:
    from app.core.deps import visible_pipeline_ids

    vis = visible_pipeline_ids(db, current)
    from app.modules.pipeline.service import PIPELINE_HIDDEN

    stmt = select(Pipeline).options(defer(Pipeline.yaml)).where(Pipeline.status.notin_(PIPELINE_HIDDEN))
    if vis is not None:
        if not vis:
            return []
        stmt = stmt.where(Pipeline.id.in_(vis))
    rows = list(db.scalars(stmt).all())
    if not rows:
        return []
    # 项目/分组批量取：逐条 db.get 在几千条流水线时就是几千次往返
    projects = {
        p.id: p
        for p in db.scalars(select(Project).where(Project.id.in_({r.project_id for r in rows}))).all()
    }
    groups = {
        g.id: g
        for g in db.scalars(select(Group).where(Group.id.in_({r.group_id for r in rows}))).all()
    }
    from app.core.env import is_platform_project

    return [
        (p, proj, groups.get(p.group_id))
        for p in rows
        for proj in (projects.get(p.project_id),)
        if not is_platform_project(proj.code if proj else None)
    ]


# system prompt 里最多摆多少条流水线摘要
CATALOG_PREVIEW = 80


def catalog_text(db: Session, current) -> str:
    rows = visible_pipelines(db, current)
    if not rows:
        return "（当前用户可见流水线为空）"
    lines = []
    for p, proj, g in rows[:CATALOG_PREVIEW]:
        lines.append(
            f"- id={p.id} name={p.name} project={proj.name if proj else p.project_id} "
            f"env={g.type if g else ''} group={g.name if g else p.group_id}"
        )
    if len(rows) > CATALOG_PREVIEW:
        # 不说清楚这是截断的，模型会把这几十条当全集，一口咬定「没有这条流水线」
        lines.append(
            f"（以上只是 {len(rows)} 条可见流水线里的前 {CATALOG_PREVIEW} 条，不是全部。"
            "这里没有的不代表不存在，用 list_pipelines 带 keyword/project 查）"
        )
    return "\n".join(lines)


# 匹配强度。分级是必须的：以前只要名字里任一个词出现就算命中，
# 于是用户说「test-C 发布」时 order-service-test 靠一个 test 抢先命中，发错流水线。
MATCH_EXACT = 3   # 整个名字完整出现
MATCH_ALIAS = 2   # 业务别名命中
MATCH_TOKEN = 1   # 只命中名字里的某一个词，最弱

_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_SEPARATORS = re.compile(r"[\s\-_/.]+")


def _tokens(text: str) -> list[str]:
    raw = text or ""
    # 「查一下test-C的状态」中文和英文名粘在一起，不切开就对不上流水线名
    raw = re.sub(r"([\u4e00-\u9fff])(?=[0-9a-zA-Z])", r"\1 ", raw)
    raw = re.sub(r"([0-9a-zA-Z])(?=[\u4e00-\u9fff])", r"\1 ", raw)
    return [t for t in _TOKEN_SPLIT.split(raw.lower()) if t]


def _contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    """名字的词必须连续出现，否则 order-service-test 会被 test-c 命中。"""
    if not needle or len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return True
    return False


def _name_candidates(p: Pipeline) -> list[str]:
    from app.modules.pipeline.schemas import parse_yaml

    names = [p.name or ""]
    try:
        definition = parse_yaml(p.yaml)
        if definition.pipeline.name:
            names.append(definition.pipeline.name)
    except ValueError:
        pass
    return names


def match_score(text_lower: str, p: Pipeline) -> int:
    """0 不匹配；数字越大越可信。

    工作台显示名整名命中最强。YAML 里的 pipeline.name 只当别名：复制流水线时常
    忘了改 YAML 名，若和显示名平权，用户说 test-C 会把显示名为 test-B 的副本一起算命中。
    """
    text_lower = (text_lower or "").lower()
    if not text_lower:
        return 0
    text_tokens = _tokens(text_lower)
    best = 0
    for index, name in enumerate(_name_candidates(p)):
        nl = (name or "").lower().strip()
        if not nl:
            continue
        scored = 0
        if _CJK.search(nl):
            if _SEPARATORS.sub("", nl) in _SEPARATORS.sub("", text_lower):
                scored = MATCH_EXACT
        else:
            name_tokens = _tokens(nl)
            if _contains_sequence(text_tokens, name_tokens):
                scored = MATCH_EXACT
            elif any(len(tok) >= 2 and tok in text_tokens for tok in name_tokens):
                scored = MATCH_TOKEN
        if index > 0 and scored == MATCH_EXACT:
            scored = MATCH_ALIAS
        best = max(best, scored)
    lowered_name = (p.name or "").lower()
    for key, aliases in SERVICE_ALIASES.items():
        if key in lowered_name and any(a.lower() in text_lower for a in aliases):
            best = max(best, MATCH_ALIAS)
    return best


_STRIP_RELEASE = re.compile(
    r"(请|帮我|麻烦|把|将|一下|执行|发布|部署|上线|跑|发|"
    r"项目|下的|下面的|所有|全部|流水线|的|"
    r"测试环境|生产环境|线上环境|预发环境|开发环境|uat环境|staging环境|"
    r"测试分组|生产分组|只要测试|只要生产)",
    re.I,
)


def project_keyword(message: str) -> str:
    """从发布用语里抽出项目关键字。

    实现：去掉「执行/发布/项目/生产环境」这类套话，剩下的当作项目名，例如
    「执行AI陪练项目生产环境」→「AI陪练」。
    """
    text = _STRIP_RELEASE.sub(" ", message or "")
    return re.sub(r"\s+", " ", text).strip()


def match_projects_by_keyword(projects: list, keyword: str) -> list:
    """项目名或代号命中关键字。全等优先，避免短词误伤。"""
    kw = (keyword or "").replace(" ", "").lower()
    if len(kw) < 2:
        return []
    hits: list = []
    exact: list = []
    for p in projects:
        name = (getattr(p, "name", None) or "").replace(" ", "")
        code = (getattr(p, "code", None) or "").lower()
        nl = name.lower()
        if kw == nl or kw == code:
            exact.append(p)
        elif kw in nl or (nl and nl in kw) or (code and (kw in code or code in kw)):
            hits.append(p)
    return exact or hits


def listed_business_projects(db: Session) -> list[Project]:
    """全部业务项目（不含平台内置），用来区分「没有这个项目」和「没权限看见流水线」。"""
    from app.core.env import is_platform_project

    return [
        p
        for p in db.scalars(select(Project).order_by(Project.id)).all()
        if not is_platform_project(p.code)
    ]


def best_matches(text_lower: str, rows: list) -> list:
    """只留匹配最强的那一档。宁可让用户再说一次，也不能挑错流水线去发布。"""
    scored = [(match_score(text_lower, item[0] if isinstance(item, tuple) else item), item) for item in rows]
    scored = [(score, item) for score, item in scored if score > 0]
    if not scored:
        return []
    top = max(score for score, _ in scored)
    return [item for score, item in scored if score == top]


def match_pipeline(text_lower: str, p: Pipeline) -> bool:
    return match_score(text_lower, p) > 0


def format_release_candidates(items: list, lead: str) -> str:
    """把候选流水线列成给用户或模型看的清单，带真实 id。"""
    lines = [lead]
    for p, _proj, g in items[:10]:
        extra = f"（{g.name}）" if g else ""
        lines.append(f"· #{p.id} {p.name}{extra}")
    return "\n".join(lines)


def _env_ok(item: tuple, env_code: str) -> bool:
    """分组类型是否等于用户要的环境。没指定环境则一律通过。"""
    if not env_code:
        return True
    group = item[2]
    return group is not None and group.type == env_code


def _strong_name_hits(text: str, rows: list) -> list:
    """只取整名或业务别名命中，弱词（名字里碰巧带 ai）不算。"""
    if not text or not rows:
        return []
    lowered = text.lower()
    named = best_matches(lowered, rows)
    exact = [item for item in named if match_score(lowered, item[0]) >= MATCH_EXACT]
    if exact:
        return exact
    return [item for item in named if match_score(lowered, item[0]) >= MATCH_ALIAS]


def resolve_release_target(
    db: Session,
    current,
    *,
    pipeline_id: int = 0,
    pipeline: str = "",
    project: str = "",
    env: str = "",
    query: str = "",
) -> dict:
    """把发布槽位落到用户可见的流水线。

    实现顺序：可见集合里的真实 id → 流水线全名/别名 → 项目+环境。
    编造的 pipeline_id 不在可见集合里就丢掉，改用其它槽位，避免模型猜号。
    grounded=True 表示项目或名字已经钉死，不必再问模型；False 表示槽位不够，交给模型填。
    """
    from app.core.env import env_label
    from app.modules.ai.intent import normalize_env_slot, parse_env_code, parse_project_id

    rows = visible_pipelines(db, current)
    env_code = normalize_env_slot(env) or parse_env_code(query)
    name_hint = (pipeline or "").strip()
    project_hint = (project or "").strip()
    text = (query or "").strip()
    scoped = [item for item in rows if _env_ok(item, env_code)]
    has_slots = bool(name_hint or project_hint or text)

    # 1. 纯数字 id 且用户没点名时才认。模型和 query 同时出现时，名称/项目优先，避免可见假 id 抢先。
    if pipeline_id and not has_slots:
        hit = next((item for item in scoped if item[0].id == pipeline_id), None)
        if hit is not None:
            return {"match": hit[0], "item": hit, "grounded": True}

    # 2. 点名的流水线名：独立槽，或整句里的全名（「发布 test-C」）。整名优先于项目匹配。
    for name_text in dict.fromkeys((name_hint, text)):
        if not name_text:
            continue
        hits = _strong_name_hits(name_text, scoped)
        if len(hits) == 1:
            return {"match": hits[0][0], "item": hits[0], "grounded": True}
        if len(hits) > 1:
            return {
                "candidates": hits,
                "grounded": True,
                "reply": format_release_candidates(hits, "有多条流水线都对得上，请用返回的 id 再调一次："),
            }

    # 3. 项目槽。模型抽出「陪练」比从整句里剥套话更稳。
    projects = listed_business_projects(db)
    pid = parse_project_id(project_hint) or parse_project_id(text)
    if pid:
        phits = [p for p in projects if p.id == pid]
    else:
        kw = project_hint or project_keyword(text)
        phits = match_projects_by_keyword(projects, kw) if kw else []

    if len(phits) == 1:
        proj = phits[0]
        pipes = [item for item in scoped if item[1] is not None and item[1].id == proj.id]
        if len(pipes) == 1:
            return {"match": pipes[0][0], "item": pipes[0], "grounded": True}
        if len(pipes) > 1:
            env_txt = env_label(env_code) if env_code else "该项目"
            return {
                "candidates": pipes,
                "grounded": True,
                "project": proj,
                "reply": format_release_candidates(
                    pipes,
                    f"「{proj.name}」{env_txt}有 {len(pipes)} 条流水线，请用返回的 id 再调一次：",
                ),
            }
        env_txt = env_label(env_code) if env_code else ""
        scope = f"「{proj.name}」{env_txt}环境" if env_code else f"「{proj.name}」"
        return {
            "error": f"看不到{scope}的流水线。可能没有执行权限，或该环境还没有流水线。",
            "grounded": True,
            "project": proj,
        }

    if len(phits) > 1:
        lead = "找到这些项目，请指出要执行哪一个："
        listing = "\n".join(f"· #{p.id} {p.name}" for p in phits[:10])
        return {
            "projects": phits,
            "grounded": True,
            "reply": f"{lead}\n{listing}",
        }

    hint = project_hint or name_hint or project_keyword(text) or (env_label(env_code) if env_code else "")
    bogus = f"流水线 #{pipeline_id} 不存在或你看不见。" if pipeline_id else ""
    miss = f"找不到项目或流水线「{hint}」。" if hint else "还不能唯一对应到一条流水线。"
    return {
        "error": f"{bogus}{miss}不要编造 pipeline_id，请改传 project、env 或流水线名称。".strip(),
        "grounded": False,
    }
