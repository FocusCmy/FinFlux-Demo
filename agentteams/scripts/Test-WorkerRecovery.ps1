param(
    [switch]$Execute,
    [string]$BaseUrl = "http://127.0.0.1:8768",
    [string]$ContainerName = "agentteams-worker-downstream-impact-analyst",
    [int]$RecoveryTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$scriptRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$demoRoot = (Resolve-Path -LiteralPath (Join-Path $scriptRoot "..")).Path
$evidenceRoot = Join-Path $demoRoot "evidence\fault-injection"
$evidencePath = Join-Path $evidenceRoot "latest-worker-recovery-summary.json"
$evidenceStamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
$immutableEvidenceRoot = Join-Path $evidenceRoot ("worker-recovery-" + $evidenceStamp)

function Get-FinFluxJson([string]$Path) {
    return Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + $Path) -TimeoutSec 10
}

function Write-ImmutableJson([string]$Path, [object]$Value) {
    if (Test-Path -LiteralPath $Path) {
        throw "拒绝覆盖不可变证据文件: $Path"
    }
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $json = $Value | ConvertTo-Json -Depth 30
    [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Capture-TokenGuard([string]$Stage) {
    $path = Join-Path $immutableEvidenceRoot ("$Stage-token-guard.json")
    try {
        $snapshot = Get-FinFluxJson "/api/v1/token-guard"
    }
    catch {
        return [pscustomobject]@{
            status = "NOT_CAPTURED"
            stage = $Stage
            file = $null
            sha256 = $null
            snapshot = $null
            error = $_.Exception.Message
        }
    }
    # Persist the endpoint response even when it reports usage unavailable, so
    # the failure itself remains auditable instead of becoming an oral claim.
    $sha256 = Write-ImmutableJson $path $snapshot
    if (
        -not [bool]$snapshot.provider_usage_captured -or
        $null -eq $snapshot.daily -or
        $null -eq $snapshot.daily.total_tokens -or
        $null -eq $snapshot.daily.call_count
    ) {
        return [pscustomobject]@{
            status = "NOT_CAPTURED"
            stage = $Stage
            file = $path
            sha256 = $sha256
            snapshot = $snapshot
            error = "Token Guard未提供可核对的供应商usage、daily.total_tokens或daily.call_count"
        }
    }
    return [pscustomobject]@{
        status = "CAPTURED"
        stage = $Stage
        file = $path
        sha256 = $sha256
        snapshot = $snapshot
        error = $null
    }
}

function Write-TokenEvidenceFailure([string]$Reason, [object]$Before, [object]$After = $null) {
    New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
    $failure = [ordered]@{
        protocol = "FINFLUX_WORKER_RECOVERY_TEST_V1.1"
        tested_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        target_container = $ContainerName
        safety_gate = "NO_ACTIVE_AGENTTEAMS_RUN"
        result = "NOT_EXECUTED_OR_NOT_VERIFIED"
        provider_token_evidence = [ordered]@{
            status = "NOT_CAPTURED"
            reason = $Reason
            before = $Before
            after = $After
            provider_tokens_delta = $null
            provider_call_delta = $null
            zero_token_claim = "FORBIDDEN"
        }
        truth_boundary = "Token证据不可用时失败关闭；不得把缺失usage写成零Token，也不得宣称零模型恢复。"
    }
    [IO.File]::WriteAllText(
        $evidencePath,
        ($failure | ConvertTo-Json -Depth 30),
        [Text.UTF8Encoding]::new($false)
    )
}

function Get-ContainerRunning([string]$Name) {
    $value = docker inspect --format "{{.State.Running}}" $Name 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "未找到容器 $Name；请先启动 AgentTeams v1.2.2 Runtime。"
    }
    return ($value.Trim().ToLowerInvariant() -eq "true")
}

$plan = [ordered]@{
    protocol = "FINFLUX_WORKER_RECOVERY_TEST_V1.1"
    execute = [bool]$Execute
    target_container = $ContainerName
    safety_gate = "NO_ACTIVE_AGENTTEAMS_RUN"
    provider_token_evidence = "REQUIRED_AT_EXECUTION"
    provider_tokens = "NOT_CAPTURED_DRY_RUN"
    model_calls = "NOT_CAPTURED_DRY_RUN"
    purpose = "验证单 Worker 进程中断后，Runtime 拓扑可观测、容器可恢复且不重放模型调用"
}
if (-not $Execute) {
    $plan | ConvertTo-Json -Depth 8
    exit 0
}

$active = Get-FinFluxJson "/api/agent/active-run"
if ($active.active) {
    $runId = [string]$active.run.run_id
    $state = [string]$active.run.state
    throw "拒绝故障注入：当前仍有活动 Run $runId ($state)。请先由真实 Human Gate 完成处置。"
}

# Token evidence is captured before the first mutating Docker command.  If the
# backend cannot expose supplier-reported usage, fail closed and do not stop a
# Worker merely to produce an unverifiable zero-token claim.
$tokenBefore = Capture-TokenGuard "before"
if ($tokenBefore.status -ne "CAPTURED") {
    Write-TokenEvidenceFailure "BEFORE_TOKEN_GUARD_NOT_CAPTURED" $tokenBefore
    throw "拒绝故障注入：无法捕获前置Token Guard快照；已记录NOT_CAPTURED。"
}

$initiallyRunning = Get-ContainerRunning $ContainerName
if (-not $initiallyRunning) {
    throw "目标容器原本未运行，无法证明本次中断由测试触发。"
}

$startedAt = [DateTimeOffset]::UtcNow
$before = Get-FinFluxJson "/api/agent/status"
$during = $null
$after = $null
$restarted = $false
try {
    docker stop --time 10 $ContainerName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker stop 失败" }
    $during = Get-FinFluxJson "/api/agent/status"
}
finally {
    if (-not (Get-ContainerRunning $ContainerName)) {
        docker start $ContainerName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "docker start 失败" }
        $restarted = $true
    }
}

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($RecoveryTimeoutSeconds)
do {
    Start-Sleep -Seconds 2
    $after = Get-FinFluxJson "/api/agent/status"
    $ready = [bool]$after.connected -and
        ([int]$after.resources.ready_workers -eq [int]$after.resources.total_workers)
} while (-not $ready -and [DateTimeOffset]::UtcNow -lt $deadline)

if (-not $ready) {
    throw "Worker 容器已重启，但 Runtime 未在 $RecoveryTimeoutSeconds 秒内恢复全员 Ready。"
}

$tokenAfter = Capture-TokenGuard "after"
if ($tokenAfter.status -ne "CAPTURED") {
    Write-TokenEvidenceFailure "AFTER_TOKEN_GUARD_NOT_CAPTURED" $tokenBefore $tokenAfter
    throw "恢复完成但验收失败：无法捕获后置Token Guard快照；不得宣称零Token。"
}

$beforeDaily = $tokenBefore.snapshot.daily
$afterDaily = $tokenAfter.snapshot.daily
if ([string]$tokenBefore.snapshot.source -ne [string]$tokenAfter.snapshot.source) {
    Write-TokenEvidenceFailure "PROVIDER_USAGE_SOURCE_CHANGED" $tokenBefore $tokenAfter
    throw "前后Token Guard供应商usage来源不同，差值不可比较。"
}
if ([string]$beforeDaily.date_utc -ne [string]$afterDaily.date_utc) {
    Write-TokenEvidenceFailure "UTC_USAGE_WINDOW_CHANGED" $tokenBefore $tokenAfter
    throw "恢复窗口跨越供应商usage统计日，Token差值不可比较；不得宣称零Token。"
}
$providerTokensDelta = [int64]$afterDaily.total_tokens - [int64]$beforeDaily.total_tokens
$providerCallDelta = [int64]$afterDaily.call_count - [int64]$beforeDaily.call_count
if ($providerTokensDelta -lt 0 -or $providerCallDelta -lt 0) {
    Write-TokenEvidenceFailure "PROVIDER_USAGE_COUNTER_REGRESSED" $tokenBefore $tokenAfter
    throw "供应商usage计数回退，可能发生日志轮转；Token证据不可比较。"
}

$summary = [ordered]@{
    protocol = "FINFLUX_WORKER_RECOVERY_TEST_V1.1"
    tested_at_utc = $startedAt.ToString("o")
    target_container = $ContainerName
    safety_gate = "NO_ACTIVE_AGENTTEAMS_RUN"
    model_calls = $providerCallDelta
    provider_tokens = $providerTokensDelta
    provider_token_evidence = [ordered]@{
        status = "CAPTURED"
        source = [string]$tokenAfter.snapshot.source
        usage_date_utc = [string]$afterDaily.date_utc
        before = [ordered]@{
            file = $tokenBefore.file
            sha256 = $tokenBefore.sha256
            captured_at_utc = [string]$tokenBefore.snapshot.captured_at_utc
            total_tokens = [int64]$beforeDaily.total_tokens
            call_count = [int64]$beforeDaily.call_count
        }
        after = [ordered]@{
            file = $tokenAfter.file
            sha256 = $tokenAfter.sha256
            captured_at_utc = [string]$tokenAfter.snapshot.captured_at_utc
            total_tokens = [int64]$afterDaily.total_tokens
            call_count = [int64]$afterDaily.call_count
        }
        provider_tokens_delta = $providerTokensDelta
        provider_call_delta = $providerCallDelta
        zero_token_claim = if ($providerTokensDelta -eq 0 -and $providerCallDelta -eq 0) {
            "OBSERVED_ZERO_DURING_BOUNDED_WINDOW"
        } else {
            "FORBIDDEN_PROVIDER_ACTIVITY_OBSERVED"
        }
    }
    before = [ordered]@{
        connected = [bool]$before.connected
        ready_workers = [int]$before.resources.ready_workers
        total_workers = [int]$before.resources.total_workers
    }
    during_interruption = [ordered]@{
        connected = [bool]$during.connected
        ready_workers = [int]$during.resources.ready_workers
        total_workers = [int]$during.resources.total_workers
    }
    after_recovery = [ordered]@{
        connected = [bool]$after.connected
        ready_workers = [int]$after.resources.ready_workers
        total_workers = [int]$after.resources.total_workers
    }
    container_restarted = $restarted
    result = if ($providerTokensDelta -eq 0 -and $providerCallDelta -eq 0) {
        "RECOVERED_WITH_ZERO_PROVIDER_USAGE_OBSERVED"
    } else {
        "RECOVERED_BUT_PROVIDER_ACTIVITY_OBSERVED"
    }
    truth_boundary = "仅验证无活动Run时的单Worker进程中断、拓扑降级与恢复；前后Token取自供应商usage快照并按同一UTC统计日求差。未在活动Run中断Worker，未证明Session恢复、跨节点迁移或高可用。"
}
New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
$summary | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $evidencePath -Encoding utf8
$summary | ConvertTo-Json -Depth 10

if ($providerTokensDelta -ne 0 -or $providerCallDelta -ne 0) {
    throw "Worker已恢复，但恢复窗口观察到供应商调用或Token增量；零模型验收失败。"
}
