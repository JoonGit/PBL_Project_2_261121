"""SQLAlchemy Engine/Session 경계.

- Engine 만 재사용한다(Streamlit 은 st.cache_resource, 워커는 자기 Engine).
- 서비스 호출마다 Session 을 열어 commit/rollback 후 닫는다.
- Session·Connection 을 화면 상태나 전역 캐시에 두지 않는다.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .util import loads


def make_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800, future=True)

    @event.listens_for(engine, "connect")
    def _utc(dbapi_conn, _record):  # pragma: no cover - 연결 훅
        cur = dbapi_conn.cursor()
        cur.execute("SET time_zone = '+00:00'")
        cur.close()

    return engine


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
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


JSON_COLUMNS = {"data_issues_json", "steps_json", "request_json", "result_json", "profiles_json"}


def row_to_dict(row) -> dict | None:
    if row is None:
        return None
    data = dict(row._mapping)
    for key in JSON_COLUMNS & data.keys():
        data[key] = loads(data[key])
    return data


def fetch_one(session: Session, sql: str, **params) -> dict | None:
    return row_to_dict(session.execute(text(sql), params).first())


def fetch_all(session: Session, sql: str, **params) -> list[dict]:
    return [row_to_dict(r) for r in session.execute(text(sql), params).all()]


def execute(session: Session, sql: str, **params) -> int:
    return session.execute(text(sql), params).rowcount
