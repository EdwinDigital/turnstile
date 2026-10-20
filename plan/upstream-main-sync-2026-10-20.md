# 2026-10-20 上游 Issue #36 修复同步记录

## 来源与范围

- 用户仓库：`origin = EdwinDigital/turnstile`。
- 上游仓库：`upstream = xuleihive/turnstile`。
- 同步前本地 `main` 与 `origin/main`：
  `697f43f3d95ea74e698b0c4bc78c199079b7066b`。
- 本次获取的上游 `main`：
  `f6924a3d700d169f002627ea949c29118c911f96`，提交时间为 2026-10-19。
- 上游提交：`fix: decouple APIM data plane from control-plane proof TTL and add runtime-diagnostic-rbac.bicep`。
- 使用正常 merge 保留双方历史，不 rebase、不重写既有提交。
- 本次只更新本地代码，不推送、不修改其他功能分支、不部署 Azure、不调用付费模型。

## 合入内容

1. 补齐 `infra/modules/runtime-diagnostic-rbac.bicep`，并接入主模板、控制面 RBAC 模板及部署文档。
   参数为 `apimName`、`probeSubscriptionId`、`apiPrincipalIds`。
   角色限定到指定 APIM 系统探测订阅，读取权限来自共享合同，不授予应用订阅密钥轮换权限。
2. APIM 数据面不再仅因控制面证明 TTL 到期而拒绝请求；保留身份绑定、时间顺序以及真实凭据有效期校验。
3. 兼容旧版 route target identity 摘要，减少既有安装升级后的身份不匹配。
4. 为模型可用性及固定路由证明续期增加 300 秒失败退避，并调整可用性同步、证明续期与预算结算周期。
5. 在采集及查询中排除特定的未归属 `budget_protocol_fenced` 探测噪声，保留真实归属的拒绝记录。
6. 请求追踪行恢复显示模型名称。

## 冲突与兼容处理

- `functions/control_plane/function_app.py`：
  保留本仓库 `project_directory_identities` 和 `synchronize_directory`；
  保留现有发布任务 30 秒周期，不改成上游的 10 秒周期。
  其他上游定时任务调整正常合入。
- `turnstile_core/persistence/in_memory.py`：
  同时保留部门白名单、人员数据范围及目录状态过滤，并增加上游探测噪声过滤。
  PostgreSQL 查询路径中的既有范围过滤也已核对保留。
- 新增 4 个参数化过滤回归用例和 1 个定时任务注册回归用例。
- 清除上游基础设施测试文件末尾多余空行，使差异格式检查通过。
- 未新增或修改数据库迁移、依赖清单及锁文件。

## 本次本地验证

| 检查 | 结果 |
| --- | --- |
| Ruff，全仓库约定范围及两个 Function 入口 | 通过 |
| Mypy | 376 个源文件通过 |
| 后端与基础设施定向回归 | 1331 通过，0 失败、0 跳过；11 条现有依赖/序列化警告 |
| .NET 可执行 APIM 策略 | 155 通过，0 失败、0 跳过 |
| TypeScript / Vite 生产构建 | 通过，保留大 chunk 警告 |
| Bicep 主模板编译 | 通过，4 条警告，不代表 Azure what-if 或部署通过 |
| 独立诊断 RBAC 模板编译 | 通过，无警告 |
| 差异格式与冲突标记 | `git diff --cached --check` 通过，无未解决冲突 |

定向后端回归覆盖模型可用性、APIM 目标客户端、路由投影及合同、连接诊断、用量采集、
目录投影、权限数据范围、合并合同、控制面 Function、基础设施合同和三组预算安装测试。
含真实隔离本机 PostgreSQL 测试，未连接生产数据库。

策略测试使用临时安装的 .NET SDK 8.0.426。首次冷启动构建超过测试夹具的 60 秒上限，
导致共享夹具报错；单独预热项目并关闭工作负载更新通知后，整组 155 项重跑通过。
未通过修改业务逻辑或放宽测试超时掩盖该问题。

较宽的后端检查在 170 项通过后主动停止，转为上述定向套件。
本次未完成全量 pytest，不把重叠集合相加为全量通过数。

### 前端基线对照

- 直接执行 Node 前端测试时，合并前和合并后均出现两处无扩展名 TypeScript 导入错误及一处旧助手设置断言失败。
- 使用现有临时加载器处理相对 TypeScript 导入后，对合并前 `main` 和候选版本执行同一完整 Node 套件：
  两者均为 **560 通过、61 失败、6 跳过**，61 个失败名称逐项相同。
- 此次前端源码只合入追踪行的模型名显示修复；相关旧失败未在本次源代码同步中展开修复。
  前端生产构建通过不等于独立测试全绿。

### Bicep 警告

主模板编译的 4 条警告包括 `data-plane.bicep` 两处最小长度提示、
`apim-integration.bicep` 一处数值下限提示，以及此次上游新增模块参数访问控制面输出时的
BCP318 可空模块提示。此次不扩大范围修改上游模板。

## 部署边界

拉取并合并源码不会自动为线上托管身份授予 RBAC，也不会部署新版 APIM 策略。
既有环境如需修复连接诊断权限，应使用新增模板，按 `infra/README.md` 先执行并审阅
Azure `what-if`，再进行经授权的资源更新；本次未执行这两个云端步骤。
