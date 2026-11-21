"""Aside 브라우저를 눈에 보이는 창으로 띄우고 조종한다.

- 이 앱 전용 프로필 폴더로 실행한다. 로그인 쿠키는 그 폴더에만 남는다.
- 평소 쓰는 Aside 와는 다른 프로필이라 서로 영향을 주지 않는다.
- 조종은 크로미움의 원격 디버깅 포트(내 PC 안에서만 열림)에 Playwright 로 붙어서 한다.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import psutil

INTERNAL_PREFIXES = ("chrome-extension://", "chrome://", "devtools://")


class BrowserError(Exception):
    pass


class AsideBrowser:
    def __init__(self, exe: Path, profile_dir: Path, port: int, window: tuple[int, int, int, int],
                 headless: bool = False):
        self.exe = Path(exe)
        self.profile_dir = Path(profile_dir)
        self.port = port
        self.window = window
        self.headless = headless      # True 면 창 없이 돈다(개발·시험용). 기본은 창이 보인다.
        self.page = None
        self._proc: subprocess.Popen | None = None
        self._pw = None
        self._browser = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def cdp_alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.endpoint}/json/version", timeout=1) as resp:
                json.loads(resp.read())
            return True
        except Exception:
            return False

    # --- 열기 -------------------------------------------------------------
    def start(self, url: str):
        """창을 띄우고 url 을 연 Playwright Page 를 돌려준다."""
        if not self.exe.exists():
            raise BrowserError(f"Aside 브라우저를 찾지 못했습니다: {self.exe}")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile_dir, 0o700)
        fresh = not self.cdp_alive()
        if fresh:
            x, y, w, h = self.window
            args = [str(self.exe), f"--user-data-dir={self.profile_dir}", f"--remote-debugging-port={self.port}",
                    "--no-first-run", "--no-default-browser-check", f"--window-size={w},{h}"]
            args += ["--headless=new", url] if self.headless else [f"--window-position={x},{y}", f"--app={url}"]
            self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 30
            while not self.cdp_alive():
                if time.monotonic() > deadline or self._proc.poll() is not None:
                    self.close()
                    raise BrowserError("Aside 브라우저가 뜨지 않았습니다.")
                time.sleep(0.3)
        from playwright.sync_api import sync_playwright  # 브라우저를 쓸 때만 불러온다

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.endpoint)
            context = self._browser.contexts[0]
            self.page = self._adopt_page(context, url, fresh)
        except Exception as exc:
            self.close()
            raise BrowserError(f"Aside 브라우저에 연결하지 못했습니다: {type(exc).__name__}") from exc
        return self.page

    @staticmethod
    def _pump(context, ms: int) -> None:
        """기다리는 동안에도 브라우저가 알려 주는 일(새 탭 등)을 받는다. time.sleep 으로는 받지 못한다."""
        with contextlib.suppress(Exception):
            context.wait_for_event("page", timeout=ms)

    @staticmethod
    def _window_id(context, page) -> int | None:
        try:
            return context.new_cdp_session(page).send("Browser.getWindowForTarget")["windowId"]
        except Exception:
            return None

    def _open_window(self, context, url: str):
        """새 탭이 아니라 새 창으로 연다. Aside 는 시작하면서 자기 창의 탭을 정리하는데, 그때 같이 닫히지 않게."""
        known = set(context.pages)
        self._browser.new_browser_cdp_session().send("Target.createTarget", {"url": url, "newWindow": True})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            page = next((p for p in context.pages if p not in known), None)
            if page is not None:
                with contextlib.suppress(Exception):
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                return page
            self._pump(context, 300)
        raise BrowserError("Aside 에 새 창을 열지 못했습니다.")

    def _adopt_page(self, context, url: str, fresh: bool):
        """우리가 쓸 페이지를 정한다.

        - 창 모드로 새로 띄웠으면 --app 으로 연 페이지(http 주소)가 우리 것이다.
        - 헤드리스이거나 이미 떠 있던 브라우저면 새 탭을 열어 쓴다.
        - 빈 탭은 잡지 않는다. 곧 Aside 안내 화면으로 바뀌는 탭일 수 있다.
        - Aside 가 처음 띄우는 안내 창은 우리 창과 다른 창일 때만 닫는다.
          같은 창의 안내 탭을 닫으면 Aside 가 그 창을 통째로 닫아 우리 탭까지 사라진다.
        """
        def web_page():
            return next((p for p in context.pages if p.url.startswith(("http://", "https://"))), None)

        page = None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            page = web_page()
            settled = any(p.url.startswith(INTERNAL_PREFIXES) for p in context.pages)
            if page is not None or (settled and (self.headless or not fresh)):
                break
            self._pump(context, 300)
        if page is None:
            page = self._open_window(context, url)
        elif not fresh:
            page.goto(url, wait_until="domcontentloaded")
        ours = self._window_id(context, page)
        for other in list(context.pages):
            if other is page or not other.url.startswith(INTERNAL_PREFIXES):
                continue
            theirs = self._window_id(context, other)
            if ours is not None and theirs is not None and ours != theirs:
                with contextlib.suppress(Exception):
                    other.close()
        with contextlib.suppress(Exception):
            page.bring_to_front()
        return page

    @property
    def context(self):
        return self._browser.contexts[0] if self._browser else None

    def is_open(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    # --- 닫기 -------------------------------------------------------------
    def close(self) -> None:
        """창과 브라우저 프로세스를 모두 닫는다. 로그인 쿠키는 프로필 폴더에 남는다."""
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.new_browser_cdp_session().send("Browser.close")
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
        if self._proc is not None:
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._proc.wait(timeout=5)
                if self._proc.poll() is None:
                    self._proc.kill()
        self._browser = self._pw = self._proc = self.page = None


def open_detached(exe: Path, profile_dir: Path, port: int, window: tuple[int, int, int, int], url: str,
                  headless: bool = False) -> str:
    """로그인된 전용 프로필의 Aside 에서 url 을 새 창으로 보여 준다. 열어 둔 채 돌아온다(창을 닫지 않는다).

    Aside 가 이미 떠 있으면(예전에 열어 둔 창이 남은 경우 포함) 그 브라우저에 새 창만 더한다.
    돌려주는 값: OPENED / MISSING(Aside 없음) / FAILED(브라우저가 뜨지 않았거나 붙지 못함).
    워커와 같은 인자(원격 조종 포트 포함)로 띄워서, 이어지는 등록 작업이 이 브라우저를 이어 받아도 문제가 없게 한다.
    """
    browser = AsideBrowser(exe, profile_dir, port, window, headless)
    if not browser.exe.exists():
        return "MISSING"
    if not browser.cdp_alive():
        browser.profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(browser.profile_dir, 0o700)
        x, y, w, h = window
        args = [str(exe), f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
                "--no-first-run", "--no-default-browser-check", f"--window-size={w},{h}"]
        args += ["--headless=new", "about:blank"] if headless else [f"--window-position={x},{y}"]
        detached = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                    else {"start_new_session": True})
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached)
        deadline = time.monotonic() + 30
        while not browser.cdp_alive():
            if time.monotonic() > deadline or proc.poll() is not None:
                return "FAILED"
            time.sleep(0.3)
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser._pw = pw
        browser._browser = pw.chromium.connect_over_cdp(browser.endpoint)
        context = browser._browser.contexts[0]
        page = browser._open_window(context, url)
        with contextlib.suppress(Exception):
            page.bring_to_front()
        return "OPENED"
    except Exception:
        return "FAILED"
    finally:
        with contextlib.suppress(Exception):
            pw.stop()          # 연결만 끊는다. 브라우저와 창은 그대로 둔다


def _profile_processes(profile_dir: Path) -> list[psutil.Process]:
    """명령행에서 이 프로필을 정확히 지정한 프로세스만 찾는다."""
    profile = os.path.normcase(os.path.normpath(profile_dir))
    prefix = "--user-data-dir="
    matches = []
    for process in psutil.process_iter(["cmdline"], ad_value=None):
        if process.pid != os.getpid():
            args = process.info["cmdline"] or []
            if any(arg.startswith(prefix) and os.path.normcase(os.path.normpath(arg[len(prefix):])) == profile
                   for arg in args[1:]):
                matches.append(process)
    return matches


def profile_pids(profile_dir: Path) -> list[int]:
    return [process.pid for process in _profile_processes(profile_dir)]


def _wait_gone(profile_dir: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not profile_pids(profile_dir):
            return True
        time.sleep(0.3)
    return not profile_pids(profile_dir)


def stop_profile_processes(profile_dir: Path) -> bool:
    """이 프로필로 떠 있는 Aside 를 끝낸다. 모두 끝났으면 True.

    먼저 종료를 요청하고, 몇 초 뒤에도 남아 있으면 강제로 끝낸다.
    그사이 새로 생긴 도우미 프로세스도 놓치지 않도록, 보낼 때마다 대상을 다시 찾는다.
    """
    for stop, wait in ((psutil.Process.terminate, 6), (psutil.Process.kill, 4)):
        processes = _profile_processes(profile_dir)
        if not processes:
            return True
        for process in processes:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                stop(process)
        if _wait_gone(profile_dir, wait):
            return True
    return False


def remove_profile(profile_dir: Path) -> bool:
    """저장된 로그인 정보(프로필 폴더)를 지운다. 브라우저가 끝났고 폴더도 없으면 True."""
    profile_dir = Path(profile_dir)
    if not stop_profile_processes(profile_dir):
        return False          # 브라우저가 살아 있으면 지워도 다시 생긴다
    for _ in range(5):
        shutil.rmtree(profile_dir, ignore_errors=True)
        if not profile_dir.exists():
            return True
        time.sleep(0.5)
    return not profile_dir.exists()
