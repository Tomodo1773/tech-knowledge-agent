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
    // The AVM default is true, which stores the caller's full IP address on every request
    // and dependency instead of masking it. Slack and the Azure services are the only
    // callers today, but the Function App's HTTP trigger is reachable from anywhere and a
    // direct call would record the caller's address. Nothing in docs/telemetry.md reads
    // client IP, so keep masking on.
    disableIpMasking: false
    // Ingestion sampling stays off; the daily cap and 30-day retention are what hold the
    // volume (docs/telemetry.md). This matches the AVM default, but the default is the
    // one lever that would silently discard telemetry if a later version changed it.
    samplingPercentage: 100
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
