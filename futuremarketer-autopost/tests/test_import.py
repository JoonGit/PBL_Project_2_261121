"""상품 불러오기와 유효성 검사(등록 가능 여부)."""
from __future__ import annotations

import pytest

from futuremarketer.db import execute, fetch_all, fetch_one, session_scope
from futuremarketer.errors import DomainError
from futuremarketer.parsing import parse_price, read_products
from futuremarketer.services import import_service, query_service
from tests.conftest import connect, make_image, make_xlsx, set_ai


def load(engine, store, settings, rows, header=None):
    return import_service.load_products(engine, store, settings, data=make_xlsx(rows, header),
                                        file_name="상품목록.xlsx", source="UPLOAD")


def rows_by_code(engine, secrets, settings):
    return {r["code"]: r for r in query_service.product_rows(engine, secrets, settings)["rows"]}


def test_price_formats():
    assert parse_price("24,900원") == (24900, None)
    assert parse_price(950000.0) == (950000, None)
    assert parse_price(" 1 200 ") == (1200, None)
    assert parse_price(None)[0] is None
    assert parse_price("무료")[0] is None
    assert parse_price(0)[0] is None
    assert parse_price(12.5)[0] is None


def test_load_matches_images_and_keeps_bytes(engine, store, settings):
    make_image(settings.image_dir / "안경_1.png")
    make_image(settings.image_dir / "안경_2.png", color=(200, 30, 30))
    summary = load(engine, store, settings, [
        ["P001", "안경", 950000, "실착 5회", "[정품] 안경", "대본 그대로 올릴 글입니다.", "안경_1.png, 안경_2.png"],
        ["P002", "장난감", "24,900원", "터그놀이 장난감", None, None, None],
    ])
    assert summary["total"] == 2 and summary["new"] == 2 and summary["blocked"] == 0
    with session_scope(engine) as s:
        product = fetch_one(s, "SELECT * FROM product WHERE product_code='P001'")
        images = fetch_all(s, "SELECT * FROM product_image WHERE product_id=:p ORDER BY sort_order", p=product["id"])
        other = fetch_one(s, "SELECT * FROM product WHERE product_code='P002'")
    assert product["script"] == "대본 그대로 올릴 글입니다." and product["post_title"] == "[정품] 안경"
    assert other["price_krw"] == 24900 and other["script"] is None
    assert [i["original_name"] for i in images] == ["안경_1.png", "안경_2.png"]
    # 사진은 앱 데이터 폴더에 사본으로 남는다(원본 폴더가 바뀌어도 등록에 쓸 수 있게)
    assert store.read(images[0]["storage_key"]) == (settings.image_dir / "안경_1.png").read_bytes()


def test_row_problems_block_registration(engine, store, settings, secrets):
    (settings.image_dir / "가짜.png").write_text("이미지가 아님")
    load(engine, store, settings, [
        ["A1", "가격 없음", None, "정보", None, None, None],
        ["A2", "", 1000, "정보", None, None, None],
        ["A3", "사진 없음", 1000, "정보", None, None, "없는파일.png"],
        ["A4", "손상 사진", 1000, "정보", None, None, "가짜.png"],
        ["A5", "근거 없음", 1000, None, None, None, None],
        ["A6", "경로 금지", 1000, "정보", None, None, "../밖.png"],
        ["A7", "수식", 1000, "=1+1", None, None, None],
        ["DUP", "겹침 1", 1000, "정보", None, None, None],
        ["DUP", "겹침 2", 1000, "정보", None, None, None],
        [None, "코드 없음", 1000, "정보", None, None, None],
    ])
    connect(engine)
    set_ai(engine, secrets)
    rows = query_service.product_rows(engine, secrets, settings)["rows"]
    reasons = {r["name"]: " ".join(r["reasons"]) for r in rows}
    assert all(r["state"] == "BLOCKED" and not r["selectable"] for r in rows)
    assert "가격이 비어" in reasons["가격 없음"]
    assert "상품명이 비어" in reasons["(이름 없음)"]
    assert "파일이 없습니다" in reasons["사진 없음"]
    assert "이미지 파일이 아니" in reasons["손상 사진"]
    assert "상품정보나 이미지" in reasons["근거 없음"]
    assert "파일 이름만" in reasons["경로 금지"]
    assert "수식" in reasons["수식"]
    assert "겹칩니다" in reasons["겹침 1"] and "겹칩니다" in reasons["겹침 2"]
    assert "상품코드가 비어" in reasons["코드 없음"]


def test_readiness_depends_on_connections(engine, store, settings, secrets):
    make_image(settings.image_dir / "a.png")
    load(engine, store, settings, [
        ["S1", "대본+사진", 1000, None, None, "대본입니다. 그대로 올립니다.", "a.png"],
        ["S2", "대본 없음", 1000, "정보", None, None, "a.png"],
        ["S3", "사진 없음", 1000, "정보", None, "대본입니다. 그대로 올립니다.", None],
    ])
    rows = rows_by_code(engine, secrets, settings)
    assert all(r["state"] == "BLOCKED" for r in rows.values())            # 당근 계정 미연결
    assert "당근 계정" in rows["S1"]["reasons"][0]

    connect(engine)
    rows = rows_by_code(engine, secrets, settings)
    assert rows["S1"]["state"] == "READY" and rows["S1"]["selectable"]     # 대본·사진이 다 있으면 AI 없이 가능
    assert "Claude" in rows["S2"]["reasons"][0]
    assert "GPT" in rows["S3"]["reasons"][0]

    set_ai(engine, secrets, claude="API_KEY", gpt="NONE")
    rows = rows_by_code(engine, secrets, settings)
    assert rows["S2"]["state"] == "READY" and rows["S3"]["state"] == "BLOCKED"

    set_ai(engine, secrets)
    assert all(r["state"] == "READY" for r in rows_by_code(engine, secrets, settings).values())


def test_reload_unchanged_changed_and_busy(engine, store, settings):
    base = [["P1", "상품 1", 1000, "정보", None, None, None], ["P2", "상품 2", 2000, "정보", None, None, None]]
    load(engine, store, settings, base)
    with session_scope(engine) as s:
        execute(s, "UPDATE product SET reg_status='DONE' WHERE product_code='P1'")
        execute(s, "UPDATE product SET reg_status='RUNNING' WHERE product_code='P2'")

    again = load(engine, store, settings, base)
    assert again["unchanged"] == 2 and again["new"] == 0
    with session_scope(engine) as s:
        assert fetch_one(s, "SELECT reg_status FROM product WHERE product_code='P1'")["reg_status"] == "DONE"

    changed = load(engine, store, settings, [["P1", "상품 1", 1500, "정보", None, None, None],
                                             ["P2", "상품 2", 2500, "정보", None, None, None]])
    assert changed["updated"] == 1 and changed["kept"] == ["P2"] and changed["note"]
    with session_scope(engine) as s:
        p1 = fetch_one(s, "SELECT * FROM product WHERE product_code='P1'")
        p2 = fetch_one(s, "SELECT * FROM product WHERE product_code='P2'")
        assert fetch_one(s, "SELECT COUNT(*) AS n FROM product")["n"] == 2
    assert p1["price_krw"] == 1500 and p1["reg_status"] == "DONE"     # 이미 올린 상품: 내용은 바꾸되 등록 기록은 지우지 않는다
    assert p2["price_krw"] == 2000 and p2["reg_status"] == "RUNNING"  # 진행 중인 상품은 그대로

    with session_scope(engine) as s:
        execute(s, "UPDATE product SET reg_status='FAILED' WHERE product_code='P2'")
    load(engine, store, settings, [["P1", "상품 1", 1500, "정보", None, None, None],
                                   ["P2", "상품 2", 2500, "정보", None, None, None]])
    with session_scope(engine) as s:
        p2 = fetch_one(s, "SELECT * FROM product WHERE product_code='P2'")
    assert p2["price_krw"] == 2500 and p2["reg_status"] == "NONE"     # 아직 올리지 못한 상품은 처음 상태로


def test_only_latest_file_is_listed(engine, store, settings, secrets):
    load(engine, store, settings, [["OLD", "옛 상품", 1000, "정보", None, None, None]])
    load(engine, store, settings, [["NEW", "새 상품", 1000, "정보", None, None, None]])
    assert [r["code"] for r in query_service.product_rows(engine, secrets, settings)["rows"]] == ["NEW"]


def test_bad_files_are_rejected_without_touching_db(engine, store, settings):
    with pytest.raises(DomainError):
        import_service.load_products(engine, store, settings, data=b"not an excel", file_name="x.xlsx", source="UPLOAD")
    with pytest.raises(DomainError) as err:
        load(engine, store, settings, [["P1", "상품", 1000]], header=["코드", "이름", "값"])
    assert "머리글" in err.value.message
    with pytest.raises(DomainError):
        load(engine, store, settings, [])
    with pytest.raises(DomainError):
        read_products(make_xlsx([[f"P{i}", "상품", 1000, "정보"] for i in range(4)]), settings.image_dir, max_rows=3)
    with session_scope(engine) as s:
        assert fetch_one(s, "SELECT COUNT(*) AS n FROM import_batch")["n"] == 0
        assert fetch_one(s, "SELECT COUNT(*) AS n FROM product")["n"] == 0


def test_default_file_missing_message(engine, store, settings):
    with pytest.raises(DomainError) as err:
        import_service.load_default(engine, store, settings)
    assert err.value.code == "NOT_FOUND"
    settings.default_excel.write_bytes(make_xlsx([["P1", "상품", 1000, "정보", None, None, None]]))
    assert import_service.load_default(engine, store, settings)["total"] == 1
