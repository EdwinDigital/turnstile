# 用户版 0.4.2 本地额度控制上线验收

## 发布来源

- 代码提交 `010d0f864416c34c217e83078fbc564fb43939fa`，已推送
  `origin/codex/aigateway-proxy-client`；订阅版保持 0.1.2。
- 只发布既有 API 包，实际挂载 `20261013081322.zip`。
  SHA-256：`498845ebbb58101f3c3e2bdebaa86f8e6b8fdba0848d389b11cc0e7abace65e5`。
  挂载 ZIP 与候选 ZIP 一致，首页及引用静态资源逐字节一致。
- Core 摘要仍为 `d938910d0efd637d2f47d9ea348e5281ef1522a3080985fef0bac9efcdd9dcef`；
  依赖输入/已打包依赖、19 项已应用迁移校验和不变。
- 未修改 magpie 源码、APIM XML/API 属性、Functions 活动部署、应用设置或历史原始数据。
  发布前后 PostgreSQL 均为 Ready，登录与真实业务读取通过，不以 `/health` 单独验收。

## 测试

- 全量 Python 1873 passed、7 skipped；发布前定向/契约复验 41 passed。
- 插件 JS 41 passed；Ruff、Mypy（4 个 Python 文件）和前端构建通过。
- 原生超额、只读锁定、图片/MCP、本地文件权限及策略竞争等模拟验收见
  [额度控制设计](quota-control-design.md)。超额测试没有修改线上用户预算。
- 真实单账号调用可能不被宿主原生路由额度提前拒绝，已通过插件派发检查补足：
  本地开启/超额时 429，模拟模型端收不到请求；不只验证图表或 aside 字段。

## 线上 ZIP 与实际原生宿主

- 下载用户版 `aigateway-proxy-user-Edwin.zip` 与订阅版 `aigateway-proxy-Edwin-Agent.zip`；
  文件名、版本、no-store、完整单价与模块内容均通过检查。
- 默认订阅仍为 Turnstile Dashboard；身份 Edwin、单用户账号、模型命名空间不变，
  用户版和订阅版共存，其他供应商和手动价格保留。
- `usage?fresh=true` 实际 Bearer 读取通过。配置选择/保存使用新鲜个人预算，普通轮询保留缓存。
- 在隔离 HOME/XDG/端口中，使用真实 `magpie plugin login <provider> 4` 开启本地阻断。
  平台仍返回 audit/aside=true，本地私有偏好为 enabled=true，原生 quota-history 自动生成。
- 实际 magpie web GUI 的用户额度卡片出现 quota-curve。截图展示 Edwin 的平台读数
  895,379 / 60,000,000 Token，只有新采样，不补安装前历史。
  该截图来自实际线上 ZIP 和真实额度 API，不是模拟预算或独立 HTML。
- 再用同一入口关闭本地阻断，偏好回到 false，平台 audit 策略没有变化。
- 一次真实文本调用：200，13.825 秒，输入 16 / 输出 7 / 总计 23 Token。
  原生与平台估算费用均为 $0.000169，unpriced=0。
- 平台请求 ID `ef16df1f-25e6-476f-bd38-192a35732b2e`；归属 Edwin 的治理人员身份、
  actor_type=person、request_source=aigateway-user、订阅 turnstile-dashboard。
  本机请求 ID 不同，通过调用时间、模型和精确 Token 匹配后再比较平台记录。
- 平台 created_at-ts 为 16.613 秒，包含请求处理时间，不是响应结束后再等待 16.613 秒。
- 价格并发同步曾遇到预期锁竞争；等待并通过模型加载重试后，所有完整价格回读一致，
  才执行真实模型调用。首次失败预验未调用模型，其授权已撤销。
- 未进行真实图像生成；图片/MCP 超额拦截通过实际 Bun 桥接和模拟平台验证。

## 浏览器与清理

- Browser 插件不在当前技能列表，使用已有 bundled Playwright/Chromium。
- 实际点击两个线上下载按钮，文件名正确；桌面 1440x1000、移动 390x844 无应用错误或横向溢出。
- 原生额度卡片和独立个人页面均渲染通过。独立页面保留，原生曲线不再只能依赖它。
- 测试授权撤销后 catalog 返回 401；原生网关、隔离 web GUI、图片助手及浏览器已停止。
  未修改日常 WorkBuddy/Codex 配置。
- 私有发布备份、包校验、对账、日志与截图在
  `/tmp/ts-plugin-release-quota-control-20261013/`；包含凭据的资料不提交 Git。

## 升级与使用

1. 下载新的用户版 ZIP，替换原用户版插件文件并在 magpie 重新加载插件。
   与 0.4.1 的命名空间相同，不需清空账号、历史或重新选择模型；更早的 UUID 命名包仍需显式替换。
2. 在供应商的插件配置方法中选择“额度控制”。平台 audit 时选择开启/关闭，默认关闭。
   平台 block 时没有可修改选择，只显示平台强制开启的说明。
3. 开启后保留宿主“走势”为非关闭状态，原生卡片开始记录真实采样；关闭不删除已有原生历史。
4. 此选项只增加本用户插件的本地提前拦截，不改变平台预算。缓存、并发和长请求仍可能
   导致本地读数滞后，不能用它代替平台的强制预算控制。
