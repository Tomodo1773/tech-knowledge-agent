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

    [string] $DatabaseName = 'knowledge',

    [string] $ContainerName = 'chunks'
)

$ErrorActionPreference = 'Stop'
$readerRoleId = '00000000-0000-0000-0000-000000000001'
$accountId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.DocumentDB/databaseAccounts/$CosmosAccountName"
$scope = "$accountId/dbs/$DatabaseName/colls/$ContainerName"
$assignmentId = [guid](New-Object -TypeName System.Security.Cryptography.MD5CryptoServiceProvider).ComputeHash(
    [Text.Encoding]::UTF8.GetBytes("$scope|$AgentPrincipalId|$readerRoleId")
)

if ($PSCmdlet.ShouldProcess('Hosted Agent identity', 'Assign Cosmos DB Built-in Data Reader')) {
    $azOutput = & az cosmosdb sql role assignment create `
        --account-name $CosmosAccountName `
        --resource-group $ResourceGroupName `
        --subscription $SubscriptionId `
        --role-assignment-id $assignmentId `
        --role-definition-id $readerRoleId `
        --principal-id $AgentPrincipalId `
        --scope $scope `
        --output none 2>&1
    $azExitCode = $LASTEXITCODE
    $azOutput = $null

    if ($azExitCode -ne 0) {
        throw 'Cosmos data-plane reader assignment failed.'
    }

    Write-Output 'Cosmos data-plane reader assignment is present.'
}
