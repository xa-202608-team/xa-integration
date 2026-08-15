# -*- coding: utf-8 -*-
"""组件版本锁：load_lock / validate_lock。

``schemas/component-lock.schema.json``（2020-12）的忠实镜像校验：
- 顶层键恰为 schema_version / generated_at / contract / components；
- contract 恰含 repository / tag / commit；
- components 恰含 battery / phased_array / wheel，各恰含
  repository / tag / commit / artifact / artifact_sha256；
- 提交一律 40 位小写十六进制、工件哈希一律 64 位小写十六进制；
- 任何多余键（additionalProperties=false）或缺失键都报告为错误。

真实 ``component-lock.yaml`` 只能在 RC 交接时用已验证的 Tag/Commit/工件哈希
生成（Task 15），被 .gitignore 排除、不入版本库。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    from .artifact_common import is_full_commit, is_sha256_hex
except ImportError:  # pragma: no cover - 以 `python tools/lockfile.py` 运行
    from artifact_common import is_full_commit, is_sha256_hex  # type: ignore

#: component-lock.schema.json 的 schema_version const
LOCK_SCHEMA_VERSION = "1.0.0"

#: 锁定的组件枚举（与 artifact-map / manifest 的下划线枚举一致）
LOCK_COMPONENTS: tuple[str, ...] = ("battery", "phased_array", "wheel")

_TOP_KEYS = frozenset({"schema_version", "generated_at", "contract", "components"})
_CONTRACT_KEYS = frozenset({"repository", "tag", "commit"})
_ENTRY_KEYS = frozenset(
    {"repository", "tag", "commit", "artifact", "artifact_sha256"}
)


@dataclass(frozen=True)
class ContractEntry:
    """契约仓库版本锁定。"""

    repository: str
    tag: str
    commit: str


@dataclass(frozen=True)
class ComponentEntry:
    """单个组件仓库 + RC 工件的版本锁定。"""

    repository: str
    tag: str
    commit: str
    artifact: str
    artifact_sha256: str


@dataclass(frozen=True)
class ComponentLock:
    """装配输入的完整版本锁。"""

    schema_version: str
    generated_at: str
    contract: ContractEntry
    components: dict[str, ComponentEntry]


def _check_field_set(
    where: str, value: object, expected: frozenset[str], errors: list[str]
) -> bool:
    """对象键集合 == expected（additionalProperties=false + 全必填）。"""
    if not isinstance(value, dict):
        errors.append(f"{where} must be an object")
        return False
    extra = sorted(set(value) - expected)
    if extra:
        errors.append(f"{where} has unexpected keys: {extra}")
    missing = sorted(expected - set(value))
    if missing:
        errors.append(f"{where} is missing keys: {missing}")
    return not extra and not missing


def _check_nonempty_str(where: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{where} must be a non-empty string")


def _check_commit(where: str, value: object, errors: list[str]) -> None:
    if not is_full_commit(value):  # type: ignore[arg-type]
        errors.append(f"{where} must be 40 lowercase hex chars (full commit)")


def _check_entry_fields(where: str, entry: dict, errors: list[str]) -> None:
    for key in ("repository", "tag"):
        _check_nonempty_str(f"{where}.{key}", entry.get(key), errors)
    _check_commit(f"{where}.commit", entry.get("commit"), errors)


def validate_lock(data: object) -> list[str]:
    """按 component-lock.schema.json 镜像校验锁字典，返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["lock must be a mapping object"]

    if not _check_field_set("lock", data, _TOP_KEYS, errors):
        # 键集合不对时其余字段校验意义有限，但仍尽量报告
        pass

    if "schema_version" in data and data.get("schema_version") != LOCK_SCHEMA_VERSION:
        errors.append(f"lock.schema_version must be '{LOCK_SCHEMA_VERSION}'")

    if "generated_at" in data:
        _check_nonempty_str("lock.generated_at", data.get("generated_at"), errors)

    contract = data.get("contract")
    if "contract" in data:
        if _check_field_set("lock.contract", contract, _CONTRACT_KEYS, errors) and isinstance(
            contract, dict
        ):
            _check_entry_fields("lock.contract", contract, errors)

    components = data.get("components")
    if "components" in data:
        if not isinstance(components, dict):
            errors.append("lock.components must be an object")
        else:
            extra = sorted(set(components) - set(LOCK_COMPONENTS))
            if extra:
                errors.append(
                    f"lock.components has unexpected components: {extra} "
                    f"(expected exactly {list(LOCK_COMPONENTS)})"
                )
            missing = sorted(set(LOCK_COMPONENTS) - set(components))
            if missing:
                errors.append(f"lock.components is missing components: {missing}")
            for component in LOCK_COMPONENTS:
                if component not in components:
                    continue
                entry = components[component]
                where = f"lock.components.{component}"
                if _check_field_set(where, entry, _ENTRY_KEYS, errors) and isinstance(
                    entry, dict
                ):
                    _check_entry_fields(where, entry, errors)
                    artifact = entry.get("artifact")
                    _check_nonempty_str(f"{where}.artifact", artifact, errors)
                    if "artifact_sha256" in entry and not is_sha256_hex(
                        entry.get("artifact_sha256")  # type: ignore[arg-type]
                    ):
                        errors.append(
                            f"{where}.artifact_sha256 must be 64 lowercase hex chars"
                        )
    return errors


def lock_from_dict(data: object) -> ComponentLock:
    """已通过 validate_lock 的字典 -> ComponentLock（不再重复校验）。"""
    if not isinstance(data, dict):
        raise ValueError("lock must be a mapping object")
    contract = data["contract"]
    components = {
        name: ComponentEntry(
            repository=entry["repository"],
            tag=entry["tag"],
            commit=entry["commit"],
            artifact=entry["artifact"],
            artifact_sha256=entry["artifact_sha256"],
        )
        for name, entry in data["components"].items()
    }
    return ComponentLock(
        schema_version=data["schema_version"],
        generated_at=data["generated_at"],
        contract=ContractEntry(
            repository=contract["repository"],
            tag=contract["tag"],
            commit=contract["commit"],
        ),
        components=components,
    )


def load_lock(path: Path) -> ComponentLock:
    """读取并校验 component-lock.yaml，返回 ComponentLock；任何错误 ValueError。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"lock file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"lock file is not parsable YAML: {exc}") from exc
    errors = validate_lock(data)
    if errors:
        raise ValueError(
            "component lock violates component-lock.schema.json:\n  - "
            + "\n  - ".join(errors)
        )
    return lock_from_dict(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate a component-lock.yaml against component-lock.schema.json."
    )
    parser.add_argument("--lock", required=True, type=Path, help="component-lock.yaml 路径")
    args = parser.parse_args(argv)

    try:
        lock = load_lock(args.lock)
    except (FileNotFoundError, ValueError) as exc:
        print(f"lock INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"lock OK: schema_version={lock.schema_version} components={sorted(lock.components)}")
    print(f"  contract: {lock.contract.repository} @ {lock.contract.tag} ({lock.contract.commit[:8]})")
    for name in LOCK_COMPONENTS:
        entry = lock.components[name]
        print(
            f"  {name}: {entry.repository} @ {entry.tag} "
            f"({entry.commit[:8]}) artifact={entry.artifact}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
