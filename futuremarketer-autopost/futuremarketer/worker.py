"""워커: 줄에 선 등록 작업을 하나씩 처리하고, 브라우저를 여는 일을 도맡는다.

실행:  python -m futuremarketer.worker     (화면을 켜면 자동으로 함께 뜬다)

상품 하나의 등록 = 네 단계
    SCRIPT  대본이 있으면 그대로, 없으면 Claude 가 글을 쓴다
    IMAGE   사진이 있으면 그대로, 없으면 GPT 가 그림을 만들어 앱 데이터 폴더의 generated 에 저장한다
    PRICE   당근 비즈프로필 가격표에 상품 사진·상품명·가격을 올린다(같은 이름이 있으면 그 항목을 고친다)
    NEWS    당근 비즈프로필 소식에 사진을 모두 올린 뒤 제목·본문을 쓴다

지키는 규칙
- 한 번에 워커 하나만 돈다(파일 잠금). 작업마다 표식(run_token)을 받아, 표식이 같을 때만 결과를 쓴다.
- 끝난 단계는 다시 하지 않는다. 실패한 작업을 다시 등록하면 멈춘 단계부터 이어서 한다.
- 「저장」「완료」를 누른 뒤에 결과를 확인하지 못하면 다시 누르지 않는다. 사람이 확인하도록 「확인 필요」로 둔다.
- 등록 시도가 한 번 끝날 때마다 결과 줄(outcome)을 남긴다. 멈췄다면 그 순간의 브라우저 화면도 함께 남긴다.
  대시보드의 작업 기록은 이 줄을 읽는다.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

from PIL import Image

from .adapters.aside import AsideBrowser, BrowserError, remove_profile
from .adapters.daangn import DaangnSite, SiteError, practice_urls, real_urls
from .adapters.image_ai import CodexCliImageGenerator, MockImageGenerator, OpenAIImageGenerator, add_ai_label
from .adapters.text_ai import (MAX_TEXT_IMAGES, AiError, ClaudeApiTextGenerator, ClaudeCliTextGenerator,
                               MockTextGenerator)
from .config import Settings, load_settings
from .content import AMOUNT_RE, SALE_TERMS, TITLE_MAX, format_price, post_from_ai, post_from_script
from .db import execute, fetch_all, fetch_one, make_app_db, session_scope
from .db_init import ensure_schema
from .defaults import STEPS
from .filelock import lock_file
from .repositories import core
from .services import readiness, settings_service
from .services.register_service import finish_product, finish_removal, job_place
from .storage import FileStore, SecretStore
from .util import dumps, iso_utc, new_id, sha256_bytes, utcnow

log = logging.getLogger("worker")
LOGIN_MESSAGE = "당근 로그인이 풀렸습니다. 계정 설정에서 다시 연결해 주세요."
STALE_REQUEST_SEC = 120


class StepFailed(Exception):
    """등록되지 않은 것이 분명한 실패. 고친 뒤 다시 등록하면 이 단계부터 이어서 한다.
    shot: 멈춘 순간의 브라우저 화면(있으면 작업 기록에 붙인다)."""

    def __init__(self, code: str, message: str, shot: bytes | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.shot = shot


class StepUnknown(Exception):
    """제출한 뒤라 등록됐는지 모른다. 자동으로 다시 하지 않는다."""

    def __init__(self, message: str, shot: bytes | None = None):
        super().__init__(message)
        self.message = message
        self.shot = shot


class LostJob(Exception):
    """작업의 표식이 바뀌었다(다른 곳에서 상태를 바꿈). 더 쓰지 않고 물러난다."""


class Worker:
    def __init__(self, engine, settings: Settings, store: FileStore, secrets: SecretStore, *,
                 text_factory=None, image_factory=None, site_factory=None, profile_remover=remove_profile,
                 sleep=time.sleep):
        self.engine = engine
        self.settings = settings
        self.store = store
        self.secrets = secrets
        self._text_factory = text_factory
        self._image_factory = image_factory
        self._site_factory = site_factory or self._default_site
        self._profile_remover = profile_remover
        self.sleep = sleep
        self._site = None
        self._site_used_at = 0.0
        self._sink = lambda message, level, shot: None
        self._practice = None

    # ------------------------------------------------------------------ 루프
    def run_forever(self) -> None:
        self.recover_on_start()
        self._start_practice()
        log.info("워커 시작 (대상=%s, AI=%s)", self.settings.daangn_target, self.settings.ai_mode)
        failures = 0
        stamp, checked = code_stamp(), time.monotonic()
        while True:
            try:
                worked = self.run_once()
                failures = 0
            except Exception as exc:  # 한 번의 실패로 워커가 죽지 않게
                failures += 1
                if failures == 1:
                    log.exception("처리 중 오류")
                elif failures in (2, 5, 10) or failures % 100 == 0:       # 같은 오류로 기록을 채우지 않는다
                    log.error("처리 중 오류가 이어집니다(%d번째): %s", failures, str(exc).splitlines()[0][:200])
                self._close_site()
                # DB 가 꺼진 동안처럼 오류가 이어지면 점점 천천히(최대 1분 간격) 다시 해 본다
                self.sleep(min(60.0, self.settings.poll_interval_sec * 2 ** min(failures, 6)))
                continue
            if not worked:
                if sys.platform != "win32" and time.monotonic() - checked > 3:
                    # Windows의 execv는 PID가 바뀌므로 앱을 재시작해 새 코드와 워커를 함께 적용한다.
                    checked = time.monotonic()
                    if code_stamp() != stamp:
                        raise CodeChanged
                self.sleep(self.settings.poll_interval_sec)

    def run_once(self) -> bool:
        """할 일을 하나 처리했으면 True."""
        if self.process_control():
            return True
        job = self.claim_job()
        if job is None:
            self._close_site_if_idle()
            return self.recover_running() > 0
        self.process_job(job)
        return True

    def _start_practice(self) -> None:
        if self.settings.daangn_target != "practice" or self._practice is not None:
            return
        from .practice.server import PracticeServer

        try:
            self._practice = PracticeServer(self.settings.data_dir, self.settings.practice_port).start()
            log.info("연습용 화면: %s", self._practice.base_url)
        except OSError as exc:
            log.warning("연습용 화면을 띄우지 못했습니다(포트 %s): %s", self.settings.practice_port, exc)

    # ------------------------------------------------------------ 브라우저
    def _default_site(self, on_log):
        urls = (practice_urls(self.settings.practice_base_url) if self.settings.daangn_target == "practice"
                else real_urls())
        browser = AsideBrowser(self.settings.aside_path, self.settings.browser_profile_dir, self.settings.cdp_port,
                               self.settings.window, headless=self.settings.browser_headless)
        return DaangnSite(browser, urls, on_log=on_log, action_delay=self.settings.action_delay_sec,
                          step_timeout=self.settings.step_timeout_sec,
                          diagnostics_dir=self.settings.data_dir / "diagnostics")

    def _close_site(self) -> None:
        if self._site is not None:
            with contextlib.suppress(Exception):
                self._site.close()
            self._site = None

    def _close_site_if_idle(self) -> None:
        if self._site is not None and time.monotonic() - self._site_used_at > self.settings.idle_browser_close_sec:
            self._close_site()

    # ---------------------------------------------------- 연결·해제(제어 요청)
    def process_control(self) -> bool:
        now = utcnow()
        with session_scope(self.engine) as s:
            conn = core.connection(s, lock=True)
            if conn["control_status"] != "REQUESTED":
                return False
            token, action = conn["control_token"], conn["control_action"]
            if conn["control_requested_at"] and now - conn["control_requested_at"] > timedelta(seconds=STALE_REQUEST_SEC):
                # 연결 요청이면 「연결 중」을 되돌리고, 해제 요청이면 지금 상태를 그대로 둔다
                execute(s, "UPDATE channel_connection SET status=:st, control_status='FAILED', control_token=NULL,"
                           " control_message='요청한 지 오래되어 취소했습니다. 다시 눌러 주세요.', control_updated_at=:now,"
                           " updated_at=:now WHERE id=1",
                        st=self._fallback_status(conn) if action == "CONNECT" else conn["status"], now=now)
                return True
            execute(s, "UPDATE channel_connection SET control_status='RUNNING', control_message=:m, control_updated_at=:now"
                       " WHERE id=1", m="브라우저를 준비하고 있습니다.", now=now)
        if action in ("CONNECT", "SELECT"):
            self._run_connect(token, choose=action == "SELECT")
        else:
            self._run_disconnect(token)
        return True

    @staticmethod
    def _fallback_status(conn: dict) -> str:
        """연결 시도가 끝나지 못했을 때 돌아갈 상태."""
        return "EXPIRED" if conn["biz_id"] else "DISCONNECTED"

    def _control_message(self, token: str, message: str) -> None:
        with session_scope(self.engine) as s:
            execute(s, "UPDATE channel_connection SET control_message=:m, control_updated_at=:now"
                       " WHERE id=1 AND control_token=:t", m=message[:500], now=utcnow(), t=token)

    def _control_token(self) -> str | None:
        with session_scope(self.engine) as s:
            return core.connection(s)["control_token"]

    @staticmethod
    def _merge_profiles(conn: dict, result: dict) -> list[dict]:
        """골라 본 비즈프로필 목록에 방금 고른 것을 더하거나 고친다."""
        chosen = {"biz_id": str(result["biz_id"]), "biz_name": result.get("biz_name"), "public_id": result.get("public_id")}
        out = [chosen]
        for item in settings_service.known_profiles(conn):
            if item["biz_id"] != chosen["biz_id"]:
                out.append(item)
        return out[:20]

    def _run_connect(self, token: str, choose: bool = False) -> None:
        self._close_site()
        site = None
        result, failure = None, None
        try:
            site = self._site_factory(lambda message, level, shot: self._control_message(token, message))
            result = site.login(self.settings.login_timeout_sec, lambda: self._control_token() != token, choose=choose)
        except (SiteError, BrowserError) as exc:
            failure = getattr(exc, "message", None) or str(exc)
        except Exception as exc:
            log.exception("연결 중 오류")
            failure = f"브라우저 조작 중 오류가 났습니다({type(exc).__name__})."
        finally:
            if site is not None:       # 로그인이 끝나면 창을 닫는다. 쿠키는 전용 프로필에 남는다.
                with contextlib.suppress(Exception):
                    site.close()
        now = utcnow()
        cleanup = False       # 「연결 안 됨」으로 끝나면 이 컴퓨터에 로그인 정보가 남아 있지 않게 한다
        with session_scope(self.engine) as s:
            conn = core.connection(s, lock=True)
            if conn["control_token"] != token:        # 그 사이 취소됨(로그인이 확인된 직후였더라도)
                cleanup = not conn["biz_id"]
            elif result:
                # 다른 비즈프로필로 바꿔 연결했으면 앞 업체의 프로필 번호를 남기지 않는다(public_id 를 biz_id 보다 먼저 정한다)
                execute(s, "UPDATE channel_connection SET public_id=CASE WHEN biz_id=:b THEN COALESCE(:pub, public_id) ELSE :pub END,"
                           " profiles_json=:profiles, status='CONNECTED', target=:tg, biz_id=:b, biz_name=:n,"
                           " connected_at=:now, verified_at=:now, control_status='DONE', control_message='연결되었습니다.',"
                           " control_updated_at=:now, updated_at=:now WHERE id=1",
                        tg=self.settings.target_code, b=str(result["biz_id"]), n=result.get("biz_name"),
                        pub=result.get("public_id"), profiles=dumps(self._merge_profiles(conn, result)), now=now)
            else:                       # 고르기가 실패해도 지금 연결은 그대로 둔다
                cleanup = not conn["biz_id"] and not choose
                execute(s, "UPDATE channel_connection SET status=:st, control_status='FAILED', control_message=:m,"
                           " control_updated_at=:now, updated_at=:now WHERE id=1",
                        st=conn["status"] if choose else self._fallback_status(conn),
                        m=(failure or ("비즈프로필을 고르지 못했습니다." if choose else "연결하지 못했습니다."))[:500], now=now)
        if cleanup:
            with contextlib.suppress(Exception):
                self._profile_remover(self.settings.browser_profile_dir)

    def _run_disconnect(self, token: str) -> None:
        self._close_site()
        try:
            removed = self._profile_remover(self.settings.browser_profile_dir)
        except Exception:
            log.exception("프로필 삭제 중 오류")
            removed = False
        if removed:      # 계정 화면이 찍힌 캡처와 진단 파일도 함께 지운다
            for folder in ("shots", "diagnostics"):
                shutil.rmtree(self.settings.data_dir / folder, ignore_errors=True)
        now = utcnow()
        with session_scope(self.engine) as s:
            if removed:
                execute(s, "UPDATE channel_connection SET status='DISCONNECTED', biz_id=NULL, biz_name=NULL, public_id=NULL, profiles_json=NULL,"
                           " connected_at=NULL, verified_at=NULL, control_status='DONE',"
                           " control_message='연결을 해제하고 저장된 로그인 정보를 지웠습니다.', control_updated_at=:now,"
                           " updated_at=:now WHERE id=1 AND control_token=:t", now=now, t=token)
            else:
                execute(s, "UPDATE channel_connection SET control_status='FAILED',"
                           " control_message='로그인 정보를 지우지 못했습니다. Aside 창을 닫고 다시 시도해 주세요.',"
                           " control_updated_at=:now, updated_at=:now WHERE id=1 AND control_token=:t", now=now, t=token)

    def _mark_expired(self) -> None:
        with session_scope(self.engine) as s:
            execute(s, "UPDATE channel_connection SET status='EXPIRED', updated_at=:now WHERE id=1 AND status='CONNECTED'",
                    now=utcnow())

    # ---------------------------------------------------------------- 작업 점유
    def claim_job(self) -> dict | None:
        """가장 먼저 줄 선 작업 하나를 맡는다. 맡는 순간 표식을 새로 받는다."""
        now = utcnow()
        with session_scope(self.engine) as s:
            job = fetch_one(s, "SELECT * FROM task_job WHERE status = 'QUEUED' ORDER BY created_at, id LIMIT 1"
                               " FOR UPDATE SKIP LOCKED")
            if not job:
                return None
            token = new_id()
            execute(s, "UPDATE task_job SET status='RUNNING', run_token=:t, started_at=COALESCE(started_at, :now),"
                       " heartbeat_at=:now, finished_at=NULL, attempt_count=attempt_count+1 WHERE id=:id",
                    t=token, now=now, id=job["id"])
            execute(s, "UPDATE product SET reg_status='RUNNING', updated_at=:now WHERE id=:id", now=now, id=job["product_id"])
            job.update(status="RUNNING", run_token=token)
            return job

    # ------------------------------------------------------------- 기록 도우미
    def _event(self, job: dict, step: str | None, level: str, message: str, shot: bytes | None = None,
               shot_key: str | None = None, outcome: str | None = None) -> None:
        if shot:
            shot_key = f"shots/{job['id']}/{int(time.time() * 1000)}.jpg"
            self.store.write(shot_key, shot)
        now = utcnow()
        with session_scope(self.engine) as s:
            core.add_event(s, job["id"], step, level, message, now, outcome=outcome, shot_key=shot_key)
            execute(s, "UPDATE task_job SET heartbeat_at=:now WHERE id=:id AND run_token=:t",
                    now=now, id=job["id"], t=job["run_token"])
        log.info("[%s] %s %s", job["request_json"].get("product_code"), step or "-", message)

    def _save_steps(self, job: dict, step: str | None) -> None:
        with session_scope(self.engine) as s:
            changed = execute(s, "UPDATE task_job SET steps_json=:steps, current_step=:st, heartbeat_at=:now"
                                 " WHERE id=:id AND run_token=:t AND status='RUNNING'",
                              steps=dumps(job["steps_json"]), st=step, now=utcnow(), id=job["id"], t=job["run_token"])
        if not changed:
            raise LostJob(job["id"])

    def _finish(self, job: dict, status: str, code: str | None = None, message: str | None = None) -> None:
        product_status = {"SUCCEEDED": "DONE", "FAILED": "FAILED", "NEEDS_CHECK": "NEEDS_CHECK"}[status]
        steps = job["steps_json"]
        result = {"title": steps["SCRIPT"].get("title"), "image_keys": steps["IMAGE"].get("image_keys"),
                  "news_url": steps["NEWS"].get("url")}
        now = utcnow()
        with session_scope(self.engine) as s:
            changed = execute(s, "UPDATE task_job SET status=:st, steps_json=:steps, result_json=:res, error_code=:c,"
                                 " error_message=:m, finished_at=:now, current_step=:cur WHERE id=:id AND run_token=:t",
                              st=status, steps=dumps(steps), res=dumps(result), c=code, m=(message or "")[:500] or None,
                              now=now, cur=None if status == "SUCCEEDED" else job.get("current_step"),
                              id=job["id"], t=job["run_token"])
            if not changed:
                return
            if product_status == "NEEDS_CHECK":
                execute(s, "UPDATE product SET reg_status='NEEDS_CHECK', updated_at=:now WHERE id=:id",
                        now=now, id=job["product_id"])
            else:           # 등록 완료·실패는 그 작업이 향했던 곳의 기록으로도 남긴다
                finish_product(s, job, product_status, now)

    # ---------------------------------------------------------------- 작업 실행
    def process_job(self, job: dict) -> None:
        if job["request_json"].get("kind") == "REMOVE":
            return self._process_removal(job)
        steps = job["steps_json"]
        pending = [st for st in STEPS if steps[st]["status"] != "DONE"]
        step = pending[0] if pending else None
        submitted = {"flag": False}

        def mark(status: str) -> None:
            """멈춘 단계의 상태를 남긴다. 이미 끝난 단계는 어떤 실패에도 덮어쓰지 않는다(다시 올라가지 않게)."""
            if step and steps[step]["status"] != "DONE":
                steps[step] = {**steps[step], "status": status} if status == "UNKNOWN" else {"status": status}

        try:
            if pending:               # 남은 일이 있을 때만: 접수했던 곳에 지금도 연결돼 있는지 본다
                self._check_place(job)
            for step in STEPS:
                if steps[step]["status"] == "DONE":
                    continue
                job["current_step"] = step
                submitted["flag"] = False
                steps[step] = {"status": "RUNNING"}
                self._save_steps(job, step)
                self._sink = lambda message, level, shot, _step=step: self._event(job, _step, level, message, shot)
                outcome = getattr(self, f"_step_{step.lower()}")(job, submitted)
                steps[step] = {"status": "DONE", "done_at": iso_utc(), **outcome}
                self._save_steps(job, step)
            self._finish(job, "SUCCEEDED")
            self._event(job, None, "DONE", "등록을 마쳤습니다.", outcome="SUCCEEDED")
        except LostJob:
            log.warning("작업 표식이 바뀌어 물러납니다: %s", job["id"])
        except StepUnknown as exc:
            mark("UNKNOWN")
            self._finish(job, "NEEDS_CHECK", "RESULT_UNKNOWN", exc.message)
            self._event(job, step, "ERROR", f"{exc.message} 당근에서 직접 확인한 뒤 처리 방법을 골라 주세요.",
                        shot=exc.shot, outcome="NEEDS_CHECK")
        except StepFailed as exc:
            mark("FAILED")
            self._finish(job, "FAILED", exc.code, exc.message)
            self._event(job, step, "ERROR", exc.message, shot=exc.shot, outcome="FAILED")
            if exc.code == "LOGIN_REQUIRED":
                self._fail_queued(exc.code, exc.message)
        except Exception as exc:  # 예상하지 못한 오류: 제출 뒤였다면 결과를 모른다
            log.exception("작업 실패")
            shot = self._capture(self._site)
            self._close_site()
            if submitted["flag"] and step and steps[step]["status"] != "DONE":
                mark("UNKNOWN")
                message, outcome = "제출한 뒤 오류가 나서 결과를 확인하지 못했습니다.", "NEEDS_CHECK"
                self._finish(job, "NEEDS_CHECK", "RESULT_UNKNOWN", message)
            else:
                mark("FAILED")
                message, outcome = f"예상하지 못한 오류로 멈췄습니다({type(exc).__name__}).", "FAILED"
                self._finish(job, "FAILED", "UNEXPECTED", message)
            with contextlib.suppress(Exception):
                self._event(job, step, "ERROR", message, shot=shot, outcome=outcome)
        finally:
            self._sink = lambda message, level, shot: None
            self._site_used_at = time.monotonic()

    def _process_removal(self, job: dict) -> None:
        """당근에 올라간 가격 항목과 소식을 지운다. 지우는 일은 두 번 해도 같아서(이미 없으면 그대로 끝),
        중간에 멈추면 사람이 다시 삭제를 누르면 된다."""
        steps, req = job["steps_json"], job["request_json"]
        submitted = {"flag": False}
        step = None

        def end(status: str, message: str, outcome: str, code: str | None = None, shot: bytes | None = None) -> None:
            now = utcnow()
            with session_scope(self.engine) as s:
                changed = execute(s, "UPDATE task_job SET status=:st, steps_json=:steps, error_code=:c, error_message=:m,"
                                     " finished_at=:now, run_token=NULL, current_step=NULL WHERE id=:id AND run_token=:t",
                                  st=status, steps=dumps(steps), c=code, m=message[:500] if status != "SUCCEEDED" else None,
                                  now=now, id=job["id"], t=job["run_token"])
                if changed:
                    finish_removal(s, job, status, now)
            if changed:
                self._event(job, step, "DONE" if status == "SUCCEEDED" else "ERROR", message, shot=shot, outcome=outcome)

        try:
            self._check_place(job)
            for step, key, call in (
                    ("NEWS", "news_title", lambda site, title, before: site.remove_news(req["biz_id"], title, before_submit=before)),
                    ("PRICE", "price_name", lambda site, name, before: site.remove_price(req["biz_id"], name, before_submit=before))):
                if steps[step]["status"] == "DONE":
                    continue
                job["current_step"] = step
                submitted["flag"] = False
                steps[step] = {"status": "RUNNING"}
                self._save_steps(job, step)
                self._sink = lambda message, level, shot, _step=step: self._event(job, _step, level, message, shot)
                result = self._site_call(job, step, submitted, lambda site, before, _c=call, _k=key: _c(site, req[_k], before))
                steps[step] = {"status": "DONE", "done_at": iso_utc(), "action": result.get("action")}
                self._save_steps(job, step)
            step = None
            end("SUCCEEDED", "당근에서 가격 항목과 소식을 지웠습니다. 이 상품은 다시 등록할 수 있습니다.", "SUCCEEDED")
        except LostJob:
            log.warning("작업 표식이 바뀌어 물러납니다: %s", job["id"])
        except StepUnknown as exc:
            steps[step] = {**steps[step], "status": "UNKNOWN"}
            end("FAILED", f"{exc.message} 당근에서 확인한 뒤 다시 삭제해 주세요.", "NEEDS_CHECK", "RESULT_UNKNOWN", exc.shot)
        except StepFailed as exc:
            if step:
                steps[step] = {"status": "FAILED"}
            end("FAILED", exc.message, "FAILED", exc.code, exc.shot)
            if exc.code == "LOGIN_REQUIRED":
                self._fail_queued(exc.code, exc.message)
        except Exception as exc:
            log.exception("삭제 실패")
            shot = self._capture(self._site)
            self._close_site()
            if step:
                steps[step] = {"status": "FAILED"}
            with contextlib.suppress(Exception):
                end("FAILED", f"예상하지 못한 오류로 멈췄습니다({type(exc).__name__}). 당근에서 확인한 뒤 다시 삭제해 주세요.",
                    "FAILED", "UNEXPECTED", shot)
        finally:
            self._sink = lambda message, level, shot: None
            self._site_used_at = time.monotonic()

    def _check_place(self, job: dict) -> None:
        """접수했던 곳(대상·비즈프로필)에 지금도 연결돼 있을 때만 진행한다. 다른 계정에 올리지 않게."""
        with session_scope(self.engine) as s:
            conn = core.connection(s)
        if conn["status"] != "CONNECTED":
            raise StepFailed("LOGIN_REQUIRED", LOGIN_MESSAGE)
        if readiness.connected_place(conn) != job_place(job):
            raise StepFailed("PLACE_CHANGED", "접수한 뒤 연결된 곳이 바뀌었습니다. 지금 연결된 곳에 다시 등록해 주세요.")

    def _fail_queued(self, code: str, message: str) -> None:
        """로그인이 풀렸으면 줄에 남은 작업도 같은 이유로 멈춘다(하나씩 브라우저를 열어 실패하지 않게)."""
        now = utcnow()
        with session_scope(self.engine) as s:
            for job in fetch_all(s, "SELECT * FROM task_job WHERE status='QUEUED' FOR UPDATE"):
                execute(s, "UPDATE task_job SET status='FAILED', error_code=:c, error_message=:m, finished_at=:now"
                           " WHERE id=:id", c=code, m=message, now=now, id=job["id"])
                if job["request_json"].get("kind") == "REMOVE":
                    finish_removal(s, job, "FAILED", now)
                else:
                    finish_product(s, job, "FAILED", now)
                core.add_event(s, job["id"], None, "ERROR", f"시작하지 않고 멈췄습니다. {message}", now, outcome="FAILED")

    # ------------------------------------------------------------- AI 고르기
    def _ai_mode(self, provider: str) -> str:
        with session_scope(self.engine) as s:
            return core.ai_connections(s)[provider]["mode"]

    def _text_generator(self):
        if self._text_factory:
            return self._text_factory()
        if self.settings.ai_mode == "mock":
            return MockTextGenerator()
        mode = self._ai_mode("CLAUDE")
        key = self.secrets.get("anthropic_api_key")
        if mode == "API_KEY" and key:
            return ClaudeApiTextGenerator(key, self.settings.claude_api_model)
        if mode == "SUBSCRIPTION":
            return ClaudeCliTextGenerator(self.settings.claude_cli_model)
        raise StepFailed("AI_NOT_CONNECTED", "글을 쓸 Claude 연결이 없습니다. 계정 설정에서 연결해 주세요.")

    def _image_generator(self):
        if self._image_factory:
            return self._image_factory()
        if self.settings.ai_mode == "mock":
            return MockImageGenerator()
        mode = self._ai_mode("GPT")
        key = self.secrets.get("openai_api_key")
        if mode == "API_KEY" and key:
            return OpenAIImageGenerator(key, self.settings.openai_image_model)
        if mode == "SUBSCRIPTION":
            return CodexCliImageGenerator()
        raise StepFailed("AI_NOT_CONNECTED", "그림을 만들 GPT 연결이 없습니다. 계정 설정에서 연결해 주세요.")

    def _with_ai_retry(self, job: dict, step: str, call):
        """잠깐의 오류(혼잡·연결 끊김)는 몇 번 더 해 본다. AI 호출은 다시 해도 당근에 영향이 없다."""
        attempts = self.settings.max_ai_attempts
        for attempt in range(1, attempts + 1):
            try:
                return call()
            except AiError as exc:
                if not exc.transient or attempt == attempts:
                    raise StepFailed(exc.code, exc.message) from exc
                self._event(job, step, "WARN", f"{exc.message} 다시 시도합니다({attempt}/{attempts - 1}).")
                self.sleep(2 * attempt)
        raise StepFailed("AI_TEMPORARY", "AI 호출에 실패했습니다.")  # pragma: no cover

    # ------------------------------------------------------------------ 단계
    def _images(self, product_id: str, source_type: str) -> list[dict]:
        with session_scope(self.engine) as s:
            return core.images_for(s, product_id, source_type)

    def _step_script(self, job: dict, _submitted: dict) -> dict:
        req = job["request_json"]
        if req.get("script"):
            title, body = post_from_script(req["name"], req.get("post_title"), req["script"])
            wanted = " ".join((req.get("post_title") or req["name"]).split())
            if title != wanted:
                self._event(job, "SCRIPT", "INFO", f"소식 제목 칸은 {TITLE_MAX}자까지라 제목을 「{title}」(으)로 줄입니다.")
            self._event(job, "SCRIPT", "DONE", f"대본을 그대로 씁니다({len(body)}자).")
            return {"mode": "SCRIPT", "title": title, "body": body}
        photos = [self.store.read(i["storage_key"]) for i in self._images(job["product_id"], "LOCAL")[:MAX_TEXT_IMAGES]]
        generator = self._text_generator()
        self._event(job, "SCRIPT", "INFO", f"대본이 없어 Claude 가 글을 씁니다(참고 사진 {len(photos)}장).")
        result = self._with_ai_retry(job, "SCRIPT", lambda: generator.generate(
            req["text_prompt"], req["name"], req.get("info"), photos))
        try:
            title, body = post_from_ai(req["name"], req["price"], req.get("info"), req.get("post_title"),
                                       result.title, result.body)
        except ValueError as exc:
            raise StepFailed("AI_OUTPUT", str(exc)) from exc
        self._event(job, "SCRIPT", "DONE", f"글을 썼습니다: {title}")
        return {"mode": "AI", "title": title, "body": body, "model": result.model}

    def _step_image(self, job: dict, _submitted: dict) -> dict:
        req = job["request_json"]
        local = self._images(job["product_id"], "LOCAL")
        if local:
            self._event(job, "IMAGE", "DONE", f"내 사진 {len(local)}장을 씁니다.", shot_key=local[0]["storage_key"])
            return {"mode": "LOCAL", "image_keys": [i["storage_key"] for i in local]}
        made = self._images(job["product_id"], "GENERATED")
        if made:
            self._event(job, "IMAGE", "DONE", "앞서 만든 그림을 다시 씁니다.", shot_key=made[0]["storage_key"])
            return {"mode": "GENERATED", "image_keys": [i["storage_key"] for i in made]}
        generator = self._image_generator()
        self._event(job, "IMAGE", "INFO", "이미지가 없어 GPT 가 그림을 만듭니다.")
        result = self._with_ai_retry(job, "IMAGE", lambda: generator.generate(
            req["image_prompt"], req["name"], req.get("info")))
        try:
            data = add_ai_label(result.data)
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except Exception as exc:
            raise StepFailed("AI_OUTPUT", "만든 그림 파일을 열 수 없습니다.") from exc
        key = f"generated/{req['product_code']}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        self.store.write(key, data)
        with session_scope(self.engine) as s:
            execute(s, "INSERT INTO product_image (id, product_id, source_type, storage_key, original_name, sha256, mime,"
                       " width, height, sort_order, created_at) VALUES (:id, :p, 'GENERATED', :key, NULL, :sha, 'image/png',"
                       " :w, :h, 1, :now)", id=new_id(), p=job["product_id"], key=key, sha=sha256_bytes(data),
                    w=width, h=height, now=utcnow())
        self._event(job, "IMAGE", "DONE", f"그림을 만들어 저장했습니다: {short_path(self.store.path_for(key))}", shot_key=key)
        return {"mode": "GENERATED", "image_keys": [key], "model": result.model}

    def _ensure_site(self, job: dict, step: str):
        """브라우저가 닫혀 있으면 열고, 로그인이 살아 있는지 먼저 본다."""
        if self._site is not None:
            return self._site
        self._event(job, step, "INFO", "Aside 브라우저를 엽니다.")
        site = None
        try:
            site = self._site_factory(lambda message, level, shot: self._sink(message, level, shot))
            logged_in = site.is_logged_in(job["request_json"]["biz_id"])
        except BrowserError as exc:
            raise StepFailed("BROWSER", str(exc)) from exc
        except Exception as exc:
            if site is not None:
                with contextlib.suppress(Exception):
                    site.close()
            raise StepFailed("BROWSER", f"브라우저를 열지 못했습니다({type(exc).__name__}).") from exc
        if not logged_in:
            with contextlib.suppress(Exception):
                site.close()
            self._mark_expired()
            raise StepFailed("LOGIN_REQUIRED", LOGIN_MESSAGE)
        with session_scope(self.engine) as s:        # 손님이 보는 업체 프로필 번호도 알게 되면 함께 남긴다
            execute(s, "UPDATE channel_connection SET verified_at=:now, public_id=COALESCE(:pub, public_id) WHERE id=1",
                    now=utcnow(), pub=getattr(site, "public_id", None))
        self._site = site
        return site

    @staticmethod
    def _capture(site) -> bytes | None:
        """멈춘 순간의 브라우저 화면. 브라우저가 이미 닫혔으면 None."""
        if site is None:
            return None
        with contextlib.suppress(Exception):
            return site.capture()
        return None

    def _site_call(self, job: dict, step: str, submitted: dict, call):
        """화면 조작 한 단계. 제출 직전에 「제출 중」을 먼저 기록해, 그 뒤의 실패를 구분한다.
        멈추면 그 순간의 화면을 함께 넘겨 작업 기록에 남긴다."""
        def before_submit():
            job["steps_json"][step] = {"status": "RUNNING", "submitting": True}
            self._save_steps(job, step)
            submitted["flag"] = True

        site = self._ensure_site(job, step)
        try:
            request = job["request_json"]
            if request.get("kind") != "REMOVE" and (request.get("register_news", True) or request.get("draft_only")):
                status = site.business_verification(request["biz_id"])
                if status not in ("VERIFIED", "UNVERIFIED", "UNKNOWN"):
                    reason = ("당근이 이 비즈프로필의 소식 작성을 제한했습니다." if status == "RESTRICTED"
                              else "선택한 업체의 소식 작성 화면을 확인할 수 없습니다.")
                    raise StepFailed("PREVIEW_ONLY", f"게시 보류 · {reason} 가격 없는 초안을 앱에서 확인하세요.")
                if job["request_json"].get("draft_only"):
                    raise StepFailed("PREVIEW_ONLY", "게시 보류 · 초안만 준비했습니다. 가격 없는 글과 사진을 앱에서 확인하세요.")
                if step == "NEWS" and status != "VERIFIED":
                    script = job["steps_json"]["SCRIPT"]
                    copy = f"{script['title']}\n{script['body']}"
                    if SALE_TERMS.search(copy) or AMOUNT_RE.search(copy):
                        raise StepFailed("PREVIEW_ONLY", "게시 보류 · 금액이나 판매 안내가 있는 소식은 사업자 인증을 확인한 뒤 게시할 수 있습니다.")
            return call(site, before_submit)
        except SiteError as exc:
            shot = self._capture(site)
            if exc.code == "LOGIN_REQUIRED":
                self._close_site()
                self._mark_expired()
                raise StepFailed(exc.code, LOGIN_MESSAGE, shot) from exc
            if exc.unknown or (submitted["flag"] and not exc.definite):
                raise StepUnknown(exc.message, shot) from exc
            raise StepFailed(exc.code, exc.message, shot) from exc
        except (StepFailed, StepUnknown, LostJob):
            raise
        except Exception as exc:  # 창이 닫히거나 연결이 끊긴 경우
            shot = self._capture(site)
            self._close_site()
            if submitted["flag"]:
                raise StepUnknown("제출한 뒤 브라우저 연결이 끊겨 결과를 확인하지 못했습니다.", shot) from exc
            raise StepFailed("BROWSER", f"브라우저 조작이 중단됐습니다({type(exc).__name__}). 창을 닫지 말고 다시 등록해 주세요.",
                             shot) from exc

    def _step_price(self, job: dict, submitted: dict) -> dict:
        """가격표에 대표 사진(첫 번째 사진)과 상품명·가격을 올린다."""
        req = job["request_json"]
        if not req.get("register_price", True):
            self._event(job, "PRICE", "INFO", "가격표 등록을 건너뜁니다.")
            return {"skipped": True}
        keys = job["steps_json"]["IMAGE"].get("image_keys") or []
        image = self.store.path_for(keys[0]) if keys else None
        description = job["steps_json"]["SCRIPT"]["body"]
        result = self._site_call(job, "PRICE", submitted, lambda site, before: site.register_price(
            req["biz_id"], req["name"], int(req["price"]), image, before_submit=before, description=description))
        return {"action": result.get("action"), "item_name": result.get("item_name"), "photo": bool(result.get("photo"))}

    def _step_news(self, job: dict, submitted: dict) -> dict:
        """소식: 상품 사진을 모두 올린 뒤 제목·본문을 쓴다."""
        req = job["request_json"]
        if not req.get("register_news", True) and not req.get("draft_only"):
            self._event(job, "NEWS", "INFO", "소식 등록을 건너뜁니다.")
            return {"skipped": True}
        steps = job["steps_json"]
        script = steps["SCRIPT"]
        if script.get("mode") == "AI":
            # 이전 버전에서 앱이 덧붙인 마지막 판매가격 줄도 재시도 시 공개 소식에서 뺀다.
            suffix = f"\n\n판매가격: {format_price(req['price'])}"
            script["body"] = script["body"].removesuffix(suffix)
        paths = [self.store.path_for(k) for k in steps["IMAGE"].get("image_keys") or []][:self.settings.max_images]
        result = self._site_call(job, "NEWS", submitted, lambda site, before: site.write_news(
            req["biz_id"], steps["SCRIPT"]["title"], steps["SCRIPT"]["body"], paths, before_submit=before))
        return {"url": result.get("url"), "title": result.get("title"), "images": len(paths)}

    # ------------------------------------------------------------------ 복구
    def recover_running(self) -> int:
        """주인 없는 RUNNING 작업을 정리한다. 워커는 하나뿐이라, 내가 일하고 있지 않을 때의 RUNNING 은 모두 고아다.

        제출 중이던 단계는 다시 하지 않고 「확인 필요」로, 그 밖에는 「실패」로 두어 이어서 할 수 있게 한다.
        """
        now = utcnow()
        count = 0
        with session_scope(self.engine) as s:
            for job in fetch_all(s, "SELECT * FROM task_job WHERE status = 'RUNNING' FOR UPDATE"):
                steps = job["steps_json"]
                running = next((st for st in STEPS if steps[st]["status"] == "RUNNING"), None)
                if running and steps[running].get("submitting"):
                    steps[running] = {**steps[running], "status": "UNKNOWN"}
                    status, code = "NEEDS_CHECK", "RESULT_UNKNOWN"
                    message = "제출하는 중에 프로그램이 멈춰 결과를 확인하지 못했습니다."
                else:
                    if running:
                        steps[running] = {"status": "FAILED"}
                    status, code = "FAILED", "INTERRUPTED"
                    message = "작업이 중간에 멈췄습니다. 다시 등록하면 멈춘 단계부터 이어서 합니다."
                execute(s, "UPDATE task_job SET status=:st, steps_json=:steps, run_token=NULL, error_code=:c,"
                           " error_message=:m, finished_at=:now WHERE id=:id",
                        st=status, steps=dumps(steps), c=code, m=message, now=now, id=job["id"])
                if job["request_json"].get("kind") == "REMOVE":      # 삭제는 확인 대기 없이 실패로 끝내고 다시 누르게 한다
                    message = "삭제 작업이 중간에 멈췄습니다. 당근에서 확인한 뒤 다시 삭제해 주세요."
                    execute(s, "UPDATE task_job SET status='FAILED' WHERE id=:id", id=job["id"])
                    finish_removal(s, job, "FAILED", now)
                elif status == "FAILED":
                    finish_product(s, job, "FAILED", now)
                else:
                    execute(s, "UPDATE product SET reg_status='NEEDS_CHECK', updated_at=:now WHERE id=:id",
                            now=now, id=job["product_id"])
                core.add_event(s, job["id"], running, "ERROR", message, now, outcome=status)
                count += 1
        return count

    def recover_on_start(self) -> None:
        """워커가 꺼지면서 남은 흔적을 정리한다."""
        self.recover_running()
        settings_service.sync_target(self.engine, self.settings.target_code)   # 실제 ↔ 연습용을 바꿨으면 연결부터 다시
        now = utcnow()
        with session_scope(self.engine) as s:
            conn = core.connection(s, lock=True)
            if conn["control_status"] == "RUNNING":
                status = self._fallback_status(conn) if conn["control_action"] == "CONNECT" else conn["status"]
                execute(s, "UPDATE channel_connection SET status=:st, control_status='FAILED', control_token=NULL,"
                           " control_message='브라우저 작업이 중단됐습니다. 다시 시도해 주세요.', control_updated_at=:now,"
                           " updated_at=:now WHERE id=1", st=status, now=now)
                conn["status"] = status
        self._remove_stale_profiles(conn)

    def _remove_stale_profiles(self, conn: dict) -> None:
        """연결 기록이 가리키지 않는 로그인 정보는 남기지 않는다.

        - 다른 대상(실제 ↔ 연습용)의 프로필: 지금 연결 기록과 무관하다.
        - 「연결 안 됨」인데 남아 있는 프로필: 화면이 말하는 상태와 맞춘다.
        """
        current = self.settings.browser_profile_dir
        for path in sorted(self.settings.private_dir.glob("browser-profile-*")):
            if path == current and conn["status"] != "DISCONNECTED":
                continue
            removed = False
            with contextlib.suppress(Exception):
                removed = self._profile_remover(path)
            if not removed:
                log.warning("남은 로그인 정보를 지우지 못했습니다: %s", path.name)


# ---------------------------------------------------------------------- 실행
CODE_DIR = Path(__file__).resolve().parent


class CodeChanged(Exception):
    """앱 코드가 바뀌었다. 하던 일이 없을 때 새 코드로 다시 뜬다."""


def code_stamp() -> tuple[int, float]:
    """앱 코드(futuremarketer 아래 .py)의 파일 수와 마지막 수정 시각. 바뀌면 워커를 새 코드로 다시 띄운다."""
    files = list(CODE_DIR.rglob("*.py"))
    return len(files), max((f.stat().st_mtime for f in files), default=0.0)


def short_path(path: Path) -> str:
    """집 폴더 아래면 ~ 로 줄여 보여 준다."""
    try:
        return f"~/{Path(path).relative_to(Path.home())}"
    except ValueError:
        return str(path)


def acquire_lock(data_dir: Path):
    """워커는 하나만. 잠금을 얻지 못하면 None."""
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / "worker.lock", "a+b")
    try:
        lock_file(handle)
    except OSError:
        handle.close()
        return None
    return handle


def worker_running(data_dir: Path) -> bool:
    """화면이 워커가 떠 있는지 볼 때 쓴다(잠금이 잡혀 있으면 떠 있는 것)."""
    path = data_dir / "worker.lock"
    if not path.exists():
        return False
    with open(path, "a+b") as handle:
        try:
            lock_file(handle)
        except OSError:
            return True
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    settings = load_settings()
    lock = acquire_lock(settings.data_dir)
    if lock is None:
        print("워커가 이미 실행 중입니다.")
        return 0
    engine = make_app_db(settings)
    if ensure_schema(engine, settings.target_code) == "outdated":
        print("저장 형식이 지금 코드와 다릅니다. 기존 기록을 백업한 뒤 프로젝트 버전을 확인하세요.")
        return 1
    worker = Worker(engine, settings, FileStore(settings.data_dir), SecretStore(settings.secrets_path))
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker._close_site()
        print("워커를 끝냅니다.")
    except CodeChanged:
        worker._close_site()
        log.info("앱 코드가 바뀌어 새 코드로 다시 시작합니다.")
        sys.stdout.flush()
        lock.close()              # 잠금을 놓고, 같은 프로세스 자리에서 새 코드로 다시 뜬다
        os.execv(sys.executable, [sys.executable, "-m", "futuremarketer.worker"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
