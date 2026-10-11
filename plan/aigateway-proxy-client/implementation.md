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

## 待完成

- 实际生产 API/frontend 发布、线上页面和下载验证、从线上包再次导入并调用真实 magpie 网关。
- 普通用户的应用订阅授权、真实第三方 Agent 文件改写、图像生成不在当前核心验证范围。
- 本地直连生产 PostgreSQL 因网络不可达失败；未开放防火墙。生产业务验证通过平台真实 API，而不是把本地快照当作生产数据库验证。
