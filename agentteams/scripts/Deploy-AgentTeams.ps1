[CmdletBinding()]
param(
    [string]$EnvFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentDemoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectRoot = (Resolve-Path (Join-Path $AgentDemoRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    # Windows PowerShell 5.1 evaluates parameter defaults before $PSScriptRoot
    # is reliably available. Resolve the default only after entering the body.
    $EnvFile = Join-Path $AgentDemoRoot '.env'
}
$AgentTeamsCandidates = @()
if (-not [string]::IsNullOrWhiteSpace($env:FINFLUX_AGENTTEAMS_SOURCE_DIR)) {
    $AgentTeamsCandidates += $env:FINFLUX_AGENTTEAMS_SOURCE_DIR
}
$AgentTeamsCandidates += (Join-Path $ProjectRoot '.cache\AgentTeams-v1.2.2')
$AgentTeamsRoot = $AgentTeamsCandidates |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ 'install\agentteams-install.ps1') } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($AgentTeamsRoot)) {
    $AgentTeamsRoot = $AgentTeamsCandidates[0]
}
$Installer = Join-Path $AgentTeamsRoot 'install\agentteams-install.ps1'
$Template = Join-Path $AgentDemoRoot 'resources\finchange-resources.yaml.template'
$BuildRoot = Join-Path $AgentDemoRoot 'build'
$RenderedRoot = Join-Path $BuildRoot 'rendered'
$InstallerEnvFile = Join-Path $RenderedRoot 'agentteams-manager.env'
$Controller = 'agentteams-controller'
$Manager = 'agentteams-manager'

function Read-DotEnv([string]$Path) {
    $Result = @{}
    foreach ($Line in Get-Content -LiteralPath $Path) {
        if ($Line -match '^\s*#' -or $Line -notmatch '=') { continue }
        $Pair = $Line -split '=', 2
        $Result[$Pair[0].Trim()] = $Pair[1].Trim()
    }
    return $Result
}

function Get-Value([hashtable]$Values, [string]$Name, [string]$Default = '') {
    if ($Values.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace($Values[$Name])) {
        return $Values[$Name]
    }
    return $Default
}

function ConvertTo-YamlScalar([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-ProviderField([hashtable]$Values, [string]$Name) {
    $Provider = Get-Value $Values $Name
    if ([string]::IsNullOrWhiteSpace($Provider)) { return '' }
    return '  modelProvider: ' + (ConvertTo-YamlScalar $Provider)
}

if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Official AgentTeams v1.2.2 installer is missing: $Installer. Run scripts\FinFlux.ps1 -Action BootstrapAgents first, or set FINFLUX_AGENTTEAMS_SOURCE_DIR to an external v1.2.2 checkout."
}
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "No runtime config found at $EnvFile. Copy .env.example to .env and configure it yourself."
}

$Values = Read-DotEnv $EnvFile
$Required = @('AGENTTEAMS_LLM_PROVIDER', 'AGENTTEAMS_DEFAULT_MODEL', 'AGENTTEAMS_LLM_API_KEY', 'AGENTTEAMS_ADMIN_PASSWORD')
foreach ($Name in $Required) {
    $Value = Get-Value $Values $Name
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '^(CHANGE_ME|YOUR_|<)') {
        throw "$Name is missing or still a placeholder. Deployment stopped before any API call."
    }
}

$ConfiguredVersion = Get-Value $Values 'AGENTTEAMS_VERSION' 'v1.2.2'
if ($ConfiguredVersion -ne 'v1.2.2') {
    throw "This migration wrapper is locked to AGENTTEAMS_VERSION=v1.2.2, got $ConfiguredVersion."
}
$Provider = (Get-Value $Values 'AGENTTEAMS_LLM_PROVIDER').ToLowerInvariant()
if ($Provider -notin @('qwen', 'openai-compat')) {
    throw "AGENTTEAMS_LLM_PROVIDER must be 'qwen' or 'openai-compat'."
}
if ($Provider -eq 'openai-compat') {
    $BaseUrl = Get-Value $Values 'AGENTTEAMS_OPENAI_BASE_URL'
    $ParsedBaseUrl = $null
    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        throw 'AGENTTEAMS_OPENAI_BASE_URL is required for openai-compat.'
    }
    if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$ParsedBaseUrl) -or $ParsedBaseUrl.Scheme -notin @('https', 'http')) {
        throw 'AGENTTEAMS_OPENAI_BASE_URL must be an absolute HTTP(S) URL.'
    }
    if ($ParsedBaseUrl.AbsolutePath -match '/(chat/completions|responses|messages)/?$') {
        throw 'AGENTTEAMS_OPENAI_BASE_URL must be a base URL, not a full inference endpoint.'
    }
}
if ((Get-Value $Values 'AGENTTEAMS_ADMIN_PASSWORD').Length -lt 8) {
    throw 'AGENTTEAMS_ADMIN_PASSWORD must contain at least 8 characters.'
}
$ManagerRuntime = Get-Value $Values 'AGENTTEAMS_MANAGER_RUNTIME' 'copaw'
$WorkerRuntime = Get-Value $Values 'AGENTTEAMS_DEFAULT_WORKER_RUNTIME' 'qwenpaw'
$WorkspaceDir = Get-Value $Values 'AGENTTEAMS_WORKSPACE_DIR' (Join-Path $AgentDemoRoot 'runtime-workspace')
$Values['AGENTTEAMS_WORKSPACE_DIR'] = $WorkspaceDir
if ($ManagerRuntime -ne 'copaw') {
    throw 'For this v1.2.2 Windows deployment, AGENTTEAMS_MANAGER_RUNTIME must be copaw; it selects the QwenPaw Manager compatibility image.'
}
if ($WorkerRuntime -ne 'qwenpaw') {
    throw 'FINCHANGE v1.2.2 requires AGENTTEAMS_DEFAULT_WORKER_RUNTIME=qwenpaw.'
}

foreach ($Entry in $Values.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, 'Process')
}
$env:DOCKER_CONTEXT = 'desktop-linux'
$env:AGENTTEAMS_VERSION = 'v1.2.2'
$env:AGENTTEAMS_NON_INTERACTIVE = '1'

& (Join-Path $PSScriptRoot 'Test-AgentDemoPreflight.ps1') -RequireRuntimeConfig -EnvFile $EnvFile | Out-Host
& (Join-Path $PSScriptRoot 'Build-AgentPackages.ps1')
New-Item -ItemType Directory -Path $RenderedRoot -Force | Out-Null

# The official installer owns all AgentTeams runtime containers, networks,
# volumes, gateway routes, Matrix services, and the default Manager resource.
# Run it in a child PowerShell process so this wrapper's StrictMode does not
# turn the installer's early language lookup into a terminating error. Keep its
# generated/upgrade state separate from the user-owned .env: v1.2.2 rewrites
# the file passed via -EnvFile and must never overwrite the source API key file.
$AdminPasswordForRedaction = Get-Value $Values 'AGENTTEAMS_ADMIN_PASSWORD'
$CoreBeforeInstall = @(docker --context desktop-linux ps --format '{{.Names}}')
$CoreAlreadyRunning = (
    $CoreBeforeInstall -contains $Controller -and
    $CoreBeforeInstall -contains $Manager
)
if ($CoreAlreadyRunning) {
    Write-Host 'AgentTeams v1.2.2 core containers are already running; preserving resources and skipping the destructive in-place installer upgrade.'
} else {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Installer manager -NonInteractive -EnvFile $InstallerEnvFile 2>&1 |
        ForEach-Object { ([string]$_).Replace($AdminPasswordForRedaction, '<redacted>') } |
        Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw 'Official AgentTeams v1.2.2 installer failed. Inspect the installer log without publishing secrets.'
    }
}

$RequiredContainers = @($Controller, $Manager)
foreach ($Container in $RequiredContainers) {
    $Running = @(docker --context desktop-linux ps --filter "name=^/$Container$" --format '{{.Names}}')
    if ($LASTEXITCODE -ne 0 -or $Running -notcontains $Container) {
        throw "Expected AgentTeams v1.2.2 container is not running: $Container"
    }
}

# Rewire the official Higress openai-compat provider through a local,
# fail-closed sidecar before any FinFlux Worker is (re)started.  The script
# performs a provider readback and emits a hash-bound route attestation used by
# the application admission gate. It never prints or persists the API key.
& (Join-Path $PSScriptRoot 'Deploy-ModelBudgetGateway.ps1') `
    -EnvFile $EnvFile | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'The model budget gateway deployment/readback gate failed.'
}

$DefaultModel = Get-Value $Values 'AGENTTEAMS_DEFAULT_MODEL'
$Models = @{
    MANAGER = Get-Value $Values 'FINCHANGE_MANAGER_MODEL' $DefaultModel
    LEADER = Get-Value $Values 'FINCHANGE_LEADER_MODEL' $DefaultModel
    EVIDENCE = Get-Value $Values 'FINCHANGE_EVIDENCE_MODEL' $DefaultModel
    ANALYST = Get-Value $Values 'FINCHANGE_ANALYST_MODEL' $DefaultModel
    VALIDATOR = Get-Value $Values 'FINCHANGE_VALIDATOR_MODEL' $DefaultModel
    RESULT = Get-Value $Values 'FINCHANGE_RESULT_MODEL' $DefaultModel
}

New-Item -ItemType Directory -Path $RenderedRoot -Force | Out-Null
$Rendered = Get-Content -Raw -LiteralPath $Template
foreach ($Role in @('MANAGER', 'LEADER', 'EVIDENCE', 'ANALYST', 'VALIDATOR', 'RESULT')) {
    $Rendered = $Rendered.Replace("__$($Role)_MODEL_ID__", (ConvertTo-YamlScalar $Models[$Role]))
    $Rendered = $Rendered.Replace("__$($Role)_MODEL_PROVIDER_FIELD__", (Get-ProviderField $Values "FINCHANGE_$($Role)_MODEL_PROVIDER"))
}
$RenderedPath = Join-Path $RenderedRoot 'finchange-resources.v1.2.2.runtime.yaml'
$Rendered | Set-Content -Encoding utf8 -LiteralPath $RenderedPath

$Documents = @($Rendered -split '(?m)^\s*---\s*$')
$CoreDocuments = @($Documents | Where-Object { $_ -notmatch '(?m)^\s*kind:\s*Human\s*$' })
if ($CoreDocuments.Count -ne 11) {
    throw "Expected eleven non-Human AgentTeams resources, found $($CoreDocuments.Count)."
}
$CoreRenderedPath = Join-Path $RenderedRoot 'finchange-core.v1.2.2.runtime.yaml'
($CoreDocuments | ForEach-Object { $_.Trim() }) -join "`r`n---`r`n" |
    Set-Content -Encoding utf8 -LiteralPath $CoreRenderedPath

docker --context desktop-linux cp $CoreRenderedPath "${Manager}:/tmp/finchange-core.yaml" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not copy rendered core resources to AgentTeams Manager.' }
docker --context desktop-linux exec $Manager agt apply -f /tmp/finchange-core.yaml
if ($LASTEXITCODE -ne 0) {
    throw 'AgentTeams v1.2.2 core resource apply failed. Inspect agentteams-manager and agentteams-controller logs.'
}

# Applying the Manager CR can restart the Manager container and erase /tmp.
# Wait for the replacement container first, then copy ZIPs. Copying before the
# core apply creates a race where only the first Worker package may upload.
$ManagerReady = $false
$StableManagerID = ''
$StableManagerChecks = 0
Start-Sleep -Seconds 2
for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
    $CurrentManagerID = docker --context desktop-linux inspect --format '{{.Id}}' $Manager 2>$null
    docker --context desktop-linux exec $Manager agt version *> $null
    if ($LASTEXITCODE -eq 0 -and $CurrentManagerID) {
        if ($CurrentManagerID -eq $StableManagerID) {
            $StableManagerChecks++
        } else {
            $StableManagerID = $CurrentManagerID
            $StableManagerChecks = 1
        }
    } else {
        $StableManagerID = ''
        $StableManagerChecks = 0
    }
    if ($StableManagerChecks -ge 3) {
        $ManagerReady = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $ManagerReady) {
    throw 'AgentTeams Manager did not become ready after the resource update.'
}

# `agt apply worker --zip` uploads packages to AgentTeams MinIO and replaces the
# local path with an oss:// URI. Copying file:// packages only into Controller
# is invalid because QwenPaw resolves them inside each Worker container.
docker --context desktop-linux exec $Manager mkdir -p /tmp/finchange-packages | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not create Manager package directory.' }
foreach ($Zip in Get-ChildItem -LiteralPath (Join-Path $BuildRoot 'packages') -Filter '*.zip') {
    $PackageCopied = $false
    for ($CopyAttempt = 0; $CopyAttempt -lt 5; $CopyAttempt++) {
        docker --context desktop-linux exec $Manager mkdir -p /tmp/finchange-packages | Out-Null
        docker --context desktop-linux cp $Zip.FullName "${Manager}:/tmp/finchange-packages/$($Zip.Name)" | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $PackageCopied = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $PackageCopied) { throw "Could not copy Worker package after restart-safe retries: $($Zip.Name)" }
}

$PackageModels = [ordered]@{
    'evidence-investigator' = $Models.EVIDENCE
    'semantic-impact-analyst' = $Models.ANALYST
    'downstream-impact-analyst' = $Models.ANALYST
    'data-rights-steward' = $Models.VALIDATOR
    'research-context-analyst' = $Models.EVIDENCE
    'runtime-resilience-auditor' = $Models.VALIDATOR
    'independent-validator' = $Models.VALIDATOR
    'result-composer' = $Models.RESULT
}
foreach ($Role in $PackageModels.Keys) {
    docker --context desktop-linux exec $Manager agt apply worker `
        --name $Role `
        --zip "/tmp/finchange-packages/$Role.zip" `
        --runtime qwenpaw
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload and attach the AgentTeams package for $Role."
    }
    # v1.2.2 ignores --model when --zip is supplied, so restore the explicit
    # role model in a second patch without clearing the uploaded oss:// package.
    docker --context desktop-linux exec $Manager agt apply worker `
        --name $Role `
        --model $PackageModels[$Role] `
        --runtime qwenpaw
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restore the configured model for $Role."
    }
}

# The v1.2.2 generic apply path can create a Human, but its update endpoint
# returns HTTP 405. Keep deployment idempotent by creating this stable identity
# only when it does not already exist.
$HumanJson = docker --context desktop-linux exec $Controller agt get humans finchange-data-owner -o json 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($HumanJson -join "`n"))) {
    docker --context desktop-linux exec $Controller agt create human `
        --name finchange-data-owner `
        --display-name 'FinChange Data Owner' `
        --permission-level 2 `
        --accessible-teams finchange-cross-asset-review `
        --note '唯一放行签署者；负责 PASS、BLOCK、NEEDS_EVIDENCE 与 SkillCandidate 审批'
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the FinChange Human resource.'
    }
}

$ConfiguredHumanUser = Get-Value $Values 'FINCHANGE_MATRIX_HUMAN_USER'
$ConfiguredHumanPassword = Get-Value $Values 'FINCHANGE_MATRIX_HUMAN_PASSWORD'
if (
    [string]::IsNullOrWhiteSpace($ConfiguredHumanUser) -or
    [string]::IsNullOrWhiteSpace($ConfiguredHumanPassword)
) {
    # Only consume the controller-issued one-time credential when the external,
    # gitignored runtime config does not already contain a Human identity.  The
    # helper captures the JSON response and never prints either credential.
    & (Join-Path $PSScriptRoot 'Show-AgentTeamsHumanCredential.ps1') -WriteEnv -EnvFile $EnvFile | Out-Host
} else {
    Write-Host 'Existing external Human Matrix credentials retained; controller one-time credential was not read.'
}

$ExpectedWorkerContainers = @(
    'agentteams-worker-finchange-case-lead',
    'agentteams-worker-evidence-investigator',
    'agentteams-worker-semantic-impact-analyst',
    'agentteams-worker-downstream-impact-analyst',
    'agentteams-worker-data-rights-steward',
    'agentteams-worker-research-context-analyst',
    'agentteams-worker-runtime-resilience-auditor',
    'agentteams-worker-independent-validator',
    'agentteams-worker-result-composer'
)
$Deadline = (Get-Date).AddSeconds(240)
do {
    $RunningNames = @(docker --context desktop-linux ps --format '{{.Names}}')
    $MissingWorkers = @($ExpectedWorkerContainers | Where-Object { $RunningNames -notcontains $_ })
    if ($MissingWorkers.Count -eq 0) { break }
    # Package replacement may leave the previous container stopped while the
    # controller is still materialising the new OSS package.  The v1.2.2 CR
    # can already report Running during this window, so container truth wins.
    # Re-starting an existing stopped container is safe and avoids waiting for
    # the next controller reconcile tick; missing containers are still left to
    # the official controller.
    foreach ($MissingWorker in $MissingWorkers) {
        $Exists = @(docker --context desktop-linux ps -a --filter "name=^/$MissingWorker$" --format '{{.Names}}')
        if ($Exists -contains $MissingWorker) {
            docker --context desktop-linux start $MissingWorker *> $null
        }
    }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $Deadline)
if ($MissingWorkers.Count -gt 0) {
    throw "Worker containers failed to become ready: $($MissingWorkers -join ', ')"
}

# FinFlux's bounded runtime gate depends on executable enforcement inside the
# *running* TeamHarness and Manager plugins.  Modifying the vendored source is
# not evidence that an already-created container uses it.  Install the exact
# source bytes into both the image-local and QwenPaw workspace plugin paths,
# restart, then require a post-restart SHA256 readback from every target.
$TeamHarnessPatch = Join-Path $AgentTeamsRoot 'plugins\teamharness\mcp\server.py'
$ManagerToolsPatch = Join-Path $AgentTeamsRoot 'plugins\agentteams-manager-tools\plugin.py'
foreach ($PatchSource in @($TeamHarnessPatch, $ManagerToolsPatch)) {
    if (-not (Test-Path -LiteralPath $PatchSource)) {
        throw "Required FinFlux runtime patch is missing: $PatchSource"
    }
}
if (-not (Select-String -LiteralPath $TeamHarnessPatch -SimpleMatch 'finflux-bounded-tool-profile' -Quiet)) {
    throw 'TeamHarness source lacks the FinFlux bounded-tool-profile enforcement marker.'
}
$TeamHarnessPatchHash = (Get-FileHash -LiteralPath $TeamHarnessPatch -Algorithm SHA256).Hash.ToLowerInvariant()
$ManagerToolsPatchHash = (Get-FileHash -LiteralPath $ManagerToolsPatch -Algorithm SHA256).Hash.ToLowerInvariant()
$RuntimePatchGate = Join-Path $PSScriptRoot 'runtime_patch_gate.py'
if (-not (Test-Path -LiteralPath $RuntimePatchGate)) {
    throw "Runtime patch gate is missing: $RuntimePatchGate"
}
$RuntimePatchGateHash = (Get-FileHash -LiteralPath $RuntimePatchGate -Algorithm SHA256).Hash.ToLowerInvariant()

foreach ($Container in $ExpectedWorkerContainers) {
    docker --context desktop-linux cp $RuntimePatchGate "${Container}:/tmp/finflux-runtime-patch-gate.py" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not stage runtime patch gate in $Container" }
    docker --context desktop-linux cp $TeamHarnessPatch "${Container}:/tmp/finflux-teamharness-server.py" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not stage TeamHarness patch in $Container" }
    docker --context desktop-linux exec $Container python3 /tmp/finflux-runtime-patch-gate.py `
        install teamharness `
        --source /tmp/finflux-teamharness-server.py `
        --expected $TeamHarnessPatchHash `
        --gate-sha256 $RuntimePatchGateHash | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not install TeamHarness patch in $Container" }
}
docker --context desktop-linux cp $RuntimePatchGate "${Manager}:/tmp/finflux-runtime-patch-gate.py" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not stage runtime patch gate in Manager.' }
docker --context desktop-linux cp $ManagerToolsPatch "${Manager}:/tmp/finflux-manager-tools-plugin.py" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not stage Manager tools patch.' }
docker --context desktop-linux exec $Manager python3 /tmp/finflux-runtime-patch-gate.py `
    install manager `
    --source /tmp/finflux-manager-tools-plugin.py `
    --expected $ManagerToolsPatchHash `
    --gate-sha256 $RuntimePatchGateHash | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Could not install Manager tools patch.' }

foreach ($Container in @($Manager) + $ExpectedWorkerContainers) {
    docker --context desktop-linux restart $Container | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restart patched runtime: $Container" }
}

$RuntimePatchDeadline = (Get-Date).AddSeconds(180)
do {
    $PatchedRunning = @(docker --context desktop-linux ps --format '{{.Names}}')
    $PatchedMissing = @((@($Manager) + $ExpectedWorkerContainers) | Where-Object { $PatchedRunning -notcontains $_ })
    if ($PatchedMissing.Count -eq 0) { break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $RuntimePatchDeadline)
if ($PatchedMissing.Count -gt 0) {
    throw "Patched runtimes did not return after restart: $($PatchedMissing -join ', ')"
}
foreach ($Container in $ExpectedWorkerContainers) {
    docker --context desktop-linux exec $Container python3 /tmp/finflux-runtime-patch-gate.py `
        readback teamharness `
        --expected $TeamHarnessPatchHash `
        --gate-sha256 $RuntimePatchGateHash | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "TeamHarness post-restart hash gate failed in $Container" }
}
docker --context desktop-linux exec $Manager python3 /tmp/finflux-runtime-patch-gate.py `
    readback manager `
    --expected $ManagerToolsPatchHash `
    --gate-sha256 $RuntimePatchGateHash | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Manager tools post-restart hash gate failed.' }

# Keep private execution traces in each QwenPaw Console while exposing only
# final business messages to shared Matrix rooms. Without this official
# channel setting, reasoning/tool streams become cross-role messages and can
# leak context as well as consume the bounded protocol budget.
$MatrixProtocolOnlyScript = @'
import json
import time
import urllib.request

url = "http://127.0.0.1:8088/api/config/channels/agentteams_matrix"
last_error = None
for _ in range(30):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            config = json.loads(response.read())
        if not isinstance(config, dict):
            raise RuntimeError("AgentTeams Matrix plugin config is missing")
        config["show_thinking"] = False
        config["show_tool_calls"] = False
        config["show_tool_results"] = False
        request = urllib.request.Request(
            url,
            data=json.dumps(config).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status != 200:
                raise RuntimeError("channel update returned HTTP " + str(response.status))
        with urllib.request.urlopen(url, timeout=5) as response:
            actual = json.loads(response.read())
        for key in ("show_thinking", "show_tool_calls", "show_tool_results"):
            if actual.get(key) is not False:
                raise RuntimeError("channel readback mismatch for " + key)
        print("matrix_protocol_only=true")
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(1)
raise SystemExit("Matrix protocol-only update failed: " + str(last_error))
'@
# Worker package reconciliation can rewrite the Matrix plugin defaults a few
# seconds after the CR first reports Running. Re-apply after the runtime has
# settled, and require the final API readback to remain false.
function Wait-AgentContainerRunning([string]$Container, [int]$TimeoutSeconds = 90) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Running = docker --context desktop-linux inspect --format '{{.State.Running}}' $Container 2>$null
        if ($LASTEXITCODE -eq 0 -and (($Running -join '').Trim() -eq 'true')) {
            return
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Agent container did not return to Running within ${TimeoutSeconds}s: $Container"
}

foreach ($ProtocolPass in 1..3) {
    foreach ($Container in $ExpectedWorkerContainers) {
        Wait-AgentContainerRunning -Container $Container
        $MatrixProtocolOnlyScript | docker --context desktop-linux exec -i $Container `
            python3 -c 'import sys;exec(sys.stdin.read().lstrip(chr(65279)))'
        if ($LASTEXITCODE -ne 0) {
            throw "Could not enable Matrix protocol-only output for $Container"
        }
    }
    if ($ProtocolPass -lt 3) {
        Start-Sleep -Seconds 6
    }
}

docker --context desktop-linux exec $Controller agt get workers
docker --context desktop-linux exec $Controller agt get teams
$HumanStatusRaw = docker --context desktop-linux exec $Controller agt get humans finchange-data-owner -o json
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($HumanStatusRaw -join "`n"))) {
    throw 'Could not read the final Human resource status.'
}
$HumanStatus = ($HumanStatusRaw -join "`n") | ConvertFrom-Json
[pscustomobject]@{
    name = [string]$HumanStatus.name
    phase = [string]$HumanStatus.phase
    matrixUserID = [string]$HumanStatus.matrixUserID
    credential = if ([string]::IsNullOrWhiteSpace([string]$HumanStatus.initialPassword)) {
        'NOT_RETURNED'
    } else {
        'PRESENT_REDACTED'
    }
} | Format-List | Out-Host

Write-Host 'AgentTeams v1.2.2 resources applied.'
Write-Host 'Human Matrix credentials were synchronized into the gitignored runtime config without being displayed.'
Write-Host 'The web console remains fail-closed until the core Team is ready; RootRouteDecision additionally requires every selected extension Worker. Seven route-selectable specialists form conditional evidence quorum and Result Composer remains zero-model-first.'
