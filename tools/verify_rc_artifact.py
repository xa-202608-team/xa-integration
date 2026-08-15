# -*- coding: utf-8 -*-
"""RC 工件拒收校验器：ZIP 内流式校验，全部通过后才允许安全解压。

安全模型（任务简报 Step 5）：
- 先在 ZIP 内**流式**计算哈希（``zipfile.ZipFile.open`` 分块读取），绝不先解压；
- Schema（handoff-manifest.schema.json 镜像）、成员集合、成员路径、逐文件
  大小/哈希、组件、提交、契约版本**全部通过**才允许 ``--extract-to``；
- 目标目录必须不存在或为空；每个最终 resolved path 必须是目标根的子路径；
- 任何失败都不在目标目录内外产生文件；
- sidecar（``<zip>.sha256``）存在时必须精确匹配本 ZIP 的文件名与整包哈希，
  指向其他 ZIP 的 sidecar 一律拒绝。

错误码枚举：
- ``UNSAFE_MEMBER_PATH``     成员/声明路径逃逸（反斜杠/绝对/..、盘符）
- ``SYMLINK_MEMBER``         成员外部属性为符号链接
- ``DUPLICATE_MEMBER``       重复 ZIP 成员名
- ``MANIFEST_INVALID``       缺失/不可解析/违反 schema 的 HANDOFF_MANIFEST.json
- ``CONTRACT_VERSION_MISMATCH`` manifest 契约版本不受支持
- ``MEMBER_SET_MISMATCH``    删除已声明文件或混入未声明文件
- ``FILE_SIZE_MISMATCH``     实际大小与 manifest 声明不符
- ``FILE_HASH_MISMATCH``     payload 字节篡改（哈希不符）
- ``CHECKSUM_FILE_INVALID``  SHA256SUMS 缺失/格式非法/与 manifest 矛盾
- ``SIDECAR_MISMATCH``       包外 .sha256 指向其他 ZIP 或哈希错误
- ``COMPONENT_MISMATCH``     组件不符（含非法组件枚举）
- ``GIT_COMMIT_MISMATCH``    提交不符（含短提交/非法格式）
- ``EXTRACT_TARGET_NOT_EMPTY`` 解压目标已存在且非空
- ``UNSAFE_EXTRACT_PATH``    解压 resolved path 逃出目标根（纵深防御）
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

try:
    from .artifact_common import (
        CHECKSUMS_MEMBER_NAME,
        COMPONENT_SLUGS,
        MANIFEST_MEMBER_NAME,
        SUPPORTED_CONTRACT_VERSION,
        format_sha256_line,
        is_full_commit,
        normalize_member,
        parse_sha256_line,
        sha256_stream,
        validate_manifest_structure,
    )
except ImportError:  # pragma: no cover - 以 `python tools/verify_rc_artifact.py` 运行
    from artifact_common import (  # type: ignore
        CHECKSUMS_MEMBER_NAME,
        COMPONENT_SLUGS,
        MANIFEST_MEMBER_NAME,
        SUPPORTED_CONTRACT_VERSION,
        format_sha256_line,
        is_full_commit,
        normalize_member,
        parse_sha256_line,
        sha256_stream,
        validate_manifest_structure,
    )

#: manifest / sums 成员的最大读取量（防解压炸弹；合法清单远小于此）
_MAX_META_BYTES = 10 * 1024 * 1024

_CODE_MANIFEST_INVALID = "MANIFEST_INVALID"
_CODE_MEMBER_SET_MISMATCH = "MEMBER_SET_MISMATCH"
_CODE_FILE_SIZE_MISMATCH = "FILE_SIZE_MISMATCH"
_CODE_FILE_HASH_MISMATCH = "FILE_HASH_MISMATCH"
_CODE_CHECKSUM_FILE_INVALID = "CHECKSUM_FILE_INVALID"
_CODE_SIDECAR_MISMATCH = "SIDECAR_MISMATCH"
_CODE_COMPONENT_MISMATCH = "COMPONENT_MISMATCH"
_CODE_GIT_COMMIT_MISMATCH = "GIT_COMMIT_MISMATCH"
_CODE_DUPLICATE_MEMBER = "DUPLICATE_MEMBER"
_CODE_SYMLINK_MEMBER = "SYMLINK_MEMBER"
_CODE_UNSAFE_MEMBER_PATH = "UNSAFE_MEMBER_PATH"
_CODE_EXTRACT_TARGET_NOT_EMPTY = "EXTRACT_TARGET_NOT_EMPTY"
_CODE_UNSAFE_EXTRACT_PATH = "UNSAFE_EXTRACT_PATH"


@dataclass(frozen=True)
class VerificationError:
    """一条拒收理由：错误码 + 涉及成员 + 说明（不含任何文件内容）。"""

    code: str
    member: Optional[str] = None
    message: str = ""


@dataclass(frozen=True)
class VerificationReport:
    """校验结果：``ok`` 为 True 当且仅当 ``errors`` 为空。"""

    archive: Path
    ok: bool
    errors: tuple[VerificationError, ...] = ()
    component: Optional[str] = None
    release_candidate: Optional[str] = None
    git_commit: Optional[str] = None
    contract_version: Optional[str] = None
    file_count: int = 0
    extracted_to: Optional[Path] = None

    def error_codes(self) -> list[str]:
        return [item.code for item in self.errors]


def _read_member_capped(zf: zipfile.ZipFile, name: str, cap: int) -> bytes:
    """读取成员并在超过 cap 时截断（调用方负责校验完整性）。"""
    chunks: list[bytes] = []
    total = 0
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise ValueError(f"member exceeds size cap ({cap} bytes): {name}")
            chunks.append(chunk)
    return b"".join(chunks)


def _check_members(infos: list[zipfile.ZipInfo], errors: list[VerificationError]) -> None:
    """重复成员 / 路径归一化 / symlink 外部属性检查。"""
    seen: set[str] = set()
    for info in infos:
        name = info.filename
        if name in seen:
            errors.append(
                VerificationError(_CODE_DUPLICATE_MEMBER, member=name, message="duplicate ZIP member")
            )
            continue
        seen.add(name)
        try:
            normalize_member(name)
        except ValueError as exc:
            errors.append(VerificationError(_CODE_UNSAFE_MEMBER_PATH, member=name, message=str(exc)))
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            errors.append(
                VerificationError(
                    _CODE_SYMLINK_MEMBER, member=name, message="member is a symbolic link"
                )
            )


def _load_manifest(
    zf: zipfile.ZipFile, names: set[str], errors: list[VerificationError]
) -> Optional[dict]:
    if MANIFEST_MEMBER_NAME not in names:
        errors.append(
            VerificationError(
                _CODE_MANIFEST_INVALID, member=MANIFEST_MEMBER_NAME, message="missing manifest"
            )
        )
        return None
    try:
        raw = _read_member_capped(zf, MANIFEST_MEMBER_NAME, _MAX_META_BYTES)
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(
            VerificationError(
                _CODE_MANIFEST_INVALID, member=MANIFEST_MEMBER_NAME, message=f"unparsable manifest: {exc}"
            )
        )
        return None
    for code, message in validate_manifest_structure(manifest):
        errors.append(VerificationError(code, member=MANIFEST_MEMBER_NAME, message=message))
    return manifest if isinstance(manifest, dict) else None


def _check_identity(
    manifest: Optional[dict],
    expected_component: str,
    expected_commit: str,
    errors: list[VerificationError],
) -> bool:
    """组件 / 提交一致性。返回 manifest 是否可继续做成员级校验。"""
    if expected_component not in COMPONENT_SLUGS:
        errors.append(
            VerificationError(
                _CODE_COMPONENT_MISMATCH,
                message=f"invalid expected component: {expected_component!r}",
            )
        )
    if not is_full_commit(expected_commit):
        errors.append(
            VerificationError(
                _CODE_GIT_COMMIT_MISMATCH,
                message=f"expected commit must be 40 lowercase hex chars, got: {expected_commit!r}",
            )
        )
    if not isinstance(manifest, dict):
        return False
    component = manifest.get("component")
    if isinstance(component, str):
        if component != expected_component:
            errors.append(
                VerificationError(
                    _CODE_COMPONENT_MISMATCH,
                    message=f"manifest component {component!r} != expected {expected_component!r}",
                )
            )
        if component not in COMPONENT_SLUGS:
            return False
    git_commit = manifest.get("git_commit")
    if isinstance(git_commit, str) and is_full_commit(expected_commit) and git_commit != expected_commit:
        errors.append(
            VerificationError(
                _CODE_GIT_COMMIT_MISMATCH,
                message=f"manifest git_commit {git_commit!r} != expected {expected_commit!r}",
            )
        )
    return all(
        isinstance(manifest.get(key), str) and manifest.get(key) for key in ("component", "git_commit")
    )


def _declared_files(manifest: Optional[dict]) -> list[dict]:
    if not isinstance(manifest, dict):
        return []
    files = manifest.get("files")
    if not isinstance(files, list):
        return []
    return [entry for entry in files if isinstance(entry, dict) and isinstance(entry.get("path"), str)]


def _check_member_set(
    declared: list[dict], names: set[str], errors: list[VerificationError]
) -> None:
    declared_paths = [entry["path"] for entry in declared]
    expected = set(declared_paths) | {MANIFEST_MEMBER_NAME, CHECKSUMS_MEMBER_NAME}
    for name in sorted(names - expected):
        errors.append(
            VerificationError(
                _CODE_MEMBER_SET_MISMATCH, member=name, message="undeclared archive member"
            )
        )
    for path in sorted(expected - names):
        errors.append(
            VerificationError(
                _CODE_MEMBER_SET_MISMATCH, member=path, message="declared file missing from archive"
            )
        )


def _load_sums(
    zf: zipfile.ZipFile, names: set[str], errors: list[VerificationError]
) -> Optional[dict[str, str]]:
    if CHECKSUMS_MEMBER_NAME not in names:
        errors.append(
            VerificationError(
                _CODE_CHECKSUM_FILE_INVALID,
                member=CHECKSUMS_MEMBER_NAME,
                message="missing SHA256SUMS member",
            )
        )
        return None
    try:
        raw = _read_member_capped(zf, CHECKSUMS_MEMBER_NAME, _MAX_META_BYTES)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        errors.append(
            VerificationError(
                _CODE_CHECKSUM_FILE_INVALID, member=CHECKSUMS_MEMBER_NAME, message=str(exc)
            )
        )
        return None
    sums: dict[str, str] = {}
    for line in text.splitlines(keepends=True):
        if not line.endswith("\n"):
            errors.append(
                VerificationError(
                    _CODE_CHECKSUM_FILE_INVALID,
                    member=CHECKSUMS_MEMBER_NAME,
                    message=f"line not newline-terminated: {line[:80]!r}",
                )
            )
            return None
        parsed = parse_sha256_line(line[:-1])
        if parsed is None:
            errors.append(
                VerificationError(
                    _CODE_CHECKSUM_FILE_INVALID,
                    member=CHECKSUMS_MEMBER_NAME,
                    message=f"malformed checksum line: {line[:80]!r}",
                )
            )
            return None
        digest, path = parsed
        if path in sums:
            errors.append(
                VerificationError(
                    _CODE_CHECKSUM_FILE_INVALID,
                    member=CHECKSUMS_MEMBER_NAME,
                    message=f"duplicate checksum entry: {path}",
                )
            )
            return None
        sums[path] = digest
    return sums


def _check_payload_hashes(
    zf: zipfile.ZipFile,
    declared: list[dict],
    names: set[str],
    sums: Optional[dict[str, str]],
    errors: list[VerificationError],
) -> None:
    """ZIP 内流式哈希：先算不解压；以 manifest 声明大小封顶读取。"""
    for entry in declared:
        path = entry["path"]
        if path not in names:
            continue  # 成员集合检查已单独报错
        expected_size = entry.get("size")
        expected_hash = entry.get("sha256")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool):
            continue  # manifest 结构检查已单独报错
        try:
            with zf.open(path) as fh:
                digest, total = sha256_stream(fh, max_bytes=expected_size)
        except ValueError:
            errors.append(
                VerificationError(
                    _CODE_FILE_SIZE_MISMATCH,
                    member=path,
                    message=f"actual size exceeds declared {expected_size} bytes",
                )
            )
            continue
        except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            errors.append(
                VerificationError(_CODE_FILE_SIZE_MISMATCH, member=path, message=str(exc))
            )
            continue
        if total != expected_size:
            errors.append(
                VerificationError(
                    _CODE_FILE_SIZE_MISMATCH,
                    member=path,
                    message=f"declared size {expected_size} != actual {total}",
                )
            )
        if isinstance(expected_hash, str) and digest != expected_hash:
            errors.append(
                VerificationError(
                    _CODE_FILE_HASH_MISMATCH,
                    member=path,
                    message="actual sha256 differs from manifest",
                )
            )
        if sums is not None and sums.get(path) != expected_hash:
            errors.append(
                VerificationError(
                    _CODE_CHECKSUM_FILE_INVALID,
                    member=CHECKSUMS_MEMBER_NAME,
                    message=f"SHA256SUMS entry disagrees with manifest for {path}",
                )
            )


def _check_sidecar(archive: Path, errors: list[VerificationError]) -> None:
    """包外 sidecar：存在时必须精确等于 ``<zip哈希>  <zip名>\\n``。"""
    sidecar = archive.with_name(archive.name + ".sha256")
    if not sidecar.is_file():
        return  # sidecar 可选（完整性由 ZIP 内 manifest+SUMS 保证）
    text = sidecar.read_text(encoding="utf-8")
    with archive.open("rb") as fh:
        digest, _ = sha256_stream(fh)
    expected = format_sha256_line(digest, archive.name)
    if text != expected:
        errors.append(
            VerificationError(
                _CODE_SIDECAR_MISMATCH,
                member=sidecar.name,
                message="sidecar must be '<zip sha256>  <this zip filename>' (reject sidecars for other archives)",
            )
        )


def _extract(
    zf: zipfile.ZipFile,
    names: list[str],
    extract_to: Path,
    errors: list[VerificationError],
) -> Optional[Path]:
    """全部校验通过后的安全解压：目标为空 + resolved path 必须落在目标根内。"""
    if extract_to.exists() and (not extract_to.is_dir() or any(extract_to.iterdir())):
        errors.append(
            VerificationError(
                _CODE_EXTRACT_TARGET_NOT_EMPTY,
                message=f"extract target must not exist or be empty: {extract_to}",
            )
        )
        return None
    root_resolved = extract_to.resolve()
    extract_to.mkdir(parents=True, exist_ok=True)
    for name in names:
        member_path = normalize_member(name)
        destination = extract_to / member_path
        destination_resolved = destination.resolve()
        if root_resolved not in destination_resolved.parents:
            errors.append(
                VerificationError(
                    _CODE_UNSAFE_EXTRACT_PATH,
                    member=name,
                    message="resolved destination escapes extract root",
                )
            )
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(name) as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    return extract_to


def verify_artifact(
    archive: Path,
    expected_component: str,
    expected_commit: str,
    extract_to: Optional[Path] = None,
) -> VerificationReport:
    """校验 RC ZIP 工件；``extract_to`` 提供且全部检查通过时才安全解压。"""
    archive = Path(archive)
    if not archive.is_file():
        raise FileNotFoundError(f"archive not found: {archive}")

    errors: list[VerificationError] = []
    manifest: Optional[dict] = None
    names: list[str] = []
    extracted_to: Optional[Path] = None

    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        names = [info.filename for info in infos]
        unique_names = set(names)

        _check_members(infos, errors)
        manifest = _load_manifest(zf, unique_names, errors)
        identity_usable = _check_identity(manifest, expected_component, expected_commit, errors)

        declared = _declared_files(manifest) if identity_usable else []
        _check_member_set(declared, unique_names, errors)

        sums: Optional[dict[str, str]] = None
        if CHECKSUMS_MEMBER_NAME in unique_names or declared:
            sums = _load_sums(zf, unique_names, errors)
        if identity_usable and declared:
            _check_payload_hashes(zf, declared, unique_names, sums, errors)

    _check_sidecar(archive, errors)

    if extract_to is not None and not errors:
        with zipfile.ZipFile(archive) as zf:
            extracted_to = _extract(zf, names, Path(extract_to), errors)

    component = manifest.get("component") if isinstance(manifest, dict) else None
    release_candidate = manifest.get("release_candidate") if isinstance(manifest, dict) else None
    git_commit = manifest.get("git_commit") if isinstance(manifest, dict) else None
    contract_version = manifest.get("contract_version") if isinstance(manifest, dict) else None
    file_count = len(_declared_files(manifest)) if identity_usable else 0

    return VerificationReport(
        archive=archive,
        ok=not errors,
        errors=tuple(errors),
        component=component if isinstance(component, str) else None,
        release_candidate=release_candidate if isinstance(release_candidate, str) else None,
        git_commit=git_commit if isinstance(git_commit, str) else None,
        contract_version=contract_version if isinstance(contract_version, str) else None,
        file_count=file_count,
        extracted_to=extracted_to,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an RC handoff ZIP artifact (streaming hashes, reject-before-extract)."
    )
    parser.add_argument("--archive", required=True, type=Path, help="RC ZIP 路径")
    parser.add_argument("--expected-component", required=True, help="期望组件（下划线枚举）")
    parser.add_argument("--expected-commit", required=True, help="期望 40 位组件提交")
    parser.add_argument(
        "--extract-to",
        type=Path,
        default=None,
        help="可选：全部校验通过后解压到的空目录",
    )
    args = parser.parse_args(argv)

    report = verify_artifact(
        args.archive,
        expected_component=args.expected_component,
        expected_commit=args.expected_commit,
        extract_to=args.extract_to,
    )
    for item in report.errors:
        location = f" member={item.member}" if item.member else ""
        print(f"[{item.code}]{location} {item.message}")
    if report.ok:
        print(
            f"verification OK: component={report.component} "
            f"release_candidate={report.release_candidate} files={report.file_count}"
        )
        if report.extracted_to is not None:
            print(f"extracted to: {report.extracted_to}")
        return 0
    print(f"verification FAILED: {len(report.errors)} error(s); nothing extracted")
    return 1


if __name__ == "__main__":
    sys.exit(main())
