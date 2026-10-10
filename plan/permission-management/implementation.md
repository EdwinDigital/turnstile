# 权限管理实施记录

## 当前状态

需求、设计、评审、实现、本地回归、提交及上线部署完成。生产验收通过。

| 任务 | 状态 |
| --- | --- |
| 当前架构、权限与数据查询调查 | 完成 |
| 需求/设计与冲突评审 | 完成 |
| 018迁移、策略CAS/审计与Owner API | 完成，增量升级/重复运行通过 |
| 自动数据scope与功能门禁 | 完成，补充跨层级与撤权回归中 |
| 四角色矩阵交互与多语言 | 完成，首轮桌面/移动端Light/Dark及键盘验证通过 |
| 多账号/API/真实PostgreSQL/浏览器与完整本地回归 | 完成：1768通过/7显式数据库测试另跑7通过；多角色浏览器通过 |
| 提交全部修改、备份、迁移与匹配三包部署 | 完成：094b14e，018已应用，三包文件读回一致 |
| 线上实际包、Owner工作流与数据/APIM基线验证 | 完成：Owner页面/同值保存/409冲突，旧目录账务保持，未发布APIM |

## 验证边界

使用真实本地PostgreSQL及cookie登录会话，模拟组织/部门/团队/普通用户和跨成员范围；
菜单授权不改变Owner写入、预算分配或APIM模型调用准入。旧全域报表对受限用户拒绝。
本地浏览器预览为http://localhost:5177；Browser插件未提供，使用已有Playwright/Edge。
首轮全量回归已发现并修正旧全域读取断言、目录facet截断及原生按钮样式问题；
最终结果和生产备份/发布证据将在完成后记录。真实Entra/AD同步、付费模型调用不计验收。

真实浏览器检查：Owner勾选保存/刷新持久化，恢复原矩阵；组织/部门/团队/普通用户
分别登录，人员列表4/4/1/1且权限管理接口403。桌面1440x1000、手机390x844、
Light/Dark无运行错误。临时本地组织/人员归档，矩阵恢复原值。
Ruff、Mypy（264文件）、前端构建、XML和Bicep编译通过；OpenAPI无错误，
保留项目已有警告。全量最终执行结果在发布前记录。

最终全量：`1768 passed, 7 skipped, 1 warning`；7跳过项为
`test_user_settings_store.py`需要显式数据库，另建隔离PostgreSQL补跑`7 passed`。
最后归档父级权限保护补充回归18通过；前端多语言/表格及权限API补充测试通过。

## 发布记录

- 功能及全部此前文档更新提交：`094b14e`，已推送
  `origin/codex/org-people-management`，未向upstream提交PR。
- 生产环境使用现有rgAIGateway/API/Telemetry/Control-plane配置。
- 发布前旧三包、完整settings、私有部署状态及数据库备份已保存，
  加密数据库副本和解密密钥分开保存在权限受限的私有目录。
- 备份根目录：
  `/Users/edwin/Development/turnstile/.turnstile/deployments/organization-management/17c3d3d/policy-rollout/094b14e/before`。
- 生产备份隔离恢复：18人员、27部门团队、12账号、128用量事实、3预算；
  隔离018升级及重复运行通过，历史表及旧迁移checksum保持。
- Azure Linux预检：旧数据库001-017匹配，新包包含018，三包core摘要一致，
  Telemetry/Control-plane各4入口，pending publication/release/billable均为0。
- 线上018已应用，唯一预期目录变化为permission_revision加1，新策略默认矩阵验证通过。
- 三包部署、逐文件读回及完整运行配置恢复通过；API、Telemetry和Control-plane
  均恢复原Running状态。core摘要：
  `97ff48ac3c42e7c84b59dba135c9e1eca87f0aa980072306e83ad440ccd3eb6e`。
- 发布日志`journal.json`为`final_verified`，未重复旧包在线回滚演练，
  已验证生产备份隔离恢复；旧包缺少新权限边界，只能维护模式恢复，不能直接开放Member。
- 客户9张目录表所有既有记录和历史账务基线保持；没有改APIM/provider配置，
  没有付费调用、线上模拟账号或实际菜单赋权变更。临时备份读取角色已验证删除。
- 线上https://aigateway.edwinai.in/ 的Owner权限页面、四角色矩阵、键盘/搜索/恢复草稿、
  Light/Dark/390px移动端通过；无页面运行错误。Owner同值PUT返回200且revision不增，
  错误revision返回409，匿名401，配置保持初始矩阵。
- 既有2份报表读取、归档分区总数、日/周趋势汇总、组织多选并集及请求/异常接口通过。
  历史用量仍128请求/37030 tokens，本次业务数据写入0。
- 私有证据：`permission-live-api-proof.json`、`ai-finops-live-api-proof.json`、
  `customer-after-proof.json`、`final-verification.json`及三包`backup-proof.json`。
- 本次真实Entra/AD同步、外部Provider/付费推理未测试，功能开关保持原值。

## 单菜单403修复

2026-10-11普通用户配置分布/趋势/治理/追踪后页面仍报`Menu access is not granted`。
根因：页面共用executive-overview、distribution、trends等统计API，上一版按接口名称
仅绑定单个菜单，没有覆盖实际消费页面。修正为共享读接口按其消费菜单授权并集，
不扩大DataAccessScope，不自动勾选管理总览，也不改预算/APIM/历史数据。
请求详情只允许追踪或治理；撤销所有相关菜单后共享统计API仍403。

主布局在没有组织管理菜单权限时停止查询目录能力，权限profile刷新保留。
新增六组普通用户“只开放一个菜单”真实PostgreSQL测试，验证本人一条请求、
同部门他人请求不可见、恶意user_id返回零、Owner/预算接口拒绝及撤权即时生效。
Playwright/Edge在隔离真实数据库逐页验证五个治理页面正常加载/统计只读本人，
用量分布切换人员维度正常；Browser插件未提供。全量与上线结果随后补记。

本地完整回归`1774 passed, 7 skipped`；7项显式数据库测试另跑全部通过。
Ruff/Mypy（264文件）、前端构建及diff检查通过。修复无新迁移，现有001-018保持。
