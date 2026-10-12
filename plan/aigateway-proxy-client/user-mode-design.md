# AIGateway-Proxy 用户登录版设计

日期：2026-10-12。状态：implemented / deployed；当前用户版 0.2.1，验证范围见 implementation.md。

本文最初作为 V2 设计基线；用户批准后已实施平台与插件代码、追加授权迁移和发布。V1 0.1.1 指定订阅版继续使用 design.md 的既有流程。未修改 magpie 应用源码或用户日常 Agent 配置；实际宿主测试使用独立 HOME/XDG 与端口。

### 实施确认与限制

- magpie 的 config hook 接受 Config.Model，而动态 provider.models hook 必须返回 Model.Info；用户版将平台目录转换为 api.id/url/npm、limit 和 capabilities，不能直接返回配置对象丢失 SDK 协议。
- 真实 magpie CLI 的自动回调、稳定用户账号合并、cc-switch -> Edwin-Agent -> cc-switch 替换及重新登录通过；文本模型目录依次为 5 -> 2 -> 5，用户版账号始终只有一个，订阅版和其他配置不变。
- 普通/流式 GPT、Claude 流式别名、工具调用往返及真实图片 MCP 调用通过。原生图片配置仍需用户登记生成文件，不宣称自动注册桌面图片选择器。
- 原生额度接口已读到个人与应用窗口；六次真实调用合计 520 Tokens，两组额度最终均增加 520。额度缓存 60 秒，应用确认用量另依赖 Event Hub 摄取，不是逐 Token 实时显示。
- WorkBuddy 新用户版界面、所有模型、长期运行/高并发和图片专属订阅的真实生产场景尚未单独验收，详见实施记录。

## 1. 目标、边界与关键取舍

1. 在下载页并列提供指定订阅包与指定用户包，不让现有订阅包悄悄改变认证或密钥行为。
2. 用户版在浏览器验证平台身份，在平台选择本人可调用的订阅/网关，模型目录与额度持续读取平台。
3. 一个用户版供应商一次生效一个应用订阅及关联网关；导入是替换而不是追加。多个模型共享这一调用上下文，不能随机从其他订阅取凭据。
4. 不修改 magpie 源码。它决定插件页「登录」按钮和认证面板的布局；插件能定义方法标签、URL、提示和回调，不能任意添加宿主 UI。
5. 文本模型使用供应商插件；文生图设独立兼容性门，必要时通过插件包内配套图片桥接接入，不假设通用插件已经支持原生图片选择器。
6. 新版需要新增平台客户端能力。旧版「只前端改动」「不改 schema/APIM」的交付边界不再适用，但历史数据、既有授权与预算语义保持不变。

## 2. 当前源码事实

| 事实 | 本地证据 | 设计影响 |
| --- | --- | --- |
| 当前下载接口使用 OwnerSession，service.options 忽略 identity | backend/http/client_downloads.py；backend/services/client_downloads.py | 不能直接拿旧 options 当个人订阅目录 |
| 当前只导出启用/已发布/应用允许的 chat 模型 | backend/services/client_downloads.py | 用户版需完整能力目录和图片协议分支 |
| 当前包包含 subscriptionKey，版本 0.1.1 | backend/services/client_downloads.py；backend/services/client_plugin | 新包必须使用独立模板且不导出 APIM Key |
| 当前 models hook 只取线上与快照的交集 | backend/services/client_plugin/index.mjs | 新版需从平台全量目录增删模型，而不只是过滤 |
| 浏览器授权回调、OAuth Token 保存、刷新均已存在 | magpie/internal/provider/pluginsignin.go；magpie/internal/plugin/host.js | 可做真实外部认证，无需更改 magpie |
| 宿主有 ask 扩展，但 authorize 用 prompts.length 判断是否传 inputs | magpie/internal/plugin/host.js: nextPrompt / authorize | 原生动态下拉需专项验证；平台选择页为主流程 |
| settle 按同 provider 的稳定 accountId/uid 合并账号 | magpie/internal/plugin/host.js: settle | 固定用户账号身份，不将订阅名称作为 accountId |
| auth.usage 能产生额度窗口，并影响账号可用性 | magpie/internal/provider/plugin_usage.go；magpie/internal/plugin/host.js | audit 窗口必须 aside=true，不能伪造 100% 已耗尽 |
| 普通插件 Provider 带 Account；Drawers 排除普通 Account | magpie/internal/provider/plugins.go；magpie/internal/gateway/draw.go | 文生图不是加 vision 字段即可完成，需要桥接验证 |
| 应用详情预算有 confirmed、pending、upper bound、available 等字段 | turnstile_core/domain/application_access.py | 展示必须保留预留与实际可用口径 |
| 平台已有个人身份绑定与模型/预算检查 | backend/http/model_platform.py；backend/services/runtime_service.py | 可复用规则，但既有 invoke 响应不是原生 SSE 代理协议 |

以上只是源码与本机版本能力核对，不构成新插件已运行的证明。实施 P0 必须在实际版本上验证；未来宿主版本变化需重新核对。

## 3. 下载页与版本隔离

### 3.1 指定订阅下载

- 保留当前应用订阅、网关、主/备用密钥、模型预览、含密钥警告与下载确认。
- 版本固定 0.1.1；根目录 `aigateway-proxy`，providerId 为现有 `aigateway-<subscription UUID>`。
- 保留原有自动导入一次、导入配置和更新API Key。该版依旧是私密服务订阅配置，不自动获得用户认证、动态新模型或个人额度功能。
- 使用既有 OwnerSession 和 API。旧模板与历史包不因用户版开发被修改；今后升级旧版另设版本和验收。

### 3.2 指定用户下载

- 卡片展示当前账号、平台地址、用户版版本、登录后选择订阅的说明和支持能力。无需选择 APIM Key，也无需勾选「包内含订阅密钥」。
- 新增 `POST /api/v1/client-downloads/magpie-user`，使用 CurrentSession 和自身下载策略；正常用户可下载自己的包，不从普通用户输入中接受目标账号 ID。
- 根目录 `aigateway-proxy-user`；独立模板建议 `backend/services/client_user_plugin/`，建议插件版本 0.2.0。下载响应 no-store，文件名不含邮箱。
- 仅包含公开 client_id、schema_version、platform_origin、platform_id、签名引导描述、非秘密 opaque 用户绑定和安装说明。不带 Cookie、用户密码、APIM Key 或 OAuth Token；仍按个人配置保管。
- providerId 稳定为 `aigateway-user-<platform opaque ID>-<user opaque ID>`，与旧订阅版和其他用户版不冲突；供应商显示名为「AIGateway User · 当前用户」，所选订阅在状态/计划信息展示。
- 用户绑定由服务端签名且在授权时校验，不把修改 ZIP 中的 user_id 视作权限。登录其他账号拒绝完成授权并提示下载该账号自己的包。

### 3.3 名称与命名空间（双安装硬约束）

名称区分由插件包及其元数据完成，不修改 magpie 应用代码。业务名称、宿主实际插件技术名称和供应商标识分开定义：旧版不能为了展示改名而重建现有 providerId 或移动用户已经安装的目录。

| 标识 | 指定订阅版（保留 0.1.1） | 指定用户版（建议 0.2.0） |
| --- | --- | --- |
| 下载页/说明中的产品名 | AIGateway-Proxy 订阅版 | AIGateway-Proxy 用户版 |
| 插件技术名称/根目录 | `aigateway-proxy`（保留现有安装） | `aigateway-proxy-user` |
| package.name | `opencode-aigateway-<subscription UUID hex>-auth`（现有值） | `opencode-aigateway-user-<platform opaque ID>-<user opaque ID>-auth` |
| ZIP 文件名 | `aigateway-proxy-<subscription UUID>.zip`（现有值） | `aigateway-proxy-user-<platform opaque ID>-<user opaque ID>.zip` |
| 供应商显示名 | `AIGateway · <应用名>`（现有值） | `AIGateway User · <用户名>` |
| providerId | `aigateway-<subscription UUID hex>`（现有值） | `aigateway-user-<platform opaque ID>-<user opaque ID>` |
| 模型完整 ID | `<订阅 providerId>/<公开模型别名>` | `<用户 providerId>/<公开模型别名>` |
| 包内配置文件 | `aigateway.config.json`（现有值） | `aigateway-user.config.json` |
| 本地标记/状态目录 | `aigateway-proxy-imports/<订阅 providerId>.json` 等现有路径 | `aigateway-proxy-user/<platform ID>/<user ID>/<installation ID>/` |
| 后端模板目录 | `backend/services/client_plugin/` | `backend/services/client_user_plugin/` |
| 图片派生供应商 | 旧版不自动新增 | `<用户 providerId>-images`；显示 `AIGateway User Images · <用户名>` |

- opaque ID 必须是平台稳定产生的非秘密、包名安全字符，不将邮箱或展示名插入技术 ID/路径；版本号不进入 providerId，升级同版插件保留账号身份。
- 两个根目录解压到同一父目录不互相覆盖；两版相同的 `index.mjs` 文件名不会冲突，因为安装 spec 和模块目录不同。不得要求用户把新版复制到旧版目录原地覆盖。
- 宿主实际如何显示 package.name/目录 basename 需 P0 验证；新版技术名称必须带 `user`，即使宿主不读取业务显示名，插件列表也能与旧版区分。
- 登录和配置清理以精确 providerId 为边界；账号槽位仅匹配 `id === providerId` 或 `id.startsWith(providerId + '#')`。禁止用 `startsWith('aigateway-')` 清理所有 AIGateway 账号，因为该前缀同时包含两版和其他订阅。
- 图片派生项使用记录在 ownership manifest 中的精确 ID。导入/移除不删除另一版本、另一用户、另一订阅的账号、模型、标记或桥接进程。
- 同一公开模型别名在两版存在时使用供应商限定名，不向全局裸模型 ID 写入覆盖关系；也不复用 `openai`、`copilot`、`grok` 等已有/内置供应商 ID。
- 不自动修改旧包元数据、重命名已安装文件夹或迁移旧账号。两版是独立插件，不注册成同一个包的不同版本供自动更新器互相替换。

## 4. 认证和订阅选择体验

### 4.1 用户流程

```text
安装用户版插件
  -> magpie 现有登录入口 / 登录平台
  -> 平台验证页：登录 + 确认账号、设备和权限
  -> OAuth 授权完成，magpie 显示平台用户
  -> 供应商：导入模型配置
  -> 平台选择页：读取本人订阅 -> 选择订阅与关联网关 -> 预览文本/图片模型
  -> 确认导入 -> 客户端回调保存 -> 激活本次导入
  -> 替换本插件模型目录；读取额度
```

- 图 1 的「登录」是 magpie 现有宿主入口；对用户版执行真实平台认证，不能仅将 auth.type 改成 oauth 就声称已认证。
- 图 2 的方法标签由新插件提供：「登录平台」「导入模型配置」「重新登录」，没有「更新API Key」。已登录状态是否隐藏某个按钮由宿主决定，不承诺截图级自定义布局。
- 主流程的订阅列表在平台选择页展示，从 magpie 的导入入口打开；不是承诺在 magpie 面板内新增任意下拉控件。该页面可将登录与首次导入串联，但登录成功不等于已选订阅。
- 原生 `ask(inputs)` select 是可选体验增强。必须验证实时目录读取、账号作用域、prompts/authorize 的输入传递以及登录完成后的列表刷新，才可采用；不能依赖不存在的 client.auth.get。
- 导入入口没有可用登录时进入平台认证；已有浏览器 Session 可用于身份确认，不必再次输入密码。重新登录必须强制平台再认证，不能只重用旧 Cookie 或刷新模型缓存。
- 同一用户重新登录成功后重新验证原选择：仍有调用权则保留订阅选择并刷新完整目录；已失去调用权则撤销该上下文、移除本插件旧模型并要求重新选择。重新登录失败不破坏仍合法的旧选择，不能以新空目录无意覆盖旧导入。
- 未导入时供应商可显示已登录、无模型，并提示「请选择订阅并导入模型配置」。没有可访问订阅时显示原因，不导入空模板作为成功。

### 4.2 Device Authorization Grant

选择 RFC 8628 风格设备授权，避免依赖固定 localhost 回调端口或未知 magpie URL scheme。它是面向公共桌面客户端的设备流程，不使用 implicit grant，也不在包内放 client_secret。

1. 插件生成本机 installation_id 与请求 nonce；匿名调用设备授权接口，带公开 client_id 和签名用户绑定。服务端限速并生成高熵 device_code、短 user_code、verification_uri、expires_in 和轮询 interval。
2. 建议 device_code 有效期 5 分钟，初始轮询 5 秒；服从 slow_down，退避有上限。短码只能定位申请，不能兑换令牌；服务端只保存设备码摘要。
3. authorize 返回平台 URL、确认短码/设备的提示、`method: auto`；callback 在有效期内轮询，不占用或改动其他 Agent。URL 只含非秘密短码，不带 device_code、Access Token 或 Refresh Token。
4. 浏览器用既有平台 Session 登录；确认下载绑定用户、设备、申请的只读目录/额度与模型调用权限。approve/deny 是同源、有 CSRF 防护的显式 POST，GET 不自动授权。
5. 兑换一次性成功结果；插件 callback 返回 OAuth success，含 access、refresh、expires、稳定 accountId 和 uid。宿主保存凭据，插件不直接重写全局 auth 文件。
6. 初始建议 access 15 分钟、refresh 30 天绝对到期且 7 天未使用到期；均须可配置并受撤销约束。`expires` 使用宿主要求的毫秒值；实现 auth.refresh，refreshLead 建议 60 秒，避免 15 分钟 token 提前 5 分钟反复更新。
7. Refresh Token 长期只保存摘要，轮换使用事务与 single-flight。对相同请求 ID 的重试提供有限幂等结果：如需重新返回首次成功的令牌结果，应加密短期保存（建议 30 秒内清理），不能只存摘要却承诺可再次返回原始 Token。不同消费者使用旧令牌判重放并撤销 token family，不无限放宽旧令牌有效期。
8. 取消/超时/浏览器拒绝不删除当前有效账号。用户禁用、注销、退出 family、身份关联变化均可撤销；正常网络故障不标记为永久登录失效。

安全要求：令牌只能发往配置中的平台 HTTPS origin；禁止开放重定向、任意 token_url 和跨域凭据转发。设备授权记录、Token、Cookie 和短码不进入普通业务日志。个人包绑定不是授权，最终身份来自浏览器批准与服务端令牌。

退出语义必须另行验证：magpie 删除本地登录并不自动证明已调用平台 revoke。平台应提供本人设备授权撤销入口，主插件/桥接在能检测到自身账号删除或停用时撤销对应授权并停止服务；不能假设存在 auth.logout/uninstall 钩子。若宿主没有可靠通知，配套桥接需要验证只读检测自身账号/心跳的方案并说明检测窗口，否则不得宣称本地退出立即完成服务器撤销。服务器令牌到期与逐请求 grant 状态检查仍是底线。

## 5. 订阅可访问性、身份与调用链路

### 5.1 访问规则

- 新建统一 ClientAccessService，由目录、导入、额度和实际调用共同使用；不复用旧 service.options 的全局 Owner 清单而忽略 identity。
- 账号必须启用。本人应用归属使用平台 app_user 与 governance identity 的明确绑定；数据库目录启用时必须有有效人员关联，不能将用户提交的 email/header 作为人员身份。
- 平台 Owner 按现有显式管理/调用策略处理；普通用户首期只读取归属于本人或已有明确调用授权的应用。当前未发现可直接复用的完整应用成员 ACL，不能将同部门/同组织的人自动视作应用调用成员；若需要共享委派，应追加独立调用授权模型，不复用菜单管理员范围冒充调用权。
- 应用、订阅、网关与运行时必须启用，订阅 scope 存在，排除系统保留订阅；只返回平台认可的已发布路径。
- 模型目录取生效发布、应用模型策略、本人模型策略及模型/运行时状态的交集；保留现有「未配置」和显式 deny-all 的不同语义。权限不能由下载快照或历史登录固定。
- 订阅只剩图片模型时仍允许图片导入，不要求必须存在 chat 模型；文本目录可以为空但派生图片目录非空。只有所有已支持用途的授权模型都为空时才视为没有可导入模型。
- 每次实际调用再次检查 grant、用户状态、所选上下文和模型授权。目录短缓存只影响展示，不给撤销后的账号继续调用机会。
- 未绑定有效人员时可提示登录/关联状态，但用户模式不得静默降级到 service/unattributed 调用。此前服务订阅的历史 unattributed 数据不回填。

### 5.2 推荐调用架构

```text
WorkBuddy / 其他 Agent
  -> magpie 用户版供应商插件（协议转换 / 本地 OAuth 会话）
  -> Turnstile Client Runtime（校验用户、选中订阅、模型与 grant）
  -> 既有 APIM 已发布模型路由（服务端订阅凭据 + 可信用户上下文）
  -> Foundry / 其他供应商

APIM / observer -> 原有 Event Hub / PostgreSQL / 预算预留与结算
```

- 用户版 loader 指向平台 Client Runtime，而不是把 APIM Key 下发到本机。代理根据服务端保存的导入上下文确定网关和订阅；不接受任意 target_url、key 或可信人员头。
- Client Runtime 提供原生 Responses、Chat、Messages 和 Images 的受控代理路径。既有 `/api/v1/model-gateway/invoke` 绑定规则可复用，但其封装响应与浏览器 Session 不适合作为原生 SSE endpoint 原样复用。
- 服务器清除客户端 Authorization、Ocp-Apim-Subscription-Key、x-user-id、组织/预算身份头后，注入所选订阅的服务端凭据与短效签名上下文。签名绑定 user、application、subscription、gateway、model、request_id、iat、exp、aud 与 issuer。
- APIM 用户模式分支验证服务端身份及签名上下文，再派生人员/应用元数据进入现有 admission；不能只因为某个自报头存在就信任它。必要的 APIM 编译/发布变更必须独立测试，旧订阅请求分支不变。
- 预算预留只在一条权威链路进行一次：代理做授权/协议检查，APIM 复用预留/阻断/结算；不能代理和 APIM 各扣一次。若协议无法复用既有链路，必须先设计统一 settlement，不能部署双账本。
- 流式转发采用 async、有界缓冲和背压，保留请求取消、状态码、Retry-After 与错误语义；JSON 别名适配也必须有响应大小上限。
- 不自动重试可能已经接受/计费的文本或图片 POST；Access Token 在发送前刷新，收到 401 后提示重新登录或后续请求刷新，不盲目重复推理。
- request_source 标记用户插件，保存 application 与 canonical person attribution；不记录完整 prompt、图片、Token 或密钥。首次真实调用须检查人员预算和应用预算是否都归入正确范围。

## 6. 导入事务、替换范围与动态目录

### 6.1 导入和重新登录

- 登录用户身份固定，accountId/uid 不随订阅切换。借助宿主 settle 合并同 provider 的同一账号，防止每次导入创建 `provider#slot`。
- 每次导入创建版本化 prepare 操作：服务端重新验证目录，冻结 subscription_id、gateway_id、publication_revision、catalog_revision 和模型快照，返回新授权及 import_id。
- callback 成功保存新授权后，第一次 models/loader 调用激活 import_id；激活幂等，使用 generation/CAS，确保只有最新准备的操作能替换当前上下文。未激活的授权只允许确认/读取准备结果，不能发起模型调用。
- 激活后撤销本次安装的旧运行上下文，替换本插件目录与本插件拥有的派生图片配置；后续新请求只能走新上下文。已开始的旧请求保留原身份/订阅完成，不把结算转到新订阅。
- 如果宿主保存或本机配置应用失败，不能激活；旧导入保持有效。若服务端激活后本地确认失败，客户端查询幂等操作结果进行恢复，不以下载旧配置覆盖服务器状态。
- 配置拥有范围为本用户版 providerId、本插件状态目录、该 provider 派生图片配置；旧订阅版和其他供应商不在删除/替换集合。
- 普通导入不修改 Agent 选中模型；如 Agent 仍指旧模型，明确提示模型已不可用，不偷偷改为其他供应商。
- 清理旧缓存和生成配置，不删除平台审计、原始用量或预算记录。server audit 保留导入成功/失败和旧/新上下文 ID，不保留令牌或完整模型请求。

### 6.2 模型发现与别名

- provider.models 读取平台 `/catalog` 全量有效集合，可增加也可删除模型；不继续使用下载时交集。ETag 对应用户/权限 revision、导入 generation、所选发布及模型目录 revision。
- 每个模型有公开 alias、上游模型映射、用途 chat/image_generation、api_format/path、能力、known limits 和图片参数配置。未知限制标为未知，不把固定 8192 称为厂商最大输出。
- 文本协议复用平台现有路由规则。图片读取既有 image_profiles 与已发布 image binding；不能将普通 invocation_api_paths 的 chat 返回值作为图片协议。
- 默认目录缓存 5 分钟；用户点导入/重新登录强制重新读取权威目录。平台返回的新增、撤销模型在刷新后生效；实际授权每次调用实时检查。
- 网络故障允许保留同用户、同导入 generation 的上一份展示缓存并标注过期；401/撤销清空可调用状态，空合法集合保持空。不能混用另一个账号或旧订阅的缓存。
- 平台自动从公开 alias/upstream_model_id 生成返回名适配表，仅当返回值属于此绑定声明的真实上游 ID/已验证版本别名时映射公开 alias，并保留真实上游名称到受控诊断元数据。
- 非预期模型不覆盖返回名；SSE 仅修改声明的模型字段，不改 item/call ID、内容、usage、结束事件或任意字符串。须验证分片、UTF-8、超大事件与取消不引入流式缓冲。

## 7. 文生图兼容性与配套方案

### 7.1 已知限制

当前 generic provider plugin 会构造带 Account 的 Provider，而 `gateway/draw.go:Drawers` 对普通 Account 返回空清单，只有特定内置/迁移账号有例外。插件模型的 `image` 在宿主目录里主要表示图片输入，不是任意图片输出。因而不能承诺仅注册图片模型并放行 `/images/generations` 就自动进入 magpie 原生图片选择器。

P0 必须验证：显式命名插件图片模型能否经现有 draw 路径调用、发现/选择器能否呈现、协议是否误走 chat。即使显式请求成功，选择器不可见仍是体验限制，不算完整验收。未来若宿主已有公开图片注册能力可优先采用，但不能为了本功能改宿主源码或借用 `grok` 等内置 ID 绕过。

### 7.2 无宿主代码修改的推荐兜底

- 配套一个受控本地图片桥接，向 magpie 注册本插件拥有的普通 OpenAI-compatible 图片供应商；必要时同一桥接额外提供标准 MCP `generate_image` 工具供 Agent 使用。
- 派生 providerId 为 `<user providerId>-images`，只列出当前导入中的 image_generation 模型。它不是其他供应商，属于同一插件的替换/卸载集合；不把图片模型加入文本 Agent 列表。
- 使用 magpie 已公开的 CLI/配置接口添加和替换这一专属配置；自动登记能力在 P0 验证。不可用时提供清楚的手动登记步骤，不能偷偷批量改 Agent/MCP 配置或声称自动完成。
- 桥接只绑定 loopback，随机端口且要求高熵本地凭据；不得无认证监听 LAN。平台令牌不写到普通 provider 的 API Key 字段，也不出现在 MCP URL/query。
- 桥接的图片能力凭据由平台在当前用户授权下单独签发，绑定 grant family、安装、导入 generation、image-only scope 和到期时间；撤销/重新导入时失效。它不是 APIM Key、不能调用普通目录/其他订阅或管理接口。
- 桥接凭据由本插件自己的 0700/0600 私密状态保存、短期在内存使用，和主 OAuth Refresh Token 分开，不能让两进程同时轮换同一个 Refresh Token。续签只在主用户授权有效且当前导入匹配时发生；无法续签时提示重新登录，不降级到旧包内 Key。
- 如果桥接仅使用短效 capability，需要完成主插件与桥接的受认证续签 IPC；如果采用较长有效期的 image-only capability，必须逐请求检查服务端 family/generation 撤销状态。具体进程生命周期/续签策略是 P0 放行门，不能让桥接成为永久不受撤销影响的后门。
- 关闭/卸载插件后停止其桥接、撤销其能力、移除派生配置；保留其他 provider、用户生成图片和平台 usage/audit 数据。

### 7.3 图片协议与验收

- 平台 Client Runtime 图片路径代理到所选网关已发布 `/images/generations`，按平台注册 profile 校验模型、prompt、n、size、quality、format 等，不凭模型名字猜测可用参数。
- 返回 OpenAI Images 兼容 `data[].b64_json` 或平台允许的短效 URL；URL 的读取/过期需单独测试，不为返回远端 URL 盲目携带用户凭据。拒绝任意 URL 代理和重定向 SSRF。
- 限制请求/响应大小、图片数量和并发；不自动重试生成，超时和客户端取消保留请求标识，按照现有未决预算/结算规则处理，不直接释放不确定的预留。
- 验收覆盖真实图片预览/保存、实际 Agent 工具调用、正确人员/应用归因、模型别名、价格/usage 口径和能力撤销。没有图片 Token 的模型按平台既有计费规则展示，不编造 Token。

## 8. 用户额度与刷新

- `auth.usage` 向平台读取本人有效预算窗口，另外按需返回「当前应用共享额度」。组织/部门/个人/应用窗口独立，不能相加；没有合法可见范围的层级不返回。
- 响应包含 subject_scope、scope_id、unit、period_start/end、limit、confirmed_used、pending_reserved、finalized_upper_bound、available、enforce、as_of、source_watermark、stale 和 revision。
- 复用现有预算 evidence policy；`available = max(limit - confirmed_used - pending_reserved - finalized_upper_bound, 0)` 仅在该账本契约成立时使用，其他 scope 按其权威计算返回，不用 display totals 替代 enforcement。
- magpie window 使用 `amount/limit/unit/used/resetsAt/span`；`used` 必须明确是有效占用比还是已确认比，display 标注已确认与预留。不要一边显示 confirmed 百分比，一边称其为真正可用余额。
- audit 窗口设置 aside=true；block 窗口按真实可用性映射。未知、异常、stale 且无法验证的余额不能伪造为 0%/100%；返回说明或 error，并由服务端继续权威判断。
- 未配置额度表示「未配置」而非 0/0 或无限；服务端无限制/不阻断与缺数据分开。平台原始月周期不随用户查看时区变化；resetsAt 使用平台周期边界，magpie 可按本机时区显示。
- 初始建议额度读数 60 秒共享缓存、目录 5 分钟客户端缓存，刷新失败退避。key 至少含用户/权限 revision、grant、导入 generation、scope 与周期，不能跨用户复用缓存。
- 合并同一窗口并发读取；权限与实际调用授权不等缓存到期。额度/目录 GET 用 ETag/条件读取和服务端缓存，但敏感响应不得被公共 CDN/browser 共享缓存；返回 private/no-store。
- 5 秒宿主 UI 刷新不等于每 5 秒直接扫描数据库；手动刷新也需要限速。页面统一刷新频率是浏览器设置，不控制 magpie 宿主频率，两者共用服务端新鲜度契约。
- 此前生产诊断中的 PostgreSQL 为 Burstable 小规格，历史测量不能证明新增链路能承载；上线前重查实际规格/状态，并做索引查询、连接池、缓存命中、并发和故障恢复测试。初期不引入每客户端独立后台轮询或全目录扫描，不因刷新功能启动新付费基础设施。
- Token settlement 与 Event Hub 精确读数可能晚于请求结束；接口暴露更新时间/待结算状态，不宣称刷新后立即得到最终 Token，也不能因 UI 不刷新就停止后台预算控制。

## 9. 新接口草案与存储

以下路径和字段为待实现契约，不能作为已经存在的线上 API 使用。

| 接口 | 认证 | 作用 |
| --- | --- | --- |
| POST /api/v1/client-downloads/magpie-user | 当前浏览器 Session | 为当前用户生成无运行凭据的引导 ZIP |
| POST /api/v1/client-auth/device-authorizations | public client + 签名引导描述 + 限速 | 产生设备申请和短码 |
| GET /api/v1/client-auth/verification | 浏览器 Session / 引导到登录 | 展示申请、账号和授权范围；GET 不批准 |
| POST /api/v1/client-auth/device-authorizations/{id}/decision | 浏览器 Session + CSRF | 显式批准或拒绝，校验包绑定主体 |
| POST /api/v1/client-auth/token | device_code 或 refresh grant | 一次性兑换、轮换；OAuth 错误语义 |
| POST /api/v1/client-auth/revoke | 用户 token 或本人 Session | 撤销当前安装/family；幂等 |
| GET /api/v1/client-runtime/subscriptions | 用户 access token | 当前可调用订阅/关联网关，无 APIM Key |
| GET /api/v1/client-runtime/import-selection | 浏览器 Session + 非秘密 operation handle | 平台订阅选择/模型预览页 |
| POST /api/v1/client-runtime/imports | 同主体浏览器 Session 或用户 token | 准备导入；仅接受平台 ID、expected_generation |
| POST /api/v1/client-runtime/imports/{id}/activate | 此次准备的用户 token | 保存后幂等激活，CAS 替换并撤销旧上下文 |
| GET /api/v1/client-runtime/catalog | 用户 token + 当前导入 | 完整授权模型目录/ETag，不导出上游凭据 |
| GET /api/v1/client-runtime/usage | 用户 token + 当前导入 | 本人窗口、允许展示的应用共享窗口及新鲜度 |
| POST /api/v1/client-runtime/image-capabilities | 当前主用户授权 | 签发/续签本安装专属 image-only 桥接能力 |
| POST /api/v1/client-runtime/{responses,chat/completions,v1/messages,images/generations} | 用户 access / image-only token | 原生流式/图片代理；严格 scope 与已发布路径 |

- 浏览器导入提交和设备批准要有同源/CSRF；客户端 token endpoint 不依赖浏览器 Cookie，也不能粗暴复用所有 OwnerSession 管理接口。
- 不存在、他人或越权对象用 404/统一错误避免枚举；令牌过期 401、scope/已撤销授权 403、导入 revision 冲突 409、pending/slow_down 等设备状态按明确 OAuth 错误处理。
- 拟新增 device_authorization、client_grant/token_family、client_installation_import 和 client_operation 记录；image capability 关联同一 family/generation。必要的普通应用共享调用 ACL 单独追加，不复用归因表当授权表。
- 设备码/refresh/capability 长期记录只保存摘要，原始值只交给客户端私密存储；轮换结果有限重试所需的加密短期缓存是明确例外，过期即清理。用户 access token 可采用短效签名 Token，逐请求校验服务端 grant/用户/导入状态，签名密钥及既有 APIM/供应商凭据仍由平台现有密钥管理设施保存，不下发给客户端。
- 索引包括 token digest、family/install、subject + state、操作 deadline。轮换/激活有事务和幂等键；短期设备记录按 TTL 清理，长期审计仅留必要元数据，不保存完整目录请求或秘密。
- 不使用进程内 dict 作为跨 App Service worker 的授权真相；缓存是优化，PostgreSQL 是 grant/import 真相。清理复用现有后台能力，新增迁移只追加，不能改已执行 checksum。
- `auth` 由 magpie 的成功 callback/refresh 正常保存；插件自身缓存只写拥有的目录。跨进程本地写入要加锁、权限 0600、原子替换，不改整个 providers/auth 文件以清空历史。

## 10. 状态机与失败语义

```text
UNAUTHENTICATED -> AUTHORIZING -> AUTHENTICATED_NO_SELECTION
    -> IMPORT_PREPARED -> ACTIVE(generation N)
ACTIVE(N) -> REAUTHORIZING / IMPORT_PREPARED -> ACTIVE(N+1)
任一阶段取消/超时 -> 保留之前仍合法的 ACTIVE(N) 或未登录状态
ACTIVE -> EXPIRED / REVOKED -> 需要重新登录，不恢复旧 Key
```

- 新操作成功前旧配置有效；安全撤销优先于「失败保留旧配置」，被撤销的旧授权不能继续使用。
- 浏览器换账号、列表中订阅被删除、模型发布变更、请求并发、重复确认和 App Service 多实例都必须走同主体/CAS 检查。
- 同时打开两次导入只有一个能激活，另一个明确报冲突并重新读取目录；不得把旧选择的 Token 与新选择的模型混在一起。
- 登录状态、目录 freshness、导入状态、额度 freshness 分开显示；网络失败不等于未登录，登录成功也不等于存在可调用模型。

## 11. 验收与发布门

| 验收项 | 通过标准 |
| --- | --- |
| V1 回归 | 0.1.1 指定订阅 ZIP/认证/下载和已安装配置不变，与新用户版共存 |
| 双安装命名 | 同时解压/安装两版，技术包名和 providerId 不同；插件列表可区分且不出现同 provider 竞争服务提示 |
| 双版本生命周期 | 交替导入、重新登录、退出、停用、删除和升级一版，另一版的配置/账号/模型及标记 hash 保持不变 |
| 用户下载 | 普通用户自己的包无任何运行秘密；不能下载/授权给其他主体 |
| 平台认证 | 真浏览器确认 + magpie callback；短码猜测/重放/过期/取消均拒绝 |
| 刷新/撤销 | 单次 refresh 轮换、并发幂等、旧 Token 重放、禁用用户和 logout 有效 |
| 重新登录 | 原订阅仍授权则保留并更新目录；失权则清除上下文；失败不破坏合法旧选择 |
| 订阅隔离 | 两个用户的清单不同且各自正确；篡改 ID/URL/header 无法跨订阅 |
| 导入替换 | A -> B 后本插件 A 模型/派生配置移除，无副账号累积；其他供应商 hash 不变 |
| 失败恢复 | 取消/失败保留合法旧导入；保存失败不激活、激活确认失败可恢复 |
| 动态模型 | 平台新增/撤销后完整增删目录；没有授权的模型在实际调用时立即拒绝 |
| 别名 | 正常映射不误报，非预期上游模型保留警告，SSE 内容和 ID 不变 |
| 文本/工具 | 真实 WorkBuddy -> magpie -> 用户代理 -> APIM -> Foundry，多轮、流式、取消均正常 |
| 图片 | 实际生成/查看图片；原生选择器或配套桥接路径明确，关闭/卸载可清理 |
| 图片专属订阅 | 无文本模型但有授权图片模型也能导入、调用和读取正确额度 |
| 额度 | 本人和共享额度分开、预留一致、audit 不让 magpie 停用、未知不伪造余额 |
| 身份/计费 | 人员和应用 attribution 正确，预留/结算一次，无自动重复生成或推理 |
| 负载/故障 | UI 快速刷新被缓存合并，数据库连接/CPU可承载，断网与数据库不可用安全失败 |
| 生产上线 | 当前包/迁移/APIM基线、回滚包与 token family 撤销路径齐备；真实浏览器/Agent验证 |

发布顺序：P0 宿主兼容性 -> 后端追加迁移/认证/目录 -> 代理与 APIM 可信上下文 -> 用户插件/双下载 -> 额度/图片 -> 端到端 -> 上线。代理与 token API 可先 feature flag 关闭发布；没有用户身份验证和 APIM 单次预算闭环时不开放模型调用。

回滚：隐藏用户下载并停止新授权；撤销用户版 family/image capability，恢复已留存应用包与对应 APIM 策略，不重写 usage/budget 数据或删除新审计表。V1 插件仍可使用原订阅路径；已经撤销的用户令牌不得因回滚重新有效。

## 12. 实施前必须确认的能力门

- P0：实际 magpie 0.1.1194 的 OAuth callback、expires 单位、刷新保存、同用户 settle、模型刷新和注销行为；入口标签/已登录状态由宿主决定。
- P0：通用插件图片选择/显式请求能力；配套普通图片供应商的公开登记与卸载方式、桥接进程和续签 IPC。当前只核对源码，尚未确认哪条路径可端到端交付。
- P0：本地注销、插件停用/移除的通知能力；若没有公开钩子，验证只读检测或桥接心跳方案及其窗口，不承诺无法兑现的即时撤销/卸载。
- P1：平台 Owner/普通用户的应用可调用关系和人员绑定口径；共享 Member ACL 当前不能假定存在。权限目录和调用服务要共用明确规则。
- P3：APIM 可信用户上下文的校验机制、现有员工/应用模型与预算策略的组合、图片 settlement 和日志归因，不能只加客户端 x-user-id。
- P4：预算窗口真实字段、时区/周期、预留新鲜度、当前数据库容量与缓存行为；页面刷新配置不替代服务端认证或预算控制。

这些是实施放行条件，不是要求用户批准后才编写设计；本文件不将未验证宿主能力或尚不存在的接口标记为完成。

## 13. 参考

- [magpie 插件开发文档](https://usemagpie.ai/docs/zh/plugins)
- [OAuth 2.0 Device Authorization Grant / RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)
- magpie 本地源码：internal/plugin/host.js、internal/provider/pluginsignin.go、internal/provider/plugin_usage.go、internal/provider/plugins.go、internal/gateway/draw.go。
- Turnstile 源码：backend/http/client_downloads.py、backend/services/client_downloads.py、backend/services/client_plugin/、backend/http/model_platform.py、turnstile_core/integrations/gateway_protocol.py、turnstile_core/domain/image_profiles.py、turnstile_core/domain/application_access.py、turnstile_core/persistence/directory_store.py。
