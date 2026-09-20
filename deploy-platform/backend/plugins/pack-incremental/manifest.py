# -*- coding: utf-8 -*-
"""发布清单解析。

发布人员只知道文件在项目里的相对位置，不知道生产服务器的目录结构，所以清单一律
按「相对于编译产物根目录」来写。这份清单是整条链路的唯一事实来源：它决定打进包里
的文件，而包里的文件列表又决定目标机上备份和替换哪些路径。

支持的写法（一行一条，# 开头是注释）：

    bin/*.dll          通配，只匹配 bin 下这一层
    bin/**/*.dll       通配，递归子目录
    Areas/             目录，整个递归打进去
    Content/site.css   单个文件
    !bin/*.pdb         排除，最后统一生效

开头的 / 和 \\ 会被忽略（发布人员习惯写 /Areas/），路径分隔符统一成 /。
"""
from __future__ import annotations

import fnmatch
import posixpath
from pathlib import Path, PurePosixPath


class ManifestError(ValueError):
    """清单本身写错了，属于用户输入问题，要把行号和原因说清楚。"""


def _normalize(line: str, lineno: int) -> str:
    rule = line.strip().replace("\\", "/").lstrip("/")
    if not rule:
        raise ManifestError(f"第 {lineno} 行是空规则")
    # 平台只替换本次执行里存在的变量，认不出来的原样透传。当成路径去匹配的话，
    # 报出来的会是「规则没匹配到文件」，让人以为是路径写错了
    if "${" in rule:
        raise ManifestError(
            f"第 {lineno} 行的变量没被替换：{line.strip()}\n"
            "  DEPLOY_MANIFEST 的值来自发起发布时填写的清单，或「发布提交」单。\n"
            "  请在执行弹窗里填写发布清单，或者把清单直接写死在这个参数里。"
        )
    parts = PurePosixPath(rule).parts
    if ".." in parts:
        raise ManifestError(f"第 {lineno} 行不允许出现 ..：{line.strip()}")
    if len(rule) > 1 and rule[1] == ":":
        raise ManifestError(f"第 {lineno} 行不能写绝对路径：{line.strip()}")
    return rule


def parse(text: str) -> tuple[list[str], list[str]]:
    """把清单文本拆成 (包含规则, 排除规则)。"""
    includes: list[str] = []
    excludes: list[str] = []
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            excludes.append(_normalize(line[1:], lineno))
        else:
            includes.append(_normalize(line, lineno))
    if not includes:
        raise ManifestError("发布清单是空的，至少要写一条要发布的文件或目录")
    return includes, excludes


def _match(rel: str, rule: str) -> bool:
    """判断相对路径是否命中一条规则。"""
    # 目录规则：整个子树都算命中
    if rule.endswith("/"):
        return rel == rule[:-1] or rel.startswith(rule)
    if "*" not in rule and "?" not in rule:
        # 没有通配符时，写目录名也当整个目录处理
        return rel == rule or rel.startswith(rule + "/")
    # ** 交给 fnmatch 递归匹配，单个 * 不能跨目录
    if "**" in rule:
        return fnmatch.fnmatchcase(rel, rule.replace("**/", "*"))
    if posixpath.dirname(rule) != posixpath.dirname(rel):
        return False
    return fnmatch.fnmatchcase(posixpath.basename(rel), posixpath.basename(rule))


def _missing_file_error(rule: str, all_files: list[str]) -> ManifestError:
    """清单写了但编译产物里没有：把原因说清楚，并终止发布。

    最常见是文件名填错，或者这次构建根本没产出这个文件。
    缺一条就整次停，不能把其余文件发出去。
    """
    name = posixpath.basename(rule.rstrip("/"))
    exact = "*" not in rule and "?" not in rule
    same_name = [
        rel for rel in all_files if posixpath.basename(rel) == name
    ] if exact and name else []
    same_ci = []
    if exact and name and not same_name:
        want = name.lower()
        same_ci = [rel for rel in all_files if posixpath.basename(rel).lower() == want]
    lines = [
        f"发布清单里的「{rule}」在编译产物目录里找不到，发布已终止。",
        "可能是文件名写错了，或者这次构建没有编译出这个文件。",
    ]
    if same_name:
        shown = "、".join(same_name[:5])
        extra = f" 等 {len(same_name)} 处" if len(same_name) > 5 else ""
        lines.append(
            f"编译产物里有同名文件：{shown}{extra}。"
            "请把清单改成相对于编译产物根目录的路径后再发布。"
        )
    elif same_ci:
        shown = "、".join(same_ci[:5])
        lines.append(
            f"编译产物里有大小写不同的同名文件：{shown}。"
            "清单匹配区分大小写，请改成产物里的实际路径。"
        )
    elif exact and not rule.endswith("/"):
        lines.append(
            "路径相对于编译产物根目录，例如 bin/Admin.NET.Application.dll；"
            "不要只写文件名（除非它就在根目录）。"
        )
    return ManifestError("\n".join(lines))


def collect(root: Path, text: str) -> list[str]:
    """按清单在 root 下枚举出要发布的文件，返回相对路径（/ 分隔）并去重排序。

    一条规则匹配不到任何文件会直接报错：宁可发布失败，也不能让人以为发出去了、
    实际上漏了一个 dll——那种问题要到线上才会暴露。
    """
    if not root.is_dir():
        raise ManifestError(f"编译产物目录不存在：{root}")

    includes, excludes = parse(text)
    all_files = [
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    ]

    picked: set[str] = set()
    for rule in includes:
        hit = {rel for rel in all_files if _match(rel, rule)}
        if not hit:
            raise _missing_file_error(rule, all_files)
        picked |= hit

    for rule in excludes:
        picked -= {rel for rel in picked if _match(rel, rule)}

    if not picked:
        raise ManifestError("按清单筛完之后一个文件都不剩，请检查排除规则")
    return sorted(picked)
