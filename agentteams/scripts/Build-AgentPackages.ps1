[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AgentDemoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectRoot = (Resolve-Path (Join-Path $AgentDemoRoot '..')).Path
$AppRoot = @(
    (Join-Path $ProjectRoot 'app'),
    (Join-Path $ProjectRoot 'demo')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($AppRoot)) { throw 'FinFlux app source is missing.' }
$BuildRoot = Join-Path $AgentDemoRoot 'build'
$StageRoot = Join-Path $BuildRoot 'staging'
$PackageOutput = Join-Path $BuildRoot 'packages'

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function New-AgentPackageZip([string]$SourceDirectory, [string]$DestinationPath) {
    # Compress-Archive writes Windows backslashes into ZIP entry names. Python's
    # zipfile extractor on the Linux QwenPaw runtime treats those as literal
    # characters, so config\AGENTS.md never becomes config/AGENTS.md and the
    # package prompt is silently not applied. Always emit portable POSIX names.
    $RootPath = (Resolve-Path -LiteralPath $SourceDirectory).Path
    $Archive = [System.IO.Compression.ZipFile]::Open(
        $DestinationPath,
        [System.IO.Compression.ZipArchiveMode]::Create
    )
    try {
        foreach ($File in Get-ChildItem -LiteralPath $RootPath -Recurse -File | Where-Object {
            $_.Extension -ne '.pyc' -and $_.FullName -notmatch '[\\/]__pycache__[\\/]'
        }) {
            $EntryName = $File.FullName.Substring($RootPath.Length)
            $EntryName = $EntryName.TrimStart(
                [System.IO.Path]::DirectorySeparatorChar,
                [System.IO.Path]::AltDirectorySeparatorChar
            ).Replace('\', '/')
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $Archive,
                $File.FullName,
                $EntryName,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    }
    finally {
        $Archive.Dispose()
    }

    $Readback = [System.IO.Compression.ZipFile]::OpenRead($DestinationPath)
    try {
        $EntryNames = @($Readback.Entries | ForEach-Object { $_.FullName })
        if ($EntryNames | Where-Object { $_.Contains('\') }) {
            throw "Agent package contains non-portable backslash entries: $DestinationPath"
        }
        foreach ($RequiredEntry in @('config/AGENTS.md', 'config/SOUL.md', 'task_identity.py')) {
            if ($EntryNames -notcontains $RequiredEntry) {
                throw "Agent package is missing required portable entry $RequiredEntry"
            }
        }
    }
    finally {
        $Readback.Dispose()
    }
}

foreach ($Path in @($StageRoot, $PackageOutput)) {
    if (Test-Path -LiteralPath $Path) {
        $Resolved = (Resolve-Path -LiteralPath $Path).Path
        if (-not $Resolved.StartsWith($BuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean path outside the AgentTeams build directory: $Resolved"
        }
        Remove-Item -LiteralPath $Resolved -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

$Roles = @(
    'evidence-investigator',
    'semantic-impact-analyst',
    'downstream-impact-analyst',
    'data-rights-steward',
    'research-context-analyst',
    'runtime-resilience-auditor',
    'independent-validator',
    'result-composer'
)
$RuntimeSkillRoles = @(
    'evidence-investigator',
    'semantic-impact-analyst',
    'data-rights-steward',
    'research-context-analyst',
    'runtime-resilience-auditor',
    'independent-validator'
)

# The application runtime verifies the same frozen manifests before a Worker
# package is deployed.  Refresh those source manifests first so changing the
# bounded entrypoint cannot leave local acceptance and packaged execution on
# different hashes.
$FreezeRuntimeManifests = Join-Path $PSScriptRoot 'freeze_runtime_skill_manifests.py'
& python $FreezeRuntimeManifests $AppRoot
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to freeze application runtime Skill manifests.'
}

# The source manifests are frozen against the app tree (package_root="..").
# A deployable Agent package has a different filesystem layout, so copying the
# source JSON verbatim leaves entrypoint/instruction hashes pointing outside the
# ZIP.  Rebuild the package-local manifest from the staged bytes and from the
# actual Python callable source before the archive is sealed.
$RuntimeManifestBuilder = @'
import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


source_path = Path(sys.argv[1]).resolve()
stage = Path(sys.argv[2]).resolve()
manifest = json.loads(source_path.read_text(encoding="utf-8-sig"))
manifest.pop("manifest_sha256", None)
manifest["package_root"] = "."
manifest["entrypoint"] = {
    "path": "bounded_worker_task.py",
    "sha256": sha256_file(stage / "bounded_worker_task.py"),
}

sys.path.insert(0, str(stage))
spec = importlib.util.spec_from_file_location(
    "finflux_packaged_bounded_worker_task", stage / "bounded_worker_task.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load staged bounded_worker_task.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

for item in manifest.get("skills") or []:
    skill_id = str(item["skill_id"])
    instruction_rel = f"skills/{skill_id}/SKILL.md"
    instruction = stage / instruction_rel
    callable_name = str(item["callable"])
    callable_obj = getattr(module, callable_name, None)
    if not callable(callable_obj):
        raise RuntimeError(f"runtime Skill callable is unavailable: {callable_name}")
    item["instruction_path"] = instruction_rel
    item["instruction_sha256"] = sha256_file(instruction)
    item["callable_sha256"] = hashlib.sha256(
        inspect.getsource(callable_obj).encode("utf-8")
    ).hexdigest()

manifest["manifest_sha256"] = canonical_sha256(manifest)
(stage / "runtime-skill-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
'@
$RuntimeManifestBuilderPath = Join-Path $BuildRoot 'runtime-manifest-builder.py'
[System.IO.File]::WriteAllText(
    $RuntimeManifestBuilderPath,
    $RuntimeManifestBuilder,
    [System.Text.UTF8Encoding]::new($false)
)
$Index = @()

foreach ($Role in $Roles) {
    $Source = Join-Path $AgentDemoRoot "packages\$Role"
    $Stage = Join-Path $StageRoot $Role
    Copy-Item -LiteralPath $Source -Destination $Stage -Recurse

    $Map = Get-Content -Raw (Join-Path $Source 'skills.map.json') | ConvertFrom-Json
    $SkillsRoot = Join-Path $Stage 'skills'
    New-Item -ItemType Directory -Path $SkillsRoot -Force | Out-Null
    foreach ($Skill in $Map.skills) {
        $SkillSource = Join-Path $AppRoot "agentteams-skills\$Skill"
        if (-not (Test-Path -LiteralPath (Join-Path $SkillSource 'SKILL.md'))) {
            throw "Missing Skill source: $SkillSource"
        }
        Copy-Item -LiteralPath $SkillSource -Destination (Join-Path $SkillsRoot $Skill) -Recurse
    }

    # Every deployable Worker package carries the same exact Case/Run/role
    # identity derivation.  This includes the deterministic Result Composer:
    # it may not execute a financial Skill, but any result it composes must be
    # addressable in the same nonce-bound Run namespace.
    Copy-Item -LiteralPath (Join-Path $AppRoot 'task_identity.py') -Destination (Join-Path $Stage 'task_identity.py')

    if ($Role -eq 'result-composer') {
        Copy-Item -LiteralPath (Join-Path $AppRoot 'decision_reports.py') -Destination (Join-Path $Stage 'decision_reports.py')
        Copy-Item -LiteralPath (Join-Path $AppRoot 'result_composer_agent.py') -Destination (Join-Path $Stage 'result_composer_agent.py')
        Copy-Item -LiteralPath (Join-Path $AppRoot 'profile_registry.py') -Destination (Join-Path $Stage 'profile_registry.py')
        Copy-Item -LiteralPath (Join-Path $AppRoot 'protocol_v02.py') -Destination (Join-Path $Stage 'protocol_v02.py')
        Copy-Item -LiteralPath (Join-Path $ProjectRoot 'agentteams\config\profile_registry_v0.2.json') -Destination (Join-Path $Stage 'profile_registry_v0.2.json')
    }
    else {
        Copy-Item -LiteralPath (Join-Path $AppRoot 'finchange_gate_core.py') -Destination (Join-Path $Stage 'finchange_gate_core.py')
        Copy-Item -LiteralPath (Join-Path $AppRoot 'bounded_worker_task.py') -Destination (Join-Path $Stage 'bounded_worker_task.py')
        # The bounded worker validates every artifact path against the exact
        # Case/Run/role task identity.  Keep this validator package-local so a
        # clean Linux runtime can rebuild/import the package without relying
        # on files from the host application tree.
        Copy-Item -LiteralPath (Join-Path $AppRoot 'context_capsule.py') -Destination (Join-Path $Stage 'context_capsule.py')
        Copy-Item -LiteralPath (Join-Path $AppRoot 'tool_gateway.py') -Destination (Join-Path $Stage 'tool_gateway.py')
        Copy-Item -LiteralPath (Join-Path $AppRoot 'semantic_contracts.json') -Destination (Join-Path $Stage 'semantic_contracts.json')

        if ($Role -eq 'downstream-impact-analyst') {
            Copy-Item -LiteralPath (Join-Path $AppRoot 'change_control.py') -Destination (Join-Path $Stage 'change_control.py')
            Copy-Item -LiteralPath (Join-Path $AppRoot 'bounded_change_task.py') -Destination (Join-Path $Stage 'bounded_change_task.py')
        }

        # Public packages are data-neutral. Financial evidence is injected as a
        # per-Run sealed Context Slice; rights-unconfirmed reference snapshots
        # are never copied into deployable Worker ZIPs.
        $DataRoot = Join-Path $Stage 'data'
        New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    }

    if ($Role -in @('evidence-investigator', 'research-context-analyst')) {
        $ResearchModule = Join-Path $Stage 'research_data'
        New-Item -ItemType Directory -Path $ResearchModule -Force | Out-Null
        foreach ($ResearchFile in @('__init__.py', 'core.py', 'investigator.py')) {
            Copy-Item -LiteralPath (Join-Path $AppRoot "research_data\$ResearchFile") -Destination (Join-Path $ResearchModule $ResearchFile)
        }
        Copy-Item -LiteralPath (Join-Path $AppRoot 'research_data\config') -Destination (Join-Path $ResearchModule 'config') -Recurse
        # Research evidence is likewise supplied by the current EvidenceBundle.
        # Provider registry/schema code is packaged, cached provider responses
        # are deliberately excluded from the public distribution.
    }

    if ($Role -in $RuntimeSkillRoles) {
        $SourceRuntimeManifest = Join-Path $AppRoot "runtime-skill-manifests\$Role.json"
        if (-not (Test-Path -LiteralPath $SourceRuntimeManifest)) {
            throw "Missing frozen runtime Skill manifest: $SourceRuntimeManifest"
        }
        & python $RuntimeManifestBuilderPath $SourceRuntimeManifest $Stage
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to generate package-local runtime Skill manifest for $Role"
        }
    }

    Remove-Item -LiteralPath (Join-Path $Stage 'skills.map.json') -Force
    $ZipPath = Join-Path $PackageOutput "$Role.zip"
    New-AgentPackageZip -SourceDirectory $Stage -DestinationPath $ZipPath
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
    $Index += [ordered]@{
        role = $Role
        file = "packages/$Role.zip"
        bytes = (Get-Item -LiteralPath $ZipPath).Length
        sha256 = $Hash
        skills = @($Map.skills)
    }
}

$IndexObject = [ordered]@{
    generated_at_utc = [DateTime]::UtcNow.ToString('o')
    contains_api_credentials = $false
    packages = $Index
}
$IndexObject | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $BuildRoot 'package-index.json')
if (Test-Path -LiteralPath $StageRoot) {
    $ResolvedStage = (Resolve-Path -LiteralPath $StageRoot).Path
    if (-not $ResolvedStage.StartsWith($BuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean staging path outside the AgentTeams build directory: $ResolvedStage"
    }
    Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
}
if (Test-Path -LiteralPath $RuntimeManifestBuilderPath) {
    Remove-Item -LiteralPath $RuntimeManifestBuilderPath -Force
}
Write-Host "Built $($Roles.Count) AgentTeams worker packages at $PackageOutput"
