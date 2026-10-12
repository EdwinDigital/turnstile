# AIGateway-Proxy 用户版 / User Plugin

This package is separate from the subscription plugin `aigateway-proxy`.
Keep its `aigateway-proxy-user` directory in a permanent location. Do not copy it
over an existing subscription installation.

1. 在 magpie 插件页面添加本地 `aigateway-proxy-user` 文件夹。
2. 下载时已经通过当前平台会话授权，并绑定平台实际默认订阅。首次安装自动导入 OAuth 和本人模型。
3. 「刷新模型」使用已有授权，直接读取本人最新授权模型；不打开浏览器、不切换订阅。
4. 刷新完整替换本插件的文本/图片模型目录，其他供应商和订阅版不变。
5. 「重新登录」才打开平台验证页，重新认证并解析平台实际默认订阅，更新本地授权与配置。
6. 不覆盖已有本插件账号；主动退出后不会从旧包恢复登录，此时使用「重新登录」。

The private download contains personal OAuth access and refresh tokens. It does
not contain an APIM key, password or browser cookie. Never share, upload or commit
the ZIP, extracted configuration or runtime state. Each download has an independent
installation and revocable grant; access lasts 15 minutes, refresh validity is
7 days (rolling), and the grant requires reauthentication after 30 days.
Models and the personal monthly Token allowance are read from the platform.
Only the personal monthly allowance is shown. Application accounting and control remain governed by the existing platform.
Audit-only quota windows do not stop the account. Actual calls remain governed
by the platform and APIM.

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
