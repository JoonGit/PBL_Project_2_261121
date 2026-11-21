"""화면 공통: 자원 재사용, 워커 자동 실행, 모양(CSS)과 작은 표시 조각."""
from __future__ import annotations

import atexit
import hashlib
import html
import os
import subprocess
import sys
import time
import uuid
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from PIL import Image

from futuremarketer.config import PROJECT_ROOT, load_settings
from futuremarketer.db import make_app_db
from futuremarketer.db_init import ensure_schema
from futuremarketer.defaults import STEP_LABEL, STEPS
from futuremarketer.errors import DomainError
from futuremarketer.services import settings_service
from futuremarketer.storage import FileStore, SecretStore
from futuremarketer.worker import worker_running

KST = ZoneInfo("Asia/Seoul")

CSS = """
<style>
:root{--fm-ink:#1b1b1d;--fm-sub:#55555a;--fm-muted:#77777c;--fm-line:rgba(27,27,29,.08);--fm-raise:#f1f1f2;
 --fm-accent:#ff5f1f;--fm-accent-ink:#b93c07;--fm-accent-soft:#ffe9df;--fm-ok:#16803c;--fm-bad:#c62828;--fm-warn:#8a5a00}
.block-container{max-width:1320px;padding-top:2.5rem;padding-bottom:4rem}
[class^="fm-"],[class*=" fm-"]{word-break:keep-all;overflow-wrap:anywhere}
h1{letter-spacing:-.02em}
/* 주황 버튼 위 글자는 먹색(흰 글자는 대비가 부족하다) */
[data-testid="stBaseButton-primary"],[data-testid="stBaseButton-primaryFormSubmit"]{color:var(--fm-ink)!important;font-weight:600}
[data-testid="stBaseButton-primary"]:disabled,[data-testid="stBaseButton-primaryFormSubmit"]:disabled{color:var(--fm-muted)!important}
[data-testid="stSidebar"] [data-testid="stSidebarNavLink"]{min-height:36px}
.fm-title{font-size:28px;line-height:1.15;font-weight:700;color:var(--fm-ink);margin:0;letter-spacing:-.02em}
.fm-lead{font-size:15px;color:var(--fm-sub);margin:8px 0 0;max-width:44rem}
.fm-head{margin:0 0 24px}
.fm-sec{font-size:16px;font-weight:600;color:var(--fm-ink);margin:0}
.fm-note{font-size:13px;line-height:20px;color:var(--fm-muted);margin:4px 0 0}
.fm-name{font-size:15px;line-height:22px;font-weight:500;color:var(--fm-ink);margin:0;word-break:keep-all}
.fm-meta{font-size:13px;line-height:20px;color:var(--fm-muted);margin:0}
.fm-why{font-size:13px;line-height:20px;color:var(--fm-sub);margin:4px 0 0}
.fm-why.bad{color:var(--fm-bad)}
.fm-price{font-size:15px;font-weight:500;color:var(--fm-ink);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.fm-pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:12px;font-size:12px;
 font-weight:500;background:var(--fm-raise);color:var(--fm-sub);white-space:nowrap}
.fm-dot{width:6px;height:6px;border-radius:3px;background:var(--fm-muted);flex:none}
.fm-pill.READY .fm-dot,.fm-pill.DONE .fm-dot,.fm-pill.ok .fm-dot{background:var(--fm-ok)}
.fm-pill.READY,.fm-pill.DONE,.fm-pill.ok{color:var(--fm-ink)}
.fm-pill.RUNNING{background:var(--fm-accent-soft);color:var(--fm-accent-ink)}
.fm-pill.RUNNING .fm-dot{background:var(--fm-accent);animation:fm-live 1.6s ease-in-out infinite}
.fm-pill.FAILED .fm-dot,.fm-pill.bad .fm-dot{background:var(--fm-bad)}
.fm-pill.NEEDS_CHECK .fm-dot,.fm-pill.warn .fm-dot{background:#d08700}
@keyframes fm-live{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.fm-pill.RUNNING .fm-dot{animation:none}}
.fm-steps{display:flex;gap:4px;flex-wrap:wrap;margin:6px 0 0}
.fm-step{font-size:12px;line-height:20px;padding:0 8px;border-radius:6px;background:var(--fm-raise);color:var(--fm-muted)}
.fm-step.DONE{color:var(--fm-ink)}
.fm-step.RUNNING{background:var(--fm-accent-soft);color:var(--fm-accent-ink);font-weight:500}
.fm-step.FAILED{color:var(--fm-bad)}
.fm-step.UNKNOWN{color:var(--fm-warn)}
.fm-tile{background:#fff;border:1px solid var(--fm-line);border-radius:12px;padding:16px 20px;min-height:116px}
.fm-tile .k{font-size:13px;color:var(--fm-muted);margin:0}
.fm-tile .v{font-size:28px;line-height:1;font-weight:500;color:var(--fm-ink);margin:12px 0 0;font-variant-numeric:tabular-nums}
.fm-tile .v small{font-size:13px;font-weight:400;color:var(--fm-muted);margin-left:4px}
.fm-tile .s{font-size:15px;font-weight:500;color:var(--fm-ink);margin:8px 0 0}
.fm-tile .d{font-size:13px;color:var(--fm-muted);margin:4px 0 0}
.fm-log{font-size:13px;line-height:20px;color:var(--fm-sub);margin:0;display:flex;gap:8px}
.fm-log time{color:var(--fm-muted);font-variant-numeric:tabular-nums;flex:none}
.fm-log.DONE span{color:var(--fm-ink)}.fm-log.ERROR span{color:var(--fm-bad)}.fm-log.WARN span{color:var(--fm-warn)}
.fm-job{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:8px 0;border-top:1px solid var(--fm-line)}
.fm-job:first-child{border-top:0;padding-top:0}
.fm-noshot{aspect-ratio:5/4;border-radius:8px;background:var(--fm-raise);color:var(--fm-muted);font-size:12px;
 display:flex;align-items:center;justify-content:center}
.fm-hist-head{display:flex;align-items:center;gap:8px;margin:0 0 8px}
.fm-right{text-align:right}
[data-testid="stImage"] img{border-radius:8px;border:1px solid var(--fm-line)}
.fm-noimg{width:48px;height:48px;border-radius:8px;background:var(--fm-raise);color:var(--fm-muted);font-size:12px;
 display:flex;align-items:center;justify-content:center}
.fm-alert{padding:12px 16px;border-radius:10px;font-size:14px;line-height:22px;background:#eaf5ee;color:var(--fm-ink);margin:0 0 8px}
.fm-alert.error{background:#fdecec;color:var(--fm-bad)}
.fm-alert.warning{background:#fff4dd;color:var(--fm-warn)}
.fm-alert.info{background:var(--fm-raise);color:var(--fm-sub)}
.fm-empty{font-size:14px;color:var(--fm-muted);padding:24px 0;margin:0}
.fm-brand{font-size:17px;font-weight:600;color:#f2f2f3;margin:0;letter-spacing:-.01em}
.fm-brand-sub{font-size:12px;color:#a1a1a6;margin:2px 0 0}
.fm-side{font-size:12px;line-height:20px;color:#a1a1a6;margin:0;display:flex;align-items:center;gap:6px}
.fm-side-box{margin-top:24px;padding-top:16px;border-top:1px solid #343437}
.fm-side .fm-dot{background:#85858b}.fm-side.on .fm-dot{background:#4cc38a}
</style>
"""


# --- 자원 -----------------------------------------------------------------
@st.cache_resource
def _resources():
    settings = load_settings()
    return settings, make_app_db(settings), FileStore(settings.data_dir), SecretStore(settings.secrets_path)


def settings():
    return _resources()[0]


def engine():
    """Engine 만 재사용한다. Session 은 서비스 호출마다 새로 열고 닫는다."""
    return _resources()[1]


def store() -> FileStore:
    return _resources()[2]


def secrets() -> SecretStore:
    return _resources()[3]


# --- 시작 준비 --------------------------------------------------------------
def trim_log(path: Path, limit: int = 5 * 1024 * 1024, keep: int = 512 * 1024) -> None:
    """워커 기록 파일이 너무 커지면, 띄우기 전에 마지막 부분만 worker.log.1 에 옮겨 두고 새로 시작한다."""
    try:
        if not path.exists() or path.stat().st_size <= limit:
            return
        with open(path, "rb") as handle:
            handle.seek(-keep, os.SEEK_END)
            tail = handle.read()
        path.with_name(path.name + ".1").write_bytes(tail)
        path.write_bytes(b"")
    except OSError:
        pass


def start_worker(wait: bool = False) -> bool:
    """워커가 꺼져 있으면 띄운다. 화면과 같은 묶음으로 떠서, 화면을 끄면 함께 꺼진다.

    wait=True 면 워커가 실제로 뜰 때까지(잠금을 잡을 때까지) 몇 초 기다린다.
    """
    cfg = settings()
    if os.environ.get("FM_NO_AUTOSTART") == "1" or worker_running(cfg.data_dir):
        return False
    log_path = cfg.data_dir / "logs" / "worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    trim_log(log_path)
    with open(log_path, "a") as log:
        proc = subprocess.Popen([sys.executable, "-m", "futuremarketer.worker"], cwd=PROJECT_ROOT,
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    deadline = time.monotonic() + (4 if wait else 0)
    while time.monotonic() < deadline and not worker_running(cfg.data_dir) and proc.poll() is None:
        time.sleep(0.2)
    return True


@st.cache_resource
def _boot() -> str:
    """앱이 처음 뜰 때 한 번: 표가 없으면 만들고 워커를 띄운다."""
    state = ensure_schema(engine(), settings().target_code)
    if state != "outdated":
        # 등록 대상(실제/연습용)을 바꿨다면 워커가 뜨기 전에 연결 기록부터 맞춘다
        settings_service.sync_target(engine(), settings().target_code)
        start_worker(wait=True)          # 첫 화면의 「자동화 엔진」 표시가 맞도록 워커가 뜰 때까지 잠깐 기다린다
    return state


def boot() -> bool:
    """준비가 됐으면 True. 안 됐으면 이유를 화면에 보여 주고 False."""
    inject_css()
    try:
        state = _boot()
    except Exception as exc:
        _boot.clear()
        page_header("시작할 수 없습니다", "저장소를 열지 못했습니다")
        if isinstance(exc, (DomainError, ValueError)):
            st.caption(str(exc))
        elif load_settings().db_backend == "mysql":
            st.code("docker compose up -d", language="bash")
            st.caption(f"MySQL을 켠 뒤 새로 고쳐 주세요. ({type(exc).__name__})")
        else:
            st.caption(f"로컬 JSON 파일과 데이터 폴더의 쓰기 권한을 확인하세요. ({type(exc).__name__})")
        return False
    if state == "outdated":
        _boot.clear()
        page_header("시작할 수 없습니다", "저장 형식이 지금 코드와 다릅니다")
        st.caption("기존 기록을 백업한 뒤 해당 저장소를 만든 프로젝트 버전을 확인하세요.")
        return False
    return True


# --- 모양 -----------------------------------------------------------------
def inject_css() -> None:
    st.html(CSS)


def esc(value) -> str:
    """엑셀에서 온 글자는 그대로 HTML 에 넣지 않는다."""
    return html.escape("" if value is None else str(value))


def page_header(title: str, lead: str | None = None) -> None:
    lead_html = f'<p class="fm-lead">{esc(lead)}</p>' if lead else ""
    st.html(f'<div class="fm-head"><h1 class="fm-title">{esc(title)}</h1>{lead_html}</div>')


def section(title: str, note: str | None = None) -> None:
    note_html = f'<p class="fm-note">{esc(note)}</p>' if note else ""
    st.html(f'<p class="fm-sec">{esc(title)}</p>{note_html}')


def pill(state: str, label: str) -> str:
    return f'<span class="fm-pill {esc(state)}"><span class="fm-dot"></span>{esc(label)}</span>'


def steps_html(steps: dict | None) -> str:
    """글 → 이미지 → 가격 → 소식. 지금 어디까지 왔는지 한 줄로."""
    if not steps:
        return ""
    parts = []
    for step in STEPS:
        result = steps.get(step) or {}
        status, label = result.get("status", "PENDING"), STEP_LABEL[step]
        if step in ("PRICE", "NEWS") and result.get("skipped"):
            status, label = "SKIPPED", f"{label} 건너뜀"
        parts.append(f'<span class="fm-step {esc(status)}">{esc(label)}</span>')
    return f'<div class="fm-steps">{"".join(parts)}</div>'


def tile(label: str, value: str, unit: str = "", detail: str = "") -> str:
    unit_html = f"<small>{esc(unit)}</small>" if unit else ""
    detail_html = f'<p class="d">{esc(detail)}</p>' if detail else ""
    return f'<div class="fm-tile"><p class="k">{esc(label)}</p><p class="v">{esc(value)}{unit_html}</p>{detail_html}</div>'


def status_tile(label: str, state: str, status: str, detail: str = "") -> str:
    detail_html = f'<p class="d">{esc(detail)}</p>' if detail else ""
    return (f'<div class="fm-tile"><p class="k">{esc(label)}</p>'
            f'<p class="s">{pill(state, status)}</p>{detail_html}</div>')


# --- 값 표시 ----------------------------------------------------------------
def kst(dt, fmt: str = "%m월 %d일 %H:%M") -> str:
    if not dt:
        return "-"
    return dt.replace(tzinfo=timezone.utc).astimezone(KST).strftime(fmt)


def won(value) -> str:
    return f"{int(value):,}원" if value is not None else "가격 없음"


def thumb(key: str | None, size: int = 160) -> str | None:
    """목록용 작은 그림. 한 번 만들면 앱 데이터 폴더의 thumbs 에 두고 다시 쓴다."""
    if not key:
        return None
    source = store().path_for(key)
    if not source.is_file():
        return None
    target = store().root / "thumbs" / f"{hashlib.sha1(f'{key}:{size}'.encode()).hexdigest()}.jpg"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as img:
            img = img.convert("RGB")
            side = min(img.size)
            left, top = (img.width - side) // 2, (img.height - side) // 2
            img.crop((left, top, left + side, top + side)).resize((size, size)).save(target, "JPEG", quality=82)
    return str(target)


def preview(key: str | None, width: int = 720) -> str | None:
    """브라우저 화면 캡처를 목록에 보일 크기로 줄인 그림(자르지 않는다). 앱 데이터 폴더의 thumbs 에 두고 다시 쓴다."""
    if not key:
        return None
    source = store().path_for(key)
    if not source.is_file():
        return None
    target = store().root / "thumbs" / f"{hashlib.sha1(f'{key}:w{width}'.encode()).hexdigest()}.jpg"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(source) as img:
                img = img.convert("RGB")
                if img.width > width:
                    img = img.resize((width, round(img.height * width / img.width)))
                img.save(target, "JPEG", quality=80)
        except (OSError, ValueError):       # 깨진 파일이면 그림 없이 보여 준다
            return None
    return str(target)


# --- 요청키·알림 --------------------------------------------------------------
def request_key(name: str) -> str:
    """같은 클릭이 다시 실행(rerun)돼도 같은 값. 처리가 끝나면 rotate 로 새 값을 준비한다."""
    key = f"rk::{name}"
    if key not in st.session_state:
        st.session_state[key] = str(uuid.uuid4())
    return st.session_state[key]


def rotate(name: str) -> None:
    st.session_state.pop(f"rk::{name}", None)


def flash(message: str, kind: str = "success") -> None:
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    """알림 한 줄. 상품명 같은 엑셀 글자가 섞이므로 마크다운으로 해석하지 않고 글자 그대로 보여 준다."""
    item = st.session_state.pop("flash", None)
    if item:
        st.html(f'<div class="fm-alert {esc(item[0])}" role="status">{esc(item[1])}</div>')


def error_text(exc: Exception) -> str:
    if isinstance(exc, DomainError):
        hint = exc.detail.get("hint") if exc.detail else None
        return f"{exc.message} {hint}" if hint else exc.message
    return f"처리하지 못했습니다({type(exc).__name__})."


def relative_path(path: Path) -> str:
    """프로젝트 안이면 상대 경로로, 집 폴더 안이면 ~ 로 줄여 보여 준다."""
    path = Path(path).resolve()
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        pass
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)
