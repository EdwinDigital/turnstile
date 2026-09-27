import type { ApimTargetTopology, ApimTargetTopologyList, GatewayProfile, GatewayPublication, GatewayReleaseOperation, ManagedModel, ModelRuntime } from "../data-sources/apim/types"

export const MODEL_MANAGEMENT_TABS = ["models", "connections", "gateways", "apim"] as const
export type ModelManagementTab = typeof MODEL_MANAGEMENT_TABS[number]
export const MODEL_MANAGEMENT_TAB_LABELS: Record<ModelManagementTab, string> = {
  models: "模型", connections: "连接", gateways: "网关", apim: "APIM 实例",
}

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
  if (deployment.retired_at) return result("unavailable", "已退役", "网关已从 Turnstile 移除")
  if (deployment.declared_state === "unsupported") return result("unavailable", "不支持", "APIM 不支持此网关")
  if (deployment.declared_state === "absent") return result("unavailable", "未发现", "APIM 清单中未发现网关")
  if (!deployment.endpoint_url) return result("warning", "未配置", "端点未配置")
  if (deployment.declared_state !== "present") return result("warning", "待同步", "请先同步 APIM 清单")
  if (deployment.kind === "self_hosted" && !gatewayApiAssociated(entry)) return result("warning", "未关联", "API 未关联")
  if (deployment.provisioning_operation_id && !deployment.last_probe_at) return result("warning", "待启动", "APIM 配置已创建，等待容器接入")
  if (!deployment.last_probe_at || deployment.reachability_state === "unknown") return result("warning", "待验证", "尚未运行校验")
  if (deployment.reachability_state === "unreachable") return result("unavailable", "不可达", "上次探测未通过；并非实时健康状态")
  return result("ready", "已验证", "上次探测通过；并非实时健康状态")
}

export function apimTargetDisplayName(target: ApimTargetTopology, profiles: readonly GatewayProfile[]) {
  return profiles.find(profile => profile.id === target.target.gateway_profile_id)?.name.trim() || target.target.service_name
}

export function apimGatewayProfiles(profiles: GatewayProfile[], topology: ApimTargetTopologyList | null, topologyAvailable: boolean) {
  return profiles.filter(profile => profile.enabled && profile.implementation === "apim"
    && (!topologyAvailable || topology?.items.some(item => item.target.gateway_profile_id === profile.id)))
}

export function publicationTargetDetails(publication: Pick<GatewayPublication, "gateway_profile_id" | "target_deployments">, profiles: readonly GatewayProfile[], topology: ApimTargetTopologyList | null) {
  const target = topology?.items.find(item => item.target.gateway_profile_id === publication.gateway_profile_id)
  const profile = profiles.find(item => item.id === publication.gateway_profile_id)
  return {
    apimName: profile?.name.trim() || target?.target.service_name || publication.gateway_profile_id,
    serviceName: target?.target.service_name ?? null,
    gateways: (publication.target_deployments ?? []).map(snapshot => {
      const current = target?.deployments.find(({ deployment }) => deployment.id === snapshot.deployment_id
        && deployment.apim_target_id === publication.gateway_profile_id && deployment.kind === snapshot.kind
        && deployment.native_gateway_id.toLowerCase() === snapshot.native_gateway_id.toLowerCase())?.deployment
      return { id: snapshot.deployment_id, name: current?.display_name.trim() || snapshot.native_gateway_id,
        nativeId: snapshot.native_gateway_id, kind: snapshot.kind }
    }),
  }
}

export function modelTargetDetails(model: Pick<ManagedModel, "id">, runtime: Pick<ModelRuntime, "gateway_profile_id"> | undefined, profiles: readonly GatewayProfile[], topology: ApimTargetTopologyList | null) {
  const target = topology?.items.find(item => item.target.gateway_profile_id === runtime?.gateway_profile_id)
  const deployments = target?.deployments.filter(entry => entry.deployment.apim_target_id === runtime?.gateway_profile_id
    && !entry.deployment.retired_at && entry.published_models_available !== false
    && entry.published_models.some(published => published.id === model.id)) ?? []
  return publicationTargetDetails({ gateway_profile_id: runtime?.gateway_profile_id ?? "", target_deployments: deployments.map(({ deployment }) => ({
    deployment_id: deployment.id, native_gateway_id: deployment.native_gateway_id, kind: deployment.kind,
    endpoint_url: deployment.endpoint_url ?? "", release_gate: deployment.release_gate,
  })) }, profiles, topology)
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
  if (deployment.kind === "self_hosted" && target.target.capabilities.self_hosted_gateway !== "supported") return "APIM 未确认支持自托管网关"
  const required = apimGatewayEntries(target).filter(item => item.deployment.release_gate === "required")
  if (required.some(({ deployment: item }) => item.declared_state !== "present" || !item.endpoint_url || item.reachability_state !== "reachable")) return "必需网关未就绪"
  if (deployment.retired_at) return "网关已退役"
  if (deployment.declared_state !== "present") return "网关未就绪"
  if (!deployment.endpoint_url) return "端点未配置"
  if (deployment.kind === "self_hosted" && !gatewayApiAssociated(entry)) return "API 未关联"
  if (required.some(item => item.deployment.kind === "self_hosted" && !gatewayApiAssociated(item))) return "必需网关未关联 API"
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

export function resolveInvocationModel<Model extends Pick<ManagedModel, "id" | "enabled" | "capabilities" | "is_default">>(models: readonly Model[], modelId: string | null, imageMode: boolean) {
  const candidates = models.filter(model => model.enabled && model.capabilities.includes("image_generation") === imageMode)
  if (modelId !== null) return candidates.find(model => model.id === modelId)
  return imageMode ? undefined : candidates.find(model => model.is_default) ?? candidates[0]
}

export function gatewayInvocationChoices(profiles: GatewayProfile[], topology: ApimTargetTopologyList | null, modelId: string): GatewayPublicationChoice[] {
  const enabled = profiles.filter(profile => profile.enabled && profile.implementation === "apim")
  return (topology?.items ?? []).filter(target => enabled.some(profile => profile.id === target.target.gateway_profile_id))
    .flatMap(target => target.deployments.filter(entry => entry.published_models_available !== false
      && entry.published_models.some(model => model.id === modelId) && !entry.deployment.retired_at
      && entry.deployment.apim_target_id === target.target.gateway_profile_id).map(entry => {
      const { deployment } = entry
      const reason = target.target.drift_state === "drifted" ? "APIM 清单尚未验证"
        : deployment.declared_state !== "present" || deployment.reachability_state === "unreachable" ? "Gateway 未就绪"
        : !deployment.endpoint_url ? "Endpoint 未配置"
        : deployment.kind === "self_hosted" && !gatewayApiAssociated({ ...entry, target }) ? "API 未关联"
        : null
      return { value: `${target.target.gateway_profile_id}:${deployment.id}`, targetId: target.target.gateway_profile_id,
        deploymentId: deployment.id, name: deployment.display_name, kind: deployment.kind,
        endpoint: deployment.endpoint_url, serviceName: target.target.service_name, selectable: reason === null, reason }
    }))
}