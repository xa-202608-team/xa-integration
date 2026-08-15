# xa-integration

XA-202608 多仓库协作的**总装仓库**（Public 可读；写权限由 GitHub 角色与规则控制），承担联合验证、复现与验收的职责。

## 仓库边界

本仓库**只保存**以下内容：

- **公共契约快照**：来自 `contract` 仓库的接口/数据契约快照；
- **组件版本锁**：各组件仓库的 commit / tag 版本锁定记录；
- **顶层 Docker Compose**：跨组件联合编排的顶层 compose 文件；
- **联合验证/复现脚本**：端到端联合验证与复现的脚本；
- **报告**：汇总后的实验报告与验收报告；
- **参考结果**：联合实验的参考数值结果；
- **图表**：报告配图与结果可视化；
- **验收材料**：各项验收所需的证据材料。

**组件源码不在本仓库保存**，只通过后续装配脚本从各组件仓库按版本锁生成（生成目录 `components/` 已被 `.gitignore` 排除，不会进入版本库）。

## 权限

本仓库为 **Public 可读**（2026-08-14 起五仓库均已转公开）：任何人均可读取与 clone；**组件源码不入本仓库**，只经装配脚本按版本锁生成；**写权限**仅限总负责人（integrators），由 GitHub 团队角色与分支保护规则（PR + 审核）控制，未授权账号不能写入任何分支。

权限模型与访问审计见 [`docs/access-model.md`](docs/access-model.md) 与 [`docs/access-audit.md`](docs/access-audit.md)。

## 迁移工具

- `tools/import_component_snapshot.py`：按 `config/snapshot-policy.yaml` 白名单把只读源快照导入目标组件仓库（dry-run 默认，`--apply` 才写目标；确定性审计 JSON）。
- `tools/scan_public_repo.py`：公开泄漏自扫描（工作树 + git 索引；秘密只报规则名；严重项退出码 1）。
- `requirements-dev.lock`：锁定开发依赖（pytest / PyYAML），CI 与本地复跑使用。

## RC 工件与安全装配

- `tools/build_rc_artifact.py` / `tools/verify_rc_artifact.py`：组件 RC 工件（ZIP64）生成与拒收校验（流式哈希、schema 镜像、成员集合、sidecar；全绿才允许解压）。
- `tools/lockfile.py`：组件版本锁（`schemas/component-lock.schema.json`，2020-12）；`load_lock(path) -> ComponentLock`、`validate_lock(dict) -> list[str]`。真实 `component-lock.yaml` 只能在 RC 交接时用已验证 Tag/Commit/工件哈希生成（被 `.gitignore` 排除，不入库；格式见 `examples/component-lock.example.yaml`）。
- `tools/export_component.py`：精确导出——Tag 必须解析到 lock 记录的 40 位提交（`git rev-list -n 1 <tag>`），`git archive` 从提交对象导出（脏工作树不影响内容），`tarfile` 两阶段安全提取（先全量校验后写文件；拒绝绝对路径、`..`、symlink、hardlink、设备文件），导出后断言无 `.git`。
- `tools/assemble_delivery.py`：按版本锁装配全新交付目录 `XA-202608_最终交付-rc.N/`（01–07 固定槽位）。代码经 `export_component` 进 `03_代码/components/{battery|phased_array|wheel}`；RC payload 只写 manifest 声明路径；`05_结果/reference` 只能来自已验证 RC 工件（reproduced 覆盖即失败）；staging 完成验证后原子 rename；拒绝已存在非空目标与最终目录名 `XA-202608_最终交付`。
