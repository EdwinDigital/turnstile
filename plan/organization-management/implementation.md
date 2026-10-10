# 组织管理实施记录

## 当前状态

P1 数据库目录已发布、迁移并激活，真实线上 API 和页面工作流验证通过。
P2 同步、身份绑定、投影和独立 APIM 升级代码已提交并部署，但真实 Graph 同步及员工令牌
网关验收尚未完成；两个开关保持关闭。整个目标仍未完成，不把本地 mock 当作上线证据。
2026-10-10 独立身份策略候选已通过真实 Azure 编译及完整读回，未切换生产版本。

## 执行顺序

- [x] 核对需求、设计、分支、项目规范及原部署，保留已有规划资料。
- [x] 新增 `012_organization_directory`，不修改 001-011；补数据库迁移串行化。
- [x] 组织/部门/团队/人员、账号关联、部门授权、审计、稳定治理 ID、历史查询与范围权限。
- [x] 调岗预览摘要、未来周期批准保护、取消/冲突、月度继承协调和独立不可变 BFF 准入快照。
- [x] 同步连接/Group 映射、分段读取/分析、审核、删除保护、加密游标和整体批量应用。
- [x] 独立身份 Table 投影、ETag/单调版本/读回、失效上界及版本化 APIM 候选升级/回滚工具。
- [x] 新装/升级 CLI、三类运行包、配置/初始化/证据门禁、公共文档和 OpenAPI。
- [x] 本地完整命令集、真实 PostgreSQL 与主要浏览器验收后提交代码。
- [x] 原始三包及配置备份、数据库导出/隔离恢复、过渡版发布、追加迁移、回填/验证/CAS 激活。
- [x] P1 真实线上 CRUD、幂等、版本冲突、个人页、历史查询、原数据及 APIM 完整性核对。
- [ ] 真实指定 Entra 租户/Groups、最小应用权限及 consent、全量/增量/生命周期工作流验收。
- [ ] 真实签名员工 access token、动态 APIM 候选探针/切换/回滚和目录撤销传播验收。
- [ ] 线上跨月调岗、完整同步 UI/恢复以及最终 P2 合并验收。

## 提交

| 提交 | 内容 |
| --- | --- |
| `17c3d3d` | 组织管理、数据库目录、范围授权、历史适配、Entra 同步/投影及迁移/文档 |
| `ba65619` | 打包实测发现 Python 3.10 pip 回退错误，要求现有 Python 3.11+ 解释器 |
| `5db629d` | 真实 ARM 响应验证发现 raw XML/UTF-8 BOM，补 APIM 策略读取兼容及回归 |

尚未推送或创建 PR。新迁移已应用真实部署，不再修改 `012` 内容；后续 schema 修改须追加编号。

## 本地验证

- 完整回归：`1680 passed / 7 skipped`。跳过项不计作通过，新增目录真实 DB 套件未跳过。
  后续策略单行表达式及独立计划版本修正通过49项专项回归，并在提交前重跑完整回归通过。
- Ruff、mypy（232个文件）、frontend typecheck/build、diff检查通过。
- main、目录settings和directory-identity升级模板编译通过；main保留3条既有警告。
  APIM XML通过，OpenAPI lint通过并保留43条既有警告；目录schema引用遗漏已修复并单独重验。
- 真实隔离 PostgreSQL 16+：空初始化、并发迁移重跑、不可变旧checksum、历史账务保留、
  主部门重叠拒绝、部门管理员跨范围拒绝、团队成员去重、账号禁用与最后Owner保护。
- 10组织/1000单位/100000人员：分页、搜索及授权范围15次本地调用p95为0.3992秒。
  规模装载发现逐人advisory lock耗尽共享锁表，改为256个有界分片并保留人员行锁后通过。
- Graph mock + 真实DB：100对象分段、加密分页进度、完整来源后missing判断、240人多轮
  读取/分析/审核/一次set-based应用、期限/审批撤销/阈值/稳定键/lease恢复通过。
  不证明目标租户权限、真实分页或 Graph 最终一致性。
- 投影及策略：独立`I|1|cloud|tenant`分区，不写`Q/C/M/R`；旧版本不能覆盖停用、
  读回失败不确认outbox、5分钟失效上界、月份/预留时间冻结及发布组件保留通过。
- 本地真实浏览器：1440x1000/390x844 Owner目录CRUD/审计/关闭的Entra连接、
  真实Member仅见本部门并建团队、未保存取消来源切换、Copilot隐藏入口且35秒无目录轮询。
  页面无运行错误，不使用伪造 API 响应。
- `uv sync --frozen`/wheel下载遇到网络错误。开发检查使用原项目既有锁定环境；
  Linux/Python3.11运行包通过仓库pip回退构建并逐包核对`uv.lock`，未将失败半成品部署。
- 两项Node高风险审计来自Vite->PostCSS构建依赖nanoid/source-map-js，业务未引用，
  Python运行包不含node_modules；未自动更新无关锁文件。

## 真实发布与验证

真实目标、包/数据库SHA-256、账号、设置、租户、资源ID、what-if、截图和恢复证据仅保留私有。
本公共记录不包含环境秘密或客户资料。

1. 保存三类应用运行状态及完整settings、原private state/outputs、API实际挂载包、
   Flex两个实际released-package.zip及对应ActiveDeploymentId。所有包CRC/SHA-256验证通过。
2. 本机数据库连接超时，改在现有授权API宿主隔离工具目录执行pg_dump；未开放防火墙、
   未升级数据库或修改系统安装。352854字节备份下载后在本任务拥有的Unix socket数据库
   恢复成功，确认原链至011、原预算/用量/模型策略/账号/计费请求可读。
3. Flex备份需要临时两个精确部署容器的Blob Data Reader，独立what-if仅Create这两个权限。
   不授写权限，不开放storage网络或shared-key。读取后角色及临时凭据均删除并验证不存在。
4. 同提交构建三个Linux运行包，core digest/protocol一致；在隔离Linux目录索引验证，
   生产旧migration checksum匹配，发布/回滚/计费未决任务为零。
5. 维护窗口停止三类写者，追加012，保留001-011 checksum。发布legacy兼容三包。
   API健康/实际资源hash通过，Functions实际索引通过；短暂索引同步延迟独立记录。
6. 回填预览只采用既有目录和业务引用，物化/验证均幂等，完整计费基线通过。
   独立三份appsettings what-if/合并、全值读回和实际包manifest验证后CAS激活database。
   首次校验遇ARM `True/False`与预期小写不同，拒绝激活并恢复原settings/状态后修正读回逻辑；
   无关settings始终全值一致。最终恢复原Running状态。
7. 真实API：Owner密码登录、组织/部门/团队/人员CRUD、创建重试、409版本冲突、
   联系邮箱改动不变治理ID、个人账号/预算/模型/用量、历史目录和分布/明细查询通过。
   临时QA业务实体按测试流程归档，没有分配预算/模型/账号；原活动目录和预算额度保留。
8. 最终数据库核对19张预算/模型/用量/账单/预留/证据/应用治理表与回填基线完全一致，
   migration_count=12，APIM完整snapshot不变，三类应用Running且4+4 Functions索引通过。
9. 真实浏览器：目录树、人员、管理员/审计、Entra空态、历史候选、桌面/移动边界、
   Copilot隐藏入口且35秒无目录轮询通过，无console/pageerror。
   第一次30秒目录加载超时，重跑全部200；退出超时已独立复测，204后会话请求401，耗时0.2秒。
10. 独立APIM计划实测发现现有ledger URL双斜杠及APIM不接受跨行OData字符串。
    保留原source/operations、全部失败计划和非当前候选；补同host/table校验及单行过滤表达式。
    新`--plan-id`隔离证据并参与候选版本标识，共享环境锁，不覆盖失败记录。
    新what-if仅Create候选API及父policy，真实ARM编译、候选完整性读回通过。
    未promote；原APIM完整snapshot不变，API/Control-plane原Running状态恢复。

未将静态员工归因说成动态目录已生效。Graph/identity projection始终关闭，
person_admission_mode仍为unverified，因此线上不开放未经验证的调岗预约。

## 未验收与外部前置

- 目标控制台Entra client ID/允许域目前为空；没有已批准目录连接、目标Groups、
  应用Graph权限/consent或供网关测试的员工access-token客户端。
- 不以部署租户猜业务同步租户，不授tenant-wide Graph权限，不使用头像User.Read或CLI用户
  token冒充同步服务/员工探针。真实源配置必须由部署方明确。
- P2代码部署并不等于P2功能验收；独立policy prepare/promote/rollback及撤销传播需真实证据。
- 新ID进入生产后不能回退到旧seed-only包；保留匹配的database兼容三包。
  尚未实际演练生产包回滚或生产数据库恢复；隔离恢复和回滚准备不等于演练已完成。
- 整体目标保持进行中。缺失配置或证据不可被改名为“全部完成”。
