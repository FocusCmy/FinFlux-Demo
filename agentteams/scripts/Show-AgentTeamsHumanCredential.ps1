[CmdletBinding()]
param(
    [switch]$WriteEnv,
    [string]$EnvFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Controller = 'agentteams-controller'
$AgentDemoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $AgentDemoRoot '.env'
}

function Set-DotEnvValue([string]$Path, [string]$Name, [string]$Value) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Runtime config does not exist: $Path"
    }
    $Lines = [System.Collections.Generic.List[string]]::new()
    foreach ($Line in [IO.File]::ReadAllLines($Path)) {
        [void]$Lines.Add($Line)
    }
    $Updated = $false
    for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
        if ($Lines[$Index] -match ('^\s*' + [Regex]::Escape($Name) + '=')) {
            $Lines[$Index] = "$Name=$Value"
            $Updated = $true
        }
    }
    if (-not $Updated) {
        [void]$Lines.Add("$Name=$Value")
    }
    $Utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllLines($Path, $Lines, $Utf8NoBom)
}

function Write-CredentialState([string]$Path, [string]$MatrixUser) {
    # The receipt proves that a credential was synchronized without retaining
    # the password, token, or a reversible derivative of either value.
    $ReceiptPath = Join-Path (Split-Path -Parent $Path) 'human-credential-state.json'
    [ordered]@{
        protocol = 'FINFLUX_HUMAN_CREDENTIAL_STATE_V1.0'
        matrix_user = $MatrixUser
        secret_storage = 'EXTERNAL_GITIGNORED_ENV'
        password_exported_to_console = $false
        synchronized_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
}

$Running = @(docker --context desktop-linux ps --filter "name=^/$Controller$" --format '{{.Names}}')
if ($LASTEXITCODE -ne 0 -or $Running -notcontains $Controller) {
    throw 'agentteams-controller is not running. Deploy AgentTeams v1.2.2 first.'
}

$Raw = docker --context desktop-linux exec $Controller agt get humans finchange-data-owner -o json
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($Raw -join "`n"))) {
    throw 'Could not read Human resource finchange-data-owner.'
}
$Human = ($Raw -join "`n") | ConvertFrom-Json
$Password = [string]$Human.initialPassword
$MatrixUser = [string]$Human.matrixUserID

if ([string]::IsNullOrWhiteSpace($MatrixUser) -or [string]::IsNullOrWhiteSpace($Password)) {
    Write-Host 'No one-time password is present in Human status. It may already have been consumed or not generated yet.'
    Write-Host 'Inspect the Human phase and controller logs; do not substitute the admin password.'
    exit 1
}

if (-not $WriteEnv) {
    Write-Host 'Human credentials exist in AgentTeams, but are not displayed.'
    Write-Host 'Run this script with -WriteEnv to synchronize them into the gitignored agentteams/.env runtime file.'
    exit 0
}

Set-DotEnvValue $EnvFile 'FINCHANGE_MATRIX_HUMAN_USER' $MatrixUser
Set-DotEnvValue $EnvFile 'FINCHANGE_MATRIX_HUMAN_PASSWORD' $Password
Write-CredentialState $EnvFile $MatrixUser
Write-Host 'Human Matrix credentials synchronized into the gitignored runtime config without displaying either value.'
