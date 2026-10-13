import type {
  ManagedModel,
  ModelRegistry,
  ModelRuntime,
} from "../../data-sources/apim/types"

function configValue(runtime: ModelRuntime, key: string, fallback: unknown = null) {
  return runtime.config[key] ?? fallback
}

function apiFormat(runtime: ModelRuntime) {
  const configured = configValue(runtime, "api_format", "")
  return configured === "anthropic_messages"
    || runtime.brand_key === "amazon_bedrock"
    || runtime.brand_key === "azure_databricks"
    ? "anthropic_messages"
    : "openai_chat"
}

function authCompatible(runtime: ModelRuntime, primary: ModelRuntime) {
  const primaryAuth = String(configValue(primary, "auth_strategy", "none"))
  const runtimeAuth = String(configValue(runtime, "auth_strategy", "none"))
  const namedAuth = runtimeAuth === "named_value_api_key"
    || runtimeAuth === "named_value_bearer"
  if (namedAuth && (
    !configValue(runtime, "named_value_name")
    || configValue(runtime, "credential_provisioned", false) !== true
  )) return false
  if (primaryAuth === "managed_identity") {
    return runtimeAuth === "named_value_api_key"
      || (runtimeAuth === "managed_identity"
        && configValue(runtime, "managed_identity_resource")
          === configValue(primary, "managed_identity_resource"))
  }
  return runtimeAuth === primaryAuth
}

function runtimeCompatible(runtime: ModelRuntime, primary: ModelRuntime) {
  return Boolean(
    runtime.enabled
    && runtime.provider_id === primary.provider_id
    && runtime.gateway_profile_id === primary.gateway_profile_id
    && runtime.config.control_plane_managed === true
    && primary.config.control_plane_managed === true
    && runtime.config.backend_url
    && runtime.config.backend_path
    && primary.config.backend_url
    && primary.config.backend_path
    && apiFormat(runtime) === apiFormat(primary)
    && configValue(runtime, "backend_path") === configValue(primary, "backend_path")
    && authCompatible(runtime, primary)
    && configValue(runtime, "streaming_mode", "native")
      === configValue(primary, "streaming_mode", "native")
  )
}

export function nativeRouteUpstreamIdentity(model: ManagedModel) {
  return (model.upstream_model_id ?? model.model_key).trim().toLocaleLowerCase()
}

export function compatibleNativeRouteRuntimes(
  model: ManagedModel,
  registry: ModelRegistry,
) {
  if (model.capabilities.includes("image_generation")) return []
  const primary = registry.runtimes.find((runtime) => runtime.id === model.runtime_id)
  if (!primary) return []
  return registry.runtimes.filter((runtime) => runtimeCompatible(runtime, primary))
}

export function equivalentNativeRouteRuntimes(
  model: ManagedModel,
  registry: ModelRegistry,
) {
  const primary = registry.runtimes.find((runtime) => runtime.id === model.runtime_id)
  if (!primary) return []
  const upstream = nativeRouteUpstreamIdentity(model)
  const runtimeIdsWithDeployment = new Set(
    registry.models
      .filter((candidate) =>
        candidate.enabled
        && candidate.provider_id === model.provider_id
        && nativeRouteUpstreamIdentity(candidate) === upstream
      )
      .map((candidate) => candidate.runtime_id),
  )
  return compatibleNativeRouteRuntimes(model, registry).filter((runtime) =>
    runtimeIdsWithDeployment.has(runtime.id)
  )
}

export function hasNativeRouteCounterpart(
  model: ManagedModel,
  registry: ModelRegistry,
) {
  return equivalentNativeRouteRuntimes(model, registry)
    .some((runtime) => runtime.id !== model.runtime_id)
}
