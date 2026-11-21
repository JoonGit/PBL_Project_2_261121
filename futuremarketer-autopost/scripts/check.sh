#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ ! -x .venv/bin/python ]]; then
    echo "먼저 bash scripts/setup.sh를 실행하세요." >&2
    exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
exec .venv/bin/python scripts/check_setup.py --browser "$@"
