import type { ApimOnboardingRequest, ApimTargetWrite, SelfHostedGatewayCreate, SelfHostedGatewayWrite } from "../data-sources/apim/types"

export function isGatewayEndpoint(value: string) {
  try {
    const endpoint = new URL(value.trim())
    return endpoint.protocol === "https:" && Boolean(endpoint.hostname)
      && !endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash
      && !value.includes("?") && !value.includes("#")
  } catch { return false }
}

function textWithin(value: string, maximum: number) {
  return value.trim().length > 0 && value.trim().length <= maximum
}

export function gatewayNameFormValid(name: string, currentName: string) {
  return textWithin(name, 120) && name.trim() !== currentName.trim()
}

export function gatewayFormValid(value: SelfHostedGatewayWrite) {
  return textWithin(value.native_gateway_id, 80) && value.native_gateway_id.trim().toLowerCase() !== "managed"
    && textWithin(value.display_name, 120) && isGatewayEndpoint(value.endpoint_url)
    && (!value.location?.trim() || value.location.trim().length <= 128)
}

export function gatewayFormPayload(value: SelfHostedGatewayWrite): SelfHostedGatewayWrite {
  return { ...value, native_gateway_id: value.native_gateway_id.trim(), display_name: value.display_name.trim(),
    endpoint_url: value.endpoint_url.trim(), location: value.location?.trim() || null }
}

export type GatewayCreateForm = Omit<SelfHostedGatewayCreate, "endpoint_control_confirmed"> & { endpoint_control_confirmed: boolean }

export function gatewayCreateFormValid(value: GatewayCreateForm) {
  return textWithin(value.display_name, 120) && isGatewayEndpoint(value.endpoint_url)
    && (!value.location?.trim() || value.location.trim().length <= 128)
    && value.endpoint_control_confirmed === true
}

export function gatewayCreateFormPayload(value: GatewayCreateForm): SelfHostedGatewayCreate {
  if (!gatewayCreateFormValid(value)) throw new Error("Gateway creation fields are incomplete")
  return { display_name: value.display_name.trim(), endpoint_url: value.endpoint_url.trim(),
    location: value.location?.trim() || null, hosting_mode: value.hosting_mode ?? "docker",
    release_gate: value.release_gate ?? "required", endpoint_control_confirmed: true }
}

export function apimFormValid(value: ApimTargetWrite) {
  const uuid = /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i
  if (!uuid.test(value.tenant_id.trim()) || !uuid.test(value.subscription_id.trim())) return false
  if (!textWithin(value.name, 120) || !textWithin(value.resource_group, 90)
    || !textWithin(value.service_name, 50) || !textWithin(value.api_id, 256)
    || !textWithin(value.dashboard_subscription_id ?? "", 255) || !textWithin(value.probe_subscription_id ?? "", 255)
    || !isGatewayEndpoint(value.gateway_endpoint) || !isGatewayEndpoint(value.management_endpoint)) return false
  const endpoint = new URL(value.management_endpoint.trim())
  const expected = `/subscriptions/${value.subscription_id.trim()}/resourceGroups/${value.resource_group.trim()}/providers/Microsoft.ApiManagement/service/${value.service_name.trim()}`
  return endpoint.hostname === "management.azure.com" && endpoint.pathname.replace(/\/$/, "").toLowerCase() === expected.toLowerCase()
}

export function apimFormPayload(value: ApimTargetWrite): ApimTargetWrite {
  return Object.fromEntries(Object.entries(value).map(([name, field]) => [name, field.trim()])) as ApimTargetWrite
}

export function apimOnboardingFormValid(value: ApimOnboardingRequest) {
  const uuid = /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i
  const resource = value.resource_id.trim().replace(/\/+$/, "").split("/")
  return uuid.test(value.tenant_id.trim()) && value.resource_id.trim().length <= 1024
    && (value.name?.trim().length ?? 0) <= 120 && resource.length === 9
    && resource[0] === "" && resource[1].toLowerCase() === "subscriptions" && uuid.test(resource[2])
    && resource[3].toLowerCase() === "resourcegroups" && /^[A-Za-z0-9_().-]{1,90}$/.test(resource[4])
    && resource[5].toLowerCase() === "providers" && resource[6].toLowerCase() === "microsoft.apimanagement"
    && resource[7].toLowerCase() === "service" && /^[A-Za-z](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$/.test(resource[8])
}

export function apimOnboardingFormPayload(value: ApimOnboardingRequest): ApimOnboardingRequest {
  return { tenant_id: value.tenant_id.trim(), resource_id: value.resource_id.trim().replace(/\/+$/, ""), name: value.name?.trim() || null }
}