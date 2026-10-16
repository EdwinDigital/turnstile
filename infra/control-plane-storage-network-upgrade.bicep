targetScope = 'subscription'

@description('Existing Turnstile resource group; this template does not create a resource group.')
@minLength(1)
param resourceGroupName string

@description('Region of the existing Function VNet and private endpoint subnet.')
param location string

@description('Existing Control-plane AzureWebJobsStorage account, not the telemetry or ledger account.')
@minLength(1)
param storageAccountName string

@minLength(1)
param virtualNetworkName string

param privateEndpointSubnetName string = 'snet-private-endpoints'

module storagePrivateNetwork 'modules/storage-private-endpoints.bicep' = {
  name: 'control-plane-storage-private-network'
  scope: resourceGroup(resourceGroupName)
  params: {
    location: location
    storageAccountName: storageAccountName
    virtualNetworkName: virtualNetworkName
    privateEndpointSubnetName: privateEndpointSubnetName
  }
}

output privateEndpointIds array = storagePrivateNetwork.outputs.privateEndpointIds
