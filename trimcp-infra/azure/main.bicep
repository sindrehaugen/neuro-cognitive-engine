targetScope = 'subscription'

@minLength(3)
@maxLength(20)
param deploymentName string

@allowed([
  'westeurope'
  'northeurope'
  'eastus'
  'westus2'
])
param region string = 'westeurope'

param environment string = 'dev'

param tenantId string

var rgName = 'rg-trimcp-${deploymentName}'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: rgName
  location: region
  tags: {
    trimcp_deployment: deploymentName
    trimcp_environment: environment
  }
}

module network 'modules/network.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-network-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
  }
}

module keyvault 'modules/keyvault.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-kv-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
    tenantId: tenantId
  }
}

module storage 'modules/storage.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-sto-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
  }
}

module postgres 'modules/postgres.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-pg-placeholder-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
  }
}

module cosmos 'modules/cosmos.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-cosmos-placeholder-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
  }
}

module redis 'modules/redis.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-redis-placeholder-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
  }
}

module containerapp 'modules/containerapp.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-ca-placeholder-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
  }
}

module frontdoor 'modules/frontdoor.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-fd-placeholder-${deploymentName}'
  params: {
    deploymentName: deploymentName
  }
}

module monitoring 'modules/monitoring.bicep' = {
  scope: resourceGroup(rg.name)
  name: 'trimcp-mon-placeholder-${deploymentName}'
  params: {
    deploymentName: deploymentName
    location: region
  }
}

// References only — no connection strings (Appendix I.7).
output resourceGroupName string = rg.name
output vnetId string = network.outputs.vnetId
output keyVaultUri string = keyvault.outputs.vaultUri
output storageAccountName string = storage.outputs.accountName
output webhookEndpoint string = frontdoor.outputs.publicWebhookBaseUrl
output postgresNotes string = postgres.outputs.implementationNote
output cosmosNotes string = cosmos.outputs.implementationNote
output redisNotes string = redis.outputs.implementationNote
output containerAppNotes string = containerapp.outputs.implementationNote
