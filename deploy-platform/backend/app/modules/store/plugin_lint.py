"""插件草稿的静态校验。

插件是在构建机上以 Agent 子进程身份执行的任意代码，来源又可能是 AI 生成的，
所以在它进入插件仓库之前先过一遍：manifest 合法性、表单 DSL 合法性、危险代码模式。

这里只做静态检查，拦不住有心人（正则绕过很容易），目的是把「明显不该进仓库的东西」
和「需要人多看两眼的东西」区分出来。进仓库和安装仍要管理员，因为插件在构建机上是任意代码。
"""
from __future__ import annotations

import re

# 校验结论分三档：
#   error —— 一定不合法，改完才能存
#   high  —— 语法上合法但危险，发布时必须管理员显式确认
#   warn  —— 值得看一眼，不拦
LEVEL_ERROR = "error"
LEVEL_HIGH = "high"
LEVEL_WARN = "warn"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
FIELD_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

LANGUAGES = {"python", "java", "nodejs"}
CATEGORIES = {
    "source", "build", "deploy", "notify", "trigger", "exec", "artifact", "pipeline",
}
FIELD_TYPES = {
    "text", "textarea", "password", "number", "radio", "select",
    "checkbox", "switch", "code", "group",
}

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

# (级别, 代码, 正则, 说明)
CODE_PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    (LEVEL_HIGH, "agent_token", re.compile(r"RELEASE_AGENT_TOKEN|get_agent_token"),
     "试图获取构建机长期凭证，插件只应使用 RELEASE_TASK_TOKEN"),
    (LEVEL_HIGH, "dynamic_exec", re.compile(r"\beval\s*\(|\bexec\s*\(|__import__\s*\(|pickle\.loads"),
     "动态执行代码，无法审阅实际行为"),
    (LEVEL_HIGH, "pipe_to_shell", re.compile(r"(curl|wget)[^\n|]*\|\s*(ba)?sh"),
     "从网络下载脚本直接执行"),
    (LEVEL_HIGH, "agent_home", re.compile(r"\.release-agent"),
     "读写 Agent 自身目录（插件缓存、凭证都在这里）"),
    (LEVEL_HIGH, "destructive_fs", re.compile(r"rm\s+-rf\s+[/~]|shutil\.rmtree\s*\(\s*[\"']?[/~]"),
     "对根目录或用户目录做递归删除"),
    (LEVEL_WARN, "subprocess", re.compile(
        r"subprocess\.|os\.system\s*\(|os\.popen\s*\(|child_process|Runtime\.getRuntime\(\)\.exec"),
     "会起子进程执行命令（很多插件确实需要，确认命令来源可控）"),
    (LEVEL_WARN, "network", re.compile(
        r"urllib\.request|requests\.|http\.client|socket\.socket|fetch\s*\(|HttpURLConnection"),
     "会发起网络请求，确认目标地址是否可信"),
    (LEVEL_WARN, "credential_path", re.compile(
        r"/etc/passwd|/etc/shadow|\.ssh/|id_rsa|\.git-credentials|\.npmrc|\.docker/config\.json"),
     "读取了凭证或系统敏感路径"),
    (LEVEL_WARN, "obfuscation", re.compile(r"base64\.b64decode|codecs\.decode|Buffer\.from\([^)]*base64"),
     "存在编解码操作，注意是否在隐藏真实逻辑"),
]

# 疑似内嵌二进制/混淆载荷
LONG_BLOB_RE = re.compile(r"[A-Za-z0-9+/=]{300,}")

ENTRY_HINT = {
    "python": ("python", "python3 task.py"),
    "nodejs": ("node", "node task.js"),
    "java": ("java", "java -cp . com.example.Main"),
}


def _finding(level: str, code: str, message: str, where: str = "") -> dict:
    return {"level": level, "code": code, "message": message, "where": where}


def lint_manifest(meta: dict, files: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    name = str(meta.get("name") or "").strip()
    if not NAME_RE.match(name):
        out.append(_finding(
            LEVEL_ERROR, "bad_name",
            "插件标识只能是小写字母开头的字母、数字、连字符，长度 2-64",
            "task.json:name",
        ))

    language = str(meta.get("language") or "").strip()
    if language not in LANGUAGES:
        out.append(_finding(
            LEVEL_ERROR, "bad_language",
            f"language 只支持 {'/'.join(sorted(LANGUAGES))}",
            "task.json:language",
        ))

    entrypoint = str(meta.get("entrypoint") or "").strip()
    if not entrypoint:
        out.append(_finding(LEVEL_ERROR, "no_entrypoint", "缺少 entrypoint", "task.json:entrypoint"))
    elif language in ENTRY_HINT:
        keyword, example = ENTRY_HINT[language]
        if keyword not in entrypoint:
            out.append(_finding(
                LEVEL_ERROR, "entrypoint_mismatch",
                f"language={language} 的 entrypoint 应形如「{example}」",
                "task.json:entrypoint",
            ))

    # entrypoint 指向的脚本必须真的在包里，否则装上去也是必然失败
    if entrypoint and language in ("python", "nodejs"):
        referenced = [tok for tok in entrypoint.split() if "." in tok and "/" not in tok]
        for token in referenced:
            if token not in files:
                out.append(_finding(
                    LEVEL_ERROR, "missing_entry_file",
                    f"entrypoint 引用了 {token}，但插件文件里没有它",
                    "task.json:entrypoint",
                ))

    version = str(meta.get("version") or "").strip()
    if version and not VERSION_RE.match(version):
        out.append(_finding(
            LEVEL_WARN, "bad_version", "version 建议用 1.0.0 这样的语义化版本", "task.json:version",
        ))

    category = str(meta.get("category") or "").strip()
    if category and category not in CATEGORIES:
        out.append(_finding(
            LEVEL_WARN, "unknown_category",
            f"category「{category}」不在已知分类里，编排器分类导航可能不好找",
            "task.json:category",
        ))
    return out


def lint_config_schema(schema: dict | None) -> list[dict]:
    out: list[dict] = []
    if not schema:
        return out
    fields = schema.get("fields")
    if fields is None:
        return out
    if not isinstance(fields, list):
        out.append(_finding(LEVEL_ERROR, "schema_not_list", "config_schema.fields 必须是数组", "config_schema"))
        return out

    seen: set[str] = set()
    for idx, field in enumerate(fields):
        where = f"config_schema.fields[{idx}]"
        if not isinstance(field, dict):
            out.append(_finding(LEVEL_ERROR, "field_not_object", "字段必须是对象", where))
            continue
        key = str(field.get("key") or "")
        if not FIELD_KEY_RE.match(key):
            out.append(_finding(
                LEVEL_ERROR, "bad_field_key",
                "字段 key 只能是字母或下划线开头的标识符（它会成为 with 的键名）", where,
            ))
        elif key in seen:
            out.append(_finding(LEVEL_ERROR, "dup_field_key", f"字段 key「{key}」重复", where))
        else:
            seen.add(key)

        ftype = str(field.get("type") or "text")
        if ftype not in FIELD_TYPES:
            out.append(_finding(
                LEVEL_ERROR, "bad_field_type",
                f"字段类型「{ftype}」渲染器不认识，可用：{'/'.join(sorted(FIELD_TYPES))}", where,
            ))
        if ftype in ("select", "radio"):
            # node / credential 的选项由编排器运行时拉取，不必写死 options
            source = str(field.get("source") or "")
            if ftype == "select" and source in ("node", "credential"):
                pass
            else:
                options = field.get("options")
                if not isinstance(options, list) or not options:
                    out.append(_finding(
                        LEVEL_ERROR, "no_options", f"{ftype} 字段必须给 options", where,
                    ))
        if ftype == "group" and not isinstance(field.get("children"), list):
            out.append(_finding(LEVEL_ERROR, "no_children", "group 字段必须给 children", where))
        if field.get("required") and field.get("default") in (None, ""):
            out.append(_finding(
                LEVEL_WARN, "required_no_default",
                "必填项没有默认值，用户不填就会执行失败，建议给个合理默认", where,
            ))
    return out


def lint_files(files: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    if not files:
        out.append(_finding(LEVEL_ERROR, "no_files", "插件没有任何源码文件"))
        return out

    total = 0
    for path, content in files.items():
        norm = path.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/") or ":" in norm:
            out.append(_finding(LEVEL_ERROR, "bad_path", f"非法文件路径：{path}", path))
        if norm == "task.json":
            out.append(_finding(
                LEVEL_ERROR, "task_json_in_files",
                "task.json 由 manifest 字段生成，不要放进源码文件里", path,
            ))
        size = len(content.encode("utf-8"))
        total += size
        if size > MAX_FILE_BYTES:
            out.append(_finding(
                LEVEL_ERROR, "file_too_large",
                f"{path} 超过 {MAX_FILE_BYTES // 1024}KB，插件源码不该这么大", path,
            ))
        if LONG_BLOB_RE.search(content):
            out.append(_finding(
                LEVEL_HIGH, "embedded_blob",
                "存在超长 base64 样式字符串，疑似内嵌二进制或混淆载荷", path,
            ))

    if total > MAX_TOTAL_BYTES:
        out.append(_finding(
            LEVEL_ERROR, "package_too_large",
            f"源码总大小超过 {MAX_TOTAL_BYTES // 1024 // 1024}MB", "",
        ))
    return out


def scan_code(files: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for path, content in files.items():
        for level, code, pattern, message in CODE_PATTERNS:
            match = pattern.search(content)
            if not match:
                continue
            line = content.count("\n", 0, match.start()) + 1
            out.append(_finding(level, code, message, f"{path}:{line}"))
    return out


def lint_draft(meta: dict, files: dict[str, str]) -> dict:
    """完整体检。返回结论和明细，调用方据此决定能否落草稿 / 能否发布。"""
    schema = meta.get("config_schema")
    findings: list[dict] = []
    findings += lint_manifest(meta, files)
    findings += lint_config_schema(schema if isinstance(schema, dict) else None)
    findings += lint_files(files)
    findings += scan_code(files)

    errors = [f for f in findings if f["level"] == LEVEL_ERROR]
    highs = [f for f in findings if f["level"] == LEVEL_HIGH]
    return {
        "ok": not errors,
        "error_count": len(errors),
        "high_count": len(highs),
        "warn_count": len(findings) - len(errors) - len(highs),
        "findings": findings,
    }
