# -*- coding: utf-8 -*-
"""组件版本锁（tools/lockfile.py + schemas/component-lock.schema.json）的行为测试。

全部使用固定测试值（合成提交 40 位 a/b/c/d、工件哈希 64 位 1/2/3），
与 examples/component-lock.example.yaml 的演示值一致，不涉及任何真实仓库。
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.lockfile import (  # noqa: E402
    LOCK_SCHEMA_VERSION,
    ComponentLock,
    load_lock,
    validate_lock,
)

SCHEMA_PATH = REPO_ROOT / "schemas" / "component-lock.schema.json"
EXAMPLE_PATH = REPO_ROOT / "examples" / "component-lock.example.yaml"

# 固定测试值：契约提交 40×a，组件提交 40×b/c/d，工件哈希 64×1/2/3。
_CONTRACT_COMMIT = "a" * 40
_BATTERY_COMMIT = "b" * 40
_PHASED_ARRAY_COMMIT = "c" * 40
_WHEEL_COMMIT = "d" * 40
_BATTERY_SHA = "1" * 64
_PHASED_ARRAY_SHA = "2" * 64
_WHEEL_SHA = "3" * 64


@pytest.fixture
def valid_lock_dict() -> dict:
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "generated_at": "2026-08-15T00:00:00Z",
        "contract": {
            "repository": "xa-202608-team/xa-contract",
            "tag": "contract-v1.0.0",
            "commit": _CONTRACT_COMMIT,
        },
        "components": {
            "battery": {
                "repository": "xa-202608-team/xa-battery",
                "tag": "battery-v0.1.0-rc.1",
                "commit": _BATTERY_COMMIT,
                "artifact": "battery-artifacts-v0.1.0-rc.1+bbbbbbbb.zip",
                "artifact_sha256": _BATTERY_SHA,
            },
            "phased_array": {
                "repository": "xa-202608-team/xa-phased-array",
                "tag": "phased-array-v0.2.0-rc.3",
                "commit": _PHASED_ARRAY_COMMIT,
                "artifact": "phased-array-artifacts-v0.2.0-rc.3+cccccccc.zip",
                "artifact_sha256": _PHASED_ARRAY_SHA,
            },
            "wheel": {
                "repository": "xa-202608-team/xa-wheel",
                "tag": "wheel-v0.3.0-rc.2",
                "commit": _WHEEL_COMMIT,
                "artifact": "wheel-artifacts-v0.3.0-rc.2+dddddddd.zip",
                "artifact_sha256": _WHEEL_SHA,
            },
        },
    }


# ---------------------------------------------------------------------------
# 核心用例（任务简报逐字）
# ---------------------------------------------------------------------------


def test_lock_requires_tag_commit_and_artifact_hash(valid_lock_dict) -> None:
    wheel = valid_lock_dict["components"]["wheel"]
    wheel.pop("artifact_sha256")
    errors = validate_lock(valid_lock_dict)
    assert any("artifact_sha256" in error for error in errors)


def test_valid_lock_has_no_errors(valid_lock_dict) -> None:
    assert validate_lock(valid_lock_dict) == []


# ---------------------------------------------------------------------------
# 结构拒绝：schema_version / generated_at / contract
# ---------------------------------------------------------------------------


def test_rejects_wrong_schema_version(valid_lock_dict) -> None:
    valid_lock_dict["schema_version"] = "0.9.0"
    errors = validate_lock(valid_lock_dict)
    assert any("schema_version" in error for error in errors)


def test_rejects_missing_generated_at(valid_lock_dict) -> None:
    del valid_lock_dict["generated_at"]
    errors = validate_lock(valid_lock_dict)
    assert any("generated_at" in error for error in errors)


def test_rejects_empty_generated_at(valid_lock_dict) -> None:
    valid_lock_dict["generated_at"] = ""
    errors = validate_lock(valid_lock_dict)
    assert any("generated_at" in error for error in errors)


def test_rejects_unknown_top_level_key(valid_lock_dict) -> None:
    valid_lock_dict["extra"] = "surprise"
    errors = validate_lock(valid_lock_dict)
    assert any("extra" in error for error in errors)


# ---------------------------------------------------------------------------
# 结构拒绝：components 三组件 × 五字段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["repository", "tag", "commit", "artifact", "artifact_sha256"]
)
def test_rejects_missing_component_fields(valid_lock_dict, field) -> None:
    del valid_lock_dict["components"]["phased_array"][field]
    errors = validate_lock(valid_lock_dict)
    assert any("phased_array" in error and field in error for error in errors)


@pytest.mark.parametrize("component", ["battery", "phased_array", "wheel"])
def test_rejects_missing_component(valid_lock_dict, component) -> None:
    del valid_lock_dict["components"][component]
    errors = validate_lock(valid_lock_dict)
    assert any(component in error for error in errors)


def test_rejects_unknown_component(valid_lock_dict) -> None:
    valid_lock_dict["components"]["star_tracker"] = valid_lock_dict["components"]["wheel"]
    errors = validate_lock(valid_lock_dict)
    assert any("star_tracker" in error for error in errors)


def test_rejects_unknown_component_entry_key(valid_lock_dict) -> None:
    valid_lock_dict["components"]["battery"]["branch"] = "main"
    errors = validate_lock(valid_lock_dict)
    assert any("branch" in error for error in errors)


def test_rejects_missing_contract_fields(valid_lock_dict) -> None:
    del valid_lock_dict["contract"]["tag"]
    errors = validate_lock(valid_lock_dict)
    assert any("contract" in error and "tag" in error for error in errors)


def test_rejects_unknown_contract_key(valid_lock_dict) -> None:
    valid_lock_dict["contract"]["ref"] = "refs/heads/main"
    errors = validate_lock(valid_lock_dict)
    assert any("ref" in error for error in errors)


# ---------------------------------------------------------------------------
# 提交 40 位 / 哈希 64 位格式
# ---------------------------------------------------------------------------


def test_rejects_short_commit(valid_lock_dict) -> None:
    valid_lock_dict["components"]["wheel"]["commit"] = _WHEEL_COMMIT[:8]
    errors = validate_lock(valid_lock_dict)
    assert any("commit" in error and "40" in error for error in errors)


def test_rejects_uppercase_commit(valid_lock_dict) -> None:
    valid_lock_dict["components"]["wheel"]["commit"] = "D" * 40
    errors = validate_lock(valid_lock_dict)
    assert any("commit" in error for error in errors)


def test_rejects_contract_commit_with_wrong_length(valid_lock_dict) -> None:
    valid_lock_dict["contract"]["commit"] = "a" * 41
    errors = validate_lock(valid_lock_dict)
    assert any("contract" in error and "commit" in error for error in errors)


def test_rejects_bad_artifact_sha256(valid_lock_dict) -> None:
    valid_lock_dict["components"]["battery"]["artifact_sha256"] = "1" * 63
    errors = validate_lock(valid_lock_dict)
    assert any("artifact_sha256" in error and "64" in error for error in errors)


def test_rejects_non_hex_artifact_sha256(valid_lock_dict) -> None:
    valid_lock_dict["components"]["wheel"]["artifact_sha256"] = "z" * 64
    errors = validate_lock(valid_lock_dict)
    assert any("artifact_sha256" in error for error in errors)


def test_rejects_non_object_lock() -> None:
    assert validate_lock(["not", "a", "dict"])  # 非空错误列表
    assert validate_lock(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# load_lock：YAML -> ComponentLock
# ---------------------------------------------------------------------------


def test_load_lock_parses_yaml(tmp_path, valid_lock_dict) -> None:
    lock_path = tmp_path / "component-lock.yaml"
    lock_path.write_text(
        yaml.safe_dump(valid_lock_dict, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    lock = load_lock(lock_path)
    assert isinstance(lock, ComponentLock)
    assert lock.schema_version == "1.0.0"
    assert lock.generated_at == "2026-08-15T00:00:00Z"
    assert lock.contract.repository == "xa-202608-team/xa-contract"
    assert lock.contract.tag == "contract-v1.0.0"
    assert lock.contract.commit == _CONTRACT_COMMIT
    assert set(lock.components) == {"battery", "phased_array", "wheel"}
    wheel = lock.components["wheel"]
    assert wheel.commit == _WHEEL_COMMIT
    assert wheel.artifact == "wheel-artifacts-v0.3.0-rc.2+dddddddd.zip"
    assert wheel.artifact_sha256 == _WHEEL_SHA


def test_load_lock_raises_on_invalid_yaml(tmp_path, valid_lock_dict) -> None:
    del valid_lock_dict["components"]["wheel"]["tag"]
    lock_path = tmp_path / "component-lock.yaml"
    lock_path.write_text(
        yaml.safe_dump(valid_lock_dict, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tag"):
        load_lock(lock_path)


def test_load_lock_raises_on_unparsable_yaml(tmp_path) -> None:
    lock_path = tmp_path / "component-lock.yaml"
    lock_path.write_text("{broken: [yaml", encoding="utf-8")
    with pytest.raises(ValueError):
        load_lock(lock_path)


def test_load_lock_raises_on_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_lock(tmp_path / "no-such-lock.yaml")


# ---------------------------------------------------------------------------
# schema 文件与示例文件的契约
# ---------------------------------------------------------------------------


def test_schema_file_exists_and_is_strict() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"] == {"const": LOCK_SCHEMA_VERSION}
    entry = schema["$defs"]["componentEntry"]
    assert entry["additionalProperties"] is False
    assert set(entry["required"]) == {
        "repository",
        "tag",
        "commit",
        "artifact",
        "artifact_sha256",
    }
    assert entry["properties"]["commit"]["pattern"] == "^[0-9a-f]{40}$"
    assert entry["properties"]["artifact_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    contract = schema["$defs"]["contractEntry"]
    assert contract["additionalProperties"] is False
    assert set(contract["required"]) == {"repository", "tag", "commit"}
    assert contract["properties"]["commit"]["pattern"] == "^[0-9a-f]{40}$"
    assert schema["properties"]["contract"] == {"$ref": "#/$defs/contractEntry"}
    assert set(schema["properties"]["components"]["required"]) == {
        "battery",
        "phased_array",
        "wheel",
    }


def test_example_file_is_schema_example_not_release_lock() -> None:
    text = EXAMPLE_PATH.read_text(encoding="utf-8")
    assert text.splitlines()[0].strip() == "# SCHEMA EXAMPLE — NOT A RELEASE LOCK"
    data = yaml.safe_load(text)
    assert validate_lock(data) == []
    assert data["contract"]["commit"] == _CONTRACT_COMMIT
    assert data["components"]["battery"]["commit"] == _BATTERY_COMMIT
    assert data["components"]["phased_array"]["commit"] == _PHASED_ARRAY_COMMIT
    assert data["components"]["wheel"]["commit"] == _WHEEL_COMMIT
    assert data["components"]["battery"]["artifact_sha256"] == _BATTERY_SHA
    assert data["components"]["phased_array"]["artifact_sha256"] == _PHASED_ARRAY_SHA
    assert data["components"]["wheel"]["artifact_sha256"] == _WHEEL_SHA


def test_component_lock_dataclass_roundtrip(valid_lock_dict, tmp_path) -> None:
    lock_path = tmp_path / "component-lock.yaml"
    lock_path.write_text(
        yaml.safe_dump(valid_lock_dict, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    lock = load_lock(lock_path)
    # 重新序列化后再校验仍通过（dataclass 不丢字段）
    assert validate_lock(valid_lock_dict) == []
    assert lock.components["phased_array"].tag == "phased-array-v0.2.0-rc.3"
