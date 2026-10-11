# AIGateway-Proxy for magpie

This is a **private** plugin download. `aigateway.config.json` contains an APIM
subscription key. Do not upload this folder to npm, GitHub, chat, or a shared drive.

## 安装 / Install

1. 解压 ZIP，把 `aigateway-proxy` 文件夹移动到自己保管的长期目录。
2. 在 magpie 的「插件」页面，选择本地文件夹，添加该文件夹（不是 ZIP）。
3. 首次加载时自动导入配置中的密钥到 magpie 的 `plugin-auth.json`（权限 0600）；
   已有供应商登录信息不会被覆盖。自动导入失败时，点击供应商选择「导入配置」。
   不会修改已有 Agent 配置。
4. 供应商和模型出现后，在 Agent 页面选择该供应商的模型。

CLI equivalent (uses your normal magpie configuration):

```sh
magpie plugin add /absolute/path/aigateway-proxy
# Usually not needed after installation; this explicitly re-imports the bundled key.
magpie plugin login aigateway-<subscription-uuid-without-hyphens> 1
magpie provider test aigateway-<subscription-uuid-without-hyphens>
```

The exact provider ID is `providerId` in the configuration. Each subscription has
a distinct ID. Keep the folder in place: magpie loads local plugins in place.
For development tests, isolate HOME, XDG_CONFIG_HOME, XDG_CACHE_HOME and MAGPIE_ADDR.

The plugin sends requests only to the selected HTTPS gateway, with the selected
APIM subscription key; agent credentials are not forwarded. Redirects are not
followed. Responses and cancellation remain streaming. Models are a download-time
snapshot intersected with gateway discovery, never a grant of access: live APIM
admission, quotas and subscription revocation always apply.

Automatic import runs once per provider in magpie. A non-secret marker in
`aigateway-proxy-imports/<providerId>.json` prevents restart/reload or a deliberate
sign-out from signing you back in. Existing accounts, including additional accounts,
are preserved. Concurrent imports use magpie's auth-file lock and preserve other
providers and the prior auth-file backup. Unreadable auth files or lock/write failures
are left alone: use the manual method. OpenCode does not auto-import.

「导入配置」 explicitly imports the key in this download. 「更新API Key」 asks for
a key you enter; it does not rotate a key in APIM. After key rotation, use 更新API Key
or download a fresh package and manually import it. A fresh package does not
automatically overwrite a previous login. After model grants change, download again to add new models.
If magpie still shows no provider after installation, switch this plugin off and on
to reload its host; this does not restart other agents.
Image-generation-only models are intentionally excluded. Requests, prompts and
subscription credentials are not logged by this plugin. No runtime dependencies,
install scripts or automatic updater are included.
