param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = "Stop"
$kitDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

$outputDir = $null
for ($index = 0; $index -lt $Arguments.Count; $index++) {
    $argument = $Arguments[$index]
    if ($argument -eq "--target" -or $argument.StartsWith("--target=")) {
        throw "build.ps1 fixes the target to win-x64; do not pass --target"
    }
    if ($argument -eq "--output-dir") {
        if ($index + 1 -ge $Arguments.Count) {
            throw "--output-dir requires a value"
        }
        $outputDir = $Arguments[$index + 1]
        $index++
    }
    elseif ($argument.StartsWith("--output-dir=")) {
        $outputDir = $argument.Substring("--output-dir=".Length)
    }
}
if ([string]::IsNullOrWhiteSpace($outputDir)) {
    throw "build.ps1 requires --output-dir so the exact candidate can be audited"
}

& $python (Join-Path $kitDir "build_bridge.py") --target win-x64 @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "reference bridge MSVC build failed with exit code $LASTEXITCODE"
}

$candidate = Join-Path $outputDir "bridge.mex"
$audit = Join-Path $outputDir "native-audit.json"
& $python (Join-Path $kitDir "audit-mex.py") --target win-x64 --mex $candidate --output $audit
if ($LASTEXITCODE -ne 0) {
    throw "reference bridge PE native audit failed with exit code $LASTEXITCODE"
}
