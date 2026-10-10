# 功能规划

本目录保存功能的需求分析和设计方案。各功能的实现状态见下表及对应文档；拟议接口、字段和组件不代表已经实现。

| 功能 | 需求分析 | 设计文档 | 状态 | 已提交 PR |
| --- | --- | --- | --- | --- |
| 模型定价同步 | [requirements.md](model-pricing-sync/requirements.md) | [design.md](model-pricing-sync/design.md) | 已实现、已部署，基础线上验证通过 | [xuleihive/turnstile#31](https://github.com/xuleihive/turnstile/pull/31)，待合并 |
| 用户设置 | [requirements.md](user-settings/requirements.md) | [design.md](user-settings/design.md) | 已实现；发布与验证记录见设计文档 | [xuleihive/turnstile#32](https://github.com/xuleihive/turnstile/pull/32)，待合并 |
| 组织管理 | [requirements.md](organization-management/requirements.md) | [design.md](organization-management/design.md) | P1 已实现、已部署及线上验证；P2 代码已实现，APIM 候选编译通过，真实 Entra 同步与身份验收待外部配置；详见[实施记录](organization-management/implementation.md) | 未提交 |

每个功能使用独立子目录。需求和实现发生变化时，应同步更新对应文档；不得将计划中的能力描述成现有行为。

组织管理菜单权限组包含四个本地组，未指派管理员默认普通用户，多个管理员组的菜单取并集。
管理员配置、人员只读标签和菜单权限统一来源；部门指派同时维护原有部门数据范围，
不再提供重复“数据授权”编辑入口。组织、部门、团队分别配置，均不授予 APIM 或模型权限。
旧菜单组和只读标签已随 `59d9bc8` 发布，013/014 已应用，但旧实现存在指派与标签不一致、
团队面板复用父部门以及组织配置入口缺失问题。统一来源修正追加 015，已完成本地验证，
已随 `47b9c11` 上线；发布和验证证据见[实施记录](organization-management/implementation.md)。
真实 Entra 租户同步仍未验收。

最新组织成员流程：组织层级“新增人员”必填名称、邮箱、登录密码、归属部门；
部门/团队仅“添加成员”，团队不可嵌套，删除调岗计划和历史身份编辑入口。
追加 016/017，保留计费主部门、历史查询和审计；已随 `47b9c11` 上线，
生产迁移、独立成员/管理员范围流程及 Light/Dark 桌面/手机验收通过。

PR 列记录对应功能已提交的开发 PR；合并状态于 2026-10-10 核对，与当前仓库的实现和部署状态分别记录。
