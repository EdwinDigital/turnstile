targetScope = 'subscription'

param apimResourceGroupName string
param apimName string
param controlPlanePrincipalId string
param apiPrincipalId string

resource apimResourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' existing = {
  name: apimResourceGroupName
}

module gatewayAccess 'modules/self-hosted-gateway-rbac.bicep' = {
  name: 'turnstile-self-hosted-gateway-access'
  scope: apimResourceGroup
  params: {
    apimName: apimName
    controlPlanePrincipalId: controlPlanePrincipalId
    apiPrincipalId: apiPrincipalId
  }
}
