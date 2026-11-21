"""여러 서비스가 같이 쓰는 조회·잠금 쿼리."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db import execute, fetch_all, fetch_one
from ..errors import DomainError

ACTIVE_JOB = ("QUEUED", "RUNNING", "NEEDS_CHECK")


def connection(session: Session, *, lock: bool = False) -> dict:
    row = fetch_one(session, "SELECT * FROM channel_connection WHERE id = 1" + (" FOR UPDATE" if lock else ""))
    if not row:
        raise DomainError("NOT_FOUND", "연결 정보가 없습니다. `python -m futuremarketer.db_init` 을 실행하세요.")
    return row


def ai_connections(session: Session) -> dict[str, dict]:
    return {r["provider"]: r for r in fetch_all(session, "SELECT * FROM ai_connection")}


def active_prompt(session: Session, kind: str) -> dict:
    row = fetch_one(session, "SELECT * FROM prompt_template WHERE kind = :k AND is_active = 1", k=kind)
    if not row:
        raise DomainError("NOT_FOUND", f"{kind} 지침이 없습니다.")
    return row


def latest_batch(session: Session) -> dict | None:
    return fetch_one(session, "SELECT * FROM import_batch ORDER BY created_at DESC, id DESC LIMIT 1")


def product(session: Session, product_id: str, *, lock: bool = False) -> dict:
    row = fetch_one(session, "SELECT * FROM product WHERE id = :id" + (" FOR UPDATE" if lock else ""), id=product_id)
    if not row:
        raise DomainError("NOT_FOUND", "상품을 찾을 수 없습니다. 목록을 새로 고치세요.")
    return row


def product_by_code(session: Session, code: str, *, lock: bool = False) -> dict | None:
    return fetch_one(session, "SELECT * FROM product WHERE product_code = :c" + (" FOR UPDATE" if lock else ""), c=code)


def images_for(session: Session, product_id: str, source_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM product_image WHERE product_id = :p"
    params = {"p": product_id}
    if source_type:
        sql += " AND source_type = :t"
        params["t"] = source_type
    return fetch_all(session, sql + " ORDER BY source_type DESC, sort_order", **params)


def job(session: Session, job_id: str, *, lock: bool = False) -> dict:
    row = fetch_one(session, "SELECT * FROM task_job WHERE id = :id" + (" FOR UPDATE" if lock else ""), id=job_id)
    if not row:
        raise DomainError("NOT_FOUND", "작업을 찾을 수 없습니다.")
    return row


def add_event(session: Session, job_id: str, step: str | None, level: str, message: str, now, *,
              outcome: str | None = None, shot_key: str | None = None) -> None:
    """진행 기록 한 줄. outcome 을 주면 「등록 시도 한 번이 여기서 끝났다」는 줄이 되어 대시보드 작업 기록에 나온다."""
    execute(session, "INSERT INTO job_event (job_id, step, level, outcome, message, shot_key, created_at)"
                     " VALUES (:j, :s, :l, :o, :m, :k, :now)",
            j=job_id, s=step, l=level, o=outcome, m=message[:500], k=shot_key, now=now)
