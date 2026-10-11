# AIGateway-Proxy 客户端设计

## 接口

- GET /api/v1/client-downloads/options：现有 OwnerSession 可导出的应用订阅、合法网关及对话模型；不返回密钥。应用/订阅状态及系统保留 ID 过滤保持不变；本人行权限延后。
- POST /api/v1/client-downloads/magpie：严格 JSON {application_id, subscription_id, gateway_id, key_kind}，key_kind 为 primary/secondary。重做所有校验后，调用现有 ApplicationAccessService 读取 APIM 密钥，在内存生成 application/zip。
- UUID 全由服务端匹配，网关地址、配置头和模型协议全部由平台确定。无客户端 URL/key/model 参数；请求体上限 8 KiB，校验错误不回显输入。
- 认证沿用 sessionCookie 和现有 OwnerSession，暂不新增权限控制能力。跨站请求沿用 require_allowed_write_origin。所有接口 no-store，下载附 nosniff 和固定 UUID 文件名。

## 数据与授权

通过 QueryRepository 读取应用、关联订阅、注册模型和生效发布，不创建新表。用户在实现中明确要求先做核心链路，因此 owner_id/部门/人员委派设计延后；当前接口复用既有 OwnerSession。普通用户暂不能导出订阅密钥。

网关必须与应用和订阅 gateway_profile_id 同时一致，启用、APIM 实现且 HTTPS 同源于部署 APIM_GATEWAY_URL，避免把单 APIM key client 的密钥发给其他网关。注册模型必须启用、所属 runtime 启用且归属所选网关，并且别名位于 effective publication 的 discovery_models。应用配置模型策略时再取 allowed_model_ids 交集；deny-all 输出零模型，不能回退 unrestricted。

元数据不调用远端模型或读取密钥。下载时才调用 APIM key client；保留其 unavailable/not-found/conflict 错误语义并避免输出 SDK 错误含凭据的文本。清单和下载均使用最新读取，不信任浏览器此前结果。

## 安装包

固定根文件夹 aigateway-proxy，文件 package.json、index.mjs、aigateway.config.json、README.md。不含依赖或安装脚本。后端模板放 backend/services/client_plugin 中，随 API 的 backend 目录整体打包。

配置 version=1，包含 application/subscription/gateway 的 ID 与显示名、稳定 providerId、baseURL、subscriptionKey 和模型映射；JSON 序列化而非向 JavaScript 插值。UUID 文件名不包含用户输入。插件供应商 ID 包含订阅 UUID，避免不同订阅覆盖。

模块只导出一个 async 插件函数。config 钩子用 ??= 声明供应商和模型；auth 使用「导入配置」的本地 OAuth auto callback 返回 {type: success, key, accountId}，不启动外部 OAuth、不自动覆写已有登录。另提供「更新API Key」标准 API key 登录，便于密钥轮换后的手动更新。首次加载且没有该供应商任何账号时，在 magpie 提供的 directory 内用共享 plugin-auth.json.lock 锁读取最新账号文件，保留旧文件备份并原子写入权限 0600 的 plugin-auth.json。非敏感一次性标记存于 aigateway-proxy-imports/<providerId>.json，安装重载、密钥手动更新及主动退出后不覆盖或重新导入；读取/锁/写入失败时保留手动导入入口。OpenCode 不自动导入。

loader 从当前账号 getAuth 读取密钥，设置 APIM Ocp-Apim-Subscription-Key，移除 Agent 原来的 Authorization/x-api-key/x-goog-api-key，不加入员工身份头。请求只允许选定网关路径中的模型协议端点，重定向设 manual，不消费/缓存响应体，保留 init.signal。

复用 gateway_protocol.invocation_api_paths 决定支持的协议；如支持 Responses 优先 Responses，不凭显示名猜测。模型逐项 npm 映射：openai_responses -> @ai-sdk/openai；anthropic_messages -> @ai-sdk/anthropic；其他受支持 chat -> @ai-sdk/openai-compatible。Anthropic SDK 的 /messages 重写为平台 /v1/messages；模型发现使用平台 /v1/models。不凭显示名推断协议。模型 context 取平台注册值；未知 context 使用保守客户端默认，output 使用客户端 8192 上限（不是厂商最大输出声明）。

provider.models 调用配置网关 /v1/models，取线上与快照交集；401/403 标记登录需要重新验证，网络/其他失败保留快照。网关实时 admission 才是最终授权，不宣称快照等于持续权限。

## 页面

下载客户端独立 route page=client-downloads，沿用既有 workspace 顶栏、卡片、字体、颜色与国际化。从用户菜单及命令搜索进入；跨数据源不重定向该页。查询 key 带账号/权限 revision，退出切换账号不混用列表。

有标签的应用订阅下拉、对应网关下拉、密钥类型选择、模型概览与警告复选框。无权限/无订阅/无可调用模型时解释原因。提交中防重复点击，POST blob 成功后生成临时 Object URL，下载完成释放，不把配置保存在 localStorage 或 Query Cache。

## 发布与回滚

只发布 API/frontend 包，core/schema/APIM 配置不改，不需新基础设施部署。保留生产已挂载的 ZIP 与 hash，从干净提交 git archive 构建 frontend 并用现有 Linux 依赖安装和确定性打包流程，通过 Microsoft Entra SCM 发布。部署内容不得回滚用户 main 的已有功能。

发布后验证 /health、静态资源 hash、真实账号 options/download、无权限会话、下载包真实 magpie 加载及最小模型请求。不用健康检查代替数据库认证和模型调用。失败可恢复留存 API ZIP；不恢复/重写数据库与 APIM 配置。
