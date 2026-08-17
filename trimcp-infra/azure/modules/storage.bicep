param deploymentName string
param location string

var stName = toLower(take('sttrimcp${uniqueString(deploymentName, subscription().subscriptionId)}', 24))

resource st 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: stName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: st
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blob
  name: 'trimcp-blobs'
  properties: {
    publicAccess: 'None'
  }
}

output accountName string = st.name
output blobEndpoint string = st.properties.primaryEndpoints.blob
