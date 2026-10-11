# 应用 TPM 保存与网关执行修复

日期：2026-10-11。问题：页面保存 TPM 到 PostgreSQL、定时投影也读取了它，但 APIM 最终订阅限流节点仍固定使用部署默认 100000，因此页面配置不生效。

## 实现边界

- 配置权威来源仍为 gateway_application_budget。保存成功后立即投影该应用本月 Q 行并读回；不重建订阅，不发布模型，不写 C/M/R 行，不改变月度预留及结算。
- Q 行新增兼容的字符串 ConfigurationUpdatedAt，来自原预算 updated_at，UTC 微秒精度。PostgreSQL 预算更新使用 clock_timestamp 与已有版本+1微秒的较大值，防止先开始、后持锁的事务倒退。
- API、Telemetry 和新建应用投影复用 application_quota_entity，Table Q 更新先读版本，再使用 ETag 条件写入；旧版本、等版本和无版本写入不能覆盖已有较新版本。插入冲突和 412 重读；其他 Q/C/M/R 写入语义保持。
- 直接保存投影失败不谎报数据库保存失败。详情返回 budget_projection 状态 projected/pending/unavailable、实际投影 TPM 和版本。页面显示“网关账本已同步”而非只凭数据库响应宣称 APIM 已生效。
- 原“同步 APIM”是只读订阅发现，改名“刷新 APIM 订阅”；不调用它来推送预算。
- APIM 订阅文字限流与图像 burst 分支使用 effectiveApplicationTpm。可信应用映射和预算账本可用且 TPM 正整数、不超过 Int32.MaxValue 时使用该值；否则保留平台默认值。月度 enforce 不影响 TPM。
- 保留现有按订阅、跨模型共享的计数 key；员工 TPM、系统默认、模型授权、月度预算、预留、结算及遥测不改。
- 升级采用幂等、严格识别的窄转换。模板、策略编译器和校验一起更新；旧已知模板可升级，不覆盖自定义未知策略。scripts/application_tpm_upgrade.py 用现有候选修订/读回/回滚引擎，不修改现有 operation。

## 发布

- 无数据库新迁移；001-018 不修改。core 内容变化，必须同时更新 API、Telemetry、Control-plane 的包和 expected core digest，暂停旧投影写入，避免旧进程在新版本守卫启用后写回旧配置。
- 保留真实当前三包、全量配置、数据库及原 APIM 修订；数据库隔离恢复并运行两次 no-op 迁移，校验所有表不变。
- what-if 只允许候选 API 与 parent policy 创建、三个现有 appsettings 修改，不允许删除。首次策略升级进入维护窗口，APIM 原有推理通道保持；维护结束验证触发器索引、挂载包与 digest。
- 候选验证必须使用真实订阅和完整有效策略，不把 XML/官方 toolkit 编译测试代替 Azure 部署验证。原修订保留作回滚，任何后续发布都应保留动态 TPM。

## 测试

- 保存后即时投影、投影失败保存仍成功；版本回退、无版本旧写入、ETag 412 与插入冲突。
- 文本/图像使用同一应用 TPM；不改变员工限流、计数 scope 或月度输出 clamp；旧策略升级与重复执行保持其他 section 和 operation。
- PostgreSQL 真实保存及版本单调性；类型上限、API/OpenAPI、多语言和页面状态。
- 线上保存不改原 cc-switch 额度值，读回数据库和 Q 版本一致；用隔离验证订阅测试低 TPM 429、高 TPM 恢复，真实客户预算/权限保持不变。测试资源只停用，保留审计，不删除客户实体。
