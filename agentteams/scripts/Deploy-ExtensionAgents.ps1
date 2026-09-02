[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 240,
    [switch]$SkipBuild,
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentTeamsRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$BuildRoot = Join-Path $AgentTeamsRoot 'build'
$PackageRoot = Join-Path $BuildRoot 'packages'
$TeamResource = Join-Path $AgentTeamsRoot 'resources\finchange-team-expanded.yaml'
$Manager = 'agentteams-manager'
$Controller = 'agentteams-controller'
$Roles = @(
    'data-rights-steward',
    'research-context-analyst',
    'runtime-resilience-auditor'
)

function Invoke-Docker([string[]]$DockerArgs, [string]$FailureMessage) {
    & docker --context desktop-linux @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Get-WorkerModel([string]$Name) {
    $Raw = & docker --context desktop-linux exec $Controller agt get workers $Name -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($Raw -join "`n"))) {
        throw "Cannot read reference model from AgentTeams Worker: $Name"
    }
    $Worker = ($Raw -join "`n") | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$Worker.model)) {
        throw "Reference Worker has no model: $Name"
    }
    return [string]$Worker.model
}

$RunningCore = @(& docker --context desktop-linux ps --format '{{.Names}}')
foreach ($Core in @($Manager, $Controller)) {
    if ($RunningCore -notcontains $Core) {
        throw "Required AgentTeams core container is not running: $Core"
    }
}

if (-not $VerifyOnly) {
    if (-not $SkipBuild) {
        & (Join-Path $PSScriptRoot 'Build-AgentPackages.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Agent package build failed.' }
    }

    $Models = @{
        'data-rights-steward' = Get-WorkerModel 'independent-validator'
        'research-context-analyst' = Get-WorkerModel 'evidence-investigator'
        'runtime-resilience-auditor' = Get-WorkerModel 'independent-validator'
    }

    Invoke-Docker @('exec', $Manager, 'mkdir', '-p', '/tmp/finflux-extension-packages') 'Cannot prepare Manager package directory.'
    foreach ($Role in $Roles) {
        $Zip = Join-Path $PackageRoot "$Role.zip"
        if (-not (Test-Path -LiteralPath $Zip)) { throw "Missing package: $Zip" }
        Invoke-Docker @('cp', $Zip, "${Manager}:/tmp/finflux-extension-packages/$Role.zip") "Cannot copy package: $Role"
        Invoke-Docker @('exec', $Manager, 'agt', 'apply', 'worker', '--name', $Role, '--zip', "/tmp/finflux-extension-packages/$Role.zip", '--runtime', 'qwenpaw') "Cannot attach package for Worker: $Role"
        Invoke-Docker @('exec', $Manager, 'agt', 'apply', 'worker', '--name', $Role, '--model', $Models[$Role], '--runtime', 'qwenpaw') "Cannot restore model for Worker: $Role"
    }

    Invoke-Docker @('cp', $TeamResource, "${Manager}:/tmp/finchange-team-expanded.yaml") 'Cannot copy expanded Team resource.'
    Invoke-Docker @('exec', $Manager, 'agt', 'apply', '-f', '/tmp/finchange-team-expanded.yaml') 'Cannot apply expanded Team resource.'
}

$Deadline = (Get-Date).AddSeconds([Math]::Max(30, $TimeoutSeconds))
do {
    Start-Sleep -Seconds 3
    $Running = @(& docker --context desktop-linux ps --format '{{.Names}}')
    $Missing = @($Roles | Where-Object { $Running -notcontains "agentteams-worker-$_" })
    $NotRunning = @($Roles)
    if ($Missing.Count -eq 0) {
        $ProbeRaw = & docker --context desktop-linux exec $Controller agt get workers -o json 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($ProbeRaw -join "`n"))) {
            $Probe = ($ProbeRaw -join "`n") | ConvertFrom-Json
            $ProbeMap = @{}
            foreach ($ProbeWorker in $Probe.workers) { $ProbeMap[[string]$ProbeWorker.name] = $ProbeWorker }
            $NotRunning = @($Roles | Where-Object {
                -not $ProbeMap.ContainsKey($_) -or [string]$ProbeMap[$_].phase -ne 'Running'
            })
        }
    }
} while (($Missing.Count -gt 0 -or $NotRunning.Count -gt 0) -and (Get-Date) -lt $Deadline)

if ($Missing.Count -gt 0) {
    throw "Extension Agent containers did not become ready: $($Missing -join ', ')"
}
if ($NotRunning.Count -gt 0) {
    throw "Extension Agent CRs did not become Running: $($NotRunning -join ', ')"
}

$WorkersRaw = & docker --context desktop-linux exec $Controller agt get workers -o json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read AgentTeams Workers after deployment.' }
$Workers = ($WorkersRaw -join "`n") | ConvertFrom-Json
$WorkerMap = @{}
foreach ($Worker in $Workers.workers) { $WorkerMap[[string]$Worker.name] = $Worker }
foreach ($Role in $Roles) {
    if (-not $WorkerMap.ContainsKey($Role)) { throw "Worker CR missing after deployment: $Role" }
    if ([string]$WorkerMap[$Role].phase -ne 'Running') { throw "Worker is not Running: $Role" }
}

$TeamRaw = & docker --context desktop-linux exec $Controller agt get teams finchange-cross-asset-review -o json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read expanded Team after deployment.' }
$Team = ($TeamRaw -join "`n") | ConvertFrom-Json
foreach ($Role in $Roles) {
    if (@($Team.workerNames) -notcontains $Role) { throw "Worker is not associated with Team: $Role" }
}
if ([int]$Team.readyWorkers -ne [int]$Team.totalWorkers) {
    throw "Team is not fully ready: $($Team.readyWorkers)/$($Team.totalWorkers)"
}

$Report = [ordered]@{
    protocol = 'FINFLUX_AGENTTEAMS_EXTENSION_DEPLOYMENT_V0.1'
    generated_at = (Get-Date).ToUniversalTime().ToString('o')
    deployment_mode = if ($VerifyOnly) { 'VERIFY_ONLY' } else { 'INCREMENTAL_WORKERS_AND_TEAM_ONLY' }
    manager_reapplied = $false
    existing_workers_reapplied = $false
    roles = @($Roles | ForEach-Object {
        [ordered]@{
            name = $_
            phase = [string]$WorkerMap[$_].phase
            container_state = [string]$WorkerMap[$_].containerState
            model = [string]$WorkerMap[$_].model
            team = [string]$WorkerMap[$_].team
            matrix_user_id = [string]$WorkerMap[$_].matrixUserID
        }
    })
    team = [ordered]@{
        name = [string]$Team.name
        phase = [string]$Team.phase
        ready_workers = [int]$Team.readyWorkers
        total_workers = [int]$Team.totalWorkers
        worker_names = @($Team.workerNames)
    }
}
$ReportPath = Join-Path $BuildRoot 'extension-agent-deployment.json'
$Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding utf8
Write-Host "Extension Agents are associated with AgentTeams and Ready: $($Roles -join ', ')"
Write-Host "Team readiness: $($Team.readyWorkers)/$($Team.totalWorkers)"
Write-Host "Evidence: $ReportPath"
