# Access Model（权限模型）

本文件是 XA-202608 多仓库协作权限配置的事实来源，仅总负责人（integrators）可见并维护。

## 权限矩阵

| 仓库 | @xa-202608-team/integrators | battery 负责人 | phased-array 负责人 | wheel 负责人 |
|---|---|---|---|---|
| contract | Admin | Read | Read | Read |
| xa-battery | Admin | Write | — | — |
| xa-phased-array | Admin | — | Write | — |
| xa-wheel | Admin | — | — | Write |
| xa-integration | Admin | — | — | — |

各仓库均为私有仓库，权限通过 team 协作组授予，具体如下：

- **contract**：`@xa-202608-team/integrators` Admin；battery / phased-array / wheel 三位负责人 Read（只读公共契约）。
- **xa-battery**：`@xa-202608-team/integrators` Admin；battery 负责人 Write。
- **xa-phased-array**：`@xa-202608-team/integrators` Admin；phased-array 负责人 Write。
- **xa-wheel**：`@xa-202608-team/integrators` Admin；wheel 负责人 Write。
- **xa-integration**：`@xa-202608-team/integrators` Admin；仅总负责人可见，三位组件负责人无任何权限。

## 三条硬性约束

1. **普通 Member 默认权限为 None**：组织成员若未被加入对应 team 协作组，对所有仓库默认无任何访问权限。
2. **三位负责人不是 Owner**：battery / phased-array / wheel 负责人仅拥有所在组件仓库的 Write 权限，不具备组织 Owner 角色，不能修改权限配置或创建/删除仓库。
3. **私有仓库禁止 fork**：所有仓库的 fork 权限已禁用，组件代码与集成产物只能通过分支/PR 流转，防止私有内容外泄。

## 可见性变更（2026-08-14）

经用户裁决，五个仓库（contract、xa-battery、xa-phased-array、xa-wheel、xa-integration）已全部由 private 转为 **public**，以在 GitHub Free 计划下启用分支/Tag 保护规则；本变更覆盖原"所有仓库均为 private"约束。影响如下：

- **读边界不再由仓库可见性隔离**：public 仓库任何人均可读、clone、fork；上表权限矩阵中的 Read 授予仅剩形式意义。
- **写边界仍由团队权限矩阵保证**：写入仍严格受 admin/push/pull 权限矩阵与分支保护规则（PR + 1 approval、禁 force push、禁删除、Tag 创建/删除仅 integrators 可绕过）约束，未授权账号对任何分支/Tag 均无写权限。
- **禁止 fork 的组织设置随 public 失去意义**：public 仓库任何人可 fork/clone，但 fork 仅为只读副本，不授予对上游仓库的任何写权限。
