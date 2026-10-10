# 功能规划

本目录保存功能的需求分析和设计方案。各功能的实现状态见下表及对应文档；拟议接口、字段和组件不代表已经实现。

| 功能 | 需求分析 | 设计文档 | 状态 | 已提交 PR |
| --- | --- | --- | --- | --- |
| 模型定价同步 | [requirements.md](model-pricing-sync/requirements.md) | [design.md](model-pricing-sync/design.md) | 已实现、已部署，基础线上验证通过 | [xuleihive/turnstile#31](https://github.com/xuleihive/turnstile/pull/31)，待合并 |
| 用户设置 | [requirements.md](user-settings/requirements.md) | [design.md](user-settings/design.md) | 已实现；发布与验证记录见设计文档 | [xuleihive/turnstile#32](https://github.com/xuleihive/turnstile/pull/32)，待合并 |
| 组织管理 | [requirements.md](organization-management/requirements.md) | [design.md](organization-management/design.md) | P1 已实现、已部署及线上验证；P2 代码已实现，APIM 候选编译通过，真实 Entra 同步与身份验收待外部配置；详见[实施记录](organization-management/implementation.md) | 未提交 |

每个功能使用独立子目录。需求和实现发生变化时，应同步更新对应文档；不得将计划中的能力描述成现有行为。

组织管理追加菜单权限组改造：四个本地组、普通用户默认组，与既有部门数据授权及 APIM 权限独立。
管理员组支持同时指派、菜单取并集；人员弹窗和列表只读标签展示，不重复管理员维护入口。
菜单组数据结构追加 013/014 迁移，本次只读展示调整无需新增迁移。
发布状态及验证结果另见组织管理实施记录，不沿用 P1 已上线状态作本次上线证明。

PR 列记录对应功能已提交的开发 PR；合并状态于 2026-10-10 核对，与当前仓库的实现和部署状态分别记录。
