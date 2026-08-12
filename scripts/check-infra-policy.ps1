$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$requiredFiles = @(
    'infra/main.bicep',
    'infra/main.parameters.json',
    'infra/app/data.bicep',
    'infra/app/foundry.bicep',
    'infra/app/functions.bicep',
    'infra/app/observability.bicep',
    'infra/app/security.bicep',
    'scripts/assign-agent-roles.ps1',
    'scripts/set_agent_endpoint.py'
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
    if ($mainLines[$index] -match '^output\s+' -and ($index -eq 0 -or $mainLines[$index - 1] -ne '@secure()')) {
        throw 'Every root deployment output must be decorated with @secure().'
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

if ($bicep -match '(?m)^\s*KNOWLEDGE_AGENT_ENDPOINT\s*:') {
    throw 'The deploy-time Agent endpoint must not be synthesized in Bicep.'
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
$requiredPostDeployMarkers = @(
    'postdeploy:',
    'windows:',
    'posix:',
    'shell: pwsh',
    'shell: sh'
)
foreach ($marker in $requiredPostDeployMarkers) {
    if (-not $azureYaml.Contains($marker)) {
        throw "Fail-closed Agent endpoint postdeploy marker is missing: $marker"
    }
}
$endpointHookCommand = 'uv run --project src/functions --no-sync python scripts/set_agent_endpoint.py'
if ([regex]::Matches($azureYaml, [regex]::Escape($endpointHookCommand)).Count -ne 2) {
    throw 'Windows and POSIX postdeploy hooks must use the no-sync Functions Python environment.'
}
if ([regex]::Matches($azureYaml, 'continueOnError:\s*false').Count -lt 2) {
    throw 'Both Agent endpoint postdeploy hooks must fail closed.'
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

$endpointWiring = Get-Content -Raw -LiteralPath (Join-Path $root 'scripts/set_agent_endpoint.py')
$requiredEndpointMarkers = @(
    'AGENT_KNOWLEDGE_AGENT_RESPONSES_ENDPOINT',
    'KNOWLEDGE_AGENT_ENDPOINT',
    'SERVICE_FUNCTIONS_RESOURCE_NAME',
    'AZURE_RESOURCE_GROUP',
    'AZURE_SUBSCRIPTION_ID',
    '"--subscription",',
    '"--output",',
    '"none",'
)
foreach ($marker in $requiredEndpointMarkers) {
    if (-not $endpointWiring.Contains($marker)) {
        throw "Agent endpoint wiring marker is missing: $marker"
    }
}

$roleWiring = Get-Content -Raw -LiteralPath (Join-Path $root 'scripts/assign-agent-roles.ps1')
$requiredRoleMarkers = @(
    'AGENT_KNOWLEDGE_AGENT_INSTANCE_IDENTITY_PRINCIPAL_ID',
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

Write-Output 'Infrastructure policy checks passed.'
