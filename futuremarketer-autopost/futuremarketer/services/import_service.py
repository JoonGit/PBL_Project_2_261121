"""상품 불러오기: 엑셀을 읽어 상품 목록을 만든다.

- 상품코드로 같은 상품을 알아본다. 내용이 그대로면 등록 상태를 그대로 둔다.
- 내용이 바뀐 상품은 새 내용으로 바꾼다. 아직 올리지 않은 상품은 등록 상태를 처음으로 돌린다.
- 이미 올린 상품은 내용이 바뀌어도 「등록 완료」를 지우지 않는다(모르고 다시 올려 당근에 두 번 올라가지 않게).
  화면이 「등록 뒤 내용이 바뀜」을 알려 주고, 다시 올릴지는 사람이 고른다.
- 등록이 진행 중인 상품은 바꾸지 않는다(진행 중인 작업이 보던 값이 달라지지 않게).
"""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..db import execute, session_scope
from ..errors import DomainError
from ..parsing import ParsedRow, read_products
from ..repositories import core
from ..storage import FileStore
from ..util import dumps, new_id, utcnow

SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
BUSY_NOTE = "등록이 진행 중이라 엑셀의 바뀐 내용을 아직 반영하지 않았습니다."


def _insert_local_images(session, store: FileStore, product_id: str, row: ParsedRow, now) -> None:
    for order, ref in enumerate(row.images, start=1):
        key, _digest = store.put_by_hash(f"products/{row.code}", ref.path.read_bytes(), SUFFIX[ref.mime])
        execute(session,
                "INSERT INTO product_image (id, product_id, source_type, storage_key, original_name, sha256, mime,"
                " width, height, sort_order, created_at) VALUES (:id, :p, 'LOCAL', :key, :name, :sha, :mime, :w, :h, :o, :now)",
                id=new_id(), p=product_id, key=key, name=ref.name, sha=ref.sha256, mime=ref.mime,
                w=ref.width, h=ref.height, o=order, now=now)


def _fields(row: ParsedRow) -> dict:
    return dict(name=row.name or "(이름 없음)", price=row.price, info=row.info, title=row.post_title, script=row.script,
                images=", ".join(row.image_names)[:1000] or None, issues=dumps(row.issues), fp=row.fingerprint)


def load_products(engine, store: FileStore, settings: Settings, *, data: bytes, file_name: str, source: str) -> dict:
    """엑셀 바이트를 받아 목록을 갱신한다. source: DEFAULT(기본 연결 파일) | UPLOAD(올린 파일)."""
    if source not in ("DEFAULT", "UPLOAD"):
        raise DomainError("VALIDATION_ERROR", "불러오기 출처가 올바르지 않습니다.")
    rows = read_products(data, settings.image_dir, max_rows=settings.max_rows, max_images=settings.max_images,
                         max_image_bytes=settings.max_image_bytes)
    _key, digest = store.put_by_hash("uploads", data, ".xlsx")
    now = utcnow()
    summary = {"total": len(rows), "blocked": 0, "new": 0, "updated": 0, "unchanged": 0, "kept": []}
    with session_scope(engine) as s:
        batch_id = new_id()
        execute(s, "INSERT INTO import_batch (id, file_name, file_sha256, source, row_count, blocked_count, created_at)"
                   " VALUES (:id, :name, :sha, :src, :n, 0, :now)",
                id=batch_id, name=Path(file_name).name[:255], sha=digest, src=source, n=len(rows), now=now)
        for row in rows:
            existing = core.product_by_code(s, row.code, lock=True)
            if existing is None:
                product_id = new_id()
                execute(s,
                        "INSERT INTO product (id, product_code, batch_id, row_no, name, price_krw, info, post_title, script,"
                        " image_names, data_issues_json, source_fingerprint, reg_status, created_at, updated_at)"
                        " VALUES (:id, :code, :b, :r, :name, :price, :info, :title, :script, :images, :issues, :fp,"
                        " 'NONE', :now, :now)",
                        id=product_id, code=row.code, b=batch_id, r=row.row_no, now=now, **_fields(row))
                _insert_local_images(s, store, product_id, row, now)
                summary["new"] += 1
                blocked = bool(row.issues)
            elif existing["source_fingerprint"] == row.fingerprint:
                execute(s, "UPDATE product SET batch_id=:b, row_no=:r, updated_at=:now WHERE id=:id",
                        b=batch_id, r=row.row_no, now=now, id=existing["id"])
                summary["unchanged"] += 1
                blocked = bool(existing["data_issues_json"])
            elif existing["reg_status"] in core.ACTIVE_JOB:
                execute(s, "UPDATE product SET batch_id=:b, row_no=:r, updated_at=:now WHERE id=:id",
                        b=batch_id, r=row.row_no, now=now, id=existing["id"])
                summary["kept"].append(row.code)
                blocked = bool(existing["data_issues_json"])
            else:
                keep = existing["reg_status"] == "DONE"      # 이미 올린 상품: 등록 기록은 그대로 둔다
                execute(s,
                        "UPDATE product SET batch_id=:b, row_no=:r, name=:name, price_krw=:price, info=:info,"
                        " post_title=:title, script=:script, image_names=:images, data_issues_json=:issues,"
                        " source_fingerprint=:fp, updated_at=:now"
                        + ("" if keep else ", reg_status='NONE', last_job_id=NULL")
                        + " WHERE id=:id", b=batch_id, r=row.row_no, now=now, id=existing["id"], **_fields(row))
                # 실패 기록은 옛 내용의 것이라 지운다(새 내용으로 처음부터). 「등록 완료」 기록은 어느 곳 것이든 남긴다.
                execute(s, "DELETE FROM product_registration WHERE product_id = :p AND status = 'FAILED'",
                        p=existing["id"])
                execute(s, "DELETE FROM product_image WHERE product_id = :p", p=existing["id"])
                _insert_local_images(s, store, existing["id"], row, now)
                summary["updated"] += 1
                blocked = bool(row.issues)
            summary["blocked"] += 1 if blocked else 0
        execute(s, "UPDATE import_batch SET blocked_count=:n WHERE id=:id", n=summary["blocked"], id=batch_id)
    summary["batch_id"] = batch_id
    summary["note"] = BUSY_NOTE if summary["kept"] else None
    return summary


def load_default(engine, store: FileStore, settings: Settings) -> dict:
    path = settings.default_excel
    if not path.is_file():
        raise DomainError("NOT_FOUND", f"기본 엑셀 파일이 없습니다: {path.name}",
                          hint="엑셀 파일을 직접 올리거나 .env 의 FM_DEFAULT_EXCEL 에 파일 위치를 적어 주세요.")
    return load_products(engine, store, settings, data=path.read_bytes(), file_name=path.name, source="DEFAULT")
