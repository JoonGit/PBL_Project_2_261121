"""실제 Aside 창으로 연습용 화면을 조작해 보는 시험.

Aside 를 실제로 실행하므로 평소에는 건너뛴다. 창 없이(헤드리스) 돌리며, 돌리려면:
    FM_BROWSER_TESTS=1 pytest tests/test_browser_practice.py -q
창을 보며 확인하려면 FM_BROWSER_SHOW=1 을 함께 준다.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from futuremarketer.adapters.aside import AsideBrowser, remove_profile
from futuremarketer.adapters.daangn import DaangnSite, practice_urls
from futuremarketer.config import load_settings
from futuremarketer.practice.server import PracticeServer
from tests.conftest import make_image

ASIDE = load_settings().aside_path
pytestmark = pytest.mark.skipif(os.environ.get("FM_BROWSER_TESTS") != "1" or not ASIDE.exists(),
                                reason="실제 브라우저 시험은 FM_BROWSER_TESTS=1 일 때만 돈다")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_login_price_and_news_on_practice_site(tmp_path):
    server = PracticeServer(tmp_path / "data", free_port()).start()
    profile = tmp_path / "profile"
    logs: list[tuple[str, str, bool]] = []
    headless = os.environ.get("FM_BROWSER_SHOW") != "1"       # 창을 보며 확인하려면 FM_BROWSER_SHOW=1
    browser = AsideBrowser(ASIDE, profile, free_port(), (80, 60, 1000, 860), headless=headless)
    site = DaangnSite(browser, practice_urls(server.base_url), on_log=lambda m, lv, shot: logs.append((m, lv, bool(shot))),
                      action_delay=0.2, step_timeout=15, diagnostics_dir=tmp_path / "diagnostics")
    photos = [make_image(tmp_path / "a.png", size=(640, 480)), make_image(tmp_path / "b.png", (200, 40, 40), size=(640, 480))]
    body = "수달 장난감을 소개합니다.\n\n- 터그놀이\n- 노즈워크\n\n판매가격: 24,900원"
    submits: list[str] = []
    try:
        def press_practice_login() -> bool:        # 사람 대신 연습용 로그인 버튼을 눌러 준다
            button = site.page.get_by_role("button", name="연습 계정으로 로그인")
            if button.count() and button.first.is_visible():
                button.first.click()
            return False

        account = site.login(60, press_practice_login)
        assert account == {"biz_id": "1001", "biz_name": "연습용 가게", "public_id": "7001"}   # 손님이 보는 업체 번호까지
        site.close()                                # 로그인 뒤 창을 닫아도

        assert site.is_logged_in("1001") is True    # 다시 열면 로그인이 유지된다(쿠키가 프로필에 남음)
        name = "수달 3IN1 멀티 강아지 장난감"            # 16자: 항목 칸(15자)에 맞춰 줄이고 전체 이름은 추가 설명에
        first = site.register_price("1001", name, 24900, photos[0], before_submit=lambda: submits.append("price"))
        assert first == {"action": "ADDED", "item_name": "수달 3IN1 멀티 강아지", "photo": True}
        same = site.register_price("1001", name, 24900, photos[0], before_submit=lambda: submits.append("same"))
        assert same["action"] == "UNCHANGED"           # 같은 내용이면 저장하지 않는다
        moved = site.register_price("1001", name, 22900, photos[0], before_submit=lambda: submits.append("price2"))
        assert moved["action"] == "UPDATED"            # 같은 이름이면 새로 만들지 않고 그 항목을 고친다

        site._goto(f"{server.base_url}/biz_accounts/1001/manager/posts/new")     # 쓰다 만 소식을 남겨 둔다
        site.page.get_by_placeholder("소식 제목").press_sequentially("쓰다 만 글")
        site.page.wait_for_timeout(300)
        long_title = "수달 장난감 들어왔어요 터그놀이 노즈워크 한 번에 되는 멀티 장난감"
        result = site.write_news("1001", long_title, body, photos, before_submit=lambda: submits.append("news"))
        assert "/manager/posts" in result["url"] and "postId=" in result["url"] and result["images"] == 2
        urls = practice_urls(server.base_url)
        site.page.goto(urls.post_share(result["url"]))                  # 공유 주소로 손님이 보는 소식이 열린다
        assert site.page.get_by_text(result["title"], exact=True).count() == 1
        site.page.goto(urls.profile_page(site.public_id or account["public_id"]))
        assert site.page.get_by_role("heading", name="연습용 가게").count() == 1
    finally:
        site.close()
        server.stop()

    state = json.loads((tmp_path / "data" / "practice" / "state.json").read_text(encoding="utf-8"))
    assert len(state["prices"]) == 1                    # 가격표에 두 번 올라가지 않았다
    price = state["prices"][0]
    assert (price["name"], price["price"], price["description"]) == ("수달 3IN1 멀티 강아지", 22900, name)
    assert price["image"] and (tmp_path / "data" / "practice" / "files" / price["image"]).is_file()
    post = state["posts"][0]
    assert post["title"] == result["title"] and len(post["title"]) <= 30 and post["title"] != long_title
    assert post["body"] == body and len(post["images"]) == 2
    assert submits == ["price", "price2", "news"]
    messages = [m for m, _lv, _shot in logs]
    assert any("새로 쓰기" in m for m in messages)                  # 「작성 중인 소식이 있어요」를 닫았다
    assert any("직접 눌렀습니다" in m for m in messages)            # 채팅 버튼에 가린 「다음」「완료」를 눌렀다
    order = [i for i, m in enumerate(messages) if "사진 2장을 올렸습니다" in m or "제목과 본문을 입력" in m]
    assert len(order) == 2 and "사진" in messages[order[0]]          # 사진을 모두 올린 뒤에 글을 쓴다
    assert any(shot for _m, _lv, shot in logs) and not (tmp_path / "diagnostics").exists()
    final = logs[-1]
    assert "등록된 소식 화면" in final[0] and final[2]              # 올라간 소식 화면이 마지막 캡처로 남는다

    assert remove_profile(profile) is True          # 연결 해제: 저장된 로그인 정보가 지워진다
    assert not Path(profile).exists()


def test_remove_price_and_news_on_practice_site(tmp_path):
    """연습용 화면에서 가격 항목(수정 창의 삭제하기 → 저장)과 소식(⋮ → 삭제 → 확인 창)을 지운다."""
    server = PracticeServer(tmp_path / "data", free_port()).start()
    logs: list[tuple[str, str, bool]] = []
    headless = os.environ.get("FM_BROWSER_SHOW") != "1"
    browser = AsideBrowser(ASIDE, tmp_path / "profile", free_port(), (80, 60, 1000, 860), headless=headless)
    site = DaangnSite(browser, practice_urls(server.base_url), on_log=lambda m, lv, shot: logs.append((m, lv, bool(shot))),
                      action_delay=0.2, step_timeout=15, diagnostics_dir=tmp_path / "diagnostics")
    photo = make_image(tmp_path / "a.png", size=(640, 480))
    body = "삭제 시험용 소식입니다.\n\n판매가격: 12,000원"
    submits: list[str] = []
    try:
        def press_practice_login() -> bool:
            button = site.page.get_by_role("button", name="연습 계정으로 로그인")
            if button.count() and button.first.is_visible():
                button.first.click()
            return False

        site.login(60, press_practice_login)
        for name in ("지울 상품 하나", "남길 상품 둘"):
            site.register_price("1001", name, 12000, photo, before_submit=lambda: None)
        news = [site.write_news("1001", title, body, [photo], before_submit=lambda: None)
                for title in ("지울 소식 제목 하나", "남길 소식 제목 둘")]

        assert site.remove_price("1001", "지울 상품 하나", before_submit=lambda: submits.append("price"))["action"] == "REMOVED"
        assert site.remove_news("1001", news[0]["title"], before_submit=lambda: submits.append("news"))["action"] == "REMOVED"
        assert site.remove_price("1001", "지울 상품 하나", before_submit=lambda: submits.append("again"))["action"] == "ABSENT"
        assert site.remove_news("1001", news[0]["title"], before_submit=lambda: submits.append("again"))["action"] == "ABSENT"
    finally:
        site.close()
        server.stop()

    state = json.loads((tmp_path / "data" / "practice" / "state.json").read_text(encoding="utf-8"))
    assert [p["name"] for p in state["prices"]] == ["남길 상품 둘"]            # 고른 것만 지워졌다
    assert [p["title"] for p in state["posts"]] == ["남길 소식 제목 둘"]
    assert submits == ["price", "news"]                                       # 이미 없는 것은 누르지 않는다
    assert any("삭제 확인 창" in m for m, _lv, _s in logs)                       # 소식 삭제의 확인 창을 눌렀다


def test_choose_second_profile_on_practice_site(tmp_path):
    """한 로그인으로 가게가 둘일 때: 고르기 화면에서 2호점을 고르면 그곳에 올라간다."""
    server = PracticeServer(tmp_path / "data", free_port()).start()
    headless = os.environ.get("FM_BROWSER_SHOW") != "1"
    browser = AsideBrowser(ASIDE, tmp_path / "profile", free_port(), (80, 60, 1000, 860), headless=headless)
    site = DaangnSite(browser, practice_urls(server.base_url), on_log=lambda m, lv, shot: None,
                      action_delay=0.2, step_timeout=15, diagnostics_dir=tmp_path / "diagnostics")
    photo = make_image(tmp_path / "a.png", size=(640, 480))
    try:
        def press_login() -> bool:
            button = site.page.get_by_role("button", name="연습 계정으로 로그인")
            if button.count() and button.first.is_visible():
                button.first.click()
            return False

        assert site.login(60, press_login)["biz_id"] == "1001"
        site.close()

        def pick_second() -> bool:                       # 사람 대신 고르기 화면에서 2호점을 누른다
            link = site.page.get_by_role("link", name="연습용 가게 2호점")
            if link.count() and link.first.is_visible():
                link.first.click()
            return False

        chosen = site.login(60, pick_second, choose=True)
        assert chosen == {"biz_id": "1002", "biz_name": "연습용 가게 2호점", "public_id": "7002"}
        site.register_price("1002", "2호점 상품", 5000, photo, before_submit=lambda: None)
    finally:
        site.close()
        server.stop()
    root = tmp_path / "data" / "practice"
    assert [p["name"] for p in json.loads((root / "state-1002.json").read_text(encoding="utf-8"))["prices"]] == ["2호점 상품"]
    assert not (root / "state.json").exists() or json.loads((root / "state.json").read_text(encoding="utf-8"))["prices"] == []
