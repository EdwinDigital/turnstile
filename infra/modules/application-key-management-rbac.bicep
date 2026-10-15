targetScope = 'resourceGroup'

param apimName string
param apiPrincipalIds array

var roleNameSuffix = uniqueString(resourceGroup().id)

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource keyOperatorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'turnstile-apim-subscription-key-operator')
  properties: {
    roleName: 'Turnstile APIM Subscription Key Operator ${roleNameSuffix}'
    description: 'Allows the Turnstile API to reveal and rotate keys for governed APIM subscriptions.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.ApiManagement/service/subscriptions/read'
          'Microsoft.ApiManagement/service/subscriptions/listSecrets/action'
          'Microsoft.ApiManagement/service/subscriptions/regeneratePrimaryKey/action'
          'Microsoft.ApiManagement/service/subscriptions/regenerateSecondaryKey/action'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource apiKeyOperators 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for apiPrincipalId in apiPrincipalIds: {
  name: guid(apim.id, apiPrincipalId, 'turnstile-apim-subscription-key-operator')
  scope: apim
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyOperatorRole.id
  }
}]

resource inventoryReaderRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'turnstile-apim-inventory-reader')
  properties: {
    roleName: 'Turnstile APIM Inventory Reader ${roleNameSuffix}'
    description: 'Reads registered APIM service, API, policy and Gateway inventory for Turnstile.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Authorization/permissions/read'
          'Microsoft.ApiManagement/service/read'
          'Microsoft.ApiManagement/service/apis/read'
          'Microsoft.ApiManagement/service/apis/operations/read'
          'Microsoft.ApiManagement/service/apis/policies/read'
          'Microsoft.ApiManagement/service/apis/operations/policies/read'
          'Microsoft.ApiManagement/service/gateways/read'
          'Microsoft.ApiManagement/service/gateways/apis/read'
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

resource apiInventoryReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for apiPrincipalId in apiPrincipalIds: {
  name: guid(apim.id, apiPrincipalId, 'turnstile-apim-inventory-reader')
  scope: apim
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: inventoryReaderRole.id
  }
}]
