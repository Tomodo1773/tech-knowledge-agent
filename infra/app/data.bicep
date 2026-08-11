targetScope = 'resourceGroup'

param location string
param tags object
param accountName string
@secure()
param functionPrincipalId string
param logAnalyticsResourceId string

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
    ]
    diagnosticSettings: [
      {
        name: 'send-to-workspace'
        workspaceResourceId: logAnalyticsResourceId
      }
    ]
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
