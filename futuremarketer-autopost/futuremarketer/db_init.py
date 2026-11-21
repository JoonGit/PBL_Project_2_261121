"""스키마 적용과 초기값.

실행:
    python -m futuremarketer.db_init            # 비어 있으면 표를 만들고 초기값을 넣는다
    python -m futuremarketer.db_init --reset    # 이 앱의 표를 모두 지우고 다시 만든다(데이터 삭제)
"""
from __future__ import annotations

import sys

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import PROJECT_ROOT, load_settings
from .db import execute, fetch_one, make_engine, session_scope
from .defaults import DEFAULT_PROMPTS
from .util import new_id, utcnow

SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
SCHEMA_VERSION = "schema:5"      # db/schema.sql 의 channel_connection COMMENT 와 같아야 한다


def _existing(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SHOW TABLES"))}


def apply_schema(engine: Engine) -> None:
    statements = [s.strip() for s in SCHEMA_PATH.read_text(encoding="utf-8").split(";\n")]
    with engine.begin() as conn:
        for stmt in statements:
            body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
            if body:
                conn.execute(text(body))


def drop_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for name in _existing(engine):
            conn.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


def seed(engine: Engine, target: str = "REAL") -> None:
    """연결 행 1개, AI 연결 2개, 기본 지침 2개. 이미 있으면 건드리지 않는다."""
    now = utcnow()
    with session_scope(engine) as s:
        if not fetch_one(s, "SELECT id FROM channel_connection WHERE id = 1"):
            execute(s, "INSERT INTO channel_connection (id, status, target, updated_at)"
                       " VALUES (1, 'DISCONNECTED', :t, :now)", t=target, now=now)
        for provider in ("CLAUDE", "GPT"):
            if not fetch_one(s, "SELECT provider FROM ai_connection WHERE provider = :p", p=provider):
                execute(s, "INSERT INTO ai_connection (provider, mode, status, updated_at)"
                           " VALUES (:p, 'NONE', 'UNKNOWN', :now)", p=provider, now=now)
        for kind, body in DEFAULT_PROMPTS.items():
            if not fetch_one(s, "SELECT id FROM prompt_template WHERE kind = :k LIMIT 1", k=kind):
                execute(s, "INSERT INTO prompt_template (id, kind, version_no, body, is_default, is_active, created_at)"
                           " VALUES (:id, :k, 1, :body, 1, 1, :now)", id=new_id(), k=kind, body=body, now=now)


def schema_version(engine: Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT table_comment FROM information_schema.tables"
                                " WHERE table_schema = DATABASE() AND table_name = 'channel_connection'")).first()
    return row[0] if row else None


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns"
                                                " WHERE table_schema = DATABASE() AND table_name = :t"), {"t": table})}


def _migrate_3_to_4(engine: Engine) -> None:
    """작업 기록(대시보드)용 칸을 더한다. 기존 기록은 지우지 않고, 끝난 줄에 결과를 채운다.

    지금까지 워커는 등록 시도 한 번이 끝날 때 마지막 줄을 이렇게 남겼다:
    완료 = 단계 없는 DONE 줄, 실패·확인 필요 = ERROR 줄.
    """
    if "outcome" not in _columns(engine, "job_event"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE job_event ADD COLUMN outcome ENUM('SUCCEEDED','FAILED','NEEDS_CHECK') NULL"
                              " COMMENT '등록 시도 한 번이 끝난 줄이면 그 결과(대시보드 작업 기록)' AFTER level,"
                              " ADD KEY ix_event_outcome (outcome, id)"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE job_event SET outcome = 'FAILED' WHERE outcome IS NULL AND level = 'ERROR'"))
        conn.execute(text("UPDATE job_event SET outcome = 'SUCCEEDED' WHERE outcome IS NULL AND level = 'DONE' AND step IS NULL"))
        # 지금 「확인 필요」로 멈춰 있는 작업은 마지막 줄이 그 결과다
        conn.execute(text("UPDATE job_event e JOIN (SELECT job_id, MAX(id) AS id FROM job_event"
                          " WHERE outcome IS NOT NULL GROUP BY job_id) last ON last.id = e.id"
                          " JOIN task_job j ON j.id = e.job_id SET e.outcome = 'NEEDS_CHECK' WHERE j.status = 'NEEDS_CHECK'"))
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE channel_connection COMMENT = 'schema:4'"))


def _migrate_4_to_5(engine: Engine) -> None:
    """손님이 보는 업체 프로필 주소에 쓸 번호 칸을 더한다. 다음 연결·등록 때 워커가 채운다."""
    if "public_id" not in _columns(engine, "channel_connection"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE channel_connection ADD COLUMN public_id VARCHAR(40) NULL"
                              " COMMENT '손님이 보는 당근 업체 프로필 번호(동네 업체 번호)' AFTER biz_name"))
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE channel_connection COMMENT = 'schema:5'"))


MIGRATIONS = {"schema:3": _migrate_3_to_4, "schema:4": _migrate_4_to_5}     # 이 판이면 → 이 함수로 다음 판으로 올린다(데이터는 그대로)


def ensure_schema(engine: Engine, target: str = "REAL") -> str:
    """앱이 뜰 때 부른다. 돌려주는 값: created / ready / outdated(표 구조가 지금 코드와 다르고 올릴 방법이 없음).

    한 판 아래의 표는 기록을 지우지 않고 그 자리에서 올린다(MIGRATIONS).
    """
    if not _existing(engine):
        apply_schema(engine)
        seed(engine, target)
        return "created"
    version = schema_version(engine)
    while version in MIGRATIONS:
        MIGRATIONS[version](engine)
        version = schema_version(engine)
    if version != SCHEMA_VERSION:
        return "outdated"
    seed(engine, target)
    return "ready"


def main(argv: list[str]) -> int:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    target = "PRACTICE" if settings.daangn_target == "practice" else "REAL"
    if "--reset" in argv:
        drop_all(engine)
        print("기존 표를 지웠습니다.")
    state = ensure_schema(engine, target)
    if state == "outdated":
        print("표 구조가 지금 코드와 다릅니다. `python -m futuremarketer.db_init --reset` 으로 다시 만드세요.")
        return 1
    print("표를 만들고 초기값을 넣었습니다." if state == "created" else "이미 준비되어 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
