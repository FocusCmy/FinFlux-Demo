[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$Listener.Start()
$Port = ([Net.IPEndPoint]$Listener.LocalEndpoint).Port
$Listener.Stop()
$LogRoot = Join-Path ([IO.Path]::GetTempPath()) ('finflux-cold-start-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$StdOut = Join-Path $LogRoot 'stdout.log'
$StdErr = Join-Path $LogRoot 'stderr.log'
$Process = $null
try {
    $Process = Start-Process -FilePath 'python' `
        -ArgumentList @('.\app\app.py', '--host', '127.0.0.1', '--port', $Port) `
        -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $StdOut -RedirectStandardError $StdErr
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        if ($Process.HasExited) { break }
        try {
            $Status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 2
            $Evaluation = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/evaluation-report" -TimeoutSec 2
            if ($Status.project -eq 'FinFlux' -and $Evaluation.corpus.case_count -eq 150) {
                $Ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $Ready) {
        $ErrorText = if (Test-Path -LiteralPath $StdErr) { Get-Content -LiteralPath $StdErr -Raw } else { '' }
        throw "FinFlux cold start failed. $ErrorText"
    }
    Write-Output "Cold start passed: API ready on ephemeral port $Port; source-bound corpus=150."
} finally {
    if ($null -ne $Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force
        $Process.WaitForExit()
    }
}
