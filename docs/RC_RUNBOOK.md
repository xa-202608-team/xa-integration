# RC 工件交接运行手册（Runbook）

适用工具：`tools/build_rc_artifact.py`（生成）与 `tools/verify_rc_artifact.py`（拒收校验）。
适用对象：线下数据/权重的**私下交接**——RC ZIP 不入公开 git 仓库，只经私有渠道
（加密网盘、移动介质等）在组件负责人与总集成之间传递。

## 0. 安全模型（为什么有这套流程）

RC ZIP 承载不可公开的数据与权重，接收方在解压前必须确认八类攻击全部被拦住：

| # | 攻击 | 拒收错误码 |
|---|------|-----------|
| 1 | 路径逃逸（`../`、绝对路径、盘符 `C:`、反斜杠） | `UNSAFE_MEMBER_PATH` |
| 2 | 提交不匹配 / 短提交（非 40 位） | `GIT_COMMIT_MISMATCH` |
| 3 | payload 字节篡改 | `FILE_HASH_MISMATCH` |
| 4 | 删除已声明文件 | `MEMBER_SET_MISMATCH`（missing） |
| 5 | 混入未声明文件 | `MEMBER_SET_MISMATCH`（undeclared） |
| 6 | 重复 ZIP 成员 | `DUPLICATE_MEMBER` |
| 7 | 符号链接外部属性 | `SYMLINK_MEMBER` |
| 8 | 错误组件 / 错误契约版本 | `COMPONENT_MISMATCH` / `CONTRACT_VERSION_MISMATCH` |

附加防线：`MANIFEST_INVALID`（清单缺失/不可解析/违反 schema）、
`FILE_SIZE_MISMATCH`、`CHECKSUM_FILE_INVALID`（SHA256SUMS 矛盾）、
`SIDECAR_MISMATCH`（包外 `.sha256` 指向其他 ZIP）、`EXTRACT_TARGET_NOT_EMPTY`、
`UNSAFE_EXTRACT_PATH`（解压路径逃出目标根，纵深防御）。

核心规则：**校验器先在 ZIP 内流式计算哈希，绝不先解压**；Schema、成员集合、
路径、大小、哈希、组件、提交、契约版本全部通过，才允许 `--extract-to`；
任何失败都不在目标目录内外产生文件。

## 1. 生成（组件负责人侧）

### 1.1 前置条件

1. 组件仓库工作树**干净**（`git status --porcelain` 为空）——正式 RC 的内容
   必须冻结在固定提交上；有未提交变更时工具直接拒绝生成。
2. 本地槽位已填充：`artifact-map.yaml` 里 `mappings[].local` 指向的目录
   （`data/`、`checkpoints/`、`results/reference/` 等）真实存在且含普通文件。
   槽位缺失时工具拒绝生成空包。
3. `handoff/artifact-map.yaml` 已补齐 manifest 元数据（除 `environment`
   可自动探测外均为必填，缺一即拒）：
   - 顶层 `component`（下划线枚举：`battery` / `phased_array` / `wheel`）；
   - `random_seeds`（非空整数数组）、`commands`（非空字符串数组）、
     `public_summary`（非空字符串）；
   - 可选 `environment`（逐键覆盖自动探测值）、`reproduce_status`
     （省略时按 `REPRODUCE_OK`）；
   - 每个 `mappings[]` 项：`local`（仓库根内相对路径）、`payload`（ZIP 内
     前缀，如 `04_数据/battery`）、`role`，可选 `redistributable`（缺省
     `false`，保守）、`source_url`（外部数据必须 https，本地仿真为 null）、
     `processing_provenance`（缺省自动生成事实性说明）。

### 1.2 命令

```bash
python tools/build_rc_artifact.py \
  --component battery \
  --repo-root <组件仓库根> \
  --version 0.1.0 \
  --rc 1 \
  --contract-version component-contract-v1.1.0 \
  --output-dir <集成仓>/.private/handoff/outgoing/battery \
  --commit <40位组件提交，可选，提供后必须与 HEAD 一致>
```

产物（均在 `--output-dir` 内）：

- `<slug>-artifacts-vX.Y.Z-rc.N+<shortsha>.zip` —— shortsha 恒为 HEAD 前 8 位，
  由工具自动形成，**调用者不得手填**；slug 映射固定
  `battery→battery`、`phased_array→phased-array`、`wheel→wheel`；
- `<zip名>.sha256` —— 包外整包哈希，内容固定
  `64位小写哈希` + 两个空格 + ZIP 文件名 + 换行；
- ZIP 内：按 payload 路径排序的全部 payload 文件 + `HANDOFF_MANIFEST.json`
  （满足契约 `handoff-manifest.schema.json`）+ `SHA256SUMS`（逐 payload 文件）。

打包细节：`ZIP_DEFLATED` + `allowZip64=True`；枚举槽位时**不跟随符号链接**
（symlink 一律跳过，不打包）；生成前对 manifest 做结构校验，不产出非法清单。

### 1.3 生成失败排查

| 报错关键字 | 含义与处置 |
|---|---|
| `工作树不干净` | 先提交或清理变更再生成 |
| `--commit (...) 与 HEAD (...) 不一致` | 核对组件仓库 HEAD；文件名里的 shortsha 来自 HEAD |
| `本地槽位不存在` | 按 HANDOFF.md 填充槽位；拒绝空包是设计行为 |
| `越界` / `unsafe archive member` | artifact-map 的 local/payload 不允许绝对路径、`..`、盘符、反斜杠 |
| `缺少合法的 random_seeds` 等 | 补齐 artifact-map 元数据 |
| `不满足契约 schema` | 按提示修 manifest 元数据（通常是 source_url 非 https 或 seeds 非法） |
| `目标工件已存在` | 同名 RC 已存在：提升 rc 号或确认移除旧包后重打 |

## 2. 私下交接

1. 只通过私有渠道传 ZIP + 对应 `.sha256`（大文件见 §4 分卷策略）。
2. 同时经公开渠道（PR 描述/Issue）告知接收方：组件枚举、40 位完整提交、
   版本号与 rc 号——这三项是接收方校验的期望值，不应随 ZIP 私下传输
   （带外核对）。
3. RC ZIP **不进入**任何公开 git 仓库；集成仓的 `.private/` 已被
   `.gitignore` 忽略并受泄漏扫描器监控。

## 3. 验证与解压（接收方侧）

```bash
python tools/verify_rc_artifact.py \
  --archive <收到的>.zip \
  --expected-component battery \
  --expected-commit <40位组件提交> \
  --extract-to <空目录>
```

- 退出码 0 = 通过（此时才解压）；非 0 = 拒收，逐行输出 `[错误码] member=... 说明`，
  且**不会解压、不产生任何文件**。
- `--extract-to` 目标目录必须不存在或为空；每个成员的最终 resolved path
  必须落在目标根内。
- 若 ZIP 旁存在同名 `.sha256` sidecar，会一并校验：哈希必须匹配该 ZIP 本身，
  指向其他 ZIP 的 sidecar 一律 `SIDECAR_MISMATCH` 拒收。
- 建议先 `sha256sum -c <zip>.sha256`（或 `certutil -hashfile ... SHA256` 比对）
  核对整包哈希，再运行本工具。

## 4. 分卷策略（> 4 GiB 或传输工具限制）

工具自身**不实现私有分卷格式**；分卷只作用于"已完成的 ZIP"，用标准工具切分：

发送方（Linux/macOS/WSL，或任何有 GNU `split` 的环境）：

```bash
# 3 GiB/卷，卷名自动为 <rc>.zip.part001、.part002、…
split -b 3G --numeric-suffixes=1 --suffix-length=3 <rc>.zip <rc>.zip.part
(cd <卷所在目录> && sha256sum <rc>.zip.part* > PARTS.sha256)
```

约定（简报 Step 6）：

- 每卷命名 `<zip名>.part001`、`.part002`、…（三位序号，从 001 起）；
- 额外生成 `PARTS.sha256`：每行 `64位小写哈希` + 两个空格 + 卷文件名 + 换行，
  覆盖全部卷；
- 原 ZIP 的 `<zip名>.sha256` sidecar 一并移交（重组后核对整包用）。

接收方（**先逐卷验证，再重组，最后才跑本工具**）：

```bash
sha256sum -c PARTS.sha256          # 1) 逐卷验证，任何一卷不符即拒收
cat <zip名>.part001 <zip名>.part002 ... > <zip名>.zip   # 2) 重组（Windows: copy /b a+b+... out.zip）
sha256sum -c <zip名>.sha256        # 3) 核对重组后整包哈希
python tools/verify_rc_artifact.py --archive <zip名>.zip \
  --expected-component <组件> --expected-commit <40位提交> --extract-to <空目录>   # 4) 拒收校验
```

Windows 无 `split`/`sha256sum` 时可用 WSL2，或 PowerShell
`Get-FileHash -Algorithm SHA256` 逐卷比对；`copy /b` 可完成重组。

## 5. 与契约/扫描的衔接

- `HANDOFF_MANIFEST.json` 满足 `xa-component-contract` 仓库
  `schemas/handoff-manifest.schema.json`（`component-contract-v1.1.0`）；
  manifest 用下划线组件枚举，release_candidate / ZIP 文件名用连字符 slug。
- `internal` 集成仓自扫描（`tools/scan_public_repo.py`）在 CI 中把关：
  `.private/`、`outputs/`、模型二进制后缀等路径规则命中即失败——RC 工件
  必须留在被忽略的私有目录内。
