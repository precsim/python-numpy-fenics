[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$CoreRuntime = '',
    [ValidateSet('core','numpy')]
    [string]$RuntimeProfile = 'core',
    [string]$CpythonBinaryLock = '',
    [string]$CpythonBinaryAsset = '',
    [string]$CpythonSource = '',
    [string]$TclSource = '',
    [string]$TkSource = '',
    [string]$TclTkLock = '',
    [string]$NumpyWheel = '',
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [string]$LayerEvidence = '',
    [string]$Version = '0.1.0'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$ExpectedPython = '3.14.10'
$ExpectedPythonAbi = 'cp314'
$ExpectedNumpy = '2.5.2'
$ExpectedWheel = 'numpy-2.5.2-cp314-cp314-win_amd64.whl'
$ExpectedWheelSha256 = '7587f53dfbd5edc0f7b87c6217b4c6d2d1f2ef9c3da70bc1315e7db5f8d7ec9d'
$ExpectedTclTk = '8.6.18'
$ExpectedCpythonBinaryAssetId = 678419363
$ExpectedCpythonBinaryAsset = 'python-3.14.10-win32-x64.zip'
$ExpectedCpythonBinaryAssetBytes = 33210246
$ExpectedCpythonBinaryAssetSha256 = '6cc56cec3d548538f418dfe2d8e18fdfcc6d2565616c82ebbde7a82ae1bb2692'
$ExpectedCpythonBinaryRelease = '3.14.10-40336797752'
$ExpectedCpythonInstaller = 'python-3.14.10-amd64.exe'
$ExpectedCpythonInstallerBytes = 33498520
$ExpectedCpythonInstallerSha256 = '92480239264744fe0386a1bd8275786fefa126e3129e56a68e22b52c73329965'
$ExpectedCpythonSource = 'Python-3.14.10.tgz'
$ExpectedCpythonSourceUrl = 'https://www.python.org/ftp/python/3.14.10/Python-3.14.10.tgz'
$ExpectedCpythonSourceSha256 = '9e4996c19ae5a3df31f10c37723661f71d3455d73bd005f79df7098e5cc13192'

function Import-VsX64Environment {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "vswhere.exe is required to locate the Visual Studio x64 build tools: $vswhere"
    }
    $installation = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
    if (-not $installation) {
        throw 'Visual Studio C++ x64 build tools are required for producer Tcl/Tk'
    }
    $vsDevCmd = Join-Path $installation 'Common7\Tools\VsDevCmd.bat'
    if (-not (Test-Path -LiteralPath $vsDevCmd -PathType Leaf)) {
        throw "Visual Studio developer environment is missing: $vsDevCmd"
    }
    $environmentScript = Join-Path ([IO.Path]::GetTempPath()) ('vsdevcmd-' + [guid]::NewGuid().ToString('N') + '.cmd')
    [IO.File]::WriteAllText(
        $environmentScript,
        ('@call "{0}" -no_logo -arch=x64 -host_arch=x64{1}@set{1}' -f $vsDevCmd, [Environment]::NewLine),
        [Text.Encoding]::ASCII
    )
    try {
        $environment = & $env:ComSpec /d /c $environmentScript
        if ($LASTEXITCODE -ne 0) {
            throw 'Visual Studio x64 developer environment initialization failed'
        }
    }
    finally {
        Remove-Item -LiteralPath $environmentScript -Force -ErrorAction SilentlyContinue
    }
    foreach ($line in @($environment)) {
        $separator = $line.IndexOf('=')
        if ($separator -le 0) { continue }
        $name = $line.Substring(0, $separator)
        $value = $line.Substring($separator + 1)
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
    foreach ($tool in @('cl.exe','dumpbin.exe','link.exe','msbuild.exe','nmake.exe')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "Visual Studio developer environment did not provide $tool"
        }
    }
}

function Invoke-NMake {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$NMakeArguments
    )
    Push-Location $WorkingDirectory
    try {
        & nmake.exe /nologo /f makefile.vc @NMakeArguments
        if ($LASTEXITCODE -ne 0) {
            throw "nmake failed in $WorkingDirectory with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Build-NMakeHelper {
    param([Parameter(Mandatory = $true)][string]$WorkingDirectory)
    $source = Join-Path $WorkingDirectory 'nmakehlp.c'
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Tk nmake helper source is missing: $source"
    }
    Push-Location $WorkingDirectory
    try {
        & cl.exe /nologo /O2 /Fe:nmakehlp.exe nmakehlp.c
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $WorkingDirectory 'nmakehlp.exe') -PathType Leaf)) {
            throw 'Tk nmake helper build failed'
        }
    }
    finally {
        Pop-Location
    }
    $env:PATH = "$WorkingDirectory;$env:PATH"
}

function Get-OnlyFile {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $matches = @(Get-ChildItem -LiteralPath $Root -File -Recurse -Filter $Name)
    if ($matches.Count -ne 1) {
        throw "Expected exactly one $Name under $Root, found $($matches.Count)"
    }
    return $matches[0].FullName
}

$outputPath = [IO.Path]::GetFullPath($Output)
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
$corePath = $null
if ($RuntimeProfile -eq 'core') {
    if (-not $Python) {
        throw 'Core profile requires -Python'
    }
    if (-not $CpythonBinaryLock -or -not $CpythonBinaryAsset -or -not $CpythonSource -or -not $TclSource -or -not $TkSource -or -not $TclTkLock) {
        throw 'Core profile requires -CpythonBinaryLock, -CpythonBinaryAsset, -CpythonSource, -TclSource, -TkSource, and -TclTkLock'
    }
    if ($CoreRuntime) {
        throw '-CoreRuntime is valid only for the numpy profile'
    }
    $pythonPath = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
} else {
    if ($Python) {
        throw 'NumPy profile derives from -CoreRuntime and does not accept -Python'
    }
    if ($CpythonBinaryLock -or $CpythonBinaryAsset -or $CpythonSource -or $TclSource -or $TkSource -or $TclTkLock) {
        throw 'CPython binary/source and Tcl/Tk source inputs are valid only for the core profile'
    }
    if (-not $CoreRuntime) {
        throw 'NumPy profile requires -CoreRuntime'
    }
    $corePath = (Resolve-Path -LiteralPath $CoreRuntime -ErrorAction Stop).Path
    if ([string]::Equals($corePath, $outputPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'NumPy output must differ from the qualified core runtime'
    }
    $coreManifestPath = Join-Path $corePath 'runtime-manifest.json'
    if (-not (Test-Path -LiteralPath $coreManifestPath -PathType Leaf)) {
        throw "Qualified core runtime manifest is missing: $coreManifestPath"
    }
    $coreManifest = Get-Content -LiteralPath $coreManifestPath -Raw | ConvertFrom-Json
    if ($coreManifest.format_version -ne 1 -or
        $coreManifest.platform -ne 'win-x64' -or
        $coreManifest.python_abi -ne $ExpectedPythonAbi -or
        $coreManifest.profile -ne 'core' -or
        $coreManifest.producer.repository -ne 'precsim/python-numpy-fenics') {
        throw 'Qualified core runtime manifest identity is invalid'
    }
    $pythonPath = Join-Path $corePath 'python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "Qualified core Python is missing: $pythonPath"
    }
}

$info = ($infoScript | & $pythonPath -I -S -) | ConvertFrom-Json
if ($info.implementation -ne 'CPython' -or $info.version -ne $ExpectedPython -or $info.pointer_bits -ne 64) {
    throw "Expected 64-bit CPython $ExpectedPython, got $($info.implementation) $($info.version)"
}

Remove-Item -LiteralPath $outputPath -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

if ($RuntimeProfile -eq 'core') {
    $source = (Resolve-Path -LiteralPath ([string]$info.prefix)).Path
    foreach ($name in @('python.exe','python314.dll','python3.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt')) {
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

    $cpythonBinaryLockPath = (Resolve-Path -LiteralPath $CpythonBinaryLock -ErrorAction Stop).Path
    $cpythonBinaryLockSha = (Get-FileHash -LiteralPath $cpythonBinaryLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $cpythonBinaryLockPayload = Get-Content -LiteralPath $cpythonBinaryLockPath -Raw | ConvertFrom-Json
    $lockedArchive = $cpythonBinaryLockPayload.upstream.archive_asset
    $lockedInstaller = $cpythonBinaryLockPayload.upstream.installer
    if ($cpythonBinaryLockPayload.schema_version -ne 1 -or
        $cpythonBinaryLockPayload.component -ne 'cpython-binary' -or
        $cpythonBinaryLockPayload.version -ne $ExpectedPython -or
        $cpythonBinaryLockPayload.python_abi -ne $ExpectedPythonAbi -or
        $cpythonBinaryLockPayload.platform -ne 'win-x64' -or
        $cpythonBinaryLockPayload.upstream.repository -ne 'actions/python-versions' -or
        $cpythonBinaryLockPayload.upstream.release -ne $ExpectedCpythonBinaryRelease -or
        $lockedArchive.id -ne $ExpectedCpythonBinaryAssetId -or
        $lockedArchive.filename -ne $ExpectedCpythonBinaryAsset -or
        $lockedArchive.bytes -ne $ExpectedCpythonBinaryAssetBytes -or
        $lockedArchive.sha256 -ne $ExpectedCpythonBinaryAssetSha256 -or
        $lockedInstaller.filename -ne $ExpectedCpythonInstaller -or
        $lockedInstaller.bytes -ne $ExpectedCpythonInstallerBytes -or
        $lockedInstaller.sha256 -ne $ExpectedCpythonInstallerSha256) {
        throw 'CPython binary lock identity differs from the producer baseline'
    }
    $cpythonBinaryAssetPath = (Resolve-Path -LiteralPath $CpythonBinaryAsset -ErrorAction Stop).Path
    if ([IO.Path]::GetFileName($cpythonBinaryAssetPath) -ne $ExpectedCpythonBinaryAsset) {
        throw "Unexpected CPython binary asset: $([IO.Path]::GetFileName($cpythonBinaryAssetPath))"
    }
    if ((Get-Item -LiteralPath $cpythonBinaryAssetPath).Length -ne $ExpectedCpythonBinaryAssetBytes) {
        throw 'CPython binary asset byte count mismatch'
    }
    $cpythonBinaryAssetSha = (Get-FileHash -LiteralPath $cpythonBinaryAssetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($cpythonBinaryAssetSha -ne $ExpectedCpythonBinaryAssetSha256) {
        throw "CPython binary asset SHA-256 mismatch: $cpythonBinaryAssetSha"
    }
    $cpythonSourcePath = (Resolve-Path -LiteralPath $CpythonSource -ErrorAction Stop).Path
    if ([IO.Path]::GetFileName($cpythonSourcePath) -ne $ExpectedCpythonSource) {
        throw "Unexpected CPython source archive: $([IO.Path]::GetFileName($cpythonSourcePath))"
    }
    $cpythonSourceSha = (Get-FileHash -LiteralPath $cpythonSourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($cpythonSourceSha -ne $ExpectedCpythonSourceSha256) {
        throw "CPython source SHA-256 mismatch: $cpythonSourceSha"
    }
    $tclSourcePath = (Resolve-Path -LiteralPath $TclSource -ErrorAction Stop).Path
    $tkSourcePath = (Resolve-Path -LiteralPath $TkSource -ErrorAction Stop).Path
    $tclTkLockPath = (Resolve-Path -LiteralPath $TclTkLock -ErrorAction Stop).Path
    $tclTkLockPayload = Get-Content -LiteralPath $tclTkLockPath -Raw | ConvertFrom-Json
    if ($tclTkLockPayload.schema_version -ne 1 -or
        $tclTkLockPayload.component -ne 'tcl-tk' -or
        $tclTkLockPayload.version -ne $ExpectedTclTk -or
        -not $tclTkLockPayload.sources.tcl -or
        -not $tclTkLockPayload.sources.tk) {
        throw 'Tcl/Tk source lock identity is invalid'
    }
    foreach ($record in @(
        [pscustomobject]@{ Label = 'Tcl'; Path = $tclSourcePath; Lock = $tclTkLockPayload.sources.tcl },
        [pscustomobject]@{ Label = 'Tk'; Path = $tkSourcePath; Lock = $tclTkLockPayload.sources.tk }
    )) {
        if ([IO.Path]::GetFileName($record.Path) -ne [string]$record.Lock.filename) {
            throw "$($record.Label) source filename differs from the producer lock"
        }
        $actualSourceSha = (Get-FileHash -LiteralPath $record.Path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSourceSha -ne [string]$record.Lock.sha256) {
            throw "$($record.Label) source SHA-256 mismatch: $actualSourceSha"
        }
    }

    $tclTkBuild = Join-Path ([IO.Path]::GetTempPath()) ('python-runtime-tcltk-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tclTkBuild | Out-Null
    try {
        & tar.exe -xf $tclSourcePath -C $tclTkBuild
        if ($LASTEXITCODE -ne 0) { throw 'Tcl source extraction failed' }
        & tar.exe -xf $tkSourcePath -C $tclTkBuild
        if ($LASTEXITCODE -ne 0) { throw 'Tk source extraction failed' }
        & tar.exe -xf $cpythonSourcePath -C $tclTkBuild
        if ($LASTEXITCODE -ne 0) { throw 'CPython source extraction failed' }
        $tclSourceRoot = Join-Path $tclTkBuild "tcl$ExpectedTclTk"
        $tkSourceRoot = Join-Path $tclTkBuild "tk$ExpectedTclTk"
        $cpythonSourceRoot = Join-Path $tclTkBuild "Python-$ExpectedPython"
        if (-not (Test-Path -LiteralPath (Join-Path $tclSourceRoot 'win\makefile.vc') -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $tkSourceRoot 'win\makefile.vc') -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $cpythonSourceRoot 'PCbuild\_tkinter.vcxproj') -PathType Leaf)) {
            throw 'CPython/Tcl/Tk source archives do not contain the expected Windows build trees'
        }

        Import-VsX64Environment
        $tclTkInstall = Join-Path $tclTkBuild 'tcltk-install'
        Invoke-NMake -WorkingDirectory (Join-Path $tclSourceRoot 'win') -NMakeArguments @('core','shell','dlls','MACHINE=AMD64')
        Invoke-NMake -WorkingDirectory (Join-Path $tclSourceRoot 'win') -NMakeArguments @(
            'install-binaries','install-libraries','MACHINE=AMD64',("INSTALLDIR=$tclTkInstall")
        )
        $tkWin = Join-Path $tkSourceRoot 'win'
        Build-NMakeHelper -WorkingDirectory $tkWin
        Invoke-NMake -WorkingDirectory $tkWin -NMakeArguments @('core','MACHINE=AMD64',("TCLDIR=$tclSourceRoot"))
        Invoke-NMake -WorkingDirectory $tkWin -NMakeArguments @(
            'install-libraries','MACHINE=AMD64',("TCLDIR=$tclSourceRoot"),("INSTALLDIR=$tclTkInstall")
        )

        $builtTclProvider = Get-OnlyFile -Root (Join-Path $tclTkInstall 'bin') -Name 'tcl86t.dll'
        $builtTclImportLib = Get-OnlyFile -Root (Join-Path $tclTkInstall 'lib') -Name 'tcl86t.lib'
        $builtTkProvider = Get-OnlyFile -Root $tkWin -Name 'tk86t.dll'
        $builtTkImportLib = Get-OnlyFile -Root $tkWin -Name 'tk86t.lib'
        Copy-Item -LiteralPath $builtTkProvider -Destination (Join-Path $tclTkInstall 'bin\tk86t.dll')
        Copy-Item -LiteralPath $builtTkImportLib -Destination (Join-Path $tclTkInstall 'lib\tk86t.lib')

        $pythonImportLib = Join-Path $source 'libs\python314.lib'
        if (-not (Test-Path -LiteralPath $pythonImportLib -PathType Leaf)) {
            throw "setup-python CPython import library is missing: $pythonImportLib"
        }
        $cpythonOut = Join-Path $cpythonSourceRoot 'PCbuild\amd64'
        New-Item -ItemType Directory -Force -Path $cpythonOut | Out-Null
        Copy-Item -LiteralPath $pythonImportLib -Destination (Join-Path $cpythonOut 'python314.lib') -Force
        $pythonDll = Join-Path $source 'python314.dll'
        if (Test-Path -LiteralPath $pythonDll -PathType Leaf) {
            Copy-Item -LiteralPath $pythonDll -Destination (Join-Path $cpythonOut 'python314.dll') -Force
        }
        $python3ImportLib = Join-Path $source 'libs\python3.lib'
        if (Test-Path -LiteralPath $python3ImportLib -PathType Leaf) {
            Copy-Item -LiteralPath $python3ImportLib -Destination (Join-Path $cpythonOut 'python3.lib') -Force
        }
        $tclTkSdkProperty = '/p:tcltkDir=' + $tclTkInstall.TrimEnd('\') + '\'
        $tkinterProject = Join-Path $cpythonSourceRoot 'PCbuild\_tkinter.vcxproj'
        & msbuild.exe $tkinterProject /nologo /m /t:Build `
            /p:Configuration=Release /p:Platform=x64 /p:BuildProjectReferences=false /p:BuildForRelease=true `
            /p:TclVersion=8.6.18.0 /p:TkVersion=8.6.18.0 $tclTkSdkProperty
        if ($LASTEXITCODE -ne 0) {
            throw "CPython _tkinter rebuild failed with exit code $LASTEXITCODE"
        }
        $rebuiltTkinter = Get-OnlyFile -Root $cpythonOut -Name '_tkinter.pyd'
        $tkinterDependencies = @(& dumpbin.exe /dependents $rebuiltTkinter)
        if ($LASTEXITCODE -ne 0 -or
            -not ($tkinterDependencies -match '(?i)\bpython314\.dll\b') -or
            -not ($tkinterDependencies -match '(?i)\btcl86t\.dll\b') -or
            -not ($tkinterDependencies -match '(?i)\btk86t\.dll\b') -or
            ($tkinterDependencies -match '(?i)\bpython312\.dll\b') -or
            ($tkinterDependencies -match '(?i)\b(?:tcl|tk)9\d*[^\s]*\.dll\b')) {
            throw 'Rebuilt _tkinter does not bind exactly to CPython 3.14 and the Tcl/Tk 8.6 provider ABI'
        }

        $dllRoot = Join-Path $outputPath 'DLLs'
        New-Item -ItemType Directory -Force -Path $dllRoot | Out-Null
        Get-ChildItem -LiteralPath $outputPath -File -Recurse |
            Where-Object { $_.Name -match '(?i)^(tcl|tk)\d.*\.dll$' -or $_.Name -match '(?i)^_tkinter.*\.pyd$' } |
            Remove-Item -Force
        Copy-Item -LiteralPath $builtTclProvider -Destination (Join-Path $dllRoot 'tcl86t.dll')
        Copy-Item -LiteralPath $builtTkProvider -Destination (Join-Path $dllRoot 'tk86t.dll')
        Copy-Item -LiteralPath $rebuiltTkinter -Destination (Join-Path $dllRoot '_tkinter.pyd')

        $tclRuntimeRoot = Join-Path $outputPath 'tcl'
        Remove-Item -LiteralPath $tclRuntimeRoot -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $tclRuntimeRoot | Out-Null
        Get-ChildItem -LiteralPath (Join-Path $tclTkInstall 'lib') -Force |
            Copy-Item -Destination $tclRuntimeRoot -Recurse -Force
    }
    finally {
        Remove-Item -LiteralPath $tclTkBuild -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Get-ChildItem -LiteralPath $corePath -Force | Copy-Item -Destination $outputPath -Recurse -Force
}

$sitePackages = Join-Path $outputPath 'Lib\site-packages'
if ($RuntimeProfile -eq 'core') {
    Remove-Item -LiteralPath $sitePackages -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
} elseif (-not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
    throw "Qualified core site-packages directory is missing: $sitePackages"
}

$numpy = $null
$tclTk = $null
if ($RuntimeProfile -eq 'core') {
    $tclProviderPath = Join-Path $outputPath 'DLLs\tcl86t.dll'
    $tkProviderPath = Join-Path $outputPath 'DLLs\tk86t.dll'
    $tkinterPath = Join-Path $outputPath 'DLLs\_tkinter.pyd'
    $tclInitPath = Join-Path $outputPath 'tcl\tcl8.6\init.tcl'
    $tkInitPath = Join-Path $outputPath 'tcl\tk8.6\tk.tcl'
    foreach ($required in @($tclProviderPath,$tkProviderPath,$tkinterPath,$tclInitPath,$tkInitPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Producer Tcl/Tk staging is incomplete: $required"
        }
    }
    $tclTk = [ordered]@{
        version = $ExpectedTclTk
        ownership = 'producer'
        source_lock = [ordered]@{
            filename = [IO.Path]::GetFileName($tclTkLockPath)
            sha256 = (Get-FileHash -LiteralPath $tclTkLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        sources = [ordered]@{
            tcl = [ordered]@{
                filename = [string]$tclTkLockPayload.sources.tcl.filename
                url = [string]$tclTkLockPayload.sources.tcl.url
                sha256 = [string]$tclTkLockPayload.sources.tcl.sha256
            }
            tk = [ordered]@{
                filename = [string]$tclTkLockPayload.sources.tk.filename
                url = [string]$tclTkLockPayload.sources.tk.url
                sha256 = [string]$tclTkLockPayload.sources.tk.sha256
            }
        }
        providers = [ordered]@{
            tcl = [ordered]@{
                path = 'DLLs/tcl86t.dll'
                sha256 = (Get-FileHash -LiteralPath $tclProviderPath -Algorithm SHA256).Hash.ToLowerInvariant()
            }
            tk = [ordered]@{
                path = 'DLLs/tk86t.dll'
                sha256 = (Get-FileHash -LiteralPath $tkProviderPath -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        tkinter_rebuild = [ordered]@{
            path = 'DLLs/_tkinter.pyd'
            sha256 = (Get-FileHash -LiteralPath $tkinterPath -Algorithm SHA256).Hash.ToLowerInvariant()
            reason = 'CPython 3.14.10 default _tkinter targets Tcl/Tk 9; producer runtime requires Tcl/Tk 8.6.18'
            cpython_source = [ordered]@{
                filename = $ExpectedCpythonSource
                url = $ExpectedCpythonSourceUrl
                sha256 = $ExpectedCpythonSourceSha256
            }
            build = [ordered]@{
                project = 'PCbuild/_tkinter.vcxproj'
                configuration = 'Release'
                platform = 'x64'
                build_project_references = $false
                tcl_version = '8.6.18.0'
                tk_version = '8.6.18.0'
                python_import_library_sha256 = (Get-FileHash -LiteralPath (Join-Path $source 'libs\python314.lib') -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        scripts = [ordered]@{
            tcl = [ordered]@{
                path = 'tcl/tcl8.6'
                init_sha256 = (Get-FileHash -LiteralPath $tclInitPath -Algorithm SHA256).Hash.ToLowerInvariant()
            }
            tk = [ordered]@{
                path = 'tcl/tk8.6'
                init_sha256 = (Get-FileHash -LiteralPath $tkInitPath -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        build = [ordered]@{
            tool = 'nmake'
            machine = 'AMD64'
            tcl_targets = @('core','shell','dlls','install-binaries','install-libraries')
            tk_targets = @('core','install-libraries')
        }
    }
} else {
    $tclTk = $coreManifest.tcl_tk
    if (-not $tclTk -or
        [string]$tclTk.version -ne $ExpectedTclTk -or
        [string]$tclTk.tkinter_rebuild.cpython_source.sha256 -ne $ExpectedCpythonSourceSha256) {
        throw 'Qualified core runtime does not carry the producer Tcl/Tk 8.6.18 identity'
    }
}
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
    # The core manifest has already been sealed. An import probe must not
    # create __pycache__ files in its inherited standard library.
    $probe = ($probeScript | & (Join-Path $outputPath 'python.exe') -B -I -) | ConvertFrom-Json
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

[IO.File]::WriteAllText((Join-Path $outputPath 'RUNTIME_PROFILE'), $RuntimeProfile, $Utf8NoBom)

$coreIdentity = $null
if ($RuntimeProfile -eq 'numpy') {
    $layerEvidencePath = if ($LayerEvidence) {
        [IO.Path]::GetFullPath($LayerEvidence)
    } else {
        "$outputPath.core-layer.json"
    }
    $verifyLayer = Join-Path $PSScriptRoot 'verify_core_layer.py'
    & $pythonPath -I -S $verifyLayer --core $corePath --numpy $outputPath --output $layerEvidencePath
    if ($LASTEXITCODE -ne 0) {
        throw 'NumPy runtime does not preserve the qualified core identity'
    }
    $coreIdentity = Get-Content -LiteralPath $layerEvidencePath -Raw | ConvertFrom-Json
}

$pythonHash = (Get-FileHash -LiteralPath (Join-Path $outputPath 'python.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
$dllHash = (Get-FileHash -LiteralPath (Join-Path $outputPath 'python314.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
$relativePrefix = $outputPath.TrimEnd('\') + '\'
$files = @(
    Get-ChildItem -LiteralPath $outputPath -File -Recurse -Force |
        Where-Object { $_.Name -ne 'runtime-manifest.json' } |
        Sort-Object FullName |
        ForEach-Object {
            if (-not $_.FullName.StartsWith($relativePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Runtime file escaped output root: $($_.FullName)"
            }
            [ordered]@{
                path = $_.FullName.Substring($relativePrefix.Length).Replace('\','/')
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                owners = @('python-numpy-fenics')
            }
        }
)
$manifest = [ordered]@{
    format_version = 1
    artifact = "python-runtime-win-x64-cp314-$RuntimeProfile"
    version = $Version
    platform = 'win-x64'
    python_abi = $ExpectedPythonAbi
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
        binary_asset = if ($RuntimeProfile -eq 'core') {
            [ordered]@{
                lock_sha256 = $cpythonBinaryLockSha
                release = $ExpectedCpythonBinaryRelease
                asset_id = $ExpectedCpythonBinaryAssetId
                filename = $ExpectedCpythonBinaryAsset
                bytes = $ExpectedCpythonBinaryAssetBytes
                sha256 = $ExpectedCpythonBinaryAssetSha256
                installer = [ordered]@{
                    filename = $ExpectedCpythonInstaller
                    bytes = $ExpectedCpythonInstallerBytes
                    sha256 = $ExpectedCpythonInstallerSha256
                }
            }
        } else {
            $null
        }
    }
    tcl_tk = $tclTk
    base_core = if ($coreIdentity) {
        [ordered]@{
            manifest_sha256 = [string]$coreIdentity.core_manifest_sha256
            inherited_tree_sha256 = [string]$coreIdentity.inherited_tree_sha256
            inherited_file_count = [int]$coreIdentity.inherited_file_count
            excluded_paths = @($coreIdentity.excluded_paths)
        }
    } else {
        $null
    }
    numpy = $numpy
    files = $files
}
$manifestJson = ($manifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine
[IO.File]::WriteAllText((Join-Path $outputPath 'runtime-manifest.json'), $manifestJson, $Utf8NoBom)

Write-Host "runtime_profile=$RuntimeProfile"
Write-Host "runtime=$outputPath"
Write-Host "runtime_file_count=$($files.Count)"
