"""등록한 상품을 당근에서 지우는 기능: 가격 항목과 소식을 지우고, 상품은 다시 등록할 수 있게 돌아온다."""
from __future__ import annotations

import pytest

from futuremarketer.adapters.daangn import SiteError
from futuremarketer.errors import DomainError
from futuremarketer.services import query_service, register_service
from tests.conftest import FAILURE_SCREEN, run_until_idle
from tests.test_flow import enqueue, job_of, prepare, product_of
from tests.test_history import headphone
from tests.test_ui_smoke import app_env, page_app  # noqa: F401  (화면 시험은 시험용 DB·폴더를 보게 한다)


def row_of(engine, secrets, settings, code):
    return {r["code"]: r for r in query_service.product_rows(engine, secrets, settings)["rows"]}[code]


def registered(engine, store, settings, secrets, worker):
    rows = headphone(engine, store, settings, secrets)
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    return rows


def test_removing_deletes_price_and_news_and_the_product_can_be_registered_again(engine, store, settings, secrets, worker, hub):
    rows = registered(engine, store, settings, secrets, worker)
    row = row_of(engine, secrets, settings, "P2")
    assert row["state"] == "DONE" and row["removal"]["price"] and row["removal"]["news"]
    assert len(hub.prices) == 1 and len(hub.posts) == 1

    register_service.request_removal(engine, rows["P2"]["id"])
    assert product_of(engine, "P2")["reg_status"] == "QUEUED"
    run_until_idle(worker)

    assert hub.prices == [] and hub.posts == []                                  # 당근에서 지워졌다
    assert product_of(engine, "P2")["reg_status"] == "NONE"
    again = row_of(engine, secrets, settings, "P2")
    assert again["state"] == "READY" and again["removal"] is None                # 다시 등록할 수 있다
    history = query_service.history(engine)
    assert history["rows"][0]["kind"] == "REMOVE" and history["rows"][0]["outcome"] == "SUCCEEDED"
    assert history["rows"][1]["kind"] == "REGISTER"                               # 등록 기록은 그대로 남는다
    assert query_service.dashboard(engine, secrets, settings)["news_today"] == 1   # 삭제를 소식 등록으로 세지 않는다

    assert enqueue(engine, secrets, settings, rows, ["P2"], run_key="run-2")["queued"] == ["P2"]
    run_until_idle(worker)
    assert len(hub.prices) == 1 and len(hub.posts) == 1 and product_of(engine, "P2")["reg_status"] == "DONE"


def test_failed_removal_keeps_state_and_can_be_retried(engine, store, settings, secrets, worker, hub):
    rows = registered(engine, store, settings, secrets, worker)
    hub.fail["rm_news"] = SiteError("ELEMENT_NOT_FOUND", "소식 삭제 버튼을 찾지 못했습니다.")
    register_service.request_removal(engine, rows["P2"]["id"])
    run_until_idle(worker)

    assert product_of(engine, "P2")["reg_status"] == "DONE"                      # 삭제를 누르기 전 상태 그대로
    assert len(hub.posts) == 1 and len(hub.prices) == 1
    failed = query_service.history(engine)["rows"][0]
    assert failed["kind"] == "REMOVE" and failed["outcome"] == "FAILED" and failed["step"] == "NEWS"
    assert store.read(failed["shot_key"]) == FAILURE_SCREEN                       # 멈춘 순간의 화면
    assert row_of(engine, secrets, settings, "P2")["removal"] is not None         # 다시 지울 수 있다

    register_service.request_removal(engine, rows["P2"]["id"])
    run_until_idle(worker)
    assert hub.prices == [] and hub.posts == [] and product_of(engine, "P2")["reg_status"] == "NONE"


def test_partly_registered_product_removes_only_what_was_put_up(engine, store, settings, secrets, worker, hub):
    rows = headphone(engine, store, settings, secrets)
    hub.fail["news_before"] = SiteError("ELEMENT_NOT_FOUND", "사진을 고르는 창이 열리지 않았습니다.")
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    assert product_of(engine, "P2")["reg_status"] == "FAILED" and len(hub.prices) == 1 and hub.posts == []

    plan = row_of(engine, secrets, settings, "P2")["removal"]
    assert plan["price"] and plan["news"] is None                                  # 가격은 올라갔고 소식은 아직
    register_service.request_removal(engine, rows["P2"]["id"])
    run_until_idle(worker)
    assert hub.prices == [] and row_of(engine, secrets, settings, "P2")["state"] == "READY"


def test_removal_is_refused_when_nothing_is_up_or_work_is_running(engine, store, settings, secrets, worker, hub):
    rows = headphone(engine, store, settings, secrets)
    with pytest.raises(DomainError):                                               # 올린 것이 없다
        register_service.request_removal(engine, rows["P2"]["id"])
    enqueue(engine, secrets, settings, rows, ["P2"])
    with pytest.raises(DomainError):                                               # 등록이 줄 서 있다
        register_service.request_removal(engine, rows["P2"]["id"])
    run_until_idle(worker)
    register_service.request_removal(engine, rows["P2"]["id"])
    with pytest.raises(DomainError):                                               # 삭제가 이미 접수됐다
        register_service.request_removal(engine, rows["P2"]["id"])
    assert register_service.cancel_queued(engine) == 1                             # 대기 중인 삭제를 취소하면
    assert product_of(engine, "P2")["reg_status"] == "DONE"                        # 원래 상태로 돌아온다


def test_interrupted_removal_goes_back_to_before(engine, store, settings, secrets, worker, hub):
    rows = registered(engine, store, settings, secrets, worker)
    register_service.request_removal(engine, rows["P2"]["id"])
    job = worker.claim_job()                              # 삭제를 시작했는데 프로그램이 멈췄다
    assert job["request_json"]["kind"] == "REMOVE"
    assert worker.recover_running() == 1
    assert product_of(engine, "P2")["reg_status"] == "DONE"
    assert query_service.history(engine)["rows"][0]["outcome"] == "FAILED"


def test_register_page_shows_delete_with_a_confirmation_step(app_env, engine, store, settings, secrets, worker, hub):
    registered(engine, store, settings, secrets, worker)
    at = page_app("register_page").run()
    assert not at.exception, at.exception
    ask = [b for b in at.button if b.label == "당근에서 삭제"]
    assert len(ask) == 1
    ask[0].click().run()
    assert not at.exception, at.exception
    assert any("정말 지울까요" in m.value for m in at.markdown) or any("정말 지울까요" in str(e.value) for e in at.get("html"))
    labels = [b.label for b in at.button]
    assert "지우기" in labels and "취소" in labels
    [b for b in at.button if b.label == "지우기"][0].click().run()
    assert not at.exception, at.exception
    assert product_of(engine, "P2")["reg_status"] == "QUEUED"                     # 확인한 뒤에야 접수된다


def test_worker_restarts_itself_with_new_code_when_idle(worker, monkeypatch):
    """화면만 새 코드로 바뀌고 워커는 옛 코드로 남아 새 작업(삭제)을 모르는 일이 없게, 쉬는 동안 코드가 바뀌면 다시 뜬다."""
    from futuremarketer import worker as worker_module

    if worker_module.sys.platform == "win32":
        pytest.skip("Windows에서는 앱 재시작으로 워커를 업데이트합니다.")
    stamps = iter([(10, 1.0), (10, 2.0)])
    monkeypatch.setattr(worker_module, "code_stamp", lambda: next(stamps))
    clock = iter([0.0, 0.0, 5.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(clock, 10.0))
    monkeypatch.setattr(worker, "recover_on_start", lambda: None)
    monkeypatch.setattr(worker, "run_once", lambda: False)
    with pytest.raises(worker_module.CodeChanged):
        worker.run_forever()
