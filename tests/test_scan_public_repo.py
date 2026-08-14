# -*- coding: utf-8 -*-
"""公开泄漏扫描器（tools/scan_public_repo.py）的行为测试。

测试中的假密钥一律用字符串拼接构造，避免测试文件自身被内容规则命中。
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from tools.scan_public_repo import (  # noqa: E402
    SLOT_DESC_FILES,
    Violation,
    main,
    scan_tree,
)


def _aws_key() -> str:
    # 假 AWS 访问密钥（拼接构造，避免本文件出现完整字面量）
    return "AKIA" + "IOSFODNN7EXAMPLE"


def _github_token() -> str:
    return "ghp_" + "abcdefghij0123456789abcdefghij012345"


def _private_key_block() -> str:
    # 拼接构造，避免本文件出现完整私钥标记字面量
    return (
        "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----\n"
    )


def _internal_path() -> str:
    # 拼接构造，避免本文件出现完整内部绝对路径字面量
    return "D:" + "/Cheng/PhD/private/path"


def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)


def _stage(root: Path, rel: str) -> None:
    subprocess.run(["git", "add", rel], cwd=root, check=True)


def _rules(violations):
    return {v.rule for v in violations}


def _find(violations, rule):
    return next((v for v in violations if v.rule == rule), None)


# ---------------------------------------------------------------------------
# 干净仓库
# ---------------------------------------------------------------------------


def test_clean_repo_has_no_violations(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    _stage(tmp_path, "src/main.py")
    assert scan_tree(tmp_path) == []


# ---------------------------------------------------------------------------
# 文件级规则：.env / 私有目录 / 产物目录 / 模型二进制 / pycache
# ---------------------------------------------------------------------------


def test_env_file_is_critical(tmp_path):
    _init_git(tmp_path)
    content = b"K=v\n"
    (tmp_path / ".env").write_bytes(content)
    violations = scan_tree(tmp_path)
    v = _find(violations, "env-secret-file")
    assert v is not None
    assert v.severity == "CRITICAL"
    assert v.relative == Path(".env")
    assert v.size == len(content)
    assert v.origin in {"worktree", "git-index"}


def test_tracked_private_dir_is_critical(tmp_path):
    _init_git(tmp_path)
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "notes.md").write_text("x\n", encoding="utf-8")
    _stage(tmp_path, ".private/notes.md")
    violations = scan_tree(tmp_path)
    assert any(v.rule == "private-dir" and v.severity == "CRITICAL" for v in violations)
    assert any(v.origin == "git-index" for v in violations if v.rule == "private-dir")


def test_ignored_private_dir_not_flagged(tmp_path):
    # .private/ 已被 .gitignore 忽略且未 tracked → 不构成公开泄漏
    _init_git(tmp_path)
    (tmp_path / ".gitignore").write_text(".private/\n", encoding="utf-8")
    _stage(tmp_path, ".gitignore")
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "local.md").write_text("x\n", encoding="utf-8")
    assert scan_tree(tmp_path) == []


def test_data_artifact_dirs_are_critical(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "raw.csv").write_text("v\n", encoding="utf-8")
    _stage(tmp_path, "data/raw.csv")
    violations = scan_tree(tmp_path)
    assert _find(violations, "artifact-data-dir") is not None
    assert _find(violations, "artifact-data-dir").severity == "CRITICAL"


def test_model_binary_is_high(tmp_path):
    # 放在产物目录之外，单独验证模型二进制规则（产物目录规则会先短路）
    _init_git(tmp_path)
    (tmp_path / "packaged").mkdir()
    (tmp_path / "packaged" / "model.ckpt").write_bytes(b"\x00" * 16)
    violations = scan_tree(tmp_path)
    v = _find(violations, "model-binary")
    assert v is not None
    assert v.severity == "HIGH"
    assert v.size == 16


def test_pycache_is_medium_and_not_severe(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "src" / "__pycache__").mkdir(parents=True)
    (tmp_path / "src" / "__pycache__" / "x.pyc").write_bytes(b"\x00pyc")
    _stage(tmp_path, "src/__pycache__/x.pyc")
    violations = scan_tree(tmp_path)
    v = _find(violations, "pycache-artifact")
    assert v is not None
    assert v.severity == "MEDIUM"
    assert main(["--root", str(tmp_path)]) == 0  # 非严重项不导致退出 1


# ---------------------------------------------------------------------------
# 内容级规则：秘密与内部路径；秘密命中只给规则名
# ---------------------------------------------------------------------------


def test_content_secrets_detected_by_rule_name_only(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "settings.py").write_text(
        "AWS = " + repr(_aws_key()) + "\nGH = " + repr(_github_token()) + "\n",
        encoding="utf-8",
    )
    violations = scan_tree(tmp_path)
    rules = _rules(violations)
    assert "aws-access-key" in rules
    assert "github-token" in rules
    for v in violations:
        # 秘密命中只给规则名：不得携带命中的明文内容
        assert v.detail is None
        rendered = repr(v)
        assert _aws_key() not in rendered
        assert _github_token() not in rendered


def test_private_key_content_is_critical(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "id_rsa").write_text(_private_key_block(), encoding="utf-8")
    violations = scan_tree(tmp_path)
    v = _find(violations, "private-key-content")
    assert v is not None and v.severity == "CRITICAL"


def test_internal_absolute_path_is_high(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "run.py").write_text(
        "SOURCE = " + repr(_internal_path()) + "\n", encoding="utf-8"
    )
    violations = scan_tree(tmp_path)
    v = _find(violations, "internal-absolute-path")
    assert v is not None and v.severity == "HIGH"


def test_worktree_only_secret_flagged_with_worktree_origin(tmp_path):
    _init_git(tmp_path)
    (tmp_path / ".env").write_text("K=v\n", encoding="utf-8")  # 未 git add
    violations = scan_tree(tmp_path)
    v = _find(violations, "env-secret-file")
    assert v is not None
    assert v.origin == "worktree"


def test_tracked_file_reported_with_git_index_origin(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "keys.py").write_text("K = " + repr(_aws_key()) + "\n", encoding="utf-8")
    _stage(tmp_path, "keys.py")
    v = _find(scan_tree(tmp_path), "aws-access-key")
    assert v is not None
    assert v.origin == "git-index"


# ---------------------------------------------------------------------------
# 允许列表：只含设计批准的槽位描述文件与 fixture
# ---------------------------------------------------------------------------


def test_fixture_fake_secret_is_allowed(tmp_path):
    _init_git(tmp_path)
    fixture_dir = tmp_path / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "fake_credentials.txt").write_text(_aws_key() + "\n", encoding="utf-8")
    _stage(tmp_path, "tests/fixtures/fake_credentials.txt")
    assert scan_tree(tmp_path) == []


def test_slot_schema_fake_secret_is_allowed(tmp_path):
    _init_git(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "owners.schema.json").write_text(
        '{"example_token": ' + repr(_github_token()) + "}\n", encoding="utf-8"
    )
    _stage(tmp_path, "config/owners.schema.json")
    assert scan_tree(tmp_path) == []


def test_fixture_env_file_still_flagged(tmp_path):
    # 允许列表只豁免内容规则，不豁免文件名规则 —— 真实 .env 不得藏进 fixture
    _init_git(tmp_path)
    fixture_dir = tmp_path / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / ".env").write_text("K=v\n", encoding="utf-8")
    _stage(tmp_path, "tests/fixtures/.env")
    assert _find(scan_tree(tmp_path), "env-secret-file") is not None


def test_allowlist_does_not_cover_real_data(tmp_path):
    # data/ 下即便叫 fixture 也不豁免产物规则
    _init_git(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fixture.csv").write_text("v\n", encoding="utf-8")
    _stage(tmp_path, "data/fixture.csv")
    assert _find(scan_tree(tmp_path), "artifact-data-dir") is not None


# ---------------------------------------------------------------------------
# 槽位描述文件豁免：仅免于 artifact-data-dir 目录规则，内容规则仍然生效
# ---------------------------------------------------------------------------

# 设计批准的 7 个槽位描述文件（与组件仓库边界测试的批准集合一致）
SLOT_DESC_FILES_EXPECTED = (
    "data/README.md",
    "data/data_manifest.json",
    "results/README.md",
    "results/public_summary.json",
    "results/expected_metrics.json",
    "checkpoints/README.md",
    "checkpoints/checkpoint_manifest.json",
)


def test_slot_desc_file_set_matches_design():
    # 锁定豁免集合与设计批准集合一致（双向：不多、不少）
    assert SLOT_DESC_FILES == frozenset(SLOT_DESC_FILES_EXPECTED)


def test_tracked_slot_desc_files_not_critical(tmp_path):
    # (a) 7 个槽位描述文件 tracked 不再触发 artifact-data-dir CRITICAL
    _init_git(tmp_path)
    for rel in SLOT_DESC_FILES_EXPECTED:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# slot\n", encoding="utf-8")
        _stage(tmp_path, rel)
    violations = scan_tree(tmp_path)
    assert not any(
        v.rule == "artifact-data-dir"
        and v.relative.as_posix() in SLOT_DESC_FILES_EXPECTED
        for v in violations
    )
    assert main(["--root", str(tmp_path)]) == 0


def test_slot_desc_file_with_token_still_critical(tmp_path):
    # (b) 豁免不覆盖内容规则：槽位文件内含 Token 仍报 CRITICAL
    _init_git(tmp_path)
    f = tmp_path / "data" / "README.md"
    f.parent.mkdir(parents=True)
    f.write_text("example = " + repr(_github_token()) + "\n", encoding="utf-8")
    _stage(tmp_path, "data/README.md")
    v = _find(scan_tree(tmp_path), "github-token")
    assert v is not None and v.severity == "CRITICAL"
    assert main(["--root", str(tmp_path)]) == 1


def test_non_slot_file_in_data_dir_still_critical(tmp_path):
    # (c) 豁免仅限批准路径：data/other.md 仍触发 artifact-data-dir
    _init_git(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "other.md").write_text("x\n", encoding="utf-8")
    _stage(tmp_path, "data/other.md")
    v = _find(scan_tree(tmp_path), "artifact-data-dir")
    assert v is not None and v.severity == "CRITICAL"


# ---------------------------------------------------------------------------
# 退出码与输出
# ---------------------------------------------------------------------------


def test_severe_violation_exits_1(tmp_path, capsys):
    _init_git(tmp_path)
    (tmp_path / ".env").write_text("K=v\n", encoding="utf-8")
    assert main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "env-secret-file" in out
    assert ".env" in out
    assert "CRITICAL" in out
    assert "PASSWORD" not in out and "K=v" not in out  # 不输出文件内容


def test_clean_repo_exits_0(tmp_path, capsys):
    _init_git(tmp_path)
    (tmp_path / "README.md").write_text("# ok\n", encoding="utf-8")
    _stage(tmp_path, "README.md")
    assert main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "violation" in out.lower()


def test_violation_dataclass_fields(tmp_path):
    # 接口契约：Violation 必须可输出规则 / 相对路径 / 大小
    _init_git(tmp_path)
    content = b"K=v\n"
    (tmp_path / ".env").write_bytes(content)
    v = _find(scan_tree(tmp_path), "env-secret-file")
    assert isinstance(v, Violation)
    assert v.rule == "env-secret-file"
    assert v.relative == Path(".env")
    assert v.size == len(content)
