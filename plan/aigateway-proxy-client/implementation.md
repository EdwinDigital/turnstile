# AIGateway-Proxy 核心功能实施记录

日期：2026-10-11。用户更新：先不考虑精细权限控制，先实现核心功能验证。

记录说明：既有章节是 V1 指定订阅版的历史实施/验证记录，不是 V2 用户登录版的交付证据。2026-10-12 文档扩展见末尾「用户登录版实施计划」；本轮没有发布或调用模型测试。

## 实现

- 用户菜单新增下载客户端，跨数据源保持独立页面；可选择应用订阅、关联平台网关和主/备用密钥，查看对话模型并下载 ZIP。
- API 复用既有 OwnerSession，精细 Member 授权延期，不修改权限策略、数据库 schema、APIM 配置或模型授权。
- ZIP 内含 package.json、index.mjs、aigateway.config.json 和 README.md，解压后添加本地文件夹，使用「导入下载配置」登录供应商。
- 模型协议复用平台 gateway_protocol。Responses 原生调用；Anthropic SDK /messages 映射到平台 /v1/messages。模型发现使用 /v1/models。
- 模板保存在 backend/services/client_plugin，随 backend 完整部署；不需要 npm 依赖、安装脚本或修改 magpie 代码。

## 已验证

- 本机 magpie 0.1.1184；隔离 HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME，端口 3499/3500，不改日常 magpie 账号或 Agent 配置。
- 从现有线上应用/网关/模型元数据生成 cc-switch 私密包。真实 magpie plugin add、plugin login 成功，供应商显示 signed in、5 models。
- magpie provider test：GPT Responses 200，约 2696 ms；Claude Anthropic 200，约 2300 ms。
- 直接通过插件 loader 的真实流式调用：GPT 200 text/event-stream、10 data 事件且 response.completed；Claude 200、8 data 事件且 message_stop。
- 本地浏览器表单选择备用密钥、警告确认与 POST 下载成功，显示 Plugin downloaded；页面桌面布局已检查。
- 插件自动化测试包含导入、保留用户配置、流式响应原样返回、取消信号、凭据清除、重定向禁止、地址边界、动态模型交集、空模型和失败回退。
- 首轮完整回归 1781 passed / 7 skipped / 3 failed；失败均为新增目录/页面/API 文件需纳入架构清单，已修复并另跑相关 21 项通过。最终完整回归 1786 passed / 7 skipped / 1 warning，通过。
- 聚焦 API/前端/OpenAPI 28 项通过；Ruff 全仓库通过，Mypy 268 源文件通过，frontend TypeScript/Vite 构建通过，已有大包体积 warning 未在此功能扩展范围内优化。

## 发布准备

- 保留生产实际挂载包 20261010190024.zip，SHA-256 aa97fe250091ec452c88221d175ec52d3301787dd4063ee7a5f358ac6bf37769。
- 与生产包比较，core Python 和 migration 全部相同；backend 仅 api.py 改动及本功能新增文件。无基础设施变更，不运行整套 provision。
- requirements.txt 和完整 uv.lock 与现有生产包逐字节相同，因此复用该已部署 Linux .python_packages 依赖树；源代码和静态页面从当前提交重新构建，不复用旧业务代码。
- 使用 git archive、现有 stage_deployment 和 deterministic_zip 生成 API 包，校验 ZIP、模板、core digest 和 frontend hash。发布前重查生产挂载包，变更则停止，不能覆盖未纳入的并行发布。

## 生产发布与核心验收

- 已发布代码提交 625c01f，生产挂载包 20261011104724.zip，SHA-256 621442742f45c1cbf6d64dcbf8672269d88124ebf1b327191074175d2819b473，SCM 发布 HTTP 200，挂载 ZIP hash 与构建包一致。
- 真实 /health、密码登录、options、POST magpie 下载均 HTTP 200；下载 Content-Type application/zip、Cache-Control no-store。未登录 401，错配网关 404。当前生产接口实际读取生效发布及数据库，并非本地模拟。
- 线上静态资源 index-D_2J5BMT.js 与 index-hFpvtLnS.css 逐字节匹配新发布包。刚重启时旧进程短暂仍提供旧静态页面，待新进程加载后重新读取和验证通过，未重复部署。
- 真实 Edge 用户菜单 -> 下载客户端 -> 选择 cc-switch -> 勾选密钥警告 -> 下载插件，页面显示下载成功。控制台 error/warn 空，桌面及 390x844 手机视口验证通过，scrollWidth=390，无横向溢出；视口测试后恢复。
- 从生产 POST 下载的 ZIP 再导入隔离 magpie，5 models，登录成功。真实模型 provider test：GPT Sol Responses 1761ms、Claude Anthropic 1814ms、Kimi Responses 2343ms、GPT Astra Responses 4939ms、Grok Responses 2655ms，全部通过。
- 模拟外部 Agent 使用隔离 magpie /v1/chat/completions -> 插件 -> 线上 APIM 的完整路径：HTTP 200 text/event-stream，5 SSE data 事件，验证客户端格式转换与流式转发。
- 测试产生少量真实模型用量，按 cc-switch 归因；未改预算、模型授权、APIM 配置、数据库结构或其他登录。私密包、备份及证据保存在 Git 外；不上传包含订阅密钥的包到公共仓库。

## 当前范围限制
- 普通用户的应用订阅授权、真实第三方 Agent 文件改写、图像生成不在当前核心验证范围。
- 本地直连生产 PostgreSQL 因网络不可达失败；未开放防火墙。生产业务验证通过平台真实 API，而不是把本地快照当作生产数据库验证。

## WorkBuddy 真实 Agent 验证（2026-10-11）

- 按用户指定使用本机 WorkBuddy 5.8.0，不改 Codex 或其他 Agent 的连接。读取 magpie 本地 workbuddy.go 核对 models.json 接口，只临时追加一个自定义模型到 WorkBuddy，保留已有 11 项。
- 临时模型指向隔离 magpie 127.0.0.1:3499，模型 ID 为 aigateway-92bb81023bc454acb9c60f50ea715b1e/gpt-6.1-sol-foundry-d8c6686a；APIM 密钥仍只存插件私密配置，不给 WorkBuddy。
- WorkBuddy 新建「AIGateway-Proxy 联调测试」，明确要求不读文件、不用工具、不搜索，只回复测试标记。应用返回 AIGATEWAY_WORKBUDDY_OK_20261011 并显示已完成。
- 隔离 magpie 实际记录：agent=workbuddy，provider 为 AIGateway 插件，model 为 GPT Sol 平台别名，status=200、stop=completed、ms=3394，输入 544/output 22 tokens。request ID ef3a6b64-1245-4e0d-838c-312293f85983。
- 验证链路为真实 WorkBuddy app -> 隔离 magpie -> 生产下载插件 -> 线上 cc-switch APIM -> 模型；非模拟 HTTP 客户端。
- 测试后 WorkBuddy models.json 与备份逐字节一致，Codex config.toml 和日常 magpie providers.json hash 不变。恢复 WorkBuddy 原 GPT-6.1 Sol Copilot 选择和原未发送草稿，保留联调任务作为证据；停止本次启动的隔离 magpie 服务。
- 此次验收覆盖真实 Agent 文本/流式请求；WorkBuddy 文件编辑、多轮工具调用及其他模型在 WorkBuddy 中的使用尚未验证。原 5 模型 magpie 连通验收仍独立成立。

## 插件 0.1.1：名称和首次自动导入

- 两个手动登录入口改为「导入配置」「更新API Key」。版本提升至 0.1.1，下载页和 README 同步更新。
- magpie 未提供读取登录信息的 client.auth.get，因此自动导入仅在明确 project.id=magpie 的宿主内，使用其提供的 directory 及现有 plugin-auth.json.lock 互斥协议；不改 magpie 源码，不在 OpenCode 环境自动写入。
- 首次无该供应商账号时自动写入包内 Key，保留其他供应商账号及此前 auth 备份，原子替换权限 0600 的 auth 文件。一次性非敏感标记防止重载、主动退出和下载旧包后重新覆盖；已经有任意该供应商账号时仅记录已处理，不写 auth。
- 读文件失败、损坏 JSON、被占用的锁和写入失败时不修改现有登录、不删除他人的锁、不打印密钥，保留手动导入入口。跨宿主并发导入、手动轮换 Key、主动退出、已有副账号及权限 0600 均有自动测试覆盖。
- 真实隔离 magpie plugin add 后直接显示 signed in as cc-switch / cc-switch，无 plugin login 操作。退出后再运行 plugin 仍未登录；手动「导入配置」可重新登录。
- 模型复验：GPT Sol Responses 2408 ms、Claude Anthropic 1584 ms 通过。此前一次 GPT 测试遇到线上 429，未改限流，稍后复测成功。
- 本机 /Users/edwin/Downloads/aigateway-proxy 已原地更新模板、README 和 package.json 到 0.1.1；订阅配置未变，已有 plugin-auth.json 逐字节未变。仅执行插件 off/on 重载，不改 Codex、WorkBuddy 或日常 magpie providers.json，三者 hash 相同。
- Node 插件测试通过（含并发/损坏/锁定/写入失败）；聚焦 API/前端/契约 28 项通过。Ruff、Mypy 268 源文件及 frontend 构建通过。最终完整回归和线上新包验收进行中。

## 用户登录版实施计划（2026-10-12）

本节为计划，不覆盖历史结果；所有开发、迁移、发布和新版端到端验收均未执行。需求见 requirements.md，详细架构与验收矩阵见 user-mode-design.md。

| 阶段 | 内容 | 状态 / 放行条件 |
| --- | --- | --- |
| D0 | 更新双版本需求、认证/授权、订阅选择、目录替换、图片和额度设计 | designed；宿主限制及配套图片登记要求已记录 |
| P0 | 隔离 magpie 验证 OAuth auto callback、同账号替换、动态目录、图片选择器/桥接 | implemented / isolated validated；本地 fixture 验证，不等于生产验收 |
| P1 | 追加迁移、设备授权、令牌轮换/撤销、本人订阅目录与授权策略 | implemented / locally validated；019 已部署，旧数据计数及迁移 checksum 保持不变 |
| P2 | 独立名称/包名/目录/供应商 ID 的用户版模板、平台验证/订阅选择页、导入准备/激活、双下载页面 | implemented / deployed；双下载页面及真实 CLI 认证/替换通过 |
| P3 | 用户调用代理与 APIM 可信上下文、动态模型和别名适配 | implemented / deployed / validated；GPT、Claude 和工具往返通过，旧认证分支保持不变 |
| P4 | 个人/共享额度窗口、缓存和负载验证 | implemented / deployed；原生额度读取及真实 520 Token 增量核对通过，高并发未验收 |
| P5 | 文生图兼容性主路线或受控配套图片桥接 | implemented / deployed / MCP validated；真实 flare 图片通过，原生选择器仍需手动登记 |
| P6 | 新用户包真实 magpie / WorkBuddy 端到端验证及发布 | deployed / partially validated；隔离真实 magpie 通过，WorkBuddy 新用户版与全模型矩阵未单独验收 |

### 已核对的宿主能力与限制

- 本机 magpie 版本记录为 0.1.1194（2026-10-12）；OAuth 页面 URL、auto callback、`auth.refresh`、`auth.usage` 已有宿主实现，仍需对本次插件协议做隔离实测。
- `host.js` 存在 `ask(inputs)` 动态问题扩展，但 authorize 的参数传递和账号读取作用域需要验证。主流程采用平台选择页，不依赖未经验证的原生动态订阅下拉。
- 同 provider 的登录返回固定 `accountId` / `uid` 后，宿主 `settle` 可合并同账号；不能将订阅名称用作账号身份，否则每次切换可能新增账号。
- 通用插件模型的图片输入标记不是图片输出能力。`gateway/draw.go` 的 `Drawers` 会排除普通插件账号，当前不能承诺只增加 `/images/generations` 即出现在图片选择器。
- 当前指定订阅插件没有 `auth.usage`，本地元数据 `usage=false`；已有调用 Token 记录不等于额度卡片。尚未开发的用户版应补平台额度读数，而不是改 magpie 展示代码。

### 实施保护

- 冻结 V1 0.1.1 模板与包行为；新模板使用独立目录。新版不得自动改写现有指定订阅包、其他供应商、Agent 的模型选择或原始 usage 数据。
- 命名按 user-mode-design.md 3.3 执行：旧插件 `aigateway-proxy`，新插件 `aigateway-proxy-user`。只改显示名不足以证明隔离，必须验证 package.name、providerId、账号槽位、状态目录和导入/卸载拥有范围；不以重命名旧版安装目录消除冲突。
- 必须使用独立 HOME/XDG/端口做插件和桥接测试；生产包、数据库与 APIM 策略变更需留存基线，应用已执行迁移不可改 checksum。
- 未来上线前应检查 PostgreSQL 状态和历史事件、备份/恢复路径、Storage 访问及当前部署配置；不得通过开放 Storage、防火墙或降低既有认证绕过失败。
- 上线证据分别记录：implemented、deployed、validated、not verified；文档、源码检查、unit test、浏览器验证、真实 Agent/图片调用不可相互替代。

## 用户版开发与隔离验证（2026-10-12，发布前）

- 已实现独立 0.2.0 用户包、设备授权/平台确认、Token 摘要存储及轮换、同安装 generation 激活、本人订阅目录、动态模型、受限原生代理、别名适配和 `auth.usage`。
- 双下载入口与验证页已实现；英文/韩文/日文目录同步。旧 `backend/services/client_plugin` 的 0.1.1 模板未修改。
- 实际 magpie 0.1.1194 隔离 HOME/XDG/随机端口验证：动态模型、受控图片桥接及 MCP 工具调用成功；使用本地 1px 图片 fixture，不是生产模型验收。其他供应商值保持相同，退出用户登录后桥接停止。
- 桌面推理端口没有 GUI 配置写接口，不能自动登记原生图片卡片；当前交付生成私密 `images-mcp.json` / `images-provider.json`，需要一次配套登记。P0 中通过隔离 CLI 登记图片供应商后，magpie `/v1/images/generations` HTTP 200。未修改 magpie 源码或日常配置。
- 生产 PostgreSQL Ready，Standard_B1ms；18 个既有迁移 checksum 与源文件匹配。追加 019 后再次运行迁移逻辑无重复应用，前后原始用量和用户计数均为 207 / 12。未改旧表数据或旧 checksum。
- 创建非当前 APIM 修订 `client-user-20261012` 验证，不切现有流量。校验错误签名 401、模型不匹配 403、真实 GPT 请求 200，约 4.45 秒、28 Tokens。当前 API 修订未改变。
- 原始 APIM 表达式含单行注释，组合策略必须使用保留属性换行的 `parse_policy` / `serialize_policy`。普通 ElementTree 序列化的候选被 Azure 拒绝，未影响当前修订；修复后候选通过。
- 应用预算 admission 是认证之后的共用块，不再复制到用户分支；用户分支仅复用个人预留，应用原块仍执行一次。回归覆盖既有 Employee/Subscription 分支不变。
- 首轮完整回归 1815 passed / 7 skipped / 4 failed；迁移链清单、文档、32 个 locale 短语及 APIM 单测位置断言已修复。修复后的聚焦测试与类型/构建继续核对，不能把首轮失败当作完整回归通过。
- 最终完整 Python 回归 1819 passed / 7 skipped / 1 warning，通过。随后补充授权请求体限制、私密响应缓存头、失效 Refresh Token 提示及池关闭；相关 API/服务/持久化回归 212 passed。前端/契约回归 129 passed，Node 插件测试 2 passed，全仓 Ruff、新增模块 Mypy 和前端构建通过。
- 已备份生产实际挂载包、AppSettings 和 APIM 原策略到 Git 外；用户版尚未在当前生产 API 启用。正式发布与真实浏览器/Agent/图片模型验收另记录结果。

## 用户版发布与真实验收（2026-10-12）

- 平台后端、双下载前端、用户代理与 APIM 可信签名分支已发布；只新增 CLIENT_USER_ENABLED / CLIENT_PLATFORM_ORIGIN。既有员工/订阅分支、共享应用 admission 与核心运行时保持原样，未发布 Function 包或改其设置。
- 发布包逐文件核对挂载内容、实际入口/静态资源和认证业务读取；0.2.1 安装包的 index.mjs 与发布源文件核对。源代码提交依次为 13ec7e7（主体）、ee41687（动态 SDK 格式）和 10fb56f（Anthropic message_start 别名）；后续文档及无图片清理修补另提交。
- HTTP 真实验证：用户绑定 ZIP 无 APIM/访问/刷新令牌；首次登录、选择 cc-switch、取消保留旧选择、重新登录要求新平台会话、刷新幂等、旧 generation 撤销、原 0.1.1 ZIP 与新 provider ID 不同。
- 实际 magpie 0.1.1194 CLI 验证登录、导入和重新登录的 auto callback 与保存/合并；cc-switch -> Edwin-Agent -> cc-switch 的文本目录为 5 -> 2 -> 5、图片目录为 3 -> 0 -> 3，用户账号槽位始终为 1。订阅版账号和无关供应商配置不变。
- 测试配置使用私有 HOME/XDG/CODEX_HOME/CLAUDE_CONFIG_DIR 与随机推理端口。MAGPIE_NO_ACCOUNTS=1 会同时隐藏插件账号，真实文本联调不能使用该开关；隔离目录开启发现后，只出现测试供应商与宿主生成的聚合组，未切换日常 Agent 路由。
- GPT 普通/流式 HTTP 200，29 + 29 Tokens，客户端约 7.79 / 3.56 秒；Claude 流式 60 Tokens；GPT 工具调用与回填 80 + 105 Tokens，call_id 保持一致。别名只改已知上游名称，真实不同模型仍保留。
- 真实 gpt-image-2.5-flare MCP 调用约 24.67 秒，生成 1024x1024 PNG，平台记录 217 Tokens、估算 $0.005985。退出后配套服务停止；并非将图片自动加入日常 magpie 选择器。
- 六次调用总计 520 Tokens，均为本人而非 unattributed。个人窗口 21729 -> 22249，cc-switch 共享窗口 1452189 -> 1452709；原生 magpie quota 接口已读到两组窗口。首次额度快照因摄取尚未包含最后 105 Tokens，下一次缓存到期后对齐；缓存为 60 秒，预算拦截不依赖该展示刷新。
- 实际桌面模型回归发现并修复 Config.Model / Model.Info 形状差异；无图片订阅切换时，本机缺少配置写 API 不再阻断文本目录替换。
- 仍未验收：WorkBuddy 新用户版专门会话、所有 Foundry 模型及参数、多轮长上下文/取消/高并发/长期续签、生产图片专属订阅与普通用户目录矩阵。不得把隔离 CLI、HTTP/MCP 或 fixture 验证写成这些场景已全部通过。

## 浏览器授权 422 热修复（2026-10-12）

- 根因：页面 JSON.stringify 请求没有 application/json 头，浏览器自动使用 text/plain;charset=UTF-8，FastAPI 在 body 模型解析前返回 model_attributes_type 422；并非密码、订阅权限或 PostgreSQL 关停。
- 之前 HTTP 测试的 json= 参数自动补头，浏览器验收未提交决策，遗漏了这个客户端传输路径。新增真实组件回调 + request 客户端执行测试，覆盖三种目的的确认/拒绝和失败保留状态；后端同时复现 text/plain 422 / application/json 200。
- 提交 65b6b2b 已推送并上线。只替换 frontend/dist，1915 个非前端包文件及 AppSettings 逐字节不变；实际挂载包、入口/静态资产和认证业务读取已核对。
- 110 项相关 Python 回归通过（其中执行页面七种 Node 场景）；构建、Ruff 通过。真实 Edge 独立测试申请：请求 Content-Type application/json、响应 200、展示「已拒绝授权」。未操作用户自己的待处理申请。
- 独立 HTTP 登录、导入、重新登录决策/兑换均 200；目录 5 文本/3 图片，测试 Family 已撤销且后续 catalog 401。PostgreSQL Ready、Web App Running。
- 0.3.0 新流程尚在本节之后单独验证；不能把上述 0.2.1 热修复证据当作新流程验收。

## 0.3.0 开发与本地验证（2026-10-12）

- 已实现预授权下载、服务端默认订阅解析、一次性 OAuth 自动导入、空 URL「刷新模型」、重新登录解析默认绑定、OpenAI/Anthropic 可信 user 字段覆盖。V1 模板、019 迁移、APIM 策略和 magpie 源码未修改。
- 原生 magpie 0.1.1194、独立 HOME/XDG、本地 HTTP fixture：安装无需登录即有 OAuth；刷新只请求 refresh-models，不发设备请求、不打开浏览器；old -> new 完整替换且只有一个账号；退出后不会恢复，其他账号不变。fixture 不是生产推理验收。
- Python 完整回归 1829 passed / 7 skipped / 1 warning。随后补首次导入覆盖旧的未授权安装 ID/目录状态、兼容旧的未绑定授权及页面名称一致性；最终相关回归 150 passed，Node 11 个场景通过。全仓 Ruff、三个后端模块 Mypy、前端构建通过；已有大包 warning 未扩展处理。
- 新下载包包含个人 OAuth；必须勾选风险提示，POST acknowledged=true，ZIP 文件 0600、响应 no-store。默认绑定缺失时明确阻断；刷新不会偷偷切到其他订阅。已有本插件账号不自动覆盖，旧版未绑定账号需要一次「重新登录」。
- 生产发布与真实 0.3.0 ZIP/native 推理验证待后续证据，不把上一节热修复上线当作本版本上线。

### 0.3.0 生产验收发现与 0.3.1 修补

- eb411eb 已发布，挂载包、静态资源、预授权下载和默认 cc-switch 绑定读回通过。新用户 ZIP 含 OAuth、5 文本/3 图片；Core/V1/迁移/设置不变。
- 真实原生调用 GPT 200、24 Tokens；Claude 400。未把这个阶段标记为完整通过，测试 Family 已撤销。
- 原生日志发现入口新增导出 importBootstrap 被宿主当作第二个插件调用并抛错；单测未验证 loaded.error，已补模块边界/原生错误检查并把辅助函数移到 helpers.mjs。
- 独立真实 HTTP 复现 Claude 明确错误：metadata.user_id 不允许邮件，需要 UUID/hash。改为验证后身份的 SHA-256；APIM 签名治理身份与用量口径不变。
- 0.3.1 修补相关 Python 回归 150 通过、插件 Node 五项通过（入口断言为新增）；原生 fixture 加载无错误、自动导入/刷新/单账号/退出/其他账号保护通过。后续正式包与真实模型验收另记录。

### 0.3.1 正式上线与真实原生验收

- 代码 b10115a 已推送 feature branch 并上线，挂载包 20261012120609.zip；实际挂载的所有文件、线上静态资源及正式下载 ZIP 与构建源码逐字节核对。1910 个未覆盖非前端文件、Core/V1/019 和 AppSettings 不变；不发布 APIM 策略、Function 或基础设施。
- 实际 magpie 0.1.1194 + 正式 ZIP + 隔离 HOME/XDG/端口：loaded 无错误，无登录自动导入 OAuth；默认 cc-switch、5 文本/3 图片；刷新空 URL，无浏览器/设备授权；同用户仅一账号，订阅版及其他账号值不变。
- 经原生网关 GPT 200（客户端 6.755 秒，24 Tokens）、Claude SSE 200（2.705 秒，43 Tokens）、图片 MCP 200（14.283 秒，215 Tokens）。API 归因均为真实治理用户而非客户端伪造标识或 unattributed。
- 三条真实记录总计 282 Tokens；个人和应用共享窗口均精确 +282，已确认用量与原生 quota API 核对。展示仍有 60 秒缓存，不能当作逐 Token 推送。测试 Family 撤销后 catalog 401；本次启动的推理进程及图片子进程已停止。
- Edge 实际页面 v0.3.1、只读默认订阅 cc-switch、个人凭据风险确认、双版本入口正确；确认框能启用下载按钮。390x844 宽度/scrollWidth 均 390、无相关控制台错误；临时视口已清除。浏览器未点击会创建持久授权的下载/确认操作，正式 ZIP 验证通过独立 HTTP 会话执行。
- 本机 Downloads/aigateway-proxy-user 的旧源码与已知 0.2.1 提交匹配，已备份后更新入口/辅助模块/README/package 版本至 0.3.1；旧配置逐字节保留，未替换现有授权。当前 CLI 注册列表只见旧订阅插件，因此未强行重新注册用户版或改 Agent 设置；完整预授权配置需重新下载 0.3.1 包，旧配置更新可用「重新登录」。
- 仍未验收：WorkBuddy 0.3.1 专门会话、所有模型/多轮长上下文/取消/高并发/30 天续签矩阵。文生图仍需一次 MCP 登记，原生图片选择器和周围「登录」标题由 magpie 控制，不改其源码。

### 个人额度展示收窄（本地验证，尚未上线）

- 用户版 `/api/v1/client-runtime/usage` 仅返回本人个人月度 Token 窗口，不再读取或返回当前应用共享额度；没有个人预算时显示「未配置额度」。
- 保留 60 秒缓存、原始月周期、审计窗口 aside 标识及服务端真实用量口径；不修改默认应用绑定、APIM 阻断、订阅版、magpie 源码或原始数据库数据。
- 服务/API/订阅下载/APIM 策略回归共 30 项通过；Ruff、聚焦 Mypy 和 diff 检查通过。新增验证覆盖治理用户标识、audit/block、无个人预算不回退应用额度、额度查询不依赖应用订阅及 60 秒缓存。
- 本次为服务端响应收窄，插件接口与 0.3.1 下载版本保持兼容。发布后现有用户版在下次成功读取额度时生效，无需重新下载插件；本记录不代表已发布或已完成线上界面验证。

### 91a6880 无订阅方案（已撤销、未上线，历史开发记录）

- 用户目录不再查询应用订阅，个人代理不读取/发送 APIM Key；新 grant 只绑定 personal 模式与平台网关，旧用户 grant 运行时归一到个人路径，历史字段保留。订阅版不变。
- 个人签名带 scope=personal 与 gateway_id，不含应用/订阅声明；APIM v2 候选升级只替换客户端鉴权分支，保留原员工/订阅/公共应用/路由逻辑。线上只读 API 元数据确认 subscriptionRequired=false；实际当前策略尚未修改。
- accountId 显示经验证的用户名称、uid 保留 UUID；原生 magpie 隔离 fixture 确认 UUID -> Edwin -> Renamed Edwin 的刷新/重登录不增加槽位，其他账号不变，不修改宿主源码。
- 人员汇总 SQL 的无明细精确结算改为当月个人/部门预算层级读投影。真实 PostgreSQL 测试用 2579 Tokens 复现缺失路径，包含 canonical 明细到达不重复、跨月/其他人员/不确定结算排除和原始表快照不变。不是直接修改线上已用数；线上 2579 差值的明细级只读核对尚未完成（本机 DB 连接受阻、SCM 环境缺少 psycopg）。
- 全量 Python 1841 passed / 7 skipped，Node 插件 6/页面 7 项通过；Ruff、聚焦 Mypy、前端构建通过。Edge fixture 下载/个人授权页、桌面/移动端和风险确认交互通过，无相关控制台错误。
- 共享 Core 摘要改变，正式发布必须协调 API/遥测/控制平面包与 manifest/摘要，不可当作前端或 API 单文件热修。详细发布门槛及未验证项见 personal-authorization-design.md；APIM 隔离编译/负例、正式个人模型与 WorkBuddy 调用未验收。


### 0.4.0 默认订阅纠正（开发中）

- 用户明确要求沿用平台设计、不绕过订阅和不修改 APIM。已恢复客户端 v1 策略与订阅代理协议，移除 v2 策略生成器；91a6880 没有上线。
- 生产只读核查：调用测试默认网关使用 APIM_DASHBOARD_SUBSCRIPTION_KEY；常量时间比对确认它是 turnstile-dashboard 主 Key。平台没有个人默认订阅字段，个人预算和模型权限按治理用户标识管理。
- 默认选择改为复用平台凭据优先级并唯一匹配有效订阅，未知/歧义时失败，不再按应用名选择 cc-switch。名字/稳定 uid、个人窗口和历史数据保护保留。
- 线上无明细精确结算仅 492 Tokens，不能声称其解释全部 2,579 差值；进一步修复人员列表只统计所筛部门请求的错误范围，改为整月个人用量后筛选人员。纠正后的测试与发布结果单独记录。

- 纠正后定向测试 135 项通过，涵盖现有 Runtime 调用、订阅管理、用户插件 API、模型权限和真实 PostgreSQL；Node 13 项、聚焦 Mypy 与前端构建通过。
- 只读 SQL 已核对完整差值：19976（AI Platform）+1847（unattributed）+240（protocol-validation）+492（无明细精确结算）=22555。人员列表改为整月个人统计后筛选人员，保留原始部门/组织标签。
- APIM 策略源码与 91a6880 前版本逐字节相同；未创建 APIM revision、未写 Named Value、未发布策略。订阅版过滤 Dashboard/master/probe 的保护不变，只有用户版在服务端验证实际平台默认 Key 匹配后使用其默认订阅。

### 下载文件名修正（本地验证，尚未上线）

- 用户版 ZIP 使用 `aigateway-proxy-user-用户名.zip`，订阅版使用 `aigateway-proxy-订阅名称.zip`；名称缺失或本身为 UUID 时使用通用名称，不回退到内部 ID。
- 服务端从当前登录身份或已校验的订阅读取名称；响应使用 ASCII fallback 与 UTF-8 `filename*`，并向允许的跨域客户端暴露 Content-Disposition。前端保存 Blob 时复用该名称，不再自行拼接 UUID。
- 保留中文，规范化空格及不安全字符，限制名称字节长度。ZIP 内目录、provider/account ID、认证配置与订阅 Key 保持不变；不修改 magpie 源码、APIM 策略或历史用量数据。
- API、文件名及浏览器组件执行回归 56 项通过（包含新增 11 个 Node 场景）；前端构建通过。此记录不代表已发布到生产。
- 发布前完整回归：1855 passed、7 skipped、1 warning；全仓 Ruff、后端/Core/脚本 Mypy（152 个文件）及前端构建通过。生产 PostgreSQL Ready，19 个迁移已应用，没有待处理的网关发布/释放操作；完整包与设置备份及数据库隔离恢复验证单独留存在私有发布证据中。

### 0.4.0 与下载名称正式上线（2026-10-13）

- 发布代码 `4af982b` 已推送用户 origin 的 `codex/aigateway-proxy-client`。API、Telemetry、Control-plane 三个实际运行包逐文件读回一致，共享 Core 摘要 `d938910d0efd`；仅更新所需 Core 摘要，原配置和后台触发器状态完整恢复。两个 Function 各索引四个函数，触发器同步均 200；APIM XML、revision 和鉴权属性未修改。
- 兼容更新维护窗口前后，原始目录、预算、账本及迁移表指纹一致；109 张表的备份隔离恢复成功，19 个迁移校验一致，两次迁移无操作。恢复备份已加密，密钥在 Git 外单独保管；完整包、设置和发布证据保留在私有目录。
- 发布期间 Flex 停机上传的触发器同步、部署锁 409、瞬时管理连接超时及私有验收助手对可选步骤字段的假设造成重试，失败尝试已恢复匹配旧包和设置。最终采用完成回执检查、非重叠上传、原运行文件权限及运行状态同包激活完成验收；这些不是 APIM 策略变更，不把运行文件权限推测当作已证实唯一根因。
- 数据库策略的停止操作于上海时间 00:05:43 发起、00:09:46 完成；00:12 查询为 Stopped，启动后 00:19 查询为 Ready。数据库和应用恢复后，真实登录、目录及下载业务读取均 200；没有修改关停策略。
- 实际 Chromium 独立会话点击两个下载按钮，保存文件名分别为 `aigateway-proxy-user-Edwin.zip` 和 `aigateway-proxy-Edwin-Agent.zip`；用户版 0.4.0、订阅版 0.1.1，内部目录及稳定 ID 不变。1440x1000 与 390x844 验收通过，无相关应用错误及移动横向溢出；前端入口和静态资产与候选包逐字节一致。
- 新用户版绑定经平台凭据验证的 Turnstile Dashboard，显示 Edwin，只展示个人月度额度；刷新模型保留绑定。原生 magpie 使用独立 HOME/XDG/端口验证双插件共存、其他供应商配置不变、同用户单账号及友好名称。GPT 真实调用 200，客户端 10.211 秒，23 Tokens；平台请求 `8931b7e3-5fb6-4c28-8729-7c860188d0ef` 归因 Edwin，个人与人员预算均由 22555 增加到 22578。
- 测试授权已撤销并验证 catalog 401，隔离网关和图片辅助进程已停止；日常 Agent 配置、magpie 源码和历史原始数据未改写。现有授权仍保留旧绑定，需「重新登录」更新默认订阅；本轮未重跑 WorkBuddy、全部模型、真实图片及长期续签矩阵。
