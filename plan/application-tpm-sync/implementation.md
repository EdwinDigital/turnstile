# 实施记录

日期：2026-10-11。完整链路修复已实现，本地验证完成，线上发布准备中。

- 完整本地回归：1794 passed / 7 skipped / 1 warning；另跑 PostgreSQL 16 持久化 53 passed / 7 skipped，再提供独立数据库运行被跳过的用户存储7项，7 passed；部署脚本75项通过。Ruff、Mypy 272 源文件、frontend TypeScript/Vite 构建通过。
- 生产数据库已只读备份；隔离 PostgreSQL 16 恢复后 106 表在两次迁移前后 hash 完全相同，001-018 checksum 验证，两次 migrate 返回空。
- 隔离恢复数据库真实保存 TPM 为 11000000、12000000，返回值正确且 updated_at 单调递增。
- APIM what-if 仅创建候选 turnstile-tpm-cfd4aab28304f38a 与其 parent policy；源修订 turnstile-9-bda0c63f 保留，7个 operation 不变。
- 三包兼容更新 what-if 只修改三个现有 appsettings，无删除。此时没有待处理控制平面 operation/publication。
- Function 包备份需临时部署容器级 Blob Data Reader：只读、两容器、独立 role ID，两项临时权限已移除并读回404。未放宽存储网络，未启用共享密钥。
- 线上发布、候选真实验证、接口/页面及限流测试待完成；不将本地通过冒称线上已生效。
