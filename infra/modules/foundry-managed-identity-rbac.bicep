targetScope = 'resourceGroup'

@minLength(2)
param accountName string

@description('Customer-approved APIM identity. Cognitive Services User includes account key access.')
@minLength(1)
param apimPrincipalId string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

var inferenceRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')

resource foundryAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, apimPrincipalId, inferenceRoleId)
  scope: account
  properties: {
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: inferenceRoleId
  }
}

output assignmentId string = foundryAccess.id
