"""다른 사람에게 보낼 묶음(zip)을 만든다.

폴더를 그대로 압축하면 내 PC 의 흔적이 따라간다(.venv 와 캐시 파일에는 내 PC 의 경로가,
.env 에는 내 설정이 들어 있다). 이 스크립트는 코드·샘플·문서만 골라 담는다.

    python scripts/make_share_zip.py                 # 프로젝트 폴더 옆에 futuremarketer-autopost-share.zip
    python scripts/make_share_zip.py 원하는/경로.zip
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".git", "var", "logs", ".idea", ".vscode"}
SKIP_FILES = {".env", ".DS_Store", "app-state.json", "app-state.lock"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".zip"}


def included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in relative.parts[:-1]):
        return False
    return path.name not in SKIP_FILES and not path.name.startswith(".app-state-") and path.suffix not in SKIP_SUFFIXES


def main(argv: list[str]) -> int:
    target = Path(argv[0]).expanduser().resolve() if argv else ROOT.parent / f"{ROOT.name}-share.zip"
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, Path(ROOT.name) / path.relative_to(ROOT))
    print(f"{len(files)}개 파일을 담았습니다: {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
