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
$ExpectedTclTk = '8.6.18'
$ExpectedCpythonSourceSha256 = '9e4996c19ae5a3df31f10c37723661f71d3455d73bd005f79df7098e5cc13192'
$ExpectedTclSourceSha256 = '14f9af32b1767ff718477a8f974ad03c34341097e6b43f4ce54644ee974e268e'
$ExpectedTkSourceSha256 = '95cd528a80f5e4bdb557af9b14a7197d6860793a3894e25e7c9fad2ed05d4c3c'
$ExpectedCpythonSourceUrl = 'https://www.python.org/ftp/python/3.14.10/Python-3.14.10.tgz'
$ExpectedTclSourceUrl = 'https://prdownloads.sourceforge.net/tcl/tcl8.6.18-src.tar.gz'
$ExpectedTkSourceUrl = 'https://prdownloads.sourceforge.net/tcl/tk8.6.18-src.tar.gz'
$root = (Resolve-Path -LiteralPath $Runtime -ErrorAction Stop).Path
$manifestPath = Join-Path $root 'runtime-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.format_version -ne 1 -or
    $manifest.platform -ne 'win-x64' -or
    $manifest.python_abi -ne 'cp314' -or
    $manifest.profile -ne $RuntimeProfile -or
    $manifest.producer.repository -ne 'precsim/python-numpy-fenics') {
    throw 'runtime manifest identity is invalid'
}

$tclTk = $manifest.tcl_tk
if (-not $tclTk -or
    [string]$tclTk.version -ne $ExpectedTclTk -or
    [string]$tclTk.ownership -ne 'producer' -or
    [string]$tclTk.source_lock.filename -ne 'tcltk-8.6.18.json' -or
    [string]$tclTk.sources.tcl.filename -ne 'tcl8.6.18-src.tar.gz' -or
    [string]$tclTk.sources.tcl.url -ne $ExpectedTclSourceUrl -or
    [string]$tclTk.sources.tcl.sha256 -ne $ExpectedTclSourceSha256 -or
    [string]$tclTk.sources.tk.filename -ne 'tk8.6.18-src.tar.gz' -or
    [string]$tclTk.sources.tk.url -ne $ExpectedTkSourceUrl -or
    [string]$tclTk.sources.tk.sha256 -ne $ExpectedTkSourceSha256 -or
    [string]$tclTk.providers.tcl.path -ne 'DLLs/tcl86t.dll' -or
    [string]$tclTk.providers.tk.path -ne 'DLLs/tk86t.dll' -or
    [string]$tclTk.tkinter_rebuild.path -ne 'DLLs/_tkinter.pyd' -or
    [string]$tclTk.tkinter_rebuild.cpython_source.filename -ne 'Python-3.14.10.tgz' -or
    [string]$tclTk.tkinter_rebuild.cpython_source.url -ne $ExpectedCpythonSourceUrl -or
    [string]$tclTk.tkinter_rebuild.cpython_source.sha256 -ne $ExpectedCpythonSourceSha256 -or
    [string]$tclTk.tkinter_rebuild.build.project -ne 'PCbuild/_tkinter.vcxproj' -or
    [string]$tclTk.tkinter_rebuild.build.configuration -ne 'Release' -or
    [string]$tclTk.tkinter_rebuild.build.platform -ne 'x64' -or
    [bool]$tclTk.tkinter_rebuild.build.build_project_references -ne $false -or
    [string]$tclTk.tkinter_rebuild.build.tcl_version -ne '8.6.18.0' -or
    [string]$tclTk.tkinter_rebuild.build.tk_version -ne '8.6.18.0' -or
    [string]$tclTk.scripts.tcl.path -ne 'tcl/tcl8.6' -or
    [string]$tclTk.scripts.tk.path -ne 'tcl/tk8.6') {
    throw 'runtime manifest does not describe the producer Tcl/Tk 8.6.18 inputs and providers'
}
foreach ($binding in @(
    [pscustomobject]@{ Label = 'Tcl'; Path = [string]$tclTk.providers.tcl.path; Sha256 = [string]$tclTk.providers.tcl.sha256 },
    [pscustomobject]@{ Label = 'Tk'; Path = [string]$tclTk.providers.tk.path; Sha256 = [string]$tclTk.providers.tk.sha256 },
    [pscustomobject]@{ Label = '_tkinter'; Path = [string]$tclTk.tkinter_rebuild.path; Sha256 = [string]$tclTk.tkinter_rebuild.sha256 },
    [pscustomobject]@{ Label = 'Tcl init'; Path = 'tcl/tcl8.6/init.tcl'; Sha256 = [string]$tclTk.scripts.tcl.init_sha256 },
    [pscustomobject]@{ Label = 'Tk init'; Path = 'tcl/tk8.6/tk.tcl'; Sha256 = [string]$tclTk.scripts.tk.init_sha256 }
)) {
    $path = Join-Path $root ($binding.Path -replace '/', '\')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "$($binding.Label) runtime file is missing: $($binding.Path)"
    }
    $sha = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sha -ne $binding.Sha256) {
        throw "$($binding.Label) runtime file differs from its Tcl/Tk manifest binding"
    }
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
if (Get-ChildItem -LiteralPath $root -Recurse -Force | Where-Object { $_.Name -match '(?i)^(tcl|tk)9' } | Select-Object -First 1) {
    throw 'producer Tcl/Tk 8.6.18 runtime still contains inherited Tcl/Tk 9 content'
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
import json, pathlib, platform, re, sys, _tkinter, tkinter
root = pathlib.Path(sys.argv[1]).resolve()
profile = sys.argv[2]
if pathlib.Path(sys.prefix).resolve() != root:
    raise SystemExit(f"relocated sys.prefix mismatch: {sys.prefix} != {root}")
if platform.python_implementation() != "CPython" or platform.python_version() != "3.14.10":
    raise SystemExit("unexpected CPython identity")
if str(_tkinter.TCL_VERSION) != "8.6" or str(_tkinter.TK_VERSION) != "8.6":
    raise SystemExit("unexpected _tkinter Tcl/Tk API version")
tkinter_path = pathlib.Path(_tkinter.__file__).resolve()
if tkinter_path != (root / "DLLs/_tkinter.pyd").resolve():
    raise SystemExit(f"_tkinter escaped staged runtime: {tkinter_path}")
interp = tkinter.Tcl()
tcl_version = interp.eval("info patchlevel")
if tcl_version != "8.6.18":
    raise SystemExit(f"unexpected Tcl patchlevel: {tcl_version}")
tcl_library = pathlib.Path(interp.eval("info library")).resolve()
expected_tcl_library = (root / "tcl/tcl8.6").resolve()
if tcl_library != expected_tcl_library:
    raise SystemExit(f"Tcl scripts escaped runtime: {tcl_library} != {expected_tcl_library}")
tk_text = (root / "tcl/tk8.6/tk.tcl").read_text(encoding="utf-8", errors="strict")
tk_versions = re.findall(r"^package\s+(?:require\s+-exact|provide)\s+Tk\s+([^\s]+)\s*$", tk_text, re.MULTILINE)
tk_versions = list(dict.fromkeys(tk_versions))
if tk_versions != ["8.6.18"]:
    raise SystemExit(f"unexpected Tk patchlevel: {tk_versions!r}")
window = tkinter.Tk()
try:
    window.withdraw()
    tk_runtime_version = str(window.tk.call("set", "tk_patchLevel"))
    tk_library = pathlib.Path(window.tk.eval("set tk_library")).resolve()
finally:
    window.destroy()
if tk_runtime_version != "8.6.18":
    raise SystemExit(f"unexpected running Tk patchlevel: {tk_runtime_version}")
expected_tk_library = (root / "tcl/tk8.6").resolve()
if tk_library != expected_tk_library:
    raise SystemExit(f"Tk scripts escaped runtime: {tk_library} != {expected_tk_library}")
result = {"python": platform.python_version(), "tcl": tcl_version, "tk": tk_runtime_version}
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
