# USD 预算迁移与执行链路只读审计

检查日期：2026-10-17，Asia/Shanghai。线上取证开始于 15:50。
本地源码：`c22973e`。本轮没有修改 Azure 资源、运行开关、APIM 或预算，没有模型推理。

## 结论

当前分支的数据库迁移已经完整应用，但已有安装的 USD 预算执行协议没有完成安装、
初始化和验收。不能通过重新运行数据库迁移、刷新页面或单独开启执行开关解决。
Test Org-USD 的每月 100 USD、Test Dept-1-USD 的每月 70 USD 均未配置生效。

## 官方文档的适用边界

- [美元预算设置教程](https://turn-stile.com/docs/usd-budget-tutorial) 明确以已经具备美元预算
  能力的 v1.2 环境为前提。教程截图经过本地示例替换，并未保存任何线上预算。
- 教程对缺少 USD 选项或保存 503 的建议是检查 API、网关预算协议及发布验证，不绕过就绪检查。
- [v1.2 部署准备](https://turn-stile.com/docs/deployment#prepare-v1-2) 明确区分应用更新和
  协议安装；普通部署器及 images-v2 升级命令不能代替通用 v1.2 预算协议安装。
- [预算验证](https://turn-stile.com/docs/gateway-releases#budget-verification) 的就绪诊断不产生
  推理，联合验证使用 paused 下的隔离作用域。验证本身不会初始化余额或开启执行协议。
- [配置](https://turn-stile.com/docs/configuration#v1-2-gates) 区分三个 Publisher 开关和独立的
  budget execution / settlement / completion 配置。不能将这两组配置混为一谈。

## 当前线上证据

| 项目 | 取证结果 | 判断 |
| --- | --- | --- |
| PostgreSQL | Ready，认证身份、预算能力及预算列表读取均为 200 | 当前数据库可用 |
| SQL 迁移 | 已应用 40，本地 40；pending、extra、checksum mismatch 均为空 | 当前分支迁移完整，无需重复执行或修改旧迁移 |
| API 运行包 | 实际包 `20261016180812.zip` 与保留的已部署包 SHA-256 一致；Core digest 为 `f4feffce569596db7a13b9627b293c1970c1e3212533fca64b7a04ee25b7d737` | API 包已读回 |
| Functions | Telemetry 5、Control-plane 9 个函数已索引；两个 active deployment ID 与保留部署证明一致 | 不等于金融 worker 或监听器已验收 |
| Functions 包字节边界 | Flex SCM 的 SitePackages 指针及直接 wwwroot manifest 路径为 404；本轮以 active deployment ID 对比旧证明，未重新下载两个实际 ZIP | 不能把 404 当作包缺失，也不能声称本轮重新验证全部包字节 |
| 普通发布开关 | Control-plane 的 CONTROL_PLANE_ENABLED、GATEWAY_PUBLICATION_WORKER_ENABLED、GATEWAY_RELEASE_WORKER_ENABLED 均为 true | 普通发布已开启，不代表 USD 已开启 |
| APIM 目标登记 | 三个应用的 APIM_TARGET_REGISTRY_ENABLED 均为 false | 目标登记功能关闭；不据此推断已有目标丢失 |
| USD 开关 | 三个应用的 BUDGET_EXECUTION_ENABLED、BUDGET_SETTLEMENT_ENABLED、BUDGET_RESERVATION_RESOLUTION_ENABLED 均为 false | 尚未启用 |
| USD 目标/委托 | BUDGET_EXECUTION_TARGET_IDS、BUDGET_DELEGATION_TARGETS、BUDGET_DELEGATION_TRUST 未配置 | 正式运行配置不完整 |
| 当前 APIM | 当前修订 `turnstile-tpm-9e80afc7ae5ec57a`；父策略没有 budgetExecutionVersion；共享服务策略没有 budgetExecutionFence | 仍是原执行路径 |
| 准备候选 | `usd-c8c9a87e04dd49bda96c36ef` 非当前，仅 GET /v1/models | 不是已安装推理链路 |
| Observer | 仍使用 `envoy-cache-adapter:d7d63182c59a`；BUDGET_COMPLETION_ENABLED 未设置、默认 false | 新预算完成处理未上线 |
| 金融初始化 | rollout / scope / attempt / settlement_task 全部为 0 | 未初始化，也没有 USD 扣费验证证据 |
| 发布历史 | 9 条，1 active、8 superseded | 不能把原 active 发布当成新预算发布证据 |
| 目标预算 | 2026-10 的组织、部门目标记录均为空；现有当月 5 条预算全为 Token | 100 USD / 70 USD 均未生效 |
| 能力接口 | usd_available=false，reason_code=execution_disabled | 页面禁用 USD 符合后端状态 |

API、Telemetry 的 LEDGER_SYNC_ENABLED=true 且账本端点/表存在，不等于新协议 scope、价格和
完成事件已经初始化或结算工作器可用。函数名出现 settle_budget_requests 也不能代替开关和真实验收。

## 当前工具缺口

`scripts/install_budget_execution.py` 的 CLI 目前只有 `--plan`，输出 cloud_writes=0、
activation=false。内部已经有计划、journal、维护证据和协调初始化保护，但没有完整的云端
安装、受控发布接管、激活与消费后恢复执行器。其代码没有被遗漏执行成一条现成的安装命令。

部署源码 `5515bfe` 到当前 `c22973e` 的差异只有安装保护、测试及进展文档，没有新的运行时或
SQL 迁移。这一源码差异本身不能补全金融安装流程。

上一轮临时真实服务诊断在新设置加载后仍返回通用 503，未获得 ready 正反例证明；
不能据此断言是 Entra、Table 权限或订阅密钥中的某一个已确诊。临时设置/共享策略已恢复、
自有 prepared F 已移除、临时 Table writer 已撤销，会话已注销。本轮没有重放该窗口。

## 应完成的流程

1. 先补齐适配当前有历史用量安装的执行器及恢复流程，并做隔离还原库和负例测试。
   不绕过现有正常保存保护，不用仅支持空安装的 bootstrap 接管已有消费。
2. 在维护计划中锁定现有目标、期间、epoch、模型计数/价格契约、全部可达修订及制品摘要。
   刷新数据库、APIM、账本、包/镜像、配置和密钥的加密备份，验证可恢复；避免并发发布。
3. 部署匹配的 API / Telemetry / Control-plane / Observer；以各自真实身份验证精确 Table
   权限、网络及完成事件处理。验证真实 web worker 委托正反例，不以 SCM 模拟身份替代。
4. 准备具备实际推理接口和契约的候选，并受控接管发布证据。安装覆盖全部可达旧路径的共享
   paused fence，等待在途推理、Observer、遥测与结算排空，读取新鲜且一致的 SQL / 旧账本期初。
5. 在网关仍 paused 时，使用受保护的初始化创建同一 epoch 的协调状态，再通过现有 scope
   服务一次性建立期初、策略和价格投影。数据库协调 active 不等于网关普通流量 active。
   保留已发生的 Token 用量及未决费用，不用零余额种子覆盖历史。
6. 满足正常预算写入门槛后，经平台 API 设置同一月份的组织 100.00 USD、部门 70.00 USD，
   读回并核对分配层级。个人预算、模型权限和订阅配额独立处理，不能擅自分配未批准的个人金额，
   也不能把共享 Dashboard 订阅改成 USD 来影响另一 Token 组织。
7. 在隔离个人/应用/订阅上完成就绪正反例、一次有界联合推理及 receipt / 正式事件 / SQL
   settlement / Table S、A 的关联核对；补充余额不足、价格缺失、并发、流式/异常及原 Token
   路径测试。结果未知的付费请求不自动重放。证明消费后安全恢复，再允许普通流量。
8. 按服务职责有序激活运行开关及共享 fence。能力接口应为 usd_available=true、reason_code=ready，
   组织/部门/个人/订阅实际单位及金额与批准计划一致。未通过的步骤必须继续标明不可用。

本轮仅完成审计，不代表以上安装阶段已经执行。本地只读证据位于
`/tmp/ts-usd-audit-20261017`；其中没有将私有凭据提交到仓库。
