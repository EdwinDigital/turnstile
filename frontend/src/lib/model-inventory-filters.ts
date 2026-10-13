import type { ApimTargetTopologyList, GatewayProfile, GatewayPublication, ManagedModel, ModelProvider, ModelRuntime } from "../data-sources/apim/types"
import { modelTargetDetails } from "./gateway-topology"

export const MODEL_FILTER_DIMENSIONS = ["family", "platform", "capability", "connection", "apim", "status"] as const
export type ModelFilterDimension = typeof MODEL_FILTER_DIMENSIONS[number]
export type ModelInventoryFilters = Record<ModelFilterDimension, string[]>
export type ModelFilterRecord = Record<ModelFilterDimension, string[]> & { searchText: string }
export type ModelInventoryFamily = "openai" | "claude" | "deepseek" | "kimi" | "generic"

export function emptyModelFilters(): ModelInventoryFilters {
  return { family: [], platform: [], capability: [], connection: [], apim: [], status: [] }
}

export function modelInventoryFamily(model: Pick<ManagedModel, "model_key" | "display_name" | "family_key" | "upstream_model_id">): ModelInventoryFamily {
  const identity = `${model.model_key} ${model.display_name} ${model.upstream_model_id ?? ""}`.toLowerCase()
  if (identity.includes("deepseek")) return "deepseek"
  if (identity.includes("kimi") || identity.includes("moonshot")) return "kimi"
  if (model.family_key && model.family_key !== "generic") return model.family_key
  if (identity.includes("claude") || identity.includes("anthropic")) return "claude"
  if (identity.includes("gpt") || identity.includes("openai")) return "openai"
  return "generic"
}

export function modelInventoryPlatform(
  runtime?: Pick<ModelRuntime, "brand_key" | "runtime_kind">,
  provider?: Pick<ModelProvider, "brand_key" | "provider_kind">,
): string {
  if (runtime?.brand_key && runtime.brand_key !== "generic") return runtime.brand_key
  if (provider?.brand_key && provider.brand_key !== "generic") return provider.brand_key
  if (runtime?.runtime_kind === "foundry") return "microsoft_foundry"
  return provider?.provider_kind ?? runtime?.runtime_kind ?? "unknown"
}

export function modelFilterRecord(
  model: ManagedModel,
  runtime?: ModelRuntime,
  provider?: ModelProvider,
  topology: ApimTargetTopologyList | null = null,
  profiles: readonly GatewayProfile[] = [],
): ModelFilterRecord {
  const targets = (topology?.items ?? []).map(target => ({
    id: target.target.gateway_profile_id,
    gateways: modelTargetDetails(model, { gateway_profile_id: target.target.gateway_profile_id }, profiles, topology).gateways,
  })).filter(target => target.gateways.length > 0)
  const primaryApim = profiles.find(profile => profile.id === runtime?.gateway_profile_id && profile.implementation === "apim")
  return {
    family: [modelInventoryFamily(model)],
    platform: [modelInventoryPlatform(runtime, provider)],
    capability: model.capabilities.includes("image_generation")
      ? ["image_generation"]
      : model.capabilities.includes("decision") ? ["decision"]
      : model.capabilities.includes("chat") ? ["chat"] : [],
    connection: [model.runtime_id],
    apim: [...new Set([...targets.flatMap(target => [target.id, ...target.gateways.map(gateway => `${target.id}:${gateway.id}`)]), ...(primaryApim ? [primaryApim.id] : [])])],
    status: [model.enabled ? "enabled" : "disabled"],
    searchText: [model.display_name, model.model_key, model.upstream_model_id, model.runtime_name, model.provider_name].filter(Boolean).join(" "),
  }
}

export function matchesModelFilters(record: ModelFilterRecord, filters: ModelInventoryFilters, search = ""): boolean {
  return record.searchText.toLowerCase().includes(search.trim().toLowerCase())
    && MODEL_FILTER_DIMENSIONS.every(dimension => !filters[dimension].length
      || filters[dimension].some(value => record[dimension].includes(value)))
}

export function publicationFilterRecord(publication: GatewayPublication): ModelFilterRecord {
  return {
    family: [modelInventoryFamily({ ...publication, family_key: "generic", upstream_model_id: null })],
    platform: [], capability: [], connection: [],
    apim: [publication.gateway_profile_id, ...(publication.target_deployments ?? []).map(target => `${publication.gateway_profile_id}:${target.deployment_id}`)],
    status: [publication.status === "failed" ? "failed" : "pending"],
    searchText: `${publication.display_name} ${publication.model_key}`,
  }
}

export function toggleModelFilter(filters: ModelInventoryFilters, dimension: ModelFilterDimension, value: string): ModelInventoryFilters {
  const selected = filters[dimension]
  if (dimension === "apim" && !selected.includes(value)) {
    const parentId = value.split(":")[0]
    const retained = selected.filter(item => value.includes(":") ? item !== parentId : !item.startsWith(`${value}:`))
    return { ...filters, apim: [...retained, value] }
  }
  return { ...filters, [dimension]: selected.includes(value) ? selected.filter(item => item !== value) : [...selected, value] }
}