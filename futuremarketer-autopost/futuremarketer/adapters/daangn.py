"""당근 비즈니스 화면을 사람이 하듯 조작한다(페이지 객체).

- 주소와 화면 글자는 당근 비즈니스 웹의 공개 스크립트에서 확인한 것을 쓴다.
    소식 작성  {base}/biz_accounts/{bizId}/manager/posts/new
    가격표     {base}/biz_accounts/{bizId}/manager/pricing
- 같은 코드를 연습용 화면(practice)에도 쓴다. 연습용 화면은 같은 주소 모양과 같은 글자를 쓴다.
- 요소를 찾지 못하면 추측해서 누르지 않고 멈춘다. 그때의 화면은 앱 데이터 폴더의 diagnostics 에 남긴다.
- 칸마다 글자 수 제한이 있다(가격 항목 15자, 추가 설명 50자, 소식 제목 30자). 넘치면 띄어쓰기에서 줄여 넣는다.
"""
from __future__ import annotations

import contextlib
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlsplit

from PIL import Image, ImageOps

from ..content import fit_text

BIZ_RE = re.compile(r"/biz_accounts/(\d+)(?:/|$)")
PRE_DIALOGS = ("새로 쓰기", "직접 작성")          # 「작성 중인 소식이 있어요」처럼 소식 작성에 들어갈 때 뜨는 물음
POST_DIALOGS = ("첨부하지 않기", "아니오")        # 완료를 누른 뒤 뜰 수 있는 권유
QUOTA_TEXT = re.compile("소식 업로드가 불가능해요|작성 가능한 소식 5개를 모두")
PRICE_NAME_MAX = 15        # 가격 항목 이름 칸은 15자까지 받는다
PRICE_NOTE_MAX = 50        # 가격 추가 설명 칸은 50자까지
NEWS_TITLE_MAX = 30        # 소식 제목 칸은 30자까지
NEWS_IMAGE_MAX = 10        # 소식 사진은 10장까지
UPLOAD_IMAGE_LIMIT = 7_000_000
RESTRICTION_TEXT = re.compile(r"비즈프로필이 제재되어 이용할 수 없|운영정책을 위반해 활동이 제한")
PRICE_ACCESS_TEXT = re.compile(r"권한이\s*(?:없|필요)|접근할 수 없|비즈프로필이 제재되어 이용할 수 없")
PHOTO_COUNTER = re.compile(r"^\s*\d+\s*/\s*10\s*$")     # 소식 사진 버튼에 보이는 「0/10」
SORTABLE = '[aria-roledescription="sortable"]'           # 소식에 붙인 사진 하나하나(끌어서 순서를 바꾸는 칸)


@dataclass(frozen=True)
class SiteUrls:
    label: str
    base: str
    login: str
    session_re: re.Pattern      # 이 응답이 200 이면 로그인된 상태
    cookie_domains: tuple[str, ...]   # 로그인 쿠키가 놓이는 도메인(끝부분 일치)
    public: str = "https://www.daangn.com"   # 손님이 보는 당근 화면(업체 프로필·공유된 소식)

    def profile_page(self, public_id: str | None) -> str | None:
        """당근에서 손님이 보는 업체 프로필. public_id 는 비즈프로필 번호와 다른 「동네 업체」 번호다."""
        return f"{self.public}/kr/local-profile/{public_id}/" if public_id else None

    def post_share(self, news_url: str | None) -> str | None:
        """소식 「공유하기」가 주는 주소. 소식을 올린 뒤의 관리 화면 주소에 붙은 postId 로 만든다."""
        match = re.search(r"[?&]postId=([0-9A-Za-z]+)", news_url or "")
        return f"{self.public}/kr/business-posts/{match[1]}" if match else None


def real_urls() -> SiteUrls:
    return SiteUrls(
        label="당근 비즈니스",
        base="https://bizprofile.daangn.com",
        login="https://business.daangn.com/login?redirect=https%3A%2F%2Fbizprofile.daangn.com%2F",
        session_re=re.compile(r"business-gateway\.daangn\.com/auth/api/v\d+/auths/me"),
        cookie_domains=("daangn.com",),
    )


def practice_urls(base: str) -> SiteUrls:
    return SiteUrls(label="연습용 화면", base=base, login=f"{base}/login",
                    session_re=re.compile(re.escape(base) + r"/api/me$"), cookie_domains=("127.0.0.1",), public=base)


class SiteError(Exception):
    """unknown=True: 제출 버튼을 누른 뒤라 등록됐는지 알 수 없다.
    definite=True: 제출 뒤에 났지만 화면이 「등록되지 않았다」고 분명히 알려 준 경우."""

    def __init__(self, code: str, message: str, *, unknown: bool = False, definite: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.unknown = unknown
        self.definite = definite


@contextlib.contextmanager
def _upload_files(paths: list[Path]) -> Iterator[list[str]]:
    """당근에 보낼 7MB 이상의 사진만 임시 JPEG로 줄인다."""
    with contextlib.ExitStack() as cleanup:
        folder = None
        files = []
        for index, path in enumerate(paths):
            try:
                if path.stat().st_size < UPLOAD_IMAGE_LIMIT:
                    files.append(str(path))
                    continue
                if folder is None:
                    folder = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="fm-upload-")))
                output = folder / f"{index}-{path.stem}.jpg"
                with Image.open(path) as source:
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                    image = image.convert("RGBA")
                    jpeg = Image.new("RGB", image.size, "white")
                    jpeg.paste(image, mask=image.getchannel("A"))
                    jpeg.save(output, format="JPEG", quality=88, optimize=True)
                if output.stat().st_size >= UPLOAD_IMAGE_LIMIT:
                    raise SiteError("UPLOAD_FAILED", f"사진을 7MB 미만으로 줄이지 못했습니다: {path.name}")
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise SiteError("UPLOAD_FAILED", f"업로드할 사진을 준비하지 못했습니다: {path.name}") from exc
            files.append(str(output))
        yield files


def price_description(script: str) -> str:
    """가능하면 문장을 끝까지 담고, 긴 첫 문장은 입력 칸 길이에 맞춘다."""
    text = " ".join(script.split())
    note = ""
    for sentence in re.split(r"(?<=[.!?。！？])\s+", text):
        candidate = f"{note} {sentence}".strip()
        if len(candidate) > PRICE_NOTE_MAX:
            break
        note = candidate
    return note or fit_text(text, PRICE_NOTE_MAX)


LogFn = Callable[[str, str, bytes | None], None]


class DaangnSite:
    def __init__(self, browser, urls: SiteUrls, *, on_log: LogFn | None = None, action_delay: float = 0.5,
                 step_timeout: float = 20, diagnostics_dir: Path | None = None):
        self.browser = browser
        self.urls = urls
        self.page = None
        self.action_delay = action_delay
        self.step_timeout = step_timeout
        self.diagnostics_dir = diagnostics_dir
        self._on_log = on_log or (lambda message, level, shot: None)
        self._session: bool | None = None
        self.public_id: str | None = None
        self._saw_select = False

    # --- 공통 도구 ---------------------------------------------------------
    def _log(self, message: str, level: str = "INFO", shot: bool = False) -> None:
        self._on_log(message, level, self._shot() if shot else None)

    def _shot(self) -> bytes | None:
        try:
            return self.page.screenshot(type="jpeg", quality=60)
        except Exception:
            return None

    def capture(self) -> bytes | None:
        """지금 화면. 등록이 멈췄을 때 그 순간을 작업 기록에 남기려고 쓴다."""
        return self._shot() if self.page is not None else None

    def _press(self, locator, what: str) -> None:
        """버튼을 누른다. 화면 구석에 떠 있는 채팅 버튼 같은 것이 버튼을 가리고 있으면
        마우스로는 누를 수 없으므로, 그 버튼에 직접 「누름」 신호를 보낸다."""
        try:
            locator.click(trial=True, timeout=1500)      # 누르지는 않고 누를 수 있는지만 본다
        except Exception:
            locator.dispatch_event("click", timeout=5000)
            self._log(f"「{what}」 버튼이 다른 요소(채팅 버튼 등)에 가려 있어 버튼을 직접 눌렀습니다.")
            return
        try:
            locator.click(timeout=5000)
        except Exception:      # 창이 움직이는 중이거나 다른 층이 덮고 있으면 기다리지 않고 버튼에 바로 신호를 보낸다
            locator.dispatch_event("click", timeout=5000)
            self._log(f"「{what}」 버튼을 마우스로 누를 수 없어 버튼을 직접 눌렀습니다.")

    def _on_response(self, response) -> None:
        with contextlib.suppress(Exception):
            if "/select_profile" in response.url:
                self._saw_select = True
            if self.urls.session_re.search(response.url):
                self._session = response.status == 200

    def open(self, url: str) -> None:
        self.page = self.browser.start(url)
        self.browser.context.on("response", self._on_response)

    def close(self) -> None:
        if self.page is not None:       # 닫기 직전의 쿠키 상태로 로그인을 남긴다
            with contextlib.suppress(Exception):
                self._keep_session_cookies()
        self.browser.close()
        self.page = None

    def _pump(self, ms: float) -> None:
        """기다리는 동안에도 브라우저 이벤트를 받는다."""
        try:
            self.page.wait_for_timeout(ms)
        except Exception:
            time.sleep(ms / 1000)

    def _goto(self, url: str) -> None:
        """주소로 이동한다. 앞 화면이 스스로 다른 화면으로 넘어가는 중이면(관리자 홈 → 프로필 채우기 등)
        이동이 중단될 수 있어, 잠깐 기다렸다 두 번까지 다시 해 본다."""
        self._session = None
        for attempt in range(3):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=self.step_timeout * 1000)
                break
            except Exception as exc:
                if "ERR_ABORTED" not in str(exc) or attempt == 2:
                    raise
                self._pump(800)
        self._pump(self.action_delay * 1000)

    def _is_login_url(self, url: str) -> bool:
        return (url.startswith(self.urls.login.split("?")[0]) or "accounts.daangn.com" in url
                or "error=login_required" in url or re.search(r"/login(\?|$)", url) is not None)

    def _visible(self, locator) -> bool:
        try:
            return locator.count() > 0 and locator.first.is_visible()
        except Exception:
            return False

    def _dismiss(self, names: tuple[str, ...]) -> None:
        for name in names:
            button = self.page.get_by_role("button", name=name, exact=True)
            if self._visible(button):
                with contextlib.suppress(Exception):
                    button.first.click()
                    self._log(f"안내 창에서 「{name}」을(를) 눌렀습니다.")
                    self._pump(self.action_delay * 1000)

    def _find(self, what: str, *candidates, timeout: float | None = None, required: bool = True,
              dismiss: tuple[str, ...] = ()):
        """후보 가운데 화면에 보이는 첫 요소. 못 찾으면 멈춘다(추측해서 누르지 않는다)."""
        deadline = time.monotonic() + (self.step_timeout if timeout is None else timeout)
        while True:
            for make in candidates:
                locator = make(self.page)
                if self._visible(locator):
                    return locator.first
            if dismiss:
                self._dismiss(dismiss)
            if time.monotonic() > deadline:
                break
            self._pump(250)
        if not required:
            return None
        saved = self._diagnose(what)
        raise SiteError("ELEMENT_NOT_FOUND", f"화면에서 「{what}」을(를) 찾지 못했습니다. 화면 구성이 바뀌었을 수 있습니다.{saved}")

    def _ready(self, locator, what: str, dismiss: tuple[str, ...] = ()) -> None:
        """누를 수 있는 상태가 될 때까지 기다린다. 안내 창이 가리고 있으면 그 창부터 닫는다."""
        deadline = time.monotonic() + self.step_timeout
        while time.monotonic() < deadline:
            try:
                locator.click(trial=True, timeout=800)
                return
            except Exception:
                if dismiss:
                    self._dismiss(dismiss)
                self._pump(200)
        saved = self._diagnose(what)
        raise SiteError("ELEMENT_BLOCKED", f"「{what}」이(가) 다른 창에 가려 누를 수 없습니다.{saved}")

    def _wait_enabled(self, locator, what: str) -> None:
        deadline = time.monotonic() + self.step_timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                if locator.is_enabled():
                    return
            self._pump(250)
        saved = self._diagnose(what)
        raise SiteError("ELEMENT_DISABLED", f"「{what}」이(가) 눌리지 않는 상태입니다. 입력값을 확인해 주세요.{saved}")

    def _wait_until(self, condition: Callable[[], bool], timeout: float, dismiss: tuple[str, ...] = ()) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                if condition():
                    return True
            if dismiss:
                self._dismiss(dismiss)
            self._pump(300)
        return False

    def _diagnose(self, what: str) -> str:
        """찾지 못한 순간의 화면을 내 PC 에만 남긴다(선택자를 고칠 때 본다)."""
        if not self.diagnostics_dir:
            return ""
        try:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^0-9A-Za-z가-힣]+', '_', what)[:30]}"
            (self.diagnostics_dir / f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
            shot = self._shot()
            if shot:
                (self.diagnostics_dir / f"{stem}.jpg").write_bytes(shot)
            return f" (그때의 화면: 앱 데이터 폴더의 diagnostics/{stem}.html)"
        except Exception:
            return ""

    def _ensure_authed(self) -> None:
        """관리 화면을 연 직후 로그인 화면으로 튕기는지 잠깐 지켜본다."""
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if self._is_login_url(self.page.url) or self._session is False:
                raise SiteError("LOGIN_REQUIRED", "당근 로그인이 풀렸습니다. 계정 설정에서 다시 연결해 주세요.")
            if self._session is True:
                return
            self._pump(300)

    # --- 로그인 ------------------------------------------------------------
    def _find_biz(self):
        for page in self.browser.context.pages:
            if page.url.startswith(self.urls.base) and not self._is_login_url(page.url):
                match = BIZ_RE.search(page.url)
                if match:
                    return page, match.group(1)
        return None

    def _biz_name(self) -> str | None:
        with contextlib.suppress(Exception):
            name = self.page.evaluate("() => document.querySelector('[data-biz-name]')?.textContent || ''")
            return name.strip()[:100] or None
        return None

    def _keep_session_cookies(self, days: int = 30) -> int:
        """창을 닫아도 로그인이 남게 한다.

        「브라우저를 닫으면 사라지는」 쿠키는 창을 닫는 순간 없어진다. 로그인 쿠키가 그런 종류면
        닫고 나서 다시 로그인해야 하므로, 이 사이트의 그런 쿠키에 만료일을 붙여 전용 프로필에 남긴다.
        """
        context = self.browser.context
        expires = time.time() + days * 86400
        kept = 0
        for cookie in context.cookies():
            domain = cookie.get("domain", "").lstrip(".")
            if cookie.get("expires", -1) > 0 or not any(domain == d or domain.endswith("." + d) for d in self.urls.cookie_domains):
                continue
            item = {key: cookie[key] for key in ("name", "value", "domain", "path", "httpOnly", "secure", "sameSite")
                    if key in cookie}
            item["expires"] = expires
            with contextlib.suppress(Exception):      # 브라우저가 받아 주지 않는 쿠키는 그대로 둔다
                context.add_cookies([item])
                kept += 1
        return kept

    def login(self, timeout: float, should_cancel: Callable[[], bool], choose: bool = False) -> dict:
        """로그인 화면을 띄우고 사람이 로그인할 때까지 기다린다. 비밀번호·인증번호는 앱이 다루지 않는다.

        choose=True 면 비즈프로필 고르기 화면부터 띄운다(한 로그인으로 여러 비즈프로필을 관리할 때).
        그 화면을 거친 뒤에 들어간 비즈프로필을 고른 것으로 본다. 로그인이 풀려 있으면 로그인부터 하게 된다."""
        self._saw_select = False
        if choose:
            self.open(f"{self.urls.base}/select_profile")
            self._log("Aside 창에서 등록에 쓸 비즈프로필을 골라 주세요.", shot=True)
        else:
            self.open(self.urls.login)
            self._log(f"Aside 창에서 {self.urls.label}에 로그인해 주세요.", shot=True)
        deadline = time.monotonic() + timeout
        session_seen_at = None
        hinted = False
        candidate = None
        while time.monotonic() < deadline:
            if should_cancel():
                raise SiteError("CANCELED", "연결을 취소했습니다.")
            pages = [p for p in self.browser.context.pages if not p.is_closed()]
            if not pages:
                raise SiteError("WINDOW_CLOSED", "로그인 창이 닫혔습니다. 다시 연결해 주세요.")
            if self.page.is_closed():
                self.page = pages[0]
            found = self._find_biz()
            if choose and not self._saw_select:       # 고르기 화면을 거치기 전의 비즈프로필 화면은 고른 것이 아니다
                found = None
            if found and self._session is not False:
                if candidate == found[1]:        # 두 번 연속 같은 주소에 머물러야 로그인으로 본다
                    self.page = found[0]
                    self._pump(1200)
                    self._keep_session_cookies()
                    result = {"biz_id": found[1], "biz_name": self._biz_name()}
                    with contextlib.suppress(Exception):          # 손님이 보는 업체 프로필 번호(없어도 연결은 된다)
                        self._goto(f"{self.urls.base}/biz_accounts/{found[1]}/manager/home")
                        result["public_id"] = self.read_public_id()
                    self._log("로그인을 확인했습니다.", "DONE", shot=True)
                    return result
                candidate = found[1]
                self._pump(1500)
                continue
            candidate = None
            if self._session and session_seen_at is None:
                session_seen_at = time.monotonic()
            if session_seen_at and not hinted and time.monotonic() - session_seen_at > 8:
                hinted = True
                self._log("관리할 비즈프로필을 Aside 창에서 선택해 주세요.")
                with contextlib.suppress(Exception):
                    self.page.goto(f"{self.urls.base}/select_profile", wait_until="domcontentloaded")
            self._pump(500)
        raise SiteError("LOGIN_TIMEOUT", "시간 안에 로그인이 확인되지 않았습니다. 다시 연결해 주세요.")

    PUBLIC_ID_JS = """() => { for (const el of document.querySelectorAll('[data-log-click-params],[data-log-impression-params]')) {
        for (const name of ['data-log-click-params', 'data-log-impression-params']) {
          try { const v = JSON.parse(el.getAttribute(name) || '{}'); if (v.lp_id) return String(v.lp_id) } catch (e) {} } }
      return null }"""

    def read_public_id(self, wait: float = 5) -> str | None:
        """관리자 홈에 심겨 있는 「동네 업체」 번호(손님이 보는 업체 프로필 주소에 쓰인다). 못 찾으면 None."""
        deadline = time.monotonic() + wait
        while True:
            with contextlib.suppress(Exception):
                found = self.page.evaluate(self.PUBLIC_ID_JS)
                if found and str(found).isdigit():
                    return str(found)
            if time.monotonic() > deadline:
                return None
            self._pump(400)

    def is_logged_in(self, biz_id: str) -> bool:
        home = f"{self.urls.base}/biz_accounts/{biz_id}/manager/home"
        if self.page is None:
            self.open(home)
        self._goto(home)      # 응답을 지켜보기 시작한 뒤에 한 번 더 연다(처음 연 순간의 응답은 놓칠 수 있다)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._is_login_url(self.page.url) or self._session is False:
                return False
            if self._session is True:
                self.public_id = self.read_public_id()
                return True
            self._pump(300)
        # 로그인 화면으로 튕기지도, 거절 응답을 받지도 않았다면 로그인된 것으로 보고 진행한다.
        # (정말 풀려 있었다면 다음 단계가 로그인 화면을 만나 멈춘다.)
        if self.page.url.startswith(self.urls.base):
            self.public_id = self.read_public_id(wait=2)
            return True
        return False

    def business_verification(self, biz_id: str) -> str:
        """소식 작성 제한과 사업자 증빙을 별도로 읽는다. 업체별 편집 화면에 입력하는 일은 없다."""
        base = f"{self.urls.base}/biz_accounts/{biz_id}/manager"
        info = f"{base}/home/update/base_info"
        def at(expected: str) -> bool:
            current = urlsplit(self.page.url)
            if self._is_login_url(self.page.url) or self._session is False:
                raise SiteError("LOGIN_REQUIRED", "당근 로그인이 풀렸습니다.")
            wanted = urlsplit(expected)
            return current.netloc == wanted.netloc and current.path.rstrip("/") == wanted.path.rstrip("/")

        editor = f"{base}/posts/new"
        self._goto(editor)
        restricted = self.page.get_by_text(RESTRICTION_TEXT)
        title = self.page.get_by_placeholder("소식 제목", exact=True)
        start = self.page.get_by_role("button", name=re.compile(r"^(새로 쓰기|직접 작성)$"))
        with contextlib.suppress(Exception):
            restricted.or_(title).or_(start).first.wait_for(state="visible", timeout=6000)
        if not at(editor):
            return "EDITOR_UNAVAILABLE"
        if self._visible(restricted):
            return "RESTRICTED"
        if not (self._visible(title) or self._visible(start)):
            return "EDITOR_UNAVAILABLE"

        self._goto(info)
        section = self.page.get_by_text("증빙 서류", exact=True)
        if not at(info):
            return "EDITOR_UNAVAILABLE"
        with contextlib.suppress(Exception):
            section.or_(restricted).first.wait_for(state="visible", timeout=6000)
        if self._visible(restricted):
            return "RESTRICTED"
        if not self._visible(section) or not at(info):
            return "UNKNOWN"
        # 같은 칸에 활동 증빙 등 다른 서류도 있다. 사업자등록증 행의 상태만 판정한다.
        rows = section.locator("xpath=../..").locator(":scope > *").filter(has_text=re.compile("사업자등록증|사업자 인증"))
        if rows.count() != 1 or not rows.first.is_visible():
            return "UNKNOWN"
        label = " ".join(rows.first.inner_text().split())
        if re.search(r"등록증\s*추가|미인증|미등록|미완료|심사\s*중|검수\s*중|대기|반려|만료|취소|거절", label):
            return "UNVERIFIED"
        if "활동 증빙" in label:
            return "UNKNOWN"
        approved = re.fullmatch(r"사업자\s*(?:등록증|정보)?\s*[·:|]?\s*(?:(?:인증|승인|검수)\s*완료|승인됨|인증됨)", label)
        return "VERIFIED" if approved else "UNKNOWN"

    # --- 가격 등록 ----------------------------------------------------------
    def _price_edit_button(self, item_name: str):
        """가격 목록에서 이름이 같은 항목의 「수정」 버튼. 없으면 None."""
        names = self.page.get_by_text(item_name, exact=True)
        for idx in range(names.count()):
            node = names.nth(idx)
            if not self._visible(node):
                continue
            row = node.locator("xpath=ancestor::*[.//button[normalize-space()='수정']][1]")
            buttons = row.get_by_role("button", name="수정", exact=True)
            if buttons.count() == 1:
                return buttons.first
            saved = self._diagnose("가격항목")
            raise SiteError("ELEMENT_NOT_FOUND", f"같은 이름의 가격 항목은 있지만 어느 「수정」 버튼인지 알 수 없습니다.{saved}")
        return None

    def _price_photo(self, dialog, image_path: Path) -> bool:
        """가격 창의 「사진」 칸에 상품 사진을 올린다. 이미 사진이 있으면 그대로 둔다. 새로 올렸으면 True."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if self._visible(dialog.locator('img[alt="가격 이미지"]')):
            self._log("이 가격 항목에는 사진이 이미 있어 그대로 둡니다.")
            return False
        button = self._find("가격 사진 버튼", lambda p: dialog.locator(
            "xpath=.//*[self::h2 or self::h3 or self::h4][normalize-space()='사진']/following::button[1]"))
        with _upload_files([image_path]) as files:
            try:
                with self.page.expect_file_chooser(timeout=8000) as chooser:
                    button.click()
                chooser.value.set_files(files)
            except PlaywrightTimeout:
                saved = self._diagnose("가격사진")
                raise SiteError("ELEMENT_NOT_FOUND", f"가격 사진을 고르는 창이 열리지 않았습니다.{saved}") from None
            self._log("가격 항목에 상품 사진을 올리는 중입니다.")
            if not self._wait_until(lambda: self._visible(dialog.locator('img[alt="가격 이미지"]')), timeout=90):
                saved = self._diagnose("가격사진_업로드")
                raise SiteError("UPLOAD_TIMEOUT", f"가격 사진 올리기가 끝나지 않았습니다.{saved}")
        return True

    @staticmethod
    def _retype(field, value: str) -> None:
        """입력란의 글을 모두 지우고 새로 쓴다."""
        field.click()
        field.press("ControlOrMeta+a")
        field.press("Backspace")
        field.press_sequentially(value, delay=25)

    def register_price(self, biz_id: str, name: str, price: int, image_path: Path | None = None, *,
                       before_submit: Callable[[], None], description: str | None = None) -> dict:
        """가격표에 상품 하나를 올린다(사진·항목·정가·추가 설명).

        같은 이름의 항목이 이미 있으면 새로 만들지 않고 그 항목을 고친다(두 번 올라가지 않게).
        돌려주는 값: {"action": ADDED|UPDATED|UNCHANGED, "item_name": 실제로 쓴 항목 이름, "photo": 사진이 붙어 있는지}
        """
        full = " ".join(name.split())
        item_name = fit_text(full, PRICE_NAME_MAX)
        if description is not None:
            note = price_description(description)
        else:
            note = fit_text(full, PRICE_NOTE_MAX) if item_name != full else ""
        self._log("가격표 화면을 엽니다.")
        pricing_url = f"{self.urls.base}/biz_accounts/{biz_id}/manager/pricing"
        self._goto(pricing_url)
        self._ensure_authed()
        location, expected = urlsplit(self.page.url), urlsplit(pricing_url)
        restricted = self.page.get_by_text(PRICE_ACCESS_TEXT)
        if ((location.netloc, location.path.rstrip("/")) != (expected.netloc, expected.path)
                or self._visible(restricted)):
            raise SiteError("PREVIEW_ONLY", "게시 보류 · 선택한 비즈프로필의 가격표 작성 화면을 이용할 수 없습니다.")
        add = self._find("가격 추가 버튼",
                         lambda p: p.get_by_role("button", name=re.compile("가격 추가")),
                         lambda p: p.get_by_text("가격 추가", exact=True))
        with contextlib.suppress(Exception):           # 가격 목록을 다 받아 온 뒤에 같은 이름을 찾는다
            self.page.wait_for_load_state("networkidle", timeout=4000)
        if description is not None and note:
            self._log(f"준비한 원고를 가격 추가 설명에 {len(note)}자로 입력합니다.")
        elif note:
            self._log(f"항목 이름 칸은 {PRICE_NAME_MAX}자까지라 「{item_name}」(으)로 줄이고, 전체 이름은 추가 설명에 적습니다.")
        edit = self._price_edit_button(item_name)
        if edit is not None:
            self._log(f"같은 이름의 가격 항목이 있어 새로 만들지 않고 그 항목을 고칩니다: {item_name}")
            edit.click()
            heading = "가격 수정"
        else:
            add.click()
            heading = "가격 추가"
        self._pump(self.action_delay * 1000)
        title = self._find(f"「{heading}」 창", lambda p: p.get_by_role("heading", name=heading, exact=True))
        dialog = title.locator("xpath=ancestor::*[@role='dialog'][1]")
        changed = edit is None
        photo = self._visible(dialog.locator('img[alt="가격 이미지"]'))
        if image_path is not None:
            changed = self._price_photo(dialog, Path(image_path)) or changed
            photo = True
        if edit is None:
            item = self._find("항목 이름 입력란",
                              lambda p: dialog.get_by_placeholder(re.compile("항목명을 입력")),
                              lambda p: dialog.get_by_label("항목", exact=True))
            item.click()
            item.press_sequentially(item_name, delay=25)
        fixed = self._find("정가 선택", lambda p: dialog.get_by_text("정가", exact=True), required=False, timeout=1.5)
        if fixed is not None:
            with contextlib.suppress(Exception):
                fixed.click()
        price_input = self._find("가격 입력란",
                                 lambda p: dialog.get_by_placeholder(re.compile("가격을 입력")),
                                 lambda p: dialog.locator('input[inputmode="numeric"]'))
        if re.sub(r"\D", "", price_input.input_value() or "") != str(int(price)):
            self._retype(price_input, str(int(price)))
            changed = True
        if note:
            box = self._find("추가 설명 입력란",
                             lambda p: dialog.get_by_placeholder(re.compile("설명이 필요하다면")),
                             lambda p: dialog.locator("textarea"), required=description is not None, timeout=2)
            if box is not None and (box.input_value() or "").strip() != note:
                self._retype(box, note)
                changed = True
        self._log(f"가격 항목을 입력했습니다: {item_name} · {int(price):,}원" + (" · 사진 1장" if photo else ""), shot=True)
        if not changed:
            self.page.keyboard.press("Escape")
            self._log("가격표에 이미 같은 내용이 있어 저장하지 않고 닫습니다.", "DONE", shot=True)
            return {"action": "UNCHANGED", "item_name": item_name, "photo": photo}
        done = self._find("완료 버튼", lambda p: dialog.get_by_role("button", name="완료", exact=True))
        self._wait_enabled(done, "완료 버튼")
        self._press(done, "완료")
        self._pump(self.action_delay * 1000)
        save = self._find("저장 버튼", lambda p: p.get_by_role("button", name="저장", exact=True))
        self._wait_enabled(save, "저장 버튼")
        before_submit()
        self._press(save, "저장")
        self._log("저장을 눌렀습니다. 결과를 확인합니다.")
        saved = self._wait_until(
            lambda: self._visible(self.page.get_by_text(re.compile("가격표 정보를 수정했어요|저장했어요|저장되었"))),
            timeout=self.step_timeout)
        if not saved:
            raise SiteError("RESULT_UNKNOWN", "가격 저장 결과를 확인하지 못했습니다.", unknown=True)
        self._log("가격을 등록했습니다." if edit is None else "가격 항목을 고쳤습니다.", "DONE", shot=True)
        return {"action": "ADDED" if edit is None else "UPDATED", "item_name": item_name, "photo": photo}

    # --- 소식 등록 ----------------------------------------------------------
    def _type_body(self, editor, body: str) -> None:
        tag = editor.evaluate("el => el.tagName")
        if tag in ("TEXTAREA", "INPUT"):
            editor.fill(body)
            return
        editor.click()
        lines = body.split("\n")
        for idx, line in enumerate(lines):
            if line:
                self.page.keyboard.insert_text(line)
            if idx < len(lines) - 1:
                self.page.keyboard.press("Enter")
            if idx % 6 == 5:
                self._pump(60)          # 길게 쓰는 모습이 보이게 조금씩 쉰다

    def _photo_count(self, button) -> int:
        """소식에 붙은 사진 수. 사진 버튼의 「3/10」을 읽고, 없으면 사진 칸의 그림 수를 센다."""
        with contextlib.suppress(Exception):
            match = re.search(r"(\d+)\s*/\s*\d+", button.inner_text())
            if match:
                return int(match.group(1))
        return self.page.locator(f"{SORTABLE} img").count()

    def _attach_images(self, paths: list[Path]) -> None:
        with _upload_files(paths) as files:
            self._attach_upload_files(files)

    def _attach_upload_files(self, files: list[str]) -> None:
        """사진 버튼 → 「사진 추가」 창의 「내 앨범에서 선택」 → 파일 고르기. 다 올라갈 때까지 기다린다."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        button = self._find("사진 추가 버튼",
                            lambda p: p.locator("button").filter(has_text=PHOTO_COUNTER),
                            lambda p: p.get_by_role("button", name=re.compile("사진 ?(첨부|추가)")),
                            dismiss=PRE_DIALOGS)
        before = self._photo_count(button)
        want = before + len(files)
        self._log(f"상품 사진 {len(files)}장을 먼저 올립니다.")
        for attempt in (1, 2):
            self._ready(button, "사진 추가 버튼", dismiss=PRE_DIALOGS)   # 「작성 중인 소식」 물음 창이 가리고 있으면 먼저 닫는다
            try:
                with self.page.expect_file_chooser(timeout=10000) as chooser:
                    button.click()
                    album = self._find("내 앨범에서 선택", lambda p: p.get_by_text("내 앨범에서 선택", exact=True),
                                       required=False, timeout=3)
                    if album is not None:
                        album.click()
                chooser.value.set_files(files)
            except PlaywrightTimeout:
                saved = self._diagnose("사진첨부")
                raise SiteError("ELEMENT_NOT_FOUND", f"사진을 고르는 창이 열리지 않았습니다.{saved}") from None
            if self._wait_until(lambda: self._photo_count(button) >= want, timeout=15):
                break
            if attempt == 1 and self._photo_count(button) == before:     # 하나도 붙지 않았으면 다시 골라도 겹치지 않는다
                self._log("고른 사진이 붙지 않아 한 번 더 골라 봅니다.", "WARN")
                continue
            saved = self._diagnose("사진첨부")
            raise SiteError("UPLOAD_FAILED", f"사진을 골랐지만 소식에 붙지 않았습니다.{saved}")

        # 올리는 동안에는 사진 버튼이 잠기고, 아직 서버에 올라가지 않은 사진은 내 PC 의 그림(data:)으로 보인다
        pending = self.page.locator(f'{SORTABLE} img[src^="data:"], {SORTABLE} img[src^="blob:"]')
        deadline = time.monotonic() + 120
        while True:
            count = self._photo_count(button)
            if count < want:                 # 붙었던 사진이 빠졌다: 그 사진은 올라가지 못한 것
                saved = self._diagnose("사진업로드")
                raise SiteError("UPLOAD_FAILED", f"사진 {want - count}장이 올라가지 못했습니다.{saved}")
            with contextlib.suppress(Exception):
                if not button.is_disabled() and pending.count() == 0:
                    break
            if time.monotonic() > deadline:
                saved = self._diagnose("사진업로드")
                raise SiteError("UPLOAD_TIMEOUT", f"사진 올리기가 끝나지 않았습니다.{saved}")
            self._pump(300)
        self._log(f"사진 {len(files)}장을 올렸습니다.", shot=True)

    def write_news(self, biz_id: str, title: str, body: str, image_paths: list[Path], *,
                   before_submit: Callable[[], None]) -> dict:
        """소식 작성: 상품 사진을 모두 올린 뒤 제목·본문을 쓰고, 다음 → 완료."""
        self._log("소식 작성 화면을 엽니다.")
        self._goto(f"{self.urls.base}/biz_accounts/{biz_id}/manager/posts/new")
        self._ensure_authed()
        title_input = self._find("소식 제목 입력란", lambda p: p.get_by_placeholder("소식 제목"), dismiss=PRE_DIALOGS)
        if self._visible(self.page.get_by_text(QUOTA_TEXT)):
            raise SiteError("QUOTA", "오늘 올릴 수 있는 소식을 모두 썼습니다. 내일 다시 시도해 주세요.")
        self._pump(600)                                  # 들어가자마자 뜨는 물음 창을 기다렸다가 닫는다
        self._ready(title_input, "소식 제목 입력란", dismiss=PRE_DIALOGS)
        images = list(image_paths)[:NEWS_IMAGE_MAX]
        if images:
            self._attach_images(images)
        posted_title = fit_text(title, NEWS_TITLE_MAX)
        if posted_title != " ".join(title.split()):
            self._log(f"소식 제목 칸은 {NEWS_TITLE_MAX}자까지라 「{posted_title}」(으)로 줄입니다.")
        title_input.click()
        title_input.press_sequentially(posted_title, delay=20)
        editor = self._find("본문 입력란",
                            lambda p: p.locator('[contenteditable]:not([contenteditable="false"])'),
                            lambda p: p.locator("textarea"))
        self._type_body(editor, body)
        self._log(f"제목과 본문을 입력했습니다({len(body)}자).", shot=True)
        finish = lambda p: p.get_by_role("button", name="완료", exact=True)  # noqa: E731
        following = self._find("다음 버튼", lambda p: p.get_by_role("button", name="다음", exact=True),
                               required=False, timeout=2)
        if following is not None:
            self._wait_enabled(following, "다음 버튼")
            self._press(following, "다음")
            self._wait_until(lambda: self._visible(finish(self.page)), timeout=self.step_timeout)
            self._log("다음 단계로 넘어갑니다. 소식 태그·단골 알림·채팅 설정은 기본값 그대로 둡니다.", shot=True)
        done = self._find("완료 버튼", finish)
        self._wait_enabled(done, "완료 버튼")
        before_submit()
        self._press(done, "완료")
        self._log("완료를 눌렀습니다. 결과를 확인합니다.")
        left = self._wait_until(lambda: "/posts/new" not in self.page.url, timeout=max(30, self.step_timeout),
                                dismiss=POST_DIALOGS)
        if not left:
            if self._visible(self.page.get_by_text(QUOTA_TEXT)):
                raise SiteError("QUOTA", "오늘 올릴 수 있는 소식을 모두 썼습니다. 내일 다시 시도해 주세요.", definite=True)
            raise SiteError("RESULT_UNKNOWN", "소식 등록 결과를 확인하지 못했습니다.", unknown=True)
        self._show_posted(posted_title)
        self._log("소식을 등록했습니다. 등록된 소식 화면을 저장했습니다.", "DONE", shot=True)
        return {"url": self.page.url, "title": posted_title, "images": len(images)}

    def _show_posted(self, title: str) -> None:
        """올라간 소식이 화면에 보일 때까지(제목과 사진이 그려질 때까지) 기다린다. 못 봐도 등록은 성공이다."""
        shown = self.page.get_by_text(title[:12], exact=False)
        self._wait_until(lambda: self._visible(shown), timeout=10, dismiss=POST_DIALOGS)
        with contextlib.suppress(Exception):
            self.page.wait_for_load_state("networkidle", timeout=5000)
        self._pump(800)

    # --- 삭제 ---------------------------------------------------------------
    CONFIRM_DELETE = re.compile(r"^(삭제|삭제하기|확인|네)$")

    def _confirm_delete(self, timeout: float = 6) -> bool:
        """삭제를 누른 뒤 「소식을 삭제할까요?」 같은 확인 창이 뜨면 그 창의 삭제 버튼을 누른다. 눌렀으면 True.

        확인 창은 「삭제할까요」 문구로 찾고, 그 문구를 감싼 창 안의 삭제 버튼만 누른다.
        소식의 ⋮ 메뉴(공유하기·소식 수정이 함께 있는 창)는 확인 창이 아니므로 건드리지 않는다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            question = self.page.get_by_text(re.compile("삭제할까요|삭제하시겠"))
            for idx in range(question.count()):
                node = question.nth(idx)
                if not self._visible(node):
                    continue
                box = node.locator("xpath=ancestor::*[.//button[normalize-space()='삭제' or normalize-space()='확인']][1]")
                button = box.get_by_role("button", name=re.compile(r"^(삭제|확인)$"))
                if button.count():
                    self._log("삭제 확인 창에서 삭제를 누릅니다.", shot=True)
                    self._press(button.last, "삭제 확인")
                    self._log("삭제 확인 창에서 삭제를 눌렀습니다.")
                    return True
            dialogs = self.page.locator('[role="dialog"], [role="alertdialog"]')
            for idx in range(dialogs.count()):
                box = dialogs.nth(idx)
                if not self._visible(box) or box.get_by_text("공유하기", exact=True).count():
                    continue
                button = box.get_by_role("button", name=self.CONFIRM_DELETE)
                if self._visible(button):
                    self._press(button.last, "삭제 확인")
                    self._log("삭제 확인 창에서 삭제를 눌렀습니다.")
                    return True
            self._pump(250)
        return False

    def _price_row_gone(self, biz_id: str, item_name: str) -> bool:
        self._goto(f"{self.urls.base}/biz_accounts/{biz_id}/manager/pricing")
        self._ensure_authed()
        self._find("가격 추가 버튼", lambda p: p.get_by_role("button", name=re.compile("가격 추가")))
        with contextlib.suppress(Exception):
            self.page.wait_for_load_state("networkidle", timeout=4000)
        return self._price_edit_button(item_name) is None

    def remove_price(self, biz_id: str, item_name: str, *, before_submit: Callable[[], None]) -> dict:
        """가격표에서 이름이 같은 항목을 지운다(수정 창의 「삭제하기」 → 확인 → 저장).
        돌려주는 값: {"action": REMOVED | ABSENT(이미 없음)}"""
        self._log("가격표 화면을 엽니다.")
        self._goto(f"{self.urls.base}/biz_accounts/{biz_id}/manager/pricing")
        self._ensure_authed()
        self._find("가격 추가 버튼", lambda p: p.get_by_role("button", name=re.compile("가격 추가")))
        with contextlib.suppress(Exception):
            self.page.wait_for_load_state("networkidle", timeout=4000)
        edit = self._price_edit_button(item_name)
        if edit is None:
            self._log(f"가격표에 「{item_name}」 항목이 없어 지울 것이 없습니다.", "DONE", shot=True)
            return {"action": "ABSENT"}
        edit.click()
        self._pump(self.action_delay * 1000)
        title = self._find("「가격 수정」 창", lambda p: p.get_by_role("heading", name="가격 수정", exact=True))
        dialog = title.locator("xpath=ancestor::*[@role='dialog'][1]")
        remove = self._find("삭제하기 버튼", lambda p: dialog.get_by_role("button", name=re.compile("삭제")),
                            lambda p: dialog.get_by_text(re.compile("^삭제하기$")))
        self._log(f"「{item_name}」 항목의 삭제하기를 누릅니다.", shot=True)
        before_submit()
        self._press(remove, "삭제하기")
        self._confirm_delete()
        self._pump(self.action_delay * 1000)
        save = self._find("저장 버튼", lambda p: p.get_by_role("button", name="저장", exact=True), required=False, timeout=3)
        if save is not None and save.is_enabled():           # 삭제는 「저장」을 눌러야 반영된다
            self._press(save, "저장")
            self._wait_until(lambda: self._visible(self.page.get_by_text(re.compile("가격표 정보를 수정했어요|저장했어요|저장되었"))),
                             timeout=self.step_timeout)
        if not self._price_row_gone(biz_id, item_name):
            raise SiteError("RESULT_UNKNOWN", "가격 항목이 지워졌는지 확인하지 못했습니다.", unknown=True)
        self._log(f"가격표에서 「{item_name}」 항목을 지웠습니다.", "DONE", shot=True)
        return {"action": "REMOVED"}

    def _post_title_node(self, title: str):
        """소식 목록에서 제목이 같은(목록이 줄여 보여 줄 때를 위해 앞 12글자가 같은) 첫 소식. 없으면 None."""
        for node in (self.page.get_by_text(title, exact=True), self.page.get_by_text(title[:12], exact=False)):
            for idx in range(node.count()):
                if self._visible(node.nth(idx)):
                    return node.nth(idx)
        return None

    def _post_count(self, title: str) -> int:
        """소식 목록에 보이는, 제목이 같은 소식의 수."""
        for node in (self.page.get_by_text(title, exact=True), self.page.get_by_text(title[:12], exact=False)):
            shown = sum(1 for idx in range(node.count()) if self._visible(node.nth(idx)))
            if shown:
                return shown
        return 0

    def remove_news(self, biz_id: str, title: str, *, before_submit: Callable[[], None]) -> dict:
        """소식 목록에서 제목이 같은 소식을 지운다(⋮ → 삭제 → 확인). 같은 제목이 여럿이면 가장 위(최근) 하나만.
        돌려주는 값: {"action": REMOVED | ABSENT}"""
        posts = f"{self.urls.base}/biz_accounts/{biz_id}/manager/posts"
        self._log("소식 목록 화면을 엽니다.")
        self._goto(posts)
        self._ensure_authed()
        self._find("소식 목록", lambda p: p.get_by_text("소식", exact=True))
        with contextlib.suppress(Exception):
            self.page.wait_for_load_state("networkidle", timeout=4000)
        self._pump(600)
        node = self._post_title_node(title)
        if node is None:
            self._log(f"소식 목록에 「{title}」 소식이 없어 지울 것이 없습니다.", "DONE", shot=True)
            return {"action": "ABSENT"}
        before = self._post_count(title)
        row = node.locator("xpath=ancestor::*[.//button][1]")
        menu_delete = lambda p: p.get_by_role("button", name="삭제", exact=True)  # noqa: E731
        remove = None
        for _attempt in range(2):          # ⋮ 를 눌러도 메뉴가 안 열리면 한 번 더 누른다
            self._press(row.get_by_role("button").last, "소식 더보기(⋮)")
            remove = self._find("소식 삭제 버튼", menu_delete, required=False, timeout=6)
            if remove is not None:
                break
        if remove is None:
            remove = self._find("소식 삭제 버튼", menu_delete, timeout=1)
        self._log(f"「{title}」 소식의 삭제를 누릅니다.", shot=True)
        before_submit()
        self._press(remove, "삭제")
        if not self._confirm_delete():
            saved = self._diagnose("소식삭제확인")
            raise SiteError("ELEMENT_NOT_FOUND", f"「소식을 삭제할까요?」 확인 창을 찾지 못해 삭제하지 않았습니다.{saved}",
                            definite=True)
        self._pump(1500)
        self._goto(posts)
        self._find("소식 목록", lambda p: p.get_by_text("소식", exact=True))
        with contextlib.suppress(Exception):
            self.page.wait_for_load_state("networkidle", timeout=4000)
        self._pump(600)
        if self._post_count(title) >= before:
            raise SiteError("RESULT_UNKNOWN", "소식이 지워졌는지 확인하지 못했습니다.", unknown=True)
        self._log(f"소식 「{title}」을(를) 지웠습니다.", "DONE", shot=True)
        return {"action": "REMOVED"}
