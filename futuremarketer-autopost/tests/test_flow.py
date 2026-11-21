"""등록 흐름: 접수 → 글 → 이미지 → 가격 등록 → 소식 등록, 그리고 실패했을 때의 규칙."""
from __future__ import annotations

import io
import os
import stat

import pytest
from PIL import Image

from futuremarketer.adapters.daangn import SiteError
from futuremarketer.adapters.text_ai import AiError
from futuremarketer.db import execute, fetch_all, fetch_one, session_scope
from futuremarketer.defaults import DEFAULT_PROMPTS
from futuremarketer.errors import DomainError
from futuremarketer.services import import_service, query_service, register_service, settings_service
from futuremarketer.worker import Worker
from tests.conftest import connect, make_image, make_xlsx, run_until_idle, set_ai

SCRIPT = "아끼던 안경을 판매합니다.\n상태 좋습니다. 채팅 주세요."


def prepare(engine, store, settings, secrets, rows):
    import_service.load_products(engine, store, settings, data=make_xlsx(rows), file_name="상품목록.xlsx", source="UPLOAD")
    connect(engine)
    set_ai(engine, secrets)
    return {r["code"]: r for r in query_service.product_rows(engine, secrets, settings)["rows"]}


def enqueue(engine, secrets, settings, rows, codes, run_key="run-1"):
    return register_service.enqueue_register(engine, secrets, settings, [rows[c]["id"] for c in codes], run_key=run_key)


def job_of(engine, code):
    with session_scope(engine) as s:
        return fetch_one(s, "SELECT j.* FROM task_job j JOIN product p ON p.last_job_id = j.id"
                            " WHERE p.product_code = :c", c=code)


def product_of(engine, code):
    with session_scope(engine) as s:
        return fetch_one(s, "SELECT * FROM product WHERE product_code = :c", c=code)


# ---------------------------------------------------------------------------
def test_script_and_photos_are_used_as_is(engine, store, settings, secrets, worker, hub, text_ai, image_ai):
    make_image(settings.image_dir / "a.png")
    make_image(settings.image_dir / "b.png", color=(10, 200, 10))
    rows = prepare(engine, store, settings, secrets,
                   [["P1", "크롬하츠 안경", 950000, "실착 5회", "[정품] 크롬하츠 안경", SCRIPT, "a.png, b.png"]])
    out = enqueue(engine, secrets, settings, rows, ["P1"])
    assert out["queued"] == ["P1"] and not out["skipped"]
    assert product_of(engine, "P1")["reg_status"] == "QUEUED"

    assert run_until_idle(worker) >= 1
    job = job_of(engine, "P1")
    assert job["status"] == "SUCCEEDED" and product_of(engine, "P1")["reg_status"] == "DONE"
    assert text_ai.calls == 0 and image_ai.calls == 0                      # 대본·사진이 있으면 AI 를 부르지 않는다
    assert hub.prices == [("1001", "크롬하츠 안경", 950000)]
    post = hub.posts[0]
    assert post["title"] == "[정품] 크롬하츠 안경" and post["body"] == SCRIPT   # 한 글자도 바꾸지 않는다
    assert [p.read_bytes() for p in post["images"]] == [(settings.image_dir / n).read_bytes() for n in ("a.png", "b.png")]
    assert job["steps_json"]["SCRIPT"]["mode"] == "SCRIPT" and job["steps_json"]["IMAGE"]["mode"] == "LOCAL"
    with session_scope(engine) as s:
        events = fetch_all(s, "SELECT * FROM job_event WHERE job_id = :j ORDER BY id", j=job["id"])
    assert events[-1]["message"] == "등록을 마쳤습니다."
    assert any(e["shot_key"] and store.exists(e["shot_key"]) for e in events)   # 화면 캡처가 남는다
    assert hub.closed == 1                                                  # 줄이 비면 브라우저를 닫는다


def test_ai_writes_text_and_makes_image_right_before_registering(engine, store, settings, secrets, worker, hub,
                                                                  text_ai, image_ai):
    rows = prepare(engine, store, settings, secrets,
                   [["P2", "수달 장난감", 24900, "터그놀이\n노즈워크\n정상가 30,000원", None, None, None]])
    assert rows["P2"]["state"] == "READY"
    enqueue(engine, secrets, settings, rows, ["P2"])
    with session_scope(engine) as s:
        assert fetch_one(s, "SELECT COUNT(*) AS n FROM product_image")["n"] == 0   # 접수만으로는 그림을 만들지 않는다
    run_until_idle(worker)

    job = job_of(engine, "P2")
    assert job["status"] == "SUCCEEDED"
    assert text_ai.calls == 1 and image_ai.calls == 1
    assert text_ai.last["guideline"] == DEFAULT_PROMPTS["TEXT"] and image_ai.last["guideline"] == DEFAULT_PROMPTS["IMAGE"]
    post = hub.posts[0]
    assert "판매가격: 24,900원" not in post["body"]
    assert hub.prices[0][2] == 24900
    key = job["steps_json"]["IMAGE"]["image_keys"][0]
    assert key.startswith("generated/P2-") and post["images"] == [store.path_for(key)]
    saved = store.read(key)
    assert saved != image_ai.generate("", "수달 장난감", None).data         # 「AI 생성 이미지」 표시가 들어갔다
    with Image.open(io.BytesIO(saved)) as img:
        assert img.format == "PNG"
    with session_scope(engine) as s:
        row = fetch_one(s, "SELECT * FROM product_image WHERE source_type='GENERATED'")
    assert row["storage_key"] == key
    assert query_service.product_rows(engine, secrets, settings)["rows"][0]["thumb_key"] == key


def test_ai_text_with_unknown_amount_is_not_posted(engine, store, settings, secrets, worker, hub, text_ai):
    make_image(settings.image_dir / "a.png")
    rows = prepare(engine, store, settings, secrets, [["P3", "헤드폰", 950000, "정상가 1,139,000원", None, None, "a.png"]])
    text_ai.override = ("헤드폰 판매", "지금 사면 500,000원에 드립니다. 채팅 주세요.")
    enqueue(engine, secrets, settings, rows, ["P3"])
    run_until_idle(worker)
    job = job_of(engine, "P3")
    assert job["status"] == "FAILED" and job["error_code"] == "AI_OUTPUT" and "500,000원" in job["error_message"]
    assert hub.prices == [] and hub.posts == [] and hub.opened == 0

    text_ai.override = ("헤드폰 판매", "정상가 1,139,000원인 제품입니다. 채팅 주세요.")    # 입력에 있던 금액은 허용
    enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P3"], run_key="run-2")
    run_until_idle(worker)
    assert job_of(engine, "P3")["status"] == "SUCCEEDED"


def query_rows(engine, secrets, settings):
    return {r["code"]: r for r in query_service.product_rows(engine, secrets, settings)["rows"]}


@pytest.mark.parametrize("business_status", ["EDITOR_UNAVAILABLE", "RESTRICTED"])
def test_sales_wait_as_a_priceless_local_draft(engine, store, settings, secrets, worker, hub, text_ai, image_ai,
                                               business_status):
    rows = prepare(engine, store, settings, secrets, [["P1", "접이식 테이블", 45000, "70cm 원목 상판", None, None, None]])
    hub.business_status = business_status
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    assert not hub.prices and not hub.posts
    row = query_rows(engine, secrets, settings)["P1"]
    assert row["label"] == "게시 보류" and row["removal"] is None
    draft = row["job"]["preview"]
    assert "70cm 원목" in draft["body"] and "45,000" not in draft["body"]
    assert store.exists(draft["image_keys"][0]) and draft["title"]
    assert row["job"]["steps"]["NEWS"]["status"] == "PENDING"
    assert text_ai.calls == image_ai.calls == 1

    hub.business_status = "VERIFIED"
    enqueue(engine, secrets, settings, {"P1": row}, ["P1"], run_key="verified")
    run_until_idle(worker)
    assert text_ai.calls == image_ai.calls == 1
    assert len(hub.prices) == len(hub.posts) == 1
    assert "45,000원" not in hub.posts[0]["body"]


@pytest.mark.parametrize("business_status", ["UNVERIFIED", "UNKNOWN"])
def test_unrestricted_profile_can_register_a_product_and_an_information_post_without_certificate(
        engine, store, settings, secrets, worker, hub, business_status):
    rows = prepare(engine, store, settings, secrets, [["P1", "탁상시계", 12000, "원목 시계", None, "시곗바늘은 뒤쪽 다이얼로 맞춥니다.", None]])
    hub.business_status = business_status
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    assert hub.prices[0][2] == 12000 and hub.posts[0]["body"] == "시곗바늘은 뒤쪽 다이얼로 맞춥니다."


def test_unverified_can_register_price_but_sales_copy_stays_in_draft(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "탁상시계", 12000, "원목 시계", None, "시계를 판매합니다. 직거래 가능해요.", None]])
    hub.business_status = "UNVERIFIED"
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    row = query_rows(engine, secrets, settings)["P1"]
    assert len(hub.prices) == 1 and not hub.posts
    assert row["removal"] == {"price": "탁상시계", "news": None} and row["label"] == "게시 보류"


def test_reverification_before_news_keeps_an_existing_price_removable(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "접이식 테이블", 45000, "원목 상판", None, "크기는 70cm예요.\n판매가격: 45,000원", None]])
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "소식 작성이 중단됐습니다.")
    enqueue(engine, secrets, settings, rows, ["P1"])
    run_until_idle(worker)
    assert len(hub.prices) == 1 and not hub.posts
    hub.fail.clear()
    hub.business_status = "RESTRICTED"
    enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P1"], run_key="resumed")
    run_until_idle(worker)
    row = query_rows(engine, secrets, settings)["P1"]
    assert not hub.posts and row["job"]["preview"]["body"] == "크기는 70cm예요."
    assert row["removal"] == {"price": "접이식 테이블", "news": None}


def test_draft_only_button_stops_a_verified_job_until_explicit_registration(engine, store, settings, secrets, worker, hub, text_ai):
    rows = prepare(engine, store, settings, secrets, [["P1", "탁상시계", 12000, "원목 시계", None, None, None]])
    register_service.enqueue_register(engine, secrets, settings, [rows["P1"]["id"]], run_key="preview", draft_only=True)
    run_until_idle(worker)
    assert not hub.prices and not hub.posts and query_rows(engine, secrets, settings)["P1"]["job"]["preview"]
    enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P1"], run_key="publish")
    run_until_idle(worker)
    assert len(hub.prices) == len(hub.posts) == text_ai.calls == 1


def test_price_off_registers_and_removes_only_news(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "탁상시계", 12000, "원목 시계", None, "시곗바늘을 맞추는 방법을 소개합니다.", None]])
    product_id = rows["P1"]["id"]
    register_service.enqueue_register(engine, secrets, settings, [product_id], run_key="news-only", register_price=False)
    run_until_idle(worker)
    job = job_of(engine, "P1")
    assert job["status"] == "SUCCEEDED" and job["steps_json"]["PRICE"]["skipped"]
    assert not hub.prices and len(hub.posts) == 1
    removal = register_service.request_removal(engine, product_id)
    assert removal["price"] is None and removal["news"] == hub.posts[0]["title"]
    run_until_idle(worker)
    assert not hub.prices and not hub.posts


def test_price_can_be_enabled_on_retry_after_it_was_skipped(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P1", "탁상시계", 12000, "원목 시계", None, "원목 탁상시계 사용법입니다.", None]])
    product_id = rows["P1"]["id"]
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "소식 작성이 중단됐습니다.")
    register_service.enqueue_register(engine, secrets, settings, [product_id], run_key="price-off", register_price=False)
    run_until_idle(worker)
    job = job_of(engine, "P1")
    assert job["status"] == "FAILED" and job["steps_json"]["PRICE"]["skipped"] and not hub.prices
    hub.fail.clear()
    register_service.enqueue_register(engine, secrets, settings, [product_id], run_key="price-on")
    run_until_idle(worker)
    resumed = job_of(engine, "P1")
    assert resumed["id"] == job["id"] and resumed["status"] == "SUCCEEDED"
    assert len(hub.prices) == len(hub.posts) == 1


def test_new_default_guidelines_preserve_history_and_custom_prompts(engine):
    from futuremarketer.db_init import seed
    from futuremarketer.repositories.core import active_prompt

    with session_scope(engine) as db:
        execute(db, "UPDATE prompt_template SET body='예전 기본 소식 지침' WHERE kind='TEXT' AND is_active=1")
    seed(engine, "PRACTICE")
    with session_scope(engine) as db:
        assert active_prompt(db, "TEXT")["body"] == DEFAULT_PROMPTS["TEXT"]
        assert fetch_one(db, "SELECT body FROM prompt_template WHERE kind='TEXT' AND version_no=1")["body"] == "예전 기본 소식 지침"
    custom = "우리 가게에서 직접 사용한 방법을 먼저 설명하고 문장은 짧게 씁니다."
    settings_service.save_prompt(engine, "TEXT", custom)
    seed(engine, "PRACTICE")
    with session_scope(engine) as db:
        assert active_prompt(db, "TEXT")["body"] == custom


def test_transient_ai_errors_are_retried(engine, store, settings, secrets, worker, hub, text_ai, image_ai):
    rows = prepare(engine, store, settings, secrets, [["P4", "머그컵", 12000, "350ml 도자기", None, None, None]])
    text_ai.errors = [AiError("AI_TEMPORARY", "혼잡합니다.", transient=True)] * 2
    enqueue(engine, secrets, settings, rows, ["P4"])
    run_until_idle(worker)
    assert job_of(engine, "P4")["status"] == "SUCCEEDED" and text_ai.calls == 3

    rows = prepare(engine, store, settings, secrets, [["P5", "접시", 9000, "도자기 접시", None, None, None]])
    image_ai.errors = [AiError("AI_AUTH", "키가 올바르지 않습니다.")]
    enqueue(engine, secrets, settings, rows, ["P5"], run_key="run-2")
    run_until_idle(worker)
    job = job_of(engine, "P5")
    assert job["status"] == "FAILED" and job["error_code"] == "AI_AUTH"
    assert job["steps_json"]["SCRIPT"]["status"] == "DONE" and job["steps_json"]["IMAGE"]["status"] == "FAILED"


def test_failed_before_submit_resumes_from_that_step(engine, store, settings, secrets, worker, hub, text_ai, image_ai):
    rows = prepare(engine, store, settings, secrets, [["P6", "머그컵", 12000, "350ml 도자기", None, None, None]])
    hub.fail["price_before"] = SiteError("ELEMENT_NOT_FOUND", "화면에서 「가격 추가 버튼」을(를) 찾지 못했습니다.")
    enqueue(engine, secrets, settings, rows, ["P6"])
    run_until_idle(worker)
    job = job_of(engine, "P6")
    assert job["status"] == "FAILED" and job["error_code"] == "ELEMENT_NOT_FOUND"
    assert product_of(engine, "P6")["reg_status"] == "FAILED"
    row = query_rows(engine, secrets, settings)["P6"]
    assert row["state"] == "FAILED" and row["selectable"]

    out = enqueue(engine, secrets, settings, {"P6": row}, ["P6"], run_key="run-2")
    assert out["queued"] == ["P6"]
    run_until_idle(worker)
    again = job_of(engine, "P6")
    assert again["id"] == job["id"] and again["status"] == "SUCCEEDED"     # 새 작업이 아니라 같은 작업을 이어서
    assert text_ai.calls == 1 and image_ai.calls == 1                       # 끝난 단계는 다시 하지 않는다
    assert len(hub.prices) == 1 and len(hub.posts) == 1


def test_unknown_after_submit_needs_a_person(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets,
                   [["P7", "안경", 950000, None, None, SCRIPT, None], ["P8", "헤드폰", 950000, None, None, SCRIPT, None]])
    hub.fail["news_after"] = SiteError("RESULT_UNKNOWN", "소식 등록 결과를 확인하지 못했습니다.", unknown=True)
    enqueue(engine, secrets, settings, rows, ["P7"])
    run_until_idle(worker)
    job = job_of(engine, "P7")
    assert job["status"] == "NEEDS_CHECK" and job["steps_json"]["NEWS"]["status"] == "UNKNOWN"
    assert job["steps_json"]["PRICE"]["status"] == "DONE"
    assert run_until_idle(worker) == 0 and hub.posts == []                 # 자동으로 다시 올리지 않는다
    row = query_rows(engine, secrets, settings)["P7"]
    assert row["state"] == "NEEDS_CHECK" and not row["selectable"]
    assert enqueue(engine, secrets, settings, {"P7": row}, ["P7"], run_key="r2")["skipped"][0]["reason"].startswith("이미 등록이 진행")

    # 사람이 「올라가 있더라」고 확인 → 완료
    assert register_service.resolve_check(engine, job["id"], outcome="DONE") == {"result": "DONE"}
    assert product_of(engine, "P7")["reg_status"] == "DONE" and job_of(engine, "P7")["status"] == "SUCCEEDED"
    with pytest.raises(DomainError):
        register_service.resolve_check(engine, job["id"], outcome="DONE")

    # 가격 저장 뒤 불명 → 「올라가지 않았다」 → 그 단계부터 다시
    hub.fail["price_after"] = SiteError("RESULT_UNKNOWN", "가격 저장 결과를 확인하지 못했습니다.", unknown=True)
    enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["P8"], run_key="r3")
    run_until_idle(worker)
    job8 = job_of(engine, "P8")
    assert job8["status"] == "NEEDS_CHECK" and job8["steps_json"]["PRICE"]["status"] == "UNKNOWN"
    assert register_service.resolve_check(engine, job8["id"], outcome="RETRY") == {"result": "QUEUED"}
    run_until_idle(worker)
    assert job_of(engine, "P8")["status"] == "SUCCEEDED"
    assert [p[1] for p in hub.prices] == ["안경", "헤드폰"] and [p["title"] for p in hub.posts] == ["헤드폰"]


def test_explicit_rejection_after_submit_is_a_plain_failure(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [["P9", "안경", 950000, None, None, SCRIPT, None]])
    hub.fail["news_after"] = SiteError("QUOTA", "오늘 올릴 수 있는 소식을 모두 썼습니다.", definite=True)
    enqueue(engine, secrets, settings, rows, ["P9"])
    run_until_idle(worker)
    job = job_of(engine, "P9")
    assert job["status"] == "FAILED" and job["error_code"] == "QUOTA"


def test_same_click_twice_and_skips(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets, [
        ["Q1", "상품 1", 1000, None, None, SCRIPT, None],
        ["Q2", "상품 2", None, None, None, SCRIPT, None],       # 가격 없음 → 등록 불가
        ["Q3", "상품 3", 3000, None, None, SCRIPT, None],
    ])
    first = enqueue(engine, secrets, settings, rows, ["Q1", "Q2", "Q3"], run_key="same")
    second = enqueue(engine, secrets, settings, rows, ["Q1", "Q2", "Q3"], run_key="same")
    assert first["queued"] == ["Q1", "Q3"] and second["queued"] == ["Q1", "Q3"] and first["run_id"] == second["run_id"]
    assert "가격" in first["skipped"][0]["reason"]
    with session_scope(engine) as s:
        assert fetch_one(s, "SELECT COUNT(*) AS n FROM task_job")["n"] == 2
    run_until_idle(worker)
    assert [p[1] for p in hub.prices] == ["상품 1", "상품 3"]                # 엑셀 순서대로
    assert hub.opened == 1                                                  # 한 묶음은 브라우저 하나로
    done = enqueue(engine, secrets, settings, query_rows(engine, secrets, settings), ["Q1"], run_key="other")
    assert done["queued"] == [] and done["skipped"][0]["reason"] == "이미 등록한 상품입니다."
    with pytest.raises(DomainError):
        register_service.enqueue_register(engine, secrets, settings, [], run_key="none")
    assert query_service.dashboard(engine, secrets, settings)["news_today"] == 2


def test_one_active_job_per_product_is_enforced_by_db(engine, store, settings, secrets):
    rows = prepare(engine, store, settings, secrets, [["U1", "상품", 1000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["U1"])
    job = job_of(engine, "U1")
    with pytest.raises(Exception) as err, session_scope(engine) as s:
        execute(s, "INSERT INTO task_job (id, run_id, product_id, status, steps_json, request_json, idempotency_key, created_at)"
                   " SELECT UUID(), run_id, product_id, 'QUEUED', steps_json, request_json, 'other-key', created_at"
                   " FROM task_job WHERE id = :id", id=job["id"])
    assert "uq_job_active" in str(err.value)


def test_expired_login_stops_the_whole_queue(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets,
                   [["L1", "상품 1", 1000, None, None, SCRIPT, None], ["L2", "상품 2", 2000, None, None, SCRIPT, None]])
    hub.logged_in = False
    enqueue(engine, secrets, settings, rows, ["L1", "L2"])
    run_until_idle(worker)
    assert job_of(engine, "L1")["error_code"] == "LOGIN_REQUIRED" and job_of(engine, "L2")["error_code"] == "LOGIN_REQUIRED"
    assert hub.opened == 1 and hub.prices == []
    overview = settings_service.overview(engine, secrets, settings)
    assert overview["connection"]["status"] == "EXPIRED"
    rows = query_rows(engine, secrets, settings)
    assert rows["L1"]["state"] == "FAILED" and not rows["L1"]["selectable"]      # 다시 연결하기 전에는 고를 수 없다


def test_worker_restart_recovery(engine, store, settings, secrets, worker, hub):
    rows = prepare(engine, store, settings, secrets,
                   [["R1", "상품 1", 1000, None, None, SCRIPT, None], ["R2", "상품 2", 2000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["R1", "R2"])
    first, second = worker.claim_job(), worker.claim_job()
    first["steps_json"]["SCRIPT"] = {"status": "DONE", "title": "t", "body": "b"}
    first["steps_json"]["IMAGE"] = {"status": "DONE", "image_keys": []}
    first["steps_json"]["PRICE"] = {"status": "RUNNING", "submitting": True}     # 저장을 누르던 중에 꺼짐
    worker._save_steps(first, "PRICE")
    second["steps_json"]["SCRIPT"] = {"status": "RUNNING"}                       # 글을 쓰던 중에 꺼짐
    worker._save_steps(second, "SCRIPT")

    restarted = Worker(engine, settings, store, secrets, site_factory=hub.factory, sleep=lambda _s: None)
    restarted.recover_on_start()
    one, two = job_of(engine, "R1"), job_of(engine, "R2")
    assert one["status"] == "NEEDS_CHECK" and one["steps_json"]["PRICE"]["status"] == "UNKNOWN"
    assert two["status"] == "FAILED" and two["error_code"] == "INTERRUPTED"
    assert restarted.run_once() is False and hub.prices == []                    # 아무것도 저절로 다시 올리지 않는다
    # 예전 워커가 늦게 결과를 쓰려 해도 표식이 달라 무시된다
    worker._finish(first, "SUCCEEDED")
    assert job_of(engine, "R1")["status"] == "NEEDS_CHECK"


def test_cancel_queued(engine, store, settings, secrets):
    rows = prepare(engine, store, settings, secrets,
                   [["C1", "상품 1", 1000, None, None, SCRIPT, None], ["C2", "상품 2", 2000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["C1", "C2"])
    assert register_service.cancel_queued(engine) == 2
    assert product_of(engine, "C1")["reg_status"] == "NONE" and job_of(engine, "C1")["status"] == "CANCELED"
    assert query_rows(engine, secrets, settings)["C1"]["state"] == "READY"
    assert enqueue(engine, secrets, settings, rows, ["C1"], run_key="again")["queued"] == ["C1"]


# --- 계정 연결·해제 -----------------------------------------------------------
def test_connect_then_disconnect(engine, settings, secrets, worker, hub, removed_profiles):
    assert settings_service.overview(engine, secrets, settings)["connection"]["status"] == "DISCONNECTED"
    settings_service.request_connect(engine)
    assert settings_service.overview(engine, secrets, settings)["connection"]["status"] == "CONNECTING"
    with pytest.raises(DomainError):
        settings_service.request_connect(engine)                               # 이미 진행 중
    assert worker.process_control() is True
    conn = settings_service.overview(engine, secrets, settings)["connection"]
    assert conn["status"] == "CONNECTED" and conn["biz_id"] == "1001" and conn["biz_name"] == "연습용 가게"
    assert conn["control_status"] == "DONE" and hub.closed == 1                # 로그인이 끝나면 창을 닫는다

    settings_service.request_disconnect(engine)
    assert worker.process_control() is True
    conn = settings_service.overview(engine, secrets, settings)["connection"]
    assert conn["status"] == "DISCONNECTED" and conn["biz_id"] is None and conn["connected_at"] is None
    assert removed_profiles == [settings.browser_profile_dir]                  # 저장된 로그인 정보를 지운다
    assert settings.browser_profile_dir.name == "browser-profile-practice"
    assert settings.private_dir in settings.browser_profile_dir.parents       # 프로젝트 폴더가 아니라 개인 폴더


def test_connect_failure_and_cancel(engine, settings, secrets, worker, hub):
    hub.fail["login"] = SiteError("LOGIN_TIMEOUT", "시간 안에 로그인이 확인되지 않았습니다.")
    settings_service.request_connect(engine)
    worker.process_control()
    conn = settings_service.overview(engine, secrets, settings)["connection"]
    assert conn["status"] == "DISCONNECTED" and conn["control_status"] == "FAILED" and "시간 안에" in conn["control_message"]

    hub.on_login = lambda: settings_service.cancel_connect(engine)             # 로그인 창이 떠 있는 동안 취소
    settings_service.request_connect(engine)
    worker.process_control()
    conn = settings_service.overview(engine, secrets, settings)["connection"]
    assert conn["status"] == "DISCONNECTED" and conn["control_message"] == "연결을 취소했습니다."
    assert hub.closed == 2


def test_control_is_blocked_while_registering(engine, store, settings, secrets):
    rows = prepare(engine, store, settings, secrets, [["B1", "상품", 1000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["B1"])
    with pytest.raises(DomainError) as err:
        settings_service.request_disconnect(engine)
    assert err.value.code == "BUSY"


# --- AI 연결·지침 --------------------------------------------------------------
def test_api_key_lives_outside_db_and_project(engine, settings, secrets):
    settings_service.save_ai(engine, secrets, "CLAUDE", mode="API_KEY", api_key="sk-ant-test-0123456789abcdefghij")
    mode = stat.S_IMODE(os.stat(settings.secrets_path).st_mode)
    assert mode == 0o600
    with session_scope(engine) as s:
        dump = str(fetch_all(s, "SELECT * FROM ai_connection"))
    assert "sk-ant-test" not in dump
    view = settings_service.overview(engine, secrets, settings)["providers"]["CLAUDE"]
    assert view["mode"] == "API_KEY" and view["ready"] and "0123456789" not in view["key_hint"]
    settings_service.save_ai(engine, secrets, "CLAUDE", mode="API_KEY", api_key="")      # 빈 칸은 기존 키 유지
    assert secrets.get("anthropic_api_key") == "sk-ant-test-0123456789abcdefghij"
    with pytest.raises(DomainError):
        settings_service.save_ai(engine, secrets, "GPT", mode="API_KEY", api_key="")     # 저장된 키가 없으면 오류
    with pytest.raises(DomainError):
        settings_service.save_ai(engine, secrets, "GPT", mode="API_KEY", api_key="short")
    settings_service.delete_ai_key(engine, secrets, "CLAUDE")
    view = settings_service.overview(engine, secrets, settings)["providers"]["CLAUDE"]
    assert view["mode"] == "NONE" and not view["ready"] and not secrets.has("anthropic_api_key")


def test_prompt_edit_reset_and_snapshot(engine, store, settings, secrets, worker, hub, text_ai):
    rows = prepare(engine, store, settings, secrets,
                   [["T1", "상품 1", 1000, "정보", None, None, None], ["T2", "상품 2", 2000, "정보", None, None, None]])
    enqueue(engine, secrets, settings, rows, ["T1"])
    custom = "반말로 짧게 씁니다. 세 문장을 넘기지 않습니다. 과장하지 않습니다."
    assert settings_service.save_prompt(engine, "TEXT", custom)["version_no"] == 2
    assert settings_service.save_prompt(engine, "TEXT", custom)["result"] == "UNCHANGED"
    view = settings_service.overview(engine, secrets, settings)["prompts"]["TEXT"]
    assert view["body"] == custom and not view["is_default"]
    run_until_idle(worker)
    assert text_ai.last["guideline"] == DEFAULT_PROMPTS["TEXT"]                 # 접수할 때의 지침 사본을 쓴다

    enqueue(engine, secrets, settings, rows, ["T2"], run_key="run-2")
    run_until_idle(worker)
    assert text_ai.last["guideline"] == custom
    assert settings_service.reset_prompt(engine, "TEXT")["version_no"] == 3
    assert settings_service.overview(engine, secrets, settings)["prompts"]["TEXT"]["is_default"]
    with pytest.raises(DomainError):
        settings_service.save_prompt(engine, "TEXT", "짧음")
    with session_scope(engine) as s:
        assert fetch_one(s, "SELECT COUNT(*) AS n FROM prompt_template WHERE kind='TEXT' AND is_active=1")["n"] == 1


def test_progress_view(engine, store, settings, secrets, worker, hub):
    assert query_service.progress(engine)["focus"] is None
    rows = prepare(engine, store, settings, secrets,
                   [["V1", "상품 1", 1000, None, None, SCRIPT, None], ["V2", "상품 2", 2000, None, None, SCRIPT, None]])
    enqueue(engine, secrets, settings, rows, ["V1", "V2"])
    view = query_service.progress(engine)
    assert view["active"] and view["queued"] == 2 and [j["code"] for j in view["jobs"]] == ["V1", "V2"]
    worker.run_once()
    view = query_service.progress(engine)
    assert view["focus"]["code"] == "V2" and view["jobs"][0]["status"] == "SUCCEEDED"
    run_until_idle(worker)
    view = query_service.progress(engine)
    assert not view["active"] and view["focus"]["status"] == "SUCCEEDED" and view["events"] and view["shot_key"]
