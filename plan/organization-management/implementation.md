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
| `0de0394` | 真实APIM候选编译修复、集合查询失效分类、独立计划恢复及发布说明 |
| `a830e44` | database目录普通发布前digest门禁、真实兼容回滚记录及验收清单 |
| `24f05c3` | Group映射独立Field与标签修复、范围Member及本地完整同步页面验收 |
| `ba94fdc` | 十万人审核同步应用的临时索引/统计优化及失败原子回滚回归 |

尚未推送或创建 PR。新迁移已应用真实部署，不再修改 `012` 内容；后续 schema 修改须追加编号。

## 本地验证

- 完整回归：`1694 passed / 7 skipped`。跳过项不计作通过，新增目录真实 DB 套件未跳过。
  后续策略单行表达式及独立计划版本修正通过49项专项回归，并在提交前重跑完整回归通过。
- Ruff、mypy（236个文件）、frontend typecheck/build、diff检查通过。
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
11. 同步发布`0de0394`三个兼容运行包及相同core digest。重新备份实际三包、
    完整settings及443395字节数据库，并隔离恢复已激活database目录和全部12条迁移。
    独立what-if仅修改三份appsettings，暂停实际写触发器；三个阶段分别读回真实包、
    同步trigger、索引4+4 Functions、Owner登录和目录查询，并比较全部目录表、
    schema记录与19张计费治理表。新版、备份兼容包回滚、最终新版再发布均通过。
12. 演练前几轮真实失败均保存私有记录并恢复兼容包：停止Flex时上传成功但trigger sync
    返回403，启动后必须同步/索引；验证脚本误把组织list当分页、长窗口管理token过期。
    修正脚本并刷新token后完整演练通过。数据库没有降级或生产恢复，未切回legacy。
    最终APIM完整snapshot不变，三类应用Running，原无关settings及触发器配置完整保留。
13. 最终`0de0394`线上浏览器重验通过：1440x1000和390x844、目录/管理员/审计/
    历史/Entra空态、Copilot切换和35秒无目录轮询。无console/pageerror，
    logout=204后auth/me=401。没有使用响应拦截或Graph mock作为线上证据。
14. 普通`scripts.deploy`补上传前保护：database模式三类运行时必须具有匹配当前源码的
    expected core digest，拒绝混合权威源或旧digest，提示走兼容维护流程。
    53项专项及完整1684项回归通过，Ruff/mypy通过。此修正只影响本机发布工具，
    不改变已发布core digest；三包无须因文档或部署器更新重复发布。

## 验收核对

下表区分真实本地数据库/API/浏览器验证、生产验证及尚缺证据的部分。
“部分”不等于该AC整体通过；单元模拟不证明Graph或员工数据面。

| 验收 | 已有证据 | 仍未验证 |
| --- | --- | --- |
| AC-01 | 本地Owner/Member及无权限API，线上Owner/范围Member桌面/移动入口 | 无 |
| AC-02 | 本地持久化/显式账号关联，线上CRUD及三轮重启数据保留 | 无 |
| AC-03 | 真实生产19表计费治理基线及全部原迁移保留 | 无 |
| AC-04 | 本地新人员预算解析/多团队去重 | 线上新人员预算及模型完整流程 |
| AC-05 | 本地未知/归档/改名聚合，线上历史候选及查询 | 线上含真实使用记录的改名归档流程 |
| AC-06 | 本地跨月/容量冲突/准入锁/预览漂移 | 线上跨月执行；admission仍unverified |
| AC-07 | 本地真实DB/API范围、分页及助手查询；线上独立Member跨范围人员/预算/应用访问拒绝 | 线上助手参数与历史请求ID篡改 |
| AC-08 | 本地会话/缓存/禁用/投影期限，线上logout失效 | 真实员工网关撤销传播 |
| AC-09 | 页面明确静态归因待对齐，真实候选编译及原policy保留 | 签名员工token及伪造头探针 |
| AC-10 | 本地Graph mock+真实DB稳定键及碰撞检查 | 指定真实Entra对象生命周期 |
| AC-11 | 本地分页/403/429/delta/删除阈值保护 | 真实Graph失败/恢复与分页 |
| AC-12 | 本地迁移/回填/同步幂等，生产迁移/回填幂等及计费保留 | 真实Graph重复同步 |
| AC-13 | 本地及线上Copilot隐藏、来源切换、35秒无目录轮询 | 无 |
| AC-14 | 本地空库/真实Owner登录/首个目录/重启验证 | 第二个独立空Azure实例 |
| AC-15 | 生产备份/隔离恢复/批准回填/验证/CAS激活 | 无 |
| AC-16 | 本地真实并发迁移和中断回填，生产不可变checksum | 生产多实例并发故障注入 |
| AC-17 | 真实三包相同digest、4+4 Functions、关闭独立开关、ledger不变 | 无 |
| AC-18 | 真实兼容包回滚/再发布；目录全表hash及计费基线保留 | 动态身份启用后的撤销/回滚 |

补充验收：真实生产独立Member账号只见临时部门，桌面/移动端可创建团队；
跨部门人员GET/PATCH、部门人员预算及Owner-only批量预算/账号/连接/应用/Copilot请求拒绝。
撤销管理员后API立即拒绝，页面35秒内显示无权限且移除目录内容。临时账号停用、
权限撤销与目录归档后，原活动catalog和已有预算额度不变；未调用模型。
前几次脚本定位/响应比较与30秒网络超时不计通过，独立最终重跑及清理证明保存私有。

本地Graph fixture与真实PostgreSQL/API/worker/浏览器完整同步审核链路通过：
新增连接、Group映射、验证/启用、全量预览/批准应用、增量改名保持UUID及治理ID、
来源缺失超过20%双重批准、source disable保留历史且不创建登录账号。
此次发现Group映射行共享Field导致重复控件ID及错误标签，拆分独立Field并补回归；
桌面1440x1000和移动390x844截图、控件ID唯一及完整链路重跑通过。
真实Graph权限、分页和员工token仍未验证。

同步应用规模补验：此前仅240人应用，追加真实PostgreSQL十万人审核staging测试。
首次审计写入超过20秒语句期限并完整回滚；定位临时表缺索引/统计后，添加仅事务
生命周期的object/person索引及ANALYZE，十万人完整应用耗时10.8419秒，审计及任职各
十万条，不创建账号/预算。另以审计trigger注入应用中途故障，证明人员、任职、
外部绑定、目录版本及checkpoint全部回滚，job记录failed。未修改已应用012迁移。
该结果不是真实Graph吞吐或生产延迟承诺。

`ba94fdc`已完成兼容三包发布：保存现网实际三包、完整settings和458214字节数据库，
隔离恢复database/active目录与12条迁移通过。Linux逐包依赖/旧checksum/4+4索引、
未决发布与计费任务检查通过。三份appsettings what-if仅修改精确目标，维护时暂停
实际写触发器，上传全部三包后在触发器禁用下启动/同步/索引，再读回真实包相同digest，
恢复完整原触发器配置。Owner登录、目录查询、全部目录表及19张计费治理基线保持不变；
最终三应用Running，APIM完整snapshot不变，Graph/projection仍关闭。回滚三包和配置
已备份，沿用此前真实兼容回滚证明；本轮没有再做生产数据库恢复或动态身份切换。

`24f05c3`前端修复已发布：新旧API实际包和完整settings备份，core digest与两个Functions
保持一致，因此仅更新API包，没有schema或基础设施变更。首次健康先于新资源切换，
资源检查拒绝并自动回滚；独立重试等待实际新资源后，包SHA-256及settings全值读回通过。
线上真实页面验证Group输入/两个选择器的独立标签、无重复ID、桌面/移动布局、
关闭开关时全量/增量按钮禁用。仅保存明确标记的停用QA连接，无Group映射、
无Graph验证或同步调用；未将部署租户当成已批准业务源。保留停用连接作为UI审计。
全部5个临时范围账号独立DB核对为disabled，关联人员archived/manual_disabled，
有效部门授权为0。原预算/模型/用量19表及APIM完整snapshot仍保留。

未将静态员工归因说成动态目录已生效。Graph/identity projection始终关闭，
person_admission_mode仍为unverified，因此线上不开放未经验证的调岗预约。

## 用量查询与组件回归

2026-10-10 用户截图中的总览“用量数据加载失败”和异常治理500已在真实自定义域复现。
相同查询窗口的overview/distribution/anomalies/requests正常，仅
`observability/trends?group_by=none`的小时/每日聚合500。真实PostgreSQL诊断为
`array_agg(unknown) is not unique`：目录改名聚合改为最新名称array_agg时，
未分组常量`all`未显式指定text类型。修复常量类型，不改历史用量或计费事实。
新增真实DB/API回归覆盖hour/day/week、UTC/Asia/Shanghai、范围Member及Copilot隔离。

组织管理28px按钮原使用24px lucide图标，菜单触发器被包装后出现原生边框；
补Button尺寸/variant元数据及本模块局部样式，28px操作配14px图标、30px菜单项，
保留键盘焦点/悬浮提示/禁用态，不改变其他模块全局尺寸。本地桌面/移动端菜单与
编辑弹窗已用真实API验证，生产验证结果待本轮发布读回。

## 未验收与外部前置

- 目标控制台Entra client ID/允许域目前为空；没有已批准并验证的业务目录连接、目标Groups、
  应用Graph权限/consent或供网关测试的员工access-token客户端。
- 不以部署租户猜业务同步租户，不授tenant-wide Graph权限，不使用头像User.Read或CLI用户
  token冒充同步服务/员工探针。真实源配置必须由部署方明确。
- P2代码部署并不等于P2功能验收；独立policy prepare/promote/rollback及撤销传播需真实证据。
- 新ID进入生产后不能回退到旧seed-only包；保留匹配的database兼容三包。
  真实兼容生产包回滚及再发布已演练通过；生产数据库恢复没有执行，隔离恢复不等于
  生产数据库恢复演练。动态APIM候选尚未promote或rollback。
- 整体目标保持进行中。缺失配置或证据不可被改名为“全部完成”。
