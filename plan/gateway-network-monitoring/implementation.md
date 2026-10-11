# 实施与验证

2026-10-12：初始化模板、独立网络hotfix、请求监控筛选、轮询和统一时区已实现；监控修复已发布并验收。

- 1812项Python测试通过，无跳过，1项既有Starlette弃用警告；Ruff、Mypy 293源文件通过。
- 目录API回归包含active+unattributed、归档优先以及部门数据权限；5项前端筛选测试、7项hotfix/轮询定向测试通过。
- 前端TypeScript/Vite构建及157项Node前端测试通过，保留既有大chunk警告；main及独立subnet hotfix Bicep编译通过，main只有原有长度/endpoint范围警告。
- 独立hotfix真实执行 --apply 返回 attach_needed=false、subnet_creation_needed=false，Developer/capacity1保持；没有Azure网络写入、SKU升级或重发布模型策略。
- 00:37两条真实cc-switch记录已定位，均HTTP200、非估算，记录正常消费；旧页面默认active过滤隐藏了未归因订阅调用。完整异步链路仍是近实时而非SSE/WebSocket严格实时。
- 当前API、Telemetry、Control-plane设置及真实包已私有备份；兼容发布仅更新包和expected digest，不修改001-018迁移、客户预算/定价/模型授权。

## 正式发布

- 92378f7兼容三包同时发布，统一core digest为2ec8c3239a532c613f81a6411cca7610253d24af2c45a480d05b7c331650228d。三个真实运行包所有上传文件逐字节一致；完整原appsettings仅改变该digest，两组各4个函数触发器索引/同步通过。
- 浏览器验收发现请求追踪日趋势按UTC、详情按本地时区，凌晨调用被画到“昨天”。47595da补丁统一所选时区，使用既有带时区cache key的usageActivity接口；只有API前端包再发布，core digest及Functions包不变。
- 最终实际挂载API包所有上传文件、线上HTML和入口JS/CSS再次逐字节一致；当前入口index-CafQqgOd.js。真实登录、应用业务接口、GPT Responses HTTP200通过，剩余9999980，动态应用TPM没有回退。
- 不重跑main，不改APIM网络/模型策略；独立网络hotfix现网验证为no-op。没有新迁移、新建收费APIM实例或调整Azure关停任务。

## 浏览器与事件链路

- 默认“启用”范围直接显示00:37:12和00:37:27两条cc-switch记录，62563/64259 Tokens、HTTP200、eventhub、estimated=false、ingest_error=null。保留组织/部门未归因事实，没有伪造目录绑定或重写遥测。
- 页面保持打开，在不点击刷新/不重载的情况下，真实小探测事件c80e00a4-c6b0-44a2-ba06-d30fade2e0f7自动出现，记录97变98；证明异步入库加页面10秒轮询完整链路可用，不承诺严格10秒事件送达SLA。
- 统一Asia/Shanghai后，趋势包含10/12、详情为2026/10/12 00:37:27。按cc-switch搜索显示51/98，清空后恢复列表。
- Edge页面身份、非空、无框架错误覆盖层及console error/warn检查通过；桌面和390x844 CSS像素移动端通过，移动端scrollWidth=innerWidth=390，无横向溢出。因浏览器原125%缩放，临时窗口尺寸校正后验证，最后已恢复视口，未改用户缩放设置。
- 截图及包/设置/事件证据仅在 /tmp/ts-network-monitor-release 下，未进入Git。临时两项容器级Blob Reader均删除并读回404；只停止自有本地PostgreSQL测试进程。Azure PostgreSQL最后读回Ready。

## 验证限制

- 初始化模板的从零创建、StandardV2真实接网，以及hotfix的“新建子网/长操作”分支本次只做编译和自动化验证，没有为测试另建付费环境。当前Developer网络是此前真实接网成功的环境，本次只进行幂等no-op验证，不能称此次又完整执行了一遍首次接网。
- 流式最终用量校准仍受日志入库、安全滞后及原10分钟任务影响；匿名记录可见和页面刷新不代表所有流式计量都立即精确。本次00:37两条记录已是非估算数据。
