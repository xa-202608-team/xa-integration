# -*- coding: utf-8 -*-
"""精确导出：按版本锁把组件仓库固定提交导出为干净源码树。

流程（任务简报 Step 3）：
1. ``git -C $repo cat-file -e $commit^{commit}`` —— 提交必须存在于仓库对象库；
2. ``git -C $repo rev-list -n 1 $tag`` —— Tag 解析结果必须**等于** lock 记录
   的 commit（防 Tag 被移动/重打）；
3. ``git -C $repo archive --format=tar --output $tmp $commit`` —— 从提交对象
   归档（脏工作树不影响内容）；
4. Python ``tarfile`` 安全提取：**两阶段**（先校验全部成员、后写文件），
   拒绝绝对路径、``..``、反斜杠、盘符、symlink、hardlink、设备/FIFO 成员，
   以及任何 ``.git`` 路径段；
5. 导出后断言没有 ``.git``。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional

try:
    from .artifact_common import is_full_commit, normalize_member
except ImportError:  # pragma: no cover - 以 `python tools/export_component.py` 运行
    from artifact_common import is_full_commit, normalize_member  # type: ignore

#: tar 成员里允许的文件类型（普通文件 / 目录）
_ALLOWED_TYPES = frozenset({tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE})


def _git(repo_root: Path, *args: str) -> str:
    """在仓库内执行 git 命令，失败抛 RuntimeError（带 stderr 摘要）。"""
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


def resolve_tag_commit(repo_root: Path, tag: str) -> str:
    """``git rev-list -n 1 $tag``：Tag 解析到的完整提交哈希。"""
    resolved = _git(Path(repo_root), "rev-list", "-n", "1", tag)
    if not is_full_commit(resolved):
        raise ValueError(f"tag {tag!r} did not resolve to a full commit: {resolved!r}")
    return resolved


def _check_destination(destination: Path) -> None:
    """提取目标必须不存在或为空目录。"""
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError(f"destination must be new and empty: {destination}")


def _validate_member(info: tarfile.TarInfo, destination: Path) -> PurePosixPath:
    """校验单个 tar 成员（不写任何文件），返回归一化相对路径。"""
    name = info.name
    if name.startswith("/") or name.startswith("\\"):
        raise ValueError(f"absolute path in archive is forbidden: {name!r}")
    try:
        rel = normalize_member(name.rstrip("/"))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if info.type not in _ALLOWED_TYPES:
        if info.issym():
            raise ValueError(f"symbolic link member is forbidden: {name!r}")
        if info.islnk():
            raise ValueError(f"hard link member is forbidden: {name!r}")
        if info.ischr() or info.isblk():
            raise ValueError(f"device file member is forbidden: {name!r}")
        if info.isfifo():
            raise ValueError(f"FIFO member is forbidden: {name!r}")
        raise ValueError(f"unsupported archive member type: {name!r} ({info.type!r})")
    if ".git" in rel.parts:
        raise ValueError(f".git path segment in archive is forbidden: {name!r}")
    # 纵深防御：resolved 目标必须落在提取根内
    target = destination / rel
    root_resolved = destination.resolve()
    if root_resolved not in target.resolve().parents and target.resolve() != root_resolved:
        raise ValueError(f"member destination escapes extract root: {name!r}")
    return rel


def safe_extract_tar(tar_path: Path, destination: Path) -> Path:
    """安全提取 tar：先校验**全部**成员再写文件（恶意归档零写入）。"""
    tar_path = Path(tar_path)
    destination = Path(destination)
    if not tar_path.is_file():
        raise FileNotFoundError(f"archive not found: {tar_path}")
    _check_destination(destination)

    with tarfile.open(tar_path, "r:") as tf:
        # 阶段 1：全量校验（此阶段绝不写文件）
        members = tf.getmembers()
        validated: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for info in members:
            validated.append((info, _validate_member(info, destination)))

        # 阶段 2：全部合法才落地
        destination.mkdir(parents=True, exist_ok=True)
        for info, rel in validated:
            target = destination / rel
            if info.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(info)
            if source is None:  # pragma: no cover - 类型白名单已排除
                raise ValueError(f"cannot extract member content: {info.name!r}")
            with source, target.open("wb") as dst:
                shutil.copyfileobj(source, dst, length=1024 * 1024)
    return destination


def export_commit(repo_root: Path, commit: str, destination: Path) -> Path:
    """把 ``commit`` 的树导出到 ``destination``（不涉及 Tag）。"""
    repo_root = Path(repo_root)
    destination = Path(destination)
    if not is_full_commit(commit):
        raise ValueError(f"commit must be 40 lowercase hex chars: {commit!r}")
    if not repo_root.is_dir():
        raise ValueError(f"repo_root is not a directory: {repo_root}")
    _check_destination(destination)

    # 提交必须存在于对象库
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"failed to run git: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"commit not found in {repo_root}: {commit}")

    with tempfile.TemporaryDirectory(prefix="xa-export-") as tmp:
        tar_path = Path(tmp) / "export.tar"
        _git(repo_root, "archive", "--format=tar", f"--output={tar_path}", commit)
        safe_extract_tar(tar_path, destination)

    # 导出结果绝不含 .git（git archive 本不打包；此处为显式断言）
    if (destination / ".git").exists() or any(
        p.name == ".git" for p in destination.rglob("*")
    ):
        raise ValueError(f"exported tree must not contain .git: {destination}")
    return destination


def export_component(
    repo_root: Path, tag: str, commit: str, destination: Path
) -> Path:
    """按版本锁导出：Tag 必须解析到 lock 记录的 commit，再导出该提交。"""
    if not isinstance(tag, str) or not tag:
        raise ValueError(f"tag must be a non-empty string: {tag!r}")
    try:
        resolved = resolve_tag_commit(Path(repo_root), tag)
    except RuntimeError as exc:
        raise ValueError(f"failed to resolve tag {tag!r}: {exc}") from exc
    if resolved != commit:
        raise ValueError(
            f"tag {tag!r} resolves to commit {resolved}, but the lock pins {commit}; "
            "refusing to export (tag moved?)"
        )
    return export_commit(repo_root, commit, destination)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Export a component repo at a pinned commit (tag must resolve to it)."
    )
    parser.add_argument("--repo-root", required=True, type=Path, help="组件仓库根目录")
    parser.add_argument("--tag", required=True, help="版本锁记录的精确 Tag")
    parser.add_argument("--commit", required=True, help="版本锁记录的 40 位提交哈希")
    parser.add_argument("--destination", required=True, type=Path, help="导出目标（须为空）")
    args = parser.parse_args(argv)

    try:
        export_component(args.repo_root, args.tag, args.commit, args.destination)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"export FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"export OK: {args.tag} @ {args.commit[:8]} -> {args.destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
