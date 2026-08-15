# Access Audit

本文件记录最近一次由真实负责人账号完成的访问边界检查。

| 角色 | contract | battery | phased-array | wheel | integration |
|---|---:|---:|---:|---:|---:|
| battery | PASS | PASS | DENIED | DENIED | DENIED |
| phased-array | PASS | DENIED | PASS | DENIED | DENIED |
| wheel | PASS | DENIED | DENIED | PASS | DENIED |

## 审计元数据

- 运行日期：2026-08-14
- 核验人角色：
  - battery 行由电池负责人（yclpig）本人账号实测；
  - wheel 行由飞轮负责人（ABeginner-op）本人账号实测；
  - phased-array 行由总负责人（Black-Chan）暂代实测。
- 局限声明：phased-array 行由组织 Owner 暂代实测，Owner 天然可见全部仓库，该行隔离边界未获纯验证；待指定相控阵实际负责人账号加入团队后必须复测，复测前不将该行视为最终验收依据。
- 隐私声明：本审计未记录任何密码、令牌或私人联系方式；文中用户名（yclpig、ABeginner-op、Black-Chan）均为 GitHub 公开账号名。

只有全部预期可见项为 `PASS`、全部预期不可见项为 `DENIED` 后，基础设施才可验收。

## 转公开后的口径说明（2026-08-14）

2026-08-14 的三行实测矩阵反映的是**转公开前的私有读隔离边界**（用户裁决五仓库转 public 之前完成）。转公开后该矩阵仅具历史意义：读访问不再构成隔离边界（public 仓库任何人均可读）；**写权限边界未变**，仍由团队权限矩阵与分支/Tag 保护规则保证。如需复测，应改为针对写边界的验证（未授权账号尝试 push 应被拒）。

## 证据链更正（2026-08-14，最终评审发现）

- **事实更正**：经最终评审核实，yclpig 与 ABeginner-op 的组织邀请在审计落表时及至今均为 pending 状态，battery 与 wheel 团队成员列表为空——私有时期二人不具备任何仓库读权限。因此，审计元数据中 battery 行与 wheel 行"由本人账号实测"的记载不成立（结论数值可能与转述一致，但证据链无法独立复核）。
- **降级处理**：battery 行与 wheel 行降级为"结果经总负责人转述确认，未经本人账号独立验证"。
- **验收前提**：两位负责人接受组织邀请（https://github.com/orgs/xa-202608-team/invitation）加入对应团队后，须执行一次写边界复测——各自对无写权限仓库尝试 push 应被拒、对本组件仓库 push 走 PR 流程验证——结果落表后，方可视为访问边界验收完成；phased-array 行的 Owner 暂代局限同时复测，并一并澄清该行 DENIED 值的产生方式。
- 本节不记录任何密码、令牌或私人联系方式。

## 写授权操作记录（2026-08-15）

执行者：总集成侧（codex，经用户授权的 XA-202608 Task 8 第二批操作）。三组授权操作如下：

| 目标仓库 | 账号 | 操作 | 结果核实 |
|---|---|---|---|
| xa-battery | yclpig | `PUT repos/xa-202608-team/xa-battery/collaborators/yclpig` `permission=push` | GET `.../collaborators/yclpig/permission` 返回 `permission=="write"`、`role_name=="write"`；已列于 push 级 collaborator 列表；仓库 invitations 为空 |
| xa-wheel | ABeginner-op | `PUT repos/xa-202608-team/xa-wheel/collaborators/ABeginner-op` `permission=push` | GET `.../collaborators/ABeginner-op/permission` 返回 `permission=="write"`、`role_name=="write"`；已列于 push 级 collaborator 列表；仓库 invitations 为空 |
| xa-phased-array | （Black-Chan） | **跳过授权**：Black-Chan 为组织 Owner（Admin > push），兼任阶段无需另授 collaborator Write | 待指定相控阵专人后补授 Write 并复测；本变体经用户批准 |

- 授权时点两位负责人均已是有效 collaborator（PUT 返回无邀请体），不存在 PENDING_INVITATION 状态；若后续出现邀请未接受（PENDING_INVITATION）情形，应如实记录为"已发出邀请、权限未生效"，不得记为已具备。
- 2026-08-14 节所述"组织邀请 pending"状态截至本次授权已不成立：yclpig 与 ABeginner-op 的仓库级写权限已直接生效，二人可对本组件仓库走 PR 流程推送。
- 本节不记录任何密码、令牌或私人联系方式；文中用户名均为 GitHub 公开账号名。
