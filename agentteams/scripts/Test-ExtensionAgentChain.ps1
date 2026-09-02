[CmdletBinding()]
param(
    [string]$SourceRunId = 'RUN-LIVE-20260829115130-a28359',
    [int]$ToolTimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentTeamsRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectRoot = (Resolve-Path (Join-Path $AgentTeamsRoot '..')).Path
$BuildRoot = Join-Path $AgentTeamsRoot 'build'
$ProbeSpecPath = Join-Path $BuildRoot 'extension-agent-probe-input.json'
$ResultPath = Join-Path $BuildRoot 'extension-agent-chain-result.json'
$MarkdownPath = Join-Path $BuildRoot 'extension-agent-chain-result.md'
$Controller = 'agentteams-controller'
$TeamName = 'finchange-cross-asset-review'
$PolicyId = 'FINFLUX-BOUNDED-EXECUTION-V0.1'
$PackageRoot = '/root/agentteams-fs/agents/{0}/.qwenpaw/agent-packages/current'
$TaskRoot = "/root/agentteams-fs/teams/$TeamName/shared/tasks"
$ContextRoot = "/root/agentteams-fs/teams/$TeamName/shared/context-capsules"

if ($ToolTimeoutSeconds -lt 1 -or $ToolTimeoutSeconds -gt 90) {
    throw 'ToolTimeoutSeconds must be between 1 and 90.'
}

function Invoke-DockerJson([string[]]$DockerArgs, [string]$FailureMessage) {
    $Raw = & docker --context desktop-linux @DockerArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage`n$($Raw -join "`n")"
    }
    $Text = ($Raw -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($Text)) { throw "$FailureMessage (empty JSON)" }
    return ($Text | ConvertFrom-Json)
}

function Get-TokenGuard() {
    return Invoke-RestMethod -Uri 'http://127.0.0.1:8768/api/v1/token-guard' -TimeoutSec 15
}

& (Join-Path $PSScriptRoot 'Deploy-ExtensionAgents.ps1') -VerifyOnly
if ($LASTEXITCODE -ne 0) { throw 'AgentTeams extension association verification failed.' }

$GuardBefore = Get-TokenGuard
$PayloadBuilder = Join-Path $PSScriptRoot 'build_extension_probe_payload.py'
& python $PayloadBuilder --source-run-id $SourceRunId --output $ProbeSpecPath
if ($LASTEXITCODE -ne 0) { throw 'Cannot build extension acceptance payload.' }
$Probe = Get-Content -Raw -LiteralPath $ProbeSpecPath -Encoding utf8 | ConvertFrom-Json

$Workers = @(
    [ordered]@{
        role = 'evidence-investigator'
        suffix = 'evidence'
        result_file = 'evidence_result.json'
        expected_status = 'VERIFIED'
        expected_invocations = 2
    },
    [ordered]@{
        role = 'semantic-impact-analyst'
        suffix = 'semantic'
        result_file = 'semantic_impact_result.json'
        expected_status = 'SUCCESS'
        expected_invocations = 2
    },
    [ordered]@{
        role = 'data-rights-steward'
        suffix = 'rights'
        result_file = 'rights_review_result.json'
        expected_status = 'PASS'
        expected_invocations = 2
    },
    [ordered]@{
        role = 'research-context-analyst'
        suffix = 'research'
        result_file = 'research_context_result.json'
        expected_status = 'VERIFIED_CONTEXT'
        expected_invocations = 2
    },
    [ordered]@{
        role = 'runtime-resilience-auditor'
        suffix = 'resilience'
        result_file = 'runtime_resilience_result.json'
        expected_status = 'READY_FOR_CHECKPOINTED_RUN'
        expected_invocations = 2
    },
    [ordered]@{
        role = 'independent-validator'
        suffix = 'validator'
        result_file = 'independent_validation.json'
        expected_status = 'PASS'
        expected_invocations = 1
    }
)

$ContextTargets = @('agentteams-manager') + @($Workers | ForEach-Object { "agentteams-worker-$([string]$_.role)" })
foreach ($ContextTarget in $ContextTargets) {
    & docker --context desktop-linux exec $ContextTarget mkdir -p $ContextRoot
    if ($LASTEXITCODE -ne 0) { throw "Cannot create Context Capsule directory in $ContextTarget." }
    & docker --context desktop-linux cp ([string]$Probe.context_capsule_local_path) "${ContextTarget}:$([string]$Probe.context_capsule_shared_path)"
    if ($LASTEXITCODE -ne 0) { throw "Cannot publish Context Capsule to $ContextTarget." }
}

$Chain = @()
foreach ($Worker in $Workers) {
    $Role = [string]$Worker.role
    $TaskId = "task-$($Probe.case_id)-$($Probe.probe_run_id)-$($Worker.suffix)"
    $Container = "agentteams-worker-$Role"
    $RolePackageRoot = [string]::Format($PackageRoot, $Role)
    $ToolPath = "$RolePackageRoot/tool_gateway.py"
    $DockerArgs = @(
        'exec', $Container,
        'python', $ToolPath,
        '--entry', 'bounded-worker',
        '--timeout-s', [string]$ToolTimeoutSeconds,
        '--',
        '--role', $Role,
        '--asset', 'futures',
        '--case-id', [string]$Probe.case_id,
        '--run-id', [string]$Probe.probe_run_id,
        '--task-id', $TaskId,
        '--policy-id', $PolicyId,
        '--scenario', 'blocked',
        '--context-capsule-ref', [string]$Probe.context_capsule_sha256
    )
    $Execution = & docker --context desktop-linux @DockerArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Bounded Agent execution failed: $Role`n$($Execution -join "`n")"
    }

    $TaskPath = "$TaskRoot/$TaskId"
    $Result = Invoke-DockerJson @('exec', $Container, 'cat', "$TaskPath/$($Worker.result_file)") "Cannot read result for $Role"
    $Receipt = Invoke-DockerJson @('exec', $Container, 'cat', "$TaskPath/tool_execution_receipt.json") "Cannot read tool receipt for $Role"
    if ([string]$Result.status -ne [string]$Worker.expected_status) {
        throw "Unexpected status for ${Role}: $($Result.status)"
    }
    $Invocations = @($Result.skill_invocations)
    if ($Invocations.Count -ne [int]$Worker.expected_invocations) {
        throw "Unexpected runtime Skill invocation count for ${Role}: $($Invocations.Count)"
    }
    if (@($Invocations | Where-Object { -not [bool]$_.discovered_at_runtime }).Count -gt 0) {
        throw "Runtime Skill discovery proof is incomplete for $Role"
    }
    if ([string]$Receipt.status -ne 'SUCCEEDED' -or [int]$Receipt.provider_tokens -ne 0) {
        throw "Tool receipt is not a zero-token success for $Role"
    }
    if ([string]$Receipt.context_transport -ne 'CONTENT_ADDRESSED_ROLE_SLICE' -or [string]$Result.context_cache_status -ne 'HIT_SHARED_CAPSULE') {
        throw "Context Capsule transport was not used by $Role"
    }
    $Chain += [ordered]@{
        role = $Role
        container = $Container
        task_id = $TaskId
        status = [string]$Result.status
        tool_run_id = [string]$Result.tool_run_id
        result_file = [string]$Worker.result_file
        tool_receipt_sha256 = [string]$Receipt.receipt_sha256
        provider_tokens = [int]$Receipt.provider_tokens
        context_transport = [string]$Receipt.context_transport
        context_capsule_sha256 = [string]$Result.context_capsule_sha256
        context_slice_sha256 = [string]$Result.context_slice_sha256
        skill_invocations = @($Invocations | ForEach-Object {
            [ordered]@{
                skill_id = [string]$_.skill_id
                version = [string]$_.version
                digest = [string]$_.digest
                input_sha256 = [string]$_.input_sha256
                output_sha256 = [string]$_.output_sha256
                status = [string]$_.status
                discovered_at_runtime = [bool]$_.discovered_at_runtime
            }
        })
    }
}

$WorkerSnapshot = Invoke-DockerJson @('exec', $Controller, 'agt', 'get', 'workers', '-o', 'json') 'Cannot read AgentTeams Worker snapshot.'
$TeamSnapshot = Invoke-DockerJson @('exec', $Controller, 'agt', 'get', 'teams', $TeamName, '-o', 'json') 'Cannot read AgentTeams Team snapshot.'
$WorkerMap = @{}
foreach ($Item in $WorkerSnapshot.workers) { $WorkerMap[[string]$Item.name] = $Item }
$Associations = @($Workers | ForEach-Object {
    $Name = [string]$_.role
    [ordered]@{
        name = $Name
        phase = [string]$WorkerMap[$Name].phase
        container_state = [string]$WorkerMap[$Name].containerState
        team = [string]$WorkerMap[$Name].team
        matrix_user_id = [string]$WorkerMap[$Name].matrixUserID
    }
})
$GuardAfter = Get-TokenGuard
$TokenDelta = [int64]$GuardAfter.active_run_provider_tokens - [int64]$GuardBefore.active_run_provider_tokens
if ($TokenDelta -ne 0) { throw "Provider Token changed during zero-model chain: $TokenDelta" }

$Report = [ordered]@{
    protocol = 'FINFLUX_CONTEXT_CAPSULE_AGENT_CHAIN_ACCEPTANCE_V1.0'
    generated_at_utc = [DateTime]::UtcNow.ToString('o')
    acceptance_mode = 'REAL_AGENTTEAMS_CONTAINERS_CONTEXT_CAPSULE_BOUNDED_SKILL_CHAIN_NO_MODEL_CALL'
    source_run_id = [string]$Probe.source_run_id
    probe_run_id = [string]$Probe.probe_run_id
    case_id = [string]$Probe.case_id
    submission_id = [string]$Probe.submission_id
    real_evidence = [ordered]@{
        instrument = [string]$Probe.instrument
        trade_date = [string]$Probe.trade_date
        candidate_mapping = [string]$Probe.candidate_mapping
        research_item_count = [int]$Probe.research_item_count
        file_sha256 = [string]$Probe.file_sha256
        evidence_root_hash = [string]$Probe.evidence_root_hash
        worker_payload_sha256 = [string]$Probe.worker_payload_sha256
    }
    agentteams_association = [ordered]@{
        team_name = [string]$TeamSnapshot.name
        phase = [string]$TeamSnapshot.phase
        ready_workers = [int]$TeamSnapshot.readyWorkers
        total_workers = [int]$TeamSnapshot.totalWorkers
        tested_agents = $Associations
    }
    chain = $Chain
    token_audit = [ordered]@{
        source = [string]$GuardAfter.source
        provider_usage_captured = [bool]$GuardAfter.provider_usage_captured
        active_run_id = [string]$GuardAfter.active_run_id
        active_run_state = [string]$GuardAfter.active_run_state
        before_provider_tokens = [int64]$GuardBefore.active_run_provider_tokens
        after_provider_tokens = [int64]$GuardAfter.active_run_provider_tokens
        probe_provider_token_delta = $TokenDelta
    }
    acceptance = [ordered]@{
        runtime_associated = $true
        team_ready = ([int]$TeamSnapshot.readyWorkers -eq [int]$TeamSnapshot.totalWorkers)
        bounded_skill_chain_passed = $true
        runtime_skill_discovery_recorded = $true
        context_capsule_loaded_by_every_worker = $true
        provider_token_delta_zero = ($TokenDelta -eq 0)
        new_matrix_model_collaboration_run_executed = $false
    }
    truth_boundary = 'This acceptance proves AgentTeams CR/Team/container association and real containerized Context Capsule plus bounded Skill execution. It does not claim a new Matrix/LLM collaboration Run and does not describe character savings as provider-token savings.'
}
$Report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ResultPath -Encoding utf8

$SkillLines = @($Chain | ForEach-Object {
    $SkillNames = @($_.skill_invocations | ForEach-Object { "$($_.skill_id)@$($_.version)" }) -join ', '
    "| $($_.role) | $($_.status) | $SkillNames | $($_.provider_tokens) |"
})
$Markdown = @(
    '# FinFlux 六专业 Agent Context Capsule 运行时链路验收',
    '',
    "- Probe Run: ``$($Probe.probe_run_id)``",
    "- Source Judge Run: ``$($Probe.source_run_id)``",
    "- 真实证据: $($Probe.instrument) / $($Probe.trade_date) / 文件 SHA256 ``$($Probe.file_sha256)``",
    "- AgentTeams Team: ``$($TeamSnapshot.name)``，``$($TeamSnapshot.readyWorkers)/$($TeamSnapshot.totalWorkers) Ready``",
    "- 本次新增供应商 Token: ``$TokenDelta``",
    '',
    '| Agent | 结果 | 运行时发现的 Skill | Provider Token |',
    '|---|---|---|---:|',
    $SkillLines,
    '',
    '## 验收结论',
    '',
    '六个专业 Agent 已拥有独立 AgentTeams Worker CR、Matrix 身份、容器、角色包与 Team 成员关系；本次在各自真实 AgentTeams 容器中通过同一Context Capsule哈希加载最小角色切片，运行11个版本化Worker Skill，产生输入/输出哈希、Slice哈希和工具回执。',
    '',
    '## 真实性边界',
    '',
    '本验收证明运行时关联、内容寻址上下文和确定性 Skill 链已经可执行；本次没有发起新的 Matrix/大模型协作 Run，也不把受控工具验收冒充成完整多轮模型协作。'
)
$Markdown | Set-Content -LiteralPath $MarkdownPath -Encoding utf8

Write-Host "Context Capsule Agent chain PASSED: $($Chain.Count)/$($Workers.Count)"
Write-Host "AgentTeams Team readiness: $($TeamSnapshot.readyWorkers)/$($TeamSnapshot.totalWorkers)"
Write-Host "Provider Token delta: $TokenDelta"
Write-Host "JSON evidence: $ResultPath"
Write-Host "Human-readable evidence: $MarkdownPath"
