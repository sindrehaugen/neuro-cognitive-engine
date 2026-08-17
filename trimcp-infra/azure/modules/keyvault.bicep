param deploymentName string
param location string
param tenantId string

var raw = toLower(replace('kv-trimcp-${deploymentName}', '-', ''))
var vaultName = length(raw) > 24 ? substring(raw, 0, 24) : raw

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenantId
    enableRbacAuthorization: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
  }
}

output vaultUri string = kv.properties.vaultUri
output vaultName string = kv.name
