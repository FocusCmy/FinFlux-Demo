[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $ProjectRoot
try {
    Write-Host '[1/5] Build the eight data-neutral AgentTeams Worker packages'
    & powershell -NoProfile -ExecutionPolicy Bypass -File .\agentteams\scripts\Build-AgentPackages.ps1
    if ($LASTEXITCODE -ne 0) { throw 'Build-AgentPackages.ps1 failed' }

    Write-Host '[2/5] Validate AgentTeams configuration and packages'
    & python .\agentteams\scripts\validate_agent_demo.py
    if ($LASTEXITCODE -ne 0) { throw 'validate_agent_demo.py failed' }

    Write-Host '[3/5] Smoke-test deterministic Skills in Worker packages'
    & python .\agentteams\scripts\smoke_test_packages.py
    if ($LASTEXITCODE -ne 0) { throw 'smoke_test_packages.py failed' }

    Write-Host '[4/5] Run the public-repository core test suite'
    $PublicTestModules = @(
        'test_agentteams_adapter',
        'test_bounded_change_task',
        'test_change_control',
        'test_context_capsule',
        'test_context_memory',
        'test_control_plane',
        'test_decision_workflow',
        'test_emergency_stop',
        'test_live_intake',
        'test_manager_routing',
        'test_model_budget_gateway',
        'test_model_gateway_control',
        'test_p0_ui_contract',
        'test_prompt_budget_readiness',
        'test_protocol_v02',
        'test_provider_usage_attribution',
        'test_real_50x3_evaluation',
        'test_research_data',
        'test_run_lifecycle',
        'test_run_supervisor',
        'test_structured_memory',
        'test_task_identity',
        'test_tool_gateway',
        'test_unified_intake',
        'test_v02_acceptance_orchestrator',
        'test_v02_live_acceptance',
        'test_v02_projection_export_gate',
        'test_worker_recovery_script',
        'test_worker_skill_runtime',
        'test_zero_model_runtime_gate'
    )
    Push-Location .\app
    try {
        & python -m unittest @PublicTestModules -v
        if ($LASTEXITCODE -ne 0) { throw 'Public core tests failed' }
    } finally {
        Pop-Location
    }

    Write-Host '[5/5] Parse-check modular frontend JavaScript'
    if (Get-Command node -ErrorAction SilentlyContinue) {
        Get-ChildItem -LiteralPath .\app\web -Recurse -File -Filter '*.js' | ForEach-Object {
            & node --check $_.FullName
            if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed: $($_.FullName)" }
        }
    } else {
        Write-Host 'Node.js not found; JavaScript syntax check skipped.' -ForegroundColor Yellow
    }

    Write-Host 'FinFlux single-product submission self-check passed.' -ForegroundColor Green
} finally {
    Pop-Location
}
