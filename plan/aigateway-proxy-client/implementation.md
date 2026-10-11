# AIGateway-Proxy 核心功能实施记录

日期：2026-10-11。用户更新：先不考虑精细权限控制，先实现核心功能验证。

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
