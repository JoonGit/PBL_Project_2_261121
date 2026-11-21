"""환경 변수 기반 설정.

비밀값(API 키)과 로그인 정보(브라우저 프로필)는 프로젝트 폴더 밖의 개인 폴더에 둔다.
프로젝트 폴더를 통째로 공유해도 따라가지 않게 하기 위해서다.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_DB_URL = "mysql+pymysql://fm_app:fm_app_local@127.0.0.1:3317/futuremarketer?charset=utf8mb4"
APP_DIR_NAME = "futuremarketer-autopost"


def _default_private_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform == "win32":
        return _windows_local_app_data() / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME}"


def _windows_local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _default_aside_path() -> Path:
    if sys.platform != "win32":
        return Path("/Applications/Aside.app/Contents/MacOS/Aside")
    local = _windows_local_app_data()
    roots = [local / "Aside", local / "Programs" / "Aside"]
    roots.extend(Path(os.environ[name]) / "Aside" for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")
                 if os.environ.get(name))
    candidates = [root / subdir / "aside.exe" for root in roots for subdir in ("Application", "")]
    return next((path for path in candidates if path.is_file()), candidates[0])


def ai_cli_path(command: str) -> str | None:
    # Windows 배치 파일에는 상품명이나 지침을 셸 인자로 넘기지 않는다.
    executable = shutil.which(f"{command}.exe" if sys.platform == "win32" else command)
    if sys.platform == "win32" and executable and Path(executable).suffix.lower() != ".exe":
        return None
    return executable


def _path(value: str | None, fallback: Path) -> Path:
    return (Path(value).expanduser() if value else fallback).resolve()


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path            # 앱 데이터: 엑셀 사본·사진 사본·AI 이미지·화면 캡처·로그(기본: 개인 폴더 아래 data)
    private_dir: Path         # 프로젝트 밖 개인 폴더: 브라우저 프로필·API 키
    default_excel: Path       # 「불러오기」의 기본 연결 파일
    image_dir: Path           # 엑셀의 이미지 파일 이름을 찾는 폴더
    aside_path: Path          # Aside 브라우저 실행 파일
    cdp_port: int             # 워커가 Aside 를 조종할 때 쓰는 로컬 포트
    daangn_target: str        # real: 실제 당근 비즈니스 / practice: 내 PC 의 연습용 화면
    practice_port: int
    ai_mode: str              # live: 설정 화면의 연결을 사용 / mock: 연결 없이 흉내만(시험·오프라인 연습)
    claude_api_model: str = "claude-opus-5-5"
    claude_cli_model: str = "opus"
    openai_image_model: str = "gpt-image-2.5-sunburst"
    window: tuple[int, int, int, int] = (760, 60, 1120, 900)   # x, y, 너비, 높이
    browser_headless: bool = False    # True: 창 없이 조작(개발·시험). 기본은 자동화 화면이 보이게 창을 띄운다
    max_rows: int = 100
    max_images: int = 10
    max_image_bytes: int = 20 * 1024 * 1024
    max_ai_attempts: int = 3
    login_timeout_sec: int = 300
    step_timeout_sec: int = 20
    action_delay_sec: float = 0.5     # 화면 조작 사이에 쉬는 시간(사람이 따라 볼 수 있게)
    poll_interval_sec: float = 1.0
    idle_browser_close_sec: float = 4.0

    @property
    def browser_profile_dir(self) -> Path:
        """로그인 쿠키가 남는 전용 프로필. 실제·연습용을 섞지 않는다."""
        return self.private_dir / f"browser-profile-{self.daangn_target}"

    def profile_dir_for(self, target: str) -> Path:
        return self.private_dir / f"browser-profile-{target.lower()}"

    @property
    def target_code(self) -> str:
        return "PRACTICE" if self.daangn_target == "practice" else "REAL"

    @property
    def secrets_path(self) -> Path:
        return self.private_dir / "secrets.json"

    @property
    def practice_base_url(self) -> str:
        return f"http://127.0.0.1:{self.practice_port}"


def _window(value: str | None) -> tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(v) for v in (value or "").split(","))
        return x, y, w, h
    except ValueError:
        return Settings.window


def load_settings(**overrides) -> Settings:
    env = os.environ
    samples = PROJECT_ROOT / "samples"
    private_dir = _path(env.get("FM_PRIVATE_DIR"), _default_private_dir())
    values = dict(
        database_url=env.get("FM_DATABASE_URL") or DEFAULT_DB_URL,
        # 화면 캡처와 로그에는 계정 화면이 담길 수 있어, 프로젝트 폴더가 아니라 개인 폴더에 둔다
        data_dir=_path(env.get("FM_DATA_DIR"), private_dir / "data"),
        private_dir=private_dir,
        default_excel=_path(env.get("FM_DEFAULT_EXCEL"), samples / "상품목록.xlsx"),
        image_dir=_path(env.get("FM_IMAGE_DIR"), samples / "images"),
        aside_path=_path(env.get("FM_ASIDE_PATH"), _default_aside_path()),
        cdp_port=int(env.get("FM_CDP_PORT") or 9339),
        daangn_target=(env.get("FM_DAANGN_TARGET") or "real").lower(),
        practice_port=int(env.get("FM_PRACTICE_PORT") or 18765),
        ai_mode=(env.get("FM_AI_MODE") or "live").lower(),
        claude_api_model=env.get("FM_CLAUDE_API_MODEL") or Settings.claude_api_model,
        claude_cli_model=env.get("FM_CLAUDE_CLI_MODEL") or Settings.claude_cli_model,
        openai_image_model=env.get("FM_OPENAI_IMAGE_MODEL") or Settings.openai_image_model,
        window=_window(env.get("FM_BROWSER_WINDOW")),
        browser_headless=(env.get("FM_BROWSER_HEADLESS") or "").lower() in ("1", "true", "yes"),
    )
    values.update(overrides)
    return Settings(**values)
