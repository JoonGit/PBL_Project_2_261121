"""상품 등록: 엑셀 불러오기 → 목록에서 고르기 → 한 번에 등록 → 진행 과정 보기."""
from __future__ import annotations

from base64 import b64encode
from io import BytesIO
from pathlib import Path

import streamlit as st
from PIL import Image

from futuremarketer.services import import_service, query_service, register_service
from ui import common

RUN_KEY = "register"


# --- 버튼 동작(콜백) ----------------------------------------------------------
def _load() -> None:
    cfg = common.settings()
    upload = st.session_state.get("excel_upload")
    try:
        if upload is not None:
            out = import_service.load_products(common.engine(), common.store(), cfg, data=upload.getvalue(),
                                               file_name=upload.name, source="UPLOAD")
        else:
            out = import_service.load_default(common.engine(), common.store(), cfg)
    except Exception as exc:
        common.flash(common.error_text(exc), "error")
        return
    for key in [k for k in st.session_state if k.startswith("sel_")]:
        st.session_state[key] = False
    message = f"상품 {out['total']}개를 불러왔습니다."
    if out["blocked"]:
        message += f" 그중 {out['blocked']}개는 엑셀 내용을 고쳐야 등록할 수 있습니다."
    if out.get("note"):
        message += f" {out['note']}"
    common.flash(message)


def _register() -> None:
    ids = [i for i in st.session_state.get("_selectable", []) if st.session_state.get(f"sel_{i}")]
    draft_only = st.session_state.get("draft_only", False)
    register_price = st.session_state.get("register_price", True)
    register_news = st.session_state.get("register_news", False)
    try:
        common.start_worker()       # 워커가 꺼져 있었다면 다시 켠다
        out = register_service.enqueue_register(common.engine(), common.secrets(), common.settings(), ids,
                                                run_key=common.request_key(RUN_KEY), draft_only=draft_only,
                                                register_price=register_price, register_news=register_news)
    except Exception as exc:
        common.flash(common.error_text(exc), "error")
        return
    common.rotate(RUN_KEY)
    for product_id in ids:
        st.session_state[f"sel_{product_id}"] = False
    st.session_state["sel_all"] = False
    message = f"{len(out['queued'])}개 초안을 준비합니다." if draft_only else f"{len(out['queued'])}개 등록을 시작합니다."
    if out["skipped"]:
        message += " 제외: " + ", ".join(f"{s['code']}({s['reason']})" for s in out["skipped"])
    common.flash(message, "success" if out["queued"] else "warning")


def _select_all() -> None:
    for product_id in st.session_state.get("_selectable", []):
        st.session_state[f"sel_{product_id}"] = st.session_state.get("sel_all", False)


def _resolve(job_id: str, outcome: str) -> None:
    try:
        out = register_service.resolve_check(common.engine(), job_id, outcome=outcome)
        if out["result"] == "FAILED":
            common.flash(out["message"], "warning")
        else:
            common.flash("등록된 것으로 처리했습니다." if outcome == "DONE" else "그 단계부터 다시 시도합니다.")
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _reopen(product_id: str) -> None:
    try:
        register_service.reopen(common.engine(), product_id)
        common.flash("바뀐 내용으로 다시 등록할 수 있게 했습니다. 당근에 이미 올라간 소식은 그대로 남습니다.")
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _ask_remove(product_id: str, asking: bool) -> None:
    st.session_state[f"rm_{product_id}"] = asking


def _remove(product_id: str) -> None:
    st.session_state[f"rm_{product_id}"] = False
    try:
        common.start_worker()
        out = register_service.request_removal(common.engine(), product_id)
    except Exception as exc:
        common.flash(common.error_text(exc), "error")
        return
    common.flash("당근에서 지우는 중입니다. 끝나면 이 상품은 다시 등록할 수 있습니다.")


def _cancel() -> None:
    count = register_service.cancel_queued(common.engine())
    common.flash(f"대기 중이던 {count}개를 취소했습니다." if count else "취소할 대기 작업이 없습니다.", "info")


# --- 불러오기 ----------------------------------------------------------------
def _load_block(has_products: bool) -> None:
    cfg = common.settings()
    with st.container(border=True):
        left, right = st.columns([3, 2], gap="large", vertical_alignment="bottom")
        with left:
            common.section("상품 불러오기",
                           f"연결된 엑셀 {common.relative_path(cfg.default_excel)} · 이미지 폴더 {common.relative_path(cfg.image_dir)}")
            st.file_uploader("다른 엑셀 파일로 불러오기(선택)", type=["xlsx"], key="excel_upload")
        with right:
            st.html('<p class="fm-note">파일을 올리지 않으면 연결된 엑셀을 불러옵니다. 불러올 때마다 등록 가능 여부를 다시 검사합니다.</p>')
            st.button("불러오기", type="secondary" if has_products else "primary", on_click=_load, width="stretch",
                      icon=":material/download:")


# --- 목록 --------------------------------------------------------------------
@st.dialog("상품 사진", width="large")
def _image_preview(image_key: str, name: str) -> None:
    with st.container(key="register_image_preview"):
        st.html("""
            <style>
            .st-key-register_image_preview [data-testid="stImage"] img {
                max-height: 72vh;
                object-fit: contain;
            }
            </style>
        """)
        if common.store().exists(image_key):
            st.image(str(common.store().path_for(image_key)), caption=name, width="stretch")
        else:
            st.info("사진 파일을 찾을 수 없습니다.")


def _image_button(thumbnail: str, image_key: str, name: str, *, key: str, width: int) -> None:
    data = Path(thumbnail).read_bytes()
    encoded = b64encode(data).decode("ascii")
    with Image.open(BytesIO(data)) as image:
        image_width, image_height = image.size
    st.html(f"""
        <style>
        .st-key-{key} button {{
            width: min(100%, {width}px);
            min-height: 0;
            aspect-ratio: {image_width} / {image_height};
            padding: 0;
            background: center / contain no-repeat url("data:image/jpeg;base64,{encoded}");
        }}
        .st-key-{key} button p {{
            position: absolute;
            width: 1px;
            height: 1px;
            overflow: hidden;
            clip-path: inset(50%);
        }}
        </style>
    """)
    if st.button("상품 사진 크게 보기", key=key, help=f"{name} 사진 크게 보기", width="stretch"):
        _image_preview(image_key, name)


def _row_text(row: dict) -> str:
    text = "글 대본 그대로" if row["has_script"] else "글 Claude 작성"
    if row["local_images"]:
        image = f"사진 {row['local_images']}장"
    elif row["generated_images"]:
        image = "AI 이미지 1장"
    else:
        image = "이미지 GPT 생성"
    other = " · 이전에 불러온 엑셀의 상품" if row.get("other_file") else ""
    meta = f'<p class="fm-meta">{common.esc(row["code"])} · {text} · {image}{other}</p>'
    job, why = row["job"], ""
    if row["state"] == "FAILED" and job and job["error_message"]:
        why += f'<p class="fm-why bad">{common.esc(job["error_message"])}</p>'
    if row["state"] == "NEEDS_CHECK" and job:
        why += (f'<p class="fm-why">{common.esc(job["error_message"] or "")} '
                '당근에서 직접 확인한 뒤 아래에서 골라 주세요.</p>')
    if row["reasons"]:
        why += f'<p class="fm-why">{common.esc(" · ".join(row["reasons"]))}</p>'
    if row["state"] == "DONE":
        why += f'<p class="fm-why">{common.esc(common.kst(row["registered_at"]))}에 등록</p>'
    if row.get("changed"):
        why += '<p class="fm-why">등록한 뒤 엑셀 내용이 바뀌었습니다. 당근에는 이전 내용이 올라가 있습니다.</p>'
    if job and row["state"] in ("FAILED", "NEEDS_CHECK"):
        why += common.steps_html(job["steps"])          # 어느 단계에서 멈췄는지
    return f'<p class="fm-name">{common.esc(row["name"])}</p>{meta}{why}'


def _row(row: dict) -> None:
    with st.container(border=True):
        sel, img, main, price, state = st.columns([0.5, 1.65, 4.85, 1.8, 2.2], gap="small", vertical_alignment="center")
        with sel:
            st.checkbox("선택", key=f"sel_{row['id']}", disabled=not row["selectable"], label_visibility="collapsed")
        with img:
            path = common.thumb(row["thumb_key"])
            if path:
                _image_button(path, row["thumb_key"], row["name"], key=f"product_photo_{row['id']}", width=72)
            else:
                st.html('<div class="fm-noimg">없음</div>')
        main.html(_row_text(row))
        price.html(f'<p class="fm-price">{common.esc(common.won(row["price"]))}</p>')
        job = row["job"]
        state.html(common.pill(row["state"], row["label"]))
        if job and job.get("preview"):
            preview = job["preview"]
            with st.expander("가격 없는 소식 초안"):
                st.text(preview["title"])
                if preview["body"]:
                    st.text(preview["body"])
                for index, key in enumerate(preview["image_keys"]):
                    path = common.preview(key, width=220)
                    if path:
                        _image_button(path, key, f"{row['name']} · 사진 {index + 1}",
                                      key=f"draft_photo_{row['id']}_{index}", width=220)
        if row["state"] == "NEEDS_CHECK" and job:
            _a, yes, no = st.columns([1.35, 2.2, 2.2])
            yes.button("당근에 올라가 있음", key=f"ok_{job['id']}", on_click=_resolve, args=(job["id"], "DONE"), width="stretch")
            no.button("올라가지 않음 · 다시 시도", key=f"re_{job['id']}", on_click=_resolve, args=(job["id"], "RETRY"),
                      width="stretch")
        plan = row.get("removal")
        if plan:
            _a, box = st.columns([1.35, 4.4])
            with box:
                if st.session_state.get(f"rm_{row['id']}"):
                    what = " · ".join(x for x in (f"가격 항목 「{plan['price']}」" if plan["price"] else "",
                                                  f"소식 「{plan['news']}」" if plan["news"] else "") if x)
                    st.html(f'<p class="fm-why bad">정말 지울까요? 당근에서 {common.esc(what)}이(가) 지워지고 되돌릴 수 없습니다.</p>')
                    yes, no = st.columns(2)
                    yes.button("지우기", key=f"rmyes_{row['id']}", on_click=_remove, args=(row["id"],), type="primary",
                               width="stretch", icon=":material/delete:")
                    no.button("취소", key=f"rmno_{row['id']}", on_click=_ask_remove, args=(row["id"], False), width="stretch")
                else:
                    st.button("당근에서 삭제", key=f"rm_ask_{row['id']}", on_click=_ask_remove, args=(row["id"], True),
                              width="stretch", icon=":material/delete:")
        if row.get("changed"):
            _a, again = st.columns([1.35, 4.4])
            again.button("바뀐 내용으로 다시 등록할 수 있게 하기", key=f"reopen_{row['id']}", on_click=_reopen,
                         args=(row["id"],), width="stretch")


def _list(listing: dict) -> None:
    rows = listing["rows"]
    selectable = [r["id"] for r in rows if r["selectable"]]
    st.session_state["_selectable"] = selectable
    chosen = [i for i in selectable if st.session_state.get(f"sel_{i}")]
    counts = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    summary = " · ".join(f"{label} {n}" for label, n in counts.items())
    head, action = st.columns([3, 2], vertical_alignment="center")
    with head:
        common.section(f"상품 {len(rows)}개", summary)
    with action:
        draft_only = st.checkbox("게시하지 않고 초안만 준비", key="draft_only")
        pricing, news = st.columns(2)
        register_price = pricing.toggle("가격표 등록", value=True, key="register_price", disabled=draft_only,
                                        help="가격표의 추가 설명에 원고의 앞부분을 최대 50자로 넣습니다.")
        register_news = news.toggle("소식 등록", value=False, key="register_news", disabled=draft_only)
        can_submit = draft_only or register_price or register_news
        if draft_only:
            action_name = "초안 준비"
        elif register_price and register_news:
            action_name = "등록"
        else:
            action_name = "가격표 등록" if register_price else "소식 등록"
        button_label = f"선택한 {len(chosen)}개 {action_name}" if chosen else "등록할 상품을 골라 주세요"
        if not can_submit:
            button_label = "가격표 또는 소식 등록을 켜 주세요"
        st.button(button_label, type="primary", disabled=not chosen or not can_submit,
                  on_click=_register, width="stretch", icon=":material/send:")
    st.checkbox("등록 가능한 상품 모두 선택", key="sel_all", on_change=_select_all, disabled=not selectable)
    for row in rows:
        _row(row)


# --- 진행 ---------------------------------------------------------------------
def _progress(prog: dict) -> None:
    with st.container(border=True):
        common.section("등록 진행", "글과 사진을 준비하고 사업자 인증과 소식 작성 가능 여부를 확인합니다. 게시가 보류되면 상품의 초안을 열어 보세요.")
        focus = prog["focus"]
        if not focus:
            st.html('<p class="fm-empty">상품을 고르고 등록을 누르면 자동화 과정이 여기에 보입니다. '
                    '브라우저 조작은 Aside 창에서도 그대로 볼 수 있습니다.</p>')
            return
        jobs = []
        for job in prog["jobs"]:
            state = {"SUCCEEDED": "DONE", "CANCELED": ""}.get(job["status"], job["status"])
            done = "삭제 완료" if job.get("kind") == "REMOVE" else "등록 완료"
            label = {"SUCCEEDED": done, "CANCELED": "취소"}.get(job["status"]) or query_service.STATE_LABEL.get(job["status"], "")
            if job.get("preview"):
                label = "게시 보류"
            jobs.append('<div class="fm-job"><div>'
                        f'<p class="fm-name">{common.esc(job["name"])}</p>{common.steps_html(job["steps"])}</div>'
                        f'<div>{common.pill(state, label)}</div></div>')
        st.html("".join(jobs))
        if prog["shot_key"] and common.store().exists(prog["shot_key"]):
            st.image(str(common.store().path_for(prog["shot_key"])))
            st.html(f'<p class="fm-note" style="text-align:center">{common.esc(focus["name"])} · 방금 화면</p>')
        if prog["events"]:
            with st.container(height=232):
                st.html("".join(
                    f'<p class="fm-log {common.esc(e["level"])}"><time>{common.esc(common.kst(e["created_at"], "%H:%M:%S"))}</time>'
                    f'<span>{common.esc(e["message"])}</span></p>' for e in reversed(prog["events"])))
        if prog.get("queued"):
            st.button("대기 중인 작업 취소", on_click=_cancel, width="stretch")


def _body() -> None:
    cfg = common.settings()
    listing = query_service.product_rows(common.engine(), common.secrets(), cfg)
    prog = query_service.progress(common.engine())
    if prog["active"] != st.session_state.get("_reg_polling", False) or st.session_state.get("flash"):
        # 자동 새로 고침을 켜거나 끌 때, 그리고 이 영역 안의 버튼이 알림을 남겼을 때는 화면 전체를 다시 그린다.
        # 알림은 화면 위 한 곳에서만 보여 준다(이전 알림과 겹치지 않게).
        st.rerun()
    if not listing["rows"]:
        st.html('<p class="fm-empty">아직 불러온 상품이 없습니다. 위에서 불러오기를 눌러 주세요.</p>')
        return
    left, right = st.columns([1.55, 1], gap="large")
    with left:
        _list(listing)
    with right:
        _progress(prog)


def run() -> None:
    common.page_header("상품 등록", "엑셀의 상품을 골라 당근 가격표와 소식에 한 번에 올립니다")
    common.show_flash()
    engine = common.engine()
    listing = query_service.product_rows(engine, common.secrets(), common.settings())
    _load_block(bool(listing["rows"]))
    st.html('<div style="height:8px"></div>')
    active = query_service.progress(engine)["active"]
    st.session_state["_reg_polling"] = active
    st.fragment(run_every="2s" if active else None)(_body)()
