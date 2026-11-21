"""MySQL과 로컬 JSON이 사용하는 조회·트랜잭션 경계.

- Engine 만 재사용한다(Streamlit 은 st.cache_resource, 워커는 자기 Engine).
- 서비스 호출마다 Session 을 열어 commit/rollback 후 닫는다.
- Session·Connection 을 화면 상태나 전역 캐시에 두지 않는다.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .json_database import JSON_COLUMNS, JsonDatabase
from .util import loads

Database = Engine | JsonDatabase
DbSession = Session | sqlite3.Connection
LOCK_CLAUSE = re.compile(r"\s+FOR UPDATE(?: SKIP LOCKED)?\s*$", re.IGNORECASE)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800, future=True)

    @event.listens_for(engine, "connect")
    def _utc(dbapi_conn, _record):  # pragma: no cover - 연결 훅
        cur = dbapi_conn.cursor()
        cur.execute("SET time_zone = '+00:00'")
        cur.close()

    return engine


def make_app_db(settings: Settings) -> Database:
    return JsonDatabase(settings.json_path) if settings.db_backend == "json" else make_engine(settings.database_url)


@contextmanager
def session_scope(engine: Database) -> Iterator[DbSession]:
    if isinstance(engine, JsonDatabase):
        with engine.session() as session:
            yield session
        return
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def row_to_dict(row) -> dict | None:
    if row is None:
        return None
    local = isinstance(row, sqlite3.Row)
    data = dict(row if local else row._mapping)
    for key in JSON_COLUMNS & data.keys():
        data[key] = loads(data[key])
    if local:
        for key, value in data.items():
            if key.endswith("_at") and isinstance(value, str):
                data[key] = datetime.fromisoformat(value)
    return data


def _execute(session: DbSession, sql: str, params: dict):
    if isinstance(session, sqlite3.Connection):
        # JSON 세션은 파일 전체에 잠금을 잡는다. MySQL의 행 잠금 절은 그 커서에서만 제외한다.
        values = {k: v.isoformat(sep=" ", timespec="microseconds") if isinstance(v, datetime) else v
                  for k, v in params.items()}
        try:
            return session.execute(LOCK_CLAUSE.sub("", sql), values)
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(sql, params, exc) from exc
    return session.execute(text(sql), params)


def fetch_one(session: DbSession, sql: str, **params) -> dict | None:
    return row_to_dict(_execute(session, sql, params).fetchone())


def fetch_all(session: DbSession, sql: str, **params) -> list[dict]:
    return [row_to_dict(row) for row in _execute(session, sql, params).fetchall()]


def execute(session: DbSession, sql: str, **params) -> int:
    return _execute(session, sql, params).rowcount
