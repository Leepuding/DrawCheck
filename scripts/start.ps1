[CmdletBinding()]
param([switch]$Browser)
$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path $PSScriptRoot -Parent
$TaskPython = Join-Path $TaskRoot '.venv-win\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $TaskPython)) { throw 'Prepare .venv-win first; see README.md.' }
Push-Location $TaskRoot
try {
    & $TaskPython (Join-Path $PSScriptRoot 'check-environment.py')
    if ($LASTEXITCODE -ne 0) { throw 'Local dependency check failed.' }
    if ($Browser) { & $TaskPython -m app.launcher --browser }
    else { & $TaskPython -m app.launcher }
    exit $LASTEXITCODE
} finally { Pop-Location }
