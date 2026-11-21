"""한 로그인으로 비즈프로필을 여러 개 관리할 때: 등록할 비즈프로필을 고르고 바꾼다."""
from __future__ import annotations

import pytest

from futuremarketer.adapters.daangn import SiteError
from futuremarketer.db import session_scope
from futuremarketer.errors import DomainError
from futuremarketer.repositories import core
from futuremarketer.services import query_service, settings_service
from tests.conftest import run_until_idle
from tests.test_flow import enqueue, prepare
from tests.test_history import headphone


def conn_of(engine):
    with session_scope(engine) as s:
        return core.connection(s)


def connect_real_way(engine, worker, hub, biz="1001", name="연습용 가게", public="7001"):
    hub.login_result = {"biz_id": biz, "biz_name": name, "public_id": public}
    settings_service.request_connect(engine)
    assert worker.process_control() is True


def test_choose_another_profile_then_switch_back_without_browser(engine, store, settings, secrets, worker, hub):
    connect_real_way(engine, worker, hub)
    assert [p["biz_id"] for p in settings_service.known_profiles(conn_of(engine))] == ["1001"]

    hub.login_result = {"biz_id": "1002", "biz_name": "연습용 가게 2호점", "public_id": "7002"}
    settings_service.request_select(engine)
    assert conn_of(engine)["status"] == "CONNECTED"                     # 고르는 동안에도 연결은 그대로
    assert worker.process_control() is True
    assert hub.chose is True                                            # 비즈프로필 고르기 화면으로 열었다
    conn = conn_of(engine)
    assert (conn["biz_id"], conn["biz_name"], conn["public_id"]) == ("1002", "연습용 가게 2호점", "7002")
    assert [p["biz_id"] for p in settings_service.known_profiles(conn)] == ["1002", "1001"]

    opened = hub.opened
    settings_service.switch_profile(engine, "1001")                     # 골라 본 곳으로는 브라우저 없이 바꾼다
    conn = conn_of(engine)
    assert (conn["biz_id"], conn["public_id"]) == ("1001", "7001") and hub.opened == opened
    with pytest.raises(DomainError):
        settings_service.switch_profile(engine, "9999")                 # 고른 적 없는 곳


def test_registration_is_kept_per_profile(engine, store, settings, secrets, worker, hub):
    connect_real_way(engine, worker, hub)
    rows = headphone(engine, store, settings, secrets)
    enqueue(engine, secrets, settings, rows, ["P2"])
    run_until_idle(worker)
    assert hub.prices[-1][0] == "1001"

    hub.login_result = {"biz_id": "1002", "biz_name": "연습용 가게 2호점", "public_id": "7002"}
    settings_service.request_select(engine)
    worker.process_control()
    row = {r["code"]: r for r in query_service.product_rows(engine, secrets, settings)["rows"]}["P2"]
    assert row["state"] == "READY"                                      # 2호점에는 아직 올리지 않았다
    enqueue(engine, secrets, settings, {"P2": row}, ["P2"], run_key="run-2")
    run_until_idle(worker)
    assert hub.prices[-1][0] == "1002"                                  # 고른 곳에 올라간다

    settings_service.switch_profile(engine, "1001")
    row = {r["code"]: r for r in query_service.product_rows(engine, secrets, settings)["rows"]}["P2"]
    assert row["state"] == "DONE"                                       # 1호점 기록은 그대로


def test_failed_or_canceled_choice_keeps_the_connection(engine, settings, secrets, worker, hub):
    connect_real_way(engine, worker, hub)
    hub.fail["login"] = SiteError("LOGIN_TIMEOUT", "시간 안에 확인되지 않았습니다.")
    settings_service.request_select(engine)
    worker.process_control()
    conn = conn_of(engine)
    assert conn["status"] == "CONNECTED" and conn["biz_id"] == "1001" and conn["control_status"] == "FAILED"

    settings_service.request_select(engine)
    settings_service.cancel_connect(engine)
    conn = conn_of(engine)
    assert conn["status"] == "CONNECTED" and conn["biz_id"] == "1001" and "취소" in conn["control_message"]


def test_switch_is_refused_while_work_is_waiting(engine, store, settings, secrets, worker, hub):
    connect_real_way(engine, worker, hub)
    hub.login_result = {"biz_id": "1002", "biz_name": "2호점", "public_id": None}
    settings_service.request_select(engine)
    worker.process_control()
    rows = prepare(engine, store, settings, secrets, [["P9", "상품", 1000, "정보", None, "대본입니다. 충분히 깁니다.", None]])
    enqueue(engine, secrets, settings, rows, ["P9"])
    with pytest.raises(DomainError):
        settings_service.switch_profile(engine, "1001")
    with pytest.raises(DomainError):
        settings_service.request_select(engine)


def test_disconnect_forgets_profiles(engine, settings, secrets, worker, hub, removed_profiles):
    connect_real_way(engine, worker, hub)
    settings_service.request_disconnect(engine)
    worker.process_control()
    assert conn_of(engine)["profiles_json"] is None and settings_service.known_profiles(conn_of(engine)) == []
