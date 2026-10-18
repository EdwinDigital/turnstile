targetScope = 'resourceGroup'

param location string
param storageAccountName string
param virtualNetworkName string
param privateEndpointSubnetName string = 'snet-private-endpoints'

var services = [
  'blob'
  'queue'
  'table'
]

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: virtualNetworkName
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
}

resource privateDnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' existing = [for service in services: {
  name: 'privatelink.${service}.${environment().suffixes.storage}'
}]

resource privateEndpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for service in services: {
  name: 'pe-${storageAccountName}-${service}'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'storage-${service}'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            service
          ]
        }
      }
    ]
  }
}]

resource privateDnsZoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (service, index) in services: {
  parent: privateEndpoints[index]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: service
        properties: {
          privateDnsZoneId: privateDnsZones[index].id
        }
      }
    ]
  }
}]

output privateEndpointIds array = [for (service, index) in services: privateEndpoints[index].id]
