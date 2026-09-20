"""流水线名匹配的回归。挑错一条就是发错生产，这里必须卡死。"""
from __future__ import annotations

from app.modules.ai.context import MATCH_ALIAS, MATCH_EXACT, MATCH_TOKEN, best_matches, match_score
from app.modules.pipeline.models import Pipeline


def _pipeline(pk: int, name: str) -> Pipeline:
    row = Pipeline(name=name, yaml="")
    row.id = pk
    return row


def test_partial_token_never_beats_full_name() -> None:
    """用户说 test-C，不能被 order-service-test 抢走。"""
    target = _pipeline(8, "test-C")
    decoy = _pipeline(2, "order-service-test")
    text = "test-c 发布一下 admin.net.core.dll"

    assert match_score(text, target) == MATCH_EXACT
    assert match_score(text, decoy) == MATCH_TOKEN
    assert [p.id for p in best_matches(text, [target, decoy])] == [8]


def test_copy_suffix_does_not_match_shorter_name() -> None:
    """order-service-test_copy 不能被 test-c 命中。"""
    assert match_score("test-c 发布", _pipeline(5, "order-service-test_copy")) < MATCH_EXACT


def test_full_name_matches_regardless_of_separator() -> None:
    pipeline = _pipeline(2, "order-service-test")
    assert match_score("发布 order service test", pipeline) == MATCH_EXACT
    assert match_score("order-service-test 上线", pipeline) == MATCH_EXACT


def test_chinese_name_matches_by_substring() -> None:
    pipeline = _pipeline(6, "AI陪练项目生产环境")
    assert match_score("发布 ai陪练项目生产环境", pipeline) == MATCH_EXACT


def test_unrelated_text_does_not_match() -> None:
    assert match_score("看看构建机在线吗", _pipeline(8, "test-C")) == 0


def test_chinese_glue_still_matches_latin_pipeline_name() -> None:
    """「查一下test-C的状态」中英文粘在一起，也要整名命中。"""
    assert match_score("查一下test-C的状态", _pipeline(15, "test-C")) == MATCH_EXACT
    assert match_score("查一下test-C的状态", _pipeline(47, "test-B")) == MATCH_TOKEN
    assert [p.id for p in best_matches("查一下test-C的状态", [_pipeline(15, "test-C"), _pipeline(47, "test-B")])] == [15]


def test_best_matches_keeps_all_ties() -> None:
    a = _pipeline(1, "pay-service-ci")
    b = _pipeline(2, "pay-service-ci")
    assert len(best_matches("发布 pay-service-ci", [a, b])) == 2


def test_yaml_name_does_not_tie_display_name() -> None:
    """副本流水线 YAML 仍写着 test-C 时，用户说 test-C 只能中显示名为 test-C 的那条。"""
    target = _pipeline(15, "test-C")
    target.yaml = "pipeline:\n  name: test-C\n"
    copy = _pipeline(47, "test-B")
    copy.yaml = "pipeline:\n  name: test-C\n"
    text = "发布 test-C 流水线"
    assert match_score(text, target) == MATCH_EXACT
    assert match_score(text, copy) == MATCH_ALIAS
    assert [p.id for p in best_matches(text, [target, copy])] == [15]
