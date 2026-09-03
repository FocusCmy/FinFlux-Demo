[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        'evidence-investigator',
        'semantic-impact-analyst',
        'downstream-impact-analyst',
        'data-rights-steward',
        'research-context-analyst',
        'runtime-resilience-auditor',
        'independent-validator',
        'result-composer'
    )]
    [string]$Role,
    [Parameter(Mandatory = $true)]
    [string]$EnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$AgentTeamsRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectRoot = (Resolve-Path (Join-Path $AgentTeamsRoot '..')).Path
$Manager = 'agentteams-manager'
$Container = "agentteams-worker-$Role"
$Package = Join-Path $AgentTeamsRoot "build\packages\$Role.zip"

function Read-DotEnv([string]$Path) {
    $Result = @{}
    foreach ($Line in Get-Content -LiteralPath $Path) {
        if ($Line -match '^\s*#' -or $Line -notmatch '=') { continue }
        $Pair = $Line -split '=', 2
        $Result[$Pair[0].Trim()] = $Pair[1].Trim()
    }
    return $Result
}

if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Runtime env not found: $EnvFile" }
if (-not (Test-Path -LiteralPath $Package)) { throw "Built Worker package not found: $Package" }
$Values = Read-DotEnv $EnvFile
$DefaultModel = [string]$Values['AGENTTEAMS_DEFAULT_MODEL']
$ModelKey = switch ($Role) {
    'evidence-investigator' { 'FINCHANGE_EVIDENCE_MODEL' }
    'semantic-impact-analyst' { 'FINCHANGE_ANALYST_MODEL' }
    'downstream-impact-analyst' { 'FINCHANGE_ANALYST_MODEL' }
    'data-rights-steward' { 'FINCHANGE_VALIDATOR_MODEL' }
    'research-context-analyst' { 'FINCHANGE_EVIDENCE_MODEL' }
    'runtime-resilience-auditor' { 'FINCHANGE_VALIDATOR_MODEL' }
    'independent-validator' { 'FINCHANGE_VALIDATOR_MODEL' }
    'result-composer' { 'FINCHANGE_RESULT_MODEL' }
}
$Model = if ($Values.ContainsKey($ModelKey) -and -not [string]::IsNullOrWhiteSpace([string]$Values[$ModelKey])) {
    [string]$Values[$ModelKey]
} else { $DefaultModel }
if ([string]::IsNullOrWhiteSpace($Model)) { throw "Model is not configured for $Role" }

$ManagerRunning = docker --context desktop-linux inspect --format '{{.State.Running}}' $Manager 2>$null
if ($LASTEXITCODE -ne 0 -or (($ManagerRunning -join '').Trim()) -ne 'true') {
    throw 'AgentTeams Manager is not running; targeted Worker repair is not safe.'
}

docker --context desktop-linux exec $Manager mkdir -p /tmp/finflux-runtime-repair | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not create Manager repair staging directory.' }
docker --context desktop-linux cp $Package "${Manager}:/tmp/finflux-runtime-repair/$Role.zip" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not stage package for $Role" }

# Removing only the named managed container forces the v1.2.2 controller to
# allocate a fresh random host port. The Worker CR, Team, Matrix identity,
# persistent workspace and every business Run remain intact.
$Existing = docker --context desktop-linux ps -a --filter "name=^/$Container$" --format '{{.Names}}'
if ($Existing -contains $Container) {
    docker --context desktop-linux rm -f $Container | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove unhealthy container $Container" }
}

docker --context desktop-linux exec $Manager agt apply worker `
    --name $Role `
    --zip "/tmp/finflux-runtime-repair/$Role.zip" `
    --runtime qwenpaw | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not apply package for $Role" }
docker --context desktop-linux exec $Manager agt apply worker `
    --name $Role `
    --model $Model `
    --runtime qwenpaw | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restore model for $Role" }

$Deadline = [DateTime]::UtcNow.AddSeconds(150)
do {
    Start-Sleep -Seconds 3
    $Running = docker --context desktop-linux inspect --format '{{.State.Running}}' $Container 2>$null
    if ($LASTEXITCODE -eq 0 -and (($Running -join '').Trim()) -eq 'true') { break }
} while ([DateTime]::UtcNow -lt $Deadline)
if ((($Running -join '').Trim()) -ne 'true') {
    throw "Controller did not recreate $Container within 150 seconds."
}

# Reinstall the exact bounded TeamHarness enforcement bytes into the fresh
# container. This is runtime policy code, not a Worker business result.
$PatchGate = Join-Path $PSScriptRoot 'runtime_patch_gate.py'
$TeamHarnessPatch = Join-Path $ProjectRoot 'vendor\AgentTeams-v1.2.2\plugins\teamharness\mcp\server.py'
if (-not (Test-Path -LiteralPath $PatchGate) -or -not (Test-Path -LiteralPath $TeamHarnessPatch)) {
    throw 'Runtime policy patch sources are missing.'
}
$GateHash = (Get-FileHash -LiteralPath $PatchGate -Algorithm SHA256).Hash.ToLowerInvariant()
$PatchHash = (Get-FileHash -LiteralPath $TeamHarnessPatch -Algorithm SHA256).Hash.ToLowerInvariant()
docker --context desktop-linux cp $PatchGate "${Container}:/tmp/finflux-runtime-patch-gate.py" | Out-Null
docker --context desktop-linux cp $TeamHarnessPatch "${Container}:/tmp/finflux-teamharness-server.py" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not stage runtime policy patch in $Container" }
docker --context desktop-linux exec $Container python3 /tmp/finflux-runtime-patch-gate.py `
    install teamharness `
    --source /tmp/finflux-teamharness-server.py `
    --expected $PatchHash `
    --gate-sha256 $GateHash | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not install runtime policy patch in $Container" }
docker --context desktop-linux restart $Container | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restart repaired container $Container" }

$Deadline = [DateTime]::UtcNow.AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    $Running = docker --context desktop-linux inspect --format '{{.State.Running}}' $Container 2>$null
    if ($LASTEXITCODE -eq 0 -and (($Running -join '').Trim()) -eq 'true') { break }
} while ([DateTime]::UtcNow -lt $Deadline)
if ((($Running -join '').Trim()) -ne 'true') { throw "$Container did not return after policy patch." }
docker --context desktop-linux exec $Container python3 /tmp/finflux-runtime-patch-gate.py `
    readback teamharness `
    --expected $PatchHash `
    --gate-sha256 $GateHash | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Runtime policy digest readback failed in $Container" }

# Restore protocol-only Matrix output so tool/reasoning streams are not
# mistaken for cross-role business evidence after the container recreation.
$ProtocolScript = @'
import json, time, urllib.request
url = "http://127.0.0.1:8088/api/config/channels/agentteams_matrix"
last = None
for _ in range(30):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            config = json.loads(response.read())
        config["show_thinking"] = False
        config["show_tool_calls"] = False
        config["show_tool_results"] = False
        request = urllib.request.Request(url, data=json.dumps(config).encode(), headers={"Content-Type":"application/json"}, method="PUT")
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status != 200:
                raise RuntimeError("channel update failed")
        print("protocol_only=true")
        raise SystemExit(0)
    except Exception as exc:
        last = exc
        time.sleep(1)
raise SystemExit("Matrix protocol-only update failed: " + str(last))
'@
$ProtocolScript | docker --context desktop-linux exec -i $Container python3 -c 'import sys;exec(sys.stdin.read().lstrip(chr(65279)))' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restore protocol-only output for $Role" }

$PackageHash = (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLowerInvariant()
$Identity = docker --context desktop-linux exec $Container sh -lc "cat /root/agentteams-fs/agents/$Role/.qwenpaw/agent-packages/current.identity"
if ($LASTEXITCODE -ne 0 -or (($Identity -join '') -notmatch $PackageHash.Substring(0, 16))) {
    throw "Repaired container package identity does not match repository package for $Role"
}

[pscustomobject]@{
    protocol = 'FINFLUX_TARGETED_WORKER_REPAIR_V1'
    status = 'REBUILT_SAME_ROLE'
    role = $Role
    container = $Container
    package_sha256 = $PackageHash
    business_run_created = $false
    repaired_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
} | ConvertTo-Json -Compress
