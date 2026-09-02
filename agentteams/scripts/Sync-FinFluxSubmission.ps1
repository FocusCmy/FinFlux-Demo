[CmdletBinding()]
param(
    [string]$Destination = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentTeamsSource = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$SourceRoot = (Resolve-Path (Join-Path $AgentTeamsSource '..')).Path
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $SourceRoot 'FinFlux-Demo'
}
$Destination = (Resolve-Path -LiteralPath $Destination).Path
$ExpectedDestination = (Resolve-Path -LiteralPath (Join-Path $SourceRoot 'FinFlux-Demo')).Path
if (-not $Destination.Equals($ExpectedDestination, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to rewrite an unexpected destination: $Destination"
}

function Assert-ChildPath([string]$Path) {
    $Full = [IO.Path]::GetFullPath($Path)
    $Prefix = $Destination.TrimEnd('\') + '\'
    if (-not $Full.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the submission directory: $Full"
    }
}

function Reset-CopyTree([string]$Source, [string]$Target) {
    Assert-ChildPath $Target
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force |
        Copy-Item -Destination $Target -Recurse -Force
}

$AppTarget = Join-Path $Destination 'app'
$AgentTeamsTarget = Join-Path $Destination 'agentteams'
Reset-CopyTree (Join-Path $SourceRoot 'demo') $AppTarget
Reset-CopyTree $AgentTeamsSource $AgentTeamsTarget

# Submission never carries local secrets, mutable installer state or bytecode.
$Excluded = @(
    (Join-Path $AgentTeamsTarget '.env'),
    (Join-Path $AgentTeamsTarget 'runtime-workspace'),
    (Join-Path $AgentTeamsTarget 'build\rendered')
)
foreach ($Path in $Excluded) {
    Assert-ChildPath $Path
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}
Get-ChildItem -LiteralPath $Destination -Recurse -Directory -Filter '__pycache__' |
    ForEach-Object {
        Assert-ChildPath $_.FullName
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
Get-ChildItem -LiteralPath $Destination -Recurse -File -Filter '*.pyc' |
    ForEach-Object {
        Assert-ChildPath $_.FullName
        Remove-Item -LiteralPath $_.FullName -Force
    }

# Remove the former two-demo layout only after both new module roots exist.
if (-not (Test-Path -LiteralPath (Join-Path $AppTarget 'app.py')) -or
    -not (Test-Path -LiteralPath (Join-Path $AgentTeamsTarget 'config\agent_demo.json'))) {
    throw 'Unified application copy is incomplete; the former layout was preserved.'
}
foreach ($LegacyName in @('demo', 'agent_demo')) {
    $LegacyPath = Join-Path $Destination $LegacyName
    Assert-ChildPath $LegacyPath
    if (Test-Path -LiteralPath $LegacyPath) {
        Remove-Item -LiteralPath $LegacyPath -Recurse -Force
    }
}

$Summary = [ordered]@{
    protocol = 'FINFLUX_SINGLE_PRODUCT_LAYOUT_V1.0'
    destination = $Destination
    application_root = 'app'
    agentteams_runtime_root = 'agentteams'
    legacy_demo_directories_present = [bool](
        (Test-Path -LiteralPath (Join-Path $Destination 'demo')) -or
        (Test-Path -LiteralPath (Join-Path $Destination 'agent_demo'))
    )
    agent_package_count = @(Get-ChildItem -LiteralPath (Join-Path $AgentTeamsTarget 'packages') -Directory).Count
    skill_count = @(Get-ChildItem -LiteralPath (Join-Path $AppTarget 'agentteams-skills') -Directory).Count
}
$Summary | ConvertTo-Json -Depth 4
