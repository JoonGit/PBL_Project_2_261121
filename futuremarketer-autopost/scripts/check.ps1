$ErrorActionPreference = 'Stop'
$exitCode = 0
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    $python = '.\.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw '먼저 .\scripts\setup.ps1을 실행하세요.'
    }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $python scripts/check_setup.py --browser
    $exitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
} finally {
    Pop-Location
}
exit $exitCode
