param deploymentName string
param location string

var vnetName = 'vnet-trimcp-${deploymentName}'

// Appendix I.6: No public DB IPs — apply NSGs compatible with subnet delegation, or private endpoints
// (some delegated subnets cannot host classic NSGs; validate with Microsoft.DBforPostgreSQL requirements).

resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.40.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'snet-app'
        properties: {
          addressPrefix: '10.40.1.0/24'
        }
      }
      {
        name: 'snet-db'
        properties: {
          addressPrefix: '10.40.2.0/24'
          delegations: [
            {
              name: 'pgflex'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output appSubnetId string = '${vnet.id}/subnets/snet-app'
output dbSubnetId string = '${vnet.id}/subnets/snet-db'
