# -*- coding: utf-8 -*-
"""RC 工件生成与校验共享的路径归一化、常量与哈希工具。

安全约束（与任务简报 Step 3 对齐）：
- ``normalize_member`` 是 ZIP 成员 / payload 路径的唯一归一化入口，
  生成（build_rc_artifact）与校验（verify_rc_artifact）必须调用同一函数；
- 成员名统一 POSIX ``/`` 分隔：拒绝反斜杠、绝对路径、空路径、``..`` 段、
  盘符段（``C:``），四类输入一律 ValueError；
- 组件标识（manifest 用下划线枚举）与文件 slug（tag/ZIP 文件名用连字符）
  的映射固定，不得由调用者注入。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

#: ZIP 内交接清单成员名（固定）
MANIFEST_MEMBER_NAME = "HANDOFF_MANIFEST.json"
#: ZIP 内逐文件校验和成员名（固定）
CHECKSUMS_MEMBER_NAME = "SHA256SUMS"

#: manifest `component` 枚举（下划线） -> 文件名/tag slug（连字符）
COMPONENT_SLUGS: dict[str, str] = {
    "battery": "battery",
    "phased_array": "phased-array",
    "wheel": "wheel",
}

#: 当前支持的契约版本（handoff-manifest.schema.json 的 const）
SUPPORTED_CONTRACT_VERSION = "component-contract-v1.1.0"

#: handoff-manifest.schema.json 的 schema_version const
MANIFEST_SCHEMA_VERSION = "1.1.0"

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

_HASH_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# 路径归一化（简报 Step 3 逐字）
# ---------------------------------------------------------------------------


def normalize_member(name: str) -> PurePosixPath:
    if "\\" in name:
        raise ValueError("backslash is forbidden in archive paths")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe archive member: {name}")
    if ":" in path.parts[0]:
        raise ValueError(f"drive path is forbidden: {name}")
    return path


# ---------------------------------------------------------------------------
# 校验谓词
# ---------------------------------------------------------------------------


def is_full_commit(value: str) -> bool:
    """40 位小写十六进制完整提交哈希。"""
    return isinstance(value, str) and bool(_COMMIT_RE.match(value))


def is_sha256_hex(value: str) -> bool:
    """64 位小写十六进制 SHA256。"""
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def is_semver(value: str) -> bool:
    """X.Y.Z 三段数字版本号。"""
    return isinstance(value, str) and bool(_SEMVER_RE.match(value))


def component_slug(component: str) -> str:
    """下划线组件枚举 -> 连字符 slug；未知组件 ValueError。"""
    try:
        return COMPONENT_SLUGS[component]
    except KeyError:
        raise ValueError(
            f"unknown component: {component!r} (expected one of {sorted(COMPONENT_SLUGS)})"
        ) from None


# ---------------------------------------------------------------------------
# 命名与哈希
# ---------------------------------------------------------------------------


def artifact_basename(component: str, version: str, rc: int, shortsha: str) -> str:
    """RC ZIP 文件名：``{slug}-artifacts-vX.Y.Z-rc.N+{shortsha}.zip``。

    shortsha 必须由 HEAD 自动形成（调用方不得手填完整哈希以外的值）。
    """
    return f"{component_slug(component)}-artifacts-v{version}-rc.{rc}+{shortsha}.zip"


def release_candidate_name(component: str, version: str, rc: int) -> str:
    """manifest `release_candidate`：连字符 slug + vX.Y.Z-rc.N（schema pattern）。"""
    return f"{component_slug(component)}-v{version}-rc.{rc}"


def sha256_stream(fh: BinaryIO, max_bytes: int | None = None) -> tuple[str, int]:
    """流式计算 SHA256 与字节数；``max_bytes`` 限制读取量防解压炸弹。"""
    hasher = hashlib.sha256()
    total = 0
    while True:
        chunk = fh.read(_HASH_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ValueError(f"content exceeds expected size limit ({max_bytes} bytes)")
        hasher.update(chunk)
    return hasher.hexdigest(), total


def sha256_file(path: Path) -> tuple[str, int]:
    """流式计算磁盘文件 SHA256 与字节数。"""
    with Path(path).open("rb") as fh:
        return sha256_stream(fh)


def format_sha256_line(digest: str, name: str) -> str:
    """``.sha256`` / SHA256SUMS 行格式：64 位小写哈希 + 两个空格 + 名称 + 换行。"""
    if not is_sha256_hex(digest):
        raise ValueError(f"not a lowercase sha256 hex digest: {digest!r}")
    return f"{digest}  {name}\n"


def parse_sha256_line(line: str) -> tuple[str, str] | None:
    """解析一行 ``hash  name``；格式非法返回 None（不抛异常）。"""
    if len(line) < 68 or line[64:66] != "  ":
        return None
    digest, name = line[:64], line[66:]
    if not is_sha256_hex(digest) or not name or "\n" in name:
        return None
    return digest, name


def iter_sorted_payloads(pairs: Iterable[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """按 payload 成员路径排序的 (member, source) 列表。"""
    return sorted(pairs, key=lambda item: item[0])


# ---------------------------------------------------------------------------
# HANDOFF_MANIFEST.json 结构校验（handoff-manifest.schema.json 的忠实镜像）
# ---------------------------------------------------------------------------

#: 结构校验默认错误码；个别字段有专属错误码（路径逃逸/契约版本）
CODE_MANIFEST_INVALID = "MANIFEST_INVALID"
CODE_UNSAFE_MEMBER_PATH = "UNSAFE_MEMBER_PATH"
CODE_CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"

_MANIFEST_TOP_KEYS = frozenset(
    {
        "schema_version",
        "component",
        "release_candidate",
        "git_commit",
        "contract_version",
        "created_at",
        "environment",
        "random_seeds",
        "commands",
        "files",
        "public_summary",
        "reproduce_status",
    }
)
_MANIFEST_ENV_KEYS = frozenset({"python", "pytorch", "os", "device"})
_MANIFEST_FILE_KEYS = frozenset(
    {
        "path",
        "role",
        "size",
        "sha256",
        "redistributable",
        "source_url",
        "processing_provenance",
    }
)
_RC_NAME_RE = re.compile(r"^(battery|phased-array|wheel)-v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+$")


def validate_manifest_structure(manifest: object) -> list[tuple[str, str]]:
    """按契约 schema 逐项校验 manifest，返回 (错误码, 说明) 列表（空 = 通过）。

    镜像 ``handoff-manifest.schema.json``（2020-12）：必填键、const/enum/pattern、
    additionalProperties=false、数组长下限。``contract_version`` 的 const 违反
    单独记 ``CONTRACT_VERSION_MISMATCH``；``files[].path`` 的逃逸路径单独记
    ``UNSAFE_MEMBER_PATH``。
    """
    issues: list[tuple[str, str]] = []
    if not isinstance(manifest, dict):
        return [(CODE_MANIFEST_INVALID, "manifest must be a JSON object")]

    extra = sorted(set(manifest) - _MANIFEST_TOP_KEYS)
    if extra:
        issues.append((CODE_MANIFEST_INVALID, f"unexpected manifest keys: {extra}"))
    missing = sorted(_MANIFEST_TOP_KEYS - set(manifest))
    if missing:
        issues.append((CODE_MANIFEST_INVALID, f"missing manifest keys: {missing}"))

    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        issues.append((CODE_MANIFEST_INVALID, "schema_version must be '1.1.0'"))

    component = manifest.get("component")
    if component not in COMPONENT_SLUGS:
        issues.append(
            (CODE_MANIFEST_INVALID, f"component must be one of {sorted(COMPONENT_SLUGS)}")
        )

    rc_name = manifest.get("release_candidate")
    if not isinstance(rc_name, str) or not _RC_NAME_RE.match(rc_name):
        issues.append(
            (CODE_MANIFEST_INVALID, "release_candidate must match '<slug>-vX.Y.Z-rc.N'")
        )
    elif isinstance(component, str) and component in COMPONENT_SLUGS:
        expected_prefix = COMPONENT_SLUGS[component] + "-"
        if not rc_name.startswith(expected_prefix):
            issues.append(
                (CODE_MANIFEST_INVALID, "release_candidate slug does not match component")
            )

    if not is_full_commit(manifest.get("git_commit")):  # type: ignore[arg-type]
        issues.append((CODE_MANIFEST_INVALID, "git_commit must be 40 lowercase hex chars"))

    contract = manifest.get("contract_version")
    if not isinstance(contract, str) or not contract:
        issues.append((CODE_MANIFEST_INVALID, "contract_version must be a non-empty string"))
    elif contract != SUPPORTED_CONTRACT_VERSION:
        issues.append(
            (CODE_CONTRACT_VERSION_MISMATCH,
             f"unsupported contract_version: {contract!r} (expected {SUPPORTED_CONTRACT_VERSION!r})")
        )

    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        issues.append((CODE_MANIFEST_INVALID, "created_at must be a non-empty date-time string"))

    environment = manifest.get("environment")
    if not isinstance(environment, dict):
        issues.append((CODE_MANIFEST_INVALID, "environment must be an object"))
    else:
        env_extra = sorted(set(environment) - _MANIFEST_ENV_KEYS)
        if env_extra:
            issues.append((CODE_MANIFEST_INVALID, f"unexpected environment keys: {env_extra}"))
        for key in sorted(_MANIFEST_ENV_KEYS - set(environment)):
            issues.append((CODE_MANIFEST_INVALID, f"environment missing key: {key}"))
        for key, value in environment.items():
            if not isinstance(value, str) or not value:
                issues.append((CODE_MANIFEST_INVALID, f"environment.{key} must be non-empty"))

    seeds = manifest.get("random_seeds")
    if (
        not isinstance(seeds, list)
        or not seeds
        or not all(isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in seeds)
    ):
        issues.append((CODE_MANIFEST_INVALID, "random_seeds must be a non-empty int array"))

    commands = manifest.get("commands")
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(c, str) and c for c in commands)
    ):
        issues.append((CODE_MANIFEST_INVALID, "commands must be a non-empty string array"))

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        issues.append((CODE_MANIFEST_INVALID, "files must be a non-empty array"))
        files = []
    for index, entry in enumerate(files):
        where = f"files[{index}]"
        if not isinstance(entry, dict):
            issues.append((CODE_MANIFEST_INVALID, f"{where} must be an object"))
            continue
        entry_extra = sorted(set(entry) - _MANIFEST_FILE_KEYS)
        if entry_extra:
            issues.append((CODE_MANIFEST_INVALID, f"{where} unexpected keys: {entry_extra}"))
        entry_missing = sorted(_MANIFEST_FILE_KEYS - set(entry))
        if entry_missing:
            issues.append((CODE_MANIFEST_INVALID, f"{where} missing keys: {entry_missing}"))
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            issues.append((CODE_MANIFEST_INVALID, f"{where}.path must be a non-empty string"))
        else:
            try:
                normalize_member(path)
            except ValueError as exc:
                issues.append((CODE_UNSAFE_MEMBER_PATH, f"{where}.path {exc}"))
        role = entry.get("role")
        if not isinstance(role, str) or not role:
            issues.append((CODE_MANIFEST_INVALID, f"{where}.role must be a non-empty string"))
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            issues.append((CODE_MANIFEST_INVALID, f"{where}.size must be an integer >= 0"))
        if not is_sha256_hex(entry.get("sha256")):  # type: ignore[arg-type]
            issues.append((CODE_MANIFEST_INVALID, f"{where}.sha256 must be 64 lowercase hex"))
        if not isinstance(entry.get("redistributable"), bool):
            issues.append((CODE_MANIFEST_INVALID, f"{where}.redistributable must be a boolean"))
        source_url = entry.get("source_url")
        if source_url is not None and (
            not isinstance(source_url, str) or not source_url.startswith("https://")
        ):
            issues.append(
                (CODE_MANIFEST_INVALID, f"{where}.source_url must be an https URL or null")
            )
        provenance = entry.get("processing_provenance")
        if not isinstance(provenance, str) or not provenance:
            issues.append(
                (CODE_MANIFEST_INVALID, f"{where}.processing_provenance must be non-empty")
            )

    summary = manifest.get("public_summary")
    if not isinstance(summary, str) or not summary:
        issues.append((CODE_MANIFEST_INVALID, "public_summary must be a non-empty string"))

    if manifest.get("reproduce_status") != "REPRODUCE_OK":
        issues.append((CODE_MANIFEST_INVALID, "reproduce_status must be 'REPRODUCE_OK'"))

    return issues
