"""시험 공통: 시험 전용 MySQL DB 를 매 시험 비우고 다시 만든다.

FM_TEST_DATABASE_URL (기본: root@127.0.0.1:3317/futuremarketer_test). 이름이 _test 로 끝나야만 돈다.
브라우저와 AI 는 가짜로 바꿔 끼운다(실제 브라우저 시험은 test_browser_practice.py).
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True      # 캐시 파일(.pyc)에는 내 PC 의 경로가 들어간다. 되도록 남기지 않는다.

import pytest
from openpyxl import Workbook
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from futuremarketer.adapters.daangn import SiteError
from futuremarketer.adapters.image_ai import MockImageGenerator
from futuremarketer.adapters.text_ai import MockTextGenerator
from futuremarketer.config import load_settings
from futuremarketer.db import execute, make_engine, session_scope
from futuremarketer.db_init import apply_schema, seed
from futuremarketer.storage import FileStore, SecretStore
from futuremarketer.util import utcnow
from futuremarketer.worker import Worker

TEST_URL = os.environ.get("FM_TEST_DATABASE_URL",
                          "mysql+pymysql://root:fm_root_local@127.0.0.1:3317/futuremarketer_test?charset=utf8mb4")
HEADER = ["상품코드", "상품명", "가격", "상품정보", "소식제목", "대본", "이미지"]


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 40), color).save(buf, format="JPEG")
    return buf.getvalue()


SCREEN = _jpeg((240, 240, 240))              # 가짜 브라우저 화면
FAILURE_SCREEN = _jpeg((250, 210, 210))      # 가짜 「멈춘 순간」 화면


@pytest.fixture
def engine():
    url = make_url(TEST_URL)
    assert url.database and url.database.endswith("_test"), "시험 DB 이름은 _test 로 끝나야 합니다."
    admin = create_engine(url.set(database=""))
    with admin.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{url.database}`"))
        conn.execute(text(f"CREATE DATABASE `{url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"))
    admin.dispose()
    eng = make_engine(TEST_URL)
    apply_schema(eng)
    seed(eng, "PRACTICE")
    yield eng
    eng.dispose()


@pytest.fixture
def settings(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    return load_settings(database_url=TEST_URL, data_dir=tmp_path / "data", private_dir=tmp_path / "private",
                         default_excel=tmp_path / "상품목록.xlsx", image_dir=images, daangn_target="practice",
                         ai_mode="live", poll_interval_sec=0, login_timeout_sec=5, action_delay_sec=0,
                         idle_browser_close_sec=0)


@pytest.fixture
def store(settings):
    return FileStore(settings.data_dir)


@pytest.fixture
def secrets(settings):
    return SecretStore(settings.secrets_path)


class SiteHub:
    """가짜 당근 화면. 워커가 무엇을 등록하려 했는지 기록하고, 원하는 지점에서 실패를 일으킨다."""

    def __init__(self):
        self.logged_in = True
        self.prices: list[tuple] = []
        self.price_photos: list[Path | None] = []
        self.posts: list[dict] = []
        self.opened = 0
        self.closed = 0
        self.fail: dict[str, SiteError] = {}      # price_before / price_after / news_before / news_after / login
        self.login_result = {"biz_id": "1001", "biz_name": "연습용 가게", "public_id": "7001"}
        self.public_id = "7001"
        self.on_login = None

    def factory(self, on_log):
        self.opened += 1
        return FakeSite(self, on_log)


class FakeSite:
    def __init__(self, hub: SiteHub, on_log):
        self.hub = hub
        self.on_log = on_log

    def _maybe_fail(self, point: str):
        if point in self.hub.fail:
            raise self.hub.fail.pop(point)

    def login(self, timeout, should_cancel, choose=False):
        self.hub.chose = choose
        self.on_log("Aside 창에서 비즈프로필을 골라 주세요." if choose else "Aside 창에서 로그인해 주세요.", "INFO", None)
        if self.hub.on_login:
            self.hub.on_login()
        if should_cancel():
            raise SiteError("CANCELED", "연결을 취소했습니다.")
        self._maybe_fail("login")
        return dict(self.hub.login_result)

    def is_logged_in(self, biz_id):
        self.public_id = self.hub.public_id
        return self.hub.logged_in

    def register_price(self, biz_id, name, price, image_path=None, *, before_submit):
        self.on_log("가격표 화면을 엽니다.", "INFO", None)
        self._maybe_fail("price_before")
        before_submit()
        self._maybe_fail("price_after")
        self.hub.prices.append((biz_id, name, price))
        self.hub.price_photos.append(Path(image_path) if image_path else None)
        self.on_log("가격을 등록했습니다.", "DONE", SCREEN)
        return {"action": "ADDED", "item_name": name, "photo": image_path is not None}

    def write_news(self, biz_id, title, body, image_paths, *, before_submit):
        self.on_log("소식 작성 화면을 엽니다.", "INFO", None)
        self._maybe_fail("news_before")
        before_submit()
        self._maybe_fail("news_after")
        self.hub.posts.append({"biz_id": biz_id, "title": title, "body": body,
                               "images": [Path(p) for p in image_paths]})
        self.on_log("소식을 등록했습니다.", "DONE", SCREEN)
        return {"url": "http://practice/biz_accounts/1001/manager/posts/?postId=abc123", "title": title}

    def remove_price(self, biz_id, name, *, before_submit):
        self.on_log("가격표 화면을 엽니다.", "INFO", None)
        self._maybe_fail("rm_price")
        found = [p for p in self.hub.prices if p[0] == biz_id and p[1] == name]
        if not found:
            self.on_log("가격표에 지울 항목이 없습니다.", "DONE", SCREEN)
            return {"action": "ABSENT"}
        before_submit()
        self.hub.prices.remove(found[0])
        self.on_log("가격 항목을 지웠습니다.", "DONE", SCREEN)
        return {"action": "REMOVED"}

    def remove_news(self, biz_id, title, *, before_submit):
        self.on_log("소식 목록 화면을 엽니다.", "INFO", None)
        self._maybe_fail("rm_news")
        found = [p for p in self.hub.posts if p["biz_id"] == biz_id and p["title"] == title]
        if not found:
            self.on_log("소식 목록에 지울 소식이 없습니다.", "DONE", SCREEN)
            return {"action": "ABSENT"}
        before_submit()
        self.hub.posts.remove(found[0])
        self.on_log("소식을 지웠습니다.", "DONE", SCREEN)
        return {"action": "REMOVED"}

    def capture(self):
        """멈춘 순간의 화면(가짜)."""
        return FAILURE_SCREEN

    def close(self):
        self.hub.closed += 1


class CountingText(MockTextGenerator):
    def __init__(self):
        self.calls = 0
        self.errors: list[Exception] = []
        self.override = None

    def generate(self, guideline, name, info, images):
        self.calls += 1
        self.last = {"guideline": guideline, "name": name, "info": info, "images": images}
        if self.errors:
            raise self.errors.pop(0)
        result = super().generate(guideline, name, info, images)
        if self.override:
            result.title, result.body = self.override
        return result


class CountingImage(MockImageGenerator):
    def __init__(self):
        self.calls = 0
        self.errors: list[Exception] = []

    def generate(self, guideline, name, info):
        self.calls += 1
        self.last = {"guideline": guideline, "name": name, "info": info}
        if self.errors:
            raise self.errors.pop(0)
        return super().generate(guideline, name, info)


@pytest.fixture
def hub():
    return SiteHub()


@pytest.fixture
def text_ai():
    return CountingText()


@pytest.fixture
def image_ai():
    return CountingImage()


@pytest.fixture
def removed_profiles():
    return []


@pytest.fixture
def worker(engine, settings, store, secrets, hub, text_ai, image_ai, removed_profiles):
    def remover(path):
        removed_profiles.append(Path(path))
        return True

    return Worker(engine, settings, store, secrets, text_factory=lambda: text_ai, image_factory=lambda: image_ai,
                  site_factory=hub.factory, profile_remover=remover, sleep=lambda _s: None)


# --- 도우미 -------------------------------------------------------------------
def make_xlsx(rows: list[list], header: list[str] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "상품"
    ws.append(header or HEADER)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_image(path: Path, color=(30, 120, 200), fmt="PNG", size=(96, 64)) -> Path:
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


def connect(engine, *, biz_id="1001"):
    """당근 계정이 연결된 상태로 만든다."""
    with session_scope(engine) as s:
        execute(s, "UPDATE channel_connection SET status='CONNECTED', biz_id=:b, biz_name='연습용 가게',"
                   " connected_at=:now, updated_at=:now WHERE id=1", b=biz_id, now=utcnow())


def set_ai(engine, secrets, *, claude="API_KEY", gpt="API_KEY"):
    """AI 연결을 저장된 상태로 만든다(실제 호출은 시험용 가짜가 대신한다)."""
    for provider, mode, name in (("CLAUDE", claude, "anthropic_api_key"), ("GPT", gpt, "openai_api_key")):
        if mode == "API_KEY":
            secrets.set(name, f"test-key-{provider.lower()}-0123456789abcdef")
        with session_scope(engine) as s:
            execute(s, "UPDATE ai_connection SET mode=:m, updated_at=:now WHERE provider=:p",
                    m=mode, now=utcnow(), p=provider)


def run_until_idle(worker, limit: int = 30) -> int:
    count = 0
    while count < limit and worker.run_once():
        count += 1
    return count
