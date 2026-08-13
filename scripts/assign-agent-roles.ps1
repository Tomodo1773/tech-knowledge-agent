[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')]
    [string] $SubscriptionId,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $ResourceGroupName,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $CosmosAccountName,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')]
    [string] $AgentPrincipalId,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $FoundryAccountName,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Insights/components/[^/]+$')]
    [string] $ApplicationInsightsResourceId,

    [string] $DatabaseName = 'knowledge',

    [string] $ContainerName = 'chunks'
)

$ErrorActionPreference = 'Stop'

# Every assignment below needs the same shape: run an az command whose stdout/stderr must
# never reach the log (principal, scope, and resource names are not for public CI output),
# check its exit code, and throw a fixed message that reveals none of the values involved.
# $azOutput is discarded rather than left assigned so a caller inspecting variables after a
# failure cannot recover what the command printed.
function Invoke-RoleAssignment {
    param(
        [Parameter(Mandatory)] [string[]] $ArgumentList,
        [Parameter(Mandatory)] [string] $SuccessMessage,
        [Parameter(Mandatory)] [string] $FailureMessage
    )
    $azOutput = & az @ArgumentList --output none 2>&1
    $azExitCode = $LASTEXITCODE
    $azOutput = $null

    if ($azExitCode -ne 0) {
        throw $FailureMessage
    }

    Write-Output $SuccessMessage
}

$readerRoleId = '00000000-0000-0000-0000-000000000001'
$accountId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.DocumentDB/databaseAccounts/$CosmosAccountName"
$scope = "$accountId/dbs/$DatabaseName/colls/$ContainerName"
$assignmentId = [guid](New-Object -TypeName System.Security.Cryptography.MD5CryptoServiceProvider).ComputeHash(
    [Text.Encoding]::UTF8.GetBytes("$scope|$AgentPrincipalId|$readerRoleId")
)

if ($PSCmdlet.ShouldProcess('Hosted Agent identity', 'Assign Cosmos DB Built-in Data Reader')) {
    Invoke-RoleAssignment `
        -ArgumentList @(
            'cosmosdb', 'sql', 'role', 'assignment', 'create',
            '--account-name', $CosmosAccountName,
            '--resource-group', $ResourceGroupName,
            '--subscription', $SubscriptionId,
            '--role-assignment-id', $assignmentId,
            '--role-definition-id', $readerRoleId,
            '--principal-id', $AgentPrincipalId,
            '--scope', $scope
        ) `
        -SuccessMessage 'Cosmos data-plane reader assignment is present.' `
        -FailureMessage 'Cosmos data-plane reader assignment failed.'
}

# knowledge_search embeds the query before it can search, and the documented OpenAI v1
# inference endpoint is the account root. The Agent's implicit model access covers the
# project it runs in, and RBAC does not inherit from a child project up to its account, so
# the inference role has to be granted here. The Agent identity only exists after deploy,
# which is why this is not in Bicep. Narrowest role that allows embeddings. Evidence and
# the route decision are in docs/architecture.md#embeddingがaccount-scopeを要求する理由.
$inferenceRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
$foundryAccountId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.CognitiveServices/accounts/$FoundryAccountName"

if ($PSCmdlet.ShouldProcess('Hosted Agent identity', 'Assign Cognitive Services OpenAI User')) {
    Invoke-RoleAssignment `
        -ArgumentList @(
            'role', 'assignment', 'create',
            '--assignee-object-id', $AgentPrincipalId,
            '--assignee-principal-type', 'ServicePrincipal',
            '--role', $inferenceRoleId,
            '--scope', $foundryAccountId
        ) `
        -SuccessMessage 'Foundry inference role assignment is present.' `
        -FailureMessage 'Foundry inference role assignment failed.'
}

# Application Insights has local auth disabled, so the Agent's own spans are rejected with
# Forbidden until its identity can publish. Bicep grants this to the Function UAI and the
# project MI, but the Agent identity does not exist until deploy. Without it the Agent side
# of a Slack question is missing from the trace.
$metricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

if ($PSCmdlet.ShouldProcess('Hosted Agent identity', 'Assign Monitoring Metrics Publisher')) {
    Invoke-RoleAssignment `
        -ArgumentList @(
            'role', 'assignment', 'create',
            '--assignee-object-id', $AgentPrincipalId,
            '--assignee-principal-type', 'ServicePrincipal',
            '--role', $metricsPublisherRoleId,
            '--scope', $ApplicationInsightsResourceId
        ) `
        -SuccessMessage 'Application Insights publisher role assignment is present.' `
        -FailureMessage 'Application Insights publisher role assignment failed.'
}
