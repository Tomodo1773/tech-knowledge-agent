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
@secure()
param deployerPrincipalId string
@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string
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
    // This AVM version leaves networkAcls unset, which resolves to defaultAction Deny and
    // blocks both the Flex Consumption host and azd deploy's local zip upload. There is no
    // VNet in this individual-dev MVP (docs/platform-and-operations.md#ローカル開発), so allow
    // public access explicitly instead of listing IP rules.
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    // No service-level diagnostic settings: the AVM 'allLogs' default records every
    // blob, queue, and table transaction, including the host's queue polling. That
    // duplicates the queue.* and cosmos.* spans and is the fastest way to exhaust the
    // 0.1 GB/day workspace cap. Platform metrics remain available without them.
    blobServices: {
      containers: [
        {
          name: 'deployment'
          publicAccess: 'None'
        }
      ]
    }
    queueServices: {
      queues: [
        { name: 'slack-questions' }
        { name: 'slack-questions-poison' }
      ]
    }
    tableServices: {
      tables: [
        { name: 'state' }
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
      // Shared key access is disabled, so the deployer needs data-plane roles to run the
      // Functions locally and to inspect the queue, poison queue, and state table during
      // the live gates. Contributor, not the Owner the Flex host itself requires.
      {
        principalId: deployerPrincipalId
        principalType: deployerPrincipalType
        roleDefinitionIdOrName: 'Storage Blob Data Contributor'
      }
      {
        principalId: deployerPrincipalId
        principalType: deployerPrincipalType
        roleDefinitionIdOrName: 'Storage Queue Data Contributor'
      }
      {
        principalId: deployerPrincipalId
        principalType: deployerPrincipalType
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
    // This AVM version defaults siteConfig to { alwaysOn: true, ... }, and Flex
    // Consumption rejects alwaysOn outright. The module passes siteConfig straight
    // through, so the default has to be replaced rather than patched.
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'FtpsOnly'
    }
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
          // FUNCTIONS_EXTENSION_VERSION and FUNCTIONS_WORKER_RUNTIME are deprecated in
          // Flex Consumption and rejected as invalid app settings; the runtime comes from
          // functionAppConfig.runtime above.
          AzureWebJobsStorage__accountName: storageAccountName
          AzureWebJobsStorage__credential: 'managedidentity'
          AzureWebJobsStorage__clientId: functionIdentityClientId
          APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsightsConnectionString
          APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'Authorization=AAD;ClientId=${functionIdentityClientId}'
          // Lets the Python worker stream OpenTelemetry directly, so host and worker
          // spans correlate without duplicate host-level entries.
          PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY: 'true'
          // The worker passes this to configure_azure_monitor as logger_name and collects
          // that subtree alone. Its default is the root logger, which collects the
          // exporter's own records, so delivering telemetry produces telemetry. Must stay
          // equal to LOGGER_NAMESPACE in knowledge_agent/telemetry.py.
          PYTHON_APPLICATIONINSIGHTS_LOGGER_NAME: 'knowledge_agent'
          AZURE_CLIENT_ID: functionIdentityClientId
          AZURE_STORAGE_ACCOUNT_NAME: storageAccountName
          // The database and container names are fixed contract constants in
          // contracts.py, not settings, so they are deliberately not app settings here.
          COSMOS_ENDPOINT: cosmosEndpoint
          FOUNDRY_PROJECT_ENDPOINT: foundryProjectEndpoint
          EMBEDDING_MODEL_DEPLOYMENT_NAME: embeddingModelDeploymentName
          GITHUB_OWNER: githubOwner
          GITHUB_REPOSITORY: githubRepository
          GITHUB_DEFAULT_BRANCH: githubDefaultBranch
          GITHUB_TOKEN: '@Microsoft.KeyVault(SecretUri=${keyVaultUri}secrets/github-token/)'
          SLACK_ALLOWED_TEAM_ID: slackTeamId
          SLACK_ALLOWED_USER_ID: slackUserId
          SLACK_SIGNING_SECRET: '@Microsoft.KeyVault(SecretUri=${keyVaultUri}secrets/slack-signing-secret/)'
          SLACK_BOT_TOKEN: '@Microsoft.KeyVault(SecretUri=${keyVaultUri}secrets/slack-bot-token/)'
          CHUNKING_VERSION: 'markdown-v1-1600-200'
        }
      }
    ]
    // No diagnostic settings: host.json sets telemetryMode OpenTelemetry, so host and
    // worker logs already reach the same workspace through Application Insights.
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
