# 客户端下载按钮上线验收

## 来源与范围

- 代码提交 `5725dd78eed900a35766cb14146031e4864eda0f`，已推送用户仓库
  `origin/codex/aigateway-proxy-client`，未合并 main 或推送上游。
- 沿用既有 API App Service 代码包发布路径；2026-10-13 23:35（Asia/Shanghai）提交发布，
  23:38 完成挂载包及静态资源读回验证。
- 实际挂载包 `20261013153531.zip`；候选包和挂载包 SHA-256 均为
  `d768a61d0a684dcc7a5f26b547c773a9c27811b13a1aa191b09e6b1aa0d4d212`。
- 仅替换平台前端；1930 个非前端文件逐字节保持不变，含后端、Linux 依赖、20 项迁移及
  runtime manifest。通过公共 `deterministic_zip` 打包，普通文件 0644、可执行文件 0755。
- Core digest 仍为 `4c6eed6c867c85f8352519a2ac43841a178b08f923e3ff81bfaa1a7d81e1f8f3`。
  API/Functions 应用设置、Functions 活动部署、APIM XML 原始字节及 API 属性均保持不变。
  没有修改 magpie/msai-client、日常 Agent 配置或插件版本。

## 用户可见变化

- 下载客户端页面标题右侧显示高亮客户端下载按钮；浅色主题黑底白字，深色主题遵循平台主色
  高对比配色。保留键盘焦点及「其他版本」入口，桌面靠右，窄屏自动换行。
- 按系统切换 macOS、Windows、Linux 按钮文字；可用时读取浏览器 Client Hints 选择 ARM/x64。
  移动/未知系统及无法可靠区分处理器的 Mac 跳转官网版本选择页。
- 直接使用 `https://usemagpie.ai/download/...` 官方稳定入口，不写死客户端版本或代理安装包。
  其他版本入口为 `https://usemagpie.ai/zh/#get`。
- 按钮不依赖插件目录查询完成，也不新增平台 API 查询；原双版本插件下载与权限控制不变。

## 本地与线上验证

- 252 项 JavaScript 测试、TypeScript/Vite 构建及 diff 检查通过；保留已有大 chunk 提示。
- 本地隔离页面验证九种系统/架构场景、目录加载期间可用性、键盘跳转、浅色/深色主题以及
  1440x1000、390x844、320x844 布局，未发现应用错误或横向溢出。
- 线上当前 Mac 的真实浏览器识别为 Apple Silicon，链接为 `/download/mac-arm64`。
  另以浏览器信息模拟 Windows x64/ARM、Linux x64/ARM、Intel Mac、无 Client Hints 的 Mac
  及 Android，按钮文字和目标地址符合预期；不是对应设备的真实安装验收。
- 线上键盘激活验证新标签页目标地址、原页面不跳转及 `noopener noreferrer`；浅色/深色主按钮
  配色、双插件下载仍需确认、桌面/手机布局和控制台检查通过。
- 官方六种架构下载入口 HEAD 均返回最新安装包的 302 重定向。浏览器测试拦截官方下载导航，
  没有实际下载或安装客户端、导出插件授权、调用模型或修改权限。
- 线上首页及其引用的 JS/CSS 与候选包逐字节一致。真实登录、身份、个人预算、组织目录、
  权限策略及双版本下载目录读取通过，不以 `/health` 单独验收。
- 发布后 PostgreSQL 为 Ready，20 项迁移校验和、原权限策略及用户默认订阅保持不变；
  无待执行的网关发布/释放任务。测试浏览器已关闭，测试登录 session 已注销。

## 备份与边界

- 保留原 API 包 `20261013141147.zip`、原应用设置和 APIM 快照，供包级回退。
- 新的只读 PostgreSQL custom 备份已加密保存，归档目录和解密摘要验证通过。
  本次无数据库结构变化，没有再次执行隔离恢复；不将 `pg_restore --list` 描述为恢复演练。
- 私有包、配置、备份、核验和截图位于 `/tmp/ts-client-download-button-release-20261013/`，
  不将凭据、安装包或加密备份密钥提交 Git。
- 本次不修复初始化/升级审计中发现的两个公开入口缺口，也不重新发布 Functions 或 APIM 策略。
