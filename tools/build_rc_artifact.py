# -*- coding: utf-8 -*-
"""RC 工件生成器：从组件仓库本地槽位构建可私下交接的 ZIP64 工件。

流程（任务简报 Step 4 八步）：
1. ``git status --porcelain`` 非空 -> 拒绝生成正式 RC（工件内容必须冻结在
   固定提交上，脏工作树无法保证可复现）；
2. ``git rev-parse HEAD`` 取 40 位完整哈希，CLI ``--commit`` 提供时必须一致；
3. 读取组件仓库 ``handoff/artifact-map.yaml``，local/payload 一律经
   ``normalize_member`` 归一化，禁止绝对路径与越界（``..``）路径；
4. 枚举槽位普通文件：不跟随符号链接，按 payload 成员路径排序；
5. 逐文件流式计算 size/SHA256，生成满足契约 ``handoff-manifest.schema.json``
   的 ``HANDOFF_MANIFEST.json``（生成前先做结构校验，不产出非法 manifest）；
6. 写 ``SHA256SUMS``，以 ``ZIP_DEFLATED`` + ``allowZip64=True`` 打包；
7. 包外生成整个 ZIP 的小写 SHA256 sidecar（``<hash>  <zip名>\\n``）；
8. 本地槽位缺失 / 映射为空 / manifest 元数据缺失时立即失败，绝不生成空包。

文件名 ``{slug}-artifacts-vX.Y.Z-rc.N+{shortsha}.zip``，shortsha 恒取 HEAD
前 8 位由工具自动形成，调用者不得手填。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

try:
    from .artifact_common import (
        CHECKSUMS_MEMBER_NAME,
        COMPONENT_SLUGS,
        MANIFEST_MEMBER_NAME,
        MANIFEST_SCHEMA_VERSION,
        SUPPORTED_CONTRACT_VERSION,
        artifact_basename,
        format_sha256_line,
        is_full_commit,
        is_semver,
        iter_sorted_payloads,
        normalize_member,
        release_candidate_name,
        sha256_file,
        sha256_stream,
        validate_manifest_structure,
    )
except ImportError:  # pragma: no cover - 以 `python tools/build_rc_artifact.py` 运行
    from artifact_common import (  # type: ignore
        CHECKSUMS_MEMBER_NAME,
        COMPONENT_SLUGS,
        MANIFEST_MEMBER_NAME,
        MANIFEST_SCHEMA_VERSION,
        SUPPORTED_CONTRACT_VERSION,
        artifact_basename,
        format_sha256_line,
        is_full_commit,
        is_semver,
        iter_sorted_payloads,
        normalize_member,
        release_candidate_name,
        sha256_file,
        sha256_stream,
        validate_manifest_structure,
    )

ARTIFACT_MAP_RELPATH = Path("handoff") / "artifact-map.yaml"


def _git(repo_root: Path, *args: str) -> str:
    """在组件仓库内执行 git 命令，失败时抛 RuntimeError（带 stderr 摘要）。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"failed to run git: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}): {stderr}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _detect_environment(overrides: Optional[dict]) -> dict:
    """构建环境自检；artifact-map 提供的 environment 逐键覆盖。"""
    detected = {
        "python": platform.python_version(),
        "pytorch": _detect_pytorch(),
        "os": platform.platform(),
        "device": "cpu",
    }
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in detected and isinstance(value, str) and value:
                detected[key] = value
    return detected


def _detect_pytorch() -> str:
    try:
        import torch  # type: ignore
    except Exception:
        return "not-installed"
    try:
        if torch.cuda.is_available():
            return f"{torch.__version__}+cuda"
    except Exception:
        pass
    return str(torch.__version__)


def _require_str_list(value: object, key: str, *, min_items: int, item_type: type) -> list:
    if not isinstance(value, list) or len(value) < min_items:
        raise ValueError(f"artifact-map 缺少合法的 {key}（非空列表，manifest 必填）")
    for item in value:
        if not isinstance(item, item_type) or (
            item_type is str and not item  # type: ignore[comparison-overlap]
        ):
            raise ValueError(f"artifact-map.{key} 含非法元素: {item!r}")
        if item_type is int and isinstance(item, bool):
            raise ValueError(f"artifact-map.{key} 含非法元素: {item!r}")
    if item_type is int and any(item < 0 for item in value):  # type: ignore[operator]
        raise ValueError(f"artifact-map.{key} 的种子必须 >= 0")
    return value  # type: ignore[return-value]


def _load_artifact_map(repo_root: Path, component: str, contract_version: str) -> dict:
    map_path = repo_root / ARTIFACT_MAP_RELPATH
    if not map_path.is_file():
        raise RuntimeError(f"artifact map not found: {map_path}")
    data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("artifact-map.yaml 必须是映射对象")
    map_component = data.get("component")
    if map_component != component:
        raise ValueError(
            f"artifact-map component ({map_component!r}) 与 --component ({component!r}) 不一致"
        )
    map_contract = data.get("contract_version")
    if map_contract is not None and map_contract != contract_version:
        raise ValueError(
            f"artifact-map contract_version ({map_contract!r}) 与 --contract-version "
            f"({contract_version!r}) 不一致"
        )
    return data


def _resolve_slot(repo_root: Path, local: str, payload: str) -> tuple[Path, str]:
    """归一化并解析单个映射：返回 (本地槽位绝对路径, payload 成员前缀)。"""
    local_rel = normalize_member(local)  # 相对、无 ..、无盘符、无反斜杠
    payload_rel = normalize_member(payload)
    repo_resolved = repo_root.resolve()
    slot_abs = (repo_root / local_rel).resolve()
    if slot_abs == repo_resolved or repo_resolved not in slot_abs.parents:
        raise ValueError(f"local 槽位越界（必须位于组件仓库根内）: {local!r}")
    return slot_abs, payload_rel.as_posix()


def _enumerate_slot_files(slot_abs: Path, payload_prefix: str) -> list[tuple[str, Path]]:
    """枚举槽位内普通文件（不跟随 symlink），返回 (payload 成员路径, 源文件)。"""
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(slot_abs, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            filepath = Path(dirpath) / name
            if filepath.is_symlink() or not filepath.is_file():
                continue  # 只打包普通文件，symlink 一律跳过（不跟随）
            rel = filepath.relative_to(slot_abs)
            member = f"{payload_prefix}/{rel.as_posix()}"
            normalize_member(member)  # 成员路径必须能通过统一归一化
            found.append((member, filepath))
    return found


def build_artifact(
    repo_root: Path,
    component: str,
    version: str,
    rc: int,
    contract_version: str,
    output_dir: Path,
) -> Path:
    """构建 RC ZIP 工件，返回 ZIP 路径（同目录生成 ``<zip>.sha256`` sidecar）。"""
    repo_root = Path(repo_root)
    output_dir = Path(output_dir)

    if component not in COMPONENT_SLUGS:
        raise ValueError(f"unknown component: {component!r} (expected {sorted(COMPONENT_SLUGS)})")
    if not is_semver(version):
        raise ValueError(f"version 必须是 X.Y.Z 三段数字: {version!r}")
    if not isinstance(rc, int) or isinstance(rc, bool) or rc < 1:
        raise ValueError(f"rc 必须是 >= 1 的整数: {rc!r}")
    if contract_version != SUPPORTED_CONTRACT_VERSION:
        raise ValueError(
            f"unsupported contract version: {contract_version!r} "
            f"(expected {SUPPORTED_CONTRACT_VERSION!r})"
        )
    if not repo_root.is_dir():
        raise ValueError(f"repo_root 不存在或不是目录: {repo_root}")

    # Step 1: 拒绝脏工作树（正式 RC 内容必须冻结在固定提交）
    porcelain = _git(repo_root, "status", "--porcelain")
    if porcelain:
        raise RuntimeError(
            "工作树不干净，拒绝生成正式 RC（先提交或清理以下变更）:\n" + porcelain
        )

    # Step 2: HEAD 完整哈希（文件名 shortsha 由此自动形成）
    head = _git(repo_root, "rev-parse", "HEAD")
    if not is_full_commit(head):
        raise RuntimeError(f"git rev-parse HEAD 返回非法哈希: {head!r}")

    # Step 3: 读取并校验 artifact-map（local/payload 均受统一归一化约束）
    artifact_map = _load_artifact_map(repo_root, component, contract_version)
    mappings = artifact_map.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("artifact-map.yaml 必须包含非空 mappings 列表")

    payload_files: list[tuple[str, Path]] = []
    member_mapping: dict[str, dict] = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError(f"artifact-map mappings 项必须是映射对象: {mapping!r}")
        local = mapping.get("local")
        payload = mapping.get("payload")
        role = mapping.get("role")
        if not isinstance(local, str) or not local:
            raise ValueError(f"mapping 缺少合法 local: {mapping!r}")
        if not isinstance(payload, str) or not payload:
            raise ValueError(f"mapping 缺少合法 payload: {mapping!r}")
        if not isinstance(role, str) or not role:
            raise ValueError(f"mapping 缺少合法 role: {mapping!r}")
        slot_abs, payload_prefix = _resolve_slot(repo_root, local, payload)
        if not slot_abs.is_dir():
            raise RuntimeError(f"本地槽位不存在（拒绝生成空包）: {local} -> {slot_abs}")
        for member, src in _enumerate_slot_files(slot_abs, payload_prefix):
            if member in member_mapping:
                raise ValueError(
                    f"payload 成员路径冲突: {member}（已被 {member_mapping[member]['payload']} 占用）"
                )
            member_mapping[member] = mapping
            payload_files.append((member, src))

    # Step 4: 按 payload 路径排序
    payload_files = iter_sorted_payloads(payload_files)
    if not payload_files:
        raise RuntimeError("artifact-map 未映射到任何普通文件，拒绝生成空包")

    # Step 5: 逐文件 size/SHA256 + HANDOFF_MANIFEST.json
    file_entries = []
    for member, src in payload_files:
        digest, size = sha256_file(src)
        mapping = member_mapping[member]
        file_entries.append(
            {
                "path": member,
                "role": mapping["role"],
                "size": size,
                "sha256": digest,
                # role 级元数据从 mapping 带出（缺省保守值：不可再分发 + 事实性溯源）
                "redistributable": bool(mapping.get("redistributable", False)),
                "source_url": mapping.get("source_url"),
                "processing_provenance": str(
                    mapping.get("processing_provenance")
                    or (
                        f"由 build_rc_artifact 依 handoff/artifact-map.yaml 从本地槽位 "
                        f"{mapping['local']} 打包（git_commit={head}）；"
                        "artifact-map 未声明 processing_provenance"
                    )
                ),
            }
        )

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "component": component,
        "release_candidate": release_candidate_name(component, version, rc),
        "git_commit": head,
        "contract_version": contract_version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "environment": _detect_environment(artifact_map.get("environment")),
        "random_seeds": _require_str_list(
            artifact_map.get("random_seeds"), "random_seeds", min_items=1, item_type=int
        ),
        "commands": _require_str_list(
            artifact_map.get("commands"), "commands", min_items=1, item_type=str
        ),
        "files": file_entries,
        "public_summary": str(artifact_map.get("public_summary") or ""),
        "reproduce_status": str(artifact_map.get("reproduce_status") or "REPRODUCE_OK"),
    }
    issues = validate_manifest_structure(manifest)
    if issues:
        detail = "; ".join(f"[{code}] {message}" for code, message in issues)
        raise RuntimeError(f"生成的 HANDOFF_MANIFEST.json 不满足契约 schema: {detail}")

    # Step 6: SHA256SUMS + ZIP64 打包
    sums_text = "".join(
        format_sha256_line(entry["sha256"], entry["path"]) for entry in file_entries
    )
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    archive_name = artifact_basename(component, version, rc, head[:8])
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / archive_name
    if archive_path.exists():
        raise RuntimeError(f"目标工件已存在，拒绝覆盖（如需重打请先移除或提升 rc 号）: {archive_path}")

    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as zf:
        for member, src in payload_files:
            zf.write(src, arcname=member)
        zf.writestr(MANIFEST_MEMBER_NAME, manifest_bytes)
        zf.writestr(CHECKSUMS_MEMBER_NAME, sums_text.encode("utf-8"))

    # Step 7: 包外 sidecar（64 位小写哈希 + 两空格 + ZIP 文件名 + 换行）
    with archive_path.open("rb") as fh:
        zip_digest, _ = sha256_stream(fh)
    sidecar_path = archive_path.with_name(archive_path.name + ".sha256")
    sidecar_path.write_text(
        format_sha256_line(zip_digest, archive_path.name), encoding="utf-8"
    )

    print(f"RC artifact built: {archive_path}")
    print(f"  component={component} release_candidate={manifest['release_candidate']}")
    print(f"  git_commit={head} files={len(file_entries)}")
    print(f"  sidecar={sidecar_path.name}")
    return archive_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an RC handoff ZIP artifact from component-repo local slots."
    )
    parser.add_argument("--component", required=True, choices=sorted(COMPONENT_SLUGS))
    parser.add_argument("--repo-root", required=True, type=Path, help="组件仓库根目录")
    parser.add_argument("--version", required=True, help="X.Y.Z 语义化版本")
    parser.add_argument("--rc", required=True, type=int, help="RC 序号（>= 1）")
    parser.add_argument("--contract-version", required=True, help="契约版本")
    parser.add_argument("--output-dir", required=True, type=Path, help="输出目录")
    parser.add_argument(
        "--commit",
        default=None,
        help="可选：预期的组件仓库 HEAD（40 位十六进制）；与实际 HEAD 不一致则拒绝生成",
    )
    args = parser.parse_args(argv)

    if args.commit is not None:
        try:
            head = _git(args.repo_root, "rev-parse", "HEAD")
        except RuntimeError as exc:
            print(f"build FAILED: {exc}", file=sys.stderr)
            return 1
        if not is_full_commit(args.commit) or args.commit != head:
            print(
                f"build FAILED: --commit ({args.commit}) 与 HEAD ({head}) 不一致",
                file=sys.stderr,
            )
            return 1

    try:
        build_artifact(
            repo_root=args.repo_root,
            component=args.component,
            version=args.version,
            rc=args.rc,
            contract_version=args.contract_version,
            output_dir=args.output_dir,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"build FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
