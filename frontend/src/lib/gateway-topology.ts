import type { ApimTargetTopology, ApimTargetTopologyList, GatewayProfile } from "../data-sources/apim/types"

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
  if (deployment.retired_at) return result("unavailable", "Retired", "Removed from Turnstile")
  if (deployment.declared_state === "unsupported") return result("unavailable", "Unsupported", "Unsupported by this APIM")
  if (deployment.declared_state === "absent") return result("unavailable", "Not found", "Absent from the last ARM inventory")
  if (!deployment.endpoint_url) return result("warning", "Not configured", "Endpoint unavailable")
  if (deployment.declared_state !== "present") return result("warning", "Not synchronized", "ARM declaration is unknown")
  if (deployment.kind === "self_hosted" && !gatewayApiAssociated(entry)) return result("warning", "Not associated", "API association missing")
  if (!deployment.last_probe_at || deployment.reachability_state === "unknown") return result("warning", "Not verified", "No connection probe evidence")
  if (deployment.reachability_state === "unreachable") return result("unavailable", "Unreachable", "Last connection probe failed; not live health")
  return result("ready", "Verified", "Last connection probe passed; not live health")
}

export function apimTargetDisplayName(target: ApimTargetTopology, profiles: readonly GatewayProfile[]) {
  return profiles.find(profile => profile.id === target.target.gateway_profile_id)?.name.trim() || target.target.service_name
}

export function gatewayPublicationIssue(entry: GatewayEntry): string | null {
  const { deployment, target } = entry
  if (target.target.drift_state !== "in_sync") return "APIM inventory is not verified"
  if (deployment.kind === "self_hosted" && target.target.capabilities.self_hosted_gateway !== "supported") return "Self-hosted Gateway support is not verified"
  const required = apimGatewayEntries(target).filter(item => item.deployment.release_gate === "required")
  if (required.some(({ deployment: item }) => item.declared_state !== "present" || !item.endpoint_url || item.reachability_state !== "reachable")) return "Required Gateway is not ready"
  if (required.some(item => item.deployment.kind === "self_hosted" && !gatewayApiAssociated(item))) return "Required Gateway is not associated with the API"
  if (deployment.retired_at) return "Gateway is retired"
  if (deployment.declared_state !== "present") return "Gateway is not ready"
  if (!deployment.endpoint_url) return "Endpoint unavailable"
  if (deployment.kind === "self_hosted" && !gatewayApiAssociated(entry)) return "API association missing"
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