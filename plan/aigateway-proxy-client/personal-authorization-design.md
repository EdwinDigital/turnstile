# 用户版个人授权修复设计（0.4.0）

## 范围与状态

本方案替代用户版 0.3.x 的「按应用名称排序选择默认订阅」和双额度展示设计。订阅版 0.1.1、magpie 应用源码和其他供应商不变。当前状态是开发完成、本地验证通过；APIM 当前策略和正式应用尚未发布本方案。

## 1. 个人授权与目录

- 身份来源仍为平台验证后的 OAuth grant。平台账号 UUID 负责登录、撤销和安装绑定，治理用户标识负责个人预算与模型授权；二者不能混作订阅 ID。
- 新下载配置为 version=4，用户插件版本 0.4.0；providerId、用户版技术包名和命名空间保持稳定，订阅版目录不变。
- grant.selection 仅保存 `{ "mode": "personal", "gateway_id": "<平台网关 UUID>" }`。个人网关来自平台唯一默认网关，必须启用、为 APIM、地址与受信任配置一致且有有效发布。
- 模型目录来自个人模型策略与当前已发布模型的交集，再校验模型/连接/供应商启用状态、角色和协议能力；支持文本及文生图。不读取应用归属、应用模型策略或应用订阅目录。
- 「刷新模型」使用当前个人网关，读取最新个人授权目录，替换本插件目录；「重新登录」重新验证身份并重新解析平台默认网关。网关不可用时拒绝，不能兜底选择 cc-switch 或其他应用。
- 旧用户 grant 的应用绑定只作为历史字段保留。运行时将它归一为平台默认个人网关，不改数据库历史 grant，不继续使用该应用密钥或预算。新订阅版 grant/目录不受此规则影响。

## 2. 账号显示与兼容迁移

magpie 将 OAuth 的 accountId 用作账号标签，按 accountId 与 uid 合并登录。新配置中 accountId 为经平台验证的显示名称（缺失时用邮箱），uid 保持平台用户 UUID。

插件使用宿主既有 plugin-auth.json.lock 锁，只更新自身 provider 命名空间中 uid 匹配的 OAuth 账号显示字段；兼容旧 accountId=UUID 且没有 uid 的账号。平台返回的 profile.id 必须与插件 userId 相同。在新登录结果交给宿主 settle 前更新旧槽位名称，避免重登录或改名增加重复账号。

迁移不更改 access/refresh、安装信息、其他用户或其他供应商，不创建不存在的账号，不恢复已退出的账号。入口仍只导出 default factory，辅助函数保留在 helpers.mjs。

## 3. 平台代理与 APIM v2

### 请求路径

Agent -> magpie 用户插件 -> 平台 Client Runtime -> APIM -> 已发布模型。

平台逐次验证 grant、当前目录身份、角色、个人模型策略、有效发布与实际网关路由。用户调用不读取、不传输 Ocp-Apim-Subscription-Key；拒绝依赖应用绑定。OpenAI user 继续由平台覆盖为治理用户标识；Anthropic metadata.user_id 为该标识的 SHA-256，签名上下文仍保留真实治理身份。

### 可信上下文

HS256 签名保持原有独立派生密钥、issuer/audience 和 60 秒有效期。新增 `scope=personal` 与 `gateway_id`，保留 Model.Invoke、model、jti、用户、组织和部门。移除 subscription/application_id 声明。

APIM v2 分支必须：

1. 验证签名、签发者、受众、到期时间、personal/Model.Invoke 和目标网关。
2. 拒绝附带 APIM Subscription 的个人请求及包含应用/订阅声明的上下文，防止应用预算重新混入。
3. 拒绝空人员/组织/部门/模型；校验请求 model 和 x-request-id 与签名声明相同。
4. 复用原员工分支的人员账本、模型准入、预留、审计/阻断和 TPM，保持可信身份头覆盖。
5. 原员工及订阅鉴权分支、公共应用账本/TPM、模型后端路由和遥测保持不变。没有 Subscription 的个人请求不会进入公共应用预算分支。

当前线上策略的员工/个人 TPM 为 100,000，本候选照常保留；解除应用绑定不等于取消个人突发限流或模型上游限制，也不会把当前个人 audit 模式自动改为 block。

`compose_client_user_policy` 用于模板上的初始组合；`upgrade_client_user_policy` 只替换当前已发布策略中的版本化客户端分支，遇到未知版本或歧义时拒绝。必须基于线上实际策略产生候选，不能用仓库模板覆盖线上模型路由。

只生成本地候选、不修改 Azure 的命令：

```sh
python -m scripts.client_user_personal_policy \
  --source /private/current-api-policy.xml \
  --output /private/personal-policy-v2.xml \
  --gateway-id 10000000-0000-4000-8000-000000000001
```

线上只读核查确认当前 API 的 subscriptionRequired=false，因此无需为了无订阅用户调用扩大 API 本身的匿名权限；策略仍要求有效个人签名或原有员工/订阅身份。

## 4. 预算显示口径

用户额度接口仅返回个人月度 Token；无配置则显示「未配置额度」。保留 60 秒缓存、平台 UTC 月周期和真实 audit/block 标记，不改变后台控制模式。

已定位的 SQL 差异：人员列表按部门过滤，而 `billable_request_effective_v1` 的非严格证据精确结算分支曾固定返回 unattributed 部门/组织。这类请求已有个人精确 Tokens，但可能尚未有 token_usage 明细，因而个人总额包含它、人员列表将它过滤掉。

修复在读取时按 attempt.period_start、个人预算 parent_scope_id 及部门预算父级投影该精确结算的层级；与既有内存仓库回退规则一致。没有当月个人/部门分配时仍保留 unattributed，不凭当前目录或其他月份归属猜测历史。其余原始明细、证据选择、实际结算和月份口径不变。

不新增 migration，不 UPDATE 原始 token_usage、账本、预算或结算记录，不为数字对齐制造或删除调用。

## 5. 验证与发布边界

已验证：

- 全量 Python 1841 passed / 7 skipped；包含真实本地 PostgreSQL 上无明细精确结算、明细到达不重复计数、跨月/其他用户/不确定结算排除及原始表快照不变。
- API 代理测试确认移除 Agent 伪造 user/key/headers、签名个人身份和网关、没有应用/订阅声明；没有任何应用也可调用，撤销仍生效。
- 安装的原生 magpie + 隔离 HOME/XDG + 本地平台 fixture：UUID 标签迁移为 Edwin，模型刷新及重登录/改名维持单一槽位，其他账号保留，无插件加载错误。不是线上模型连通性验收。
- 插件 Node 测试 6 项、授权页面实际请求客户端测试 7 项通过；Ruff/聚焦 Mypy/前端构建通过。
- 全量 Mypy 检查 286 个源文件通过。基于当前线上实际策略的候选对比确认，客户端分支以外 372 个节点的标签、属性与表达式均保持不变；CLI 输出从创建时起为 0600，尚未提交到 Azure。
- Edge 本地静态构建与 fixture API：下载用户版不显示应用选择、个人授权页无应用订阅，风险确认能启用下载按钮，桌面/移动端无溢出及相关控制台错误。移动端设置 390x844，实际 CSS 宽度/scrollWidth 同为 355；临时视口已清除。未通过浏览器创建线上持久授权。

尚未验证：APIM 隔离 revision 的 Azure 编译、负例和真实个人文本/图片调用，正式发布后的个人额度增长与 cc-switch 额度不增长，WorkBuddy 专门会话和长时间续签。

### 发布要求

- 本次 repository_budgets.py 属于共享 Core，core_runtime_digest 会变化。不能仅覆盖 API 单文件后沿用旧目录运行时摘要；须按现有发布流程协调 API/遥测/控制平面包、manifest 和 DIRECTORY_EXPECTED_CORE_DIGEST，一致性验证后再启用用户版。保留原依赖、Function 设置、迁移和基础设施。
- 保存当前包、AppSettings、策略及数据库/迁移校验基线；先验证隔离 APIM revision。未签名、错误签名、错误网关/模型/request-id、缺少身份及附带订阅请求必须失败；原订阅/员工调用必须正常。
- 在协调切换期间只关闭 CLIENT_USER_ENABLED；发布 v2 与相匹配的运行时，逐文件和摘要核对，恢复开关后创建独立测试授权。不要让 v1 后端和 v2 策略长期混用。
- 验收一次文本和一次图片调用：最终人员预算、人员列表、插件额度一致；个人 Token 增加，cc-switch 应用共享额度不因这些请求增加；确认缓存与摄取时延。只撤销测试授权，不改变日常 Agent 路由或用户已有授权。
- 回滚同时恢复客户端策略和匹配运行时包/摘要。预算读取修复不改数据，回滚不需要恢复或删除历史原始数据。
