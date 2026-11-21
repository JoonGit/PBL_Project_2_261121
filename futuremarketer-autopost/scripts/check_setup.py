"""로컬 세팅 점검. 사람이 직접 돌려도 되고, AI 코딩 도구가 세팅을 마쳤는지 확인할 때도 쓴다.

    python scripts/check_setup.py

항목마다 [통과] / [확인 필요] / [선택] 을 출력한다. 필수 항목이 모두 통과하면 종료 코드 0.
아무것도 바꾸지 않는다(읽기만 한다). 단, DB 표가 없으면 처음 뜰 때처럼 만든다.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

results: list[tuple[str, bool, str, bool]] = []      # (이름, 통과, 설명, 필수)


def check(name: str, ok: bool, detail: str = "", required: bool = True, *, hint: str = "") -> bool:
    """detail 은 늘 보이고, hint(고치는 방법)는 통과하지 못했을 때만 보인다."""
    results.append((name, ok, detail if ok or not hint else f"{detail} → {hint}".lstrip(" →"), required))
    return ok


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    windows = sys.platform == "win32"
    check("Python 3.12 이상", sys.version_info >= (3, 12), sys.version.split()[0])
    python_cmd = r".\.venv\Scripts\python.exe" if windows else ".venv/bin/python"
    check("가상환경(.venv) 안에서 실행", sys.prefix != sys.base_prefix,
          hint=f"{python_cmd} scripts/check_setup.py 로 실행하세요")
    check(".env 파일", (ROOT / ".env").is_file(), hint="Copy-Item .env.example .env" if windows else "cp .env.example .env")

    missing = []
    for module in ("streamlit", "sqlalchemy", "pymysql", "openpyxl", "PIL", "dotenv", "playwright", "psutil", "anthropic", "openai"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    check("파이썬 패키지", not missing, ("없음: " + ", ".join(missing) + " → pip install -r requirements.txt") if missing else "")

    check("Docker 명령", shutil.which("docker") is not None, hint="Docker Desktop 을 설치하고 켜 두세요")
    check("MySQL(127.0.0.1:3317)", port_open("127.0.0.1", 3317), hint="docker compose up -d")

    if not missing:
        from futuremarketer.config import load_settings
        from futuremarketer.db import make_engine
        from futuremarketer.db_init import ensure_schema

        settings = load_settings()
        try:
            state = ensure_schema(make_engine(settings.database_url), settings.target_code)
            check("DB 표", state != "outdated",
                  "python -m futuremarketer.db_init --reset (기록이 지워집니다)" if state == "outdated" else state)
        except Exception as exc:  # 접속 실패 등
            check("DB 표", False, f"DB 에 접속하지 못했습니다({type(exc).__name__}). MySQL 이 뜬 지 몇 초 뒤 다시 실행하세요")
        check("Aside 브라우저", settings.aside_path.is_file(), str(settings.aside_path),
              hint="aside.com/download에서 설치하거나 .env의 FM_ASIDE_PATH에 실행 파일 경로를 적으세요")
        check("엑셀·사진 샘플", settings.default_excel.is_file() and settings.image_dir.is_dir(),
              f"{settings.default_excel.name}, {settings.image_dir.name}/")
        check("등록 대상", True, f"{settings.daangn_target} (practice 면 연습용 화면), AI={settings.ai_mode}", required=False)

    for name, command, use in (("Claude Code", "claude", "글 작성을 구독 로그인으로 쓸 때"),
                               ("Codex", "codex", "이미지 생성을 구독 로그인으로 쓸 때")):
        found = shutil.which(f"{command}.exe" if windows else command)
        version = ""
        if found:
            try:
                version = subprocess.run([found, "--version"], capture_output=True, encoding="utf-8", errors="replace",
                                         timeout=15).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                version = ""
        check(f"{name} (선택)", bool(found), (version or "설치됨") if found else f"없음 — {use}만 필요. API 키로 써도 됩니다",
              required=False)

    width = max(len(r[0]) for r in results)
    for name, ok, detail, required in results:
        mark = "통과" if ok else ("확인 필요" if required else "선택")
        print(f"[{mark}] {name.ljust(width)}  {detail}")
    failed = [r for r in results if r[3] and not r[1]]
    print()
    print(f"필수 항목을 모두 통과했습니다. {python_cmd} -m streamlit run streamlit_app.py 로 실행하세요."
          if not failed else f"확인이 필요한 필수 항목 {len(failed)}개")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
