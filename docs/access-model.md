# Access Model（权限模型）

本文件是 XA-202608 多仓库协作权限配置的事实来源。自 2026-08-14 五仓库全部转为 public 后，本文件定义的隔离目标是**写边界**：任何人都可读、clone、fork 五个仓库；写入仅对被授权账号开放，且 main/Tag 只能经保护规则放行的 PR 变更。

## 仓库可见性事实

- contract、xa-battery、xa-phased-array、xa-wheel、xa-integration 均为 **public** 仓库：任何账号（含未登录访客）可读、clone、fork。
- 读访问不再构成隔离边界；fork 仅为只读副本，不授予对上游仓库的任何写权限。
- 写隔离由两层共同实现：
  1. **GitHub 仓库角色**：组件负责人仅对本组件仓库持有 Write；总集成（integrators）对五仓 Admin；组件负责人均非组织 Owner。
  2. **main/Tag 保护规则**：main 分支变更须 PR + 1 approval，禁 force push、禁删除；Tag 创建/删除仅 integrators 可绕过。

## 权限矩阵（2026-08-15 授权更新后）

| 仓库 | integrators（总集成） | battery 负责人（yclpig） | phased-array 负责人（暂缺，Black-Chan 兼任） | wheel 负责人（ABeginner-op） |
|---|---|---|---|---|
| contract | Admin | Public 读 | Public 读 | Public 读 |
| xa-battery | Admin | **Write（已核实生效）** | — | — |
| xa-phased-array | Admin | — | **Owner 兼任，待专人后补授 Write** | — |
| xa-wheel | Admin | — | — | **Write（已核实生效）** |
| xa-integration | Admin | — | — | — |

说明：

- 矩阵中"Public 读"表示该角色对此仓库无任何写授权，仅有公开仓库的默认读权限。
- 写授权通过仓库 collaborator 直接授予，2026-08-15 实际操作结果：
  - **yclpig → xa-battery**：`PUT collaborators` `permission=push`，GET `.../permission` 核实返回 `permission=="write"`、`role_name=="write"`，且已出现在 push 级 collaborator 列表，无 pending 邀请。
  - **ABeginner-op → xa-wheel**：`PUT collaborators` `permission=push`，GET `.../permission` 核实返回 `permission=="write"`、`role_name=="write"`，且已出现在 push 级 collaborator 列表，无 pending 邀请。
  - **phased-array 跳过授权**：相控阵负责人 Black-Chan 同时是组织 Owner（Admin > push），兼任阶段无需另授 collaborator Write；待指定相控阵专人后必须补授并复测（用户已批准本变体）。
- 授权细节与审计记录见 `docs/access-audit.md`，负责人 handle 清单见 `config/owners.yaml`。

## 三条硬性约束（2026-08-15 修订）

1. **普通 Member 默认无写权限**：组织成员若未被授予仓库角色，对所有仓库默认无任何写权限（public 仓库仍有公开读权限）。
2. **组件负责人不是 Owner**：battery / wheel 负责人仅拥有所在组件仓库的 Write 权限，不具备组织 Owner 角色，不能修改权限配置或创建/删除仓库。phased-array 负责人当前由组织 Owner Black-Chan 兼任，属已批准的过渡例外，待专人接手后必须移交并复测。
3. **fork 不授予写权限**：public 后任何人都可 fork/clone；fork 是只读副本，对仓库的实际写入仍严格受角色矩阵与 main/Tag 保护规则（PR + 1 approval、禁 force push、禁删除、Tag 创建/删除仅 integrators 可绕过）约束。原"私有仓库禁止 fork"设置随仓库转 public 失效，不再作为隔离手段。

## 可见性变更（2026-08-14）

经用户裁决，五个仓库（contract、xa-battery、xa-phased-array、xa-wheel、xa-integration）已全部由 private 转为 **public**，以在 GitHub Free 计划下启用分支/Tag 保护规则；本变更覆盖原"所有仓库均为 private"约束。影响如下：

- **读边界不再由仓库可见性隔离**：public 仓库任何人均可读、clone、fork。
- **写边界仍由角色与保护规则保证**：未授权账号对任何分支/Tag 均无写权限。
- **禁止 fork 的组织设置随 public 失去意义**：fork 仅为只读副本，不构成额外泄密面（仓库内容本身公开可读）。
