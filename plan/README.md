# 功能规划

本目录保存功能的需求分析和设计方案。各功能的实现状态见下表及对应文档；拟议接口、字段和组件不代表已经实现。

| 功能 | 需求分析 | 设计文档 | 状态 | 已提交 PR |
| --- | --- | --- | --- | --- |
| 模型定价同步 | [requirements.md](model-pricing-sync/requirements.md) | [design.md](model-pricing-sync/design.md) | 已实现、已部署，基础线上验证通过 | [xuleihive/turnstile#31](https://github.com/xuleihive/turnstile/pull/31)，待合并 |
| 用户设置 | [requirements.md](user-settings/requirements.md) | [design.md](user-settings/design.md) | 已实现；发布与验证记录见设计文档 | [xuleihive/turnstile#32](https://github.com/xuleihive/turnstile/pull/32)，待合并 |
| 组织管理 | [requirements.md](organization-management/requirements.md) | [design.md](organization-management/design.md) | 草案，未实现；仅 APIM 显示；含初始化/升级脚本、兼容迁移、部门管理员与 Entra 扩展；[规范核查](organization-management/review.md)已修订设计，实施验证待完成 | 未提交 |

每个功能使用独立子目录。需求和实现发生变化时，应同步更新对应文档；不得将计划中的能力描述成现有行为。

PR 列记录对应功能已提交的开发 PR；合并状态于 2026-10-10 核对，与当前仓库的实现和部署状态分别记录。
