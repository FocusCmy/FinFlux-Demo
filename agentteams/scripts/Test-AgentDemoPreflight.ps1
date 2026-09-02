[CmdletBinding()]
param(
    [switch]$RequireRuntimeConfig,
    [string]$EnvFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentDemoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectRoot = (Resolve-Path (Join-Path $AgentDemoRoot '..')).Path
$BuildRoot = Join-Path $AgentDemoRoot 'build'
$SourceCandidates = @()
if (-not [string]::IsNullOrWhiteSpace($env:FINFLUX_AGENTTEAMS_SOURCE_DIR)) {
    $SourceCandidates += $env:FINFLUX_AGENTTEAMS_SOURCE_DIR
}
$SourceCandidates += (Join-Path $ProjectRoot '.cache\AgentTeams-v1.2.2')
$SourceRoot = $SourceCandidates |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ 'install\agentteams-install.ps1') } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = $SourceCandidates[0]
}
$SourcePath = Join-Path $SourceRoot 'install\agentteams-install.ps1'
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null

function Read-DotEnv([string]$Path) {
    $Result = @{}
    foreach ($Line in Get-Content -LiteralPath $Path) {
        if ($Line -match '^\s*#' -or $Line -notmatch '=') { continue }
        $Pair = $Line -split '=', 2
        $Result[$Pair[0].Trim()] = $Pair[1].Trim()
    }
    return $Result
}

$DockerOk = $false
$Containers = @()
$ExpectedImages = @(
    'higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:v1.2.2',
    'higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager-copaw:v1.2.2',
    'higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-qwenpaw-worker:v1.2.2',
    'higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-worker:v1.2.2',
    'higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-copaw-worker:v1.2.2',
    'higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-hermes-worker:v1.2.2'
)
$AvailableImages = @()
$RuntimeImages = @()
try {
    docker --context desktop-linux info *> $null
    $DockerOk = ($LASTEXITCODE -eq 0)
    if ($DockerOk) {
        $Containers = @(docker --context desktop-linux ps --format '{{.Names}}')
        foreach ($Image in $ExpectedImages) {
            $RawInspect = docker --context desktop-linux image inspect $Image 2>$null
            if ($LASTEXITCODE -eq 0) {
                $Inspect = ($RawInspect -join "`n") | ConvertFrom-Json
                $AvailableImages += $Image
                $RuntimeImages += [ordered]@{
                    reference = $Image
                    id = [string]$Inspect.Id
                    repo_digest = [string]($Inspect.RepoDigests | Select-Object -First 1)
                    size_bytes = [long]$Inspect.Size
                }
            }
        }
    }
} catch {
    $DockerOk = $false
}

$EnvPath = if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    Join-Path $AgentDemoRoot '.env'
} else {
    $EnvFile
}
$EnvExists = Test-Path -LiteralPath $EnvPath
$RuntimeFieldsReady = $false
$RuntimeConfigError = $null
if ($EnvExists) {
    $Values = Read-DotEnv $EnvPath
    $Required = @('AGENTTEAMS_LLM_PROVIDER', 'AGENTTEAMS_DEFAULT_MODEL', 'AGENTTEAMS_LLM_API_KEY', 'AGENTTEAMS_ADMIN_PASSWORD')
    $RuntimeFieldsReady = $true
    foreach ($Name in $Required) {
        if (-not $Values.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace($Values[$Name]) -or $Values[$Name] -match '^(CHANGE_ME|YOUR_|<)') {
            $RuntimeFieldsReady = $false
        }
    }
    if ($RuntimeFieldsReady) {
        $Version = if ($Values.ContainsKey('AGENTTEAMS_VERSION')) { $Values['AGENTTEAMS_VERSION'] } else { '' }
        $Provider = $Values['AGENTTEAMS_LLM_PROVIDER'].ToLowerInvariant()
        if ($Version -ne 'v1.2.2') {
            $RuntimeFieldsReady = $false
            $RuntimeConfigError = 'AGENTTEAMS_VERSION must be v1.2.2'
        } elseif ($Provider -notin @('qwen', 'openai-compat')) {
            $RuntimeFieldsReady = $false
            $RuntimeConfigError = 'provider must be qwen or openai-compat'
        } elseif ($Values['AGENTTEAMS_ADMIN_PASSWORD'].Length -lt 8) {
            $RuntimeFieldsReady = $false
            $RuntimeConfigError = 'AGENTTEAMS_ADMIN_PASSWORD must be at least 8 characters'
        } elseif (-not $Values.ContainsKey('AGENTTEAMS_MANAGER_RUNTIME') -or $Values['AGENTTEAMS_MANAGER_RUNTIME'] -ne 'copaw') {
            $RuntimeFieldsReady = $false
            $RuntimeConfigError = 'AGENTTEAMS_MANAGER_RUNTIME must be copaw for the v1.2.2 Windows QwenPaw Manager image selector'
        } elseif (-not $Values.ContainsKey('AGENTTEAMS_DEFAULT_WORKER_RUNTIME') -or $Values['AGENTTEAMS_DEFAULT_WORKER_RUNTIME'] -ne 'qwenpaw') {
            $RuntimeFieldsReady = $false
            $RuntimeConfigError = 'AGENTTEAMS_DEFAULT_WORKER_RUNTIME must be qwenpaw'
        } elseif ($Provider -eq 'openai-compat') {
            $BaseUrl = if ($Values.ContainsKey('AGENTTEAMS_OPENAI_BASE_URL')) { $Values['AGENTTEAMS_OPENAI_BASE_URL'] } else { '' }
            $ParsedBaseUrl = $null
            if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
                $RuntimeFieldsReady = $false
                $RuntimeConfigError = 'openai-compat requires AGENTTEAMS_OPENAI_BASE_URL'
            } elseif (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$ParsedBaseUrl) -or $ParsedBaseUrl.Scheme -notin @('https', 'http')) {
                $RuntimeFieldsReady = $false
                $RuntimeConfigError = 'AGENTTEAMS_OPENAI_BASE_URL must be an absolute HTTP(S) URL'
            } elseif ($ParsedBaseUrl.AbsolutePath -match '/(chat/completions|responses|messages)/?$') {
                $RuntimeFieldsReady = $false
                $RuntimeConfigError = 'AGENTTEAMS_OPENAI_BASE_URL must not include the inference endpoint suffix'
            }
        }
    }
}

$ControllerRunning = $Containers -contains 'agentteams-controller'
$ManagerRunning = $Containers -contains 'agentteams-manager'
$PackageIndex = Join-Path $BuildRoot 'package-index.json'
$CoreFilesReady = $DockerOk -and (Test-Path -LiteralPath $SourcePath) -and (Test-Path -LiteralPath $PackageIndex)
$RuntimeImagesReady = $AvailableImages.Count -eq $ExpectedImages.Count
$Status = if (-not $CoreFilesReady) {
    'V122_OFFLINE_BUILD_INCOMPLETE'
} elseif (-not $RuntimeImagesReady) {
    'V122_RUNTIME_IMAGES_MISSING'
} elseif ($ControllerRunning -and $ManagerRunning) {
    'V122_RUNTIME_RUNNING_RESOURCES_UNVERIFIED'
} elseif (-not $RuntimeFieldsReady) {
    'V122_OFFLINE_ASSETS_READY_CONFIG_MISSING'
} else {
    'V122_RUNTIME_CONFIG_READY_NOT_DEPLOYED'
}

$HeadPath = Join-Path $SourceRoot '.git\HEAD'
$SourceCommit = if (Test-Path -LiteralPath $HeadPath) { (Get-Content -LiteralPath $HeadPath -Raw).Trim() } else { $null }
$SourceFileCount = if (Test-Path -LiteralPath $SourceRoot) { @(Get-ChildItem -LiteralPath $SourceRoot -Recurse -File -Force).Count } else { 0 }
$Report = [ordered]@{
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    target_version = 'v1.2.2'
    target_commit = '849182af8e017168a5a200a87b1062142caf462d'
    source_commit = $SourceCommit
    source_commit_matches = ($SourceCommit -eq '849182af8e017168a5a200a87b1062142caf462d')
    source_file_count_including_git = $SourceFileCount
    docker_context = 'desktop-linux'
    docker_ready = $DockerOk
    official_agentteams_v1_2_2_installer_present = (Test-Path -LiteralPath $SourcePath)
    agentteams_images_present = $AvailableImages.Count
    expected_agentteams_images = $ExpectedImages.Count
    runtime_images_ready = $RuntimeImagesReady
    runtime_images = $RuntimeImages
    controller_running = $ControllerRunning
    manager_running = $ManagerRunning
    worker_packages_built = (Test-Path -LiteralPath $PackageIndex)
    env_file_present = $EnvExists
    runtime_fields_ready = $RuntimeFieldsReady
    runtime_config_error = $RuntimeConfigError
    powershell_version = $PSVersionTable.PSVersion.ToString()
    powershell_7_available = [bool](Get-Command pwsh -ErrorAction SilentlyContinue)
    api_key_value_exposed = $false
    status = $Status
}
$Report | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $BuildRoot 'preflight.json')
$Report | ConvertTo-Json -Depth 6

if ($RequireRuntimeConfig -and -not $RuntimeFieldsReady) {
    throw 'Runtime configuration is intentionally absent or incomplete. Fill the gitignored agentteams/.env runtime file before deployment.'
}
