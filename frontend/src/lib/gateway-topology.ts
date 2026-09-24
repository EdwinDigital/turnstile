import type { ApimTargetTopology, ApimTargetTopologyList, GatewayProfile, GatewayReleaseOperation } from "../data-sources/apim/types"

export const MODEL_MANAGEMENT_TABS = ["models", "connections", "gateways", "apim"] as const
export type ModelManagementTab = typeof MODEL_MANAGEMENT_TABS[number]

export type GatewayEntry = ApimTargetTopology["deployments"][number] & { target: ApimTargetTopology }
export type GatewayEvidence = {
  state: "ready" | "warning" | "unavailable"
  label: string
  detail: string
  probedAt: string | null
}

export function apimGatewayEntries(target: ApimTargetTopology): GatewayEntry[] {
  return target.deployments.filter(({ deployment }) => !deployment.retired_at)
    .map(entry => ({ ...entry, target }))
    .sort((left, right) => Number(right.deployment.kind === "managed") - Number(left.deployment.kind === "managed"))
}

export function gatewayEntries(topology: ApimTargetTopologyList | null): GatewayEntry[] {
  return (topology?.items ?? []).flatMap(target => target.deployments
    .filter(({ deployment }) => deployment.kind === "self_hosted" && !deployment.retired_at)
    .map(entry => ({ ...entry, target })))
}

export function gatewayApiAssociated(entry: GatewayEntry) {
  return Boolean(entry.inventory?.associated_api_ids.some(
    identity => identity.toLowerCase() === entry.target.target.api_id.toLowerCase(),
  ))
}

export function gatewayEvidence(entry: GatewayEntry): GatewayEvidence {
  const { deployment } = entry
  const result = (state: GatewayEvidence["state"], label: string, detail: string): GatewayEvidence => ({
    state, label, detail, probedAt: deployment.last_probe_at,
  })
  if (deployment.retired_at) return result("unavailable", "已退役", "Gateway 已从 Turnstile 移除")
  if (deployment.declared_state === "unsupported") return result("unavailable", "不支持", "APIM 不支持此 Gateway")
  if (deployment.declared_state === "absent") return result("unavailable", "未发现", "APIM 清单中未发现 Gateway")
  if (!deployment.endpoint_url) return result("warning", "未配置", "Endpoint 未配置")
  if (deployment.declared_state !== "present") return result("warning", "待同步", "请先同步 APIM 清单")
  if (deployment.kind === "self_hosted" && !gatewayApiAssociated(entry)) return result("warning", "未关联", "API 未关联")
  if (!deployment.last_probe_at || deployment.reachability_state === "unknown") return result("warning", "待验证", "尚未运行校验")
  if (deployment.reachability_state === "unreachable") return result("unavailable", "不可达", "上次探测未通过；并非实时健康状态")
  return result("ready", "已验证", "上次探测通过；并非实时健康状态")
}

export function apimTargetDisplayName(target: ApimTargetTopology, profiles: readonly GatewayProfile[]) {
  return profiles.find(profile => profile.id === target.target.gateway_profile_id)?.name.trim() || target.target.service_name
}

export function apimOnboardingOperationName(operation: Pick<GatewayReleaseOperation, "semantic_preview" | "gateway_profile_id">) {
  const request = operation.semantic_preview.request
  if (request !== null && typeof request === "object" && !Array.isArray(request)
    && "name" in request && typeof request.name === "string" && request.name.trim()) return request.name.trim()
  const serviceName = operation.semantic_preview.service_name
  return typeof serviceName === "string" && serviceName.trim() ? serviceName.trim() : operation.gateway_profile_id
}

export function apimOnboardingDetails(operation: Pick<GatewayReleaseOperation, "semantic_preview">) {
  const preview = operation.semantic_preview
  const request = preview.request
  const resourceId = request !== null && typeof request === "object" && !Array.isArray(request)
    && "resource_id" in request && typeof request.resource_id === "string" ? request.resource_id : ""
  const origin = typeof preview.managed_gateway_origin === "string" ? preview.managed_gateway_origin : ""
  const apiPath = typeof preview.api_path === "string" ? preview.api_path : ""
  return {
    serviceName: typeof preview.service_name === "string" ? preview.service_name : "",
    resourceGroup: resourceId.split("/")[4] ?? "", resourceId,
    serviceTier: typeof preview.service_tier === "string" ? preview.service_tier : "",
    location: typeof preview.location === "string" ? preview.location : "",
    apiId: typeof preview.api_id === "string" ? preview.api_id : "",
    endpoint: origin && apiPath ? `${origin.replace(/\/$/, "")}/${apiPath}` : null,
  }
}

export function apimOnboardingRows<Operation extends Pick<GatewayReleaseOperation, "gateway_profile_id" | "created_at" | "status">>(
  operations: readonly Operation[], targets: readonly ApimTargetTopology[],
): Operation[] {
  const latest = new Map<string, Operation>()
  for (const operation of operations) {
    const previous = latest.get(operation.gateway_profile_id)
    if (!previous || operation.created_at > previous.created_at) latest.set(operation.gateway_profile_id, operation)
  }
  const registered = new Set(targets.map(item => item.target.gateway_profile_id))
  return [...latest.values()].filter(operation => operation.status !== "succeeded" || !registered.has(operation.gateway_profile_id))
}

export function pendingInventorySyncId(operations: readonly Pick<GatewayReleaseOperation, "id" | "operation_kind" | "status" | "created_at">[]) {
  return operations.filter(operation => operation.operation_kind === "target_inventory_sync"
    && !["succeeded", "failed", "restored"].includes(operation.status))
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0]?.id ?? null
}

export function gatewayPublicationIssue(entry: GatewayEntry): string | null {
  const { deployment, target } = entry
  if (target.target.drift_state !== "in_sync") return "APIM 清单尚未验证"
  if (deployment.kind === "self_hosted" && target.target.capabilities.self_hosted_gateway !== "supported") return "APIM 未确认支持 Self-hosted Gateway"
  const required = apimGatewayEntries(target).filter(item => item.deployment.release_gate === "required")
  if (required.some(({ deployment: item }) => item.declared_state !== "present" || !item.endpoint_url || item.reachability_state !== "reachable")) return "Required Gateway 未就绪"
  if (deployment.retired_at) return "Gateway 已退役"
  if (deployment.declared_state !== "present") return "Gateway 未就绪"
  if (!deployment.endpoint_url) return "Endpoint 未配置"
  if (deployment.kind === "self_hosted" && !gatewayApiAssociated(entry)) return "API 未关联"
  if (required.some(item => item.deployment.kind === "self_hosted" && !gatewayApiAssociated(item))) return "Required Gateway 未关联 API"
  return null
}

export type GatewayPublicationChoice = {
  value: string; targetId: string; deploymentId: string | null
  name: string; kind: "managed" | "self_hosted"; endpoint: string | null
  serviceName: string; selectable: boolean; reason: string | null
}

export function gatewayPublicationChoices(
  profiles: GatewayProfile[], topology: ApimTargetTopologyList | null, topologyAvailable: boolean,
): GatewayPublicationChoice[] {
  const enabled = profiles.filter(profile => profile.enabled && profile.implementation === "apim")
  if (!topologyAvailable) return enabled.map(profile => ({
    value: `compat:${profile.id}`, targetId: profile.id, deploymentId: null, name: profile.name,
    kind: "managed", endpoint: profile.base_url, serviceName: profile.name, selectable: true, reason: null,
  }))
  return (topology?.items ?? []).filter(target => enabled.some(profile => profile.id === target.target.gateway_profile_id))
    .flatMap(apimGatewayEntries).map(entry => {
      const reason = gatewayPublicationIssue(entry)
      return {
        value: `${entry.target.target.gateway_profile_id}:${entry.deployment.id}`,
        targetId: entry.target.target.gateway_profile_id, deploymentId: entry.deployment.id,
        name: entry.deployment.display_name, kind: entry.deployment.kind, endpoint: entry.deployment.endpoint_url,
        serviceName: entry.target.target.service_name, selectable: reason === null, reason,
      }
    })
}

export function resolveGatewaySelection(
  profiles: GatewayProfile[], choices: GatewayPublicationChoice[],
  selection: { targetId: string | null; deploymentId: string | null },
) {
  const preferred = profiles.find(profile => profile.is_default) ?? profiles[0]
  const targetId = selection.targetId ?? preferred?.id ?? ""
  const targetChoices = choices.filter(choice => choice.targetId === targetId)
  const choice = selection.deploymentId === null
    ? targetChoices.find(item => item.selectable)
    : targetChoices.find(item => item.deploymentId === selection.deploymentId)
  return { targetId, choice, targetChoices }
}