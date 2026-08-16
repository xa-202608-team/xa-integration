# -*- coding: utf-8 -*-
"""精确导出（tools/export_component.py）的行为测试。

用 tmp_path 内的合成 git 仓库（含 annotated tag 与多提交）验证：
- Tag 解析结果必须等于 lock commit；
- `git archive` 从提交对象导出，脏工作树不影响内容；
- 恶意 tar 归档（绝对路径 / .. / symlink / hardlink / 设备文件）在写任何
  文件之前被拒绝。
"""
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.export_component import (  # noqa: E402
    export_commit,
    export_component,
    safe_extract_tar,
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


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """合成组件仓库：v1 提交 -> tag v1；v2 提交 -> tag v2（annotated）。"""
    root = tmp_path / "xa-wheel-fixture"
    root.mkdir()
    _init_git(root)
    (root / "README.md").write_text("wheel v1\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "train.py").write_text("SEED = 42  # v1\n", encoding="utf-8")
    _commit_all(root, "v1 skeleton")
    _git(root, "tag", "-a", "wheel-v0.1.0-rc.1", "-m", "rc1", "HEAD")

    (root / "src" / "train.py").write_text("SEED = 2026  # v2\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("v2 changes\n", encoding="utf-8")
    _commit_all(root, "v2 update")
    _git(root, "tag", "-a", "wheel-v0.2.0-rc.1", "-m", "rc1", "HEAD")

    commit_v1 = _git(root, "rev-list", "-n", "1", "wheel-v0.1.0-rc.1")
    commit_v2 = _git(root, "rev-list", "-n", "1", "wheel-v0.2.0-rc.1")
    return root, commit_v1, commit_v2


# ---------------------------------------------------------------------------
# 正例：按提交对象精确导出
# ---------------------------------------------------------------------------


def test_export_commit_extracts_tree_at_pinned_commit(tmp_path) -> None:
    root, commit_v1, _ = _make_repo(tmp_path)
    destination = tmp_path / "out-v1"
    result = export_commit(root, commit_v1, destination)
    assert result == destination
    assert (destination / "README.md").read_text(encoding="utf-8") == "wheel v1\n"
    assert (destination / "src" / "train.py").read_text(encoding="utf-8") == "SEED = 42  # v1\n"
    assert not (destination / "CHANGELOG.md").exists()  # v2 的文件不在 v1 树里


def test_export_component_accepts_tag_resolving_to_lock_commit(tmp_path) -> None:
    root, _, commit_v2 = _make_repo(tmp_path)
    destination = tmp_path / "out-tag"
    export_component(root, "wheel-v0.2.0-rc.1", commit_v2, destination)
    assert (destination / "CHANGELOG.md").is_file()
    assert (destination / "src" / "train.py").read_text(encoding="utf-8") == "SEED = 2026  # v2\n"


def test_export_result_has_no_git_directory(tmp_path) -> None:
    root, _, commit_v2 = _make_repo(tmp_path)
    destination = tmp_path / "out-nogit"
    export_component(root, "wheel-v0.2.0-rc.1", commit_v2, destination)
    assert not (destination / ".git").exists()
    assert not any(p.name == ".git" for p in destination.rglob("*"))


def test_dirty_worktree_does_not_change_archive_content(tmp_path) -> None:
    """脏工作树不影响 git archive 内容：导出永远来自提交对象。"""
    root, _, commit_v2 = _make_repo(tmp_path)
    clean_out = tmp_path / "out-clean"
    export_component(root, "wheel-v0.2.0-rc.1", commit_v2, clean_out)

    # 弄脏工作树：改已跟踪文件 + 加未跟踪文件
    (root / "README.md").write_text("DIRTY\n", encoding="utf-8")
    (root / "src" / "train.py").write_text("HACKED\n", encoding="utf-8")
    (root / "untracked.txt").write_text("noise\n", encoding="utf-8")

    dirty_out = tmp_path / "out-dirty"
    export_component(root, "wheel-v0.2.0-rc.1", commit_v2, dirty_out)
    assert (dirty_out / "README.md").read_text(encoding="utf-8") == "wheel v1\n"
    assert (dirty_out / "src" / "train.py").read_text(encoding="utf-8") == "SEED = 2026  # v2\n"
    assert not (dirty_out / "untracked.txt").exists()
    # 与干净导出逐字节一致
    for name in ("README.md", "src/train.py", "CHANGELOG.md"):
        assert (clean_out / name).read_bytes() == (dirty_out / name).read_bytes()


# ---------------------------------------------------------------------------
# 拒绝条件：Tag 指向错误提交 / 提交不存在 / 目标非空
# ---------------------------------------------------------------------------


def test_tag_resolving_to_wrong_commit_is_rejected(tmp_path) -> None:
    root, commit_v1, commit_v2 = _make_repo(tmp_path)
    # lock 记录 v2 提交，但 tag v1 实际解析到 v1 提交 -> 拒绝
    with pytest.raises(ValueError, match="tag"):
        export_component(root, "wheel-v0.1.0-rc.1", commit_v2, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unknown_tag_is_rejected(tmp_path) -> None:
    root, _, commit_v2 = _make_repo(tmp_path)
    with pytest.raises(ValueError):
        export_component(root, "no-such-tag", commit_v2, tmp_path / "out")


def test_unknown_commit_is_rejected(tmp_path) -> None:
    root, _, _ = _make_repo(tmp_path)
    with pytest.raises(ValueError, match="commit"):
        export_commit(root, "f" * 40, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_short_commit_is_rejected_before_git(tmp_path) -> None:
    root, _, commit_v2 = _make_repo(tmp_path)
    with pytest.raises(ValueError, match="40"):
        export_commit(root, commit_v2[:8], tmp_path / "out")


def test_nonempty_destination_is_rejected(tmp_path) -> None:
    root, _, commit_v2 = _make_repo(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="new and empty"):
        export_commit(root, commit_v2, destination)
    assert (destination / "existing.txt").read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------------------
# 恶意归档：在写任何文件之前被拒绝（safe_extract_tar 两阶段）
# ---------------------------------------------------------------------------


def _make_tar(path: Path, builder) -> Path:
    with tarfile.open(path, "w") as tf:
        builder(tf)
    return path


def _add_reg(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    import io

    tf.addfile(info, io.BytesIO(data))


def test_safe_extract_rejects_absolute_path_member(tmp_path) -> None:
    tar_path = _make_tar(
        tmp_path / "evil.tar", lambda tf: _add_reg(tf, "/etc/evil.txt", b"evil")
    )
    destination = tmp_path / "dest"
    with pytest.raises(ValueError, match="absolute"):
        safe_extract_tar(tar_path, destination)
    assert not destination.exists() or not any(destination.iterdir())


def test_safe_extract_rejects_dotdot_member(tmp_path) -> None:
    tar_path = _make_tar(
        tmp_path / "evil.tar", lambda tf: _add_reg(tf, "../escape.txt", b"evil")
    )
    destination = tmp_path / "dest"
    with pytest.raises(ValueError, match=r"\.\."):
        safe_extract_tar(tar_path, destination)
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_backslash_member(tmp_path) -> None:
    tar_path = _make_tar(
        tmp_path / "evil.tar", lambda tf: _add_reg(tf, "..\\evil-win.txt", b"evil")
    )
    destination = tmp_path / "dest"
    with pytest.raises(ValueError):
        safe_extract_tar(tar_path, destination)
    assert not (tmp_path / "evil-win.txt").exists()


def test_safe_extract_rejects_symlink_member(tmp_path) -> None:
    def build(tf: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("innocent.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        info.size = 0
        tf.addfile(info)

    tar_path = _make_tar(tmp_path / "evil.tar", build)
    destination = tmp_path / "dest"
    with pytest.raises(ValueError, match="symbolic link"):
        safe_extract_tar(tar_path, destination)
    assert not destination.exists() or not any(destination.iterdir())


def test_safe_extract_rejects_hardlink_member(tmp_path) -> None:
    def build(tf: tarfile.TarFile) -> None:
        _add_reg(tf, "target.txt", b"data")
        info = tarfile.TarInfo("hardlink.txt")
        info.type = tarfile.LNKTYPE
        info.linkname = "target.txt"
        info.size = 0
        tf.addfile(info)

    tar_path = _make_tar(tmp_path / "evil.tar", build)
    destination = tmp_path / "dest"
    with pytest.raises(ValueError, match="hard link"):
        safe_extract_tar(tar_path, destination)
    # 两阶段校验：连合法成员 target.txt 也没有被写入
    assert not destination.exists() or not any(destination.iterdir())


def test_safe_extract_rejects_device_member(tmp_path) -> None:
    def build(tf: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("dev/zero")
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 5
        info.size = 0
        tf.addfile(info)

    tar_path = _make_tar(tmp_path / "evil.tar", build)
    destination = tmp_path / "dest"
    with pytest.raises(ValueError, match="device"):
        safe_extract_tar(tar_path, destination)
    assert not destination.exists() or not any(destination.iterdir())


def test_malicious_archive_rejected_before_any_file_is_written(tmp_path) -> None:
    """恶意成员与合法成员混排：校验阶段整体拒绝，合法成员也零写入。"""

    def build(tf: tarfile.TarFile) -> None:
        _add_reg(tf, "good-1.txt", b"good")
        _add_reg(tf, "nested/good-2.txt", b"good")
        _add_reg(tf, "../evil-escape.txt", b"evil")  # 排在合法成员之后
        _add_reg(tf, "good-3.txt", b"good")

    tar_path = _make_tar(tmp_path / "mixed.tar", build)
    destination = tmp_path / "dest"
    with pytest.raises(ValueError):
        safe_extract_tar(tar_path, destination)
    assert not destination.exists() or not any(destination.iterdir())
    assert not (tmp_path / "evil-escape.txt").exists()


def test_safe_extract_accepts_plain_files_and_directories(tmp_path) -> None:
    def build(tf: tarfile.TarFile) -> None:
        _add_reg(tf, "README.md", b"hello\n")
        dirinfo = tarfile.TarInfo("src")
        dirinfo.type = tarfile.DIRTYPE
        tf.addfile(dirinfo)
        _add_reg(tf, "src/train.py", b"print(1)\n")

    tar_path = _make_tar(tmp_path / "plain.tar", build)
    destination = tmp_path / "dest"
    safe_extract_tar(tar_path, destination)
    assert (destination / "README.md").read_bytes() == b"hello\n"
    assert (destination / "src" / "train.py").read_bytes() == b"print(1)\n"
    assert (destination / "src").is_dir()


def test_safe_extract_rejects_nonempty_destination(tmp_path) -> None:
    def build(tf: tarfile.TarFile) -> None:
        _add_reg(tf, "README.md", b"hello\n")

    tar_path = _make_tar(tmp_path / "plain.tar", build)
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "user.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="new and empty"):
        safe_extract_tar(tar_path, destination)
    assert (destination / "user.txt").read_text(encoding="utf-8") == "keep"
