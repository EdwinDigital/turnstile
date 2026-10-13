# 插件解压目录命名上线验收

## 发布来源

- 代码提交 `e63465b0f6e1d070713f8cf25e5eb20667455e87`，已推送用户仓库
  `origin/codex/aigateway-proxy-client`。用户版 0.4.4，订阅版 0.1.4。
- 沿用 API 包发布流程，实际挂载 `20261013111551.zip`，SHA-256
  `fd85ef4c54f275ca3848dd112bd550d39048d68c3d4905d7b5a6df9a8881c4f5`。
  挂载包、实际首页与引用静态资源均与构建包一致。
- 共享 Core 摘要仍为 `d938910d0efd637d2f47d9ea348e5281ef1522a3080985fef0bac9efcdd9dcef`。
  依赖输入、Linux 依赖树、Core 源码和 19 项迁移未改变，数据库迁移校验和保持一致。
- 未修改 APIM 策略/API 属性、Functions 活动部署、应用设置、数据库结构或历史用量，
  未修改 magpie/msai-client 源码或日常 Agent 配置。本次只修复下载 ZIP 的顶层目录，
  不代表此前 msai-client ZIP 安装名称/供应商可见性问题已修复。

## 安全与验证

- 发布前后 PostgreSQL 均为 Ready；登录、个人预算、目录与双版本下载选项读取通过。
  最终无待处理网关发布/释放任务，不以 `/health` 单独验收。
- 已备份原 API 包、应用设置及 APIM XML/API 属性，并保留原 API 包回退路径。
  新的 PostgreSQL custom 逻辑备份已加密保存，解密摘要和归档目录验证通过。
  `pg_restore --list` 不是恢复演练；本次无数据库结构变更，未进行新的隔离恢复测试。
- 发布前复验 89 项 Python/API/归档/部署打包测试、41 项插件 JS 测试通过；
  Ruff、Mypy（3 个文件）、前端构建及 diff 检查通过。保留既有 Starlette 和大 chunk 提示。
- Linux 候选 API 导入、版本及 19 项迁移校验和检查通过后，才执行 API 包发布。

## 实际线上下载与解压

| 下载文件 | ZIP 内唯一顶层目录 |
| --- | --- |
| `aigateway-proxy-user-Edwin.zip` | `aigateway-proxy-user-Edwin/` |
| `aigateway-proxy-Edwin-Agent.zip` | `aigateway-proxy-Edwin-Agent/` |

- 独立 HTTP 登录及实际浏览器点击下载均通过；两组实际 ZIP 均完成完整性检查和真实解压，
  顶层目录与下载文件名去除 `.zip` 一致。配置、模板模块、完整模型单价和预生成图标读回正确。
  ZIP 文件权限 0600，响应 no-store，HTTP 文件名没有内部 UUID。
- 默认订阅、用户身份和可读供应商命名空间保持原样；刷新模型仍使用已有授权和绑定。
  不进行真实模型调用，本次验证不产生模型调用费用。
- 实际下载页的新目录说明、页面身份、非空内容、无框架错误层及无应用错误通过。
  1440x1000 桌面和 390x844 移动视口通过，无移动横向溢出。
  Browser 插件未提供，使用已有 bundled Playwright；浏览器和测试授权均已清理，撤销后 catalog 为 401。
- 中文、特殊字符、超长名称、UUID/无效名称回退和实际解压由本地回归覆盖。
  本次没有重新验收原生客户端安装、推理及 ZIP 上传入口。

## 证据与升级

- 私有发布包、备份、校验、日志和截图在 `/tmp/ts-plugin-release-archive-names-20261013/`。
  实际 HTTP 解压位于 `smoke/extracted/`，浏览器下载解压位于 `browser/extracted/`；
  包含个人凭据或订阅密钥的资料不提交 Git。
- 新命名只影响新下载包。已按本地文件夹安装的插件需要同步更新宿主登记路径，
  不要把新旧目录重复安装为两个插件。用户自行重命名 ZIP 或浏览器追加序号不改变包内目录。
