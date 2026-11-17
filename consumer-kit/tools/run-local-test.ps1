[CmdletBinding()]
param(
    [string]$Octave,
    [string]$Python
)

$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Python) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $Python = $PythonCommand.Source
    } else {
        $PyCommand = Get-Command py -ErrorAction SilentlyContinue
        if ($PyCommand) {
            $Python = $PyCommand.Source
        } else {
            throw "reference_bridge_error=bootstrap Python 3 was not found; pass -Python"
        }
    }
}

$PythonArgs = @()
if ([System.IO.Path]::GetFileNameWithoutExtension($Python) -eq "py") {
    $PythonArgs += "-3"
}
$PythonArgs += @("-B", (Join-Path $PackageRoot "verify-package.py"), "--root", $PackageRoot)
& $Python @PythonArgs
if ($LASTEXITCODE -ne 0) {
    throw "reference_bridge_error=package verification failed"
}

if (-not $Octave) {
    $OctaveCommand = Get-Command octave-cli -ErrorAction SilentlyContinue
    if (-not $OctaveCommand) {
        $OctaveCommand = Get-Command octave -ErrorAction SilentlyContinue
    }
    if (-not $OctaveCommand) {
        throw "reference_bridge_error=GNU Octave was not found; pass -Octave"
    }
    $Octave = $OctaveCommand.Source
}
if (Test-Path -LiteralPath $Octave -PathType Leaf) {
    $Octave = (Resolve-Path -LiteralPath $Octave).Path
} else {
    $OctaveCommand = Get-Command $Octave -ErrorAction SilentlyContinue
    if (-not $OctaveCommand) {
        throw "reference_bridge_error=Octave executable does not exist: $Octave"
    }
    $Octave = $OctaveCommand.Source
}

$SavedEnvironment = @{}
$Names = @(
    "REFERENCE_BRIDGE_ROOT", "REFERENCE_BRIDGE_FRESH_ONLY", "PYTHONHOME",
    "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONDONTWRITEBYTECODE",
    "PYTHONNOUSERSITE", "XDG_CACHE_HOME"
)
foreach ($Name in $Names) {
    $SavedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
}

$Poison = Join-Path ([IO.Path]::GetTempPath()) ("reference bridge poison " + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path (Join-Path $Poison "python"), (Join-Path $Poison "home"), (Join-Path $Poison "user"), (Join-Path $Poison "jit cache") | Out-Null
Set-Content -LiteralPath (Join-Path $Poison "python\numpy.py") -Value 'raise RuntimeError("ambient numpy must not load")' -Encoding UTF8

try {
    $env:REFERENCE_BRIDGE_ROOT = $PackageRoot
    $env:PYTHONHOME = Join-Path $Poison "home"
    $env:PYTHONPATH = Join-Path $Poison "python"
    $env:PYTHONSTARTUP = ""
    $env:PYTHONUSERBASE = Join-Path $Poison "user"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONNOUSERSITE = "1"
    $env:XDG_CACHE_HOME = Join-Path $Poison "jit cache"

    $Smoke = Join-Path $PackageRoot "host\mock_smoke.m"
    foreach ($Fresh in @("0", "1")) {
        $env:REFERENCE_BRIDGE_FRESH_ONLY = $Fresh
        $Output = @(& $Octave --quiet --no-gui --no-init-file $Smoke 2>&1)
        $ExitCode = $LASTEXITCODE
        $Output | ForEach-Object { Write-Output $_ }
        if ($ExitCode -ne 0) {
            throw "reference_bridge_error=Octave smoke test failed"
        }
        if (-not ($Output -contains "reference_bridge_result=pass")) {
            throw "reference_bridge_error=Octave smoke test did not emit reference_bridge_result=pass"
        }
    }
    Write-Output "reference_bridge_fresh_process=pass"
} finally {
    foreach ($Name in $Names) {
        [Environment]::SetEnvironmentVariable($Name, $SavedEnvironment[$Name], "Process")
    }
    Remove-Item -LiteralPath $Poison -Recurse -Force -ErrorAction SilentlyContinue
}
