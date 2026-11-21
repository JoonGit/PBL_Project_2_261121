"""대시보드의 「열기」 버튼: 링크를 평소 브라우저가 아니라 로그인된 Aside 창으로 연다."""
from __future__ import annotations

from ..adapters.aside import open_detached
from ..adapters.daangn import practice_urls, real_urls
from ..config import Settings
from ..db import fetch_one, session_scope

MESSAGE = {
    "OPENED": "Aside 창으로 엽니다.",
    "BUSY": "등록 작업이 Aside 를 쓰는 중입니다. 작업이 끝난 뒤 다시 눌러 주세요.",
    "FAILED": "Aside 를 열지 못했습니다. 잠시 뒤 다시 눌러 주세요.",
    "MISSING": "Aside 브라우저를 찾지 못했습니다. 계정 설정 또는 .env 의 FM_ASIDE_PATH 를 확인하세요.",
    "REFUSED": "당근 주소가 아니라 열지 않았습니다.",
}


def working(engine) -> bool:
    """워커가 지금 브라우저를 쓰는 중인가(등록 작업이 돌고 있거나 당근 연결을 진행 중)."""
    with session_scope(engine) as s:
        jobs = fetch_one(s, "SELECT COUNT(*) AS n FROM task_job WHERE status = 'RUNNING'")["n"]
        conn = fetch_one(s, "SELECT control_status FROM channel_connection WHERE id = 1")
    return bool(jobs) or (conn or {}).get("control_status") in ("REQUESTED", "RUNNING")


def open_in_aside(settings: Settings, engine, url: str | None) -> tuple[str, str]:
    """(결과 코드, 사용자에게 보일 문장). 연결된 당근(또는 연습용) 주소만 연다."""
    urls = practice_urls(settings.practice_base_url) if settings.daangn_target == "practice" else real_urls()
    if not url or not any(url.startswith(base + "/") for base in (urls.base, urls.public)):
        return "REFUSED", MESSAGE["REFUSED"]
    if working(engine):          # 작업 중인 창을 건드리지 않는다. 브라우저가 떠 있다는 사실만으로는 막지 않는다
        return "BUSY", MESSAGE["BUSY"]
    code = open_detached(settings.aside_path, settings.browser_profile_dir, settings.cdp_port, settings.window,
                         url, settings.browser_headless)
    return code, MESSAGE[code]
