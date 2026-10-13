# 分支初始化与兼容升级覆盖检查

日期：2026-10-13。检查范围为用户仓库的 `origin/main`，不是上游。

## 基线与结论

- `origin/main` 为 `2a58e152197fc377c48f5328df4c7169264607ea`，检查代码为
  `b33e31a564e3526cefcdfdceba1d23276eeea2fd`；main 是本分支祖先，分支领先 39 个提交。
- 检查开始时工作区无未提交代码，39 个提交已推送 `origin/codex/aigateway-proxy-client`。
- 数据库新装、019/020 增量迁移、Owner bootstrap、模板/图标打包和三包 Core 一致性已覆盖。
  不能据此宣称公共新装及兼容升级流程完整：存在下述两个公开入口缺口。
- 本次是源码检查、隔离 PostgreSQL 和离线构建验证，不执行 Azure provision、APIM 策略升级、
  生产迁移或模型调用；下面的缺口尚未修复。

## 缺口

### P1：新装复用已有 APIM 缺少平台 VNet 初始化

- `scripts/deploy.py::deployment_parameters` 在指定 existing APIM 时设
  `provisionApimService=false`，但新装仍为 `provisionPostgres=true`、`deployApimBootstrap=true`。
- `infra/main.bicep` 的 `platformNetwork` 与 `provisionApimService` 绑定；此路径不会创建平台 VNet。
  `infra/modules/data-plane.bicep` 已改为只引用 existing VNet 和子网。
- 因而一个真正的新平台、仅复用已有 APIM 的文档支持场景，会引用尚不存在的平台 VNet/子网。
  现有测试只验证了参数，未验证这一分支的完整网络依赖。
- `docs/deployment.md` 仍明确说明四个 existing APIM 参数可用于新建其他平台资源，
  不能把此路径等同于接管已有完整平台。
- 修复方向：分离“创建平台网络”和“创建 APIM”的条件，并对复用 APIM 的私有账本路径提供
  明确的网络接入/复用前置条件。不得默认迁移客户 APIM 的既有网络或覆盖未知子网。

### P1：动态应用 TPM 未接入公共兼容升级命令

- `scripts/application_tpm_upgrade.py` 只有 `plan_application_tpm_upgrade`，没有 CLI/main 入口，
  也未被 `scripts.deploy` 的命令分支调用；直接 `python -m scripts.application_tpm_upgrade --help`
  返回 0，但输出为空，不执行规划或升级。
- `scripts.deploy` 的公开升级仍调用 `plan_image_upgrade`；该函数校验旧 parent policy 时，
  `validate_parent_policy` 会为比较规范化 TPM，但计划返回的 parent policy 未应用 TPM 转换。
- 使用 `origin/main` 的实际 parent XML 和当前 canonical XML 离线复现：公开计划
  `required=false`，不包含 `effectiveApplicationTpm`；专用 TPM 规划函数则包含该变量。
- 新装模板及之后的模型发布编译器包含动态 TPM，但既有安装不应为了升级而被迫重新发布模型。
  更新数据库、配额账本及运行包，不会自动改掉 APIM 原来的固定订阅 TPM。
- 修复方向：将专用规划函数接入已有修订、预览、维护窗口、读回及回滚引擎，
  增加从真实 main parent XML 出发的 CLI/调度回归测试；不得另写覆盖客户策略的快捷脚本。

## 覆盖矩阵

| 项目 | 入口/实现 | 检查结果 |
| --- | --- | --- |
| 空库初始化 | `backend.migrate` 顺序扫描全部 numbered migrations | 001-020 全部执行；重跑为空 |
| main 数据库升级 | 同一个 migration runner | 仅应用 019/020；001-018 字节、校验和及应用时间不变 |
| 原始业务数据 | 追加表/列迁移；隔离历史样本 | 历史账号、会话、预算/用量及原菜单策略保留 |
| 首个 Owner | `backend.bootstrap`、API startup 的 `&&` 链 | 迁移成功后才 bootstrap；已存在账号不重置 |
| 用户插件登录表 | 019 的三个授权/安装表 | 新装为空；既有浏览器 session 机制不替换 |
| 下载权限 | 020、permission policy/service | 四组默认用户版允许、订阅版不委派；Owner 保持全权 |
| 插件/图标资源 | `stage_deployment('api')` 整树复制 backend | 双模板、定价模块、预生成图标及 019/020 包内完整 |
| 三包 Core | API/Telemetry/Control-plane runtime manifest | 同一 digest；普通 deploy 拒绝旧/mixed digest |
| 运行包文件权限 | 公共 `deterministic_zip` | regular files 为 0644、可执行文件为 0755；不是私密插件 ZIP 的 0600 |
| 常规完整新装网络 | platform-network、data-plane、APIM 模块 | 新建 APIM 的模板链及 Bicep 编译通过；未另建付费 Azure 实例验证 |
| 新装复用已有 APIM | public deploy existing APIM 分支 | 缺口，见 P1 |
| 既有 APIM 网络修复 | `scripts.apim_ledger_hotfix` | 有独立 plan/apply、漂移/越界/no-op 检查；前提为既有私有账本/权限 |
| 既有 APIM 动态 TPM | 专用规划 helper 与现有修订引擎 | helper 存在，公共可执行入口缺失，见 P1 |
| 用户版启用 | `CLIENT_USER_ENABLED`、`CLIENT_PLATFORM_ORIGIN` | 默认关闭；仍需批准后的手动配置，不等于迁移成功就可下载 |
| 统一刷新频率 | 浏览器 localStorage 的版本化 key/default | 无新增数据库字段，不需要 SQL 升级 |
| 企业自定义图标 | 预生成脚本及共享目录接口 | 默认资源已打包；企业上传/持久共享目录属于单独配置流程 |

新装/升级不得自动创建测试模型、模拟用量、替换客户密钥、重新授权个人模型或改写历史归因。
用户版启用步骤在 testing/设计文档有说明，但部署文档还应汇总明确的配置及验证清单。
数据库目录已激活时，三包维护升级按 `docs/organization-management.md` 的兼容流程执行；
现有 digest guard 不是自动协调升级器。新增 019/020 后，不能沿用仅核对 18 项的旧无迁移示例。

## 验证证据

- 194 项迁移、bootstrap、打包、部署 guard、网络及 TPM 测试通过；保留既有 Starlette warning。
- 独立私有 Unix socket PostgreSQL：空库跑 20 项并重跑；真实 main 18 项加历史样本，
  仅升级 019/020 并重跑，验证历史与原菜单字段及迁移校验和/时间保留。
- main、APIM network upgrade、subnet 三个 Bicep 入口编译通过；编译器保留长度/边界推断 warning，
  编译成功不代表 existing APIM 场景的资源依赖齐全。
- 实际 stage 三包并用公共 ZIP 工具打包：20 项迁移和客户端资源读回正确，
  三包 Core digest 均为 `4c6eed6c867c85f8352519a2ac43841a178b08f923e3ff81bfaa1a7d81e1f8f3`。
- 私有复现脚本、结果和构建证据在 `/tmp/ts-branch-upgrade-audit-20261013/`，不提交环境凭据或安装包。
- 测试通过仅证明已覆盖的路径，不抵消两个未被现有测试验证的公开入口缺口。
