targetScope = 'resourceGroup'

param location string
param tags object
param accountName string
param projectName string
param logAnalyticsResourceId string
param applicationInsightsResourceId string
@secure()
param applicationInsightsConnectionString string
param deployments array
@secure()
param functionPrincipalId string
@secure()
param deployerPrincipalId string
@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string

var foundryUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)
var foundryProjectManagerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'eadc314b-1a2d-4efa-be10-5d325db5065e'
)
var logAnalyticsDataReaderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '3baa9e53-54e7-4a95-bca5-8eaae8b9c3a4'
)
var monitoringMetricsPublisherRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '3913510d-42f4-4e42-8a64-420c390055eb'
)

module account 'br/public:avm/res/cognitive-services/account:0.18.0' = {
  name: 'foundry-account'
  params: {
    name: accountName
    kind: 'AIServices'
    location: location
    sku: 'S0'
    customSubDomainName: accountName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
    managedIdentities: {
      systemAssigned: true
    }
    deployments: deployments
    // No diagnostic settings: the AVM 'allLogs' default adds RequestResponse and Trace
    // rows for the same calls the Hosted Agent already reports through OpenTelemetry
    // with content capture. logAnalyticsResourceId is still needed below for the
    // project's Log Analytics Data Reader assignment.
    tags: tags
    enableTelemetry: false
  }
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

// The pinned cognitive-services AVM does not expose Foundry project children.
// Re-evaluate this raw child when the next pinned AVM adds project parameters.
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'Personal technical knowledge agent project'
    displayName: projectName
  }
  tags: tags
  dependsOn: [account]
}

// Foundry connection resources are not exposed by the pinned account AVM.
resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-09-01' = {
  parent: project
  name: 'application-insights'
  properties: {
    category: 'AppInsights'
    target: applicationInsightsResourceId
    // The 0.42.1 type bundle lags the official 2025-09-01 Foundry sample.
    #disable-next-line BCP036
    authType: 'ProjectManagedIdentity'
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: applicationInsightsResourceId
      ApplicationInsightsConnectionString: applicationInsightsConnectionString
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: last(split(applicationInsightsResourceId, '/'))
}

resource projectMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: applicationInsights
  name: guid(applicationInsightsResourceId, project.id, monitoringMetricsPublisherRoleId)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: monitoringMetricsPublisherRoleId
  }
}

resource projectFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, project.id, foundryUserRoleId)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryUserRoleId
  }
}

resource functionProjectUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, functionPrincipalId, foundryUserRoleId)
  properties: {
    principalId: functionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryUserRoleId
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-02-01' existing = {
  name: last(split(logAnalyticsResourceId, '/'))
}

resource projectLogReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(logAnalyticsResourceId, project.id, logAnalyticsDataReaderRoleId)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: logAnalyticsDataReaderRoleId
  }
}

resource deployerProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, deployerPrincipalId, foundryProjectManagerRoleId)
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: foundryProjectManagerRoleId
  }
}

@secure()
output accountName string = accountName
@secure()
output projectName string = project.name
@secure()
output projectResourceId string = project.id
@secure()
output projectEndpoint string = project.properties.endpoints['AI Foundry API']
