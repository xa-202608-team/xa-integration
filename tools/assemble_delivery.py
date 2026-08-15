# -*- coding: utf-8 -*-
"""安全装配：按组件版本锁把源码 + RC 工件 + 复现结果装配成全新交付目录。

固定输出结构（任务简报 Step 4）：

    XA-202608_最终交付-rc.N/
      01_技术方案报告/                     <- slots 输入（只许写 01/02/06/07 槽位）
      02_数据集与仿真方法说明.pdf           <- slots 输入
      03_代码/components/{battery|phased_array|wheel}/   <- git 精确导出 + RC ZIP 的 checkpoints
      04_数据/                             <- RC ZIP 声明路径
      05_结果/reference/                   <- 只能来自 RC ZIP（reproduced 覆盖即失败）
      05_结果/reproduced/                  <- 本地复现输入（只许写此前缀）
      06_图表/                             <- slots 输入
      07_验收/                             <- slots 输入

安全模型：
- 目标目录必须**不存在或为空**；已存在的用户文件绝不触碰；
- 不得接受 ``XA-202608_最终交付``（最终目录，无 -rc.N 后缀）作为输出参数；
- 全部写入先落在同父目录的 staging 隐藏目录，验证通过后**原子 rename**；
- 每个交付路径只能被一个来源声明（git 导出 / RC ZIP / reproduced / slots），
  ``05_结果/reference`` 只能由已验证 RC 工件写入；
- RC ZIP 先经 ``verify_rc_artifact``（组件/提交/契约/逐文件哈希）并比对
  lock 的 ``artifact_sha256``；源码导出强制 Tag 解析 == lock commit；
- 装配目录任何位置出现 ``.git`` 即失败；失败时清理 staging、不留半成品。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional

try:
    from .artifact_common import MANIFEST_MEMBER_NAME, normalize_member, sha256_file
    from .export_component import export_component
    from .lockfile import LOCK_COMPONENTS, ComponentLock, load_lock
    from .verify_rc_artifact import verify_artifact
except ImportError:  # pragma: no cover - 以 `python tools/assemble_delivery.py` 运行
    from artifact_common import MANIFEST_MEMBER_NAME, normalize_member, sha256_file  # type: ignore
    from export_component import export_component  # type: ignore
    from lockfile import LOCK_COMPONENTS, ComponentLock, load_lock  # type: ignore
    from verify_rc_artifact import verify_artifact  # type: ignore

#: 最终交付目录名（不带 -rc.N 后缀）；禁止作为装配输出
DELIVERY_STEM = "XA-202608_最终交付"

_SLOT_CODE = "03_代码/components"
_SLOT_DATA = "04_数据"
_SLOT_REFERENCE = "05_结果/reference"
_SLOT_REPRODUCED = "05_结果/reproduced"

#: RC ZIP payload 声明路径白名单（契约 artifact-map 的 role 域）
_ARTIFACT_PREFIXES: tuple[str, ...] = (
    f"{_SLOT_CODE}/",
    f"{_SLOT_DATA}/",
    f"{_SLOT_REFERENCE}/",
)
#: reproduced 输入只允许写此前缀
_REPRODUCED_PREFIX = f"{_SLOT_REPRODUCED}/"
#: 其余槽位输入白名单（01/02/06/07）
_SLOT_PREFIXES: tuple[str, ...] = (
    "01_技术方案报告/",
    "02_数据集与仿真方法说明.pdf",
    "06_图表/",
    "07_验收/",
)

#: 固定目录槽位（装配结果必须存在）
_DIR_SLOTS: tuple[str, ...] = (
    "01_技术方案报告",
    _SLOT_CODE,
    _SLOT_DATA,
    _SLOT_REFERENCE,
    _SLOT_REPRODUCED,
    "06_图表",
    "07_验收",
)
#: 固定文件槽位
_FILE_SLOTS: tuple[str, ...] = ("02_数据集与仿真方法说明.pdf",)


def delivery_dirname(rc: int) -> str:
    """RC 装配目录名：``XA-202608_最终交付-rc.N``。"""
    if not isinstance(rc, int) or isinstance(rc, bool) or rc < 1:
        raise ValueError(f"rc must be an integer >= 1: {rc!r}")
    return f"{DELIVERY_STEM}-rc.{rc}"


@dataclass(frozen=True)
class DeliveryInputs:
    """装配输入：版本锁 + 三组件仓库 + 三 RC ZIP + 复现结果 + 其余槽位。"""

    rc: int
    lock: ComponentLock
    repos: Mapping[str, Path]
    artifacts: Mapping[str, Path]
    reproduced: Mapping[str, Path]
    slots: Mapping[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        delivery_dirname(self.rc)  # rc 合法性
        for label, mapping in (
            ("repos", self.repos),
            ("artifacts", self.artifacts),
            ("reproduced", self.reproduced),
        ):
            missing = sorted(set(LOCK_COMPONENTS) - set(mapping))
            if missing:
                raise ValueError(f"{label} is missing components: {missing}")
        for slot in self.slots:
            normalize_member(slot)  # 槽位声明路径必须可归一化


# ---------------------------------------------------------------------------
# staging 写入：声明路径唯一归属 + 前缀白名单
# ---------------------------------------------------------------------------


class _Stager:
    """staging 写入门禁：每个交付路径只允许一个来源声明并写入一次。"""

    def __init__(self, staging: Path) -> None:
        self.staging = staging
        self._claimed: dict[str, str] = {}

    def claim(self, rel: PurePosixPath, origin: str) -> None:
        key = rel.as_posix()
        if key in self._claimed:
            raise ValueError(
                f"delivery path claimed by two sources: {key} "
                f"({self._claimed[key]} and {origin})"
            )
        self._claimed[key] = origin

    def stage_file(self, src: Path, rel: PurePosixPath, origin: str) -> None:
        self.claim(rel, origin)
        target = self.staging / rel
        if target.exists():
            raise ValueError(f"delivery path already exists on disk: {rel.as_posix()}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)

    def adopt_tree(self, root: Path, origin: str) -> None:
        """把已落地的目录树（git 导出结果）登记为 origin 所有。"""
        for path in root.rglob("*"):
            if path.is_file():
                rel = PurePosixPath(path.relative_to(self.staging).as_posix())
                self.claim(rel, origin)


def _check_prefix(rel: PurePosixPath, allowed: tuple[str, ...], what: str) -> None:
    posix = rel.as_posix()
    for prefix in allowed:
        if posix == prefix.rstrip("/") or posix.startswith(prefix):
            return
    raise ValueError(f"{what} declares a path outside its allowed slots: {posix}")


def _walk_plain_files(root: Path, what: str):
    """遍历来源目录的普通文件；拒绝 symlink 与任何 .git 项。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in dirnames + sorted(filenames):
            if name == ".git":
                raise ValueError(f".git is forbidden in {what}: {Path(dirpath) / name}")
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"only plain files are allowed in {what}: {path}")
            yield path


# ---------------------------------------------------------------------------
# 装配主流程
# ---------------------------------------------------------------------------


def _stage_artifacts(inputs: DeliveryInputs, stager: _Stager) -> None:
    """三组件：ZIP 哈希门禁 + verify_rc_artifact + git 精确导出 + payload 落位。"""
    for component in LOCK_COMPONENTS:
        entry = inputs.lock.components[component]
        repo = Path(inputs.repos[component])
        archive = Path(inputs.artifacts[component])

        # 门禁 1：ZIP 整包 SHA256 必须等于 lock 记录
        if not archive.is_file():
            raise ValueError(f"RC artifact for {component} not found: {archive}")
        digest, _ = sha256_file(archive)
        if digest != entry.artifact_sha256:
            raise ValueError(
                f"{component} artifact_sha256 mismatch: lock pins {entry.artifact_sha256} "
                f"but {archive.name} hashes to {digest}"
            )

        # 门禁 2：工件校验器全绿（组件 / 提交 / 契约 / 逐文件哈希）
        report = verify_artifact(
            archive, expected_component=component, expected_commit=entry.commit
        )
        if not report.ok:
            detail = "; ".join(
                f"[{e.code}] {e.member}: {e.message}" if e.member else f"[{e.code}] {e.message}"
                for e in report.errors
            )
            raise ValueError(f"RC artifact for {component} was rejected: {detail}")

        # 门禁 3：Tag 必须解析到 lock 提交，再从提交对象导出源码
        code_dest = stager.staging / "03_代码" / "components" / component
        export_component(repo, entry.tag, entry.commit, code_dest)
        stager.adopt_tree(code_dest, f"git-export:{component}")

        # payload：只写 manifest 声明路径，且必须落在契约槽位域内
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read(MANIFEST_MEMBER_NAME).decode("utf-8"))
            for declared_path in [f["path"] for f in manifest["files"]]:
                rel = normalize_member(declared_path)
                _check_prefix(rel, _ARTIFACT_PREFIXES, f"artifact:{component}")
                stager.claim(rel, f"artifact:{component}")
                target = stager.staging / rel
                if target.exists():
                    raise ValueError(
                        f"delivery path already exists on disk: {rel.as_posix()}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(declared_path) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)


def _stage_reproduced(inputs: DeliveryInputs, stager: _Stager) -> None:
    """复现结果只允许写 05_结果/reproduced/；覆盖 reference 即失败。"""
    for component in LOCK_COMPONENTS:
        root = Path(inputs.reproduced[component])
        if not root.is_dir():
            raise ValueError(f"reproduced results for {component} not found: {root}")
        what = f"reproduced:{component}"
        for src in _walk_plain_files(root, what):
            rel = normalize_member(src.relative_to(root).as_posix())
            posix = rel.as_posix()
            if posix.startswith(f"{_SLOT_REFERENCE}/") or posix == _SLOT_REFERENCE:
                raise ValueError(
                    f"reproduced results must not overwrite reference "
                    f"(05_结果/reference is owned by verified RC artifacts): {posix}"
                )
            if not posix.startswith(_REPRODUCED_PREFIX):
                raise ValueError(
                    f"{what} declares a path outside 05_结果/reproduced/: {posix}"
                )
            stager.stage_file(src, rel, what)


def _stage_slots(inputs: DeliveryInputs, stager: _Stager) -> None:
    """其余槽位输入（01/02/06/07）：声明路径必须落在各自白名单内。"""
    for slot in sorted(inputs.slots):
        src = Path(inputs.slots[slot])
        rel_slot = normalize_member(slot)
        _check_prefix(rel_slot, _SLOT_PREFIXES, "slots")
        what = f"slot:{rel_slot.as_posix()}"
        if src.is_file():
            stager.stage_file(src, rel_slot, what)
        elif src.is_dir():
            for path in _walk_plain_files(src, what):
                inner = PurePosixPath(path.relative_to(src).as_posix())
                rel = PurePosixPath(rel_slot, inner)
                _check_prefix(rel, _SLOT_PREFIXES, what)
                stager.stage_file(path, rel, what)
        else:
            raise ValueError(f"slot source not found: {slot} -> {src}")


def _validate_staging(staging: Path) -> None:
    """staging 终检：固定槽位齐全 + 结果槽位非空 + 绝无 .git。"""
    for dir_slot in _DIR_SLOTS:
        if not (staging / dir_slot).is_dir():
            raise ValueError(f"missing delivery slot directory: {dir_slot}")
    for file_slot in _FILE_SLOTS:
        if not (staging / file_slot).is_file():
            raise ValueError(f"missing delivery slot file: {file_slot}")
    for component in LOCK_COMPONENTS:
        code = staging / _SLOT_CODE / component
        if not code.is_dir() or not any(code.rglob("*")):
            raise ValueError(f"missing exported code for component: {component}")
    for result_slot in (_SLOT_REFERENCE, _SLOT_REPRODUCED):
        if not any((staging / result_slot).rglob("*")):
            raise ValueError(f"result slot must not be empty: {result_slot}")
    for dirpath, dirnames, filenames in os.walk(staging):
        for name in dirnames + filenames:
            if name == ".git":
                raise ValueError(f".git is forbidden in the delivery: {Path(dirpath) / name}")


def _stage(inputs: DeliveryInputs, staging: Path) -> None:
    for dir_slot in _DIR_SLOTS:
        (staging / dir_slot).mkdir(parents=True, exist_ok=True)
    stager = _Stager(staging)
    _stage_artifacts(inputs, stager)
    _stage_reproduced(inputs, stager)
    _stage_slots(inputs, stager)
    _validate_staging(staging)


def assemble(inputs: DeliveryInputs, destination: Path) -> Path:
    """装配全新交付目录；staging 完成验证后原子 rename。"""
    destination = Path(destination)
    if destination.name == DELIVERY_STEM:
        raise ValueError(
            f"destination must not be the final delivery directory {DELIVERY_STEM!r}; "
            f"assemble an RC directory such as {delivery_dirname(inputs.rc)} instead"
        )
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError(f"destination must be new and empty: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.staging"
    if staging.exists():
        raise ValueError(f"staging directory already exists, remove it first: {staging}")
    try:
        _stage(inputs, staging)
        if destination.exists():  # 已验证为空目录：让位给 rename
            destination.rmdir()
        staging.rename(destination)  # 同父目录 rename：原子
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_key_value(pairs: list[str], option: str) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key or not value:
            raise ValueError(f"{option} expects KEY=VALUE, got: {pair!r}")
        parsed[key] = Path(value)
    return parsed


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Assemble a fresh delivery directory from the component lock."
    )
    parser.add_argument("--lock", required=True, type=Path, help="component-lock.yaml 路径")
    parser.add_argument("--rc", required=True, type=int, help="RC 序号（>= 1）")
    parser.add_argument(
        "--output-root", required=True, type=Path, help="输出根目录（自动建 XA-202608_最终交付-rc.N）"
    )
    parser.add_argument(
        "--repo", action="append", default=[], metavar="COMPONENT=PATH",
        help="组件本地仓库（三组件各一次）",
    )
    parser.add_argument(
        "--artifact", action="append", default=[], metavar="COMPONENT=PATH",
        help="已验证 RC ZIP（三组件各一次）",
    )
    parser.add_argument(
        "--reproduced", action="append", default=[], metavar="COMPONENT=PATH",
        help="本地复现结果根（声明路径树，三组件各一次）",
    )
    parser.add_argument(
        "--slot", action="append", default=[], metavar="SLOT=PATH",
        help="其余槽位来源（01_技术方案报告=/…、02_数据集与仿真方法说明.pdf=/…、06_图表=/…、07_验收=/…）",
    )
    args = parser.parse_args(argv)

    try:
        lock = load_lock(args.lock)
        inputs = DeliveryInputs(
            rc=args.rc,
            lock=lock,
            repos=_parse_key_value(args.repo, "--repo"),
            artifacts=_parse_key_value(args.artifact, "--artifact"),
            reproduced=_parse_key_value(args.reproduced, "--reproduced"),
            slots=_parse_key_value(args.slot, "--slot"),
        )
        destination = assemble(inputs, args.output_root / delivery_dirname(args.rc))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"assembly FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"assembly OK: {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
