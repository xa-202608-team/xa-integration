# -*- coding: utf-8 -*-
"""白名单快照导入器：从只读源目录按 YAML 策略生成复制计划，导入目标组件仓库。

设计约束（与任务简报对齐）：
- 复制计划为白名单式（allow 命中才入计划），deny 永远优先于 allow；
- 相对路径一律转 POSIX；不跟随符号链接；源目录内的 .git 不参与评估；
- glob 语义：无 ``**/`` 前缀的规则锚定在仓库根（``data/**`` 不会误伤
  ``src/data/**``），带 ``**/`` 前缀的规则匹配任意深度，``**`` 段可匹配
  零或多层目录；
- 每个计划项的目标路径必须等于目标仓库根或其子路径，否则 ValueError；
  禁止以复制计划清空/覆盖目标根本身；
- 无 ``--apply`` 时为 dry-run，不写目标；``--apply`` 只创建/覆盖计划内的
  普通工程文件，从不删除目标已有内容；受保护治理文件已存在时记录
  ``PRESERVED_EXISTING`` 并继续，禁止静默覆盖；
- 审计 JSON 确定性输出（键排序、列表按相对路径排序、不含时钟）。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Union

import yaml

PolicyLike = Union[str, os.PathLike, dict]

# 目标侧受保护治理文件：存在时不覆盖，记录 PRESERVED_EXISTING 供人工合并。
PROTECTED_PATTERNS: tuple[str, ...] = (
    ".git/**",
    ".github/**",
    "README*.md",
    "CODEOWNERS",
    "LICENSE*",
    ".gitignore",
    "docs/access-*.md",
)


# ---------------------------------------------------------------------------
# glob 语义：fnmatch 不支持 **，这里自实现（** 匹配零或多层目录）
# ---------------------------------------------------------------------------


def _match_parts(pattern_parts: list[str], path_parts: list[str]) -> bool:
    """递归匹配路径段；单段内使用 fnmatchcase（大小写敏感、不跨段）。"""
    if not pattern_parts:
        return not path_parts
    head = pattern_parts[0]
    rest = pattern_parts[1:]
    if head == "**":
        # '**' 段匹配零或多层目录
        for skip in range(len(path_parts) + 1):
            if _match_parts(rest, path_parts[skip:]):
                return True
        return False
    if not path_parts:
        return False
    if fnmatch.fnmatchcase(path_parts[0], head):
        return _match_parts(rest, path_parts[1:])
    return False


def match_glob(pattern: str, relpath: str) -> bool:
    """判断 POSIX 相对路径是否命中策略 glob 规则。

    无 ``**/`` 前缀且含 ``/`` 的规则锚定仓库根；``**/`` 前缀匹配任意深度；
    不含 ``/`` 的规则（如 ``Dockerfile``、``requirements*.txt``）只匹配根级。
    """
    if not pattern or not relpath:
        return False
    pattern_parts = [p for p in pattern.split("/") if p != ""]
    path_parts = [p for p in relpath.split("/") if p != ""]
    if not pattern_parts or not path_parts:
        return False
    return _match_parts(pattern_parts, path_parts)


def _matches_any(rel_posix: str, patterns: Iterable[str]) -> Optional[str]:
    """返回第一条命中的规则，未命中返回 None。"""
    for pattern in patterns:
        if match_glob(pattern, rel_posix):
            return pattern
    return None


# ---------------------------------------------------------------------------
# 策略加载与决策
# ---------------------------------------------------------------------------


def _ensure_policy_dict(policy: PolicyLike) -> dict:
    if isinstance(policy, dict):
        return policy
    return load_policy(policy)


def load_policy(path: Union[str, os.PathLike]) -> dict:
    """从 YAML 文件加载快照策略。"""
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"invalid policy file (not a mapping): {path}")
    return data


@dataclass(frozen=True)
class Decision:
    """源树中单个文件的策略决策。"""

    relative: Path
    decision: str  # "allow" | "deny" | "skip"
    matched: Optional[str] = None  # 命中的规则（deny/allow 时给出）


def _classify(
    rel_posix: str,
    allow_rules: list[str],
    deny_rules: list[str],
) -> tuple[str, Optional[str]]:
    """deny 永远优先于 allow；均未命中则为 skip。"""
    denied = _matches_any(rel_posix, deny_rules)
    if denied is not None:
        return "deny", denied
    allowed = _matches_any(rel_posix, allow_rules)
    if allowed is not None:
        return "allow", allowed
    return "skip", None


def iter_source_files(source: Path) -> Iterator[Path]:
    """遍历源目录，产出相对路径；不跟随符号链接，跳过源仓库 .git。"""
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                continue  # 不跟随/不复制符号链接
            yield full.relative_to(source)


def evaluate_tree(source: Path, component: str, policy: PolicyLike) -> list[Decision]:
    """对源树全量文件做策略决策，结果按相对路径排序（确定性）。"""
    data = _ensure_policy_dict(policy)
    comp = data.get("components", {}).get(component)
    if not isinstance(comp, dict):
        raise ValueError(f"unknown component in policy: {component!r}")
    allow_rules = list(data.get("common_allow", [])) + list(comp.get("allow", []))
    deny_rules = list(data.get("common_deny", [])) + list(comp.get("deny", []))
    decisions = []
    for rel in iter_source_files(source):
        rel_posix = rel.as_posix()
        decision, matched = _classify(rel_posix, allow_rules, deny_rules)
        decisions.append(Decision(relative=rel, decision=decision, matched=matched))
    decisions.sort(key=lambda d: d.relative.as_posix())
    return decisions


# ---------------------------------------------------------------------------
# 目标路径安全
# ---------------------------------------------------------------------------


def ensure_within_destination(destination_root: Path, relative: Path) -> Path:
    """校验复制项的目标位于目标仓库根之下并返回安全绝对路径。

    目标必须等于目标仓库根或其子路径，否则抛 ValueError；
    relative 为空或指向上级（含逃逸、绝对路径、目标根本身）一律拒绝，
    禁止以复制计划清空/覆盖目标根。
    """
    root_resolved = Path(destination_root).resolve()
    rel_posix = Path(relative).as_posix()
    if rel_posix in ("", ".") or rel_posix.startswith("../"):
        raise ValueError(f"relative path escapes or targets the repo root: {rel_posix!r}")
    if Path(rel_posix).is_absolute():
        raise ValueError(f"relative path must not be absolute: {rel_posix!r}")
    target = (root_resolved / relative).resolve()
    if target == root_resolved or root_resolved not in target.parents:
        raise ValueError(
            f"destination {target} is outside target repo root {root_resolved}"
        )
    return target


# ---------------------------------------------------------------------------
# 复制计划与执行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyItem:
    """计划中的一条复制项。"""

    relative: Path
    source: Path
    destination: Path
    size: int
    sha256: str


def build_copy_plan(
    source: Path,
    destination: Path,
    component: str,
    policy: PolicyLike,
) -> list[CopyItem]:
    """生成白名单复制计划：只包含 allow 命中且目标路径安全的普通文件。"""
    source = Path(source)
    destination = Path(destination)
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    src_resolved = source.resolve()
    dst_resolved = destination.resolve()
    if dst_resolved == src_resolved or src_resolved in dst_resolved.parents:
        raise ValueError(
            "destination must not be the read-only source or inside it: "
            f"{dst_resolved}"
        )
    plan = []
    for decision in evaluate_tree(source, component, policy):
        if decision.decision != "allow":
            continue
        target = ensure_within_destination(destination, decision.relative)
        src_file = source / decision.relative
        blob = src_file.read_bytes()
        plan.append(
            CopyItem(
                relative=decision.relative,
                source=src_file,
                destination=target,
                size=len(blob),
                sha256=hashlib.sha256(blob).hexdigest(),
            )
        )
    plan.sort(key=lambda item: item.relative.as_posix())
    return plan


def apply_plan(plan: list[CopyItem], destination: Path) -> list[dict]:
    """执行复制计划：只创建/覆盖计划内文件，从不删除目标已有内容。

    受保护治理文件已存在时记 PRESERVED_EXISTING 并继续，由执行者人工合并。
    """
    destination = Path(destination)
    results = []
    for item in sorted(plan, key=lambda i: i.relative.as_posix()):
        rel_posix = item.relative.as_posix()
        protected = _matches_any(rel_posix, PROTECTED_PATTERNS)
        if protected is not None and item.destination.exists():
            results.append(
                {
                    "relative": rel_posix,
                    "status": "PRESERVED_EXISTING",
                    "detail": "protected governance file exists; manual merge required",
                }
            )
            continue
        if item.destination.is_symlink():
            results.append(
                {"relative": rel_posix, "status": "SKIPPED", "detail": "SYMLINK_TARGET"}
            )
            continue
        existed = item.destination.exists()
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item.source, item.destination)
        results.append(
            {
                "relative": rel_posix,
                "status": "OVERWRITTEN" if existed else "COPIED",
                "bytes": item.size,
                "sha256": item.sha256,
            }
        )
    return results


# ---------------------------------------------------------------------------
# 审计报告（确定性 JSON）
# ---------------------------------------------------------------------------

_PLANNED = "PLANNED"
_COPIED_STATUSES = (_PLANNED, "COPIED", "OVERWRITTEN", "PRESERVED_EXISTING")


def build_audit_report(
    source: Path,
    destination: Path,
    component: str,
    policy: PolicyLike,
    plan: list[CopyItem],
    results: list[dict],
    *,
    applied: bool = False,
) -> dict:
    """组装确定性审计报告：items 按相对路径排序，键由 json 排序。"""
    result_by_rel = {r["relative"]: r for r in results}
    items = []
    for decision in evaluate_tree(Path(source), component, policy):
        rel = decision.relative.as_posix()
        if decision.decision == "deny":
            items.append(
                {"relative": rel, "status": "SKIPPED", "detail": f"DENIED:{decision.matched}"}
            )
        elif decision.decision == "skip":
            items.append({"relative": rel, "status": "SKIPPED", "detail": "NOT_ALLOWED"})
        else:
            entry = dict(result_by_rel.get(rel, {"relative": rel, "status": _PLANNED}))
            entry["relative"] = rel
            items.append(entry)
    items.sort(key=lambda entry: entry["relative"])
    skipped_deny = sum(
        1 for e in items if e["status"] == "SKIPPED" and str(e.get("detail", "")).startswith("DENIED:")
    )
    return {
        "applied": bool(applied),
        "component": component,
        "source": str(Path(source).resolve()),
        "destination": str(Path(destination).resolve()),
        "policy_version": _ensure_policy_dict(policy).get("version"),
        "items": items,
        "totals": {
            "planned_or_copied": sum(1 for e in items if e["status"] in _COPIED_STATUSES),
            "skipped": sum(1 for e in items if e["status"] == "SKIPPED"),
            "skipped_deny": skipped_deny,
            "skipped_not_allowed": sum(
                1
                for e in items
                if e["status"] == "SKIPPED" and e.get("detail") == "NOT_ALLOWED"
            ),
            "preserved_existing": sum(
                1 for e in items if e["status"] == "PRESERVED_EXISTING"
            ),
        },
    }


def write_audit(path: Path, report: dict) -> None:
    """写出确定性审计 JSON（sort_keys + 换行结尾）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    path.write_text(payload, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Whitelist snapshot importer for component repositories."
    )
    parser.add_argument("--component", required=True, help="component key in policy (battery/phased_array/wheel)")
    parser.add_argument("--source", required=True, help="read-only source snapshot directory")
    parser.add_argument("--destination", required=True, help="target component repository root")
    parser.add_argument("--policy", required=True, help="path to snapshot-policy YAML")
    parser.add_argument("--audit-output", required=True, help="path of deterministic audit JSON")
    parser.add_argument("--apply", action="store_true", help="actually write files (default: dry-run)")
    args = parser.parse_args(argv)

    policy = load_policy(args.policy)
    source = Path(args.source)
    destination = Path(args.destination)
    plan = build_copy_plan(source, destination, args.component, policy)
    if args.apply:
        results = apply_plan(plan, destination)
    else:
        results = [{"relative": item.relative.as_posix(), "status": _PLANNED} for item in plan]
    report = build_audit_report(
        source, destination, args.component, policy, plan, results, applied=args.apply
    )
    write_audit(Path(args.audit_output), report)

    totals = report["totals"]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"{mode} component={args.component} items={len(report['items'])} "
        f"planned_or_copied={totals['planned_or_copied']} skipped={totals['skipped']} "
        f"preserved_existing={totals['preserved_existing']}"
    )
    print(f"audit written: {args.audit_output}")
    for entry in report["items"]:
        print(f"  {entry['status']:<18} {entry['relative']}{' (' + entry['detail'] + ')' if entry.get('detail') else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
