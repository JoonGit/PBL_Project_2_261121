"""작업 기록(대시보드)과 실패 화면, 가격·소식에 상품 사진을 올리는 규칙."""
from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text

from futuremarketer.adapters.daangn import SiteError
from futuremarketer.content import fit_text, post_from_script
from futuremarketer.db import execute, fetch_all, fetch_one, session_scope
from futuremarketer.db_init import SCHEMA_VERSION, ensure_schema, schema_version
from futuremarketer.services import query_service, register_service
from tests.conftest import FAILURE_SCREEN, TEST_BACKEND, make_image, run_until_idle
from tests.test_flow import enqueue, job_of, prepare
from tests.test_ui_smoke import app_env, page_app  # noqa: F401  (화면 시험 준비를 같이 쓴다)

SCRIPT = "아끼던 헤드폰을 판매합니다.\n상태 좋습니다. 채팅 주세요."


def headphone(engine, store, settings, secrets, images=("h1.png", "h2.png", "h3.png")):
    for idx, name in enumerate(images):
        make_image(settings.image_dir / name, color=(20 * idx, 90, 160))
    return prepare(engine, store, settings, secrets,
                   [["P2", "바워스앤윌킨스 Px8 S2 무선 헤드폰", 950000, "새상품", None, SCRIPT, ", ".join(images)]])


def test_price_gets_the_first_photo_and_news_gets_every_photo(engine, store, settings, secrets, worker, hub):
    rows = headphone(engine, store, settings, secrets)
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    job = job_of(engine, "P2")
    keys = job["steps_json"]["IMAGE"]["image_keys"]
    assert job["status"] == "SUCCEEDED" and len(keys) == 3
    assert hub.price_photos == [store.path_for(keys[0])]                        # 가격표에는 대표 사진 한 장
    assert hub.posts[0]["images"] == [store.path_for(k) for k in keys]           # 소식에는 사진 전부
    assert job["steps_json"]["PRICE"]["photo"] is True


def test_failure_keeps_the_screen_and_every_attempt_stays_in_history(engine, store, settings, secrets, worker, hub):
    rows = headphone(engine, store, settings, secrets)
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "사진을 고르는 창이 열리지 않았습니다.")
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)

    first = query_service.history(engine)
    assert first["total"] == 1 and first["counts"] == {"FAILED": 1}
    failed = first["rows"][0]
    assert failed["outcome"] == "FAILED" and failed["step"] == "NEWS" and "사진을 고르는" in failed["message"]
    assert store.read(failed["shot_key"]) == FAILURE_SCREEN                     # 멈춘 순간의 화면이 붙는다
    assert [e["step"] for e in failed["events"]][:2] == ["SCRIPT", "IMAGE"]      # 그 시도의 진행 기록

    again = enqueue(engine, secrets, settings, rows, ["P2"], run_key="run-2")      # 실패한 상품을 다시 등록
    assert again["queued"] == ["P2"]
    run_until_idle(worker)
    history = query_service.history(engine)
    assert [r["outcome"] for r in history["rows"]] == ["SUCCEEDED", "FAILED"]     # 앞선 실패 줄이 지워지지 않는다
    done = history["rows"][0]
    assert done["shot_key"] and [e["step"] for e in done["events"]] == ["NEWS"] * (len(done["events"]) - 1) + [None]
    assert history["rows"][1]["id"] == failed["id"]

    problems = query_service.history(engine, show="PROBLEM")
    assert [r["outcome"] for r in problems["rows"]] == ["FAILED"] and not problems["more"]
    assert query_service.history(engine, limit=1)["more"] is True


def test_retry_of_an_old_job_attaches_the_price_photo(engine, store, settings, secrets, worker, hub):
    """사진 없이 가격만 올리던 예전 방식으로 끝난 가격 단계는, 다시 등록할 때 한 번 더 해서 사진을 붙인다."""
    rows = headphone(engine, store, settings, secrets)
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "사진을 첨부할 곳을 찾지 못했습니다.")
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    job = job_of(engine, "P2")
    steps = job["steps_json"]
    steps["PRICE"] = {"status": "DONE", "done_at": steps["PRICE"]["done_at"], "skipped": False}   # 예전 기록 모양
    with session_scope(engine) as s:
        execute(s, "UPDATE task_job SET steps_json = :st WHERE id = :id", st=__import__("json").dumps(steps), id=job["id"])

    enqueue(engine, secrets, settings, rows, ["P2"], run_key="run-2")
    run_until_idle(worker)
    assert job_of(engine, "P2")["status"] == "SUCCEEDED"
    assert len(hub.prices) == 2 and hub.price_photos[-1] is not None             # 가격 단계를 다시 해 사진을 붙였다


def test_finished_price_step_with_photo_is_not_redone(engine, store, settings, secrets, worker, hub):
    rows = headphone(engine, store, settings, secrets)
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "멈춤")
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    enqueue(engine, secrets, settings, rows, ["P2"], run_key="run-2")
    run_until_idle(worker)
    assert len(hub.prices) == 1 and len(hub.posts) == 1                          # 끝난 단계는 다시 하지 않는다


def test_human_resolution_and_cancel_are_recorded(engine, store, settings, secrets, worker, hub):
    rows = headphone(engine, store, settings, secrets)
    hub.fail["news_after"] = SiteError("RESULT_UNKNOWN", "소식 등록 결과를 확인하지 못했습니다.", unknown=True)
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    job = job_of(engine, "P2")
    assert query_service.history(engine)["rows"][0]["outcome"] == "NEEDS_CHECK"
    register_service.resolve_check(engine, job["id"], outcome="DONE")
    rows_now = query_service.history(engine)["rows"]
    assert [r["outcome"] for r in rows_now] == ["SUCCEEDED", "NEEDS_CHECK"]
    assert "사람이 확인" in rows_now[0]["message"]


def test_fit_text_cuts_at_word_boundaries():
    assert fit_text("크롬하츠 부바(BUBBA) 블랙/실버 안경", 15) == "크롬하츠 부바(BUBBA)"
    assert fit_text("바워스앤윌킨스 Px8 S2 무선 헤드폰", 15) == "바워스앤윌킨스 Px8 S2"
    assert fit_text("[새상품] 바워스앤윌킨스 Px8 S2 프리미엄 무선 헤드폰", 30) == "[새상품] 바워스앤윌킨스 Px8 S2 프리미엄 무선"
    assert fit_text("[정품] 크롬하츠 부바(BUBBA) 블랙/실버 안경 (실착 5회 미만, 풀박)", 30) == "[정품] 크롬하츠 부바(BUBBA) 블랙/실버 안경"
    assert fit_text("가나다라마바사아자차카타파하가나다", 15) == "가나다라마바사아자차카타파하가"   # 띄어쓰기가 없으면 그냥 자른다
    assert fit_text("  짧은   이름 ", 15) == "짧은 이름"
    assert len(post_from_script("상품", "[새상품] 바워스앤윌킨스 Px8 S2 프리미엄 무선 헤드폰", "본문")[0]) <= 30


@pytest.mark.skipif(TEST_BACKEND == "json", reason="MySQL 스키마 마이그레이션 시험")
def test_schema_3_database_is_upgraded_in_place(engine, store, settings, secrets, worker, hub):
    """한 판 아래(schema:3) DB 는 기록을 지우지 않고 올린다. 지난 기록의 끝난 줄에는 결과가 채워진다."""
    rows = headphone(engine, store, settings, secrets)
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "멈춤")
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    with engine.begin() as conn:                     # schema:3 으로 되돌린다(outcome 칸이 없던 때)
        conn.execute(text("ALTER TABLE job_event DROP INDEX ix_event_outcome, DROP COLUMN outcome"))
        conn.execute(text("ALTER TABLE channel_connection COMMENT = 'schema:3'"))
    assert schema_version(engine) == "schema:3"

    assert ensure_schema(engine, "PRACTICE") == "ready"
    assert schema_version(engine) == SCHEMA_VERSION
    history = query_service.history(engine)
    assert [r["outcome"] for r in history["rows"]] == ["FAILED"] and history["rows"][0]["step"] == "NEWS"
    with session_scope(engine) as s:
        assert fetch_one(s, "SELECT COUNT(*) AS n FROM task_job")["n"] == 1      # 기록이 그대로 남는다


def test_worker_slows_down_when_errors_repeat(engine, store, settings, secrets, worker):
    sleeps: list[float] = []

    class Stop(Exception):
        pass

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 8:
            raise Stop

    def broken():
        raise RuntimeError("Lost connection to MySQL server")

    worker.settings = dataclasses.replace(settings, poll_interval_sec=1.0)
    worker.sleep = fake_sleep
    worker.recover_on_start = lambda: None
    worker.run_once = broken
    with pytest.raises(Stop):
        worker.run_forever()
    assert sleeps == [2, 4, 8, 16, 32, 60, 60, 60]                               # 오류가 이어지면 점점 천천히


def test_dashboard_shows_history_with_the_failure_screen(app_env, engine, store, settings, secrets, worker, hub):  # noqa: F811
    rows = headphone(engine, store, settings, secrets)
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "사진을 고르는 창이 열리지 않았습니다.")
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)

    at = page_app("dashboard").run()
    assert not at.exception, at.exception
    page = " ".join(str(h.proto.body) for h in at.get("html"))
    assert "작업 기록" in " ".join(str(m.value) for m in at.markdown) or "작업 기록" in page
    assert "실패" in page and "사진을 고르는 창이 열리지 않았습니다." in page and "소식 단계에서 멈춤" in page
    assert len(at.get("image")) == 1                                             # 멈춘 순간의 화면
    expander = at.expander[0]
    assert expander.label.startswith("진행 기록")


def test_open_in_aside_refuses_foreign_urls_and_waits_only_while_the_worker_works(engine, settings, monkeypatch):
    from futuremarketer.services import open_service

    calls = []
    monkeypatch.setattr(open_service, "open_detached", lambda *a: calls.append(a) or "OPENED")
    cfg = dataclasses.replace(settings, daangn_target="real")
    good = "https://bizprofile.daangn.com/biz_accounts/1/manager/home"

    assert open_service.open_in_aside(cfg, engine, good)[0] == "OPENED"       # 작업이 없으면 열린다
    assert calls[0][4] == good and calls[0][1] == cfg.browser_profile_dir      # 로그인된 전용 프로필로
    assert open_service.open_in_aside(cfg, engine, "https://example.com/biz_accounts/1/manager/home")[0] == "REFUSED"
    assert open_service.open_in_aside(cfg, engine, None)[0] == "REFUSED" and len(calls) == 1

    with session_scope(engine) as s:                       # 연결 작업이 브라우저를 쓰는 중
        execute(s, "UPDATE channel_connection SET control_status='RUNNING' WHERE id=1")
    assert open_service.open_in_aside(cfg, engine, good)[0] == "BUSY" and len(calls) == 1
    with session_scope(engine) as s:
        execute(s, "UPDATE channel_connection SET control_status='DONE' WHERE id=1")
    assert open_service.open_in_aside(cfg, engine, good)[0] == "OPENED"


def test_open_detached_adds_a_window_to_a_leftover_browser(tmp_path, settings):
    """이전에 열어 둔 Aside 가 떠 있어도(원격 조종 포트가 열려 있어도) 막지 않고 새 창으로 연다."""
    if not settings.aside_path.exists():
        pytest.skip("Aside 가 없습니다")
    import json
    import urllib.request
    from futuremarketer.adapters.aside import AsideBrowser, open_detached, stop_profile_processes

    profile, port = tmp_path / "profile", 9355
    try:
        first = open_detached(settings.aside_path, profile, port, settings.window, "about:blank", headless=True)
        url = "data:text/html,<title>OpenedOK</title>"
        second = open_detached(settings.aside_path, profile, port, settings.window, url, headless=True)
        assert (first, second) == ("OPENED", "OPENED")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            assert any("OpenedOK" in p.get("title", "") + p.get("url", "") for p in json.loads(resp.read()))
        assert AsideBrowser(settings.aside_path, profile, port, settings.window).cdp_alive()   # 브라우저는 그대로
    finally:
        stop_profile_processes(profile)


def test_dashboard_links_use_share_address_and_public_profile(engine, store, settings, secrets, worker, hub):
    from futuremarketer.db import session_scope as scope
    from futuremarketer.repositories import core
    from futuremarketer.services import open_service

    rows = headphone(engine, store, settings, secrets)
    with scope(engine) as s:                                    # 처음에는 업체 프로필 번호를 모른다
        execute(s, "UPDATE channel_connection SET public_id=NULL WHERE id=1")
    cfg = dataclasses.replace(settings, daangn_target="real")
    assert query_service.dashboard(engine, secrets, cfg)["profile_url"] is None
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)                                      # 등록하면서 관리자 홈에서 번호를 읽어 둔다

    with scope(engine) as s:
        conn = core.connection(s)
    assert conn["public_id"] == "7001"
    data = query_service.dashboard(engine, secrets, cfg)
    assert data["profile_url"].endswith("/kr/local-profile/7001/")
    assert data["manage_url"].endswith("/biz_accounts/1001/manager/home")
    row = query_service.history(engine, settings=cfg)["rows"][0]
    assert row["news_url"].endswith("/kr/business-posts/abc123")                  # 소식의 공유 주소

    calls = []
    monkeypatch_open = lambda *a: calls.append(a) or "OPENED"   # noqa: E731
    original = open_service.open_detached
    open_service.open_detached = monkeypatch_open
    try:
        real = "https://www.daangn.com"
        assert open_service.open_in_aside(cfg, engine, f"{real}/kr/business-posts/abc123")[0] == "OPENED"  # 손님 화면도 연다
        assert open_service.open_in_aside(cfg, engine, f"{real}/kr/local-profile/7001/")[0] == "OPENED"
        assert open_service.open_in_aside(cfg, engine, "https://example.com/kr/x")[0] == "REFUSED"
    finally:
        open_service.open_detached = original
