[CmdletBinding()]
param(
    [string]$EnvFile = '',
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentTeamsRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectRoot = (Resolve-Path (Join-Path $AgentTeamsRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $AgentTeamsRoot '.env'
}
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Runtime env file not found: $EnvFile"
}

function Read-DotEnv([string]$Path) {
    $Result = @{}
    foreach ($Line in Get-Content -LiteralPath $Path) {
        if ($Line -match '^\s*#' -or $Line -notmatch '=') { continue }
        $Pair = $Line -split '=', 2
        $Result[$Pair[0].Trim()] = $Pair[1].Trim()
    }
    return $Result
}

$Values = Read-DotEnv $EnvFile
$ProviderMode = [string]$Values['AGENTTEAMS_LLM_PROVIDER']
if ($ProviderMode -ne 'openai-compat') {
    throw 'The fail-closed sidecar currently requires AGENTTEAMS_LLM_PROVIDER=openai-compat.'
}
foreach ($Role in @('MANAGER', 'LEADER', 'EVIDENCE', 'ANALYST', 'VALIDATOR', 'RESULT')) {
    $Key = "FINCHANGE_$($Role)_MODEL_PROVIDER"
    if (
        $Values.ContainsKey($Key) -and
        -not [string]::IsNullOrWhiteSpace([string]$Values[$Key]) -and
        [string]$Values[$Key] -ne 'openai-compat'
    ) {
        throw "$Key bypasses the guarded openai-compat route. Deployment stopped."
    }
}
$UpstreamBaseUrl = [string]$Values['AGENTTEAMS_OPENAI_BASE_URL']
$ApiKey = [string]$Values['AGENTTEAMS_LLM_API_KEY']
if ([string]::IsNullOrWhiteSpace($UpstreamBaseUrl) -or [string]::IsNullOrWhiteSpace($ApiKey)) {
    throw 'The upstream base URL and API key must be configured before the route can be guarded.'
}
$SidecarName = 'finflux-model-budget-gateway'
$SidecarDNSName = 'finflux-model-budget-gateway.local'
$SidecarURL = "http://$($SidecarDNSName):8090/v1"
$ConsolePort = if ($Values.ContainsKey('AGENTTEAMS_PORT_CONSOLE')) {
    [int]$Values['AGENTTEAMS_PORT_CONSOLE']
} else { 18001 }
$ConsoleURL = "http://127.0.0.1:$ConsolePort"
$ControlRoot = Join-Path $ProjectRoot 'app\runtime\model_gateway'
$AttestationPath = Join-Path $ControlRoot 'route-attestation.json'
New-Item -ItemType Directory -Path $ControlRoot -Force | Out-Null

# Higress Console is session authenticated.  Never assume that localhost is an
# authorization boundary: doing so can leave the sidecar healthy while the
# provider route silently continues to bypass it.  Keep the cookie in memory
# only and require an authenticated readback for both deploy and verify modes.
$ConsoleSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$LoginBody = @{
    username = [string]$Values['AGENTTEAMS_ADMIN_USER']
    password = [string]$Values['AGENTTEAMS_ADMIN_PASSWORD']
} | ConvertTo-Json -Compress
if (
    [string]::IsNullOrWhiteSpace([string]$Values['AGENTTEAMS_ADMIN_USER']) -or
    [string]::IsNullOrWhiteSpace([string]$Values['AGENTTEAMS_ADMIN_PASSWORD'])
) {
    throw 'Higress Console credentials are required for guarded route deployment.'
}
Invoke-RestMethod -Uri "$ConsoleURL/session/login" -Method POST `
    -ContentType 'application/json' -Body $LoginBody -WebSession $ConsoleSession | Out-Null

if (-not $VerifyOnly) {
    docker --context desktop-linux network inspect agentteams-net *> $null
    if ($LASTEXITCODE -ne 0) {
        docker --context desktop-linux network create agentteams-net *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Could not create agentteams-net.' }
    }
    docker --context desktop-linux build `
        -f (Join-Path $AgentTeamsRoot 'model-budget-gateway.Dockerfile') `
        -t finflux/model-budget-gateway:v1 `
        $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not build the model budget gateway image.' }

    $Existing = docker --context desktop-linux ps -a `
        --filter "name=^/$SidecarName$" --format '{{.Names}}'
    if ($Existing -contains $SidecarName) {
        docker --context desktop-linux rm -f $SidecarName *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Could not replace the existing budget gateway.' }
    }
    docker --context desktop-linux run -d `
        --name $SidecarName `
        --restart unless-stopped `
        --network agentteams-net `
        --network-alias finflux-model-budget-gateway `
        --network-alias $SidecarDNSName `
        -p '127.0.0.1:18769:8090' `
        -v "${ControlRoot}:/var/lib/finflux-gateway" `
        -e "FINFLUX_UPSTREAM_BASE_URL=$UpstreamBaseUrl" `
        -e 'FINFLUX_GATEWAY_CONTROL_ROOT=/var/lib/finflux-gateway' `
        finflux/model-budget-gateway:v1 *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the model budget gateway.' }

    # Higress continues to own credentials and sends its provider Authorization
    # header to the sidecar.  The sidecar stores neither the key nor payload.
    # Reuse the official openai-compat service-source identity.  Higress DNS
    # service sources require a dotted DNS name; a second arbitrary source can
    # be rejected by its validation layer.  The original provider URL remains
    # only inside the sidecar process and is no longer reachable through the
    # official service source after this authenticated PUT.
    $ServiceBody = @{
        type = 'dns'
        name = 'openai-compat'
        port = 8090
        protocol = 'http'
        proxyName = ''
        domain = $SidecarDNSName
        properties = @{}
        authN = @{ enabled = $false }
    } | ConvertTo-Json -Compress -Depth 5
    Invoke-RestMethod -Uri "$ConsoleURL/v1/service-sources/openai-compat" -Method PUT `
        -ContentType 'application/json' -Body $ServiceBody `
        -WebSession $ConsoleSession | Out-Null

    $ProviderBody = @{
        type = 'openai'
        name = 'openai-compat'
        tokens = @($ApiKey)
        version = 0
        protocol = 'openai/v1'
        tokenFailoverConfig = @{ enabled = $false }
        rawConfigs = @{
            openaiCustomUrl = $SidecarURL
            openaiCustomServiceName = 'openai-compat.dns'
            openaiCustomServicePort = 8090
        }
    } | ConvertTo-Json -Compress -Depth 5
    Invoke-RestMethod -Uri "$ConsoleURL/v1/ai/providers/openai-compat" -Method PUT `
        -ContentType 'application/json' -Body $ProviderBody `
        -WebSession $ConsoleSession | Out-Null
}

$Provider = Invoke-RestMethod -Uri "$ConsoleURL/v1/ai/providers/openai-compat" `
    -Method GET -WebSession $ConsoleSession
if ($Provider.PSObject.Properties.Name -contains 'data' -and $null -ne $Provider.data) {
    $Provider = $Provider.data
}
$ProviderURL = [string]$Provider.rawConfigs.openaiCustomUrl
if ($ProviderURL.TrimEnd('/') -ne $SidecarURL.TrimEnd('/')) {
    throw "Higress provider route does not point to the FinFlux budget sidecar."
}
$ProviderServiceName = [string]$Provider.rawConfigs.openaiCustomServiceName
$ProviderServicePort = [int]$Provider.rawConfigs.openaiCustomServicePort
if ($ProviderServiceName -ne 'openai-compat.dns' -or $ProviderServicePort -ne 8090) {
    throw 'Higress provider service binding does not point to the guarded service source.'
}
$ServiceSource = Invoke-RestMethod -Uri "$ConsoleURL/v1/service-sources/openai-compat" `
    -Method GET -WebSession $ConsoleSession
if ($ServiceSource.PSObject.Properties.Name -contains 'data' -and $null -ne $ServiceSource.data) {
    $ServiceSource = $ServiceSource.data
}
if (
    [string]$ServiceSource.type -ne 'dns' -or
    [string]$ServiceSource.domain -ne $SidecarDNSName -or
    [int]$ServiceSource.port -ne 8090 -or
    [string]$ServiceSource.protocol -ne 'http'
) {
    throw 'Higress openai-compat service source bypasses the FinFlux sidecar.'
}
$Health = Invoke-RestMethod -Uri 'http://127.0.0.1:18769/healthz' -Method GET
if ([string]$Health.status -ne 'ALIVE') {
    throw 'FinFlux model budget gateway health check failed.'
}

python (Join-Path $PSScriptRoot 'write_gateway_attestation.py') `
    --output $AttestationPath `
    --provider-name 'openai-compat' `
    --provider-custom-url $ProviderURL `
    --expected-gateway-url $SidecarURL
if ($LASTEXITCODE -ne 0) { throw 'Could not write the model route attestation.' }

Write-Host 'Model route verified: every openai-compat request traverses the FinFlux fail-closed sidecar.'
Write-Host "Route attestation: $AttestationPath"
