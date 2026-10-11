# 实施记录

日期：2026-10-11。完整链路修复已实现，本地验证完成，线上发布准备中。

- 完整本地回归：1802 passed / 1 warning，无跳过；包含独立 PostgreSQL 16 用户持久化测试。Ruff、Mypy 272 源文件、frontend TypeScript/Vite 构建通过；后续网络模板2项通过，候选边界修复待最终复跑。
- 生产数据库已只读备份；隔离 PostgreSQL 16 恢复后 106 表在两次迁移前后 hash 完全相同，001-018 checksum 验证，两次 migrate 返回空。
- 隔离恢复数据库真实保存 TPM 为 11000000、12000000，返回值正确且 updated_at 单调递增。
- APIM what-if 仅创建候选 turnstile-tpm-cfd4aab28304f38a 与其 parent policy；源修订 turnstile-9-bda0c63f 保留，7个 operation 不变。
- 三包兼容更新 what-if 只修改三个现有 appsettings，无删除。此时没有待处理控制平面 operation/publication。
- Function 包备份需临时部署容器级 Blob Data Reader：只读、两容器、独立 role ID，两项临时权限已移除并读回404。未放宽存储网络，未启用共享密钥。
- 线上发布、候选真实验证、接口/页面及限流测试待完成；不将本地通过冒称线上已生效。

候选真实验证补充：APIM 能执行动态表达式，但应用账本读取返回 application_map_unavailable。只在自有非当前候选暂加诊断头并恢复后，确认 bootstrap 将带尾斜杠的 Table endpoint 再拼接斜杠，导致 //TurnstileLedger。追加修复：新建基础设施先规范化 endpoint，已有策略窄转换只规范化已知 ledger origin；不改其他 URL。旧候选不 promote，重新生成候选与兼容包后验证。

网络实测补充：规范化地址后仍有 Table 403 AuthorizationFailure。存储防火墙保持 Deny；已存在 Table Data Contributor，新增 APIM resource-instance rule 仍无效。确认实际 SKU=classic Developer 后，新增专用子网 snet-apim-ledger (10.42.4.0/24) 和 NSG，以稀疏 PATCH 切换 External VNet；长操作成功，SKU/capacity/identity 不变。候选读回 Table 200，识别 cc-switch 与账本 tokensPerMinute=10000000。

第二个候选边界：未限制模型时 applicationAdmission=application_policy_unconfigured，原 TPM 选择错误要求 admission=ok，仍回退100000。修复为独立可信映射 + 配置账本判断，保留授权逻辑；新增早期候选升级及拒绝未知自定义表达式测试。此前候选均未切换为正式修订。
