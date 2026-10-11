# 实施与验证

2026-10-12：初始化模板、独立网络hotfix、请求监控筛选与轮询已实现；本地验证完成，线上监控包发布准备中。

- 1812项Python测试通过，无跳过，1项既有Starlette弃用警告；Ruff、Mypy 293源文件通过。
- 目录API回归包含active+unattributed、归档优先以及部门数据权限；5项前端筛选测试、7项hotfix/轮询定向测试通过。
- 前端TypeScript/Vite构建通过，保留既有大chunk警告；main及独立subnet hotfix Bicep编译通过，main只有原有长度/endpoint范围警告。
- 独立hotfix真实执行 --apply 返回 attach_needed=false、subnet_creation_needed=false，Developer/capacity1保持；没有Azure网络写入、SKU升级或重发布模型策略。
- 00:37两条真实cc-switch记录已定位，均HTTP200、非估算，记录正常消费；旧页面默认active过滤隐藏了未归因订阅调用。完整异步链路仍是近实时而非SSE/WebSocket严格实时。
- 当前API、Telemetry、Control-plane设置及真实包已私有备份；后续兼容发布仅更新包和expected digest，不修改001-018迁移、客户预算/定价/模型授权。
- 验收与临时权限清理结果待发布后追加，不把本地验证表述成线上监控已修复。
