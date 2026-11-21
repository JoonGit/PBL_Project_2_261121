$ErrorActionPreference = 'Stop'
$exitCode = 0
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    $python = '.\.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        if (Test-Path -LiteralPath '.venv') {
            throw '현재 환경에서 기존 .venv를 사용할 수 없습니다. 이름을 바꾼 뒤 다시 실행하세요.'
        }
        $versionCheck = 'import sys; sys.exit(sys.version_info < (3, 12))'
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3.12 -c $versionCheck
            $version = '-3.12'
            if ($LASTEXITCODE -ne 0) {
                $version = '-3'
                & py $version -c $versionCheck
            }
            if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 이상을 설치하세요.' }
            & py $version -m venv .venv
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -c $versionCheck
            if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 이상을 설치하세요.' }
            & python -m venv .venv
        } else {
            throw 'Python 3.12 이상을 설치하세요. https://www.python.org/downloads/'
        }
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 설치와 가상환경 생성을 확인하세요.' }
    }
    & $python -c 'import sys; sys.exit(sys.version_info < (3, 12))'
    if ($LASTEXITCODE -ne 0) { throw '기존 .venv가 Python 3.12 이상으로 만들어졌는지 확인하세요.' }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item '.env.example' '.env' }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $python -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("pip") is None)'
    if ($LASTEXITCODE -ne 0) {
        & $python -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw '가상환경에 pip를 준비하지 못했습니다.' }
    }
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '필수 패키지 설치에 실패했습니다.' }
    & $python scripts/check_setup.py --browser --start-mysql
    $exitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
} finally {
    Pop-Location
}
exit $exitCode
