[CmdletBinding()]
param(
    [string]$ApiBase = 'http://127.0.0.1:8768',
    [string]$OutputRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$AgentDemoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($OutputRoot)) { $OutputRoot = Join-Path $AgentDemoRoot 'evidence\judge-demo' }
$Judge = Invoke-RestMethod -Uri "$ApiBase/api/v1/judge-run" -TimeoutSec 15
if ([string]::IsNullOrWhiteSpace([string]$Judge.run_id)) { throw 'No eligible Judge Run is available.' }
$RunId = [string]$Judge.run_id
$Destination = Join-Path $OutputRoot $RunId
New-Item -ItemType Directory -Path $Destination -Force | Out-Null

$Workspace = Invoke-RestMethod -Uri "$ApiBase/api/v1/workspace" -TimeoutSec 30
$Run = Invoke-RestMethod -Uri "$ApiBase/api/v1/runs/$RunId" -TimeoutSec 30
$Observability = Invoke-RestMethod -Uri "$ApiBase/api/v1/runs/$RunId/observability" -TimeoutSec 30
$Workspace | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $Destination 'workspace.json') -Encoding utf8
$Run | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $Destination 'run.json') -Encoding utf8
$Observability | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $Destination 'observability.json') -Encoding utf8
Invoke-WebRequest -Uri "$ApiBase/api/v1/runs/$RunId/audit-bundle.zip" -OutFile (Join-Path $Destination "$RunId-audit.zip") -TimeoutSec 60

$Preview = $Run.report_preview
if ($null -ne $Preview -and $null -ne $Preview.download_urls) {
    foreach ($Kind in @('pdf', 'markdown', 'json')) {
        $Url = [string]$Preview.download_urls.$Kind
        if (-not [string]::IsNullOrWhiteSpace($Url)) {
            $Extension = if ($Kind -eq 'markdown') { 'md' } else { $Kind }
            Invoke-WebRequest -Uri ($ApiBase.TrimEnd('/') + $Url) -OutFile (Join-Path $Destination "preview.$Extension") -TimeoutSec 30
        }
    }
}
$Files = @(Get-ChildItem -LiteralPath $Destination -File | ForEach-Object {
    [ordered]@{ name = $_.Name; bytes = $_.Length; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant() }
})
$Token = $Observability.provider_usage
$Manifest = [ordered]@{
    protocol = 'FINFLUX_JUDGE_DEMO_EVIDENCE_V1.0'
    exported_at_utc = [DateTime]::UtcNow.ToString('o')
    judge_run = $Judge
    human_state = [string]$Run.human_gate.state
    datapass_present = ($null -ne $Run.datapass)
    provider_usage = $Token
    truth_boundary = 'Exported from existing persisted endpoints; no Run was started and no Human decision was created.'
    files = $Files
}
$Manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $Destination 'manifest.json') -Encoding utf8
$Summary = @(
    '# FinFlux 裁判 Demo 证据包', '', "- Judge Run：``$RunId``", "- Human状态：``$($Run.human_gate.state)``",
    "- DataPass存在：``$($null -ne $Run.datapass)``", "- Provider Token：``$($Token.total_tokens)``", "- Provider Calls：``$($Token.call_count)``", '',
    '> 导出操作不会启动新Run、不会调用模型、不会替代Human签署。'
)
$Summary | Set-Content -LiteralPath (Join-Path $Destination 'README.md') -Encoding utf8
$Manifest | ConvertTo-Json -Depth 12

