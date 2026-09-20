"""本轮用户话在说什么。路由、工具裁剪、技能拦截共用这一套，避免各写一套正则。"""
from __future__ import annotations

import re

# 要的是「能跑流水线」的权限，不是查申请单、也不是发布。
# 「执行发布权限」是现场常说的口吻，中间多了「发布」两个字；只认「执行权限」会漏判，
# 后面的发布动词再把这句话当成真正去发布。
_ACCESS_APPLY = re.compile(
    r"(执行发布权限|执行权限|发布权限|申请权限|申请执行|权限申请|执行权|"
    r"要.{0,8}权限|申请.{0,80}权限|申请.{0,80}角色|加入.{0,40}角色)"
)
_ROLE_APPLY = re.compile(r"(申请.{0,80}角色|加入.{0,40}角色|要.{0,20}角色)")
_ACCESS_LOOKUP_MINE = re.compile(r"(我的申请|申请怎么样|申请进度|我交的申请)")
_ACCESS_LOOKUP_PENDING = re.compile(r"(待审批|待我审批|待审核)")
_PROJECT_WIDE = re.compile(
    r"(整个项目|所有流水线|全部流水线|项目下的?所有|项目所有|该项目全部|整个项目的)"
)
_GROUP_WIDE = re.compile(
    r"(测试环境|生产环境|线上环境|预发环境|开发环境|uat环境|"
    r"只要测试|只要生产|只要预发|只要开发|只要uat|"
    r"该环境|这个环境|环境分组)"
)
_PROJECT_ID = re.compile(
    r"(?:项目\s*[#＃号]?\s*(\d+))|(?:(?:^|[^\d])(\d+)\s*号?项目)"
)
_APPLICATION_ID = re.compile(r"(?:申请|#|单号)\s*(\d+)")
_REVIEW = re.compile(r"(通过|批准|同意|驳回|拒绝).{0,16}(申请|权限|#\d+)")
_CANCEL = re.compile(r"(撤销|取消).{0,12}(申请|权限)")


# 装/卸技能。技能中文名经常不含「技能」二字（读取未读通知），对象里带上未读/通知。
_SKILL_LIFECYCLE = re.compile(
    r"(安装|卸载|卸掉|停用|启用|装一下|卸了|装上).{0,32}"
    r"(技能|插件|skill|plugin|未读|通知|流水线状态|发布状态)",
    re.I,
)


def is_skill_lifecycle_utterance(message: str) -> bool:
    """用户在装、卸、停用或启用技能/插件，不是在使用对应能力。

    直达路径（查未读、查状态、发布、申请权限）遇到这种句子应让开，
    交给对话循环里的模型选 propose_agent_skill_lifecycle，不再另开一轮分类。
    """
    return bool(_SKILL_LIFECYCLE.search(message or ""))


def is_access_apply_intent(message: str) -> bool:
    """这句话是在申请执行权，不是查审批进度、也不是发布。"""
    text = (message or "").strip()
    if not text:
        return False
    if is_skill_lifecycle_utterance(text):
        return False
    if is_access_lookup_mine(text) or is_access_lookup_pending(text):
        return False
    if is_access_review_intent(text) or is_access_cancel_intent(text):
        return False
    return bool(_ACCESS_APPLY.search(text))


def is_access_lookup_mine(message: str) -> bool:
    return bool(_ACCESS_LOOKUP_MINE.search(message or ""))


def is_access_lookup_pending(message: str) -> bool:
    return bool(_ACCESS_LOOKUP_PENDING.search(message or ""))


def is_access_review_intent(message: str) -> bool:
    return bool(_REVIEW.search(message or ""))


def is_access_cancel_intent(message: str) -> bool:
    return bool(_CANCEL.search(message or ""))


_NOT_RELEASE = re.compile(
    r"(传到|发到|下发|传文件|申请.{0,80}权限|执行发布权限|执行权限|发布权限|权限申请|"
    r"发布状态|发布记录|发布审批|查询发布|正在发布|"
    r"发布得怎样|发布怎么样|发布的怎样|发得怎样|发布进度|"
    r"取消.{0,12}发布|回滚|rebuild|"
    r"(?:查询|列出|有哪些|全部|查一下).{0,12}(?:流水线|发布|项目|状态))"
)
# 「执行某项目某环境」只是一种说法。漏判时仍把 propose_release 交给模型，由工具解析槽位。
# 「发布」不能吞掉「发布权限」：那是在要权，不是在跑流水线。
_RELEASE_VERB = re.compile(
    r"(发布(?!权限|权)|部署|上线|发一下|跑一下|"
    r"执行(?!权限|权|发布).{0,40}(项目|流水线|环境|生产|测试|预发|uat)|"
    r"执行.{0,16}流水线)"
)


def is_release_intent(message: str) -> bool:
    """高置信像在跑流水线时，用来收窄工具、尝试直达。

    这不是 NLU 全集。对不上的说法返回 False，对话循环把 propose_release 留给模型填槽。
    """
    text = (message or "").strip()
    if not text or _NOT_RELEASE.search(text):
        return False
    if is_skill_lifecycle_utterance(text):
        return False
    if is_access_apply_intent(text):
        return False
    return bool(_RELEASE_VERB.search(text))


def is_role_access_intent(message: str) -> bool:
    """这句话是在申请项目角色，不是申请流水线动作。"""
    text = (message or "").strip()
    if not text or not is_access_apply_intent(text):
        return False
    return bool(_ROLE_APPLY.search(text))


def is_project_wide_access(message: str) -> bool:
    """申请范围是整个项目（含所有环境），不是某一条流水线、也不是单个环境。"""
    text = (message or "").strip()
    if not is_access_apply_intent(text):
        return False
    if parse_env_code(text) or is_group_wide_access(text):
        return False
    return bool(_PROJECT_WIDE.search(text))


def is_group_wide_access(message: str) -> bool:
    """申请范围是项目下某一个环境分组。"""
    text = (message or "").strip()
    if not is_access_apply_intent(text):
        return False
    return bool(_GROUP_WIDE.search(text)) or bool(parse_env_code(text))


def parse_project_id(message: str) -> int | None:
    """从「1项目 / 项目1 / 项目#1」里抽出项目 id。抽不到返回 None。"""
    m = _PROJECT_ID.search(message or "")
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def parse_application_id(message: str) -> int | None:
    """从「通过 #12」「撤销申请 12」里抽出申请单号。"""
    m = _APPLICATION_ID.search(message or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if n > 0 else None


def parse_env_code(message: str) -> str:
    """抽出环境码。只认「测试环境 / 只要生产」这类说法，避免把 test-C 里的 test 当成环境。"""
    text = message or ""
    patterns = (
        (re.compile(r"只要\s*测试|测试\s*环境|测试\s*分组"), "test"),
        (re.compile(r"只要\s*生产|生产\s*环境|线上\s*环境|生产\s*分组"), "prod"),
        (re.compile(r"只要\s*uat|uat\s*环境", re.I), "uat"),
        (re.compile(r"只要\s*预发|预发\s*环境|staging\s*环境", re.I), "staging"),
        (re.compile(r"只要\s*开发|开发\s*环境"), "dev"),
        (re.compile(r"(?<![A-Za-z])(?:env\s*[=:：]?\s*)?(test|prod|uat|dev|staging)(?=\s*环境|\s*$)", re.I), None),
    )
    hits: list[str] = []
    for pattern, code in patterns:
        m = pattern.search(text)
        if not m:
            continue
        resolved = code or m.group(1).lower()
        if resolved not in hits:
            hits.append(resolved)
    # 续接短回复只说「测试 / 生产」，整句就是环境名时才认，避免 test-C 被当成环境。
    if not hits:
        bare = re.fullmatch(
            r"\s*(测试|生产|线上|uat|预发|开发|test|prod|staging|dev)\s*",
            text,
            re.I,
        )
        if bare:
            word = bare.group(1).lower()
            return {
                "测试": "test",
                "生产": "prod",
                "线上": "prod",
                "uat": "uat",
                "预发": "staging",
                "开发": "dev",
                "test": "test",
                "prod": "prod",
                "staging": "staging",
                "dev": "dev",
            }.get(word, word)
    return hits[0] if len(hits) == 1 else ""


def normalize_env_slot(raw: str) -> str:
    """把模型填的环境槽收成内部码。

    槽里可能是 prod，也可能是「生产」「线上」。整句解析不到时，再补一个「环境」后缀试一次。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    return parse_env_code(text) or parse_env_code(f"{text}环境")
