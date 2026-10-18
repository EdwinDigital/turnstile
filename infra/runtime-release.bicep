targetScope = 'resourceGroup'

@description('Existing Turnstile API Web App name.')
param apiName string

@description('Existing Turnstile control-plane Function App name.')
param controlPlaneFunctionName string

@description('Published APIM API URL including the API path.')
param apimGatewayUrl string

@description('Server-side APIM subscription key used only by authenticated dashboard invocations.')
@secure()
param apimSubscriptionKey string

@description('Response observer App Service origin.')
param usageObserverUrl string

@description('Secret APIM Named Value containing the observer authentication key.')
param usageObserverKeyNamedValue string

param publicationWorkerEnabled bool = true
param releaseWorkerEnabled bool = true
param apimTargetRegistryEnabled bool = true
param selfHostedGatewayEnabled bool = true
param selfHostedGatewayProvisioningEnabled bool = true
param multiApimTargetRolloutEnabled bool = true

@description('Existing API settings read immediately before deployment so unrelated values are preserved.')
@secure()
param currentApiSettings object

@description('Existing control-plane settings read immediately before deployment so unrelated values are preserved.')
@secure()
param currentControlPlaneSettings object

resource api 'Microsoft.Web/sites@2024-11-01' existing = {
  name: apiName
}

resource controlPlane 'Microsoft.Web/sites@2024-11-01' existing = {
  name: controlPlaneFunctionName
}

resource apiSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: api
  name: 'appsettings'
  properties: union(currentApiSettings, {
    APIM_GATEWAY_URL: apimGatewayUrl
    APIM_DASHBOARD_SUBSCRIPTION_KEY: apimSubscriptionKey
    APIM_USAGE_OBSERVER_URL: usageObserverUrl
    APIM_USAGE_OBSERVER_KEY_NAMED_VALUE: usageObserverKeyNamedValue
    GATEWAY_RELEASE_WORKER_ENABLED: string(releaseWorkerEnabled)
    APIM_TARGET_REGISTRY_ENABLED: string(apimTargetRegistryEnabled)
    SELF_HOSTED_GATEWAY_ENABLED: string(selfHostedGatewayEnabled)
    SELF_HOSTED_GATEWAY_PROVISIONING_ENABLED: string(selfHostedGatewayProvisioningEnabled)
    MULTI_APIM_TARGET_ROLLOUT_ENABLED: string(multiApimTargetRolloutEnabled)
  })
}

resource controlPlaneSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: controlPlane
  name: 'appsettings'
  properties: union(currentControlPlaneSettings, {
    CONTROL_PLANE_ENABLED: 'true'
    GATEWAY_PUBLICATION_WORKER_ENABLED: string(publicationWorkerEnabled)
    GATEWAY_RELEASE_WORKER_ENABLED: string(releaseWorkerEnabled)
    APIM_GATEWAY_URL: apimGatewayUrl
    APIM_USAGE_OBSERVER_URL: usageObserverUrl
    APIM_USAGE_OBSERVER_KEY_NAMED_VALUE: usageObserverKeyNamedValue
    APIM_TARGET_REGISTRY_ENABLED: string(apimTargetRegistryEnabled)
    SELF_HOSTED_GATEWAY_ENABLED: string(selfHostedGatewayEnabled)
    SELF_HOSTED_GATEWAY_PROVISIONING_ENABLED: string(selfHostedGatewayProvisioningEnabled)
    MULTI_APIM_TARGET_ROLLOUT_ENABLED: string(multiApimTargetRolloutEnabled)
  })
}

output apiName string = api.name
output controlPlaneFunctionName string = controlPlane.name
