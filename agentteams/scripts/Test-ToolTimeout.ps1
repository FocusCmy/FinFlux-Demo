[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentDemoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EvidenceRoot = Join-Path $AgentDemoRoot 'evidence\fault-injection'
$Container = 'agentteams-worker-downstream-impact-analyst'
$PackageRoot = '/root/agentteams-fs/agents/downstream-impact-analyst/.qwenpaw/agent-packages/current'
$TaskRoot = '/root/agentteams-fs/teams/finchange-cross-asset-review/shared/tasks'
$Stamp = [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')
$CaseId = "FI-TOOL-TIMEOUT-$Stamp"
$RunId = "RUN-FI-$Stamp"
$TaskId = "task-$CaseId-$RunId-downstream-impact-analyst"

$Payload = [ordered]@{
    change_bundle_id = "CB-FI-$Stamp"
    change_set = [ordered]@{
        change_id = "CHG-FI-$Stamp"
        change_set_sha256 = ('a' * 64)
        changed_paths = @('metadata.candidate_mapping')
    }
    downstream_tasks = @(
        [ordered]@{
            task_id = 'daily-settlement'
            dependencies = @('metadata.candidate_mapping')
        }
    )
}
$PayloadJson = $Payload | ConvertTo-Json -Depth 8 -Compress
$PayloadBytes = [Text.Encoding]::UTF8.GetBytes($PayloadJson)
$PayloadB64 = [Convert]::ToBase64String($PayloadBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

$Running = docker inspect -f '{{.State.Running}}' $Container 2>$null
if ($LASTEXITCODE -ne 0 -or $Running.Trim() -ne 'true') {
    throw "Required Worker container is not running: $Container"
}

docker exec `
    -e FINFLUX_FAULT_INJECTION=TOOL_TIMEOUT `
    -e FINFLUX_FAULT_DELAY_SECONDS=3 `
    $Container `
    python "$PackageRoot/tool_gateway.py" `
    --entry bounded-change `
    --timeout-s 1 `
    -- `
    --case-id $CaseId `
    --run-id $RunId `
    --task-id $TaskId `
    --policy-id FINFLUX-BOUNDED-EXECUTION-V0.1 `
    --change-payload-b64 $PayloadB64
$GatewayExit = $LASTEXITCODE
if ($GatewayExit -ne 124) {
    throw "Expected Tool Gateway exit 124, got $GatewayExit"
}

New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
$ReceiptPath = Join-Path $EvidenceRoot 'latest-timeout-receipt.json'
docker cp "${Container}:$TaskRoot/$TaskId/tool_execution_receipt.json" $ReceiptPath
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to copy timeout receipt from the Worker container'
}

$Receipt = Get-Content -Raw -LiteralPath $ReceiptPath | ConvertFrom-Json
if ($Receipt.status -ne 'TIMED_OUT') { throw 'Receipt status is not TIMED_OUT' }
if (-not $Receipt.timed_out) { throw 'Receipt timed_out is false' }
if ([int]$Receipt.retry_count -ne 0) { throw 'Receipt unexpectedly retried the tool' }
if ([int]$Receipt.provider_tokens -ne 0) { throw 'Fault injection consumed model tokens' }

$Summary = [ordered]@{
    protocol = 'FINFLUX_CONTAINER_FAULT_INJECTION_REPORT_V1.0'
    tested_at_utc = [DateTime]::UtcNow.ToString('o')
    container = $Container
    case_id = $CaseId
    run_id = $RunId
    task_id = $TaskId
    injected_fault = 'TOOL_TIMEOUT'
    gateway_exit_code = $GatewayExit
    receipt_status = $Receipt.status
    retry_count = $Receipt.retry_count
    provider_tokens = $Receipt.provider_tokens
    receipt_sha256 = $Receipt.receipt_sha256
    receipt_file_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ReceiptPath).Hash.ToLowerInvariant()
    production_financial_data_mutated = $false
    model_or_api_called = $false
}
$SummaryPath = Join-Path $EvidenceRoot 'latest-timeout-summary.json'
$Summary | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -LiteralPath $SummaryPath
$Summary | ConvertTo-Json -Depth 6
