[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Runtime,
    [ValidateSet('core','numpy')]
    [string]$RuntimeProfile,
    [string]$CoreRuntime = '',
    [string]$LayerEvidence = '',
    [string]$NativeClosureOutput = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$root = (Resolve-Path -LiteralPath $Runtime -ErrorAction Stop).Path
$manifestPath = Join-Path $root 'runtime-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.format_version -ne 1 -or
    $manifest.platform -ne 'win-x64' -or
    $manifest.python_abi -ne 'cp312' -or
    $manifest.profile -ne $RuntimeProfile -or
    $manifest.producer.repository -ne 'precsim/python-numpy-fenics') {
    throw 'runtime manifest identity is invalid'
}
foreach ($entry in @($manifest.files)) {
    $path = Join-Path $root ([string]$entry.path -replace '/', '\')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "manifest file is missing: $($entry.path)"
    }
    $sha = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sha -ne [string]$entry.sha256) {
        throw "manifest hash mismatch: $($entry.path)"
    }
}
if (Get-ChildItem -LiteralPath $root -File -Recurse | Where-Object { $_.Name -match '(?i)\.mex' } | Select-Object -First 1) {
    throw 'public producer runtime must not contain a MEX binary'
}

$python = Join-Path $root 'python.exe'
if ($RuntimeProfile -eq 'numpy') {
    if (-not $CoreRuntime) {
        throw 'NumPy qualification requires -CoreRuntime for exact inherited-core verification'
    }
    $core = (Resolve-Path -LiteralPath $CoreRuntime -ErrorAction Stop).Path
    $layerEvidencePath = if ($LayerEvidence) {
        [IO.Path]::GetFullPath($LayerEvidence)
    } else {
        Join-Path $env:TEMP ('windows-core-layer-' + [guid]::NewGuid().ToString('N') + '.json')
    }
    $verifyLayer = Join-Path $PSScriptRoot 'verify_core_layer.py'
    & $python -I -S $verifyLayer --core $core --numpy $root --output $layerEvidencePath
    if ($LASTEXITCODE -ne 0) {
        throw 'relocated NumPy runtime changed qualified core bytes'
    }
    $coreIdentity = Get-Content -LiteralPath $layerEvidencePath -Raw | ConvertFrom-Json
    if (-not $manifest.base_core -or
        [string]$manifest.base_core.manifest_sha256 -ne [string]$coreIdentity.core_manifest_sha256 -or
        [string]$manifest.base_core.inherited_tree_sha256 -ne [string]$coreIdentity.inherited_tree_sha256 -or
        [int]$manifest.base_core.inherited_file_count -ne [int]$coreIdentity.inherited_file_count) {
        throw 'NumPy runtime manifest does not bind the exact qualified core identity'
    }
} elseif ($CoreRuntime) {
    throw '-CoreRuntime is valid only for the numpy profile'
}

$saved = @{}
foreach ($name in @('PYTHONPATH','PYTHONUSERBASE','PYTHONHOME','PATH')) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $poison = Join-Path $env:TEMP ('python runtime poison ' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $poison | Out-Null
    [IO.File]::WriteAllText(
        (Join-Path $poison 'numpy.py'),
        "raise RuntimeError('ambient numpy poison imported')" + [Environment]::NewLine,
        $Utf8NoBom
    )
    $env:PYTHONPATH = $poison
    $env:PYTHONUSERBASE = $poison
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"

    $probe = @'
import json, pathlib, platform, sys, tkinter
root = pathlib.Path(sys.argv[1]).resolve()
profile = sys.argv[2]
if pathlib.Path(sys.prefix).resolve() != root:
    raise SystemExit(f"relocated sys.prefix mismatch: {sys.prefix} != {root}")
if platform.python_implementation() != "CPython" or platform.python_version() != "3.12.10":
    raise SystemExit("unexpected CPython identity")
interp = tkinter.Tcl()
result = {"python": platform.python_version(), "tcl": interp.eval("info patchlevel")}
if profile == "core":
    try:
        import numpy
    except ModuleNotFoundError:
        pass
    else:
        raise SystemExit(f"core unexpectedly imports NumPy from {numpy.__file__}")
else:
    import numpy
    if numpy.__version__ != "2.5.2":
        raise SystemExit(f"unexpected NumPy version: {numpy.__version__}")
    a = numpy.array([[4.0, 1.0], [1.0, 3.0]])
    b = numpy.array([1.0, 2.0])
    x = numpy.linalg.solve(a, b)
    if not numpy.allclose(a @ x, b):
        raise SystemExit("NumPy BLAS/LAPACK functional probe failed")
    cfg = numpy.show_config(mode="dicts")
    text = json.dumps(cfg, default=str).lower()
    if "openblas" not in text or "mkl" in text:
        raise SystemExit(f"unexpected BLAS provider: {cfg!r}")
    result.update({"numpy": numpy.__version__, "blas": "openblas"})
print(json.dumps(result, sort_keys=True))
'@
    $probe | & $python -I - $root $RuntimeProfile
    if ($LASTEXITCODE -ne 0) {
        throw "runtime probe failed for $RuntimeProfile"
    }
}
finally {
    foreach ($name in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
    }
    if ($poison) {
        Remove-Item -LiteralPath $poison -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$closureOutput = if ($NativeClosureOutput) {
    [IO.Path]::GetFullPath($NativeClosureOutput)
} else {
    Join-Path $env:TEMP ("windows-pe-native-closure-$RuntimeProfile-" + [guid]::NewGuid().ToString('N') + '.json')
}
$closureScript = Join-Path $PSScriptRoot 'pe_native_closure.py'
$closureReference = Join-Path $root "provenance\pe-native-closure-$RuntimeProfile.json"
if (-not (Test-Path -LiteralPath $closureReference -PathType Leaf)) {
    throw "Packaged PE native closure evidence is missing: $closureReference"
}
& $python -I -S $closureScript --runtime $root --profile $RuntimeProfile --output $closureOutput --reference $closureReference
if ($LASTEXITCODE -ne 0) {
    throw "PE native import/provider closure failed for $RuntimeProfile"
}

Write-Host "windows_runtime_test=pass"
Write-Host "runtime_profile=$RuntimeProfile"
