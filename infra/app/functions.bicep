targetScope = 'resourceGroup'

param location string
param tags object
param storageAccountName string
param planName string
param functionAppName string
param functionIdentityResourceId string
param functionIdentityClientId string
@secure()
param functionPrincipalId string
param logAnalyticsResourceId string
@secure()
param applicationInsightsConnectionString string
param applicationInsightsResourceId string
@secure()
param cosmosEndpoint string
param embeddingModelDeploymentName string
@secure()
param foundryProjectEndpoint string
@secure()
param keyVaultUri string
@secure()
param githubOwner string
@secure()
param githubRepository string
param githubDefaultBranch string
@secure()
param slackTeamId string
@secure()
param slackUserId string

var monitoringMetricsPublisherRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '3913510d-42f4-4e42-8a64-420c390055eb'
)

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: last(split(applicationInsightsResourceId, '/'))
}

resource functionMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: applicationInsights
  name: guid(applicationInsightsResourceId, functionPrincipalId, monitoringMetricsPublisherRoleId)
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: monitoringMetricsPublisherRoleId
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: 'functions-storage'
  params: {
    name: storageAccountName
    location: location
    skuName: 'Standard_LRS'
    kind: 'StorageV2'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    blobServices: {
      containers: [
        {
          name: 'deployment'
          publicAccess: 'None'
        }
      ]
      diagnosticSettings: [
        {
          name: 'send-to-workspace'
          workspaceResourceId: logAnalyticsResourceId
        }
      ]
    }
    queueServices: {
      queues: [
        { name: 'slack-questions' }
        { name: 'slack-questions-poison' }
      ]
      diagnosticSettings: [
        {
          name: 'send-to-workspace'
          workspaceResourceId: logAnalyticsResourceId
        }
      ]
    }
    tableServices: {
      tables: [
        { name: 'state' }
      ]
      diagnosticSettings: [
        {
          name: 'send-to-workspace'
          workspaceResourceId: logAnalyticsResourceId
        }
      ]
    }
    roleAssignments: [
      {
        principalId: functionPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Storage Blob Data Owner'
      }
      {
        principalId: functionPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Storage Queue Data Contributor'
      }
      {
        principalId: functionPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Storage Table Data Contributor'
      }
    ]
    tags: tags
    enableTelemetry: false
  }
}

module plan 'br/public:avm/res/web/serverfarm:0.7.0' = {
  name: 'functions-plan'
  params: {
    name: planName
    location: location
    kind: 'functionapp'
    reserved: true
    skuName: 'FC1'
    skuCapacity: 0
    zoneRedundant: false
    tags: tags
    enableTelemetry: false
  }
}

module functionApp 'br/public:avm/res/web/site:0.24.0' = {
  name: 'function-app'
  params: {
    name: functionAppName
    location: location
    kind: 'functionapp,linux'
    serverFarmResourceId: plan.outputs.resourceId
    managedIdentities: {
      userAssignedResourceIds: [functionIdentityResourceId]
    }
    keyVaultAccessIdentityResourceId: functionIdentityResourceId
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    basicPublishingCredentialsPolicies: [
      { name: 'ftp', allow: false }
      { name: 'scm', allow: false }
    ]
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: 'https://${storageAccountName}.blob.${environment().suffixes.storage}/deployment'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: functionIdentityResourceId
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
      scaleAndConcurrency: {
        instanceMemoryMB: 2048
        maximumInstanceCount: 20
      }
    }
    configs: [
      {
        name: 'appsettings'
        properties: {
          FUNCTIONS_EXTENSION_VERSION: '~4'
          FUNCTIONS_WORKER_RUNTIME: 'python'
          AzureWebJobsStorage__accountName: storageAccountName
          AzureWebJobsStorage__credential: 'managedidentity'
          AzureWebJobsStorage__clientId: functionIdentityClientId
          APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsightsConnectionString
          APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'Authorization=AAD;ClientId=${functionIdentityClientId}'
          AZURE_CLIENT_ID: functionIdentityClientId
          AZURE_STORAGE_ACCOUNT_NAME: storageAccountName
          COSMOS_ENDPOINT: cosmosEndpoint
          COSMOS_DATABASE_NAME: 'knowledge'
          COSMOS_CONTAINER_NAME: 'chunks'
          FOUNDRY_PROJECT_ENDPOINT: foundryProjectEndpoint
          EMBEDDING_MODEL_DEPLOYMENT_NAME: embeddingModelDeploymentName
          GITHUB_OWNER: githubOwner
          GITHUB_REPOSITORY: githubRepository
          GITHUB_DEFAULT_BRANCH: githubDefaultBranch
          SLACK_ALLOWED_TEAM_ID: slackTeamId
          SLACK_ALLOWED_USER_ID: slackUserId
          SLACK_SIGNING_SECRET: '@Microsoft.KeyVault(SecretUri=${keyVaultUri}secrets/slack-signing-secret/)'
          SLACK_BOT_TOKEN: '@Microsoft.KeyVault(SecretUri=${keyVaultUri}secrets/slack-bot-token/)'
          CHUNKING_VERSION: 'markdown-v1-1600-200'
        }
      }
    ]
    diagnosticSettings: [
      {
        name: 'send-to-workspace'
        workspaceResourceId: logAnalyticsResourceId
      }
    ]
    tags: union(tags, {
      'azd-service-name': 'functions'
    })
    enableTelemetry: false
  }
}

@secure()
output functionAppName string = functionApp.outputs.name
@secure()
output functionAppResourceId string = functionApp.outputs.resourceId
