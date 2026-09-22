"""自动获取流水线 source_ref（commit SHA）及代码变更区间。

发布接口不能同步去问 Git 托管：内网 GitLab 慢或不可达时，保存并执行会一直转圈。
创建发布单先返回，SHA 放到后台补；检出步骤跑完 Agent 也会回写。
代码变更：优先 compare 区间；失败或首次发布则用「单 commit」API 拉详情。
"""
from __future__ import annotations

import json
import threading
import urllib.parse

import httpx


def _strip_git_suffix(path: str) -> str:
    """去掉 .git 后缀（勿用 str.rstrip('.git')，那是按字符集剥离）。"""
    p = (path or "").lstrip("/")
    if p.endswith(".git"):
        p = p[:-4]
    return p.rstrip("/")


def _find_first_git_checkout(definition, db) -> tuple[str | None, str | None, str | None]:
    """返回 (repo_url, ref, provider)，没找到返回 (None, None, None)。"""
    from sqlalchemy import or_

    from app.modules.repository.models import Repository

    for stage in definition.pipeline.stages:
        for job in stage.jobs or []:
            for step in job.steps or []:
                if step.plugin != "git-checkout":
                    continue
                with_dict = step.with_ or {}
                repo_alias = with_dict.get("repoName") or with_dict.get("repo")
                repo = None
                if repo_alias:
                    # alias 和 name 都查（兼容 seed 旧流水线）
                    repo = db.query(Repository).filter(
                        or_(Repository.alias == repo_alias, Repository.name == repo_alias)
                    ).first()
                if repo:
                    repo_url = repo.url
                    provider = repo.provider or "gitlab"
                    ref = (
                        with_dict.get("ref")
                        or with_dict.get("branch")
                        or repo.default_branch
                    )
                else:
                    repo_url = with_dict.get("repoUrl")
                    provider = with_dict.get("repoProvider") or "gitlab"
                    ref = with_dict.get("branch") or with_dict.get("ref")
                return repo_url, ref, provider
    return None, None, None


def checkout_branch_of(yaml_text: str | None) -> str:
    """流水线第一个 git-checkout 步骤配置的分支，给执行列表「源材料」用。

    source_ref 是检出后的 commit SHA，不能拿来当 @ 后面的名字，否则会变成
    maven@2e522b4b → 2e522b4b。解析失败或没有检出步骤时按 master。
    """
    from app.modules.pipeline.schemas import parse_yaml

    try:
        definition = parse_yaml(yaml_text or "")
    except Exception:  # noqa: BLE001
        return "master"
    for stage in definition.pipeline.stages or []:
        for job in stage.jobs or []:
            for step in job.steps or []:
                if step.plugin != "git-checkout":
                    continue
                with_dict = step.with_ or {}
                branch = str(with_dict.get("branch") or with_dict.get("ref") or "").strip()
                if branch:
                    return branch
    return "master"


def _find_repo(db, repo_url: str | None, repo_alias: str | None = None):
    """按 URL / alias 查找仓库（拿凭证）。"""
    from sqlalchemy import or_

    from app.modules.repository.models import Repository, derive_alias

    if not repo_url and not repo_alias:
        return None
    if repo_url:
        repo = db.query(Repository).filter(Repository.url == repo_url).first()
        if repo:
            return repo
        # URL 大小写或尾部 .git 不一致时，用派生 alias 再找
        alias = derive_alias(repo_url)
        if alias:
            repo = db.query(Repository).filter(
                or_(Repository.alias == alias, Repository.name == alias)
            ).first()
            if repo:
                return repo
    if repo_alias:
        return db.query(Repository).filter(
            or_(Repository.alias == repo_alias, Repository.name == repo_alias)
        ).first()
    return None


def _resolve_token(db, repo) -> str | None:
    """从 Repository.credential 解密出 token（GitLab Personal Access Token / GitHub PAT）。"""
    if not repo or not repo.credential_id:
        return None
    from app.core.security import decrypt
    from app.modules.credential.models import Credential

    cred = db.get(Credential, repo.credential_id)
    if cred is None:
        return None
    try:
        return decrypt(cred.ciphertext, cred.iv)
    except Exception:  # noqa: BLE001
        return None


def _gitlab_headers(token: str | None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        # GitLab PAT：PRIVATE-TOKEN；部分版本也认 Bearer
        headers["PRIVATE-TOKEN"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_gitlab_commit(api_base: str, path: str, ref: str, token: str | None) -> str | None:
    """调 GitLab API 拿 commit SHA。"""
    detail = _fetch_gitlab_commit_detail(api_base, path, ref, token)
    return detail.get("id") if detail else None


def _fetch_gitlab_commit_detail(
    api_base: str, path: str, ref: str, token: str | None, *, timeout: float = 15
) -> dict | None:
    """调 GitLab API 拿单条 commit 详情。"""
    project_id = urllib.parse.quote(path, safe="")
    url = f"{api_base}/api/v4/projects/{project_id}/repository/commits/{urllib.parse.quote(ref, safe='')}"
    try:
        with httpx.Client(timeout=timeout, verify=False) as client:
            resp = client.get(url, headers=_gitlab_headers(token))
        if resp.status_code != 200:
            print(f"[source_ref_service] GitLab commit {resp.status_code}: {url} body={resp.text[:200]}")
            return None
        c = resp.json()
        sha = c.get("id") or ""
        return {
            "short_id": c.get("short_id") or (sha[:7] if sha else ""),
            "id": sha,
            "title": c.get("title") or "",
            "message": c.get("message") or "",
            "author_name": c.get("author_name"),
            "author_email": c.get("author_email"),
            "created_at": c.get("created_at") or c.get("authored_date"),
        }
    except Exception as e:  # noqa: BLE001
        print(f"[source_ref_service] GitLab commit 请求异常: {e}")
        return None


def _fetch_github_commit(owner: str, repo_name: str, ref: str, token: str | None) -> str | None:
    """调 GitHub API 拿 commit SHA。"""
    detail = _fetch_github_commit_detail(owner, repo_name, ref, token)
    return detail.get("id") if detail else None


def _fetch_github_commit_detail(
    owner: str, repo_name: str, ref: str, token: str | None, *, timeout: float = 15
) -> dict | None:
    url = f"https://api.github.com/repos/{owner}/{repo_name}/commits/{urllib.parse.quote(ref, safe='')}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"[source_ref_service] GitHub commit {resp.status_code}: {url}")
            return None
        data = resp.json()
        author = (data.get("commit") or {}).get("author") or {}
        msg = (data.get("commit") or {}).get("message") or ""
        sha = data.get("sha") or ""
        return {
            "short_id": sha[:7],
            "id": sha,
            "title": msg.split("\n", 1)[0],
            "message": msg,
            "author_name": author.get("name"),
            "author_email": author.get("email"),
            "created_at": author.get("date"),
        }
    except Exception as e:  # noqa: BLE001
        print(f"[source_ref_service] GitHub commit 请求异常: {e}")
        return None


def fill_source_ref_later(release_id: int) -> None:
    """发布单落库后再去仓库问 SHA，不占用创建接口。

    GitLab/GitHub 超时或失败只记日志；已经有 source_ref（调用方传入或 Agent 回写）就不再覆盖。
    """
    threading.Thread(
        target=_fill_source_ref,
        args=(int(release_id),),
        name=f"source-ref-{release_id}",
        daemon=True,
    ).start()


def _fill_source_ref(release_id: int) -> None:
    """后台补写一次 commit SHA。"""
    from app.db.session import SessionLocal
    from app.modules.pipeline.models import Pipeline, Release

    try:
        with SessionLocal() as db:
            r = db.get(Release, release_id)
            if r is None or (r.source_ref or "").strip():
                return
            pipe = db.get(Pipeline, r.pipeline_id)
            if pipe is None:
                return
            auto_ref = resolve_source_ref(db, pipe)
            if not auto_ref:
                return
            db.refresh(r)
            if (r.source_ref or "").strip():
                return
            r.source_ref = auto_ref
            db.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[source_ref_service] 后台补写 source_ref 失败 release={release_id}: {e}")


def resolve_source_ref(db, pipeline) -> str | None:
    """从流水线第一个 git-checkout 步骤自动获取 commit SHA。

    失败返回 None（不抛异常），让发布流程继续。
    """
    try:
        from app.modules.pipeline.schemas import parse_yaml

        definition = parse_yaml(pipeline.yaml)
        repo_url, ref, provider = _find_first_git_checkout(definition, db)
        if not repo_url or not ref:
            return None

        repo = _find_repo(db, repo_url)
        token = _resolve_token(db, repo)

        u = urllib.parse.urlparse(repo_url)
        path = _strip_git_suffix(u.path)
        if not path:
            return None

        provider_lower = (provider or "").lower()
        if "gitlab" in repo_url or "gitlab" in provider_lower:
            api_base = f"{u.scheme}://{u.netloc}"
            return _fetch_gitlab_commit(api_base, path, ref, token)
        if "github" in repo_url or "github" in provider_lower:
            parts = path.split("/", 1)
            if len(parts) != 2:
                return None
            return _fetch_github_commit(parts[0], parts[1], ref, token)
    except Exception as e:  # noqa: BLE001
        print(f"[source_ref_service] 自动获取 commit 失败（不影响发布）: {e}")
    return None


def _fetch_gitlab_compare(api_base: str, path: str, start: str, end: str, token: str | None) -> list[dict]:
    """GitLab compare API：拿 from..to 区间的所有 commits。"""
    project_id = urllib.parse.quote(path, safe="")
    url = f"{api_base}/api/v4/projects/{project_id}/repository/compare"
    try:
        with httpx.Client(timeout=15, verify=False) as client:
            resp = client.get(
                url,
                params={"from": start, "to": end, "straight": "true"},
                headers=_gitlab_headers(token),
            )
        if resp.status_code != 200:
            print(f"[source_ref_service] GitLab compare {resp.status_code}: {url} from={start} to={end} body={resp.text[:200]}")
            return []
        data = resp.json()
        return [
            {
                "short_id": c.get("short_id"),
                "id": c.get("id"),
                "title": c.get("title"),
                "message": c.get("message"),
                "author_name": c.get("author_name"),
                "author_email": c.get("author_email"),
                "created_at": c.get("created_at"),
            }
            for c in (data.get("commits") or [])
        ]
    except Exception as e:  # noqa: BLE001
        print(f"[source_ref_service] GitLab compare 异常: {e}")
        return []


def _fetch_github_compare(owner: str, repo_name: str, start: str, end: str, token: str | None) -> list[dict]:
    """GitHub compare API：拿 start...end 区间的所有 commits。"""
    url = f"https://api.github.com/repos/{owner}/{repo_name}/compare/{start}...{end}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"[source_ref_service] GitHub compare {resp.status_code}: {url}")
            return []
        out = []
        for c in (resp.json().get("commits") or []):
            author = (c.get("commit") or {}).get("author") or {}
            msg = (c.get("commit") or {}).get("message") or ""
            sha = c.get("sha") or ""
            out.append({
                "short_id": sha[:7],
                "id": sha,
                "title": msg.split("\n", 1)[0],
                "message": msg,
                "author_name": author.get("name"),
                "author_email": author.get("email"),
                "created_at": author.get("date"),
            })
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[source_ref_service] GitHub compare 异常: {e}")
        return []


# 审批列表补 commit 标题时的上限：待办通常就几张，避免「全部记录」把 GitLab 打满
_HEADLINE_FETCH_CAP = 10
_HEADLINE_TIMEOUT = 3.0


def _git_access(db, pipeline) -> dict | None:
    """流水线第一个 git-checkout 对应的仓库访问信息。找不到仓库时返回 None。"""
    from app.modules.pipeline.schemas import parse_yaml

    if pipeline is None:
        return None
    definition = parse_yaml(pipeline.yaml)
    repo_url, _ref, provider = _find_first_git_checkout(definition, db)
    if not repo_url:
        return None
    repo = _find_repo(db, repo_url)
    u = urllib.parse.urlparse(repo_url)
    path = _strip_git_suffix(u.path)
    provider_lower = (provider or "").lower()
    is_gitlab = "gitlab" in repo_url or "gitlab" in provider_lower
    is_github = "github" in repo_url or "github" in provider_lower
    return {
        "repo_url": repo_url,
        "provider": provider,
        "token": _resolve_token(db, repo),
        "path": path,
        "scheme": u.scheme,
        "netloc": u.netloc,
        "is_gitlab": is_gitlab,
        "is_github": is_github,
        "repo": repo,
    }


def _commit_detail(access: dict | None, ref: str, *, timeout: float = 15) -> dict | None:
    """按仓库类型拉一条 commit 详情。"""
    if not access or not (ref or "").strip() or not access.get("path"):
        return None
    if access["is_gitlab"]:
        api_base = f"{access['scheme']}://{access['netloc']}"
        return _fetch_gitlab_commit_detail(
            api_base, access["path"], ref, access["token"], timeout=timeout
        )
    if access["is_github"]:
        parts = access["path"].split("/", 1)
        if len(parts) != 2:
            return None
        return _fetch_github_commit_detail(
            parts[0], parts[1], ref, access["token"], timeout=timeout
        )
    return None


def _snapshot_of(release) -> dict:
    """读发布快照。坏 JSON 当空对象，不让审批列表炸掉。"""
    try:
        data = json.loads(release.snapshot or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember_commit(release, *, title: str, short_id: str) -> bool:
    """把 commit 标题写进快照，审批列表下次不用再打 GitLab。有改动返回 True。"""
    snap = _snapshot_of(release)
    changed = False
    if title and snap.get("commit_title") != title:
        snap["commit_title"] = title
        changed = True
    if short_id and snap.get("commit_short") != short_id:
        snap["commit_short"] = short_id
        changed = True
    if changed:
        release.snapshot = json.dumps(snap, ensure_ascii=False)
    return changed


def headlines_for_releases(db, releases: list) -> dict[int, dict]:
    """审批列表用的短 SHA + 提交说明。

    已缓存在 snapshot 里的直接用；待审批且还没有标题的，按「流水线 + ref」去重后
    最多打几次 GitLab，写回快照。历史单不再现场拉，避免全部记录把接口拖死。
    """
    from app.modules.pipeline.models import Pipeline

    out: dict[int, dict] = {}
    git_by_pipeline: dict[int, dict | None] = {}
    fetched: dict[tuple[int, str], dict | None] = {}
    fetch_left = _HEADLINE_FETCH_CAP
    dirty = False

    for rel in releases:
        if rel is None or getattr(rel, "id", None) is None:
            continue
        snap = _snapshot_of(rel)
        ref = (rel.source_ref or "").strip()
        title = (snap.get("commit_title") or "").strip()
        short = (snap.get("commit_short") or (ref[:8] if ref else "")).strip()
        pending = (rel.status or "") == "pending"
        if title or not ref or not pending or fetch_left <= 0:
            out[rel.id] = {"short_id": short, "title": title}
            continue
        key = (rel.pipeline_id, ref)
        if key not in fetched:
            if rel.pipeline_id not in git_by_pipeline:
                pipe = db.get(Pipeline, rel.pipeline_id)
                try:
                    git_by_pipeline[rel.pipeline_id] = _git_access(db, pipe)
                except Exception:  # noqa: BLE001
                    git_by_pipeline[rel.pipeline_id] = None
            fetched[key] = _commit_detail(
                git_by_pipeline.get(rel.pipeline_id), ref, timeout=_HEADLINE_TIMEOUT
            )
            fetch_left -= 1
        detail = fetched.get(key) or {}
        title = (detail.get("title") or "").strip()
        short = (detail.get("short_id") or short or ref[:8]).strip()
        if title:
            dirty = _remember_commit(rel, title=title, short_id=short) or dirty
        out[rel.id] = {"short_id": short, "title": title}

    if dirty:
        db.commit()
    return out


def fetch_commits_between(db, release) -> dict:
    """拿本次发布 source_ref 与「同 pipeline 上一次发布」source_ref 区间的 commits。

    - 有上次 commit：走 compare
    - 首次 / compare 空：走单 commit 详情 API（避免「未能拉取详情」占位）
    """
    from sqlalchemy import select

    from app.modules.pipeline.models import Pipeline, Release

    end_ref = release.source_ref
    result = {"from": None, "to": end_ref, "range": "—", "commits": [], "provider": None}

    if not end_ref:
        return result

    try:
        prior = db.execute(
            select(Release)
            .where(
                Release.pipeline_id == release.pipeline_id,
                Release.id != release.id,
                Release.source_ref.is_not(None),
            )
            .order_by(Release.id.desc())
        ).scalars().first()

        pipeline = db.get(Pipeline, release.pipeline_id)
        access = _git_access(db, pipeline)
        if access is None:
            return result

        start_ref = prior.source_ref if prior else None
        if start_ref and start_ref == end_ref:
            start_ref = None

        result["from"] = start_ref
        if start_ref:
            result["range"] = f"{start_ref[:7]} ~ {end_ref[:7]}"
        else:
            result["range"] = f"{end_ref[:7]}（首次发布）"

        path = access.get("path") or ""
        if not path:
            return result

        result["provider"] = access.get("provider") or (
            "gitlab" if access["is_gitlab"] else "github" if access["is_github"] else None
        )

        commits: list[dict] = []
        if access["is_gitlab"]:
            api_base = f"{access['scheme']}://{access['netloc']}"
            if start_ref:
                commits = _fetch_gitlab_compare(
                    api_base, path, start_ref, end_ref, access["token"]
                )
            if not commits:
                detail = _commit_detail(access, end_ref)
                if detail:
                    commits = [detail]
        elif access["is_github"]:
            parts = path.split("/", 1)
            if len(parts) == 2:
                if start_ref:
                    commits = _fetch_github_compare(
                        parts[0], parts[1], start_ref, end_ref, access["token"]
                    )
                if not commits:
                    detail = _commit_detail(access, end_ref)
                    if detail:
                        commits = [detail]

        result["commits"] = commits
        if end_ref and not commits:
            print(
                f"[source_ref_service] 无法拉取 commit 详情 path={path} to={end_ref[:8]} "
                f"has_token={bool(access.get('token'))} "
                f"repo_id={getattr(access.get('repo'), 'id', None)}"
            )
            result["commits"] = [
                {
                    "short_id": end_ref[:7],
                    "id": end_ref,
                    "title": "无法获取 commit 详情：请给代码库绑定 GitLab Token，并确保后端能访问 GitLab API",
                    "message": "",
                    "author_name": None,
                    "author_email": None,
                    "created_at": None,
                }
            ]
        if commits:
            first = commits[0]
            _remember_commit(
                release,
                title=(first.get("title") or "").strip(),
                short_id=(first.get("short_id") or "").strip(),
            )
    except Exception as e:  # noqa: BLE001
        print(f"[source_ref_service] fetch_commits_between 失败: {e}")
    return result
