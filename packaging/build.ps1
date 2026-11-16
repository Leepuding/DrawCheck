# Offline-only Windows x64 build. Does not install packages, sign, publish, or call a model.
[CmdletBinding()]
param(
    [string]$Version = '0.2.0',
    [string]$Python = '',
    [string]$ISCC = '',
    [string]$VendorManifest = '',
    [string]$EnvironmentLock = '',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PackRoot = $PSScriptRoot
$RepoRoot = Split-Path $PackRoot -Parent
if ($env:OS -ne 'Windows_NT') { throw 'Windows x64 is required. Cross-compiling from macOS is unsupported.' }
if ($Version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { throw 'Version must be numeric major.minor.patch[.build].' }
if (-not $Python) { $Python = Join-Path $RepoRoot '.venv-win\Scripts\python.exe' }
if (-not $VendorManifest) { $VendorManifest = Join-Path $PackRoot 'vendor.local.json' }
if (-not $EnvironmentLock) { $EnvironmentLock = Join-Path $PackRoot 'requirements-win.lock' }
if (-not $ISCC) {
    $Compiler = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($Compiler) { $ISCC = $Compiler.Source }
    else {
        $Candidates = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
        )
        $ISCC = $Candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}
foreach ($InputFile in @($Python, $ISCC, $VendorManifest, $EnvironmentLock)) {
    if (-not $InputFile -or -not (Test-Path -LiteralPath $InputFile -PathType Leaf)) {
        throw "Missing build input: '$InputFile'. See docs/BUILD.md; this build never downloads dependencies."
    }
}
function Invoke-Python {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python check/build failed (exit $LASTEXITCODE)." }
}
function Get-InnoCompilerVersion {
    param([string]$Compiler)
    $FileVersion = (Get-Item -LiteralPath $Compiler).VersionInfo.FileVersion
    if ($FileVersion -and $FileVersion -ne '0.0.0.0') { return $FileVersion }

    # Windows on Arm can report a zero file version for a working compiler.
    $ProbeDir = Join-Path ([IO.Path]::GetTempPath()) ('drawcheck-iscc-' + [guid]::NewGuid())
    $null = New-Item -ItemType Directory -Path $ProbeDir
    try {
        $Probe = Join-Path $ProbeDir 'version.iss'
        @('[Setup]', 'AppName=DrawCheck Compiler Probe', 'AppVersion=1.0',
            'DefaultDirName={autopf}\DrawCheckCompilerProbe', 'Output=no') |
            Set-Content -LiteralPath $Probe -Encoding Ascii
        $Output = & $Compiler '/O-' $Probe
        $CompilerExit = $LASTEXITCODE
        $Version = [regex]::Match(($Output -join "`n"), '^Compiler engine version: Inno Setup (\d+\.\d+\.\d+(?:\.\d+)?)\s*$',
            [System.Text.RegularExpressions.RegexOptions]::Multiline)
        if ($CompilerExit -ne 0 -or -not $Version.Success) { throw 'Could not determine the Inno Setup compiler engine version.' }
        return $Version.Groups[1].Value
    } finally {
        Remove-Item -LiteralPath $ProbeDir -Recurse -Force
    }
}
Push-Location $RepoRoot
try {
    Invoke-Python -Arguments @((Join-Path $PackRoot 'preflight.py'), 'windows')
    Invoke-Python -Arguments @('-m', 'pip', 'check')
    Invoke-Python -Arguments @((Join-Path $PackRoot 'preflight.py'), 'lock', '--output', $EnvironmentLock, '--verify')
    $CompilerVersion = Get-InnoCompilerVersion $ISCC
    if ($CompilerVersion -notmatch '^6\.' -or [version]$CompilerVersion -lt [version]'6.3.0') {
        throw 'Inno Setup 6.3 or newer in the 6.x series is required.'
    }
    Invoke-Python -Arguments @((Join-Path $PackRoot 'preflight.py'), 'vendor', '--manifest', $VendorManifest,
        '--vendor', (Join-Path $PackRoot 'vendor'), '--execute')
    if ($CheckOnly) { Write-Output 'Build prerequisites passed; no artifact was built.'; return }

    # Only derived build directories are reset; user data is never in these directories.
    foreach ($Generated in @('stage', 'build', 'dist')) {
        $Target = Join-Path $PackRoot $Generated
        if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
        New-Item -ItemType Directory -Path $Target | Out-Null
    }
    $ThirdParty = Join-Path $PackRoot 'stage\third_party'
    New-Item -ItemType Directory -Path $ThirdParty | Out-Null
    Copy-Item -LiteralPath (Join-Path $PackRoot 'vendor') -Destination (Join-Path $ThirdParty 'vendor') -Recurse
    Copy-Item -LiteralPath $VendorManifest -Destination (Join-Path $ThirdParty 'vendor-manifest.json')
    Copy-Item -LiteralPath $EnvironmentLock -Destination (Join-Path $ThirdParty 'requirements-win.lock')
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'LICENSES.md') -Destination $ThirdParty
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'docs\third-party') -Destination (Join-Path $ThirdParty 'license-texts') -Recurse
    Copy-Item -Path (Join-Path $PackRoot 'third_party\*.md') -Destination $ThirdParty
    Invoke-Python -Arguments @((Join-Path $PackRoot 'preflight.py'), 'licenses', '--output', $ThirdParty,
        '--vendor', (Join-Path $PackRoot 'vendor'))
    Invoke-Python -Arguments @((Join-Path $PackRoot 'preflight.py'), 'vendor', '--manifest', $VendorManifest,
        '--vendor', (Join-Path $PackRoot 'vendor'), '--execute', '--output', (Join-Path $ThirdParty 'components.json'))
    $SourceFiles = Get-ChildItem -Path @((Join-Path $RepoRoot 'app'), (Join-Path $RepoRoot 'backend'),
        (Join-Path $RepoRoot 'frontend'), (Join-Path $RepoRoot 'scripts'), $PackRoot) -Recurse -File | Where-Object {
            $_.FullName -notmatch '[\\/](vendor|stage|build|dist|__pycache__|\.pytest_cache)[\\/]' -and
            $_.Name -notmatch '\.local\.json$'
        } | ForEach-Object {
            [ordered]@{ path = $_.FullName.Substring($RepoRoot.Length + 1).Replace('\', '/');
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
        }
    $SourceFiles | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ThirdParty 'source-files.json') -Encoding UTF8
    Invoke-Python -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', (Join-Path $PackRoot 'dist'),
        '--workpath', (Join-Path $PackRoot 'build'), (Join-Path $PackRoot 'drawcheck.spec'))
    $Executable = Join-Path $PackRoot 'dist\DrawCheck\DrawCheck.exe'
    $SmokeReport = Join-Path $PackRoot 'dist\frozen-self-test.json'
    # A windowed executable must be waited explicitly; do not inspect a stale LASTEXITCODE.
    $SmokeProcess = Start-Process -FilePath $Executable -ArgumentList @('--self-test', ('"' + $SmokeReport + '"')) -PassThru
    $null = $SmokeProcess.Handle
    if (-not $SmokeProcess.WaitForExit(180000)) {
        & taskkill /PID $SmokeProcess.Id /T /F | Out-Null
        throw 'Frozen self-test timed out; terminated its process tree.'
    }
    if ($SmokeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $SmokeReport)) { throw 'Frozen self-test failed.' }
    $Smoke = Get-Content -LiteralPath $SmokeReport -Raw | ConvertFrom-Json
    if (-not $Smoke.ok) { throw 'Frozen self-test report contains failures.' }
    & $ISCC ('/DAppVersion=' + $Version) (Join-Path $PackRoot 'installer.iss')
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)." }
    $Installer = Join-Path $PackRoot ("dist\DrawCheck-$Version-win-x64-setup.exe")
    if (-not (Test-Path -LiteralPath $Installer)) { throw 'Installer was not generated.' }
    $BundleBytes = (Get-ChildItem -LiteralPath (Split-Path $Executable) -Recurse -File | Measure-Object Length -Sum).Sum
    $RuntimeRoot = Split-Path $Executable
    Get-ChildItem -LiteralPath $RuntimeRoot -Recurse -File | ForEach-Object {
        [ordered]@{ path = $_.FullName.Substring($RuntimeRoot.Length + 1).Replace('\', '/');
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $PackRoot 'dist\runtime-files.json') -Encoding UTF8
    $SourceCommit = 'unavailable'
    $SourceDirty = $null
    if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
        $SourceCommit = & git -C $RepoRoot rev-parse HEAD
        if ($LASTEXITCODE -ne 0) { throw 'Could not read source commit.' }
        $SourceDirty = [bool](& git -C $RepoRoot status --porcelain)
        if ($LASTEXITCODE -ne 0) { throw 'Could not read source working tree state.' }
    }
    $Record = [ordered]@{
        version = $Version; builtUtc = [DateTime]::UtcNow.ToString('o'); signed = $false
        installerBytes = (Get-Item -LiteralPath $Installer).Length; bundleBytes = $BundleBytes
        installerSha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
        environmentLockSha256 = (Get-FileHash -LiteralPath $EnvironmentLock -Algorithm SHA256).Hash.ToLowerInvariant()
        vendorManifestSha256 = (Get-FileHash -LiteralPath $VendorManifest -Algorithm SHA256).Hash.ToLowerInvariant()
        innoSetupVersion = $CompilerVersion; sourceCommit = $SourceCommit
        sourceDirty = $SourceDirty
        windowsInstallationAcceptance = 'NOT RUN'; defenderScan = 'NOT RUN'; smartScreen = 'NOT RUN'
    }
    $Record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PackRoot 'dist\build-record.json') -Encoding UTF8
    Write-Output "Built unsigned installer: $Installer"
    Write-Output 'Frozen checks passed. Run the clean Windows acceptance checklist before requesting publication.'
} finally { Pop-Location }
