# -*- coding: utf-8 -*-
"""安全装配（tools/assemble_delivery.py）的行为测试。

合成 fixture：三个组件仓库骨架 + 三个 RC ZIP（由 build_rc_artifact 生成）+
合成 reproduced / slots 输入 -> 固定 01–07 槽位结构；全程在 tmp_path 内，
不依赖任何真实交付物。
"""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.assemble_delivery import (  # noqa: E402
    DELIVERY_STEM,
    DeliveryInputs,
    assemble,
    delivery_dirname,
)
from tools.build_rc_artifact import build_artifact  # noqa: E402
from tools.lockfile import ComponentEntry, ComponentLock, load_lock  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "artifacts"
SLOTS_FIXTURE = FIXTURE_DIR / "slots"

COMPONENTS = ("battery", "phased_array", "wheel")
CONTRACT_VERSION = "component-contract-v1.1.0"

SLOT_FILES: dict[str, list[str]] = {
    "data": ["data_manifest.json", "telemetry.csv"],
    "checkpoints": ["model_weights.bin"],
    "results/reference": ["metrics.json"],
}


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("utf-8").strip()


def _make_component_repo(base: Path, component: str, tag: str) -> tuple[Path, str]:
    """合成组件仓库（含 annotated tag），返回 (仓库根, HEAD)。"""
    import shutil

    root = base / f"xa-{component}-fixture"
    (root / "handoff").mkdir(parents=True)
    map_data = yaml.safe_load((FIXTURE_DIR / "artifact-map.yaml").read_text(encoding="utf-8"))
    map_data["component"] = component
    # payload 前缀按组件名替换（fixture 模板写死 battery）
    for mapping in map_data["mappings"]:
        mapping["payload"] = mapping["payload"].replace("battery", component)
    (root / "handoff" / "artifact-map.yaml").write_text(
        yaml.safe_dump(map_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("data/\ncheckpoints/\nresults/\n", encoding="utf-8")
    (root / "README.md").write_text(f"# synthetic {component} fixture repo\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "model.py").write_text(f"# {component} model\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "synthetic skeleton")
    _git(root, "tag", "-a", tag, "-m", "rc1", "HEAD")

    for slot, names in SLOT_FILES.items():
        target_dir = root / slot
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copyfile(SLOTS_FIXTURE / name, target_dir / name)
    return root, _git(root, "rev-parse", "HEAD")


def _write_reproduced(base: Path) -> dict[str, Path]:
    """合成 reproduced 输入：每组件一份声明路径树（05_结果/reproduced/<c>/...）。"""
    reproduced: dict[str, Path] = {}
    for component in COMPONENTS:
        root = base / "reproduced" / component
        sub = root / "05_结果" / "reproduced" / component
        sub.mkdir(parents=True)
        (sub / "metrics.json").write_text(
            f'{{"component": "{component}", "rmse": 0.5}}\n', encoding="utf-8"
        )
        reproduced[component] = root
    return reproduced


def _write_slots(base: Path) -> dict[str, Path]:
    slots: dict[str, Path] = {}
    report_dir = base / "docs-src"
    (report_dir / "01_技术方案报告").mkdir(parents=True)
    (report_dir / "01_技术方案报告" / "report.md").write_text("# 方案\n", encoding="utf-8")
    slots["01_技术方案报告"] = report_dir / "01_技术方案报告"
    slots["02_数据集与仿真方法说明.pdf"] = report_dir / "02_数据集与仿真方法说明.pdf"
    slots["02_数据集与仿真方法说明.pdf"].write_bytes(b"%PDF-1.4 synthetic\n")
    charts_dir = base / "charts-src"
    charts_dir.mkdir()
    (charts_dir / "fig1.png").write_bytes(b"\x89PNG synthetic\n")
    slots["06_图表"] = charts_dir
    acceptance_dir = base / "acceptance-src"
    acceptance_dir.mkdir()
    (acceptance_dir / "checklist.md").write_text("- [ ] ok\n", encoding="utf-8")
    slots["07_验收"] = acceptance_dir
    return slots


def _make_valid_inputs(tmp_path: Path) -> tuple[DeliveryInputs, dict[str, str]]:
    """三仓库 + 三 ZIP + 真 lock（记录真实 HEAD 与 ZIP 哈希）。"""
    tags = {
        "battery": "battery-v0.1.0-rc.1",
        "phased_array": "phased-array-v0.2.0-rc.3",
        "wheel": "wheel-v0.3.0-rc.2",
    }
    repos: dict[str, Path] = {}
    commits: dict[str, str] = {}
    for component in COMPONENTS:
        repo, head = _make_component_repo(tmp_path / "repos", component, tags[component])
        repos[component] = repo
        commits[component] = head

    artifacts: dict[str, Path] = {}
    artifact_hashes: dict[str, str] = {}
    versions = {"battery": "0.1.0", "phased_array": "0.2.0", "wheel": "0.3.0"}
    rcs = {"battery": 1, "phased_array": 3, "wheel": 2}
    for component in COMPONENTS:
        archive = build_artifact(
            repo_root=repos[component],
            component=component,
            version=versions[component],
            rc=rcs[component],
            contract_version=CONTRACT_VERSION,
            output_dir=tmp_path / "artifacts",
        )
        artifacts[component] = archive
        artifact_hashes[component] = hashlib.sha256(archive.read_bytes()).hexdigest()

    contract_head = "a" * 40
    lock_dict = {
        "schema_version": "1.0.0",
        "generated_at": "2026-08-15T00:00:00Z",
        "contract": {
            "repository": "xa-202608-team/xa-contract",
            "tag": "contract-v1.0.0",
            "commit": contract_head,
        },
        "components": {
            component: {
                "repository": f"xa-202608-team/xa-{component}",
                "tag": tags[component],
                "commit": commits[component],
                "artifact": artifacts[component].name,
                "artifact_sha256": artifact_hashes[component],
            }
            for component in COMPONENTS
        },
    }
    lock_path = tmp_path / "component-lock.yaml"
    lock_path.write_text(
        yaml.safe_dump(lock_dict, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    lock = load_lock(lock_path)

    inputs = DeliveryInputs(
        rc=1,
        lock=lock,
        repos=repos,
        artifacts=artifacts,
        reproduced=_write_reproduced(tmp_path / "inputs"),
        slots=_write_slots(tmp_path / "inputs"),
    )
    return inputs, commits


# ---------------------------------------------------------------------------
# 核心用例（任务简报逐字）
# ---------------------------------------------------------------------------


def test_assembly_refuses_existing_nonempty_destination(tmp_path, valid_inputs) -> None:
    destination = tmp_path / "delivery"
    destination.mkdir()
    (destination / "user-file.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="destination must be new and empty"):
        assemble(valid_inputs, destination)
    assert (destination / "user-file.txt").read_text("utf-8") == "preserve"


# ---------------------------------------------------------------------------
# 正例：固定槽位结构
# ---------------------------------------------------------------------------


def test_assemble_produces_full_slot_layout(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    destination = tmp_path / "build" / delivery_dirname(1)
    result = assemble(inputs, destination)
    assert result == destination
    assert destination.name == "XA-202608_最终交付-rc.1"

    # 01/02/04/06/07 槽位
    assert (destination / "01_技术方案报告" / "report.md").read_text(encoding="utf-8") == "# 方案\n"
    assert (destination / "02_数据集与仿真方法说明.pdf").read_bytes() == b"%PDF-1.4 synthetic\n"
    assert (destination / "04_数据" / "battery" / "telemetry.csv").read_bytes() == (
        SLOTS_FIXTURE / "telemetry.csv"
    ).read_bytes()
    assert (destination / "06_图表" / "fig1.png").is_file()
    assert (destination / "07_验收" / "checklist.md").is_file()

    # 03_代码：git 导出的源码 + RC ZIP 的 checkpoints
    for component in COMPONENTS:
        code = destination / "03_代码" / "components" / component
        assert (code / "README.md").is_file()
        assert (code / "src" / "model.py").is_file()
        assert (code / "handoff" / "artifact-map.yaml").is_file()
        assert (code / "checkpoints" / "model_weights.bin").read_bytes() == (
            SLOTS_FIXTURE / "model_weights.bin"
        ).read_bytes()

    # 05_结果：reference 来自 RC ZIP，reproduced 来自本地复现输入
    for component in COMPONENTS:
        assert (destination / "05_结果" / "reference" / component / "metrics.json").read_bytes() == (
            SLOTS_FIXTURE / "metrics.json"
        ).read_bytes()
        reproduced = destination / "05_结果" / "reproduced" / component / "metrics.json"
        assert f'"{component}"' in reproduced.read_text(encoding="utf-8")

    # 装配目录绝不含 .git
    assert not any(p.name == ".git" for p in destination.rglob("*"))


def test_assemble_accepts_existing_empty_destination(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    destination = tmp_path / "delivery"
    destination.mkdir()
    assemble(inputs, destination)
    assert (destination / "04_数据" / "battery" / "telemetry.csv").is_file()


def test_assemble_cli_main_end_to_end(tmp_path) -> None:
    from tools.assemble_delivery import main as assemble_main

    inputs, _ = _make_valid_inputs(tmp_path)
    lock_path = tmp_path / "component-lock.yaml"
    # _make_valid_inputs 已把 lock 写到该路径
    assert lock_path.is_file()
    rc = assemble_main(
        [
            "--lock", str(lock_path),
            "--rc", "1",
            "--output-root", str(tmp_path / "build"),
            *[arg for c in COMPONENTS for arg in ("--repo", f"{c}={inputs.repos[c]}")],
            *[arg for c in COMPONENTS for arg in ("--artifact", f"{c}={inputs.artifacts[c]}")],
            *[arg for c in COMPONENTS for arg in ("--reproduced", f"{c}={inputs.reproduced[c]}")],
            *[arg for pair in inputs.slots.items() for arg in ("--slot", f"{pair[0]}={pair[1]}")],
        ]
    )
    assert rc == 0
    destination = tmp_path / "build" / delivery_dirname(1)
    assert (destination / "04_数据" / "wheel" / "telemetry.csv").is_file()
    assert (destination / "05_结果" / "reproduced" / "wheel" / "metrics.json").is_file()


# ---------------------------------------------------------------------------
# 拒绝：reference 被 reproduced 覆盖 / .git / 命名
# ---------------------------------------------------------------------------


def test_reproduced_cannot_overwrite_reference(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    evil = inputs.reproduced["battery"] / "05_结果" / "reference" / "battery" / "metrics.json"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text('{"forged": true}\n', encoding="utf-8")
    destination = tmp_path / "delivery"
    with pytest.raises(ValueError, match="reference"):
        assemble(inputs, destination)
    # 失败后不留半成品、reference 未被伪造内容污染（destination 未生成）
    assert not destination.exists()


def test_reference_content_comes_from_rc_artifact_only(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    destination = tmp_path / "delivery"
    assemble(inputs, destination)
    reference = destination / "05_结果" / "reference" / "battery" / "metrics.json"
    assert reference.read_bytes() == (SLOTS_FIXTURE / "metrics.json").read_bytes()


def test_assembly_rejects_git_inside_reproduced_input(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    git_dir = inputs.reproduced["wheel"] / "05_结果" / "reproduced" / "wheel" / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="\\.git"):
        assemble(inputs, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


def test_assembly_rejects_final_delivery_stem_as_destination(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    with pytest.raises(ValueError, match=DELIVERY_STEM):
        assemble(inputs, tmp_path / DELIVERY_STEM)
    assert not (tmp_path / DELIVERY_STEM).exists()


# ---------------------------------------------------------------------------
# 拒绝：Tag 指向错误提交 / 工件哈希错误 / ZIP 被篡改
# ---------------------------------------------------------------------------


def test_assembly_rejects_tag_resolving_to_wrong_commit(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    # 给 wheel 仓库追加新提交并重打同名 tag -> tag 解析不再等于 lock 记录的 commit
    wheel_repo = inputs.repos["wheel"]
    (wheel_repo / "README.md").write_text("wheel v2\n", encoding="utf-8")
    _git(wheel_repo, "add", "README.md")
    _git(wheel_repo, "commit", "-q", "-m", "post-rc change")
    _git(wheel_repo, "tag", "-f", "-a", "wheel-v0.3.0-rc.2", "-m", "moved", "HEAD")
    with pytest.raises(ValueError, match="tag"):
        assemble(inputs, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


def test_assembly_rejects_wrong_artifact_sha256(tmp_path) -> None:
    import dataclasses

    inputs, _ = _make_valid_inputs(tmp_path)
    forged = "9" * 64
    bad_entry = inputs.lock.components["phased_array"]
    inputs = dataclasses.replace(
        inputs,
        lock=dataclasses.replace(
            inputs.lock,
            components={
                **inputs.lock.components,
                "phased_array": ComponentEntry(
                    repository=bad_entry.repository,
                    tag=bad_entry.tag,
                    commit=bad_entry.commit,
                    artifact=bad_entry.artifact,
                    artifact_sha256=forged,
                ),
            },
        ),
    )
    with pytest.raises(ValueError, match="artifact_sha256"):
        assemble(inputs, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


def test_assembly_rejects_tampered_zip_payload(tmp_path) -> None:
    import zipfile

    inputs, _ = _make_valid_inputs(tmp_path)
    battery_zip = inputs.artifacts["battery"]
    # 向已验证 ZIP 追加未声明成员 -> verify_artifact 拒收
    with zipfile.ZipFile(battery_zip, "a") as zf:
        zf.writestr("04_数据/battery/injected.txt", "injected")
    # ZIP 字节变了，先重算 lock 哈希使其通过哈希门，确保拦截来自 verify
    import dataclasses

    good = inputs.lock.components["battery"]
    inputs = dataclasses.replace(
        inputs,
        lock=dataclasses.replace(
            inputs.lock,
            components={
                **inputs.lock.components,
                "battery": ComponentEntry(
                    repository=good.repository,
                    tag=good.tag,
                    commit=good.commit,
                    artifact=good.artifact,
                    artifact_sha256=hashlib.sha256(battery_zip.read_bytes()).hexdigest(),
                ),
            },
        ),
    )
    with pytest.raises(ValueError, match="battery"):
        assemble(inputs, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


def test_assembly_failure_leaves_no_staging_residue(tmp_path) -> None:
    inputs, _ = _make_valid_inputs(tmp_path)
    evil = inputs.reproduced["battery"] / "05_结果" / "reference" / "battery" / "x.txt"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text("x", encoding="utf-8")
    build_root = tmp_path / "build"
    build_root.mkdir()
    destination = build_root / delivery_dirname(1)
    with pytest.raises(ValueError):
        assemble(inputs, destination)
    # staging 已清理，只剩（空的）build 根目录
    assert list(build_root.iterdir()) == []


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_inputs(tmp_path) -> DeliveryInputs:
    inputs, _ = _make_valid_inputs(tmp_path)
    return inputs
