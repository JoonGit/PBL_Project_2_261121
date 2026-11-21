"""한 번 틀렸던 자리를 다시 틀리지 않게 하는 시험."""
from __future__ import annotations

import subprocess
import sys
import time
import zipfile
from datetime import timedelta

import pytest

from futuremarketer.adapters import aside
from futuremarketer.adapters.daangn import DaangnSite, SiteError, practice_urls
from futuremarketer.adapters.text_ai import AiError
from futuremarketer.config import PROJECT_ROOT
from futuremarketer.content import foreign_amounts
from futuremarketer.db import execute, fetch_one, session_scope
from futuremarketer.services import import_service, query_service, register_service, settings_service
from futuremarketer.util import utcnow
from tests.conftest import TEST_BACKEND, connect, make_image, make_xlsx, run_until_idle, set_ai
from tests.test_flow import SCRIPT, enqueue, job_of, prepare, product_of, query_rows


def load(engine, store, settings, rows):
    return import_service.load_products(engine, store, settings, data=make_xlsx(rows), file_name="상품목록.xlsx", source="UPLOAD")


# --- 등록 상태는 「지금 연결된 곳」에 대한 것 ---------------------------------------
def test_registered_on_practice_is_not_registered_on_another_profile(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "안경", 950000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    assert query_rows(engine, secrets, settings)["P1"]["state"] == "DONE"

    connect(engine, biz_id="2002")                       # 다른 비즈프로필(또는 연습용 → 실제)로 연결
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "READY" and row["selectable"] and row["job"] is None
    out = enqueue(engine, secrets, settings, {"P1": row}, ["P1"], run_key="run-2")
    assert out["queued"] == ["P1"]
    run_until_idle(worker)
    assert [p[0] for p in hub.prices] == ["1001", "2002"]
    assert job_of(engine, "P1")["request_json"]["biz_id"] == "2002"

    connect(engine, biz_id="1001")                       # 원래 프로필로 돌아오면 그곳의 「등록 완료」가 그대로 있다
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "DONE" and not row["selectable"]
    again = enqueue(engine, secrets, settings, {"P1": row}, ["P1"], run_key="run-3")
    assert again["queued"] == [] and again["skipped"][0]["reason"] == "이미 등록한 상품입니다."
    assert run_until_idle(worker) == 0 and [p[0] for p in hub.prices] == ["1001", "2002"]   # 같은 곳에 두 번 올라가지 않는다


def test_trying_elsewhere_and_cancelling_keeps_the_original_record(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "안경", 950000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    connect(engine, biz_id="2002")
    enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P1"], run_key="run-2")
    assert register_service.cancel_queued(engine) == 1    # 다른 곳에 접수만 하고 취소
    assert query_rows(engine, secrets, settings)["P1"]["state"] == "READY"
    connect(engine, biz_id="1001")
    assert query_rows(engine, secrets, settings)["P1"]["state"] == "DONE"


def test_job_never_runs_against_a_different_account(engine, store, settings, secrets, worker, hub, text_ai):
    rows = prepare(engine, store, settings, secrets, [
        ["P1", "머그컵", 12000, "350ml 도자기", None, None, None], ["P2", "안경", 950000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["P1"])
    connect(engine, biz_id="2002")                        # 접수한 뒤 다른 계정으로 바뀜
    run_until_idle(worker)
    job = job_of(engine, "P1")
    assert job["status"] == "FAILED" and job["error_code"] == "PLACE_CHANGED"
    assert text_ai.calls == 0 and hub.prices == [] and hub.opened == 0     # AI 도 브라우저도 쓰지 않고 멈춘다
    assert query_rows(engine, secrets, settings)["P1"]["state"] == "READY"  # 지금 계정에서는 새로 등록할 수 있다

    connect(engine, biz_id="1001")
    hub.fail["price_after"] = SiteError("RESULT_UNKNOWN", "가격 저장 결과를 확인하지 못했습니다.", unknown=True)
    enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P2"], run_key="run-2")
    run_until_idle(worker)
    needs = job_of(engine, "P2")
    assert needs["status"] == "NEEDS_CHECK"
    connect(engine, biz_id="2002")                        # 확인 필요 상태에서 다른 계정으로 바뀜
    out = register_service.resolve_check(engine, needs["id"], outcome="DONE")
    assert out["result"] == "FAILED" and "연결" in out["message"]
    assert run_until_idle(worker) == 0 and hub.posts == []                  # 남은 소식 단계를 다른 계정에 올리지 않는다
    connect(engine, biz_id="1001")
    row = query_rows(engine, secrets, settings)["P2"]
    assert row["state"] == "FAILED" and row["selectable"]                   # 원래 계정으로 돌아오면 이어서 할 수 있다
    enqueue(engine, secrets, settings, {"P2": row}, ["P2"], run_key="run-3")
    run_until_idle(worker)
    assert job_of(engine, "P2")["status"] == "SUCCEEDED" and [p["biz_id"] for p in hub.posts] == ["1001"]
    assert hub.prices == []                                                 # 사람이 「올라가 있음」으로 확인한 가격 단계는 다시 하지 않는다


def test_failed_job_is_not_resumed_on_another_profile(engine, store, settings, secrets, worker, hub, text_ai):
    rows = prepare(engine, store, settings, secrets, [["P1", "머그컵", 12000, "350ml 도자기", None, None, None]])
    text_ai.override = ("머그컵", "지금 사면 5,000원에 드립니다.")          # 입력에 없는 금액 → 실패
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    first = job_of(engine, "P1")
    assert first["status"] == "FAILED"

    text_ai.override = None
    new_prompt = "반말로 짧게 씁니다. 세 문장을 넘기지 않습니다. 과장하지 않습니다."
    settings_service.save_prompt(engine, "TEXT", new_prompt)
    out = enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P1"], run_key="run-2")
    assert out["queued"] == ["P1"]
    run_until_idle(worker)
    resumed = job_of(engine, "P1")
    assert resumed["id"] == first["id"] and resumed["status"] == "SUCCEEDED"
    assert text_ai.last["guideline"] == new_prompt       # 이어서 할 때는 지금의 지침을 쓴다

    rows = prepare(engine, store, settings, secrets, [["P2", "접시", 9000, "도자기 접시", None, None, None]])
    hub.fail["price_before"] = SiteError("ELEMENT_NOT_FOUND", "못 찾음")
    enqueue(engine, secrets, settings, rows, ["P2"], run_key="run-3")
    run_until_idle(worker)
    failed = job_of(engine, "P2")
    connect(engine, biz_id="2002")
    enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P2"], run_key="run-4")
    run_until_idle(worker)
    fresh = job_of(engine, "P2")
    assert fresh["id"] != failed["id"] and fresh["request_json"]["biz_id"] == "2002"   # 다른 곳이면 새 작업
    assert hub.prices[-1][0] == "2002"


# --- 다시 불러와도 「등록 완료」는 지워지지 않는다 -----------------------------------
def test_reload_after_registration_never_requeues_silently(engine, store, settings, secrets, worker, hub):
    make_image(settings.image_dir / "a.png")
    base = ["P1", "안경", 950000, None, None, SCRIPT, "a.png"]
    rows = prepare(engine, store, settings, secrets, [base])
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    assert len(hub.posts) == 1

    (settings.image_dir / "a.png").rename(settings.image_dir / "치움.png")          # 사진을 잠깐 치우고 불러오기
    load(engine, store, settings, [base])
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "DONE" and row["changed"] and not row["selectable"]
    (settings.image_dir / "치움.png").rename(settings.image_dir / "a.png")          # 되돌리고 다시 불러오기
    load(engine, store, settings, [base])
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "DONE" and not row["changed"] and not row["selectable"]

    edited = ["P1", "안경", 950000, None, None, SCRIPT + "\n오타를 고쳤습니다.", "a.png"]
    load(engine, store, settings, [edited])
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "DONE" and row["changed"] and not row["selectable"]
    assert row["registered_at"] is not None
    out = enqueue(engine, secrets, settings, {"P1": row}, ["P1"], run_key="run-2")
    assert out["queued"] == [] and out["skipped"][0]["reason"] == "이미 등록한 상품입니다."
    assert run_until_idle(worker) == 0 and len(hub.posts) == 1                       # 당근에 두 번 올라가지 않는다

    register_service.reopen(engine, row["id"])                                        # 사람이 「다시 올리겠다」고 골랐을 때만
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "READY" and row["selectable"] and not row["changed"]
    enqueue(engine, secrets, settings, {"P1": row}, ["P1"], run_key="run-3")
    run_until_idle(worker)
    assert len(hub.posts) == 2 and hub.posts[1]["body"].endswith("오타를 고쳤습니다.")
    with pytest.raises(Exception):
        register_service.reopen(engine, "없는-상품")


# --- 워커 ------------------------------------------------------------------------
def test_orphan_running_job_is_cleaned_up_without_restart(engine, store, settings, secrets, worker, hub, monkeypatch):
    rows = prepare(engine, store, settings, secrets, [["P1", "안경", 950000, None, None, SCRIPT, None]])
    hub.fail["price_before"] = SiteError("ELEMENT_NOT_FOUND", "못 찾음")
    enqueue(engine, secrets, settings, rows, ["P1"])
    original = worker._finish
    monkeypatch.setattr(worker, "_finish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DB 끊김")))
    with pytest.raises(RuntimeError):
        worker.run_once()                                   # 결과를 쓰지 못하고 빠져나감
    assert job_of(engine, "P1")["status"] == "RUNNING"
    monkeypatch.setattr(worker, "_finish", original)
    assert worker.run_once() is True                        # 다음 한가한 틈에 정리한다(앱을 다시 켜지 않아도)
    job = job_of(engine, "P1")
    assert job["status"] == "FAILED" and job["error_code"] == "INTERRUPTED"
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "FAILED" and row["selectable"]


def test_stale_disconnect_request_leaves_connection_alone(engine, settings, secrets, worker, removed_profiles):
    connect(engine)
    settings_service.request_disconnect(engine)
    with session_scope(engine) as s:
        execute(s, "UPDATE channel_connection SET control_requested_at = :t", t=utcnow() - timedelta(minutes=10))
    assert worker.process_control() is True
    conn = settings_service.overview(engine, secrets, settings)["connection"]
    assert conn["status"] == "CONNECTED" and conn["control_status"] == "FAILED" and removed_profiles == []


def test_failed_first_connect_leaves_no_login_data(engine, settings, secrets, worker, hub, removed_profiles):
    hub.fail["login"] = SiteError("LOGIN_TIMEOUT", "시간 안에 로그인이 확인되지 않았습니다.")
    settings_service.request_connect(engine)
    worker.process_control()
    assert removed_profiles == [settings.browser_profile_dir]      # 「연결 안 됨」이면 로그인 흔적도 없다


def test_disconnect_removes_captures(engine, settings, secrets, store, worker):
    connect(engine)
    store.write("shots/job/1.jpg", b"x")
    store.write("diagnostics/a.html", b"x")
    store.write("generated/keep.png", b"x")
    settings_service.request_disconnect(engine)
    worker.process_control()
    assert not (settings.data_dir / "shots").exists() and not (settings.data_dir / "diagnostics").exists()
    assert store.exists("generated/keep.png")                       # 내가 만든 그림은 남긴다


def test_target_switch_forgets_connection_and_old_login(engine, settings, secrets, store, hub, removed_profiles):
    from dataclasses import replace
    from futuremarketer.worker import Worker
    connect(engine)                                                  # 연습용으로 연결돼 있던 상태
    (settings.private_dir / "browser-profile-practice").mkdir(parents=True)
    real = replace(settings, daangn_target="real")
    assert settings_service.sync_target(engine, "REAL") is True      # 화면이 뜰 때 먼저 맞춘다
    settings_service.request_connect(engine)                         # 사용자가 곧바로 연결을 눌렀다
    Worker(engine, real, store, secrets, site_factory=hub.factory, profile_remover=lambda p: removed_profiles.append(p) or True,
           sleep=lambda _s: None).recover_on_start()                 # 뒤늦게 뜬 워커가
    conn = settings_service.overview(engine, secrets, real)["connection"]
    assert conn["target"] == "REAL" and conn["biz_id"] is None
    assert conn["control_status"] == "REQUESTED" and conn["status"] == "CONNECTING"   # 그 요청을 지우지 않는다
    assert removed_profiles == [settings.private_dir / "browser-profile-practice"]    # 예전 대상의 로그인 정보는 지운다


def test_disconnected_state_never_keeps_a_profile(engine, settings, secrets, store, hub, removed_profiles):
    from futuremarketer.worker import Worker
    settings.browser_profile_dir.mkdir(parents=True)                 # 「연결 안 됨」인데 프로필이 남아 있던 경우
    worker = Worker(engine, settings, store, secrets, site_factory=hub.factory,
                    profile_remover=lambda p: removed_profiles.append(p) or True, sleep=lambda _s: None)
    worker.recover_on_start()
    assert removed_profiles == [settings.browser_profile_dir]
    removed_profiles.clear()
    connect(engine)
    worker.recover_on_start()
    assert removed_profiles == []                                    # 연결돼 있으면 건드리지 않는다


# --- 목록 ------------------------------------------------------------------------
def test_needs_check_stays_visible_after_loading_another_file(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "안경", 950000, None, None, SCRIPT, None]])
    hub.fail["news_after"] = SiteError("RESULT_UNKNOWN", "소식 등록 결과를 확인하지 못했습니다.", unknown=True)
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    load(engine, store, settings, [["Q1", "다른 상품", 1000, None, None, SCRIPT, None]])
    rows = query_rows(engine, secrets, settings)
    assert set(rows) == {"Q1", "P1"} and rows["P1"]["state"] == "NEEDS_CHECK" and rows["P1"]["other_file"]
    assert query_service.dashboard(engine, secrets, settings)["counts"]["NEEDS_CHECK"] == 1
    register_service.resolve_check(engine, rows["P1"]["job"]["id"], outcome="DONE")
    assert set(query_rows(engine, secrets, settings)) == {"Q1"}      # 정리되면 지금 파일의 상품만 보인다


# --- 브라우저 프로세스·쿠키 ---------------------------------------------------------
@pytest.mark.parametrize("code", [
    "import time; time.sleep(60)",
    # 끝내 달라는 신호(SIGTERM)를 무시하는 프로세스: Aside 본체가 한동안 그렇다
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
def test_leftover_browser_is_found_and_stopped(tmp_path, code):
    profile = tmp_path / "profile"
    profile.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", code, f"--user-data-dir={profile}"])
    try:
        time.sleep(0.6)
        assert proc.pid in aside.profile_pids(profile)          # macOS 의 pgrep 도 찾는다
        assert aside.remove_profile(profile) is True
        assert proc.wait(timeout=5) is not None and not profile.exists()
    finally:
        if proc.poll() is None:
            proc.kill()


class FakeCookieContext:
    def __init__(self, cookies):
        self._cookies, self.added = cookies, []

    def cookies(self):
        return list(self._cookies)

    def add_cookies(self, items):
        self.added.extend(items)


class FakeBrowserWithContext:
    def __init__(self, context):
        self.context = context


def test_session_cookies_get_an_expiry_so_login_survives_closing_the_window():
    context = FakeCookieContext([
        {"name": "sid", "value": "v", "domain": "127.0.0.1", "path": "/", "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"},
        {"name": "long", "value": "v", "domain": "127.0.0.1", "path": "/", "expires": time.time() + 1000, "sameSite": "Lax"},
        {"name": "other", "value": "v", "domain": "tracker.example", "path": "/", "expires": -1, "sameSite": "Lax"},
    ])
    site = DaangnSite(FakeBrowserWithContext(context), practice_urls("http://127.0.0.1:1"))
    assert site._keep_session_cookies(days=30) == 1
    kept = context.added[0]
    assert kept["name"] == "sid" and kept["expires"] > time.time() + 29 * 86400 and kept["httpOnly"] is True


def test_cookies_are_kept_again_right_before_the_window_closes():
    """사이트가 등록 도중 쿠키를 새로 내려 주면 다시 「닫으면 사라지는」 쿠키가 된다. 닫기 직전에 한 번 더 남긴다."""
    context = FakeCookieContext([{"name": "sid", "value": "v2", "domain": "127.0.0.1", "path": "/", "expires": -1,
                                  "sameSite": "Lax"}])

    class Browser(FakeBrowserWithContext):
        closed = False

        def close(self):
            self.closed = True

    browser = Browser(context)
    site = DaangnSite(browser, practice_urls("http://127.0.0.1:1"))
    site.page = object()
    site.close()
    assert [c["value"] for c in context.added] == ["v2"] and browser.closed and site.page is None


# --- 글 검사 ----------------------------------------------------------------------
def test_amount_check_reads_ranges_and_ignores_non_money_words():
    info = "정가 약 180~190만 원대, 쿨거래 시 네고 가능"
    assert foreign_amounts("정가 180만 원대 제품입니다.", price=950000, info=info, name="안경") == []
    assert foreign_amounts("지름 30 원형 테이블입니다.", price=50000, info=None, name="테이블") == []
    assert foreign_amounts("단돈 3천 원 추가로 드려요.", price=50000, info=None, name="테이블") == [3000]
    assert foreign_amounts("5만원에 드립니다.", price=50000, info=None, name="테이블") == []
    assert foreign_amounts("지금 사면 500,000 원에 드립니다.", price=950000, info=None, name="안경") == [500000]
    assert foreign_amounts("1만5천원 상당의 사은품", price=950000, info=None, name="안경") == [15000]
    assert foreign_amounts("최대 30시간 재생, 가로 14 × 세로 25cm", price=950000, info=None, name="헤드폰") == []


def test_check_ai_reports_unexpected_errors_as_a_line(engine, settings, secrets, monkeypatch):
    settings_service.save_ai(engine, secrets, "GPT", mode="SUBSCRIPTION")

    class Boom:
        def check(self):
            raise subprocess.TimeoutExpired("codex", 30)

    monkeypatch.setattr(settings_service, "_checker", lambda *a, **k: Boom())
    out = settings_service.check_ai(engine, secrets, settings, "GPT")
    assert out["status"] == "ERROR" and "TimeoutExpired" in out["detail"]
    monkeypatch.setattr(settings_service, "_checker", lambda *a, **k: (_ for _ in ()).throw(AiError("AI_AUTH", "키 없음")))
    assert settings_service.check_ai(engine, secrets, settings, "GPT")["detail"] == "키 없음"


# --- 공유 묶음 --------------------------------------------------------------------
def test_share_bundle_leaves_local_traces_out(tmp_path):
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        import make_share_zip
    finally:
        sys.path.pop(0)
    target = tmp_path / "share.zip"
    assert make_share_zip.main([str(target)]) == 0
    names = zipfile.ZipFile(target).namelist()
    assert any(n.endswith("README.md") for n in names) and any(n.endswith("samples/상품목록.xlsx") for n in names)
    banned = (".venv/", "__pycache__/", ".pytest_cache/", "/var/")
    assert not [n for n in names if any(b in n for b in banned) or n.endswith((".pyc", "/.env", ".DS_Store"))]
    assert any(n.endswith(".env.example") for n in names)


# --- 표 구조 판 ---------------------------------------------------------------------
@pytest.mark.skipif(TEST_BACKEND == "json", reason="MySQL 스키마 메타데이터 시험")
def test_old_table_layout_is_detected(engine):
    from sqlalchemy import text

    from futuremarketer.db_init import ensure_schema
    assert ensure_schema(engine, "PRACTICE") == "ready"
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE channel_connection COMMENT = 'schema:2'"))
    assert ensure_schema(engine, "PRACTICE") == "outdated"       # 표 이름이 같아도 구조가 옛것이면 알아챈다


# --- 끝난 단계는 다시 하지 않는다(어떤 실패가 끼어들어도) ---------------------------------
def test_finished_steps_survive_a_failed_finish_and_a_lost_login(engine, store, settings, secrets, worker, hub, monkeypatch):
    rows = prepare(engine, store, settings, secrets, [["P1", "안경", 950000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["P1"])
    original = worker._finish
    monkeypatch.setattr(worker, "_finish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DB 끊김")))
    with pytest.raises(RuntimeError):
        worker.run_once()                       # 소식까지 올렸는데 마무리 기록만 실패
    monkeypatch.setattr(worker, "_finish", original)
    assert len(hub.posts) == 1
    worker.run_once()                           # 주인 없는 작업 정리 → 실패(중단)로 남음
    job = job_of(engine, "P1")
    assert job["status"] == "FAILED" and all(job["steps_json"][s]["status"] == "DONE" for s in job["steps_json"])

    row = query_rows(engine, secrets, settings)["P1"]
    enqueue(engine, secrets, settings, {"P1": row}, ["P1"], run_key="run-2")
    with session_scope(engine) as s:            # 다시 등록 직후 로그인이 풀린 상태가 됨
        execute(s, "UPDATE channel_connection SET status='EXPIRED'")
    run_until_idle(worker)
    job = job_of(engine, "P1")
    assert job["steps_json"]["NEWS"]["status"] == "DONE"          # 끝난 소식 단계가 「실패」로 덮이지 않는다
    assert job["status"] == "SUCCEEDED" and len(hub.posts) == 1   # 남은 일이 없으니 그대로 완료, 두 번 올라가지 않는다


def test_expired_login_still_shows_the_record_of_that_place(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "안경", 950000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    with session_scope(engine) as s:
        execute(s, "UPDATE product SET reg_status='NONE'")        # 가장 최근 상태가 달라져 있어도
        execute(s, "UPDATE channel_connection SET status='EXPIRED'")
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["state"] == "DONE" and row["registered_at"] is not None and not row["selectable"]
    assert query_service.dashboard(engine, secrets, settings)["news_today"] == 1


def test_numbers_next_to_a_price_are_not_added_to_it():
    assert foreign_amounts("모델 Px8 950,000원", price=950000, info=None, name="헤드폰") == []
    assert foreign_amounts("1+1 5,000원", price=5000, info=None, name="양말") == []
