"""Aside 를 띄운 뒤 「어느 페이지를 우리 것으로 쓸지」 정하는 규칙(브라우저 없이 가짜 객체로 확인)."""
from __future__ import annotations

from pathlib import Path

from futuremarketer.adapters.aside import AsideBrowser

ONBOARDING = "chrome-extension://abcdefgh/main.html#/onboarding/splash"
LOGIN = "https://business.example/login?redirect=x"


class FakePage:
    def __init__(self, context, url: str, window: int):
        self.context, self.url, self.window = context, url, window
        self.visited: list[str] = []
        self.closed = False

    def goto(self, url, **_kw):
        self.visited.append(url)
        self.url = url

    def close(self):
        self.closed = True
        self.context.pages.remove(self)

    def bring_to_front(self):
        pass

    def wait_for_load_state(self, *_a, **_kw):
        pass


class FakeSession:
    def __init__(self, on_send):
        self.on_send = on_send

    def send(self, method, params=None):
        return self.on_send(method, params or {})


class FakeContext:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.arrivals: list[FakePage] = []      # 기다리는 동안 새로 생길 페이지

    def add(self, url: str, window: int) -> FakePage:
        page = FakePage(self, url, window)
        self.pages.append(page)
        return page

    def wait_for_event(self, _name, timeout=0):
        if self.arrivals:
            self.pages.append(self.arrivals.pop(0))
            return
        raise TimeoutError

    def new_cdp_session(self, page):
        return FakeSession(lambda _m, _p: {"windowId": page.window})


class FakeBrowser:
    def __init__(self, context: FakeContext):
        self.context = context

    def new_browser_cdp_session(self):
        def create(method, params):
            assert method == "Target.createTarget" and params["newWindow"] is True
            self.context.add(params["url"], window=99)
            return {"targetId": "t"}
        return FakeSession(create)


def make(headless: bool):
    browser = AsideBrowser(Path("/nonexistent"), Path("/tmp/profile"), 1, (0, 0, 800, 600), headless=headless)
    context = FakeContext()
    browser._browser = FakeBrowser(context)
    return browser, context


def test_window_mode_uses_the_app_window_and_closes_the_separate_guide_window():
    browser, context = make(headless=False)
    guide = context.add(ONBOARDING, window=1)
    app = context.add(LOGIN, window=2)
    page = browser._adopt_page(context, LOGIN, fresh=True)
    assert page is app and app.visited == []          # --app 으로 이미 열린 페이지라 다시 이동하지 않는다
    assert guide.closed and context.pages == [app]


def test_blank_tab_is_never_adopted():
    browser, context = make(headless=False)
    blank = context.add("about:blank", window=1)      # 곧 Aside 안내 화면으로 바뀔 탭
    context.arrivals.append(FakePage(context, LOGIN, 2))
    page = browser._adopt_page(context, LOGIN, fresh=True)
    assert page is not blank and page.url == LOGIN and not blank.closed


def test_headless_opens_its_own_window():
    browser, context = make(headless=True)
    guide = context.add(ONBOARDING, window=1)
    page = browser._adopt_page(context, LOGIN, fresh=True)
    assert page.window == 99 and page.url == LOGIN    # 새 탭이 아니라 새 창
    assert guide.closed                               # 다른 창의 안내 화면은 닫아도 안전하다


def test_guide_tab_in_the_same_window_is_left_alone():
    browser, context = make(headless=False)
    guide = context.add(ONBOARDING, window=1)
    mine = context.add("https://business.example/other", window=1)
    page = browser._adopt_page(context, LOGIN, fresh=False)
    assert page is mine and mine.visited == [LOGIN]   # 이미 떠 있던 브라우저면 그 페이지에서 다시 연다
    assert not guide.closed                           # 같은 창의 안내 탭을 닫으면 창이 통째로 닫힌다
