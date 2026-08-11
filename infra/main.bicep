targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('azd environment name used only for deterministic resource naming.')
param environmentName string

@description('Azure region for the MVP resources.')
@allowed([
  'japaneast'
  'eastus2'
])
param location string = 'japaneast'

@description('Resource group created for this environment.')
param resourceGroupName string = 'rg-${environmentName}'

@description('Object ID of the person or service principal running azd.')
@secure()
param principalId string

@allowed([
  'User'
  'ServicePrincipal'
])
param principalType string

@description('Email address for Azure Budget notifications. Supply through azd environment state.')
@secure()
param budgetContactEmail string

@description('Public GitHub repository owner; supplied through azd environment state.')
@secure()
param githubOwner string

@description('Public GitHub repository name; supplied through azd environment state.')
@secure()
param githubRepository string

@description('Default branch of the public GitHub repository.')
param githubDefaultBranch string = 'main'

@description('Allowed Slack workspace ID; supplied through azd environment state.')
@secure()
param slackTeamId string

@description('Allowed Slack user ID; supplied through azd environment state.')
@secure()
param slackUserId string

@description('Model deployments serialized by the azure.ai.project extension from azure.yaml.')
param aiProjectDeploymentsJson string = '[]'

@description('Embedding deployment name shared by Functions and the Hosted Agent.')
param embeddingModelDeploymentName string = 'text-embedding-3-small'

var token = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  workload: 'tech-knowledge-agent'
  environment: 'dev'
}
var aiProjectDeployments = json(aiProjectDeploymentsJson)

resource rg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module observability 'app/observability.bicep' = {
  name: 'observability'
  scope: rg
  params: {
    applicationInsightsName: 'appi-${token}'
    budgetContactEmail: budgetContactEmail
    budgetName: 'budget-${environmentName}'
    location: location
    logAnalyticsName: 'log-${token}'
    tags: tags
  }
}

module security 'app/security.bicep' = {
  name: 'security'
  scope: rg
  params: {
    functionIdentityName: 'id-func-${token}'
    keyVaultName: 'kv-${token}'
    location: location
    logAnalyticsResourceId: observability.outputs.logAnalyticsResourceId
    tags: tags
  }
}

module data 'app/data.bicep' = {
  name: 'data'
  scope: rg
  params: {
    accountName: 'cosmos-${token}'
    functionPrincipalId: security.outputs.functionPrincipalId
    location: location
    logAnalyticsResourceId: observability.outputs.logAnalyticsResourceId
    tags: tags
  }
}

module foundry 'app/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    accountName: 'ai-${token}'
    applicationInsightsConnectionString: observability.outputs.applicationInsightsConnectionString
    applicationInsightsResourceId: observability.outputs.applicationInsightsResourceId
    deployerPrincipalId: principalId
    deployerPrincipalType: principalType
    functionPrincipalId: security.outputs.functionPrincipalId
    location: location
    logAnalyticsResourceId: observability.outputs.logAnalyticsResourceId
    projectName: 'project-${environmentName}'
    deployments: aiProjectDeployments
    tags: tags
  }
}

module functions 'app/functions.bicep' = {
  name: 'functions'
  scope: rg
  params: {
    applicationInsightsConnectionString: observability.outputs.applicationInsightsConnectionString
    applicationInsightsResourceId: observability.outputs.applicationInsightsResourceId
    cosmosEndpoint: data.outputs.cosmosEndpoint
    embeddingModelDeploymentName: embeddingModelDeploymentName
    foundryProjectEndpoint: foundry.outputs.projectEndpoint
    functionAppName: 'func-${token}'
    functionIdentityClientId: security.outputs.functionIdentityClientId
    functionIdentityResourceId: security.outputs.functionIdentityResourceId
    functionPrincipalId: security.outputs.functionPrincipalId
    githubDefaultBranch: githubDefaultBranch
    githubOwner: githubOwner
    githubRepository: githubRepository
    keyVaultUri: security.outputs.keyVaultUri
    location: location
    logAnalyticsResourceId: observability.outputs.logAnalyticsResourceId
    planName: 'plan-${token}'
    slackTeamId: slackTeamId
    slackUserId: slackUserId
    storageAccountName: 'st${token}'
    tags: tags
  }
}

// azd and the Foundry extension consume these values locally. Marking identifiers and
// endpoints secure keeps them out of deployment history and normal CI/log output.
@secure()
output AZURE_RESOURCE_GROUP string = rg.name
@secure()
output AZURE_AI_ACCOUNT_NAME string = foundry.outputs.accountName
@secure()
output AZURE_AI_PROJECT_NAME string = foundry.outputs.projectName
@secure()
output AZURE_AI_PROJECT_ID string = foundry.outputs.projectResourceId
@secure()
output AZURE_AI_FOUNDRY_PROJECT_ID string = foundry.outputs.projectResourceId
@secure()
output AZURE_AI_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
@secure()
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
@secure()
output APPLICATIONINSIGHTS_CONNECTION_STRING string = observability.outputs.applicationInsightsConnectionString
@secure()
output APPLICATIONINSIGHTS_RESOURCE_ID string = observability.outputs.applicationInsightsResourceId
@secure()
output COSMOS_ENDPOINT string = data.outputs.cosmosEndpoint
@secure()
output COSMOS_ACCOUNT_NAME string = data.outputs.accountName
@secure()
output EMBEDDING_MODEL_DEPLOYMENT_NAME string = embeddingModelDeploymentName
@secure()
output SERVICE_FUNCTIONS_RESOURCE_NAME string = functions.outputs.functionAppName
