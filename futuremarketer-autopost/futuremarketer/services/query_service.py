"""화면이 읽는 조회. 읽기만 하고 아무것도 바꾸지 않는다."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..adapters.daangn import practice_urls, real_urls
from ..config import Settings
from ..content import draft_without_prices
from ..db import fetch_all, fetch_one, session_scope
from ..defaults import STEPS
from ..repositories import core
from ..storage import SecretStore
from . import readiness, register_service

KST = ZoneInfo("Asia/Seoul")
DAILY_NEWS_LIMIT = 5      # 당근 비즈프로필은 소식을 하루 5개까지 받는다

STATE_LABEL = {"READY": "등록 가능", "BLOCKED": "등록 불가", "QUEUED": "대기 중", "RUNNING": "등록 중",
               "DONE": "등록 완료", "FAILED": "실패", "NEEDS_CHECK": "확인 필요"}


def display_state(status: str, reasons: list[str]) -> str:
    if status == "NONE":
        return "BLOCKED" if reasons else "READY"
    return status


def _job_view(job: dict | None) -> dict | None:
    if not job:
        return None
    preview = None
    if job.get("error_code") == "PREVIEW_ONLY":
        steps = job["steps_json"]
        title, body = draft_without_prices(steps["SCRIPT"]["title"], steps["SCRIPT"]["body"])
        preview = {"title": title, "body": body, "image_keys": steps["IMAGE"].get("image_keys") or []}
    return {"id": job["id"], "run_id": job["run_id"], "status": job["status"], "current_step": job["current_step"],
            "preview": preview,
            "kind": (job["request_json"] or {}).get("kind") or "REGISTER",
            "steps": job["steps_json"], "error_message": job["error_message"], "error_code": job["error_code"],
            "started_at": job["started_at"], "finished_at": job["finished_at"], "product_id": job["product_id"]}


def product_rows(engine, secrets: SecretStore, settings: Settings) -> dict:
    """마지막으로 불러온 엑셀의 상품 목록과, 상품마다의 등록 가능 여부(지금 연결된 곳 기준)."""
    # 마지막 파일의 상품 + (어느 파일 것이든) 진행 중이거나 사람의 확인을 기다리는 상품
    scope = "(p.batch_id = :b OR p.reg_status IN ('QUEUED','RUNNING','NEEDS_CHECK'))"
    with session_scope(engine) as s:
        env = readiness.environment(s, secrets, settings)
        conn = core.connection(s)
        place = readiness.known_place(conn)       # 로그인이 풀려 있어도 그곳의 기록으로 보여 준다
        batch = core.latest_batch(s)
        if not batch:
            return {"batch": None, "rows": [], "env": env}
        products = fetch_all(s, f"SELECT p.* FROM product p WHERE {scope}"
                                " ORDER BY (p.batch_id = :b) DESC, p.row_no, p.product_code", b=batch["id"])
        images = fetch_all(s, "SELECT i.* FROM product_image i JOIN product p ON p.id = i.product_id"
                              f" WHERE {scope} ORDER BY i.source_type DESC, i.sort_order", b=batch["id"])
        jobs = {j["id"]: j for j in fetch_all(
            s, f"SELECT j.* FROM task_job j JOIN product p ON p.id = j.product_id WHERE {scope}", b=batch["id"])}
        records = {r["product_id"]: r for r in fetch_all(
            s, "SELECT * FROM product_registration WHERE target = :t AND biz_id = :z", t=place[0], z=place[1])} if place else {}
        removable = conn["status"] == "CONNECTED"
        plans = {p["id"]: register_service.removal_plan(s, p["id"], place)
                 for p in products if removable and place and p["reg_status"] in ("DONE", "FAILED")}
    rows = []
    for product in products:
        mine = [i for i in images if i["product_id"] == product["id"]]
        local = [i for i in mine if i["source_type"] == "LOCAL"]
        generated = [i for i in mine if i["source_type"] == "GENERATED"]
        reasons = readiness.blockers(product, len(local), env)
        record = records.get(product["id"])
        status = readiness.place_status(product, conn, record)
        state = display_state(status, reasons)
        if status in core.ACTIVE_JOB or not place:
            job = jobs.get(product["last_job_id"])
        else:                    # 이곳의 기록을 낸 작업(다른 곳에 올렸던 작업은 보여 주지 않는다)
            job = jobs.get(record["job_id"]) if record else None
        thumb = (local or generated or [None])[0]
        rows.append({
            "id": product["id"], "code": product["product_code"], "name": product["name"], "price": product["price_krw"],
            "has_script": bool(product["script"]), "local_images": len(local), "generated_images": len(generated),
            "thumb_key": thumb["storage_key"] if thumb else None,
            "state": state, "label": "게시 보류" if job and job.get("error_code") == "PREVIEW_ONLY" else STATE_LABEL[state],
            "reasons": reasons if state in ("BLOCKED", "FAILED") else [],
            "selectable": status in ("NONE", "FAILED") and not reasons,
            "changed": status == "DONE" and readiness.changed_after_registration(product, record),
            "other_file": product["batch_id"] != batch["id"],
            "job": _job_view(job), "removal": plans.get(product["id"]),
            "registered_at": record["registered_at"] if record and status == "DONE" else None,
        })
    return {"batch": batch, "rows": rows, "env": env}


def progress(engine, *, event_limit: int = 40) -> dict:
    """지금 돌고 있는(없으면 가장 최근) 등록 묶음의 진행 상황."""
    with session_scope(engine) as s:
        focus = (fetch_one(s, "SELECT * FROM task_job WHERE status = 'RUNNING' ORDER BY started_at DESC LIMIT 1")
                 or fetch_one(s, "SELECT * FROM task_job WHERE status = 'QUEUED' ORDER BY created_at LIMIT 1")
                 or fetch_one(s, "SELECT * FROM task_job WHERE status <> 'CANCELED'"
                                 " ORDER BY COALESCE(finished_at, started_at, created_at) DESC LIMIT 1"))
        if not focus:
            return {"focus": None, "jobs": [], "events": [], "shot_key": None, "active": False}
        jobs = fetch_all(s, "SELECT j.*, p.product_code, p.name AS product_name FROM task_job j"
                            " JOIN product p ON p.id = j.product_id WHERE j.run_id = :r ORDER BY j.created_at, j.id",
                         r=focus["run_id"])
        events = fetch_all(s, "SELECT * FROM (SELECT * FROM job_event WHERE job_id = :j ORDER BY id DESC LIMIT :n) t"
                              " ORDER BY id", j=focus["id"], n=event_limit)
        shot = fetch_one(s, "SELECT shot_key FROM job_event WHERE job_id = :j AND shot_key IS NOT NULL"
                            " ORDER BY id DESC LIMIT 1", j=focus["id"])
        active = fetch_one(s, "SELECT COUNT(*) AS n FROM task_job WHERE status IN ('QUEUED','RUNNING')")["n"]
    views = []
    for job in jobs:
        view = _job_view(job)
        view.update(code=job["product_code"], name=job["product_name"])
        views.append(view)
    focus_view = next(v for v in views if v["id"] == focus["id"])
    return {"focus": focus_view, "jobs": views, "events": events, "shot_key": shot["shot_key"] if shot else None,
            "active": active > 0, "queued": sum(1 for v in views if v["status"] == "QUEUED")}


def news_today(session, place: tuple[str, str] | None) -> int:
    """오늘(한국 시간) 지금 연결된 곳에 소식 등록까지 끝낸 작업 수(소식 등록이 마지막 단계다)."""
    if not place:
        return 0
    start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
    return fetch_one(session, "SELECT COUNT(*) AS n FROM task_job WHERE status = 'SUCCEEDED' AND finished_at >= :s"
                              " AND JSON_EXTRACT(request_json, '$.kind') IS NULL"
                              " AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(steps_json, '$.NEWS.skipped')), 'false') <> 'true'"
                              " AND JSON_UNQUOTE(JSON_EXTRACT(request_json, '$.target')) = :t"
                              " AND JSON_UNQUOTE(JSON_EXTRACT(request_json, '$.biz_id')) = :z",
                     s=start_utc, t=place[0], z=place[1])["n"]


HISTORY_FILTER = {
    "ALL": "e.outcome IS NOT NULL",
    "PROBLEM": "e.outcome IN ('FAILED', 'NEEDS_CHECK')",
    "SUCCEEDED": "e.outcome = 'SUCCEEDED'",
}
BROWSER_STEPS = ("PRICE", "NEWS")      # 브라우저 화면이 찍히는 단계


def share_url(target: str | None, news_url: str | None, settings: Settings | None) -> str | None:
    """등록한 소식의 「공유하기」 주소(손님이 보는 화면). 만들 수 없으면 관리 화면 주소를 그대로 쓴다."""
    if not news_url:
        return None
    if settings is None:
        return news_url
    return site_urls(target, settings).post_share(news_url) or news_url


def history(engine, *, limit: int = 10, show: str = "ALL", settings: Settings | None = None) -> dict:
    """작업 기록: 등록을 한 번 시도할 때마다 한 줄. 성공·실패·확인 필요가 모두 남고, 최근 것이 위에 온다.

    실패한 상품을 다시 등록해도 앞선 실패 줄은 지워지지 않는다(시도마다 따로 쌓인다).
    줄마다 그 시도의 진행 기록과 브라우저 화면(멈췄다면 멈춘 순간의 화면)을 함께 돌려준다.
    """
    where = HISTORY_FILTER[show]
    with session_scope(engine) as s:
        counts = {r["outcome"]: r["n"] for r in fetch_all(
            s, "SELECT outcome, COUNT(*) AS n FROM job_event WHERE outcome IS NOT NULL GROUP BY outcome")}
        ends = fetch_all(s, "SELECT e.*, j.request_json, j.result_json, p.product_code, p.name AS product_name, p.price_krw"
                            " FROM job_event e JOIN task_job j ON j.id = e.job_id JOIN product p ON p.id = j.product_id"
                            f" WHERE {where} ORDER BY e.id DESC LIMIT :n", n=limit)
        rows = []
        for end in ends:
            before = fetch_one(s, "SELECT MAX(id) AS id FROM job_event WHERE job_id = :j AND id < :id"
                                  " AND outcome IS NOT NULL", j=end["job_id"], id=end["id"])["id"] or 0
            events = fetch_all(s, "SELECT id, step, level, message, shot_key, created_at FROM job_event"
                                  " WHERE job_id = :j AND id > :a AND id <= :b ORDER BY id",
                               j=end["job_id"], a=before, b=end["id"])
            screens = [e for e in events if e["shot_key"] and (e["step"] in BROWSER_STEPS or e["id"] == end["id"])]
            request = end["request_json"] or {}
            result = end["result_json"] or {}
            rows.append({
                "id": end["id"], "job_id": end["job_id"], "outcome": end["outcome"], "step": end["step"],
                "message": end["message"], "finished_at": end["created_at"],
                "started_at": events[0]["created_at"] if events else end["created_at"],
                "code": end["product_code"], "name": end["product_name"], "price": end["price_krw"],
                "target": request.get("target"), "kind": request.get("kind") or "REGISTER",
                "news_url": share_url(request.get("target"), result.get("news_url"), settings)
                if end["outcome"] == "SUCCEEDED" else None,
                "shot_key": screens[-1]["shot_key"] if screens else None,   # 끝난(멈춘) 순간에 가장 가까운 화면
                "events": events, "screens": screens,
            })
    total = sum(counts.values())
    shown = {"ALL": total, "PROBLEM": counts.get("FAILED", 0) + counts.get("NEEDS_CHECK", 0),
             "SUCCEEDED": counts.get("SUCCEEDED", 0)}[show]
    return {"rows": rows, "counts": counts, "total": total, "more": shown > len(rows)}


def site_urls(target: str | None, settings: Settings):
    return practice_urls(settings.practice_base_url) if target == "PRACTICE" else real_urls()


def manage_url(conn: dict, settings: Settings) -> str | None:
    """연결된 비즈프로필의 관리 화면 주소. 번호는 연결할 때 DB 에 저장된 값을 쓴다(코드·설정에 적어 두지 않는다)."""
    if conn["status"] not in ("CONNECTED", "EXPIRED") or not conn["biz_id"]:
        return None
    return f"{site_urls(conn['target'], settings).base}/biz_accounts/{conn['biz_id']}/manager/home"


def profile_url(conn: dict, settings: Settings) -> str | None:
    """손님이 당근에서 보는 업체 프로필 주소. 번호는 워커가 관리자 홈에서 읽어 저장해 둔 값이다."""
    if conn["status"] not in ("CONNECTED", "EXPIRED") or not conn.get("public_id"):
        return None
    return site_urls(conn["target"], settings).profile_page(conn["public_id"])


def dashboard(engine, secrets: SecretStore, settings: Settings) -> dict:
    """대시보드: 연결 상태, 상품 현황 숫자. 등록을 실행하는 기능은 없다(작업 기록은 history)."""
    listing = product_rows(engine, secrets, settings)
    rows = listing["rows"]
    counts = {"total": len(rows)}
    for state in STATE_LABEL:
        counts[state] = sum(1 for r in rows if r["state"] == state)
    with session_scope(engine) as s:
        conn = core.connection(s)
        ai = core.ai_connections(s)
        today = news_today(s, readiness.known_place(conn))
    return {"manage_url": manage_url(conn, settings), "profile_url": profile_url(conn, settings), "connection": conn, "ai": ai, "env": listing["env"], "batch": listing["batch"], "counts": counts,
            "news_today": today, "news_limit": DAILY_NEWS_LIMIT, "steps": STEPS}
