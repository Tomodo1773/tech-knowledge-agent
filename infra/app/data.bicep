targetScope = 'resourceGroup'

param location string
param tags object
param accountName string
@secure()
param functionPrincipalId string
@secure()
param deployerPrincipalId string

var databaseName = 'knowledge'
var containerName = 'chunks'
var containerScope = '${resourceId('Microsoft.DocumentDB/databaseAccounts', accountName)}/dbs/${databaseName}/colls/${containerName}'

module cosmos 'br/public:avm/res/document-db/database-account:0.21.0' = {
  name: 'cosmos-db'
  params: {
    name: accountName
    location: location
    disableKeyBasedMetadataWriteAccess: true
    disableLocalAuthentication: true
    enableFreeTier: true
    enableMultipleWriteLocations: false
    enableAutomaticFailover: false
    // Both of the following default the opposite way in this AVM version. A single-user
    // MVP does not buy availability zones, and the default
    // Continuous30Days tier bills for backup storage. Periodic keeps the two free copies.
    zoneRedundant: false
    backupPolicyType: 'Periodic'
    // The AVM default is -1, unlimited. Free Tier covers the first 1,000 RU/s on the
    // account and the single container
    // below provisions 400, so this caps the account at the free allowance instead of
    // letting a later container or an autoscale setting start billing unnoticed. Raising
    // it is a deliberate one-line change; exceeding it silently is not possible.
    totalThroughputLimit: 1000
    // The container below declares a vectorEmbeddingPolicy, which the account rejects
    // unless this capability is enabled first. It is an account-level opt-in and cannot
    // be inferred from the container definition.
    capabilitiesToAdd: ['EnableNoSQLVectorSearch']
    networkRestrictions: {
      networkAclBypass: 'AzureServices'
      publicNetworkAccess: 'Enabled'
    }
    sqlDatabases: [
      {
        name: databaseName
        containers: [
          {
            name: containerName
            paths: ['/corpusId']
            throughput: 400
            indexingPolicy: {
              automatic: true
              indexingMode: 'consistent'
              includedPaths: [
                { path: '/*' }
              ]
              excludedPaths: [
                { path: '/embedding/*' }
              ]
              vectorIndexes: [
                {
                  path: '/embedding'
                  type: 'quantizedFlat'
                }
              ]
            }
            vectorEmbeddingPolicy: {
              vectorEmbeddings: [
                {
                  path: '/embedding'
                  dataType: 'float32'
                  distanceFunction: 'cosine'
                  dimensions: 1536
                }
              ]
            }
          }
        ]
      }
    ]
    sqlRoleAssignments: [
      {
        name: guid(accountName, functionPrincipalId, 'cosmos-data-contributor', containerScope)
        principalId: functionPrincipalId
        roleDefinitionId: '00000000-0000-0000-0000-000000000002'
        scope: containerScope
      }
      // Local authentication is disabled, so the deployer needs a data-plane assignment
      // to run the Functions locally and to
      // inspect chunks during the live gates. Scoped to the same container as the
      // Function App, not the account.
      {
        name: guid(accountName, deployerPrincipalId, 'cosmos-data-contributor', containerScope)
        principalId: deployerPrincipalId
        roleDefinitionId: '00000000-0000-0000-0000-000000000002'
        scope: containerScope
      }
    ]
    // No diagnostic settings: 'allLogs' would stream every DataPlaneRequest into the
    // 0.1 GB/day workspace cap, duplicating the cosmos.* spans. Platform metrics stay
    // available in Azure Monitor without routing them to Log Analytics.
    tags: tags
    enableTelemetry: false
  }
}

@secure()
output accountName string = cosmos.outputs.name
@secure()
output accountResourceId string = cosmos.outputs.resourceId
@secure()
output cosmosEndpoint string = cosmos.outputs.endpoint
@secure()
output databaseName string = databaseName
@secure()
output containerName string = containerName
