# -*- coding: utf-8 -*-
"""RC 工件拒收校验器（tools/verify_rc_artifact.py）的安全行为测试。

全部使用合成 fixture，不依赖组件仓库实际工件。负例覆盖八类攻击：
1. 路径逃逸（../、绝对、盘符三形态参数化） -> UNSAFE_MEMBER_PATH
2. 提交不匹配 / 短提交 -> GIT_COMMIT_MISMATCH
3. payload 字节篡改 -> FILE_HASH_MISMATCH
4. 删除已声明文件 -> MEMBER_SET_MISMATCH
5. 追加未声明文件 -> MEMBER_SET_MISMATCH
6. 重复 ZIP member -> DUPLICATE_MEMBER
7. 符号链接外部属性 -> SYMLINK_MEMBER
8. 错误组件 / 错误契约版本 -> COMPONENT_MISMATCH / CONTRACT_VERSION_MISMATCH

所有负例都传 --extract-to 等价参数并断言目标外/目标内不产生任何文件。
"""
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.verify_rc_artifact import verify_artifact  # noqa: E402

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
CONTRACT_VERSION = "component-contract-v1.1.0"

# 与 manifest 声明一一对应的合成 payload（路径含中文以覆盖 UTF-8 成员名）。
PAYLOAD_FILES: dict[str, bytes] = {
    "04_数据/battery/data_manifest.json": b'{"files": []}\n',
    "03_代码/components/battery/checkpoints/model_weights.bin": b"BIN\x00PAYLOAD\x00",
}


def _sha256_hex(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


@pytest.fixture()
def valid_manifest() -> str:
    """构造满足 handoff-manifest.schema.json 的合成 manifest JSON 字符串。"""
    files = [
        {
            "path": path,
            "role": "dataset" if path.startswith("04_") else "checkpoint",
            "size": len(blob),
            "sha256": _sha256_hex(blob),
            "redistributable": True,
            "source_url": None,
            "processing_provenance": "synthetic test fixture for verify_rc_artifact",
        }
        for path, blob in sorted(PAYLOAD_FILES.items())
    ]
    manifest = {
        "schema_version": "1.1.0",
        "component": "battery",
        "release_candidate": "battery-v0.1.0-rc.1",
        "git_commit": COMMIT_A,
        "contract_version": CONTRACT_VERSION,
        "created_at": "2026-08-15T00:00:00Z",
        "environment": {
            "python": "3.12.7",
            "pytorch": "2.5.1",
            "os": "Windows-10-10.0.19045-SP0",
            "device": "cpu",
        },
        "random_seeds": [42],
        "commands": ["pytest -q"],
        "files": files,
        "public_summary": "synthetic fixture archive for verify tests",
        "reproduce_status": "REPRODUCE_OK",
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2)


def _sums_text(manifest_str: str) -> str:
    manifest = json.loads(manifest_str)
    return "".join(
        f"{entry['sha256']}  {entry['path']}\n" for entry in manifest["files"]
    )


@pytest.fixture()
def valid_archive(tmp_path, valid_manifest) -> Path:
    archive = tmp_path / "battery-artifacts-v0.1.0-rc.1+abcdef01.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    return archive


def _no_files_anywhere(root: Path) -> bool:
    """断言 root 下（含不存在时）没有任何文件产生。"""
    if not root.exists():
        return True
    return not any(p.is_file() for p in root.rglob("*"))


# ---------------------------------------------------------------------------
# 正例
# ---------------------------------------------------------------------------


def test_valid_archive_passes(valid_archive) -> None:
    report = verify_artifact(valid_archive, expected_component="battery", expected_commit=COMMIT_A)
    assert report.ok, [f"{e.code}: {e.message}" for e in report.errors]
    assert report.errors == ()
    assert report.component == "battery"
    assert report.release_candidate == "battery-v0.1.0-rc.1"
    assert report.git_commit == COMMIT_A
    assert report.contract_version == CONTRACT_VERSION
    assert report.file_count == len(PAYLOAD_FILES)


def test_valid_archive_extracts_to_empty_target(tmp_path, valid_archive) -> None:
    target = tmp_path / "out"
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=target,
    )
    assert report.ok, [f"{e.code}: {e.message}" for e in report.errors]
    assert report.extracted_to == target
    for path, blob in PAYLOAD_FILES.items():
        extracted = target / Path(path)
        assert extracted.is_file()
        assert extracted.read_bytes() == blob
    # manifest 与 sums 同样落盘
    assert (target / "HANDOFF_MANIFEST.json").is_file()
    assert (target / "SHA256SUMS").is_file()


# ---------------------------------------------------------------------------
# 负例 1：路径逃逸（三形态参数化，简报 Step 1 原文用例）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt", "C:/escape.txt"])
def test_rejects_path_escape(tmp_path, valid_manifest, member) -> None:
    archive = tmp_path / "battery-artifacts-v0.1.0-rc.1+abcdef0.zip"
    with zipfile.ZipFile(archive, "w", allowZip64=True) as zf:
        zf.writestr(member, "bad")
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
    report = verify_artifact(archive, expected_component="battery", expected_commit="a" * 40)
    assert not report.ok
    assert any(item.code == "UNSAFE_MEMBER_PATH" for item in report.errors)


@pytest.mark.parametrize("member", ["a/../../escape.txt", "..\\backslash.txt"])
def test_rejects_path_escape_more_forms(tmp_path, valid_manifest, member) -> None:
    archive = tmp_path / "battery-artifacts-v0.1.0-rc.1+abcdef0.zip"
    with zipfile.ZipFile(archive, "w", allowZip64=True) as zf:
        zf.writestr(member, "bad")
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
    report = verify_artifact(archive, expected_component="battery", expected_commit=COMMIT_A)
    assert not report.ok
    assert any(item.code == "UNSAFE_MEMBER_PATH" for item in report.errors)


# ---------------------------------------------------------------------------
# 负例 2：提交不匹配 / 短提交（简报 Step 1 原文用例 + 扩展）
# ---------------------------------------------------------------------------


def test_rejects_commit_mismatch(valid_archive) -> None:
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit="b" * 40,
    )
    assert any(item.code == "GIT_COMMIT_MISMATCH" for item in report.errors)


def test_rejects_short_commit(valid_archive) -> None:
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit="abcdef0",
    )
    assert not report.ok
    assert any(item.code == "GIT_COMMIT_MISMATCH" for item in report.errors)


# ---------------------------------------------------------------------------
# 负例 3：payload 字节篡改
# ---------------------------------------------------------------------------


def test_rejects_payload_byte_tampering(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    victim = sorted(PAYLOAD_FILES)[0]
    tampered = bytearray(PAYLOAD_FILES[victim])
    tampered[0] ^= 0x01  # 原位翻转一个字节（大小不变，仅哈希变化）
    with zipfile.ZipFile(tmp_path / "tampered.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            if path == victim:
                zf.writestr(path, bytes(tampered))
            else:
                zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "tampered.zip",
        expected_component="battery",
        expected_commit=manifest["git_commit"],
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "FILE_HASH_MISMATCH" for item in report.errors)
    assert report.extracted_to is None
    assert _no_files_anywhere(tmp_path / "out")


# ---------------------------------------------------------------------------
# 负例 4/5：删除已声明文件 / 追加未声明文件
# ---------------------------------------------------------------------------


def test_rejects_missing_declared_file(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    victim = sorted(PAYLOAD_FILES)[0]
    with zipfile.ZipFile(tmp_path / "missing.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            if path != victim:  # 删除一个已声明文件
                zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "missing.zip",
        expected_component="battery",
        expected_commit=manifest["git_commit"],
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "MEMBER_SET_MISMATCH" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


def test_rejects_undeclared_extra_file(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    with zipfile.ZipFile(tmp_path / "extra.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("04_数据/battery/extra.txt", "undeclared")  # 未声明文件
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "extra.zip",
        expected_component="battery",
        expected_commit=manifest["git_commit"],
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "MEMBER_SET_MISMATCH" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


# ---------------------------------------------------------------------------
# 负例 6：重复 ZIP member
# ---------------------------------------------------------------------------


def test_rejects_duplicate_member(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    victim = sorted(PAYLOAD_FILES)[0]
    with zipfile.ZipFile(tmp_path / "dup.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr(victim, PAYLOAD_FILES[victim])  # 同名 member 写两次
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "dup.zip",
        expected_component="battery",
        expected_commit=manifest["git_commit"],
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "DUPLICATE_MEMBER" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


# ---------------------------------------------------------------------------
# 负例 7：符号链接外部属性
# ---------------------------------------------------------------------------


def test_rejects_symlink_member(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    link_info = zipfile.ZipInfo("04_数据/battery/link-to-data")
    link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(tmp_path / "symlink.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr(link_info, "data_manifest.json")  # symlink 目标为仓库外可被利用
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "symlink.zip",
        expected_component="battery",
        expected_commit=manifest["git_commit"],
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "SYMLINK_MEMBER" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


# ---------------------------------------------------------------------------
# 负例 8：错误组件 / 错误契约版本
# ---------------------------------------------------------------------------


def test_rejects_wrong_component(valid_archive) -> None:
    report = verify_artifact(
        valid_archive,
        expected_component="wheel",
        expected_commit=COMMIT_A,
    )
    assert not report.ok
    assert any(item.code == "COMPONENT_MISMATCH" for item in report.errors)


def test_rejects_wrong_contract_version(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    manifest["contract_version"] = "component-contract-v1.0.0"
    bad_manifest = json.dumps(manifest, ensure_ascii=False, indent=2)
    with zipfile.ZipFile(tmp_path / "contract.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", bad_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "contract.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "CONTRACT_VERSION_MISMATCH" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


# ---------------------------------------------------------------------------
# Manifest / SHA256SUMS / sidecar 完整性
# ---------------------------------------------------------------------------


def test_rejects_missing_manifest(tmp_path) -> None:
    with zipfile.ZipFile(tmp_path / "nomanifest.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("SHA256SUMS", "")
    report = verify_artifact(
        tmp_path / "nomanifest.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "MANIFEST_INVALID" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


def test_rejects_manifest_schema_violation(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    del manifest["random_seeds"]  # schema 必填字段缺失
    bad_manifest = json.dumps(manifest, ensure_ascii=False, indent=2)
    with zipfile.ZipFile(tmp_path / "badschema.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", bad_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "badschema.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "MANIFEST_INVALID" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


def test_rejects_manifest_not_json(tmp_path) -> None:
    with zipfile.ZipFile(tmp_path / "notjson.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("HANDOFF_MANIFEST.json", "{not json")
    report = verify_artifact(
        tmp_path / "notjson.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
    )
    assert not report.ok
    assert any(item.code == "MANIFEST_INVALID" for item in report.errors)


def test_rejects_corrupted_sha256sums(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    sums = _sums_text(valid_manifest)
    # 篡改一行哈希（仍保持行格式合法）
    tampered_sums = sums.replace(manifest["files"][0]["sha256"], "0" * 64, 1)
    with zipfile.ZipFile(tmp_path / "badsums.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", tampered_sums)
    report = verify_artifact(
        tmp_path / "badsums.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "CHECKSUM_FILE_INVALID" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


def test_rejects_missing_sha256sums_member(tmp_path, valid_manifest) -> None:
    with zipfile.ZipFile(tmp_path / "nosums.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
    report = verify_artifact(
        tmp_path / "nosums.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
    )
    assert not report.ok
    assert any(item.code == "CHECKSUM_FILE_INVALID" for item in report.errors)


def test_rejects_sidecar_pointing_to_other_zip(tmp_path, valid_archive) -> None:
    # sidecar 命名指向另一个 ZIP：必须被拒绝
    sidecar = valid_archive.with_name(valid_archive.name + ".sha256")
    sidecar.write_text(f"{'0' * 64}  other-archive.zip\n", encoding="utf-8")
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "SIDECAR_MISMATCH" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")


def test_rejects_sidecar_with_wrong_hash(tmp_path, valid_archive) -> None:
    sidecar = valid_archive.with_name(valid_archive.name + ".sha256")
    sidecar.write_text(f"{'0' * 64}  {valid_archive.name}\n", encoding="utf-8")
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit=COMMIT_A,
    )
    assert not report.ok
    assert any(item.code == "SIDECAR_MISMATCH" for item in report.errors)


def test_accepts_correct_sidecar(tmp_path, valid_archive) -> None:
    digest = _sha256_hex(valid_archive.read_bytes())
    sidecar = valid_archive.with_name(valid_archive.name + ".sha256")
    sidecar.write_text(f"{digest}  {valid_archive.name}\n", encoding="utf-8")
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit=COMMIT_A,
    )
    assert report.ok, [f"{e.code}: {e.message}" for e in report.errors]


def test_rejects_size_mismatch(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    victim = sorted(PAYLOAD_FILES)[0]
    manifest["files"] = [
        {**entry, "size": entry["size"] + 5} if entry["path"] == victim else entry
        for entry in manifest["files"]
    ]
    bad_manifest = json.dumps(manifest, ensure_ascii=False, indent=2)
    with zipfile.ZipFile(tmp_path / "badsize.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", bad_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "badsize.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
    )
    assert not report.ok
    assert any(item.code == "FILE_SIZE_MISMATCH" for item in report.errors)


# ---------------------------------------------------------------------------
# 解压前置条件
# ---------------------------------------------------------------------------


def test_extract_refuses_non_empty_target(tmp_path, valid_archive) -> None:
    target = tmp_path / "out"
    target.mkdir()
    (target / "existing.txt").write_text("occupied", encoding="utf-8")
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=target,
    )
    assert not report.ok
    assert any(item.code == "EXTRACT_TARGET_NOT_EMPTY" for item in report.errors)
    assert (target / "existing.txt").read_text(encoding="utf-8") == "occupied"
    assert list(target.iterdir()) == [target / "existing.txt"]


def test_extract_refuses_file_target(tmp_path, valid_archive) -> None:
    target = tmp_path / "out"
    target.write_text("i am a file", encoding="utf-8")
    report = verify_artifact(
        valid_archive,
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=target,
    )
    assert not report.ok
    assert any(item.code == "EXTRACT_TARGET_NOT_EMPTY" for item in report.errors)


@pytest.mark.parametrize(
    "tamper",
    ["payload_tamper", "extra_file", "symlink", "escape"],
)
def test_no_extraction_on_any_failure(tmp_path, valid_manifest, tamper) -> None:
    archive = tmp_path / f"{tamper}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob + (b"X" if tamper == "payload_tamper" else b""))
        if tamper == "extra_file":
            zf.writestr("04_数据/battery/extra.txt", "undeclared")
        if tamper == "symlink":
            link_info = zipfile.ZipInfo("04_数据/battery/link")
            link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link_info, "data_manifest.json")
        if tamper == "escape":
            zf.writestr("../escape.txt", "bad")
        zf.writestr("HANDOFF_MANIFEST.json", valid_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    out = tmp_path / "out"
    report = verify_artifact(
        archive,
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=out,
    )
    assert not report.ok
    assert report.extracted_to is None
    assert _no_files_anywhere(out)
    # 逃逸负例额外确认：目标目录之外（tmp 根）也没有新增逃逸文件
    if tamper == "escape":
        assert not (tmp_path / "escape.txt").exists()
        assert not (tmp_path.parent / "escape.txt").exists()


def test_manifest_with_unsafe_declared_path_rejected(tmp_path, valid_manifest) -> None:
    manifest = json.loads(valid_manifest)
    manifest["files"].append(
        {
            "path": "../declared-escape.txt",
            "role": "dataset",
            "size": 3,
            "sha256": "0" * 64,
            "redistributable": True,
            "source_url": None,
            "processing_provenance": "synthetic",
        }
    )
    bad_manifest = json.dumps(manifest, ensure_ascii=False, indent=2)
    with zipfile.ZipFile(tmp_path / "declared-escape.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, blob in sorted(PAYLOAD_FILES.items()):
            zf.writestr(path, blob)
        zf.writestr("HANDOFF_MANIFEST.json", bad_manifest)
        zf.writestr("SHA256SUMS", _sums_text(valid_manifest))
    report = verify_artifact(
        tmp_path / "declared-escape.zip",
        expected_component="battery",
        expected_commit=COMMIT_A,
        extract_to=tmp_path / "out",
    )
    assert not report.ok
    assert any(item.code == "UNSAFE_MEMBER_PATH" for item in report.errors)
    assert _no_files_anywhere(tmp_path / "out")
