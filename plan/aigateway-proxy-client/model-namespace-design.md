# 模型列表可读命名空间（用户版 0.4.1 / 订阅版 0.1.2）

## 修订范围

此修订覆盖旧设计中“供应商技术 ID 包含用户/订阅 UUID”的命名要求。
只改变新下载包的插件命名空间和订阅版显示名，不改变平台鉴权、默认订阅、
模型权限、预算、APIM 策略、原始用量或 magpie 应用源码。

## 生成规则

- 用户版：`aigateway-user-<用户名>-<12 位摘要>/<平台模型 ID>`。
- 订阅版：`aigateway-subscription-<订阅名>-<12 位摘要>/<平台模型 ID>`。
- 名称 NFKC 规范化、小写化，保留 Unicode 字母和数字，其他字符转换为连字符；
  编码前名称最长 64 UTF-8 字节。非 ASCII 技术名称使用 xn-- Punycode 编码，以满足
  magpie 原生模型价格配置的 ASCII 校验；用户名和订阅显示名仍保留中文。
  空名称或 UUID 形名称使用 user/subscription 后备词。
- 摘要来自插件种类、平台 origin/网关和稳定用户/订阅 ID，区分同名、跨平台、
  两版插件。摘要不含密钥或下载时随机安装 ID，重复下载保持相同命名空间。
- 稳定 userId、subscriptionId、installationId 和 OAuth uid 保留在私有配置内。
  package.name 只使用 ASCII 类型前缀和摘要，不放明文 UUID 或 Unicode 展示名。
- 供应商、账号和模型的所有引用使用同一个生成的 providerId；模型 API ID 仍为
  平台模型 ID，不能把用户名发到 APIM 当作模型或身份。
- 订阅版供应商显示 `AIGateway Subscription · <订阅名>`，账号显示订阅名；
  用户版供应商和账号继续显示用户姓名。
- 原生 magpie 对 Unicode plugin:// 主机转义后未能正确剥离前缀。插件只修复
  与自身已配置平台地址、转义 providerId 和 /v1/ 完全匹配的路径；随后仍执行
  原有 origin/端点白名单检查，不能修复其他供应商或任意外部 URL。

## 兼容和升级

旧包配置 version=1 和用户版 version=2/3/4 仍支持，认证入口和安装根目录不变。
“刷新模型”和“重新登录”不改已安装包的 providerId，不搬动账号、历史日志、
图片助手或 Agent 配置。改名后的模型引用与旧引用不同，升级必须显式移除旧插件、
安装新下载包并重新选择 Agent 模型；不要把旧、新两代同一插件当作并行升级。
用户版和订阅版仍可同时安装。用户改名后的新下载同样按显式替换处理。

## 验证

覆盖 ASCII/中文/重音名称、路径/控制符清理、UUID 名称后备、长度、重复下载、
同名用户/订阅、跨平台及两版隔离。包测试检查可读 providerId、稳定鉴权 ID、
订阅 Key、账号显示名和文件权限。JS 测试检查新旧命名空间加载与路径注入拒绝。
原生宿主验收只使用隔离 HOME/XDG、临时端口和模拟平台，不触碰日常客户端。

## 本地验证记录（2026-10-13）

后续价格/独立额度趋势的实现和最新验证以
[插件可观测性修复](plugin-observability-fixes.md) 为准。

- 相关 Python 测试 68 passed，1 个既有 Starlette 弃用警告。
- 插件/下载 JS 测试 21 passed；Ruff 通过，Mypy 3 个修改服务文件通过。
- 前端 TypeScript/Vite 构建通过，保留既有大 chunk 提示。
- 已安装原生 magpie 在隔离目录同时列出可读用户版、中文用户版和订阅版模型。
  两个用户版各完成一次模拟文本调用，返回内容及 5 Token usage 正确；平台收到
  fixture-chat，而非含用户名的完整模型引用。没有真实模型调用或 Azure 操作。
- 测试脚本 `/tmp/ts-plugin-namespace-check/native.py`；成功结果保留在
  `/var/folders/_n/vxzws45j78148dvlcyxmc7680000gn/T/ts-namespace-native-5rj64mt7/result.json`。
  临时网关和模拟平台已停止，日常插件、Agent 配置和历史日志未改动。
- 本次未提交 Git、未部署线上；用户界面现有旧包不会因本地模板修改而自动改名。
