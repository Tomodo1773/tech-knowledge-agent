$ErrorActionPreference = 'Stop'
# Without this, PowerShell resolves an unassigned variable to $null and a check that reads
# one silently degenerates into a no-op: a regex over $null matches nothing, so the loop
# runs zero times and the script reports success. That is exactly how the azure.yaml
# ${VAR} check below sat dead from the day it was added.
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot
$requiredFiles = @(
    'infra/main.bicep',
    'infra/main.parameters.json',
    'infra/app/data.bicep',
    'infra/app/foundry.bicep',
    'infra/app/functions.bicep',
    'infra/app/observability.bicep',
    'infra/app/security.bicep',
    'scripts/assign-agent-roles.ps1'
)

foreach ($relativePath in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relativePath))) {
        throw "Required infrastructure file is missing: $relativePath"
    }
}

$expectedModules = @(
    'avm/res/web/serverfarm:0.7.0',
    'avm/res/web/site:0.24.0',
    'avm/res/storage/storage-account:0.33.0',
    'avm/res/document-db/database-account:0.21.0',
    'avm/res/cognitive-services/account:0.18.0',
    'avm/res/operational-insights/workspace:0.16.1',
    'avm/res/insights/component:0.8.0',
    'avm/res/key-vault/vault:0.14.0'
)

$bicepFiles = Get-ChildItem -LiteralPath (Join-Path $root 'infra') -Filter '*.bicep' -Recurse
$bicep = ($bicepFiles | ForEach-Object { Get-Content -Raw -LiteralPath $_.FullName }) -join "`n"

foreach ($module in $expectedModules) {
    $escaped = [regex]::Escape("br/public:$module")
    if ($bicep -notmatch "'$escaped'") {
        throw "Pinned AVM reference is missing: $module"
    }
}

$avmBlocks = [regex]::Matches(
    $bicep,
    "module\s+\w+\s+'br/public:avm/[^']+'\s*=\s*\{(?:(?!\n\}).)*\n\}",
    [Text.RegularExpressions.RegexOptions]::Singleline
)
foreach ($block in $avmBlocks) {
    if ($block.Value -notmatch 'enableTelemetry:\s*false') {
        throw 'Every AVM module must explicitly set enableTelemetry: false.'
    }
}

$main = Get-Content -Raw -LiteralPath (Join-Path $root 'infra/main.bicep')
$mainLines = $main -split '\r?\n'
for ($index = 0; $index -lt $mainLines.Count; $index++) {
    if ($mainLines[$index] -match '^output\s+' -and ($index -gt 0 -and $mainLines[$index - 1] -eq '@secure()')) {
        throw 'Root deployment outputs must not be @secure(): ARM never returns a secure output value after the deployment call returns, so azd cannot persist it into .azure/<env>/.env, breaking azd deploy and postdeploy hooks that read it back. None of these outputs are credentials -- real secrets go directly to Key Vault instead.'
    }
}

if ($bicep -match '(?i)listKeys\s*\(') {
    throw 'Key-based deployment expressions are forbidden.'
}

$parameters = Get-Content -Raw -LiteralPath (Join-Path $root 'infra/main.parameters.json')
$parameters | ConvertFrom-Json | Out-Null
if ($parameters -notmatch '\$\{AI_PROJECT_DEPLOYMENTS=\[\]\}') {
    throw 'azure.yaml model deployments must flow through AI_PROJECT_DEPLOYMENTS.'
}

if ($main -notmatch '(?m)^output COSMOS_ENDPOINT string = data\.outputs\.cosmosEndpoint$') {
    throw 'The Cosmos endpoint must flow from the data module into the azd environment.'
}
if ($main -notmatch '(?m)^output COSMOS_ACCOUNT_NAME string = data\.outputs\.accountName$') {
    throw 'The Cosmos account name must flow into the postdeploy environment.'
}
if (
    $main -notmatch '(?m)^output EMBEDDING_MODEL_DEPLOYMENT_NAME string = embeddingModelDeploymentName$'
) {
    throw 'The embedding deployment must flow into the azd environment.'
}

$azureYaml = Get-Content -Raw -LiteralPath (Join-Path $root 'azure.yaml')

# azd resolves an unset environment variable to an empty string, so an azure.yaml
# ${VAR} with no supplier silently ships an empty value to the Agent container and only
# surfaces as session_not_ready at invoke time. Every variable the Agent service reads
# must therefore have a matching root output that populates the azd environment.
$agentVariablePattern = '(?m)^\s*value:\s*\$\{([A-Z0-9_]+)\}\s*$'
$agentVariables = @(
    [regex]::Matches($azureYaml, $agentVariablePattern) |
        ForEach-Object { $_.Groups[1].Value } |
        Sort-Object -Unique
)
# A check that finds nothing to check is indistinguishable from a passing check. The Agent
# service always reads at least one substituted variable, so an empty result means the
# pattern stopped matching azure.yaml rather than that the repository became compliant.
if ($agentVariables.Count -eq 0) {
    throw 'The azure.yaml ${VAR} check matched nothing, so it can no longer detect a missing root output.'
}
foreach ($variable in $agentVariables) {
    if ($main -notmatch "(?m)^output $([regex]::Escape($variable)) string = ") {
        throw "azure.yaml passes `${$variable}` to a service but infra/main.bicep has no output that supplies it."
    }
}

$requiredPostDeployMarkers = @(
    'postdeploy:',
    'windows:',
    'posix:',
    'shell: pwsh',
    'shell: sh'
)
foreach ($marker in $requiredPostDeployMarkers) {
    if (-not $azureYaml.Contains($marker)) {
        throw "Fail-closed Agent role postdeploy marker is missing: $marker"
    }
}
if ([regex]::Matches($azureYaml, 'continueOnError:\s*false').Count -lt 2) {
    throw 'Both Agent role postdeploy hooks must fail closed.'
}
if (
    $azureYaml -notmatch '(?ms)- name: COSMOS_ENDPOINT\s+value: \$\{COSMOS_ENDPOINT\}'
) {
    throw 'The Hosted Agent must receive COSMOS_ENDPOINT from the Bicep output via azd.'
}
if (
    $azureYaml -notmatch '(?ms)- name: EMBEDDING_MODEL_DEPLOYMENT_NAME\s+value: \$\{EMBEDDING_MODEL_DEPLOYMENT_NAME\}'
) {
    throw 'The Hosted Agent must receive the dedicated embedding deployment from azd.'
}
if (
    $azureYaml -notmatch "(?ms)- name: OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT\s+value: 'true'"
) {
    throw 'Hosted Agent content capture must remain explicitly enabled for MVP evaluation.'
}

# The worker no longer receives a deploy-time Agent endpoint. It asks the SDK to build the
# Responses URL from FOUNDRY_PROJECT_ENDPOINT and the agent name contracts.py fixes, which
# is right only while azure.yaml still declares a service under that name. Nothing else
# catches a drift: the worker would keep starting and call an agent that does not exist.
$contracts = Get-Content -Raw -LiteralPath (
    Join-Path $root 'src/functions/knowledge_agent/contracts.py'
)
$agentNameMatch = [regex]::Match($contracts, '(?m)^KNOWLEDGE_AGENT_NAME = "([^"]+)"$')
if (-not $agentNameMatch.Success) {
    throw 'contracts.py no longer defines KNOWLEDGE_AGENT_NAME, so this check cannot run.'
}
$agentName = [regex]::Escape($agentNameMatch.Groups[1].Value)
if ($azureYaml -notmatch "(?m)^  ${agentName}:\s*$") {
    throw "azure.yaml declares no '$($agentNameMatch.Groups[1].Value)' service, but contracts.py points the worker at that agent."
}
if ($azureYaml -notmatch "(?m)^    name: ${agentName}\s*$") {
    throw "The azure.yaml Agent service must keep name: $($agentNameMatch.Groups[1].Value) to match contracts.py."
}

$roleWiring = Get-Content -Raw -LiteralPath (Join-Path $root 'scripts/assign-agent-roles.ps1')
$requiredRoleMarkers = @(
    'azd ai agent show knowledge-agent --output json',
    'instance_identity.principal_id',
    'COSMOS_ACCOUNT_NAME',
    'assign-agent-roles.ps1',
    '/dbs/$DatabaseName/colls/$ContainerName',
    '00000000-0000-0000-0000-000000000001',
    '$azOutput = $null',
    '2>&1',
    '*>&1',
    '>/dev/null 2>&1',
    'Hosted Agent Cosmos role assignment failed.'
)
$combinedRoleWiring = "$azureYaml`n$roleWiring"
foreach ($marker in $requiredRoleMarkers) {
    if (-not $combinedRoleWiring.Contains($marker)) {
        throw "Hosted Agent Cosmos role marker is missing: $marker"
    }
}
if ($azureYaml -notmatch "azure\.ai\.agents:\s*'>=1\.0\.0-beta\.9'") {
    throw 'azure.ai.agents beta.9 or newer is required for identity outputs.'
}

$foundry = Get-Content -Raw -LiteralPath (Join-Path $root 'infra/app/foundry.bicep')
$requiredFoundryMarkers = @(
    'allowProjectManagement: true',
    'AppInsights',
    "authType: 'ProjectManagedIdentity'",
    'ApplicationInsightsConnectionString',
    'foundryUserRoleId',
    'foundryProjectManagerRoleId',
    'monitoringMetricsPublisherRoleId'
)
foreach ($marker in $requiredFoundryMarkers) {
    if (-not $foundry.Contains($marker)) {
        throw "Foundry policy marker is missing: $marker"
    }
}
if ($foundry -match "authType:\s*'ApiKey'" -or $foundry -match 'credentials:\s*\{\s*key:') {
    throw 'Foundry Application Insights must not use local-auth credentials.'
}

$functions = Get-Content -Raw -LiteralPath (Join-Path $root 'infra/app/functions.bicep')
$requiredFunctionTelemetryMarkers = @(
    'APPLICATIONINSIGHTS_AUTHENTICATION_STRING',
    'Authorization=AAD;ClientId=${functionIdentityClientId}',
    'monitoringMetricsPublisherRoleId'
)
foreach ($marker in $requiredFunctionTelemetryMarkers) {
    if (-not $functions.Contains($marker)) {
        throw "Function AAD telemetry marker is missing: $marker"
    }
}
# The Worker builds the Agent's Responses URL from this endpoint, so it is no longer only
# the sync's input. Without it the Queue trigger fails closed at settings validation.
if ($functions -notmatch '(?m)^\s*FOUNDRY_PROJECT_ENDPOINT: foundryProjectEndpoint$') {
    throw 'The Function App must receive FOUNDRY_PROJECT_ENDPOINT; the Worker resolves the Agent from it.'
}

Write-Output 'Infrastructure policy checks passed.'
