# AIGateway-Proxy 用户版 / User Plugin

This package is separate from the subscription plugin `aigateway-proxy`.
Keep its extracted directory in a permanent location. From 0.4.4, the directory
matches the ZIP filename without `.zip`: `aigateway-proxy-user-Edwin.zip` contains
`aigateway-proxy-user-Edwin/`. Do not copy it over a subscription installation.

From 0.4.1, model namespaces use the username plus a short hashed disambiguator,
not the user's UUID. The UUID remains inside the private authorization config.
Existing downloaded packages keep their namespace: refreshing models or signing
in again does not rename it. Replace the old plugin with a new download explicitly
and reselect its models in the Agent; do not install both generations as duplicates.

1. 在 magpie 插件页面添加解压后的同名文件夹，例如 `aigateway-proxy-user-Edwin`。
2. 下载时已经通过当前平台会话授权，并绑定平台实际默认订阅。首次安装自动导入 OAuth 和本人模型。
3. 「刷新模型」使用已有授权，直接读取本人最新授权模型；不打开浏览器、不切换订阅。
4. 刷新完整替换本插件的文本/图片模型目录，其他供应商和订阅版不变。
5. 「重新登录」才打开平台验证页，重新认证并解析平台实际默认订阅，更新本地授权与配置。
6. 不覆盖已有本插件账号；主动退出后不会从旧包恢复登录，此时使用「重新登录」。
7. 「查看额度趋势」打开本机独立个人额度曲线。只记录平台真实快照，不修改 magpie 原生历史或预算模式；首次采样之前的数据未知。再次点击更新快照，平台有 60 秒读数缓存。
8. 从 0.4.3 起，「额度控制」点击一次直接切换 magpie 本地阻断，不再弹出选择框。平台仅记录时默认关闭；开启后原生卡片开始积累个人额度趋势。平台强制阻断时只提示不可关闭，不修改本地偏好。不会修改平台预算或重新登录。

The platform brand icon is prepared before download and bundled under icons/.
Both auth.icon and package.json.magpie.icon use the same offline data URI, so
plugin and provider pages show it without a network fetch. The current host has
one icon slot: the platform blue/white badge works in light and dark themes
without relying on automatic monochrome inversion. Prepared light/dark assets
and a versioned manifest are retained for future branding integration.

The quota-control choice is private to this user plugin. Platform blocking OR
the user's local choice determines magpie blocking; platform blocking cannot be
disabled here. Opening and confirming this action re-read the platform policy
without the normal 60-second cache. Ordinary quota polling keeps that cache.
Local blocking is an early guard, not a replacement for platform enforcement
across clients or concurrent requests. No budget means no invented local limit.
Text dispatch checks the allowance before invoking a model; exhausted local limits
return 429 without a model request. The image/MCP bridge checks it too when local
blocking is enabled. Its current OAuth access is read from the existing local account
only for read-only quota queries; image invocations still use the image-only capability.
No refresh token is stored in the bridge descriptor or sent by this guard. Quota
reads share a process-local cache of at most 60 seconds, with no background timer.
Turning local blocking off stops new native history samples but does not erase
magpie's existing history. Native curves also require the host's Trends setting
not to be Off. The independent page continues to show the platform's own mode.

Model prices use the platform's accounting rates in USD per million tokens,
including input, output, cache reads and cache writes. The native adapter only
passes the first two fields, so this plugin uses the existing `magpie model price`
command to import complete prices for this provider alone. Manual per-model,
provider-wide and global model price preferences take precedence. Pricing workers
cannot bootstrap accounts, rename profiles or start image companions.

The normal magpie data directory and a native CLI are required for automatic
price import (`magpie` on PATH, or the standard macOS app installation).
`AIGATEWAY_MAGPIE_EXECUTABLE` can explicitly name the installed magpie executable.
Imports have an eight-second batch budget; a later model refresh retries pending
models. Status and complete prices are private files under
`aigateway-pricing/<providerId>/`. Failed imports do not bill cache tokens as free.
Do not replace manual prices or host settings files by hand to force synchronization.

The independent quota page is for a local desktop installation; remote/web-only
hosts may not open its `file:` URL. It is not an embedded curve in magpie's native
card. No application shared budget is added. Platform audit mode remains unchanged;
magpie is non-blocking by default unless the user explicitly enables local blocking.
WorkBuddy's native session IDs and provider-card Agent icons remain host/client
integration limitations, not something this provider can manufacture.

The private download contains personal OAuth access and refresh tokens. It does
not contain an APIM key, password or browser cookie. Never share, upload or commit
the ZIP, extracted configuration or runtime state. Each download has an independent
installation and revocable grant; access lasts 15 minutes, refresh validity is
7 days (rolling), and the grant requires reauthentication after 30 days.
Models and the personal monthly Token allowance are read from the platform.
Only the personal monthly allowance is shown. Application accounting and control remain governed by the existing platform.
Audit-only quota windows do not stop the account unless local blocking is explicitly
enabled. Actual calls remain governed by the platform and APIM.

Only this plugin's namespace is replaced. Do not rename the old subscription
plugin or reuse its provider ID. Failed/canceled authorization preserves a
previous valid import; revoked permissions are never restored from a cache.

The default subscription is identified from the platform's effective gateway
credential, using the same configured-key precedence and dashboard fallback as
console invocation. The credential must match exactly one active APIM subscription;
application name sorting is never a fallback. A validated explicit user binding
still takes precedence. APIM policy, subscription authentication and application
accounting remain unchanged. Keys stay on the server, never in this user ZIP.
Model refresh keeps the existing binding; sign in again to resolve a changed default.
Existing user grants remain bound until reauthentication, without rewriting history.
The platform overwrites
caller-supplied OpenAI `user` / Anthropic `metadata.user_id` with the verified
governance identity and also signs the trusted APIM attribution context.

Anthropic rejects email-shaped IDs, so its model-facing identifier is the SHA-256
of the verified governance identity. APIM keeps the real signed identity for
personal/application accounting; the hash does not replace the budget user key.

The account name comes from the verified platform profile; the stable user UUID
remains in `uid`. Profile synchronization updates this plugin's existing account
slots, preserves tokens and other providers, and does not restore a signed-out account.

magpie's surrounding sign-in heading is host-owned. This plugin changes its two
method labels without changing magpie application code.

## 文生图 / Image Generation

有获授权的图片模型时，用户版会启动仅监听 loopback 的配套图片服务。它使用
仅图片权限、可撤销的用户能力凭据，不使用 APIM Key 或共享 OAuth Refresh Token。
普通图片供应商配置中只包含本机服务的随机凭据。

桌面 magpie 的推理端口没有供应商配置写入 API，因此不宣称自动注册原生图片选择器。
插件在 magpie 配置目录下的 `aigateway-proxy-user/<providerId>/` 生成两个私密文件：

- `images-mcp.json`：将其中本插件的 MCP 条目加入所用 Agent 的 MCP 配置；不要覆盖其其他条目。
- `images-provider.json`：需要原生图片入口时，通过 magpie 现有供应商导入/编辑界面配置
  该独立的 Images 供应商，使用文件中的本地地址、模型清单和本地 Key。

MCP 提供 `generate_image` 工具，模型枚举随本人模型目录更新。刷新模型会替换本插件
生成的图片配置；桥接每次调用检查当前插件仍启用且已登录。退出/停用/移除后桥接停止，
旧本地地址不能绕过用户登录继续调用平台。宿主没有配置写入入口时，手动登记的原生
Images 卡片可能需要手动移除，但旧图片服务已经不可调用。重新加载服务导致地址变化时，
重新导入生成的 MCP 条目，不把失效地址伪装为可用连接。

这些运行时文件包含本地凭据，请保密；不要上传、分享或写入 Git。
No magpie application source or existing Agent configuration is modified automatically.
