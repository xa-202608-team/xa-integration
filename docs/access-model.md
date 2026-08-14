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
