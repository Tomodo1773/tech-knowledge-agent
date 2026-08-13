param(
    [switch]$Staged
)

$ErrorActionPreference = 'Stop'
# An unassigned variable would otherwise resolve to $null and turn the check that reads it
# into a silent no-op instead of an error. See the same guard in check-infra-policy.ps1.
Set-StrictMode -Version Latest

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

# Skills under .claude/skills/ are copied verbatim from upstream at a pinned commit
# (see .claude/skills/VENDOR.md). Their documentation uses placeholder hosts such as
# my-account.services.ai.azure.com, which are not this project's endpoints, and editing
# them would break diffing against upstream. Only SkipVendored rules are relaxed there;
# every secret-shaped rule still applies to vendored files.
$vendoredPathPattern = '^\.claude/skills/'

$sensitivePatterns = @(
    @{ Name = 'GitHub token'; SkipVendored = $false; Pattern = 'gh[pousr]_[A-Za-z0-9]{20,}' },
    @{ Name = 'GitHub fine-grained token'; SkipVendored = $false; Pattern = 'github_pat_[A-Za-z0-9_]{20,}' },
    @{ Name = 'OpenAI-style key'; SkipVendored = $false; Pattern = 'sk-[A-Za-z0-9_-]{20,}' },
    @{ Name = 'Azure resource ID'; SkipVendored = $false; Pattern = '/subscriptions/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' },
    @{ Name = 'Azure identifier value'; SkipVendored = $false; Pattern = '(?i)(AZURE_(SUBSCRIPTION|TENANT|CLIENT)_ID|subscriptionId|tenantId|clientId|objectId)\s*[:=]\s*["'']?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' },
    @{ Name = 'Deployed Azure endpoint'; SkipVendored = $true; Pattern = 'https://[a-z0-9][a-z0-9-]*\.(azurewebsites\.net|services\.ai\.azure\.com|documents\.azure\.com|vault\.azure\.net|blob\.core\.windows\.net)' }
)

$deployedEndpointPattern = ($sensitivePatterns | Where-Object Name -eq 'Deployed Azure endpoint').Pattern
$rejectedProbe = 'https://' + 'real-resource.services.ai.azure.com/api/projects/project'
if ($rejectedProbe -notmatch $deployedEndpointPattern) {
    throw 'The repository policy must continue detecting deployed Azure endpoints.'
}

# The exemption above has to stay narrow. Without these probes a typo in the path pattern
# would either silently stop exempting vendored files or silently exempt the whole repo,
# and both failures look like a passing check.
if ('.claude/skills/microsoft-foundry/SKILL.md' -notmatch $vendoredPathPattern) {
    throw 'The vendored-skill exemption must keep matching files under .claude/skills/.'
}
foreach ($ownPath in @('docs/telemetry.md', 'src/agent/main.py', 'infra/main.bicep', 'azure.yaml')) {
    if ($ownPath -match $vendoredPathPattern) {
        throw "The vendored-skill exemption must not cover this project's own file: $ownPath"
    }
}

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

    $isVendored = $normalized -match $vendoredPathPattern
    foreach ($rule in $sensitivePatterns) {
        if ($rule.SkipVendored -and $isVendored) {
            continue
        }
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
