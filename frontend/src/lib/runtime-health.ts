import type { ModelRuntime, RuntimeHealth, UsageRequestSummary } from "../data-sources/apim/types"

export const runtimeDiagnosticPrefix = "runtime-diagnostic:v1:"

export const runtimeHealthLabels = {
  online: "上次检查通过",
  offline: "上次调用失败",
  blocked: "上次检查受阻",
  configuration: "上次诊断配置异常",
  changed: "当前配置待验证",
  inconclusive: "上次检查未确认",
  unchecked: "尚未检查",
  disabled: "已停用",
  checking: "正在检查",
} as const

export type RuntimeHealthState = keyof typeof runtimeHealthLabels

const policyErrors = [
  "application_model_not_assigned", "model_not_assigned", "budget_ledger_unavailable",
  "application_ledger_unavailable", "application_tpm_unavailable", "application_map_unavailable",
  "application_unconfigured", "application_inactive", "budget_protocol_fenced", "budget_identity_unavailable",
  "budget_count_bound_unavailable", "budget_configuration_invalid", "budget_protocol_validation_scope",
  "budget_model_unattributed", "budget_request_analysis_failed", "budget_request_invalid", "budget_plan_failed",
  "budget_stream_evidence_unavailable", "budget_output_limit_unavailable", "budget_output_limit_invalid",
  "budget_consumer_disabled", "budget_exhausted", "budget_request_bound_exceeds_balance", "budget_delegation_invalid",
  "budget_exceeded", "budget_temporarily_reserved", "model_unavailable", "rate_limit_exceeded",
]

export function runtimeDiagnostic(runtime: ModelRuntime): RuntimeHealth | null {
  try {
    const value = runtime.diagnostic ?? (runtime.health_message?.startsWith(runtimeDiagnosticPrefix)
      ? JSON.parse(runtime.health_message.slice(runtimeDiagnosticPrefix.length)) as RuntimeHealth : null)
    if (!value || value.runtime_id !== runtime.id || !Number.isFinite(Date.parse(value.checked_at))
      || !["passed", "blocked", "configuration_error", "upstream_error", "inconclusive"].includes(value.outcome ?? "")) return null
    const expectedStatus = value.outcome === "passed" ? "available" : value.outcome === "upstream_error" ? "unavailable" : "unknown"
    if (value.status !== expectedStatus) return null
    return runtime.diagnostic ? value : { ...value, configuration_status: "unverified", configuration_checked_at: null }
  } catch { return null }
}

export function runtimeHealthState(runtime: ModelRuntime, now = Date.now()): RuntimeHealthState {
  if (!runtime.enabled) return "disabled"
  if (runtime.health_message?.startsWith(`${runtimeDiagnosticPrefix}pending:`)) {
    const started = Date.parse(runtime.last_checked_at ?? "")
    return Number.isFinite(started) && now >= started && now - started < 60_000 ? "checking" : "inconclusive"
  }
  const diagnostic = runtimeDiagnostic(runtime)
  if (!diagnostic) return runtime.last_checked_at ? "inconclusive" : "unchecked"
  const checked = Date.parse(diagnostic.checked_at)
  if (checked > now + 5_000) return "inconclusive"
  if (diagnostic.configuration_status === "changed") return "changed"
  if (diagnostic.outcome === "passed") return "online"
  if (diagnostic.outcome === "blocked") return "blocked"
  if (diagnostic.outcome === "configuration_error") return "configuration"
  if (diagnostic.outcome === "upstream_error") return "offline"
  return "inconclusive"
}

export function runtimeConfigurationDescription(runtime: ModelRuntime): string | null {
  const diagnostic = runtimeDiagnostic(runtime)
  if (!diagnostic) return null
  if (diagnostic.configuration_status === "changed") return "调用配置已变更，历史结果不代表当前配置"
  if (diagnostic.configuration_status === "current") return "与检查时记录的调用配置一致"
  return diagnostic.configuration_fingerprint ? "当前配置暂时无法核对" : "历史结果未记录配置版本"
}

export function runtimeHealthDescription(runtime: ModelRuntime): string {
  const diagnostic = runtimeDiagnostic(runtime)
  if (!diagnostic) return runtime.health_message?.startsWith(runtimeDiagnosticPrefix)
    ? "诊断尚未完成" : runtime.health_message ?? "尚无检查结果"
  const outcome = diagnostic.outcome === "passed" ? "所选模型推理成功"
    : diagnostic.outcome === "blocked" ? "检查被权限、预算或网关策略拦截，未确认连接状态"
    : diagnostic.outcome === "configuration_error" ? "系统诊断身份或目标配置未就绪"
    : diagnostic.outcome === "upstream_error" ? "所选调用路径返回服务端错误，不能代表连接内全部模型"
    : "本次检查未确认连接状态"
  return [outcome, diagnostic.selection?.model_name,
    diagnostic.selection ? `${diagnostic.selection.gateway_name} (${diagnostic.selection.native_gateway_id})` : null,
    diagnostic.http_status ? `HTTP ${diagnostic.http_status}` : null, diagnostic.error_code,
    diagnostic.checked_at, runtimeConfigurationDescription(runtime)].filter(Boolean).join(" · ")
}

export function runtimeWithTelemetryHealth(runtime: ModelRuntime, requests: UsageRequestSummary[]): ModelRuntime {
  const saved = runtimeDiagnostic(runtime)
  if (saved) return { ...runtime, diagnostic: saved }
  const latest = requests.filter(request => request.request_source === "runtime-health-check" && Number.isFinite(Date.parse(request.timestamp)))
    .sort((left, right) => Date.parse(right.timestamp) - Date.parse(left.timestamp))[0]
  if (!latest || latest.status_code == null || runtime.last_checked_at && Date.parse(runtime.last_checked_at) >= Date.parse(latest.timestamp)) return runtime
  const code = policyErrors.find(value => new RegExp(`(^|[^a-z_])${value}([^a-z_]|$)`).test(latest.error_message ?? "")) ?? null
  const success = latest.status_code >= 200 && latest.status_code < 300
  const blocked = code !== null || [401, 403, 409, 429].includes(latest.status_code)
  const diagnostic: RuntimeHealth = {
    runtime_id: runtime.id, status: success ? "available" : "unknown", check_kind: "legacy",
    outcome: success ? "passed" : blocked ? "blocked" : "inconclusive",
    message: success ? "Historical inference succeeded" : "Historical check did not establish availability",
    checked_at: latest.timestamp, http_status: latest.status_code, error_code: code,
    request_id: latest.request_id, correlation_id: latest.correlation_id, latency_ms: latest.latency_ms,
  }
  return { ...runtime, health_status: diagnostic.status, health_message: diagnostic.message,
    last_checked_at: diagnostic.checked_at, diagnostic }
}

export function runtimeDiagnosticReason(reason: string | null | undefined): string | null {
  if (!reason) return null
  const messages: Record<string, string> = {
    "Connection is disabled": "连接已停用",
    "A registered APIM target is required": "连接尚未关联已注册的 APIM",
    "Provider or Gateway is disabled": "Provider 或 Gateway 已停用",
    "Synchronize and verify the APIM target before running diagnostics": "APIM 配置尚未同步验证",
    "APIM system diagnostic subscription is not configured": "尚未配置系统诊断订阅",
    "Synchronize the target's system diagnostic subscription": "系统诊断订阅尚未同步或启用",
    "The diagnostic subscription must belong to the selected APIM target": "系统诊断订阅与所选 APIM 不匹配",
    "No eligible text model and trusted Gateway route": "没有可诊断的文本模型和可信 Gateway 路由",
  }
  return messages[reason] ?? "系统诊断配置未就绪"
}