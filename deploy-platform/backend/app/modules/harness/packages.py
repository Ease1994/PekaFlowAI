"""统一扩展包 manifest、语义版本约束与依赖 DAG。"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ManifestKind = Literal[
    "pipeline-plugin", "agent-skill", "agent-tool", "model-adapter", "template"
]
MANIFEST_KINDS = {
    "pipeline-plugin", "agent-skill", "agent-tool", "model-adapter", "template"
}
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$"
)


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> SemVer:
        match = _SEMVER_RE.fullmatch(value.strip())
        if not match:
            raise ValueError(f"不是合法语义版本: {value}")
        return cls(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tuple((match.group(4) or "").split(".")) if match.group(4) else (),
        )


class ManifestDependency(BaseModel):
    key: str = Field(description="依赖稳定键 kind:name")
    version: str = "*"
    optional: bool = False

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        kind, separator, name = value.strip().partition(":")
        if not separator or kind not in MANIFEST_KINDS or not _NAME_RE.fullmatch(name):
            raise ValueError("依赖 key 必须为受支持的 kind:name")
        return f"{kind}:{name}"

    @field_validator("version")
    @classmethod
    def validate_range(cls, value: str) -> str:
        value = value.strip() or "*"
        version_satisfies("0.0.0", value)
        return value


class Manifest(BaseModel):
    schema_version: Literal["1"] = "1"
    kind: ManifestKind
    name: str
    version: str
    display_name: str = ""
    description: str = ""
    entrypoint: str = ""
    capabilities: list[str] = Field(default_factory=list)
    dependencies: list[ManifestDependency] = Field(default_factory=list)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip().lower()
        if not _NAME_RE.fullmatch(value):
            raise ValueError("name 只能包含小写字母、数字、点、下划线和连字符")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        SemVer.parse(value)
        return value.strip()

    @model_validator(mode="after")
    def validate_dependencies(self) -> Manifest:
        keys = [item.key for item in self.dependencies]
        if len(keys) != len(set(keys)):
            raise ValueError("dependencies 中存在重复稳定键")
        if self.key in keys:
            raise ValueError("扩展不能依赖自身")
        return self

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"


def _compare(left: SemVer, right: SemVer) -> int:
    left_core, right_core = (
        (left.major, left.minor, left.patch),
        (right.major, right.minor, right.patch),
    )
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if left.prerelease == right.prerelease:
        return 0
    if not left.prerelease:
        return 1
    if not right.prerelease:
        return -1
    return -1 if left.prerelease < right.prerelease else 1


def version_satisfies(version: str, constraint: str) -> bool:
    current = SemVer.parse(version)
    expression = (constraint or "*").strip()
    if expression in {"*", "x", "X"}:
        return True
    return any(
        all(
            _satisfies_token(current, token)
            for token in re.split(r"[\s,]+", branch.strip())
            if token
        )
        for branch in expression.split("||")
    )


def _satisfies_token(current: SemVer, token: str) -> bool:
    if token.startswith("^"):
        lower = SemVer.parse(token[1:])
        upper = (
            SemVer(lower.major + 1, 0, 0)
            if lower.major
            else SemVer(0, lower.minor + 1, 0)
            if lower.minor
            else SemVer(0, 0, lower.patch + 1)
        )
        return _compare(current, lower) >= 0 and _compare(current, upper) < 0
    if token.startswith("~"):
        lower = SemVer.parse(token[1:])
        return _compare(current, lower) >= 0 and _compare(
            current, SemVer(lower.major, lower.minor + 1, 0)
        ) < 0
    if "x" in token.lower() or "*" in token:
        parts = token.replace("*", "x").lower().split(".")
        actual = (current.major, current.minor, current.patch)
        return all(part == "x" or int(part) == actual[index] for index, part in enumerate(parts))
    match = re.fullmatch(r"(>=|<=|>|<|=)?(.+)", token)
    if not match:
        raise ValueError(f"无法识别版本范围: {token}")
    result = _compare(current, SemVer.parse(match.group(2)))
    return {
        "=": result == 0, ">": result > 0, ">=": result >= 0,
        "<": result < 0, "<=": result <= 0,
    }[match.group(1) or "="]


def resolve_dependency_dag(manifests: list[Manifest]) -> list[Manifest]:
    """返回依赖优先的稳定拓扑序，并拒绝缺失、版本不符和循环。"""
    by_key = {item.key: item for item in manifests}
    if len(by_key) != len(manifests):
        raise ValueError("同一依赖图不能包含重复稳定键")
    outgoing: dict[str, set[str]] = defaultdict(set)
    indegree = {key: 0 for key in by_key}
    for item in manifests:
        for dependency in item.dependencies:
            target = by_key.get(dependency.key)
            if target is None:
                if dependency.optional:
                    continue
                raise ValueError(f"{item.key} 缺少依赖 {dependency.key}")
            if not version_satisfies(target.version, dependency.version):
                raise ValueError(
                    f"{item.key} 需要 {dependency.key} {dependency.version}，实际为 {target.version}"
                )
            if item.key not in outgoing[target.key]:
                outgoing[target.key].add(item.key)
                indegree[item.key] += 1
    ready = sorted(key for key, degree in indegree.items() if degree == 0)
    result: list[Manifest] = []
    while ready:
        key = ready.pop(0)
        result.append(by_key[key])
        for dependent in sorted(outgoing[key]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if len(result) != len(manifests):
        raise ValueError("检测到循环依赖: " + " -> ".join(
            sorted(key for key, degree in indegree.items() if degree)
        ))
    return result
