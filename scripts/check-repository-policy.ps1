param(
    [switch]$Staged
)

$ErrorActionPreference = 'Stop'

function Get-RepositoryText {
    param([string]$Path)

    if ($Staged) {
        $text = git show ":$Path" 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return ($text -join "`n") + "`n"
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }

    return Get-Content -Raw -LiteralPath $Path
}

$agents = Get-RepositoryText 'AGENTS.md'
$claude = Get-RepositoryText 'CLAUDE.md'
if ($null -eq $agents -or $null -eq $claude -or $agents -cne $claude) {
    throw 'AGENTS.md and CLAUDE.md must exist and have identical content.'
}

$files = if ($Staged) {
    @(git diff --cached --name-only --diff-filter=ACMR)
} else {
    @(git ls-files --cached --others --exclude-standard)
}

$forbiddenPaths = @(
    '(^|/)\.azure(/|$)',
    '(^|/)\.env($|\.)',
    '\.local\.bicepparam$',
    '\.local\.parameters\.json$',
    '(^|/)(deployment-output|what-if-output|azure-debug)'
)

$sensitivePatterns = @(
    @{ Name = 'GitHub token'; Pattern = 'gh[pousr]_[A-Za-z0-9]{20,}' },
    @{ Name = 'GitHub fine-grained token'; Pattern = 'github_pat_[A-Za-z0-9_]{20,}' },
    @{ Name = 'OpenAI-style key'; Pattern = 'sk-[A-Za-z0-9_-]{20,}' },
    @{ Name = 'Azure resource ID'; Pattern = '/subscriptions/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' },
    @{ Name = 'Azure identifier value'; Pattern = '(?i)(AZURE_(SUBSCRIPTION|TENANT|CLIENT)_ID|subscriptionId|tenantId|clientId|objectId)\s*[:=]\s*["'']?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' },
    @{ Name = 'Deployed Azure endpoint'; Pattern = 'https://[a-z0-9][a-z0-9-]*\.(azurewebsites\.net|services\.ai\.azure\.com|documents\.azure\.com|vault\.azure\.net|blob\.core\.windows\.net)' }
)

$violations = [System.Collections.Generic.List[string]]::new()
foreach ($file in $files) {
    $normalized = $file.Replace('\', '/')
    $isAllowedEnvironmentExample = $normalized -match '(^|/)\.env\.(example|sample)$'
    foreach ($pattern in $forbiddenPaths) {
        if (-not $isAllowedEnvironmentExample -and $normalized -match $pattern) {
            $violations.Add("Forbidden tracked path: $file")
            break
        }
    }

    $text = Get-RepositoryText $file
    if ($null -eq $text -or $text.IndexOf([char]0) -ge 0) {
        continue
    }

    foreach ($rule in $sensitivePatterns) {
        if ($text -match $rule.Pattern) {
            $violations.Add("$($rule.Name): $file")
        }
    }
}

if ($violations.Count -gt 0) {
    $violations | Sort-Object -Unique | ForEach-Object { Write-Error $_ }
    throw 'Repository policy check failed.'
}

Write-Output 'Repository policy check passed.'
