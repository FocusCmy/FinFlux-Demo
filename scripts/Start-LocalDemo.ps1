[CmdletBinding()]
param(
    [int]$Port = 8768,
    [string]$HostAddress = '127.0.0.1',
    [string]$RuntimeEnvFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$AppPath = Join-Path $ProjectRoot 'app\app.py'
$EvidencePath = Join-Path $ProjectRoot 'app\data\real_50x3_v1\manifest.json'

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python was not found. Install Python 3.10+ and make sure python is on PATH.'
}
if (-not (Test-Path -LiteralPath $AppPath)) {
    throw "FinFlux application entrypoint not found: $AppPath"
}
if (-not (Test-Path -LiteralPath $EvidencePath)) {
    throw "Public source-bound manifest not found: $EvidencePath"
}
if (-not [string]::IsNullOrWhiteSpace($RuntimeEnvFile)) {
    if (-not (Test-Path -LiteralPath $RuntimeEnvFile)) {
        throw "External runtime config not found: $RuntimeEnvFile"
    }
    $env:FINFLUX_RUNTIME_ENV_FILE = (Resolve-Path -LiteralPath $RuntimeEnvFile).Path
    Write-Host 'Using external gitignored runtime config; credentials are not copied into the submission package.'
}

Write-Host "FinFlux: http://${HostAddress}:$Port/"
Write-Host 'Press Ctrl+C to stop.'
& python $AppPath --host $HostAddress --port $Port
exit $LASTEXITCODE
