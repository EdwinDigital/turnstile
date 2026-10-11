# AIGateway-Proxy for magpie

This is a **private** plugin download. `aigateway.config.json` contains an APIM
subscription key. Do not upload this folder to npm, GitHub, chat, or a shared drive.

## 安装 / Install

1. 解压 ZIP，把 `aigateway-proxy` 文件夹移动到自己保管的长期目录。
2. 在 magpie 的「插件」页面，选择本地文件夹，添加该文件夹（不是 ZIP）。
3. 在该供应商登录时，选择「导入下载配置」。该操作把配置中的密钥导入
   magpie 的 `plugin-auth.json`（权限 0600）。不会修改已有 Agent 配置。
4. 供应商和模型出现后，在 Agent 页面选择该供应商的模型。

CLI equivalent (uses your normal magpie configuration):

```sh
magpie plugin add /absolute/path/aigateway-proxy
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

After key rotation, sign in with the API key method or download a fresh package
and import it again. After model grants change, download again to add new models.
Image-generation-only models are intentionally excluded. Requests, prompts and
subscription credentials are not logged by this plugin. No runtime dependencies,
install scripts or automatic updater are included.
