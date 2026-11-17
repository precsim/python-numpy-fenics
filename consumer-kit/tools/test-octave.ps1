param(
    [Parameter(Mandatory = $true)]
    [string] $PackageRoot,
    [string] $Octave = "octave-cli.exe"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $PackageRoot).Path
$sourceSmoke = Join-Path $root "host\mock_smoke.m"
$sourceMex = Join-Path $root "bridge.mex"
if (-not (Test-Path -LiteralPath $sourceSmoke -PathType Leaf)) {
    throw "missing Octave smoke file: $sourceSmoke"
}
if (-not (Test-Path -LiteralPath $sourceMex -PathType Leaf)) {
    throw "missing reference bridge MEX: $sourceMex"
}

$poison = Join-Path ([IO.Path]::GetTempPath()) ("reference bridge poison " + [Guid]::NewGuid().ToString("N"))
$relocationParent = Join-Path ([IO.Path]::GetTempPath()) ("reference bridge relocation " + [Guid]::NewGuid().ToString("N"))
$relocatedRoot = Join-Path $relocationParent "package with spaces"
New-Item -ItemType Directory -Force -Path (Join-Path $poison "python"), (Join-Path $poison "home"), (Join-Path $poison "user"), (Join-Path $poison "jit cache") | Out-Null
Set-Content -LiteralPath (Join-Path $poison "python\numpy.py") -Value 'raise RuntimeError("ambient numpy must not load")' -Encoding Ascii
New-Item -ItemType Directory -Force -Path $relocationParent | Out-Null
Copy-Item -LiteralPath $root -Destination $relocatedRoot -Recurse -Force

$relocatedMex = Join-Path $relocatedRoot "bridge.mex"
if (-not (Test-Path -LiteralPath $relocatedMex -PathType Leaf)) {
    throw "relocated reference bridge MEX is missing: $relocatedMex"
}
$sourceHash = (Get-FileHash -LiteralPath $sourceMex -Algorithm SHA256).Hash
$relocatedHash = (Get-FileHash -LiteralPath $relocatedMex -Algorithm SHA256).Hash
if ($sourceHash -ne $relocatedHash) {
    throw "relocating the test layout changed bridge.mex bytes"
}
$smoke = Join-Path $relocatedRoot "host\mock_smoke.m"

$saved = @{}
foreach ($name in @("REFERENCE_BRIDGE_ROOT","REFERENCE_BRIDGE_FRESH_ONLY","PYTHONHOME","PYTHONPATH","PYTHONUSERBASE","PYTHONDONTWRITEBYTECODE","PYTHONNOUSERSITE","XDG_CACHE_HOME")) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

try {
    $env:REFERENCE_BRIDGE_ROOT = $relocatedRoot
    $env:PYTHONHOME = Join-Path $poison "home"
    $env:PYTHONPATH = Join-Path $poison "python"
    $env:PYTHONUSERBASE = Join-Path $poison "user"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONNOUSERSITE = "1"
    $env:XDG_CACHE_HOME = Join-Path $poison "jit cache"

    foreach ($fresh in @("0", "1")) {
        $env:REFERENCE_BRIDGE_FRESH_ONLY = $fresh
        $lines = @(& $Octave --quiet --no-gui --no-init-file $smoke 2>&1)
        $lines | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) {
            throw "Octave reference bridge smoke failed with exit code $LASTEXITCODE"
        }
        if (-not ($lines -contains "reference_bridge_result=pass")) {
            throw "Octave reference bridge smoke did not emit the pass marker"
        }
    }
    Write-Host "reference_bridge_relocation=pass"
    Write-Host "reference_bridge_fresh_process=pass"
}
finally {
    foreach ($name in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
    }
    Remove-Item -LiteralPath $poison -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $relocationParent -Recurse -Force -ErrorAction SilentlyContinue
}
