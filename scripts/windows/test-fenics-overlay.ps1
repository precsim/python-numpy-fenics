# Validate the exact FEniCS overlay against the qualified public NumPy runtime.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Zip,
    [Parameter(Mandatory = $true)]
    [string]$BaseRuntime,
    [Parameter(Mandatory = $true)]
    [string]$BaseQualification,
    [Parameter(Mandatory = $true)]
    [string]$BaseArchive,
    [Parameter(Mandatory = $true)]
    [string]$BaseReleaseTag,
    [Parameter(Mandatory = $true)]
    [string]$BaseReleaseLock
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$utf8NoBom = [Text.UTF8Encoding]::new($false)

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$zipPath = (Resolve-Path -LiteralPath $Zip -ErrorAction Stop).Path
$baseRuntimePath = (Resolve-Path -LiteralPath $BaseRuntime -ErrorAction Stop).Path
$baseQualificationPath = (Resolve-Path -LiteralPath $BaseQualification -ErrorAction Stop).Path
$baseArchivePath = (Resolve-Path -LiteralPath $BaseArchive -ErrorAction Stop).Path
$baseReleaseLockPath = (Resolve-Path -LiteralPath $BaseReleaseLock -ErrorAction Stop).Path
$baseManifestPath = Join-Path $baseRuntimePath "runtime-manifest.json"
if (-not (Test-Path -LiteralPath $baseManifestPath -PathType Leaf)) {
    throw "Qualified NumPy base runtime manifest is missing: $baseManifestPath"
}
$baseManifest = Get-Content -LiteralPath $baseManifestPath -Raw | ConvertFrom-Json
$baseManifestHash = (Get-FileHash -LiteralPath $baseManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$baseQualificationRecord = Get-Content -LiteralPath $baseQualificationPath -Raw | ConvertFrom-Json
$baseQualificationHash = (Get-FileHash -LiteralPath $baseQualificationPath -Algorithm SHA256).Hash.ToLowerInvariant()
$baseArchiveHash = (Get-FileHash -LiteralPath $baseArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$baseReleaseLockHash = (Get-FileHash -LiteralPath $baseReleaseLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
$baseReleaseLockRecord = Get-Content -LiteralPath $baseReleaseLockPath -Raw | ConvertFrom-Json
$basePlatformPins = $baseReleaseLockRecord.platforms.'win-x64'
$ExpectedBaseRevision = [string]$baseReleaseLockRecord.producer_revision
if ($BaseReleaseTag -notmatch '^runtime-v\d+\.\d+\.\d+-rc[1-9]\d*$' -or
    $baseReleaseLockRecord.format_version -ne 1 -or
    $baseReleaseLockRecord.repository -ne "precsim/python-numpy-fenics" -or
    $baseReleaseLockRecord.tag -ne $BaseReleaseTag -or
    $ExpectedBaseRevision -notmatch '^[0-9a-f]{40}$' -or
    $null -eq $basePlatformPins) {
    throw "Qualified NumPy base release identity is invalid"
}
if ($baseManifestHash -ne [string]$basePlatformPins.manifest_sha256 -or
    $baseQualificationHash -ne [string]$basePlatformPins.qualification_sha256 -or
    $baseArchiveHash -ne [string]$basePlatformPins.archive_sha256) {
    throw "Qualified NumPy base files do not match the immutable release lock"
}
if ($baseManifest.format_version -ne 1 -or
    $baseManifest.artifact -ne "python-runtime-win-x64-cp314-numpy" -or
    $baseManifest.platform -ne "win-x64" -or
    $baseManifest.python_abi -ne "cp314" -or
    $baseManifest.profile -ne "numpy" -or
    $baseManifest.python.implementation -ne "CPython" -or
    $baseManifest.python.version -ne "3.14.10" -or
    $baseManifest.tcl_tk.version -ne "8.6.18" -or
    $baseManifest.tcl_tk.ownership -ne "producer" -or
    $baseManifest.numpy.version -ne "2.5.2" -or
    -not $baseManifest.base_core -or
    $baseManifest.producer.repository -ne "precsim/python-numpy-fenics" -or
    $baseManifest.producer.revision -ne $ExpectedBaseRevision) {
    throw "FEniCS overlay requires the qualified public win-x64 cp314 NumPy base runtime with producer Tcl/Tk 8.6.18"
}
if ($baseQualificationRecord.format_version -ne 1 -or
    $baseQualificationRecord.producer_repository -ne "precsim/python-numpy-fenics" -or
    $baseQualificationRecord.producer_revision -ne $ExpectedBaseRevision -or
    $baseQualificationRecord.platform -ne "win-x64" -or
    $baseQualificationRecord.profile -ne "numpy" -or
    $baseQualificationRecord.artifact -ne [IO.Path]::GetFileName($baseArchivePath) -or
    ([string]$baseQualificationRecord.artifact_sha256).ToLowerInvariant() -ne $baseArchiveHash) {
    throw "Qualified NumPy base qualification/archive identity is invalid"
}

$workRoot = Join-Path ([IO.Path]::GetTempPath()) ("fenics embedded runtime test " + [guid]::NewGuid().ToString("N"))
$runtimeRoot = Join-Path $workRoot "runtime with spaces"
$poisonRoot = Join-Path $workRoot "ambient python poison"
$cacheRoot = Join-Path $workRoot "jit cache with spaces"
New-Item -ItemType Directory -Force -Path $runtimeRoot,$poisonRoot,$cacheRoot | Out-Null

try {
    Copy-Item -Path (Join-Path $baseRuntimePath "*") -Destination $runtimeRoot -Recurse
    $sitePackages = Join-Path $runtimeRoot "Lib\site-packages"
    if (-not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
        throw "Qualified NumPy base runtime site-packages is missing"
    }

    function Get-NumpyHashMap {
        param([Parameter(Mandatory = $true)][string]$SitePackages)
        $roots = @(
            (Join-Path $SitePackages "numpy")
            (Join-Path $SitePackages "numpy.libs")
            (Join-Path $SitePackages "numpy-2.5.2.dist-info")
        )
        $map = [ordered]@{}
        foreach ($root in $roots) {
            if (-not (Test-Path -LiteralPath $root)) { continue }
            Get-ChildItem -LiteralPath $root -File -Recurse -Force |
                Sort-Object FullName |
                ForEach-Object {
                    $relative = $_.FullName.Substring($SitePackages.Length).TrimStart("\", "/").Replace("\", "/")
                    $map[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
        }
        return $map
    }

    $numpyBefore = Get-NumpyHashMap -SitePackages $sitePackages
    if ($numpyBefore.Count -eq 0) {
        throw "Qualified NumPy base runtime contains no NumPy distribution files"
    }
    $treeHasher = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
    try {
        foreach ($relative in @($numpyBefore.Keys | Sort-Object)) {
            $treeHasher.AppendData($utf8NoBom.GetBytes("F`0$relative`0$($numpyBefore[$relative])`n"))
        }
        $baseTreeHash = ([BitConverter]::ToString($treeHasher.GetHashAndReset())).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $treeHasher.Dispose()
    }

    Expand-Archive -LiteralPath $zipPath -DestinationPath $runtimeRoot

    $manifestPath = Join-Path $runtimeRoot "fenics-embed-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Embedded-runtime manifest is missing after extraction"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format_version -ne 1 -or
        $manifest.artifact -ne "fenics-embed-runtime-win-x64-cp314" -or
        $manifest.platform -ne "win-x64" -or
        $manifest.python_abi -ne "cp314" -or
        $manifest.python.version -ne "3.14.10") {
        throw "Embedded-runtime manifest identity is invalid"
    }
    if ($manifest.numpy.version -ne "2.5.2" -or
        $manifest.numpy.base_release_tag -ne $BaseReleaseTag -or
        $manifest.numpy.base_producer_revision -ne $ExpectedBaseRevision -or
        $manifest.numpy.base_release_lock_sha256 -ne $baseReleaseLockHash -or
        $manifest.numpy.base_archive_sha256 -ne $baseArchiveHash -or
        $manifest.numpy.base_qualification_sha256 -ne $baseQualificationHash -or
        $manifest.numpy.base_manifest_sha256 -ne $baseManifestHash -or
        $manifest.numpy.base_tree_sha256 -ne $baseTreeHash -or
        $manifest.numpy.base_entry_count -ne $numpyBefore.Count -or
        $manifest.tcl_tk.version -ne "8.6.18" -or
        $manifest.tcl_tk.ownership -ne "producer" -or
        $manifest.tcl_tk.providers.tcl.path -ne $baseManifest.tcl_tk.providers.tcl.path -or
        $manifest.tcl_tk.providers.tcl.sha256 -ne $baseManifest.tcl_tk.providers.tcl.sha256 -or
        $manifest.tcl_tk.providers.tk.path -ne $baseManifest.tcl_tk.providers.tk.path -or
        $manifest.tcl_tk.providers.tk.sha256 -ne $baseManifest.tcl_tk.providers.tk.sha256 -or
        $manifest.tcl_tk.tkinter_rebuild.path -ne $baseManifest.tcl_tk.tkinter_rebuild.path -or
        $manifest.tcl_tk.tkinter_rebuild.sha256 -ne $baseManifest.tcl_tk.tkinter_rebuild.sha256 -or
        $manifest.layering.base_profile -ne "numpy" -or
        $manifest.layering.producer_python_included -ne $false -or
        $manifest.layering.producer_numpy_included -ne $false -or
        $manifest.layering.producer_tcl_tk_included -ne $false -or
        $manifest.jit.backend -ne "micro-clang" -or
        $manifest.jit.backend_version -ne "20260826") {
        throw "Embedded-runtime manifest does not describe the qualified NumPy/micro-Clang runtime"
    }

    foreach ($entry in @($manifest.files)) {
        if ([string]$entry.path -match '^(?i)Lib/site-packages/(numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)') {
            throw "FEnICS overlay manifest illegally targets the base NumPy distribution: $($entry.path)"
        }
        if (@($entry.owners) -contains "tcl" -or @($entry.owners) -contains "tk") {
            throw "FEniCS overlay manifest illegally includes source-prefix Tcl/Tk ownership: $($entry.path)"
        }
        $path = Join-Path $runtimeRoot ([string]$entry.path -replace '/', '\')
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Manifest file is missing after extraction: $($entry.path)"
        }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne [string]$entry.sha256) {
            throw "Manifest hash mismatch for $($entry.path)"
        }
    }
    $numpyAfter = Get-NumpyHashMap -SitePackages $sitePackages
    $beforeJson = $numpyBefore | ConvertTo-Json -Compress
    $afterJson = $numpyAfter | ConvertTo-Json -Compress
    if ($beforeJson -ne $afterJson) {
        throw "FEnICS overlay changed the qualified NumPy base distribution"
    }

    [IO.File]::WriteAllText(
        (Join-Path $poisonRoot "numpy.py"),
        "raise RuntimeError('ambient numpy imported')" + [Environment]::NewLine,
        $utf8NoBom
    )
    [IO.File]::WriteAllText(
        (Join-Path $poisonRoot "dolfinx.py"),
        "raise RuntimeError('ambient dolfinx imported')" + [Environment]::NewLine,
        $utf8NoBom
    )

    $runtimePython = Join-Path $runtimeRoot "python.exe"
    if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        throw "Relocated runtime is missing python.exe"
    }

    $saved = @{}
    $names = @(
        "PATH","PYTHONPATH","PYTHONUSERBASE","PYTHONHOME","FENICS_JIT_ROOT","FENICS_JIT_COMPILER",
        "PETSC_DIR","XDG_CACHE_HOME","FENICS_JIT_VERBOSE","CC","CXX","CPP","LD","LDSHARED","INCLUDE","LIB","LIBPATH",
        "LIBRARY_PATH","CPATH","C_INCLUDE_PATH","CPLUS_INCLUDE_PATH","COMPILER_PATH","GCC_EXEC_PREFIX",
        "VSINSTALLDIR","VCINSTALLDIR","VCToolsInstallDir","WindowsSdkDir","WindowsSDKVersion",
        "UniversalCRTSdkDir","UCRTVersion","DISTUTILS_USE_SDK","MSSdk"
    )
    foreach ($name in $names) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }

    try {
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
        $env:PYTHONPATH = $poisonRoot
        $env:PYTHONUSERBASE = $poisonRoot
        Remove-Item Env:PYTHONHOME,Env:FENICS_JIT_ROOT,Env:FENICS_JIT_COMPILER,Env:PETSC_DIR -ErrorAction SilentlyContinue
        $env:XDG_CACHE_HOME = $cacheRoot
        $env:FENICS_JIT_VERBOSE = "1"
        foreach ($name in @(
            "CC","CXX","CPP","LD","LDSHARED","INCLUDE","LIB","LIBPATH","LIBRARY_PATH","CPATH",
            "C_INCLUDE_PATH","CPLUS_INCLUDE_PATH","COMPILER_PATH","GCC_EXEC_PREFIX","VSINSTALLDIR",
            "VCINSTALLDIR","VCToolsInstallDir","WindowsSdkDir","WindowsSDKVersion","UniversalCRTSdkDir",
            "UCRTVersion","DISTUTILS_USE_SDK","MSSdk"
        )) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
        Get-ChildItem Env: |
            Where-Object { $_.Name -like "VSCMD_*" } |
            ForEach-Object { Remove-Item "Env:$($_.Name)" -ErrorAction SilentlyContinue }

        $importProbe = @'
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
assert Path(sys.prefix).resolve() == root, (sys.prefix, root)
import fenics_embed_runtime
assert fenics_embed_runtime.activate() == root
import _tkinter, tkinter
import numpy, dolfinx, petsc4py
from mpi4py import MPI
from dolfinx import mesh
assert numpy.__version__ == "2.5.2"
assert str(_tkinter.TCL_VERSION) == "8.6"
assert str(_tkinter.TK_VERSION) == "8.6"
assert tkinter.Tcl().eval("info patchlevel") == "8.6.18"
assert Path(numpy.__file__).resolve().is_relative_to(root / "Lib" / "site-packages")
assert Path(dolfinx.__file__).resolve().is_relative_to(root / "Lib" / "site-packages")
assert Path(petsc4py.__file__).resolve().is_relative_to(root / "Lib" / "site-packages")
assert Path(petsc4py.get_config()["PETSC_DIR"]).resolve() == root / "Library"
mesh.create_unit_square(MPI.COMM_SELF, 4, 4)
print("fenics_embed_import=pass")
'@
        $importProbePath = Join-Path $workRoot "import-probe.py"
        [IO.File]::WriteAllText(
            $importProbePath,
            $importProbe + [Environment]::NewLine,
            $utf8NoBom
        )
        & $runtimePython -I $importProbePath $runtimeRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Relocated FEniCS import/mesh probe failed"
        }

        $poisson = Join-Path $repoRoot "tests\fenics\test_poisson.py"
        $solveProbe = @'
from pathlib import Path
import sys
import fenics_embed_runtime
fenics_embed_runtime.activate()
script = Path(sys.argv[1]).resolve()
code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
exec(code, {"__name__": "__main__", "__file__": str(script)})
'@
        $solveProbePath = Join-Path $workRoot "solve-probe.py"
        [IO.File]::WriteAllText(
            $solveProbePath,
            $solveProbe + [Environment]::NewLine,
            $utf8NoBom
        )
        $savedErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $solveOutput = & $runtimePython -I $solveProbePath $poisson 2>&1
            $solveStatus = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $savedErrorActionPreference
        }
        $solveOutput | ForEach-Object { Write-Host $_ }
        if ($solveStatus -ne 0) {
            throw "Fresh relocated FEniCS Poisson JIT/solve failed"
        }
        $solveText = $solveOutput -join [Environment]::NewLine
        if ($solveText -notmatch "L2 error:") {
            throw "Poisson solve did not report its numerical result"
        }
        if ($solveText -notmatch "FEniCS JIT backend:\s+micro-clang") {
            throw "Fresh JIT did not report the bundled micro-Clang backend"
        }
        $jitModules = @(Get-ChildItem -LiteralPath $cacheRoot -Filter "*.pyd" -File -Recurse -ErrorAction SilentlyContinue)
        if ($jitModules.Count -eq 0) {
            throw "Fresh FFCx JIT produced no cache .pyd modules"
        }
    }
    finally {
        foreach ($name in $names) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
        }
    }

    Write-Host "fenics_embedded_runtime_test=pass"
    Write-Host "fenics_numpy_base_identity=pass"
}
finally {
    Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
}
