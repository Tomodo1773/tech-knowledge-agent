targetScope = 'resourceGroup'

param location string
param tags object
param functionIdentityName string
param keyVaultName string
param logAnalyticsResourceId string

// No identity AVM version is part of the Step 0 pin set. Keep this single raw resource
// until the next AVM review instead of introducing an unreviewed module dependency.
resource functionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: functionIdentityName
  location: location
  tags: tags
}

module keyVault 'br/public:avm/res/key-vault/vault:0.14.0' = {
  name: 'key-vault'
  params: {
    name: keyVaultName
    location: location
    enableRbacAuthorization: true
    enablePurgeProtection: false
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
    diagnosticSettings: [
      {
        name: 'send-to-workspace'
        workspaceResourceId: logAnalyticsResourceId
      }
    ]
    roleAssignments: [
      {
        principalId: functionIdentity.properties.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Key Vault Secrets User'
      }
    ]
    tags: tags
    enableTelemetry: false
  }
}

@secure()
output functionIdentityResourceId string = functionIdentity.id
@secure()
output functionIdentityClientId string = functionIdentity.properties.clientId
@secure()
output functionPrincipalId string = functionIdentity.properties.principalId
@secure()
output keyVaultResourceId string = keyVault.outputs.resourceId
@secure()
output keyVaultUri string = keyVault.outputs.uri
