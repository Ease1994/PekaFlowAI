"""环境码：分组、构建机、节点共用这一套，匹配必须全等。

以前只有 prod/test，而且流水线侧写死「不是 test 就当 prod」。
建一个 type=uat 的分组，任务会被派到生产机构建、发到生产节点——隔离形同虚设。
所以：合法码原样用，非法码直接拒绝，绝不默默当成生产。
"""
from __future__ import annotations

import re

from app.core.response import BizException

# 内置码。可以再建自定义码（如 train、sandbox），规则见 ENV_SLUG。
KNOWN: dict[str, str] = {
    "prod": "生产",
    "test": "测试",
    "uat": "UAT",
    "staging": "预发",
    "dev": "开发",
}

# 这些环境的节点，AI 下发文件可以免审批。其余（含未知自定义码）一律要审。
# 匹配隔离和审批是两件事：匹配必须全等；审批按「这台像不像生产」失败关闭。
SKIP_NODE_PUSH_APPROVAL = frozenset({"test", "dev"})

# 「随手可丢」的环境：产物发完就没用了，可以隔夜清；发布成功也不必邮件打扰。
# 其余（prod/uat/staging/自定义）都要留着回滚和追溯。
#
# 初值和 SKIP_NODE_PUSH_APPROVAL 相同但刻意分开定义：一个管审批闸门，一个管数据留存，
# 将来只想放宽其中一个时，不会连带把另一个也放开。
DISPOSABLE = frozenset({"test", "dev"})

ENV_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")


def env_label(code: str | None) -> str:
    c = (code or "").strip().lower()
    if not c:
        return "未标环境"
    return KNOWN.get(c, c)


def normalize_env(raw: str | None, *, default: str = "prod", field: str = "环境") -> str:
    """空值用 default；非法格式直接拒绝，不改写成 prod。"""
    text = (raw or "").strip().lower()
    if not text:
        return default
    if not ENV_SLUG.fullmatch(text):
        raise BizException.bad_request(
            f"{field}码不合法「{raw}」，只允许小写字母开头、最多 16 位的字母数字-_，"
            f"内置：{'、'.join(KNOWN)}"
        )
    return text


def skip_node_push_approval(code: str | None) -> bool:
    """这台节点下发文件能不能免审批。空值按生产（要审）。"""
    return (code or "prod").strip().lower() in SKIP_NODE_PUSH_APPROVAL


def is_disposable(code: str | None) -> bool:
    """这套环境的产物能不能隔夜清掉。空值按生产（要留）。

    UAT / 预发不在此列：它们也要回滚，包被当成测试包清掉是找不回来的。
    """
    return (code or "prod").strip().lower() in DISPOSABLE


def pipeline_env_of(group_type: str | None) -> str:
    """流水线跑哪套环境。分组 type 空或非法就报错，绝不默默当成生产。"""
    text = (group_type or "").strip().lower()
    if not text:
        raise BizException.bad_request(
            "这条流水线所属环境分组没有环境码，无法隔离构建机和节点。"
            "请到项目详情把该分组的环境设成生产/测试/UAT/预发/开发"
        )
    return normalize_env(text, field="环境分组")


def machine_env(raw: str | None) -> str:
    """机器上标的环境码。空串表示未标明，不能匹配任何流水线。

    以前空值按 prod：老机器没填过就会领生产任务。生产和测试要强制隔离，
    未标明的构建机/节点哪边都不能进，管理员到页面上标好再干活。
    """
    return (raw or "").strip().lower()


def assert_same_env(src: str, dst: str, *, action: str) -> None:
    """生产和测试（以及其它不同环境码）不能互相调用。匹配必须全等。"""
    a = (src or "").strip().lower()
    b = (dst or "").strip().lower()
    if a and b and a == b:
        return
    raise BizException.bad_request(
        f"环境隔离：{env_label(src)}不能{action}{env_label(dst)}。"
        "测试流水线不能调用生产流水线，也不能用生产的构建机和节点；反之亦然。"
    )


# 平台内置项目：AI「节点文件下发」挂在这里。这条线跨所有环境，
# 分组固定 prod 只为「默认要审」；真正按节点自身环境决定能不能免审。
PLATFORM_PROJECT_CODE = "_platform"


def is_platform_project(code: str | None) -> bool:
    return (code or "").strip() == PLATFORM_PROJECT_CODE
