# -*- coding: utf-8 -*-
"""RC 工件生成器（tools/build_rc_artifact.py）的行为测试。

用 tests/fixtures/artifacts/ 的合成组件仓库骨架在 tmp_path 内初始化 git 仓库
后构建，不依赖任何组件仓库实际工件。
"""
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.build_rc_artifact import build_artifact, main  # noqa: E402
from tools.verify_rc_artifact import verify_artifact  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "artifacts"

# 槽位 -> fixture 文件的复制计划（合成，全部小文件）。
SLOT_FILES: dict[str, list[str]] = {
    "data": ["data_manifest.json", "telemetry.csv"],
    "checkpoints": ["model_weights.bin"],
    "results/reference": ["metrics.json"],
}

# 期望的 payload 成员（payload 根 / 槽位内相对路径），按 payload 路径排序。
EXPECTED_MEMBERS: list[str] = sorted(
    [
        "03_代码/components/battery/checkpoints/model_weights.bin",
        "04_数据/battery/data_manifest.json",
        "04_数据/battery/telemetry.csv",
        "05_结果/reference/battery/metrics.json",
    ]
)


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("utf-8").strip()


def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)


def _make_component_repo(tmp_path: Path, map_override: dict | None = None) -> Path:
    """从合成 fixture 复制出组件仓库骨架并提交，返回仓库根。"""
    root = tmp_path / "xa-battery-fixture"
    (root / "handoff").mkdir(parents=True)
    map_data = yaml.safe_load((FIXTURE_DIR / "artifact-map.yaml").read_text(encoding="utf-8"))
    if map_override:
        map_data.update(map_override)
    (root / "handoff" / "artifact-map.yaml").write_text(
        yaml.safe_dump(map_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # 槽位内容在真实组件仓库中被 gitignore（大文件不入库）， porcelain 仍应干净
    (root / ".gitignore").write_text(
        "data/\ncheckpoints/\nresults/\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# synthetic battery fixture repo\n", encoding="utf-8")
    _init_git(root)
    _git(root, "add", ".gitignore", "README.md", "handoff/artifact-map.yaml")
    _git(root, "commit", "-q", "-m", "synthetic skeleton")

    slots_dir = FIXTURE_DIR / "slots"
    for slot, names in SLOT_FILES.items():
        target_dir = root / slot
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copyfile(slots_dir / name, target_dir / name)
    return root


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _sums_lines(archive: Path) -> dict[str, str]:
    with zipfile.ZipFile(archive) as zf:
        text = zf.read("SHA256SUMS").decode("utf-8")
    sums = {}
    for line in text.splitlines():
        digest, _, path = line.partition("  ")
        sums[path] = digest
    return sums


# ---------------------------------------------------------------------------
# 正例：生成物结构
# ---------------------------------------------------------------------------


def test_build_produces_expected_zip_manifest_sums_and_sidecar(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    head = _head(repo)
    out_dir = tmp_path / "outgoing"
    archive = build_artifact(
        repo_root=repo,
        component="battery",
        version="0.1.0",
        rc=1,
        contract_version="component-contract-v1.1.0",
        output_dir=out_dir,
    )
    shortsha = head[:8]
    assert archive.name == f"battery-artifacts-v0.1.0-rc.1+{shortsha}.zip"
    assert archive.is_file()

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("HANDOFF_MANIFEST.json").decode("utf-8"))

    # 成员 = 排序后的 payload 文件 + manifest + sums，顺序按 payload 路径排序
    assert names == EXPECTED_MEMBERS + ["HANDOFF_MANIFEST.json", "SHA256SUMS"]
    assert manifest["component"] == "battery"  # manifest 用下划线枚举
    assert manifest["release_candidate"] == "battery-v0.1.0-rc.1"  # tag 用连字符 slug
    assert manifest["git_commit"] == head
    assert manifest["contract_version"] == "component-contract-v1.1.0"
    assert manifest["reproduce_status"] == "REPRODUCE_OK"
    assert [f["path"] for f in manifest["files"]] == EXPECTED_MEMBERS

    # 逐文件 size/sha256 与磁盘一致
    slots_dir = FIXTURE_DIR / "slots"
    src_bytes = {
        "04_数据/battery/data_manifest.json": (slots_dir / "data_manifest.json").read_bytes(),
        "04_数据/battery/telemetry.csv": (slots_dir / "telemetry.csv").read_bytes(),
        "03_代码/components/battery/checkpoints/model_weights.bin": (
            slots_dir / "model_weights.bin"
        ).read_bytes(),
        "05_结果/reference/battery/metrics.json": (slots_dir / "metrics.json").read_bytes(),
    }
    for entry in manifest["files"]:
        blob = src_bytes[entry["path"]]
        assert entry["size"] == len(blob)
        assert entry["sha256"] == hashlib.sha256(blob).hexdigest()
        assert entry["redistributable"] is (entry["path"].startswith("04_") or entry["path"].startswith("05_"))
        if entry["path"].startswith("03_"):
            assert entry["source_url"] == "https://example.com/battery-weights"

    # SHA256SUMS 覆盖全部 payload 文件且与 manifest 一致
    sums = _sums_lines(archive)
    assert set(sums) == set(EXPECTED_MEMBERS)
    for entry in manifest["files"]:
        assert sums[entry["path"]] == entry["sha256"]

    # 包外 .sha256 sidecar：64 位小写哈希 + 两空格 + ZIP 名 + 换行
    sidecar = archive.with_name(archive.name + ".sha256")
    assert sidecar.is_file()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sidecar.read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"


def test_build_roundtrip_verifies_and_extracts(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    head = _head(repo)
    archive = build_artifact(
        repo_root=repo,
        component="battery",
        version="0.1.0",
        rc=1,
        contract_version="component-contract-v1.1.0",
        output_dir=tmp_path / "outgoing",
    )
    target = tmp_path / "incoming"
    report = verify_artifact(
        archive,
        expected_component="battery",
        expected_commit=head,
        extract_to=target,
    )
    assert report.ok, [f"{e.code}: {e.message}" for e in report.errors]
    assert report.file_count == len(EXPECTED_MEMBERS)
    slots_dir = FIXTURE_DIR / "slots"
    assert (target / "04_数据" / "battery" / "telemetry.csv").read_bytes() == (
        slots_dir / "telemetry.csv"
    ).read_bytes()
    assert (target / "03_代码" / "components" / "battery" / "checkpoints" / "model_weights.bin").is_file()


def test_build_phased_array_slug(tmp_path) -> None:
    repo = _make_component_repo(
        tmp_path,
        map_override={"component": "phased_array"},
    )
    head = _head(repo)
    archive = build_artifact(
        repo_root=repo,
        component="phased_array",
        version="0.2.0",
        rc=3,
        contract_version="component-contract-v1.1.0",
        output_dir=tmp_path / "outgoing",
    )
    assert archive.name == f"phased-array-artifacts-v0.2.0-rc.3+{head[:8]}.zip"
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("HANDOFF_MANIFEST.json").decode("utf-8"))
    assert manifest["component"] == "phased_array"
    assert manifest["release_candidate"] == "phased-array-v0.2.0-rc.3"
    report = verify_artifact(archive, expected_component="phased_array", expected_commit=head)
    assert report.ok, [f"{e.code}: {e.message}" for e in report.errors]


def test_cli_main_end_to_end(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    head = _head(repo)
    out_dir = tmp_path / "outgoing"
    rc = main(
        [
            "--component", "battery",
            "--repo-root", str(repo),
            "--version", "0.1.0",
            "--rc", "1",
            "--contract-version", "component-contract-v1.1.0",
            "--output-dir", str(out_dir),
            "--commit", head,
        ]
    )
    assert rc == 0
    assert (out_dir / f"battery-artifacts-v0.1.0-rc.1+{head[:8]}.zip").is_file()


# ---------------------------------------------------------------------------
# 拒生成条件
# ---------------------------------------------------------------------------


def test_refuses_dirty_worktree(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    (repo / "stray.txt").write_text("untracked", encoding="utf-8")  # 未忽略的脏文件
    with pytest.raises(RuntimeError, match="工作树不干净"):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )


def test_cli_refuses_commit_mismatch(tmp_path, capsys) -> None:
    repo = _make_component_repo(tmp_path)
    rc = main(
        [
            "--component", "battery",
            "--repo-root", str(repo),
            "--version", "0.1.0",
            "--rc", "1",
            "--contract-version", "component-contract-v1.1.0",
            "--output-dir", str(tmp_path / "outgoing"),
            "--commit", "f" * 40,  # 与 HEAD 不一致
        ]
    )
    assert rc != 0
    assert not any((tmp_path / "outgoing").glob("*.zip"))


def test_refuses_local_path_escape_in_map(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    map_path = repo / "handoff" / "artifact-map.yaml"
    map_data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    map_data["mappings"][0]["local"] = "../outside"
    map_path.write_text(yaml.safe_dump(map_data, allow_unicode=True), encoding="utf-8")
    _git(repo, "add", "handoff/artifact-map.yaml")
    _git(repo, "commit", "-q", "-m", "evil map")
    with pytest.raises(ValueError):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )
    assert not (tmp_path / "outside").exists()


def test_refuses_absolute_local_in_map(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    map_path = repo / "handoff" / "artifact-map.yaml"
    map_data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    map_data["mappings"][0]["local"] = "/etc"
    map_path.write_text(yaml.safe_dump(map_data, allow_unicode=True), encoding="utf-8")
    _git(repo, "add", "handoff/artifact-map.yaml")
    _git(repo, "commit", "-q", "-m", "evil map")
    with pytest.raises(ValueError):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )


def test_refuses_payload_escape_in_map(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    map_path = repo / "handoff" / "artifact-map.yaml"
    map_data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    map_data["mappings"][0]["payload"] = "../escape"
    map_path.write_text(yaml.safe_dump(map_data, allow_unicode=True), encoding="utf-8")
    _git(repo, "add", "handoff/artifact-map.yaml")
    _git(repo, "commit", "-q", "-m", "evil map")
    with pytest.raises(ValueError):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )


def test_refuses_missing_slot_directory(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    shutil.rmtree(repo / "checkpoints")  # 本地槽位缺失：宁可不生成空包
    with pytest.raises(RuntimeError, match="checkpoints"):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )
    outgoing = tmp_path / "outgoing"
    assert not (outgoing.exists() and any(outgoing.glob("*.zip")))


def test_refuses_map_component_mismatch(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)  # map 声明 battery
    with pytest.raises(ValueError):
        build_artifact(
            repo_root=repo,
            component="wheel",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )


def test_refuses_missing_manifest_metadata(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    map_path = repo / "handoff" / "artifact-map.yaml"
    map_data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    del map_data["random_seeds"]  # manifest 必填元数据缺失
    map_path.write_text(yaml.safe_dump(map_data, allow_unicode=True), encoding="utf-8")
    _git(repo, "add", "handoff/artifact-map.yaml")
    _git(repo, "commit", "-q", "-m", "drop seeds")
    with pytest.raises(ValueError):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )


def test_refuses_unsupported_contract_version(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    with pytest.raises(ValueError, match="contract"):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.0.0",
            output_dir=tmp_path / "outgoing",
        )


def test_refuses_invalid_version(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    with pytest.raises(ValueError):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1",  # 不是 X.Y.Z
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )


def test_refuses_unknown_component(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    with pytest.raises(ValueError):
        build_artifact(
            repo_root=repo,
            component="star_tracker",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=tmp_path / "outgoing",
        )


def test_refuses_overwriting_existing_archive(tmp_path) -> None:
    repo = _make_component_repo(tmp_path)
    out_dir = tmp_path / "outgoing"
    out_dir.mkdir()
    (out_dir / f"battery-artifacts-v0.1.0-rc.1+{_head(repo)[:8]}.zip").write_bytes(b"stale")
    with pytest.raises(RuntimeError, match="已存在"):
        build_artifact(
            repo_root=repo,
            component="battery",
            version="0.1.0",
            rc=1,
            contract_version="component-contract-v1.1.0",
            output_dir=out_dir,
        )
    assert (out_dir / f"battery-artifacts-v0.1.0-rc.1+{_head(repo)[:8]}.zip").read_bytes() == b"stale"


# ---------------------------------------------------------------------------
# symlink 处理：不跟随、不打包
# ---------------------------------------------------------------------------


def test_build_skips_symlinked_files(tmp_path) -> None:
    try:
        (tmp_path / "link-target.bin").write_bytes(b"target-bytes")
        repo = _make_component_repo(tmp_path)
        os_symlink = __import__("os").symlink
        os_symlink(
            tmp_path / "link-target.bin",
            repo / "checkpoints" / "sneaky-link.bin",
        )
    except OSError as exc:  # Windows 无开发者模式/管理员权限时无法建 symlink
        pytest.skip(f"symlink not supported on this host: {exc}")
    archive = build_artifact(
        repo_root=repo,
        component="battery",
        version="0.1.0",
        rc=1,
        contract_version="component-contract-v1.1.0",
        output_dir=tmp_path / "outgoing",
    )
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "03_代码/components/battery/checkpoints/sneaky-link.bin" not in names
    # 普通文件仍被完整打包
    assert "03_代码/components/battery/checkpoints/model_weights.bin" in names
