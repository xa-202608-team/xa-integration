# -*- coding: utf-8 -*-
"""公开泄漏扫描器：检查目标仓库工作树与 git 索引，防止私有内容进入公开仓库。

设计约束（与任务简报对齐）：
- 同时检查目标工作树与 ``git ls-files``（已跟踪文件优先记为 git-index 来源）；
- 每条违规输出：规则、相对路径、文件大小；秘密命中只给规则名，
  绝不输出命中的明文内容；
- 检测到任何严重项（CRITICAL/HIGH）时退出码 1；
- 允许列表只包含设计批准的槽位描述文件（config/*.schema.json）与测试
  fixture（tests/fixtures/**），且仅豁免内容类规则，不豁免文件名/产物规则；
- 被 .gitignore 忽略且未跟踪的文件不会进入公开仓库，工作树扫描跳过；
  已跟踪文件不受 .gitignore 影响，仍按 git-index 检查。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 复用导入器的 glob 语义：包内导入（tests）优先，脚本直接运行时退回同目录导入。
try:
    from .import_component_snapshot import match_glob  # type: ignore
except ImportError:  # pragma: no cover - 以 `python tools/scan_public_repo.py` 运行
    from import_component_snapshot import match_glob  # type: ignore

SEVERE_SEVERITIES = ("CRITICAL", "HIGH")
MAX_CONTENT_SCAN_BYTES = 2_000_000

# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------

# 路径规则：(规则名, glob 模式元组, 严重级)；不可被允许列表豁免。
PATH_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("private-dir", ("**/.private/**",), "CRITICAL"),
    ("env-secret-file", ("**/.env", "**/.env.*"), "CRITICAL"),
    (
        "artifact-data-dir",
        ("data/**", "results/**", "checkpoints/**", "components/**", "build/**", "outputs/**"),
        "CRITICAL",
    ),
    (
        "model-binary",
        ("**/*.pt", "**/*.pth", "**/*.ckpt", "**/*.pkl", "**/*.h5", "**/*.onnx"),
        "HIGH",
    ),
    (
        "pycache-artifact",
        ("**/__pycache__/**", "**/.pytest_cache/**", "**/*.pyc"),
        "MEDIUM",
    ),
)

# 内容规则：(规则名, 编译正则, 严重级)；命中只报规则名，不报明文。
CONTENT_RULES: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    (
        "private-key-content",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "CRITICAL",
    ),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}"), "CRITICAL"),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{36}"), "CRITICAL"),
    ("openai-api-key", re.compile(r"sk-[A-Za-z0-9]{20,}"), "CRITICAL"),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "CRITICAL"),
    (
        "internal-absolute-path",
        re.compile(r"[D-F]:[/\\]Cheng[/\\]"),
        "HIGH",
    ),
)

# 允许列表：只含设计批准的槽位描述文件与测试 fixture，仅豁免内容规则。
ALLOWED_EXCEPTION_PATTERNS: tuple[str, ...] = (
    "tests/fixtures/**",   # 设计批准的测试 fixture（微型、无真实数据）
    "config/*.schema.json",  # 设计批准的槽位描述文件（JSON Schema）
)


@dataclass(frozen=True)
class Violation:
    """一条公开泄漏违规：规则 / 相对路径 / 大小 / 严重级 / 来源。

    秘密类命中只给规则名，detail 恒为 None，不携带明文。
    """

    rule: str
    relative: Path
    size: int
    severity: str
    origin: str  # "worktree" | "git-index"
    detail: Optional[str] = None


def _is_allowed_exception(rel_posix: str) -> bool:
    return any(match_glob(p, rel_posix) for p in ALLOWED_EXCEPTION_PATTERNS)


# ---------------------------------------------------------------------------
# git 集成与 .gitignore（简化语义）
# ---------------------------------------------------------------------------


def _git_ls_files(root: Path) -> set[str]:
    """返回 git 索引中的 POSIX 相对路径集合；非 git 仓库时为空集合。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return set()
    if proc.returncode != 0:
        return set()
    return {
        p.decode("utf-8", errors="replace")
        for p in proc.stdout.split(b"\x00")
        if p
    }


def _load_gitignore_patterns(root: Path) -> list[tuple[str, bool]]:
    """读取 .gitignore（忽略注释与取反行），返回 (规则, 是否仅目录)。"""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return []
    patterns = []
    for line in gitignore.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        dir_only = stripped.endswith("/")
        core = stripped.rstrip("/")
        if core:
            patterns.append((core, dir_only))
    return patterns


def _compile_gitignore(patterns: list[tuple[str, bool]]) -> list[str]:
    """把 .gitignore 行编译为本工具的 glob 语义。

    无 ``/`` 的规则匹配任意深度；含 ``/`` 的规则锚定根；目录规则传播到其下全部。
    """
    compiled: list[str] = []
    for core, dir_only in patterns:
        anchored = "/" in core
        if dir_only:
            compiled.append(f"{core}/**" if anchored else f"**/{core}/**")
        else:
            compiled.append(core if anchored else f"**/{core}")
            compiled.append(f"{core}/**" if anchored else f"**/{core}/**")
    return compiled


def _is_ignored(rel_posix: str, compiled: list[str]) -> bool:
    return any(match_glob(p, rel_posix) for p in compiled)


# ---------------------------------------------------------------------------
# 扫描核心
# ---------------------------------------------------------------------------


def _check_path_rules(rel_posix: str, size: int, origin: str, out: list[Violation]) -> None:
    for rule, patterns, severity in PATH_RULES:
        if any(match_glob(p, rel_posix) for p in patterns):
            out.append(Violation(rule, Path(rel_posix), size, severity, origin))
            break


def _looks_text(head: bytes) -> bool:
    return b"\x00" not in head


def _check_content_rules(path: Path, rel_posix: str, size: int, origin: str, out: list[Violation]) -> None:
    if _is_allowed_exception(rel_posix):
        return  # 设计批准的槽位描述文件与 fixture 豁免内容规则
    try:
        with path.open("rb") as fh:
            blob = fh.read(MAX_CONTENT_SCAN_BYTES + 1)
    except OSError:
        return
    if not _looks_text(blob[:1024]):
        return
    text = blob.decode("utf-8", errors="replace")
    for rule, regex, severity in CONTENT_RULES:
        if regex.search(text):
            # 秘密命中只给规则名：detail 固定为 None
            out.append(Violation(rule, Path(rel_posix), size, severity, origin))


def scan_tree(root: Path) -> list[Violation]:
    """扫描目标仓库：git 索引 + 工作树（未跟踪且未忽略的文件）。"""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"root directory does not exist: {root}")
    tracked = _git_ls_files(root)
    violations: list[Violation] = []

    # 1) git 索引（已跟踪内容会进入公开仓库；不受 .gitignore 影响）
    for rel in sorted(tracked):
        f = root / rel
        size = -1
        if f.is_file() and not f.is_symlink():
            size = f.stat().st_size
            _check_path_rules(rel, size, "git-index", violations)
            if size <= MAX_CONTENT_SCAN_BYTES:
                _check_content_rules(f, rel, size, "git-index", violations)
        else:
            _check_path_rules(rel, size, "git-index", violations)

    # 2) 工作树（未跟踪、未被 .gitignore 忽略的文件可能被意外提交）
    compiled_ignore = _compile_gitignore(_load_gitignore_patterns(root))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept_dirs = []
        for d in sorted(dirnames):
            rel_dir = (Path(dirpath) / d).relative_to(root).as_posix()
            if d == ".git":
                continue
            if _is_ignored(rel_dir, compiled_ignore):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            f = Path(dirpath) / name
            if f.is_symlink():
                continue
            rel = f.relative_to(root).as_posix()
            if rel in tracked or _is_ignored(rel, compiled_ignore):
                continue
            size = f.stat().st_size
            _check_path_rules(rel, size, "worktree", violations)
            if size <= MAX_CONTENT_SCAN_BYTES:
                _check_content_rules(f, rel, size, "worktree", violations)

    violations.sort(key=lambda v: (v.relative.as_posix(), v.rule, v.origin))
    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a public repository for leakage.")
    parser.add_argument("--root", default=".", help="repository root to scan (default: .)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    violations = scan_tree(root)
    for v in violations:
        print(
            f"[{v.severity}] rule={v.rule} origin={v.origin} "
            f"path={v.relative.as_posix()} size={v.size}"
        )
    severe = [v for v in violations if v.severity in SEVERE_SEVERITIES]
    if severe:
        print(
            f"scan FAILED: {len(severe)} severe violation(s), "
            f"{len(violations)} total"
        )
        return 1
    print(f"scan OK: no severe violations ({len(violations)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
