# -*- coding: utf-8 -*-
"""失败通知里的错误摘要和建议：必须点名真实文件，不能把模型思考过程发出去。"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.modules.ai.followup import (
    cached_diagnosis_covers_logs,
    load_cached_diagnosis,
    parse_suggestions,
    pick_error_snippet,
)

PACK_LOG = [
    "  step: pack-incremental",
    "    $ python3 task.py",
    '      [ERROR]: 发布清单里的「Microsoft.Extensions.Http.dll」在编译产物目录里找不到，发布已终止。',
    "      可能是文件名写错了，或者这次构建没有编译出这个文件。",
    "      编译产物里有同名文件：bin/Microsoft.Extensions.Http.dll。请把清单改成相对于编译产物根目录的路径后再发布。",
    "    (exit 1)",
    "    step failed, job failed",
]


def test_pack_incremental_keeps_dll_name_not_policy_line():
    """清单写错 dll 时必须留下带文件名的 [ERROR]，不能改成最后一条政策说明。"""
    got = pick_error_snippet(PACK_LOG)
    blob = " ".join(got)
    assert "Microsoft.Extensions.Http.dll" in blob
    assert "找不到" in blob
    assert "不会跳过这一条" not in blob
    assert "(exit 1)" not in blob
    assert "bin/Microsoft.Extensions.Http.dll" in blob


def test_docker_build_keeps_missing_target_not_wrapper():
    """buildkit 把 COPY 失败打成 INFO，[ERROR] 只有「镜像构建失败」时必须留下 target。"""
    log = [
        "[INFO]: #6 [2/3] COPY ./target/cics-service.jar cics-service.jar",
        "[INFO]: ERROR: failed to solve: failed to compute cache key: "
        "failed to calculate checksum of ref abcdef::litc4: "
        "failed to walk /data/docker/tmp/buildkit-mount1945328869/target: "
        "lstat /data/docker/tmp/buildkit-mount1945328869/target: no such file or directory",
        "[ERROR]: 镜像构建失败",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    blob = " ".join(got)
    assert "no such file" in blob
    assert "target" in blob
    assert "COPY" in blob
    assert "镜像构建失败" not in got[0]


def test_maven_keeps_javac_not_exit_code_wrapper():
    """Maven 把 javac 打成 INFO，收尾是退出码时必须留下 .java。"""
    log = [
        "[INFO]: [ERROR] /src/main/java/com/qx/Foo.java:[12,5] error: cannot find symbol",
        "[ERROR]: Maven 构建失败（退出码 1）",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    blob = " ".join(got)
    assert "Foo.java" in blob
    assert "cannot find symbol" in blob
    assert "退出码" not in got[0]


def test_msbuild_keeps_cs_error_not_exit_code_wrapper():
    """MSBuild 编译器编号在 INFO 里时，不能只展示「编译失败，退出码」。"""
    log = [
        "[INFO]: Program.cs(10,20): error CS0246: The type or namespace name 'Bar' could not be found",
        "[ERROR]: MSBuild 编译失败，退出码 1",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    blob = " ".join(got)
    assert "error CS0246" in blob
    assert "Program.cs" in blob
    assert "退出码" not in got[0]


def test_npm_keeps_missing_script_not_exit_code_wrapper():
    """npm 缺 script 在 INFO 里时，不能只展示退出码收尾。"""
    log = [
        '[INFO]: npm ERR! Missing script: "build"',
        "[ERROR]: npm run build 失败（退出码 1）",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    blob = " ".join(got)
    assert "Missing script" in blob
    assert "退出码" not in got[0]


def test_docker_compile_keeps_compiler_error_not_wrapper():
    """容器内编译把 CS/javac 打成 INFO，收尾「容器内编译失败」不能当摘要。"""
    log = [
        "[INFO]: error CS0006: Metadata file 'Foo.dll' could not be found",
        "[ERROR]: 容器内编译失败",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    blob = " ".join(got)
    assert "error CS0006" in blob
    assert "Foo.dll" in blob
    assert "容器内编译失败" not in got[0]


def test_wrapper_only_still_shows_wrapper():
    """日志里只有收尾句时仍要展示，总好过空白。"""
    got = pick_error_snippet(["[ERROR]: 镜像构建失败", "(exit 1)", "step failed, job failed"])
    assert got == ["镜像构建失败"]


def test_k8s_keeps_timeout_not_status_header():
    """k8s 超时长句必须留下；「当前工作负载状态：」只是 kubectl 标题。"""
    log = [
        "[ERROR]: 滚动更新没有在超时时间内完成，新版本可能起不来",
        "[ERROR]: 当前工作负载状态：",
        "[INFO]: Name: cics-service",
        "[ERROR]: 可在平台上对本次发布执行回滚，回到 revision 5",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    blob = " ".join(got)
    assert "超时时间内完成" in blob
    assert "当前工作负载状态" not in blob
    assert "回滚" not in got[0]


def test_git_checkout_keeps_missing_ref():
    """git 自己写清的 [ERROR] 带分支名，不能被当成收尾丢掉。"""
    log = [
        '[ERROR]: 指定的分支/标签「release/v2」拉不下来（仓库里没有，或构建机访问不到）。'
        "已终止，不会改去拉默认分支。",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    blob = " ".join(got)
    assert "release/v2" in blob
    assert "拉不下来" in blob


def test_image_slash_in_push_wrapper_does_not_beat_unauthorized():
    """镜像名带斜杠不是路径；docker login 失败时应留下 unauthorized。"""
    log = [
        "[INFO]: unauthorized: authentication required",
        "[ERROR]: 推送 registry.example.com/app:1.0 失败",
        "(exit 1)",
        "step failed, job failed",
    ]
    got = pick_error_snippet(log)
    assert "unauthorized" in got[0]
    assert "推送" not in got[0]


def test_parse_suggestions_drops_duplicate():
    raw = """
1. 在目录中执行 docker build --no-cache --progress=plain .
2. 在目录中执行 docker build --no-cache --progress=plain .
"""
    got = parse_suggestions(raw)
    assert len(got) == 1


def test_parse_suggestions_drops_prompt_echo():
    raw = """
1. 检查构建输出目录，确认缺失的 DLL 是否已生成
2. 在项目文件中显式添加对应 DLL 的引用
3. 审查构建脚本中的错误处理逻辑我们被要求根据失败日志摘要给出最多3条可执行建议，每条一行，不要标题，不要客套。看起来是构建环境Windows阶段
"""
    got = parse_suggestions(raw)
    assert len(got) == 2
    assert all("我们被要求" not in x for x in got)
    assert "检查构建输出目录" in got[0]


def test_parse_suggestions_empty_when_only_thinking():
    raw = (
        "我们被要求根据失败日志摘要给出最多3条可执行建议，每条一行。"
        "给出的日志是 stage-1 不会跳过这一条。所以，现在构建失败。"
    )
    assert parse_suggestions(raw) == []


def test_load_cached_prefers_diagnosis_text():
    release = SimpleNamespace(id=20, diagnosis_text="【错误摘要】dll 找不到")
    db = MagicMock()
    assert load_cached_diagnosis(db, release) == "【错误摘要】dll 找不到"
    db.scalar.assert_not_called()


def test_load_cached_backfills_from_failed_notice():
    """已经发过站内通知的历史失败，诊断抽屉应直接用通知正文。"""
    release = SimpleNamespace(id=20, diagnosis_text="  ")
    db = MagicMock()
    db.scalar.return_value = "【发布结果】失败\n【建议】1. 把清单改成 bin/Microsoft.Extensions.Http.dll"
    got = load_cached_diagnosis(db, release)
    assert "bin/Microsoft.Extensions.Http.dll" in got
    assert release.diagnosis_text == got


def test_cached_stale_when_notice_only_has_wrapper(monkeypatch) -> None:
    """旧通知只有「镜像构建失败」时，打开诊断应按新规则重抽，不必点重新诊断。"""
    from app.modules.ai import followup as followup_mod

    monkeypatch.setattr(
        followup_mod,
        "_error_briefs",
        lambda db, release: [
            "· stage-1/构建：failed to walk /data/tmp/target: no such file or directory"
        ],
    )
    cached = "【发布结果】失败\n【错误摘要】\n· stage-1/构建：镜像构建失败"
    assert not cached_diagnosis_covers_logs(MagicMock(), SimpleNamespace(id=74), cached)


def test_cached_covers_when_notice_already_has_locator(monkeypatch) -> None:
    """通知已含关键句时仍复用，不再请求模型。"""
    from app.modules.ai import followup as followup_mod

    snippet = "failed to walk /data/tmp/target: no such file or directory"
    monkeypatch.setattr(
        followup_mod,
        "_error_briefs",
        lambda db, release: [f"· stage-1/构建：{snippet}"],
    )
    cached = f"【发布结果】失败\n【错误摘要】\n· stage-1/构建：{snippet}"
    assert cached_diagnosis_covers_logs(MagicMock(), SimpleNamespace(id=74), cached)


def test_suggest_docker_copy_not_triggered_by_maven_target(monkeypatch) -> None:
    """规则建议不能只凭 target 就当成 Dockerfile COPY；Maven 编译失败不该出那条。"""
    from app.modules.ai.followup import _suggest

    monkeypatch.setattr(
        "app.modules.llm.service.build_client",
        lambda db: (_ for _ in ()).throw(RuntimeError("skip llm")),
    )
    tips = _suggest(MagicMock(), ["· maven：Foo.java:[12,5] error: cannot find symbol"])
    assert all("Dockerfile" not in t and "COPY" not in t for t in tips)


def test_suggest_docker_copy_when_buildkit_walk_fails(monkeypatch) -> None:
    """buildkit failed to walk 时规则应提示 Dockerfile COPY / 构建上下文。"""
    from app.modules.ai.followup import _suggest

    monkeypatch.setattr(
        "app.modules.llm.service.build_client",
        lambda db: (_ for _ in ()).throw(RuntimeError("skip llm")),
    )
    tips = _suggest(
        MagicMock(),
        ["· docker：failed to walk /data/tmp/target: no such file | COPY ./target/app.jar"],
    )
    assert any("Dockerfile" in t and "COPY" in t for t in tips)


def test_watch_access_from_traces_starts_followup(monkeypatch) -> None:
    from app.modules.ai.followup import watch_access_from_traces

    calls = []
    monkeypatch.setattr(
        "app.modules.ai.followup.add_access_watch",
        lambda db, conversation_id, user_id, application_id: calls.append(application_id),
    )
    traces = [
        {"name": "apply_project_execute", "result": {"application_id": 3, "reply": "已提交"}},
        {"name": "apply_pipeline_execute", "result": {"error": "已有权限"}},
    ]
    assert watch_access_from_traces(MagicMock(), 11, 2, traces) is True
    assert calls == [3]


def test_format_access_followup_approved() -> None:
    from app.modules.ai.followup import format_access_followup

    app = SimpleNamespace(
        id=3,
        status="approved",
        project_name="DMS",
        group_name="（全部环境分组）",
        pipeline_name="（项目下全部流水线）",
        reviewer_id=None,
        review_comment="ok",
    )
    text = format_access_followup(MagicMock(), app)
    assert "已通过 #3" in text
    assert "查看" in text
    assert "执行" in text


def test_complete_access_watches_appends_once(monkeypatch) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.base import Base
    from app.modules.ai.followup import add_access_watch, complete_access_watches
    from app.modules.ai.models import AiWatch
    from app.modules.auth.models import PermissionApplication

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AiWatch.__table__, PermissionApplication.__table__])
    db = Session(engine)
    row = PermissionApplication(
        applicant_id=1,
        project_id=1,
        group_id=0,
        pipeline_id=0,
        apply_type="project_execute",
        project_name="DMS",
        group_name="全部",
        pipeline_name="全部流水线",
        reason="",
        status="approved",
        review_comment="通过",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    add_access_watch(db, conversation_id=9, user_id=1, application_id=row.id)
    posted = []
    monkeypatch.setattr(
        "app.modules.ai.followup.append_message",
        lambda db, cid, role, content, kind="chat": posted.append((cid, content, kind)),
    )
    n = complete_access_watches(db, row)
    assert n == 1
    assert posted[0][0] == 9
    assert posted[0][2] == "followup"
    assert "已通过" in posted[0][1]
    assert db.query(AiWatch).filter(AiWatch.status == "done").count() == 1
