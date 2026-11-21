"""로컬 세팅 점검. 사람이 직접 돌려도 되고, AI 코딩 도구가 세팅을 마쳤는지 확인할 때도 쓴다.

    python scripts/check_setup.py

항목마다 [통과] / [확인 필요] / [선택] 을 출력한다. 필수 항목이 모두 통과하면 종료 코드 0.
저장소가 비어 있으면 초기화한다. --browser는 임시 프로필로 Aside를 검사하고,
--start-mysql은 필요한 경우 로컬 MySQL 컨테이너를 시작한다.
"""
from __future__ import annotations

import argparse
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def probe_aside(settings) -> None:
    from futuremarketer.adapters.aside import AsideBrowser

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'<main id="fm-ready">Futuremarketer setup check</main>')

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="fm-browser-check-") as tmp:
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            cdp_port = candidate.getsockname()[1]
        with ThreadingHTTPServer(("127.0.0.1", 0), Page) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = AsideBrowser(settings.aside_path, Path(tmp) / "profile", cdp_port, (40, 40, 900, 700), headless=True)
            try:
                page = browser.start(f"http://127.0.0.1:{server.server_port}/")
                page.locator("#fm-ready").wait_for(timeout=8000)
            finally:
                browser.close()
                server.shutdown()
                thread.join()


def check_mysql(settings, start_mysql: bool) -> None:
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    host, port = url.host or "127.0.0.1", url.port or 3306
    if start_mysql and host in ("127.0.0.1", "localhost", "::1") and not port_open(host, port):
        docker = shutil.which("docker")
        if check("Docker 명령", docker is not None, hint="Docker Desktop을 설치하고 실행하세요."):
            try:
                proc = subprocess.run([docker, "compose", "up", "-d", "--wait"], cwd=ROOT, timeout=180)
                check("MySQL 컨테이너 시작", proc.returncode == 0, hint="Docker Desktop의 실행 상태를 확인하세요.")
            except (OSError, subprocess.TimeoutExpired):
                check("MySQL 컨테이너 시작", False, hint="Docker Desktop의 실행 상태를 확인하세요.")
    check(f"MySQL({host}:{port})", port_open(host, port),
          hint="설정한 MySQL을 실행하세요. Docker를 쓰면 docker compose up -d --wait를 실행하세요.")


def check_app_storage(start_mysql: bool, browser_test: bool) -> None:
    from futuremarketer.config import load_settings
    from futuremarketer.db import make_app_db
    from futuremarketer.db_init import ensure_schema

    try:
        settings = load_settings()
        local = settings.db_backend == "json"
        if not local:
            check_mysql(settings, start_mysql)
    except ValueError as exc:
        check("프로젝트 설정", False, str(exc), hint=".env 값을 확인하세요.")
        return

    storage = "로컬 JSON 저장소" if local else "MySQL 저장소"
    try:
        state = ensure_schema(make_app_db(settings), settings.target_code)
        check(storage, state != "outdated", str(settings.json_path) if local else state,
              hint="저장 형식이 바뀌었습니다. 기록을 백업한 뒤 확인하세요.")
    except Exception as exc:
        hint = "데이터 폴더의 쓰기 권한과 JSON 파일을 확인하세요" if local else "MySQL을 시작한 뒤 다시 실행하세요"
        check(storage, False, f"저장소를 열지 못했습니다({type(exc).__name__}).", hint=hint)

    if check("Aside 브라우저", settings.aside_path.is_file(), str(settings.aside_path),
             hint="aside.com/download에서 설치하거나 .env의 FM_ASIDE_PATH에 실행 파일 경로를 적으세요") and browser_test:
        try:
            probe_aside(settings)
            check("Aside 실행 및 연결", True, "임시 프로필로 로컬 화면까지 열었습니다.")
        except Exception as exc:
            check("Aside 실행 및 연결", False, type(exc).__name__,
                  hint="운영체제에 맞는 Aside 설치 파일인지와 FM_ASIDE_PATH를 확인하세요.")

    check("엑셀·사진 샘플", settings.default_excel.is_file() and settings.image_dir.is_dir(),
          f"{settings.default_excel.name}, {settings.image_dir.name}/")
    check("등록 대상", True, f"{settings.daangn_target} (practice 면 연습용 화면), AI={settings.ai_mode}", required=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="퓨처마케터 실행 전 준비 상태를 확인합니다.")
    parser.add_argument("--browser", action="store_true", help="임시 프로필로 Aside 브라우저 연결을 검사합니다.")
    parser.add_argument("--start-mysql", action="store_true", help="MySQL 모드에서만 로컬 Docker 컨테이너를 시작합니다.")
    args = parser.parse_args(argv)
    results.clear()
    windows = sys.platform == "win32"
    check("실행 환경", True, f"{platform.system()} {platform.release()} / {platform.machine()}")
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

    if not missing:
        check_app_storage(args.start_mysql, args.browser)

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
