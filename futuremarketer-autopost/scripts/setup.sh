#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

python_bin=.venv/bin/python
if [[ ! -x "$python_bin" ]]; then
    if [[ -e .venv ]]; then
        echo "현재 환경에서 기존 .venv를 사용할 수 없습니다. 이름을 바꾼 뒤 다시 실행하세요." >&2
        exit 1
    fi
    bootstrap=$(command -v python3.12 || command -v python3 || true)
    if [[ -z "$bootstrap" ]] || ! "$bootstrap" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
        echo "Python 3.12 이상을 설치한 뒤 다시 실행하세요. https://www.python.org/downloads/" >&2
        exit 1
    fi
    "$bootstrap" -m venv .venv
fi
if ! "$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    echo "기존 .venv가 Python 3.12 이상으로 만들어졌는지 확인하세요." >&2
    exit 1
fi
if [[ ! -f .env ]]; then cp .env.example .env; fi
export PYTHONDONTWRITEBYTECODE=1
if ! "$python_bin" -m pip --version >/dev/null 2>&1; then
    "$python_bin" -m ensurepip --upgrade
fi
"$python_bin" -m pip install -r requirements.txt
exec bash scripts/check.sh --start-mysql "$@"
