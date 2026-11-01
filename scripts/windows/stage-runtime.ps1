[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [ValidateSet('core','numpy')]
    [string]$RuntimeProfile = 'core',
    [string]$NumpyWheel = '',
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [string]$Version = '0.1.0'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedPython = '3.12.10'
$ExpectedNumpy = '2.5.2'
$ExpectedWheel = 'numpy-2.5.2-cp312-cp312-win_amd64.whl'
$ExpectedWheelSha256 = '28ac63476ec7651484215ee7fa15a1f78b57c14621f01e392afe17b9a1390ce4'

$pythonPath = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
$infoScript = @'
import json, pathlib, platform, struct, sys
p = pathlib.Path(sys.prefix).resolve()
print(json.dumps({
    "implementation": platform.python_implementation(),
    "version": platform.python_version(),
    "pointer_bits": 8 * struct.calcsize("P"),
    "prefix": str(p),
}))
'@
$info = ($infoScript | & $pythonPath -I -S -) | ConvertFrom-Json
if ($info.implementation -ne 'CPython' -or $info.version -ne $ExpectedPython -or $info.pointer_bits -ne 64) {
    throw "Expected 64-bit CPython $ExpectedPython, got $($info.implementation) $($info.version)"
}

$source = (Resolve-Path -LiteralPath ([string]$info.prefix)).Path
$outputPath = [IO.Path]::GetFullPath($Output)
Remove-Item -LiteralPath $outputPath -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

foreach ($name in @('python.exe','python312.dll','python3.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt')) {
    $candidate = Join-Path $source $name
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        Copy-Item -LiteralPath $candidate -Destination (Join-Path $outputPath $name)
    }
}
foreach ($name in @('Lib','DLLs','tcl','Include','libs')) {
    $candidate = Join-Path $source $name
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        Copy-Item -LiteralPath $candidate -Destination (Join-Path $outputPath $name) -Recurse
    }
}

$sitePackages = Join-Path $outputPath 'Lib\site-packages'
Remove-Item -LiteralPath $sitePackages -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null

$numpy = $null
if ($RuntimeProfile -eq 'numpy') {
    if (-not $NumpyWheel) {
        throw 'NumPy profile requires -NumpyWheel'
    }
    $wheel = (Resolve-Path -LiteralPath $NumpyWheel -ErrorAction Stop).Path
    if ([IO.Path]::GetFileName($wheel) -ne $ExpectedWheel) {
        throw "Unexpected NumPy wheel: $([IO.Path]::GetFileName($wheel))"
    }
    $wheelSha = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($wheelSha -ne $ExpectedWheelSha256) {
        throw "NumPy wheel SHA-256 mismatch: $wheelSha"
    }
    $extractScript = @'
import base64, csv, hashlib, io, pathlib, sys, zipfile
wheel = pathlib.Path(sys.argv[1]).resolve()
target = pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(wheel) as zf:
    names = [name for name in zf.namelist() if not name.endswith("/")]
    if len(names) != len(set(names)):
        raise SystemExit("wheel contains duplicate paths")
    records = [name for name in names if name == "numpy-2.5.2.dist-info/RECORD"]
    if len(records) != 1:
        raise SystemExit(f"unexpected RECORD identity: {records}")
    rows = {}
    for row in csv.reader(io.TextIOWrapper(zf.open(records[0]), encoding="utf-8", newline="")):
        if len(row) != 3:
            raise SystemExit(f"invalid RECORD row: {row!r}")
        rows[row[0]] = (row[1], row[2])
    if set(rows) != set(names):
        raise SystemExit("wheel RECORD/archive path mismatch")
    for name in names:
        pure = pathlib.PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise SystemExit(f"unsafe wheel path: {name}")
        if ".data" in pure.parts:
            raise SystemExit(f"unsupported wheel .data path: {name}")
        data = zf.read(name)
        encoded, size = rows[name]
        if name.endswith("/RECORD"):
            if encoded or size:
                raise SystemExit("RECORD self-entry must omit hash/size")
        else:
            if not encoded.startswith("sha256=") or int(size) != len(data):
                raise SystemExit(f"invalid RECORD identity for {name}")
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            if digest != encoded.split("=", 1)[1]:
                raise SystemExit(f"RECORD hash mismatch: {name}")
        destination = target.joinpath(*pure.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
'@
    $extractScript | & $pythonPath -I -S - $wheel $sitePackages
    if ($LASTEXITCODE -ne 0) {
        throw 'NumPy wheel extraction/RECORD verification failed'
    }
    $probeScript = @'
import json, numpy
cfg = numpy.show_config(mode="dicts")
text = json.dumps(cfg, default=str).lower()
if numpy.__version__ != "2.5.2":
    raise SystemExit(f"unexpected NumPy {numpy.__version__}")
if "openblas" not in text or "mkl" in text:
    raise SystemExit(f"unexpected BLAS provider: {cfg!r}")
print(json.dumps({"version": numpy.__version__, "blas": "openblas"}))
'@
    $probe = ($probeScript | & (Join-Path $outputPath 'python.exe') -I -) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw 'Staged NumPy probe failed'
    }
    $numpy = [ordered]@{
        version = $probe.version
        wheel = $ExpectedWheel
        wheel_sha256 = $ExpectedWheelSha256
        blas = $probe.blas
    }
} elseif ($NumpyWheel) {
    throw '-NumpyWheel is valid only for the numpy profile'
}

Set-Content -LiteralPath (Join-Path $outputPath 'RUNTIME_PROFILE') -Value $RuntimeProfile -Encoding utf8NoBOM -NoNewline

$pythonHash = (Get-FileHash -LiteralPath (Join-Path $outputPath 'python.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
$dllHash = (Get-FileHash -LiteralPath (Join-Path $outputPath 'python312.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
$files = @(
    Get-ChildItem -LiteralPath $outputPath -File -Recurse -Force |
        Where-Object { $_.Name -ne 'runtime-manifest.json' } |
        Sort-Object FullName |
        ForEach-Object {
            [ordered]@{
                path = [IO.Path]::GetRelativePath($outputPath, $_.FullName).Replace('\','/')
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                owners = @('python-numpy-fenics')
            }
        }
)
$manifest = [ordered]@{
    format_version = 1
    artifact = "python-runtime-win-x64-cp312-$RuntimeProfile"
    version = $Version
    platform = 'win-x64'
    python_abi = 'cp312'
    profile = $RuntimeProfile
    producer = [ordered]@{
        repository = 'precsim/python-numpy-fenics'
        revision = if ($env:PRODUCER_REVISION) {
            $env:PRODUCER_REVISION
        } elseif ($env:GITHUB_SHA) {
            $env:GITHUB_SHA
        } else {
            'local'
        }
    }
    python = [ordered]@{
        implementation = 'CPython'
        version = $ExpectedPython
        executable_sha256 = $pythonHash
        dll_sha256 = $dllHash
    }
    numpy = $numpy
    files = $files
}
$manifest | ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath (Join-Path $outputPath 'runtime-manifest.json') -Encoding utf8NoBOM

Write-Host "runtime_profile=$RuntimeProfile"
Write-Host "runtime=$outputPath"
Write-Host "runtime_file_count=$($files.Count)"
