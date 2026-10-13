import type { ModelProvider, ModelRegistry, ModelRuntime } from "../../data-sources/apim/types"

export function isDatabricksWorkspaceUrl(value: string) {
  try {
    const endpoint = new URL(value.trim())
    return endpoint.protocol === "https:"
      && endpoint.hostname.endsWith(".azuredatabricks.net")
      && !endpoint.username && !endpoint.password && !endpoint.port
      && ["", "/"].includes(endpoint.pathname)
      && !endpoint.search && !endpoint.hash
  } catch {
    return false
  }
}

export function publicationRuntimeEligible(runtime: ModelRuntime) {
  if (runtime.brand_key === "azure_databricks") {
    return runtime.config.control_plane_managed === true
      && runtime.config.api_format === "anthropic_messages"
      && typeof runtime.config.workspace_url === "string"
      && isDatabricksWorkspaceUrl(runtime.config.workspace_url)
  }
  if (runtime.brand_key === "microsoft_foundry") {
    return runtime.config.control_plane_managed === true
      && typeof runtime.config.project_endpoint === "string"
      && runtime.config.project_endpoint.length > 0
  }
  return runtime.config.api_format === "anthropic_messages"
    || runtime.config.api_format === "openai_chat"
    || runtime.brand_key === "amazon_bedrock"
}

/** The target gateway owns the connection inventory, including the empty state. */
export function publicationConnections(registry: ModelRegistry, gatewayId: string) {
  const gateway = registry.gateways.find((item) => item.id === gatewayId)
  if (!gateway?.enabled || gateway.implementation !== "apim") return []
  const providerIds = new Set(registry.providers
    .filter((provider) => provider.enabled)
    .map((provider) => provider.id))
  return registry.runtimes.filter((runtime) => runtime.gateway_profile_id === gatewayId
    && runtime.enabled
    && providerIds.has(runtime.provider_id)
    && (runtime.brand_key !== "azure_databricks" || registry.databricks_connections_supported === true)
    && (runtime.config.auth_strategy !== "oauth_client_credentials" || registry.databricks_oauth_supported === true)
    && publicationRuntimeEligible(runtime))
}

export function preferredPublicationConnection(
  connections: ModelRuntime[],
  preferredId?: string,
) {
  return connections.find((runtime) => runtime.id === preferredId)
    ?? connections.find((runtime) => runtime.is_default)
    ?? connections[0]
}

export function publicationConnectionAuth(runtime: ModelRuntime, provider?: ModelProvider) {
  const strategy = runtime.config.auth_strategy
  if (strategy === "named_value_bearer" || strategy === "named_value_api_key") return "api_key"
  if (strategy === "managed_identity") return "managed_identity"
  if (strategy === "oauth_client_credentials") return "oauth_m2m"
  if (provider?.auth_type === "azure_ad") return "managed_identity"
  if (provider?.auth_type === "api_key") return "api_key"
  return "connection"
}

/** Display only endpoint identity, never credentials, query parameters or config JSON. */
export function publicationConnectionEndpoint(runtime: ModelRuntime) {
  for (const key of ["project_endpoint", "workspace_url", "base_url", "backend_url"]) {
    const value = runtime.config[key]
    if (typeof value !== "string" || !value.trim()) continue
    try {
      const endpoint = new URL(value)
      if (endpoint.protocol !== "https:") continue
      return `${endpoint.origin}${endpoint.pathname.replace(/\/$/, "")}`
    } catch {
      // Historical connections can lack a normalized endpoint; do not guess one.
    }
  }
  return null
}