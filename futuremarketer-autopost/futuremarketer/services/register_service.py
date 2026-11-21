"""상품 등록 접수와, 등록 결과의 기록.

화면의 「선택한 상품 등록」이 부른다. 여기서는 작업을 줄에 세우기만 하고, 실제 일은 워커가 한다.
- 접수할 때 등록 가능 여부를 다시 확인한다(화면을 본 뒤로 상태가 바뀌었을 수 있다).
- 같은 클릭이 두 번 들어와도 작업은 한 번만 생긴다(idempotency_key).
- 실패한 상품을 다시 고르면 새로 시작하지 않고 멈춘 단계부터 이어서 한다.
- 「등록 완료」「실패」는 (상품, 대상, 비즈프로필)별로 따로 기억한다(product_registration).
  연습용 화면에 올린 기록과 실제 당근에 올린 기록이 서로를 덮지 않는다.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from ..config import Settings
from ..content import fit_text
from ..db import execute, fetch_all, fetch_one, session_scope
from ..defaults import STEPS
from ..errors import DomainError
from ..repositories import core
from ..storage import SecretStore
from ..util import dumps, new_id, utcnow
from . import readiness

PRICE_NAME_MAX = 15       # 가격 항목 이름 칸 길이(어댑터의 PRICE_NAME_MAX 와 같다)

PLACE_MESSAGE = "접수했던 계정에 지금 연결되어 있지 않아 남은 단계를 진행하지 않았습니다. 그 계정으로 연결한 뒤 다시 등록해 주세요."


def new_steps() -> dict:
    return {step: {"status": "PENDING"} for step in STEPS}


def job_place(job: dict) -> tuple[str | None, str | None]:
    request = job["request_json"]
    return request.get("target"), request.get("biz_id")


def registration(session, product_id: str, place: tuple[str, str] | None, *, lock: bool = False) -> dict | None:
    if not place:
        return None
    return fetch_one(session, "SELECT * FROM product_registration WHERE product_id = :p AND target = :t AND biz_id = :b"
                     + (" FOR UPDATE" if lock else ""), p=product_id, t=place[0], b=place[1])


def record_outcome(session, job: dict, status: str, now) -> None:
    """작업이 끝난 결과(DONE / FAILED)를 그 작업이 향했던 곳의 기록으로 남긴다.

    워커, 사람의 확인 처리, 취소가 모두 이 함수로 기록한다. 이미 「등록 완료」인 기록을 실패로 덮지 않는다.
    """
    target, biz_id = job_place(job)
    if not target or not biz_id or job["request_json"].get("kind") == "REMOVE":   # 삭제 작업은 등록 기록을 쓰지 않는다
        return
    existing = registration(session, job["product_id"], (target, biz_id), lock=True)
    values = dict(p=job["product_id"], t=target, b=biz_id, st=status, j=job["id"],
                  fp=job["request_json"].get("fingerprint"), reg=now if status == "DONE" else None, now=now)
    if existing is None:
        execute(session, "INSERT INTO product_registration (product_id, target, biz_id, status, job_id, fingerprint,"
                         " registered_at, updated_at) VALUES (:p, :t, :b, :st, :j, :fp, :reg, :now)", **values)
    elif status == "DONE" or existing["status"] != "DONE":
        execute(session, "UPDATE product_registration SET status=:st, job_id=:j, fingerprint=:fp, registered_at=:reg,"
                         " updated_at=:now WHERE product_id=:p AND target=:t AND biz_id=:b", **values)


def finish_product(session, job: dict, status: str, now) -> None:
    """작업 결과를 상품의 「가장 최근 상태」와 그곳의 등록 기록에 함께 반영한다. status: DONE / FAILED"""
    record_outcome(session, job, status, now)
    execute(session, "UPDATE product SET reg_status=:st, updated_at=:now WHERE id=:id",
            st=status, now=now, id=job["product_id"])


def _resume(session, job: dict, prompts: dict, now, *, draft_only: bool, register_price: bool, register_news: bool) -> None:
    """실패한 작업을 다시 줄에 세운다. 끝난 단계의 결과는 그대로 두고, 지침은 지금 것으로 바꾼다.

    가격표 등록을 켠 경우, 건너뛴 가격 단계나 사진을 올리지 않던 예전 방식으로 끝난 가격 단계를 다시 한다.
    가격 단계는 같은 이름의 항목을 찾아 고치므로, 다시 해도 가격표에 두 번 올라가지 않고 사진만 붙는다.
    """
    steps = job["steps_json"]
    for step in STEPS:
        if steps[step]["status"] != "DONE":
            steps[step] = {"status": "PENDING"}
    price = steps["PRICE"]
    if register_price and price["status"] == "DONE" and (price.get("skipped") or ("photo" not in price and "detail" not in price)):
        steps["PRICE"] = {"status": "PENDING"}
    if (register_news or draft_only) and steps["NEWS"].get("skipped"):
        steps["NEWS"] = {"status": "PENDING"}
    request = {**job["request_json"], "draft_only": draft_only, "register_price": register_price, "register_news": register_news,
               "text_prompt": prompts["TEXT"]["body"], "image_prompt": prompts["IMAGE"]["body"],
               "prompt_versions": {k: v["version_no"] for k, v in prompts.items()}}
    execute(session,
            "UPDATE task_job SET status='QUEUED', steps_json=:steps, request_json=:req, current_step=NULL, run_token=NULL,"
            " error_code=NULL, error_message=NULL, finished_at=NULL, attempt_count=0 WHERE id=:id",
            steps=dumps(steps), req=dumps(request), id=job["id"])
    execute(session, "UPDATE product SET reg_status='QUEUED', last_job_id=:j, updated_at=:now WHERE id=:id",
            j=job["id"], now=now, id=job["product_id"])


def enqueue_register(engine, secrets: SecretStore, settings: Settings, product_ids: list[str], *,
                     run_key: str, draft_only: bool = False, register_price: bool = True, register_news: bool = True) -> dict:
    """고른 상품들을 등록 줄에 세운다. 돌려주는 값: {run_id, queued:[코드], skipped:[{code, reason}]}"""
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        raise DomainError("VALIDATION_ERROR", "등록할 상품을 먼저 골라 주세요.")
    if not (draft_only or register_price or register_news):
        raise DomainError("VALIDATION_ERROR", "가격표 등록 또는 소식 등록을 켜 주세요.")
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fm-run:{run_key}"))
    queued, skipped = [], []
    now = utcnow()
    with session_scope(engine) as s:
        env = readiness.environment(s, secrets, settings)
        conn = core.connection(s)
        place = readiness.connected_place(conn)
        prompts = {kind: core.active_prompt(s, kind) for kind in ("TEXT", "IMAGE")}
        products = []
        for product_id in sorted(ids):       # 잠그는 순서를 고정해 서로 기다리다 멈추는 일을 막는다
            row = fetch_one(s, "SELECT * FROM product WHERE id = :id FOR UPDATE", id=product_id)
            if row:
                products.append(row)
        products.sort(key=lambda p: (p["row_no"], p["product_code"]))
        for order, product in enumerate(products):
            created = now + timedelta(microseconds=order)      # 엑셀 순서대로 처리되게
            code = product["product_code"]
            key = f"{run_key}:{product['id']}"
            if fetch_one(s, "SELECT id FROM task_job WHERE idempotency_key = :k", k=key):
                queued.append(code)          # 같은 클릭이 다시 들어온 것: 이미 접수되어 있다
                continue
            if product["reg_status"] in core.ACTIVE_JOB:
                skipped.append({"code": code, "reason": "이미 등록이 진행 중입니다."})
                continue
            record = registration(s, product["id"], readiness.known_place(conn), lock=True)
            status = readiness.place_status(product, conn, record)
            if status == "DONE":
                skipped.append({"code": code, "reason": "이미 등록한 상품입니다."})
                continue
            local_count = len(core.images_for(s, product["id"], "LOCAL"))
            reasons = readiness.blockers(product, local_count, env)
            if reasons:              # 연결돼 있지 않으면 여기서 걸린다. 아래부터는 place 가 있다
                skipped.append({"code": code, "reason": reasons[0]})
                continue
            last = fetch_one(s, "SELECT * FROM task_job WHERE id = :id FOR UPDATE", id=record["job_id"]) \
                if record and record["status"] == "FAILED" else None
            if last and last["status"] == "FAILED" and job_place(last) == place \
                    and last["request_json"].get("kind") != "REMOVE" \
                    and last["request_json"].get("fingerprint") == product["source_fingerprint"]:
                _resume(s, last, prompts, now, draft_only=draft_only, register_price=register_price, register_news=register_news)
                queued.append(code)
                continue
            job_id = new_id()
            request = {"product_code": code, "name": product["name"], "price": product["price_krw"],
                       "info": product["info"], "post_title": product["post_title"], "script": product["script"],
                       "fingerprint": product["source_fingerprint"], "target": place[0], "biz_id": place[1],
                       "draft_only": draft_only, "register_price": register_price, "register_news": register_news,
                       "text_prompt": prompts["TEXT"]["body"], "image_prompt": prompts["IMAGE"]["body"],
                       "prompt_versions": {k: v["version_no"] for k, v in prompts.items()}}
            execute(s,
                    "INSERT INTO task_job (id, run_id, product_id, status, steps_json, request_json, idempotency_key, created_at)"
                    " VALUES (:id, :run, :p, 'QUEUED', :steps, :req, :key, :now)",
                    id=job_id, run=run_id, p=product["id"], steps=dumps(new_steps()), req=dumps(request), key=key,
                    now=created)
            execute(s, "UPDATE product SET reg_status='QUEUED', last_job_id=:j, updated_at=:now WHERE id=:id",
                    j=job_id, now=now, id=product["id"])
            queued.append(code)
    return {"run_id": run_id, "queued": queued, "skipped": skipped}


def resolve_check(engine, job_id: str, *, outcome: str) -> dict:
    """「확인 필요」를 사람이 정리한다.

    outcome=DONE : 당근에 올라간 것을 확인했다 → 그 단계를 끝난 것으로 치고 남은 단계를 이어 간다.
    outcome=RETRY: 올라가지 않았다 → 그 단계부터 다시 한다.
    남은 단계가 있는데 접수했던 계정에 지금 연결돼 있지 않으면, 다른 계정에 올리지 않도록 실패로 끝낸다.
    """
    if outcome not in ("DONE", "RETRY"):
        raise DomainError("VALIDATION_ERROR", "처리 방식이 올바르지 않습니다.")
    now = utcnow()
    with session_scope(engine) as s:
        job = core.job(s, job_id, lock=True)
        if job["status"] != "NEEDS_CHECK":
            raise DomainError("CONFLICT", "이미 정리된 작업입니다. 화면을 새로 고치세요.")
        steps = job["steps_json"]
        for step in STEPS:
            if steps[step]["status"] == "UNKNOWN":
                steps[step] = ({**steps[step], "status": "DONE", "detail": "사람이 등록된 것을 확인함"}
                               if outcome == "DONE" else {"status": "PENDING"})
        remaining = [step for step in STEPS if steps[step]["status"] != "DONE"]
        if not remaining:
            execute(s, "UPDATE task_job SET status='SUCCEEDED', steps_json=:steps, current_step=NULL, error_code=NULL,"
                       " error_message=NULL, finished_at=:now WHERE id=:id", steps=dumps(steps), now=now, id=job_id)
            finish_product(s, job, "DONE", now)
            core.add_event(s, job_id, None, "DONE", "당근에 올라간 것을 사람이 확인해 등록 완료로 정리했습니다.", now,
                           outcome="SUCCEEDED")
            return {"result": "DONE"}
        if readiness.connected_place(core.connection(s)) != job_place(job):
            execute(s, "UPDATE task_job SET status='FAILED', steps_json=:steps, current_step=NULL, run_token=NULL,"
                       " error_code='PLACE_CHANGED', error_message=:m, finished_at=:now WHERE id=:id",
                    steps=dumps(steps), m=PLACE_MESSAGE, now=now, id=job_id)
            finish_product(s, job, "FAILED", now)
            core.add_event(s, job_id, None, "ERROR", PLACE_MESSAGE, now, outcome="FAILED")
            return {"result": "FAILED", "message": PLACE_MESSAGE}
        execute(s, "UPDATE task_job SET status='QUEUED', steps_json=:steps, current_step=NULL, run_token=NULL,"
                   " error_code=NULL, error_message=NULL, finished_at=NULL, attempt_count=0 WHERE id=:id",
                steps=dumps(steps), id=job_id)
        execute(s, "UPDATE product SET reg_status='QUEUED', updated_at=:now WHERE id=:id", now=now, id=job["product_id"])
        return {"result": "QUEUED"}


def reopen(engine, product_id: str) -> None:
    """이곳에 등록한 뒤 엑셀을 고친 상품을, 바뀐 내용으로 다시 올릴 수 있게 되돌린다.

    당근에 이미 올라간 가격·소식은 건드리지 않는다. 다시 등록하면 소식이 하나 더 생긴다.
    """
    with session_scope(engine) as s:
        product = core.product(s, product_id, lock=True)
        place = readiness.connected_place(core.connection(s))
        if not place:
            raise DomainError("NOT_READY", "당근 계정을 연결한 뒤에 할 수 있습니다.")
        if product["reg_status"] in core.ACTIVE_JOB:
            raise DomainError("BUSY", "등록이 진행 중인 상품입니다.")
        record = registration(s, product_id, place, lock=True)
        if not record or record["status"] != "DONE":
            raise DomainError("CONFLICT", "이곳에 등록을 끝낸 상품만 되돌릴 수 있습니다. 화면을 새로 고치세요.")
        execute(s, "DELETE FROM product_registration WHERE product_id = :p AND target = :t AND biz_id = :b",
                p=product_id, t=place[0], b=place[1])
        execute(s, "UPDATE product SET reg_status='NONE', last_job_id=NULL, updated_at=:now WHERE id=:id",
                now=utcnow(), id=product_id)


def cancel_queued(engine) -> int:
    """아직 시작하지 않은 작업을 줄에서 뺀다. 진행 중인 작업은 건드리지 않는다."""
    now = utcnow()
    count = 0
    with session_scope(engine) as s:
        for job in fetch_all(s, "SELECT * FROM task_job WHERE status = 'QUEUED' ORDER BY created_at FOR UPDATE"):
            if job["request_json"].get("kind") == "REMOVE":      # 삭제 대기는 취소하고 상품을 원래 상태로
                execute(s, "UPDATE task_job SET status='CANCELED', finished_at=:now WHERE id=:id", now=now, id=job["id"])
                finish_removal(s, job, "FAILED", now)
                count += 1
                continue
            started = job["started_at"] is not None or any(job["steps_json"][step]["status"] == "DONE" for step in STEPS)
            if started:      # 한 번이라도 돌았던 작업은 실패로 남겨 이어서 할 수 있게 한다
                execute(s, "UPDATE task_job SET status='FAILED', error_code='CANCELED', error_message='대기 중에 취소했습니다.',"
                           " finished_at=:now WHERE id=:id", now=now, id=job["id"])
                finish_product(s, job, "FAILED", now)
                core.add_event(s, job["id"], None, "ERROR", "대기 중에 취소했습니다.", now, outcome="FAILED")
            else:
                execute(s, "UPDATE task_job SET status='CANCELED', finished_at=:now WHERE id=:id", now=now, id=job["id"])
                execute(s, "UPDATE product SET reg_status='NONE', updated_at=:now WHERE id=:id",
                        now=now, id=job["product_id"])
            count += 1
    return count


# --- 당근에 올린 것 지우기 ------------------------------------------------------
def removal_plan(session, product_id: str, place: tuple[str, str] | None) -> dict | None:
    """이곳(대상·비즈프로필)에 올라가 있는 이 상품의 가격 항목과 소식. 지울 것이 없으면 None.

    등록 작업의 기록에서 찾는다: 가격 단계가 끝났다면 그때 쓴 항목 이름, 소식 단계가 끝났다면 그때 쓴 제목.
    이미 삭제를 마친 뒤의 기록은 보지 않는다(두 번 지우지 않게).
    """
    if not place:
        return None
    price = news = None
    for job in fetch_all(session, "SELECT * FROM task_job WHERE product_id = :p AND status <> 'CANCELED'"
                                  " ORDER BY created_at DESC, id DESC", p=product_id):
        if job_place(job) != place:
            continue
        request, steps = job["request_json"], job["steps_json"]
        if request.get("kind") == "REMOVE":
            if job["status"] == "SUCCEEDED":
                break
            continue
        if price is None and steps["PRICE"]["status"] == "DONE" and not steps["PRICE"].get("skipped"):
            price = steps["PRICE"].get("item_name") or fit_text(" ".join(request["name"].split()), PRICE_NAME_MAX)
        if news is None and steps["NEWS"]["status"] == "DONE" and steps["NEWS"].get("title"):
            news = steps["NEWS"]["title"]
    return {"price": price, "news": news} if price or news else None


def request_removal(engine, product_id: str) -> dict:
    """등록한 상품의 당근 가격 항목과 소식을 지우는 작업을 줄에 세운다(실제 일은 워커가 한다)."""
    now = utcnow()
    with session_scope(engine) as s:
        product = core.product(s, product_id, lock=True)
        place = readiness.connected_place(core.connection(s))
        if not place:
            raise DomainError("NOT_READY", "당근 계정을 연결한 뒤에 지울 수 있습니다.")
        if product["reg_status"] in core.ACTIVE_JOB:
            raise DomainError("BUSY", "등록이나 삭제가 진행 중인 상품입니다.")
        plan = removal_plan(s, product_id, place)
        if not plan:
            raise DomainError("CONFLICT", "이곳에 올라간 가격 항목이나 소식이 없습니다. 화면을 새로 고치세요.")
        steps = new_steps()
        for step in ("SCRIPT", "IMAGE"):
            steps[step] = {"status": "DONE", "skipped": True}
        for step, key in (("PRICE", "price"), ("NEWS", "news")):
            if not plan[key]:
                steps[step] = {"status": "DONE", "skipped": True}
        job_id = new_id()
        request = {"kind": "REMOVE", "product_code": product["product_code"], "name": product["name"],
                   "target": place[0], "biz_id": place[1], "price_name": plan["price"], "news_title": plan["news"],
                   "restore": product["reg_status"]}
        execute(s, "INSERT INTO task_job (id, run_id, product_id, status, steps_json, request_json, idempotency_key,"
                   " created_at) VALUES (:id, :run, :p, 'QUEUED', :steps, :req, :key, :now)",
                id=job_id, run=new_id(), p=product_id, steps=dumps(steps), req=dumps(request),
                key=f"remove:{job_id}", now=now)
        execute(s, "UPDATE product SET reg_status='QUEUED', last_job_id=:j, updated_at=:now WHERE id=:id",
                j=job_id, now=now, id=product_id)
    return {"job_id": job_id, "price": plan["price"], "news": plan["news"]}


def finish_removal(session, job: dict, status: str, now) -> None:
    """삭제 작업이 끝난 결과를 상품에 반영한다. 성공이면 이곳의 등록 기록을 지워 다시 등록할 수 있게 하고,
    실패하면 상품을 삭제를 누르기 전 상태로 되돌린다(등록 기록은 그대로)."""
    target, biz_id = job_place(job)
    if status == "SUCCEEDED":
        execute(session, "DELETE FROM product_registration WHERE product_id = :p AND target = :t AND biz_id = :b",
                p=job["product_id"], t=target, b=biz_id)
        execute(session, "UPDATE product SET reg_status='NONE', last_job_id=NULL, updated_at=:now WHERE id=:id",
                now=now, id=job["product_id"])
    else:
        execute(session, "UPDATE product SET reg_status=:st, updated_at=:now WHERE id=:id",
                st=job["request_json"].get("restore") or "DONE", now=now, id=job["product_id"])
