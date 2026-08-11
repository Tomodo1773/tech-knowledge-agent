targetScope = 'resourceGroup'

param location string
param tags object
param logAnalyticsName string
param applicationInsightsName string
param budgetName string
@secure()
param budgetContactEmail string

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

module actualBudget 'br/public:avm/res/consumption/budget/rg-scope:0.1.0' = {
  name: 'monthly-actual-budget'
  params: {
    name: '${budgetName}-actual'
    amount: 1000
    contactEmails: [budgetContactEmail]
    operator: 'GreaterThanOrEqualTo'
    resetPeriod: 'Monthly'
    thresholds: [80]
    thresholdType: 'Actual'
    enableTelemetry: false
  }
}

// The AVM exposes one threshold type per budget. Keep forecast and actual alerts
// as separate, low-cost policy resources instead of falling back to raw Bicep.
module forecastBudget 'br/public:avm/res/consumption/budget/rg-scope:0.1.0' = {
  name: 'monthly-forecast-budget'
  params: {
    name: '${budgetName}-forecast'
    amount: 1000
    contactEmails: [budgetContactEmail]
    operator: 'GreaterThanOrEqualTo'
    resetPeriod: 'Monthly'
    thresholds: [100]
    thresholdType: 'Forecasted'
    enableTelemetry: false
  }
}

@secure()
output logAnalyticsResourceId string = logAnalytics.outputs.resourceId
@secure()
output applicationInsightsResourceId string = applicationInsights.outputs.resourceId
@secure()
output applicationInsightsConnectionString string = applicationInsights.outputs.connectionString
