[CmdletBinding()]
param(
    [ValidateSet('Install', 'Start', 'Stop', 'Status', 'Doctor')]
    [string]$Action = 'Status'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.openviking.yml'
$StateRoot = Join-Path $ProjectRoot '.openviking'
$ConfigPath = Join-Path $StateRoot 'ov.conf'

function Invoke-DockerCompose([string[]]$Arguments) {
    & docker compose -f $ComposeFile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "OpenViking compose action failed (exit $LASTEXITCODE)"
    }
}

function Show-ConfigurationBoundary {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Write-Warning @"
OpenViking image is installed, but .openviking\ov.conf is not configured.
Run: docker exec -it finflux-openviking openviking-server init
The wizard must configure an embedding provider and server.root_api_key.
FinFlux remains on its deterministic local hash-memory fallback until /ready succeeds.
"@
        return $false
    }
    return $true
}

switch ($Action) {
    'Install' {
        New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
        Invoke-DockerCompose @('pull', 'openviking')
        Write-Output 'OpenViking image installed. No model API was called and no FinFlux Run was started.'
        [void](Show-ConfigurationBoundary)
    }
    'Start' {
        New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
        Invoke-DockerCompose @('up', '-d', 'openviking')
        if (-not (Show-ConfigurationBoundary)) {
            Write-Output 'Container is waiting for OpenViking configuration; FinFlux remains on local hash memory.'
            return
        }
        try {
            $Ready = Invoke-RestMethod -Uri 'http://127.0.0.1:1933/ready' -TimeoutSec 3
            Write-Output ("Readiness: " + ($Ready | ConvertTo-Json -Depth 10 -Compress))
            Write-Output 'Studio: http://127.0.0.1:1933/studio'
        }
        catch {
            Write-Warning 'OpenViking is started but not ready. Run MemoryDoctor and keep FinFlux on local hash memory.'
        }
    }
    'Stop' {
        Invoke-DockerCompose @('stop', 'openviking')
    }
    'Status' {
        & docker compose -f $ComposeFile ps openviking
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Docker/OpenViking status is unavailable. FinFlux local hash memory remains available.'
            return
        }
        try {
            $Health = Invoke-RestMethod -Uri 'http://127.0.0.1:1933/health' -TimeoutSec 3
            Write-Output ("Liveness: " + ($Health | ConvertTo-Json -Compress))
        }
        catch {
            Write-Warning 'OpenViking /health is not reachable. FinFlux will use local hash memory.'
        }
        try {
            $Ready = Invoke-RestMethod -Uri 'http://127.0.0.1:1933/ready' -TimeoutSec 3
            Write-Output ("Readiness: " + ($Ready | ConvertTo-Json -Depth 10 -Compress))
        }
        catch {
            Write-Warning 'OpenViking /ready is not ready; embedding/configuration may still be missing. Local hash memory remains active.'
        }
    }
    'Doctor' {
        if (-not (Show-ConfigurationBoundary)) {
            throw 'Configure ov.conf before running Doctor.'
        }
        & docker exec finflux-openviking openviking-server doctor
        if ($LASTEXITCODE -ne 0) {
            throw "OpenViking doctor failed (exit $LASTEXITCODE)"
        }
    }
}
