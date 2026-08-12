targetScope = 'resourceGroup'

param location string
param tags object
param logAnalyticsName string
param applicationInsightsName string

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: 'log-analytics'
  params: {
    name: logAnalyticsName
    location: location
    dailyQuotaGb: '0.1'
    dataRetention: 30
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    tags: tags
    enableTelemetry: false
  }
}

module applicationInsights 'br/public:avm/res/insights/component:0.8.0' = {
  name: 'application-insights'
  params: {
    name: applicationInsightsName
    workspaceResourceId: logAnalytics.outputs.resourceId
    location: location
    disableLocalAuth: true
    retentionInDays: 30
    tags: tags
    enableTelemetry: false
  }
}

@secure()
output logAnalyticsResourceId string = logAnalytics.outputs.resourceId
@secure()
output applicationInsightsResourceId string = applicationInsights.outputs.resourceId
@secure()
output applicationInsightsConnectionString string = applicationInsights.outputs.connectionString
