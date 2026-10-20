targetScope = 'resourceGroup'

param apimName string
param controlPlanePrincipalId string
@description('Existing system probe subscription. Omit only before automatic initialization creates it, then apply again with the actual ID. Never substitute a Dashboard subscription.')
param probeSubscriptionId string = ''
param databricksOAuthEnabled bool = false

var roleNameSuffix = uniqueString(resourceGroup().id)
var oauthActions = databricksOAuthEnabled ? [
  'Microsoft.ApiManagement/service/authorizationProviders/read'
  'Microsoft.ApiManagement/service/authorizationProviders/write'
  'Microsoft.ApiManagement/service/authorizationProviders/authorizations/read'
  'Microsoft.ApiManagement/service/authorizationProviders/authorizations/write'
  'Microsoft.ApiManagement/service/authorizationProviders/authorizations/accessPolicies/read'
  'Microsoft.ApiManagement/service/authorizationProviders/authorizations/accessPolicies/write'
] : []

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource publisherRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'turnstile-apim-publisher')
  properties: {
    roleName: 'Turnstile APIM Publisher ${roleNameSuffix}'
    description: 'Publishes Turnstile API resources and provisions governed APIM subscriptions.'
    type: 'CustomRole'
    permissions: [
      {
        actions: concat([
          'Microsoft.Authorization/permissions/read'
          'Microsoft.ApiManagement/service/apis/operations/policies/read'
          'Microsoft.ApiManagement/service/apis/operations/policies/write'
          'Microsoft.ApiManagement/service/apis/operations/read'
          'Microsoft.ApiManagement/service/apis/policies/read'
          'Microsoft.ApiManagement/service/apis/policies/write'
          'Microsoft.ApiManagement/service/apis/products/read'
          'Microsoft.ApiManagement/service/apis/read'
          'Microsoft.ApiManagement/service/apis/releases/read'
          'Microsoft.ApiManagement/service/apis/releases/write'
          'Microsoft.ApiManagement/service/apis/revisions/read'
          'Microsoft.ApiManagement/service/apis/write'
          'Microsoft.ApiManagement/service/backends/read'
          'Microsoft.ApiManagement/service/backends/write'
          'Microsoft.ApiManagement/service/gateways/apis/read'
          'Microsoft.ApiManagement/service/gateways/certificateAuthorities/read'
          'Microsoft.ApiManagement/service/gateways/hostnameConfigurations/read'
          'Microsoft.ApiManagement/service/gateways/read'
          'Microsoft.ApiManagement/service/namedValues/read'
          'Microsoft.ApiManagement/service/namedValues/write'
          'Microsoft.ApiManagement/service/policies/read'
          'Microsoft.ApiManagement/service/policyFragments/read'
          'Microsoft.ApiManagement/service/products/apiLinks/read'
          'Microsoft.ApiManagement/service/products/apis/read'
          'Microsoft.ApiManagement/service/products/policies/read'
          'Microsoft.ApiManagement/service/products/policy/read'
          'Microsoft.ApiManagement/service/products/read'
          'Microsoft.ApiManagement/service/read'
          'Microsoft.ApiManagement/service/subscriptions/read'
          'Microsoft.ApiManagement/service/subscriptions/write'
        ], oauthActions)
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

resource functionApimPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apim.id, controlPlanePrincipalId, 'turnstile-apim-publisher')
  scope: apim
  properties: {
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: publisherRole.id
  }
}

module publisherProbeAccess 'runtime-diagnostic-rbac.bicep' = if (!empty(probeSubscriptionId)) {
  name: 'publisher-probe-${uniqueString(apim.id, controlPlanePrincipalId, probeSubscriptionId)}'
  params: {
    apimName: apimName
    probeSubscriptionId: probeSubscriptionId
    apiPrincipalIds: [controlPlanePrincipalId]
  }
}

output probeScope string = !empty(probeSubscriptionId) ? publisherProbeAccess!.outputs.probeScope : ''
