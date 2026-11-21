from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from futuremarketer.config import load_settings
from futuremarketer.db import execute, fetch_one, make_app_db, session_scope
from futuremarketer.db_init import ensure_schema
from futuremarketer.errors import DomainError
from futuremarketer.services import import_service, query_service, register_service
from futuremarketer.storage import FileStore, SecretStore
from tests.conftest import connect, make_image, make_xlsx


def local_settings(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    return load_settings(db_backend="json", data_dir=tmp_path / "data", private_dir=tmp_path / "private",
                         image_dir=images, daangn_target="practice", ai_mode="live")


def test_json_persists_rows_and_job_details_after_reopening(tmp_path):
    settings = local_settings(tmp_path)
    engine = make_app_db(settings)
    assert ensure_schema(engine, settings.target_code) == "created"
    connect(engine)
    make_image(settings.image_dir / "a.png")
    store = FileStore(settings.data_dir)
    secrets = SecretStore(settings.secrets_path)
    import_service.load_products(engine, store, settings, source="UPLOAD", file_name="상품.xlsx",
                                 data=make_xlsx([["A1", "안경", 12000, None, None, "안경 소개입니다.", "a.png"]]))
    row = query_service.product_rows(engine, secrets, settings)["rows"][0]
    assert register_service.enqueue_register(engine, secrets, settings, [row["id"]], run_key="local")["queued"] == ["A1"]

    payload = json.loads(settings.json_path.read_text(encoding="utf-8"))
    assert payload["tables"]["product"][0]["name"] == "안경"
    request = payload["tables"]["task_job"][0]["request_json"]
    assert isinstance(request, dict) and request["product_code"] == "A1"
    assert not list(settings.data_dir.glob("*.sqlite*"))

    reopened = make_app_db(settings)
    assert ensure_schema(reopened, settings.target_code) == "ready"
    job = query_service.progress(reopened)["focus"]
    assert job["status"] == "QUEUED"


def test_json_transactions_do_not_overwrite_other_writers_or_keep_failed_changes(tmp_path):
    settings = local_settings(tmp_path)
    db = make_app_db(settings)
    ensure_schema(db, "PRACTICE")

    def increment(_):
        with session_scope(make_app_db(settings)) as session:
            row = fetch_one(session, "SELECT control_message FROM channel_connection WHERE id=1")
            execute(session, "UPDATE channel_connection SET control_message=:value WHERE id=1",
                    value=str(int(row["control_message"] or "0") + 1))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(16)))
    with pytest.raises(RuntimeError):
        with session_scope(db) as session:
            execute(session, "UPDATE channel_connection SET control_message='lost' WHERE id=1")
            raise RuntimeError("transaction aborted")
    with session_scope(make_app_db(settings)) as session:
        assert fetch_one(session, "SELECT control_message FROM channel_connection WHERE id=1")["control_message"] == "16"


def test_json_invalid_or_newer_file_is_not_overwritten(tmp_path):
    settings = local_settings(tmp_path)
    settings.json_path.parent.mkdir()
    db = make_app_db(settings)
    settings.json_path.write_text("{", encoding="utf-8")
    with pytest.raises(DomainError, match="로컬 JSON을 읽을 수 없습니다"):
        ensure_schema(db)
    assert settings.json_path.read_text(encoding="utf-8") == "{"
    newer = '{"version": 2}'
    settings.json_path.write_text(newer, encoding="utf-8")
    assert ensure_schema(db) == "outdated"
    assert settings.json_path.read_text(encoding="utf-8") == newer
