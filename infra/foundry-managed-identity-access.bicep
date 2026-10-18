targetScope = 'subscription'

@description('Existing resource group of the customer-approved Foundry account.')
@minLength(1)
param resourceGroupName string

@minLength(2)
param accountName string

@description('Object ID of the APIM managed identity that sends the model requests.')
@minLength(1)
param apimPrincipalId string

module foundryAccess 'modules/foundry-managed-identity-rbac.bicep' = {
  name: 'turnstile-foundry-managed-identity-access'
  scope: resourceGroup(resourceGroupName)
  params: {
    accountName: accountName
    apimPrincipalId: apimPrincipalId
  }
}

output assignmentId string = foundryAccess.outputs.assignmentId
