# -*- coding: utf-8 -*-
"""白名单快照导入器（tools/import_component_snapshot.py）的行为测试。

夹具布局与核心断言与任务简报 Step 1 逐字对齐：
源目录（名为 src）下创建 src/model.py、data/raw.csv、__pycache__/x.pyc、
.env、tests/fixtures/tiny.csv，复制计划只包含源码与 tiny fixture。
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402
import yaml  # noqa: E402

from tools.import_component_snapshot import (  # noqa: E402
    build_audit_report,
    build_copy_plan,
    ensure_within_destination,
    load_policy,
    match_glob,
)

# 与 config/snapshot-policy.yaml（简报 Step 3）逐字一致的策略结构。
POLICY = {
    "version": 1,
    "common_allow": [
        "src/**",
        "configs/**",
        "scripts/**",
        "tests/**",
        "docs/**",
        "Dockerfile",
        ".dockerignore",
        "requirements*.txt",
        "requirements*.lock",
        "pytest.ini",
        "README*.md",
        "RUN.md",
        "INPUT_SCHEMA.md",
        "PROVENANCE.md",
        "DATA_REQUIREMENTS.md",
    ],
    "common_deny": [
        "**/__pycache__/**",
        "**/.pytest_cache/**",
        "**/*.pyc",
        "**/*.log",
        "data/**",
        "results/**",
        "checkpoints/**",
        "**/*.pt",
        "**/*.pth",
        "**/*.ckpt",
    ],
    "components": {
        "battery": {
            "allow": ["reference/**"],
            "deny": ["reference/data/**", "reference/models/**", "reference/results/**"],
        },
        "phased_array": {"allow": ["pa_data_flow.png"], "deny": []},
        "wheel": {
            "allow": ["release/**", "entrypoint.sh", "requirements.unified.lock", "PROGRESS.md"],
            "deny": [
                "release/data/**",
                "release/checkpoints/**",
                "release/results/**",
                "release/docs/handoff/project_inventory.json",
                "release/docs/handoff/project_inventory.csv",
            ],
        },
    },
}


def make_source(tmp_path: Path) -> Path:
    """按简报 Step 1 构造只读源目录（目录名 src，位于 tmp_path 下）。"""
    source = tmp_path / "src"
    (source / "src").mkdir(parents=True)
    (source / "data").mkdir()
    (source / "__pycache__").mkdir()
    (source / "tests" / "fixtures").mkdir(parents=True)
    (source / "src" / "model.py").write_text("print('model')\n", encoding="utf-8")
    (source / "data" / "raw.csv").write_text("v\n1\n", encoding="utf-8")
    (source / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01pyc")
    (source / ".env").write_text("PASSWORD=topsecret\n", encoding="utf-8")
    (source / "tests" / "fixtures" / "tiny.csv").write_text("a\n", encoding="utf-8")
    return source


# ---------------------------------------------------------------------------
# Step 1 核心断言（逐字）
# ---------------------------------------------------------------------------


def test_plan_whitelist_core(tmp_path):
    source = make_source(tmp_path)
    destination = tmp_path / "dest"
    policy = POLICY
    plan = build_copy_plan(source, destination, "battery", policy)
    relative = {item.relative.as_posix() for item in plan}
    assert "src/model.py" in relative
    assert "tests/fixtures/tiny.csv" in relative
    assert "data/raw.csv" not in relative
    assert "__pycache__/x.pyc" not in relative
    assert ".env" not in relative


def test_plan_contains_only_source_and_tiny_fixture(tmp_path):
    source = make_source(tmp_path)
    plan = build_copy_plan(source, tmp_path / "dest", "battery", POLICY)
    relative = {item.relative.as_posix() for item in plan}
    assert relative == {"src/model.py", "tests/fixtures/tiny.csv"}


# ---------------------------------------------------------------------------
# 策略文件与匹配语义
# ---------------------------------------------------------------------------


def test_config_policy_matches_brief_spec():
    config_path = REPO_ROOT / "config" / "snapshot-policy.yaml"
    assert load_policy(config_path) == POLICY


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        # 根锚定：data/** 只匹配仓库根 data，不误伤 src/data/**
        ("data/**", "data/raw.csv", True),
        ("data/**", "src/data/raw.csv", False),
        ("results/**", "src/results/metrics.json", False),
        # **/ 前缀才允许匹配任意深度
        ("**/__pycache__/**", "__pycache__/x.pyc", True),
        ("**/__pycache__/**", "src/__pycache__/x.pyc", True),
        ("**/*.pyc", "a/b/c.pyc", True),
        ("**/*.log", "debug.log", True),
        # 根级普通规则不匹配子目录同名文件
        ("Dockerfile", "Dockerfile", True),
        ("Dockerfile", "src/Dockerfile", False),
        ("requirements*.txt", "requirements.txt", True),
        ("requirements*.lock", "requirements.unified.lock", True),
        ("requirements*.txt", "src/requirements.txt", False),
        # 目录递归
        ("src/**", "src/model.py", True),
        ("src/**", "src/a/b/deep.py", True),
        ("reference/**", "reference/data/x.csv", True),
        # deny 优先在 build_copy_plan 层测试，这里锁定 glob 引擎本身
        ("**/*.pt", "checkpoints/best.pt", True),
    ],
)
def test_match_glob_semantics(pattern, path, expected):
    assert match_glob(pattern, path) is expected


def test_deny_overrides_allow_for_same_path(tmp_path):
    # battery: reference/** allow，但 reference/data/** deny —— deny 永远优先
    source = tmp_path / "src"
    (source / "reference" / "data").mkdir(parents=True)
    (source / "reference" / "run.py").write_text("x=1\n", encoding="utf-8")
    (source / "reference" / "data" / "big.csv").write_text("v\n", encoding="utf-8")
    plan = build_copy_plan(source, tmp_path / "dest", "battery", POLICY)
    relative = {item.relative.as_posix() for item in plan}
    assert "reference/run.py" in relative
    assert "reference/data/big.csv" not in relative


def test_component_specific_allow(tmp_path):
    # phased_array 只额外放行 pa_data_flow.png，reference/** 不适用
    source = make_source(tmp_path)
    (source / "pa_data_flow.png").write_bytes(b"png")
    (source / "reference").mkdir()
    (source / "reference" / "a.py").write_text("x\n", encoding="utf-8")
    plan = build_copy_plan(source, tmp_path / "dest", "phased_array", POLICY)
    relative = {item.relative.as_posix() for item in plan}
    assert "pa_data_flow.png" in relative
    assert "reference/a.py" not in relative


def test_unknown_component_raises(tmp_path):
    source = make_source(tmp_path)
    with pytest.raises(ValueError):
        build_copy_plan(source, tmp_path / "dest", "unknown_component", POLICY)


# ---------------------------------------------------------------------------
# 目标路径安全：目标必须等于目标仓库根或其子路径，否则 ValueError；禁止清空目标根
# ---------------------------------------------------------------------------


def test_ensure_within_destination_accepts_subpaths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = ensure_within_destination(root, Path("src/model.py"))
    assert target == (root / "src" / "model.py").resolve()


@pytest.mark.parametrize(
    "bad_relative",
    [Path("."), Path(""), Path("../escape.py"), Path("a/../../escape.py"), Path("C:/Windows/system32/evil.py"), Path("/etc/passwd")],
)
def test_ensure_within_destination_rejects_escape_or_root(tmp_path, bad_relative):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError):
        ensure_within_destination(root, bad_relative)


def test_plan_rejects_escaping_relative_path(tmp_path, monkeypatch):
    # 即使源枚举被污染产生逃逸相对路径（先命中 allow 再向上跳出目标根），
    # build_copy_plan 也必须抛 ValueError
    import tools.import_component_snapshot as mod

    source = make_source(tmp_path)
    evil = [Path("src/../../evil.py")]
    monkeypatch.setattr(mod, "iter_source_files", lambda src: iter(evil))
    with pytest.raises(ValueError):
        build_copy_plan(source, tmp_path / "dest", "battery", POLICY)


def test_plan_rejects_destination_inside_source(tmp_path):
    source = make_source(tmp_path)
    with pytest.raises(ValueError):
        build_copy_plan(source, source / "dest", "battery", POLICY)


# ---------------------------------------------------------------------------
# dry-run / apply / 审计
# ---------------------------------------------------------------------------


def _run_cli(argv):
    from tools.import_component_snapshot import main

    return main(argv)


def test_cli_dry_run_writes_nothing_to_destination(tmp_path):
    from tools.import_component_snapshot import main

    source = make_source(tmp_path)
    destination = tmp_path / "dest"
    destination.mkdir()
    audit = tmp_path / "audit.json"
    rc = main(
        [
            "--component", "battery",
            "--source", str(source),
            "--destination", str(destination),
            "--policy", str(REPO_ROOT / "config" / "snapshot-policy.yaml"),
            "--audit-output", str(audit),
        ]
    )
    assert rc == 0
    assert list(destination.iterdir()) == []  # 无 --apply 时不得写目标
    report = json.loads(audit.read_text(encoding="utf-8"))
    assert report["applied"] is False
    assert report["component"] == "battery"


def test_cli_apply_copies_plan_and_preserves_governance(tmp_path):
    from tools.import_component_snapshot import main

    source = make_source(tmp_path)
    (source / "README.md").write_text("# NEW\n", encoding="utf-8")
    (source / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "README.md").write_text("# OLD\n", encoding="utf-8")
    (destination / "keep.txt").write_text("keep\n", encoding="utf-8")
    audit = tmp_path / "audit.json"
    rc = main(
        [
            "--component", "battery",
            "--source", str(source),
            "--destination", str(destination),
            "--policy", str(REPO_ROOT / "config" / "snapshot-policy.yaml"),
            "--audit-output", str(audit),
            "--apply",
        ]
    )
    assert rc == 0
    # 只创建/覆盖计划内普通工程文件
    assert (destination / "src" / "model.py").read_text(encoding="utf-8") == "print('model')\n"
    assert (destination / "tests" / "fixtures" / "tiny.csv").read_text(encoding="utf-8") == "a\n"
    assert (destination / "requirements.txt").read_text(encoding="utf-8") == "numpy\n"
    # 治理文件存在时 PRESERVED_EXISTING，禁止静默覆盖
    assert (destination / "README.md").read_text(encoding="utf-8") == "# OLD\n"
    # 不删除目标已有无关文件（不清空目标根）
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    report = json.loads(audit.read_text(encoding="utf-8"))
    assert report["applied"] is True
    statuses = {item["relative"]: item["status"] for item in report["items"]}
    assert statuses["README.md"] == "PRESERVED_EXISTING"
    assert statuses["src/model.py"] == "COPIED"
    # 被拒绝项记录原因（deny 规则名）
    skipped = {item["relative"]: item for item in report["items"] if item["status"] == "SKIPPED"}
    assert skipped["data/raw.csv"]["detail"].startswith("DENIED:")
    assert skipped[".env"]["status"] == "SKIPPED"


def test_audit_json_is_deterministic(tmp_path):
    from tools.import_component_snapshot import apply_plan, write_audit

    source = make_source(tmp_path)
    destination = tmp_path / "dest"
    destination.mkdir()
    plan = build_copy_plan(source, destination, "battery", POLICY)
    results = apply_plan(plan, destination)
    report1 = build_audit_report(source, destination, "battery", POLICY, plan, results, applied=True)
    report2 = build_audit_report(source, destination, "battery", POLICY, plan, results, applied=True)
    p1 = tmp_path / "a1.json"
    p2 = tmp_path / "a2.json"
    write_audit(p1, report1)
    write_audit(p2, report2)
    assert p1.read_bytes() == p2.read_bytes()
    # 列表按相对路径排序
    relatives = [item["relative"] for item in report1["items"]]
    assert relatives == sorted(relatives)


def test_build_audit_report_totals(tmp_path):
    from tools.import_component_snapshot import build_audit_report

    source = make_source(tmp_path)
    destination = tmp_path / "dest"
    plan = build_copy_plan(source, destination, "battery", POLICY)
    results = [{"relative": item.relative.as_posix(), "status": "PLANNED"} for item in plan]
    report = build_audit_report(source, destination, "battery", POLICY, plan, results, applied=False)
    totals = report["totals"]
    assert totals["planned_or_copied"] == len(plan)
    assert totals["skipped"] >= 3  # data/raw.csv、__pycache__/x.pyc、.env
