targetScope = 'resourceGroup'

param location string
param tags object
param functionIdentityName string
param keyVaultName string
param logAnalyticsResourceId string
@secure()
param deployerPrincipalId string
@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string

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
    // The AVM default is 'premium'. Only secrets are stored here, so the HSM-backed
    // tier buys nothing for this MVP.
    sku: 'standard'
    enableRbacAuthorization: true
    enablePurgeProtection: false
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
    // AuditEvent is the one log with no platform-metric equivalent, and it is how a
    // failed Key Vault reference from the Function App gets diagnosed. Everything the
    // AVM 'allLogs' default would add is either policy noise or already covered by the
    // OpenTelemetry spans in docs/quality.md.
    diagnosticSettings: [
      {
        name: 'send-to-workspace'
        workspaceResourceId: logAnalyticsResourceId
        logCategoriesAndGroups: [
          { category: 'AuditEvent' }
        ]
        metricCategories: []
      }
    ]
    roleAssignments: [
      {
        principalId: functionIdentity.properties.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Key Vault Secrets User'
      }
      // Deployer data-plane access. Provisioning already requires Owner or RBAC
      // Administrator to create role assignments, so this grants nothing the deployer
      // could not self-assign; it makes the bootstrap in docs/platform-and-operations.md
      // reproducible instead of a hidden manual step. Secrets Officer is the narrowest
      // built-in role that can write a secret. Remove this entry to bootstrap by hand.
      {
        principalId: deployerPrincipalId
        principalType: deployerPrincipalType
        roleDefinitionIdOrName: 'Key Vault Secrets Officer'
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
