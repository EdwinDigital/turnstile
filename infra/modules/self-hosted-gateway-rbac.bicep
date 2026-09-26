targetScope = 'resourceGroup'

param apimName string
param controlPlanePrincipalId string
param apiPrincipalId string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

var roleNameSuffix = uniqueString(apim.id)

resource provisionerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(apim.id, 'turnstile-self-hosted-provisioner')
  properties: {
    roleName: 'Turnstile Self-hosted Gateway Provisioner ${roleNameSuffix}'
    description: 'Creates Gateway configurations and API associations on one registered APIM.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Authorization/permissions/read'
          'Microsoft.ApiManagement/service/read'
          'Microsoft.ApiManagement/service/apis/read'
          'Microsoft.ApiManagement/service/gateways/read'
          'Microsoft.ApiManagement/service/gateways/write'
          'Microsoft.ApiManagement/service/gateways/apis/read'
          'Microsoft.ApiManagement/service/gateways/apis/write'
          'Microsoft.ApiManagement/service/gateways/hostnameConfigurations/read'
          'Microsoft.ApiManagement/service/gateways/certificateAuthorities/read'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [resourceGroup().id]
  }
}

resource bootstrapRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(apim.id, 'turnstile-self-hosted-bootstrap')
  properties: {
    roleName: 'Turnstile Self-hosted Gateway Bootstrap ${roleNameSuffix}'
    description: 'Reads Gateway ownership and issues Gateway-specific short-lived startup tokens.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.ApiManagement/service/gateways/read'
          'Microsoft.ApiManagement/service/gateways/apis/read'
          'Microsoft.ApiManagement/service/gateways/generateToken/action'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [resourceGroup().id]
  }
}

resource workerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apim.id, controlPlanePrincipalId, 'turnstile-self-hosted-provisioner')
  scope: apim
  properties: {
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: provisionerRole.id
  }
}

resource apiAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apim.id, apiPrincipalId, 'turnstile-self-hosted-bootstrap')
  scope: apim
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: bootstrapRole.id
  }
}