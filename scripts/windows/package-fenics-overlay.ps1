# Package a relocatable FEniCS Windows runtime overlay for an embedded CPython 3.14 host.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Prefix,
    [Parameter(Mandatory = $true)]
    [string]$BaseRuntime,
    [Parameter(Mandatory = $true)]
    [string]$BaseQualification,
    [Parameter(Mandatory = $true)]
    [string]$BaseArchive,
    [Parameter(Mandatory = $true)]
    [string]$BaseReleaseTag,
    [Parameter(Mandatory = $true)]
    [string]$BaseReleaseLock,
    [string]$Output = "dist",
    [string]$Version = "0.1.0"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$utf8NoBom = [Text.UTF8Encoding]::new($false)

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Text
    )
    [IO.File]::WriteAllText([IO.Path]::GetFullPath($Path), $Text, $utf8NoBom)
}

function Get-NumpyBaseIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Runtime,
        [Parameter(Mandatory = $true)]
        [string]$Qualification,
        [Parameter(Mandatory = $true)]
        [string]$Archive,
        [Parameter(Mandatory = $true)]
        [string]$ReleaseTag,
        [Parameter(Mandatory = $true)]
        [string]$ReleaseLock,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedVersion
    )

    $runtimePath = (Resolve-Path -LiteralPath $Runtime -ErrorAction Stop).Path
    $qualificationPath = (Resolve-Path -LiteralPath $Qualification -ErrorAction Stop).Path
    $archivePath = (Resolve-Path -LiteralPath $Archive -ErrorAction Stop).Path
    $releaseLockPath = (Resolve-Path -LiteralPath $ReleaseLock -ErrorAction Stop).Path
    if ($ReleaseTag -notmatch '^runtime-v(?<version>\d+\.\d+\.\d+)-rc[1-9]\d*$' -or $Matches.version -ne $ExpectedVersion) {
        throw "Qualified NumPy base release tag is invalid for version $ExpectedVersion`: $ReleaseTag"
    }
    $releaseLockRecord = Get-Content -LiteralPath $releaseLockPath -Raw | ConvertFrom-Json
    $platformPins = $releaseLockRecord.platforms.'win-x64'
    $ExpectedRevision = [string]$releaseLockRecord.producer_revision
    if ($releaseLockRecord.format_version -ne 1 -or
        $releaseLockRecord.repository -ne "precsim/python-numpy-fenics" -or
        $releaseLockRecord.tag -ne $ReleaseTag -or
        $null -eq $platformPins) {
        throw "Qualified NumPy base release lock is invalid for win-x64"
    }
    if ($ExpectedRevision -notmatch '^[0-9a-f]{40}$') {
        throw "Qualified NumPy base revision must be a full lowercase Git SHA: $ExpectedRevision"
    }
    $manifestPath = Join-Path $runtimePath "runtime-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Qualified NumPy base runtime manifest is missing: $manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format_version -ne 1 -or
        $manifest.artifact -ne "python-runtime-win-x64-cp314-numpy" -or
        $manifest.platform -ne "win-x64" -or
        $manifest.profile -ne "numpy" -or
        $manifest.python_abi -ne "cp314" -or
        $manifest.version -ne $ExpectedVersion -or
        $manifest.python.implementation -ne "CPython" -or
        $manifest.python.version -ne "3.14.10" -or
        $manifest.tcl_tk.version -ne "8.6.18" -or
        $manifest.tcl_tk.ownership -ne "producer" -or
        $manifest.numpy.version -ne "2.5.2" -or
        -not $manifest.base_core -or
        $manifest.producer.repository -ne "precsim/python-numpy-fenics" -or
        $manifest.producer.revision -ne $ExpectedRevision) {
        throw "FEniCS overlay NumPy base runtime identity is invalid"
    }

    $qualificationRecord = Get-Content -LiteralPath $qualificationPath -Raw | ConvertFrom-Json
    $archiveSha = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $qualificationSha = (Get-FileHash -LiteralPath $qualificationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifestSha = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $releaseLockSha = (Get-FileHash -LiteralPath $releaseLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($archiveSha -ne [string]$platformPins.archive_sha256 -or
        $qualificationSha -ne [string]$platformPins.qualification_sha256 -or
        $manifestSha -ne [string]$platformPins.manifest_sha256) {
        throw "Qualified NumPy base files do not match the immutable release lock"
    }
    $requiredPassChecks = @(
        "inherited_core_identity",
        "pinned_numpy_wheel",
        "openblas",
        "blas_lapack_functional",
        "pe_native_closure",
        "relocation"
    )
    if ($qualificationRecord.format_version -ne 1 -or
        $qualificationRecord.producer_repository -ne "precsim/python-numpy-fenics" -or
        $qualificationRecord.producer_revision -ne $ExpectedRevision -or
        $qualificationRecord.platform -ne "win-x64" -or
        $qualificationRecord.profile -ne "numpy" -or
        $qualificationRecord.artifact -ne [IO.Path]::GetFileName($archivePath) -or
        ([string]$qualificationRecord.artifact_sha256).ToLowerInvariant() -ne $archiveSha -or
        $qualificationRecord.checks.private_consumer_checkout -ne "not-applicable") {
        throw "Qualified NumPy base qualification evidence is invalid"
    }
    foreach ($check in $requiredPassChecks) {
        $property = $qualificationRecord.checks.PSObject.Properties[$check]
        if ($null -eq $property -or [string]$property.Value -ne "pass") {
            throw "Qualified NumPy base qualification check is not pass: $check"
        }
    }

    $entries = @()
    $seen = @{}
    foreach ($entry in @($manifest.files)) {
        $normalized = ([string]$entry.path).Replace("\", "/")
        if (-not $normalized -or $normalized.StartsWith("/") -or $normalized -match '(^|/)\.\.(/|$)') {
            throw "Qualified NumPy base manifest contains unsafe path: $normalized"
        }
        $prefix = "Lib/site-packages/"
        if (-not $normalized.StartsWith($prefix)) {
            continue
        }
        $relative = $normalized.Substring($prefix.Length)
        if ($relative -notmatch '^(numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)') {
            continue
        }
        if ($seen.ContainsKey($relative)) {
            throw "Qualified NumPy base manifest contains duplicate NumPy path: $relative"
        }
        $seen[$relative] = $true

        $kindProperty = $entry.PSObject.Properties["kind"]
        $kind = if ($null -eq $kindProperty) { "file" } else { [string]$kindProperty.Value }
        if ($kind -eq "file") {
            $shaProperty = $entry.PSObject.Properties["sha256"]
            $sha = if ($null -eq $shaProperty) { "" } else { ([string]$shaProperty.Value).ToLowerInvariant() }
            if ($sha -notmatch '^[0-9a-f]{64}$') {
                throw "Qualified NumPy base manifest has invalid SHA-256 for $relative"
            }
            $runtimeFile = Join-Path $runtimePath ($normalized -replace '/', '\')
            if (-not (Test-Path -LiteralPath $runtimeFile -PathType Leaf)) {
                throw "Qualified NumPy base runtime is missing manifest file: $normalized"
            }
            $runtimeSha = (Get-FileHash -LiteralPath $runtimeFile -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($runtimeSha -ne $sha) {
                throw "Qualified NumPy base runtime file hash differs from manifest: $normalized"
            }
            $entries += [pscustomobject]@{ relative = $relative; record = "F`0$relative`0$sha`n" }
        } elseif ($kind -eq "symlink") {
            $targetProperty = $entry.PSObject.Properties["target"]
            $target = if ($null -eq $targetProperty) { "" } else { [string]$targetProperty.Value }
            if (-not $target) {
                throw "Qualified NumPy base manifest has invalid symlink target for $relative"
            }
            $entries += [pscustomobject]@{ relative = $relative; record = "L`0$relative`0$target`n" }
        } else {
            throw "Qualified NumPy base manifest has unsupported kind for $relative`: $kind"
        }
    }
    $entries = @($entries | Sort-Object relative)
    if ($entries.Count -eq 0) {
        throw "Qualified NumPy base manifest contains no NumPy payload"
    }

    $hasher = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
    try {
        foreach ($entry in $entries) {
            $hasher.AppendData($utf8NoBom.GetBytes([string]$entry.record))
        }
        $treeHash = ([BitConverter]::ToString($hasher.GetHashAndReset())).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $hasher.Dispose()
    }
    [pscustomobject]@{
        manifest_sha256 = $manifestSha
        tree_sha256 = $treeHash
        entry_count = $entries.Count
        tcl_tk = $manifest.tcl_tk
        release_tag = $ReleaseTag
        producer_revision = $ExpectedRevision
        release_lock_sha256 = $releaseLockSha
        archive_sha256 = $archiveSha
        qualification_sha256 = $qualificationSha
    }
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$prefixPath = (Resolve-Path -LiteralPath $Prefix -ErrorAction Stop).Path
$python = Join-Path $prefixPath "python.exe"
$condaMeta = Join-Path $prefixPath "conda-meta"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "FEniCS prefix is missing python.exe: $python"
}
if (-not (Test-Path -LiteralPath $condaMeta -PathType Container)) {
    throw "FEniCS prefix is missing conda metadata: $condaMeta"
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Embedded-runtime version must be X.Y.Z, got $Version"
}
$producerRevision = if ($env:PRODUCER_REVISION) {
    $env:PRODUCER_REVISION
} elseif ($env:GITHUB_SHA) {
    $env:GITHUB_SHA
} else {
    "local"
}
$baseIdentity = Get-NumpyBaseIdentity -Runtime $BaseRuntime -Qualification $BaseQualification -Archive $BaseArchive -ReleaseTag $BaseReleaseTag -ReleaseLock $BaseReleaseLock -ExpectedVersion $Version

$records = @(
    Get-ChildItem -LiteralPath $condaMeta -Filter "*.json" -File |
        ForEach-Object {
            $record = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
            [pscustomobject]@{
                name = [string]$record.name
                version = [string]$record.version
                build = [string]$record.build
                build_number = [int]$record.build_number
                channel = [string]$record.channel
                url = [string]$record.url
                sha256 = [string]$record.sha256
                md5 = [string]$record.md5
                files = @($record.files)
            }
        }
)
$recordByName = @{}
foreach ($record in $records) {
    $recordByName[$record.name] = $record
}

foreach ($required in @(
    "python",
    "python_abi",
    "numpy",
    "fenics-dolfinx",
    "fenics-libdolfinx",
    "fenics-jit-runtime",
    "fenics-jit-micro-clang",
    "petsc",
    "petsc4py",
    "mpi4py",
    "impi_rt",
    "hdf5",
    "fenics-basix",
    "fenics-ffcx",
    "fenics-ufl",
    "cffi",
    "setuptools"
)) {
    if (-not $recordByName.ContainsKey($required)) {
        throw "FEniCS prefix is missing required package: $required"
    }
}
foreach ($forbidden in @("fenics-jit-llvm-mingw", "fenics-jit-tinycc", "vs2022_win-64", "vswhere")) {
    if ($recordByName.ContainsKey($forbidden)) {
        throw "FEniCS embedded runtime unexpectedly contains forbidden package: $forbidden"
    }
}
if ($recordByName["python"].version -ne "3.14.10") {
    throw "FEniCS embedded runtime requires CPython 3.14.10, got $($recordByName["python"].version)"
}
if ($recordByName["python_abi"].version -ne "3.14" -or $recordByName["python_abi"].build -notmatch '(^|_)cp314($|_)') {
    throw "FEniCS embedded runtime requires the cp314 Python ABI package"
}
if ($recordByName["numpy"].version -ne "2.5.2") {
    throw "FEniCS embedded runtime is pinned to NumPy 2.5.2, got $($recordByName["numpy"].version)"
}
if ($recordByName["fenics-jit-micro-clang"].version -ne "20260826") {
    throw "FEniCS embedded runtime requires qualified micro-Clang 20260826, got $($recordByName["fenics-jit-micro-clang"].version)"
}

$probeCode = @'
import json, platform, struct, sys
import basix, dolfinx, ffcx, mpi4py, numpy, petsc4py, ufl
print(json.dumps({
    "python": platform.python_version(),
    "implementation": platform.python_implementation(),
    "pointer_bits": 8 * struct.calcsize("P"),
    "numpy": numpy.__version__,
    "dolfinx": dolfinx.__version__,
    "petsc4py": petsc4py.__version__,
    "mpi4py": mpi4py.__version__,
    "ffcx": ffcx.__version__,
    "basix": basix.__version__,
    "ufl": ufl.__version__,
    "prefix": sys.prefix,
}))
'@
$probePath = Join-Path ([IO.Path]::GetTempPath()) ("fenics-embed-probe-" + [guid]::NewGuid().ToString("N") + ".py")
$oldPath = $env:PATH
try {
    Write-Utf8NoBom -Path $probePath -Text ($probeCode + [Environment]::NewLine)
    $env:PATH = (Join-Path $prefixPath "Library\bin") + ";" + $prefixPath + ";" + $oldPath
    $probeText = & $python -I $probePath 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "FEniCS prefix import probe failed: $($probeText -join [Environment]::NewLine)"
    }
}
finally {
    $env:PATH = $oldPath
    Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
}
$probe = ($probeText | Select-Object -Last 1) | ConvertFrom-Json
if ($probe.implementation -ne "CPython" -or $probe.pointer_bits -ne 64 -or $probe.python -ne "3.14.10") {
    throw "FEniCS prefix does not provide 64-bit CPython 3.14.10: $($probeText -join [Environment]::NewLine)"
}
if ($probe.numpy -ne "2.5.2") {
    throw "Imported NumPy version differs from package pin: $($probe.numpy)"
}

$outputPath = if ([IO.Path]::IsPathRooted($Output)) {
    [IO.Path]::GetFullPath($Output)
} else {
    Join-Path $repoRoot $Output
}
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

$workRoot = Join-Path ([IO.Path]::GetTempPath()) ("fenics-embed-" + [guid]::NewGuid().ToString("N"))
$stageRoot = Join-Path $workRoot "fenics-embed-runtime"
New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null

$allowedPrefixes = @(
    "Lib/site-packages/",
    "Library/bin/",
    "Library/fenics-jit/",
    "Library/share/",
    "Library/etc/",
    "Library/licenses/",
    "Include/"
)

$owners = @{}
try {
    foreach ($record in $records) {
        if ($record.name -in @("python", "numpy", "tcl", "tk")) {
            continue
        }
        foreach ($rawRelative in @($record.files)) {
            if ($null -eq $rawRelative) { continue }
            $relative = ([string]$rawRelative).Replace("\", "/").TrimStart("/")
            if ($relative -match '(^|/)__pycache__/|\.(pyc|pyo)$') {
                continue
            }
            $allowed = $false
            foreach ($allowedPrefix in $allowedPrefixes) {
                if ($relative.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                    $allowed = $true
                    break
                }
            }
            if (-not $allowed) { continue }
            if ($relative -match '^(?i)Lib/site-packages/(numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)') {
                throw "Non-NumPy package unexpectedly owns a NumPy path: $relative (owner $($record.name))"
            }

            $source = Join-Path $prefixPath ($relative -replace '/', '\')
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
                throw "Package-owned runtime file is missing from prefix: $relative (owner $($record.name))"
            }
            $destination = Join-Path $stageRoot ($relative -replace '/', '\')
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null

            if (Test-Path -LiteralPath $destination -PathType Leaf) {
                $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
                $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($sourceHash -ne $destinationHash) {
                    throw "Two conda packages own different bytes at embedded runtime path: $relative"
                }
            }
            else {
                Copy-Item -LiteralPath $source -Destination $destination
            }

            if (-not $owners.ContainsKey($relative)) { $owners[$relative] = @() }
            $owners[$relative] = @($owners[$relative]) + $record.name
        }
    }

    $bootstrapSource = Join-Path $repoRoot "runtime\windows\fenics_embed_runtime.py"
    if (-not (Test-Path -LiteralPath $bootstrapSource -PathType Leaf)) {
        throw "Embedded-runtime bootstrap source is missing: $bootstrapSource"
    }
    $bootstrapDestination = Join-Path $stageRoot "Lib\site-packages\fenics_embed_runtime.py"
    Copy-Item -LiteralPath $bootstrapSource -Destination $bootstrapDestination
    $owners["Lib/site-packages/fenics_embed_runtime.py"] = @("python-numpy-fenics")

    $provenanceRoot = Join-Path $stageRoot "provenance"
    New-Item -ItemType Directory -Force -Path $provenanceRoot | Out-Null
    $packageSummary = @(
        $records |
            Sort-Object name |
            ForEach-Object {
                [ordered]@{
                    name = $_.name
                    version = $_.version
                    build = $_.build
                    build_number = $_.build_number
                    channel = $_.channel
                    url = $_.url
                    sha256 = $_.sha256
                    md5 = $_.md5
                }
            }
    )
    Write-Utf8NoBom -Path (Join-Path $provenanceRoot "conda-packages.json") -Text (($packageSummary | ConvertTo-Json -Depth 5) + [Environment]::NewLine)

    $files = @(
        Get-ChildItem -LiteralPath $stageRoot -File -Recurse |
            Sort-Object FullName |
            ForEach-Object {
                $relative = $_.FullName.Substring($stageRoot.Length).TrimStart("\", "/").Replace("\", "/")
                [ordered]@{
                    path = $relative
                    bytes = $_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                owners = if ($owners.ContainsKey($relative)) { @($owners[$relative] | Sort-Object -Unique) } else { @("python-numpy-fenics") }
                }
            }
    )
    $forbiddenPayload = @(
        $files |
            Where-Object {
                $_.path -match '^(?i)Lib/site-packages/(numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)' -or
                $_.owners -contains "python" -or
                $_.owners -contains "numpy" -or
                $_.owners -contains "tcl" -or
                $_.owners -contains "tk"
            }
    )
    if ($forbiddenPayload.Count -ne 0) {
        throw "FEniCS overlay contains producer CPython/NumPy ownership: $($forbiddenPayload[0].path)"
    }

    $manifest = [ordered]@{
        format_version = 1
        artifact = "fenics-embed-runtime-win-x64-cp314"
        version = $Version
        platform = "win-x64"
        python_abi = "cp314"
        profile = "fenics"
        producer = [ordered]@{
            repository = "precsim/python-numpy-fenics"
            revision = $producerRevision
        }
        python = [ordered]@{
            version = $probe.python
        }
        tcl_tk = $baseIdentity.tcl_tk
        numpy = [ordered]@{
            version = $probe.numpy
            base_release_tag = $baseIdentity.release_tag
            base_producer_revision = $baseIdentity.producer_revision
            base_release_lock_sha256 = $baseIdentity.release_lock_sha256
            base_archive_sha256 = $baseIdentity.archive_sha256
            base_qualification_sha256 = $baseIdentity.qualification_sha256
            base_manifest_sha256 = $baseIdentity.manifest_sha256
            base_tree_sha256 = $baseIdentity.tree_sha256
            base_entry_count = $baseIdentity.entry_count
        }
        layering = [ordered]@{
            base_profile = "numpy"
            producer_python_included = $false
            producer_numpy_included = $false
            producer_tcl_tk_included = $false
        }
        dolfinx = [ordered]@{ version = $probe.dolfinx }
        petsc4py = [ordered]@{ version = $probe.petsc4py }
        mpi4py = [ordered]@{ version = $probe.mpi4py }
        jit = [ordered]@{
            runtime_version = $recordByName["fenics-jit-runtime"].version
            backend = "micro-clang"
            backend_version = $recordByName["fenics-jit-micro-clang"].version
            backend_build = $recordByName["fenics-jit-micro-clang"].build
        }
        layout = [ordered]@{
            site_packages = "Lib/site-packages"
            native_bin = "Library/bin"
            jit_root = "Library/fenics-jit"
            python_include = "Include"
            bootstrap = "Lib/site-packages/fenics_embed_runtime.py"
        }
        packages = $packageSummary
        files = $files
    }
    $manifestPath = Join-Path $stageRoot "fenics-embed-manifest.json"
    Write-Utf8NoBom -Path $manifestPath -Text (($manifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine)

    $assetName = "fenics-embed-runtime-v$Version-win-x64-cp314.zip"
    $zipPath = Join-Path $outputPath $assetName
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $stageRoot "*") -DestinationPath $zipPath -CompressionLevel Optimal

    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = Join-Path $outputPath "SHA256SUMS-embedded-runtime.txt"
    Write-Utf8NoBom -Path $checksumPath -Text ("$zipHash  $assetName" + [Environment]::NewLine)

    Write-Host "FEniCS embedded-runtime overlay packaged."
    Write-Host "artifact=$zipPath"
    Write-Host "artifact_bytes=$((Get-Item -LiteralPath $zipPath).Length)"
    Write-Host "sha256=$zipHash"
    Write-Host "overlay_files=$($files.Count)"
}
finally {
    Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
}
