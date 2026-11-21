"""화면이 오류 없이 그려지는지, 화면을 다시 그리는 것만으로는 작업이 생기지 않는지."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from futuremarketer.config import PROJECT_ROOT
from futuremarketer.db import fetch_one, session_scope
from futuremarketer.services import import_service, register_service, settings_service
from tests.conftest import TEST_URL, connect, make_image, make_xlsx, set_ai

SCRIPT = "아끼던 안경을 판매합니다.\n상태 좋습니다. 채팅 주세요."


@pytest.fixture
def app_env(monkeypatch, settings, engine):
    """화면이 시험용 DB·폴더를 보게 하고, 워커 자동 실행은 끈다."""
    import ui.common as common

    monkeypatch.setenv("FM_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("FM_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("FM_PRIVATE_DIR", str(settings.private_dir))
    monkeypatch.setenv("FM_IMAGE_DIR", str(settings.image_dir))
    monkeypatch.setenv("FM_DEFAULT_EXCEL", str(settings.default_excel))
    monkeypatch.setenv("FM_DAANGN_TARGET", "practice")
    monkeypatch.setenv("FM_AI_MODE", "live")
    monkeypatch.setenv("FM_NO_AUTOSTART", "1")
    common._resources.clear()
    common._boot.clear()
    yield
    common._resources.clear()
    common._boot.clear()


def job_count(engine) -> int:
    with session_scope(engine) as s:
        return fetch_one(s, "SELECT COUNT(*) AS n FROM task_job")["n"]


def page_app(module: str) -> AppTest:
    """메뉴 한 장만 그린다(공통 준비 → 그 화면의 run)."""
    script = f"from ui import common, {module}\nif common.boot():\n    {module}.run()\n"
    at = AppTest.from_string(script, default_timeout=30)
    at.session_state["_pages"] = {}
    return at


def test_pages_render_empty_and_filled(app_env, engine, store, settings, secrets):
    for module in ("register_page", "settings_page"):
        at = page_app(module).run()
        assert not at.exception, at.exception

    make_image(settings.image_dir / "a.png")
    settings.default_excel.write_bytes(make_xlsx([
        ["P1", "<b>안경</b>", 950000, None, None, SCRIPT, "a.png"],        # 엑셀 글자가 HTML 로 해석되면 안 된다
        ["P2", "가격 없는 상품", None, "정보", None, None, None],
    ]))
    at = page_app("register_page").run()
    load_button = next(b for b in at.button if b.label == "불러오기")
    load_button.click().run()
    assert not at.exception, at.exception
    html = " ".join(h.body for h in at.get("html"))
    assert "&lt;b&gt;안경&lt;/b&gt;" in html and "<b>안경</b>" not in html
    assert "등록 불가" in html and "가격이 비어 있습니다." in html
    assert job_count(engine) == 0

    connect(engine)
    set_ai(engine, secrets)
    at = page_app("register_page").run()
    for _ in range(3):
        at.run()                       # 화면을 여러 번 다시 그려도
    assert job_count(engine) == 0      # 작업은 생기지 않는다(버튼을 눌러야만 접수)
    boxes = [c for c in at.checkbox if c.key and c.key.startswith("sel_") and c.key != "sel_all"]
    assert [c.disabled for c in boxes] == [False, True]
    preview = next(b for b in at.button if b.label == "상품 사진 크게 보기")
    preview.click().run()
    assert not at.exception, at.exception
    assert len(at.get("image") or at.get("imgs")) == 1
    assert job_count(engine) == 0
    price_toggle = at.toggle(key="register_price")
    assert price_toggle.value
    price_toggle.set_value(False).run()
    at.checkbox(key=boxes[0].key).check().run()
    register = next(b for b in at.button if b.label == "선택한 1개 소식 등록")
    register.click().run()
    assert not at.exception, at.exception
    assert job_count(engine) == 1
    with session_scope(engine) as session:
        assert fetch_one(session, "SELECT request_json FROM task_job")["request_json"]["register_price"] is False
    at.run()
    assert job_count(engine) == 1


def test_settings_actions(app_env, engine, secrets, settings):
    at = page_app("settings_page").run()
    connect_button = next(b for b in at.button if b.label == "당근 계정 연결")
    connect_button.click().run()
    assert not at.exception, at.exception
    assert settings_service.overview(engine, secrets, settings)["connection"]["control_status"] == "REQUESTED"

    at = page_app("settings_page").run()
    area = next(t for t in at.text_area if t.key == "prompt_TEXT")
    area.set_value("반말로 짧게 씁니다. 세 문장을 넘기지 않습니다. 과장하지 않습니다.").run()
    next(b for b in at.button if b.key == "save_prompt_TEXT").click().run()
    assert not at.exception, at.exception
    view = settings_service.overview(engine, secrets, settings)["prompts"]["TEXT"]
    assert view["version_no"] == 2 and not view["is_default"]
    next(b for b in at.button if b.key == "reset_prompt_TEXT").click().run()
    assert settings_service.overview(engine, secrets, settings)["prompts"]["TEXT"]["is_default"]


def test_dashboard_has_no_registration_controls(app_env, engine, store, settings, secrets):
    settings.default_excel.write_bytes(make_xlsx([["P1", "안경", 950000, None, None, SCRIPT, None]]))
    import_service.load_default(engine, store, settings)
    connect(engine)
    set_ai(engine, secrets)
    at = AppTest.from_file(str(PROJECT_ROOT / "streamlit_app.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    labels = [b.label for b in at.button]
    assert not any("등록" in label or "불러오기" in label for label in labels)   # 대시보드에는 등록 기능이 없다
    assert not at.checkbox and not at.get("file_uploader")
    assert job_count(engine) == 0
    assert register_service.enqueue_register(engine, secrets, settings, [
        fetch_id(engine, "P1")], run_key="k")["queued"] == ["P1"]


def fetch_id(engine, code: str) -> str:
    with session_scope(engine) as s:
        return fetch_one(s, "SELECT id FROM product WHERE product_code = :c", c=code)["id"]
