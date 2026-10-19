targetScope = 'resourceGroup'

@description('Existing APIM service that owns the system probe subscription.')
param apimName string

@minLength(1)
@description('Exact existing APIM system probe subscription ID, not an Azure subscription ID.')
param probeSubscriptionId string

@minLength(1)
@description('Object IDs of managed identities that execute Owner diagnostics or Publisher verification.')
param apiPrincipalIds string[]

var probePermissions = loadJsonContent('../../turnstile_core/domain/apim_publication_permissions.json').probe

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource probe 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' existing = {
  parent: apim
  name: probeSubscriptionId
}

resource diagnosticReader 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(probe.id, 'turnstile-runtime-diagnostic-reader')
  properties: {
    roleName: 'Turnstile Runtime Diagnostic Reader ${uniqueString(probe.id)}'
    description: 'Reads only the designated system probe subscription and its keys; cannot rotate or modify subscriptions.'
    type: 'CustomRole'
    permissions: [
      {
        actions: probePermissions
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

resource diagnosticReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in union(apiPrincipalIds, []): {
  name: guid(probe.id, principalId, diagnosticReader.id)
  scope: probe
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: diagnosticReader.id
  }
}]

output probeScope string = probe.id
output roleDefinitionId string = diagnosticReader.id
