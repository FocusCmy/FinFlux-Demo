[CmdletBinding()]
param(
    [ValidateSet(
        'Start', 'Validate', 'BootstrapAgents', 'BuildAgents', 'ColdStart', 'Deploy', 'ExportJudge',
        'MemoryInstall', 'MemoryStart', 'MemoryStop', 'MemoryStatus', 'MemoryDoctor'
    )]
    [string]$Action = 'Start',
    [int]$Port = 8768,
    [string]$HostAddress = '127.0.0.1',
    [string]$RuntimeEnvFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Invoke-Checked([string]$Script, [object[]]$Arguments = @()) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "FinFlux action failed: $Script (exit $LASTEXITCODE)"
    }
}

switch ($Action) {
    'Start' {
        $StartArguments = @('-Port', $Port, '-HostAddress', $HostAddress)
        if (-not [string]::IsNullOrWhiteSpace($RuntimeEnvFile)) {
            $StartArguments += @('-RuntimeEnvFile', $RuntimeEnvFile)
        }
        Invoke-Checked (Join-Path $PSScriptRoot 'Start-LocalDemo.ps1') $StartArguments
    }
    'Validate' {
        Invoke-Checked (Join-Path $PSScriptRoot 'Test-Submission.ps1')
    }
    'BootstrapAgents' {
        Invoke-Checked (Join-Path $PSScriptRoot 'Bootstrap-AgentTeams.ps1')
    }
    'BuildAgents' {
        Invoke-Checked (Join-Path $ProjectRoot 'agentteams\scripts\Build-AgentPackages.ps1')
    }
    'ColdStart' {
        Invoke-Checked (Join-Path $PSScriptRoot 'Test-ColdStart.ps1')
    }
    'Deploy' {
        if ([string]::IsNullOrWhiteSpace($RuntimeEnvFile) -or -not (Test-Path -LiteralPath $RuntimeEnvFile)) {
            throw 'Deploy requires -RuntimeEnvFile pointing to an external configured .env file.'
        }
        Invoke-Checked (Join-Path $ProjectRoot 'agentteams\scripts\Deploy-AgentTeams.ps1') @(
            '-EnvFile', (Resolve-Path -LiteralPath $RuntimeEnvFile).Path
        )
    }
    'ExportJudge' {
        Invoke-Checked (Join-Path $ProjectRoot 'agentteams\scripts\Export-JudgeDemoEvidence.ps1')
    }
    'MemoryInstall' {
        Invoke-Checked (Join-Path $PSScriptRoot 'OpenViking.ps1') @('-Action', 'Install')
    }
    'MemoryStart' {
        Invoke-Checked (Join-Path $PSScriptRoot 'OpenViking.ps1') @('-Action', 'Start')
    }
    'MemoryStop' {
        Invoke-Checked (Join-Path $PSScriptRoot 'OpenViking.ps1') @('-Action', 'Stop')
    }
    'MemoryStatus' {
        Invoke-Checked (Join-Path $PSScriptRoot 'OpenViking.ps1') @('-Action', 'Status')
    }
    'MemoryDoctor' {
        Invoke-Checked (Join-Path $PSScriptRoot 'OpenViking.ps1') @('-Action', 'Doctor')
    }
}
