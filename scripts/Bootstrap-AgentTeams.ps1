[CmdletBinding()]
param(
    [string]$Destination = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ExpectedCommit = '849182af8e017168a5a200a87b1062142caf462d'
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $ProjectRoot '.cache\AgentTeams-v1.2.2'
}
$Destination = [IO.Path]::GetFullPath($Destination)

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is required to fetch the official AgentTeams source.'
}
if (Test-Path -LiteralPath $Destination) {
    $HeadPath = Join-Path $Destination '.git\HEAD'
    $Actual = if (Test-Path -LiteralPath $HeadPath) {
        (Get-Content -LiteralPath $HeadPath -Raw).Trim()
    } else {
        ''
    }
    if ($Actual -ne $ExpectedCommit) {
        throw "Existing AgentTeams checkout is not the pinned commit: $Destination"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Destination 'install\agentteams-install.ps1'))) {
        throw 'Existing pinned checkout does not contain the official Windows installer.'
    }
    Write-Output "AgentTeams v1.2.2 already verified: $Destination"
    exit 0
}

$Parent = Split-Path -Parent $Destination
New-Item -ItemType Directory -Path $Parent -Force | Out-Null
& git clone --filter=blob:none --no-checkout https://github.com/agentscope-ai/AgentTeams.git $Destination
if ($LASTEXITCODE -ne 0) { throw 'AgentTeams clone failed.' }
& git -C $Destination checkout --detach $ExpectedCommit
if ($LASTEXITCODE -ne 0) { throw 'Pinned AgentTeams commit checkout failed.' }
$Actual = (& git -C $Destination rev-parse HEAD).Trim()
if ($Actual -ne $ExpectedCommit) { throw "AgentTeams commit mismatch: $Actual" }
if (-not (Test-Path -LiteralPath (Join-Path $Destination 'install\agentteams-install.ps1'))) {
    throw 'Pinned checkout does not contain the official Windows installer.'
}
Write-Output "AgentTeams v1.2.2 verified at $Destination"
