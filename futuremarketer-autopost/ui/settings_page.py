"""계정 설정: 당근 계정 연결·해제, AI 연결, 지침(프롬프트)."""
from __future__ import annotations

import streamlit as st

from futuremarketer.defaults import PROMPT_MAX_CHARS
from futuremarketer.services import settings_service
from futuremarketer.worker import worker_running
from ui import common

MODE_LABEL = {"NONE": "사용 안 함", "API_KEY": "API 키", "SUBSCRIPTION": "구독 로그인"}
PROVIDERS = {
    "CLAUDE": {"title": "글 작성 · Claude", "note": "대본이 없는 상품의 소식 글을 씁니다",
               "cli": "Claude Code", "key_hint": "sk-ant-…"},
    "GPT": {"title": "이미지 생성 · GPT", "note": "이미지가 없는 상품의 그림을 만듭니다",
            "cli": "Codex", "key_hint": "sk-…"},
}
PROMPTS = {
    "TEXT": ("글 작성 지침", "대본이 없는 상품의 글을 Claude 가 쓸 때 따릅니다"),
    "IMAGE": ("이미지 생성 지침", "이미지가 없는 상품의 그림을 GPT 가 만들 때 따릅니다"),
}


def _overview() -> dict:
    return settings_service.overview(common.engine(), common.secrets(), common.settings())


# --- 당근 계정 ----------------------------------------------------------------
def _connect() -> None:
    try:
        common.start_worker()
        settings_service.request_connect(common.engine())
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _cancel_connect() -> None:
    settings_service.cancel_connect(common.engine())


def _ask_disconnect() -> None:
    st.session_state["confirm_disconnect"] = True


def _disconnect() -> None:
    st.session_state["confirm_disconnect"] = False
    try:
        common.start_worker()
        settings_service.request_disconnect(common.engine())
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _keep() -> None:
    st.session_state["confirm_disconnect"] = False


def _select_profile() -> None:
    try:
        common.start_worker()
        settings_service.request_select(common.engine())
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _switch_profile(biz_id: str) -> None:
    try:
        target = settings_service.switch_profile(common.engine(), biz_id)
        common.flash(f"{target['biz_name'] or '비즈프로필'}(ID {target['biz_id']})에 등록하도록 바꿨습니다.")
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _daangn_body() -> None:
    cfg = common.settings()
    conn = _overview()["connection"]
    busy = conn["control_status"] in ("REQUESTED", "RUNNING")
    if busy != st.session_state.get("_conn_polling", False) or st.session_state.get("flash"):
        # 자동 새로 고침을 켜거나 끌 때, 그리고 이 영역 안의 버튼이 알림을 남겼을 때는 화면 전체를 다시 그린다.
        # 알림은 화면 위 한 곳에서만 보여 준다(이전 알림과 겹치지 않게).
        st.rerun()
    status = conn["status"]
    label = {"CONNECTED": ("ok", "연결됨"), "CONNECTING": ("RUNNING", "연결 중"), "EXPIRED": ("warn", "다시 연결 필요"),
             "DISCONNECTED": ("", "연결 안 됨")}[status]
    where = "연습용 화면" if cfg.daangn_target == "practice" else "당근 비즈니스"
    info, action = st.columns([3, 2], gap="large", vertical_alignment="center")
    with info:
        st.html(f'<p class="fm-sec">당근 계정 {common.pill(*label)}</p>')
        if status == "CONNECTED":
            name = conn["biz_name"] or f"비즈프로필 {conn['biz_id']}"
            st.html(f'<p class="fm-note">{common.esc(name)} · {common.esc(common.kst(conn["connected_at"]))}에 연결. '
                    '로그인 정보는 이 컴퓨터의 전용 브라우저 프로필에만 있습니다.</p>')
        elif busy:
            st.html(f'<p class="fm-note">{common.esc(conn["control_message"] or "브라우저를 준비하고 있습니다.")}</p>')
        elif status == "EXPIRED":
            st.html('<p class="fm-note">로그인이 풀렸습니다. 다시 연결하면 이어서 등록할 수 있습니다.</p>')
        else:
            st.html(f'<p class="fm-note">버튼을 누르면 Aside 창에 {where} 로그인 화면이 뜹니다. '
                    '로그인을 마치면 창이 닫히고, 이후 등록은 그 로그인으로 진행합니다.</p>')
        profiles = settings_service.known_profiles(conn) if status == "CONNECTED" else []
        if len(profiles) > 1 and not busy:
            names = {p["biz_id"]: f'{p["biz_name"] or "비즈프로필"} (ID {p["biz_id"]})' for p in profiles}
            pick, go = st.columns([3, 1.4], vertical_alignment="bottom")
            chosen = pick.selectbox("등록할 비즈프로필", list(names), format_func=names.get, key="biz_pick",
                                    index=list(names).index(conn["biz_id"]) if conn["biz_id"] in names else 0)
            go.button("이 프로필로 바꾸기", on_click=_switch_profile, args=(chosen,), width="stretch",
                      disabled=chosen == conn["biz_id"])
        if conn["control_status"] == "FAILED" and conn["control_message"] and not busy:
            st.html(f'<p class="fm-why bad">{common.esc(conn["control_message"])}</p>')
        elif status == "DISCONNECTED" and conn["control_action"] == "DISCONNECT" and conn["control_status"] == "DONE":
            st.html(f'<p class="fm-why">{common.esc(conn["control_message"] or "")}</p>')
    with action:
        if busy and conn["control_action"] in ("CONNECT", "SELECT"):
            st.button("연결 취소", on_click=_cancel_connect, width="stretch")
        elif busy:
            st.button("처리 중", disabled=True, width="stretch")
        elif status == "CONNECTED":
            st.button("다른 비즈프로필 고르기", on_click=_select_profile, width="stretch", icon=":material/swap_horiz:",
                      help="한 로그인으로 비즈프로필을 여러 개 관리할 때. Aside 창에 비즈프로필 목록이 뜹니다.")
            if st.session_state.get("confirm_disconnect"):
                st.html('<p class="fm-note">이 컴퓨터에 저장된 로그인 정보를 지웁니다.</p>')
                yes, no = st.columns(2)
                yes.button("지우고 해제", type="primary", on_click=_disconnect, width="stretch")
                no.button("그대로 두기", on_click=_keep, width="stretch")
            else:
                st.button("연결 해제", on_click=_ask_disconnect, width="stretch")
        else:
            st.button("다시 연결" if status == "EXPIRED" else "당근 계정 연결", type="primary", on_click=_connect,
                      width="stretch", icon=":material/link:")
            if status == "EXPIRED":
                st.button("연결 해제", on_click=_disconnect, width="stretch")
    if not worker_running(cfg.data_dir):
        st.html('<p class="fm-why bad">자동화 엔진이 꺼져 있습니다. 왼쪽 메뉴 아래의 「자동화 엔진 켜기」를 눌러 주세요.</p>')
    if cfg.daangn_target == "practice":
        st.html(f'<p class="fm-note">지금은 연습용 화면({common.esc(cfg.practice_base_url)})에 연결합니다. '
                '실제 당근에 올리려면 .env 의 FM_DAANGN_TARGET 을 real 로 바꿉니다.</p>')


def _daangn() -> None:
    conn = _overview()["connection"]
    busy = conn["control_status"] in ("REQUESTED", "RUNNING")
    st.session_state["_conn_polling"] = busy
    with st.container(border=True):
        st.fragment(run_every="2s" if busy else None)(_daangn_body)()


# --- AI 연결 ------------------------------------------------------------------
def _save_ai(provider: str) -> None:
    mode = st.session_state.get(f"mode_{provider}") or "NONE"
    try:
        settings_service.save_ai(common.engine(), common.secrets(), provider, mode=mode,
                                 api_key=st.session_state.get(f"key_{provider}"))
        st.session_state[f"key_{provider}"] = ""
        common.flash(f"{PROVIDERS[provider]['title']} 연결 방식을 저장했습니다.")
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _delete_key(provider: str) -> None:
    try:
        settings_service.delete_ai_key(common.engine(), common.secrets(), provider)
        st.session_state[f"mode_{provider}"] = "NONE"
        common.flash(f"{PROVIDERS[provider]['title']} 의 저장된 키를 지웠습니다.")
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _ai_card(provider: str, view: dict, mock: bool) -> None:
    meta = PROVIDERS[provider]
    with st.container(border=True):
        state = ("ok", "연결됨") if view["ready"] else ("", "연결 안 됨")
        if mock:
            state = ("ok", "흉내 모드")
        st.html(f'<p class="fm-sec">{common.esc(meta["title"])} {common.pill(*state)}</p>'
                f'<p class="fm-note">{common.esc(meta["note"])}</p>')
        if f"mode_{provider}" not in st.session_state:
            st.session_state[f"mode_{provider}"] = view["mode"]
        mode = st.segmented_control("연결 방식", list(MODE_LABEL), format_func=MODE_LABEL.get, key=f"mode_{provider}") or "NONE"
        if mode == "API_KEY":
            saved = f"저장된 키 {view['key_hint']} · 바꾸려면 새 키 입력" if view["key_hint"] else meta["key_hint"]
            st.text_input("API 키", type="password", placeholder=saved, key=f"key_{provider}")
            st.html('<p class="fm-note">키는 이 프로젝트 폴더 밖의 개인 폴더에만 저장합니다.</p>')
            if view["key_hint"]:
                st.button("저장된 키 지우기", key=f"delkey_{provider}", on_click=_delete_key, args=(provider,))
        elif mode == "SUBSCRIPTION":
            if view["cli_available"]:
                st.html(f'<p class="fm-note">이 컴퓨터에 로그인된 {common.esc(meta["cli"])} 의 구독을 씁니다. 키를 넣지 않아도 됩니다.</p>')
            else:
                st.html(f'<p class="fm-why bad">이 컴퓨터에서 {common.esc(meta["cli"])} 를 찾지 못했습니다. 설치하고 로그인한 뒤 다시 확인해 주세요.</p>')
        save, check = st.columns(2)
        save.button("저장", key=f"save_{provider}", on_click=_save_ai, args=(provider,), width="stretch")
        if check.button("연결 확인", key=f"check_{provider}", width="stretch", disabled=view["mode"] == "NONE"):
            with st.spinner("실제로 한 번 불러 보는 중입니다"):
                out = settings_service.check_ai(common.engine(), common.secrets(), common.settings(), provider)
            common.flash(out["detail"], "success" if out["status"] == "OK" else "error")
            st.rerun()
        if view["status"] != "UNKNOWN" and view["detail"]:
            css = "" if view["status"] == "OK" else "bad"
            st.html(f'<p class="fm-why {css}">{common.esc(view["detail"])} · {common.esc(common.kst(view["checked_at"]))} 확인</p>')


# --- 지침 ---------------------------------------------------------------------
def _save_prompt(kind: str) -> None:
    try:
        out = settings_service.save_prompt(common.engine(), kind, st.session_state.get(f"prompt_{kind}", ""))
        common.flash("바뀐 내용이 없습니다." if out["result"] == "UNCHANGED" else "지침을 저장했습니다. 다음 등록부터 적용합니다.",
                     "info" if out["result"] == "UNCHANGED" else "success")
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _reset_prompt(kind: str) -> None:
    try:
        settings_service.reset_prompt(common.engine(), kind)
        st.session_state.pop(f"prompt_{kind}", None)
        common.flash("기본 지침으로 되돌렸습니다.")
    except Exception as exc:
        common.flash(common.error_text(exc), "error")


def _prompt_card(kind: str, view: dict) -> None:
    title, note = PROMPTS[kind]
    with st.container(border=True):
        state = common.pill("", "기본값") if view["is_default"] else common.pill("ok", f"고쳐 씀 · {view['version_no']}번째 판")
        st.html(f'<p class="fm-sec">{common.esc(title)} {state}</p><p class="fm-note">{common.esc(note)}</p>')
        if f"prompt_{kind}" not in st.session_state:
            st.session_state[f"prompt_{kind}"] = view["body"]
        st.text_area(title, key=f"prompt_{kind}", height=260, max_chars=PROMPT_MAX_CHARS, label_visibility="collapsed")
        save, reset = st.columns(2)
        save.button("저장", key=f"save_prompt_{kind}", on_click=_save_prompt, args=(kind,), width="stretch")
        reset.button("기본값으로 되돌리기", key=f"reset_prompt_{kind}", on_click=_reset_prompt, args=(kind,),
                     width="stretch", disabled=view["is_default"])


def run() -> None:
    common.page_header("계정 설정", "당근 계정과 AI 를 연결하고, AI 가 따를 지침을 정합니다")
    common.show_flash()
    view = _overview()
    _daangn()
    st.html('<div style="height:8px"></div>')
    common.section("AI 연결", "API 키를 넣거나, 이 컴퓨터에 로그인된 구독을 그대로 씁니다")
    left, right = st.columns(2, gap="medium")
    with left:
        _ai_card("CLAUDE", view["providers"]["CLAUDE"], view["ai_mode"] == "mock")
    with right:
        _ai_card("GPT", view["providers"]["GPT"], view["ai_mode"] == "mock")
    st.html('<div style="height:8px"></div>')
    common.section("지침", "기본값이 들어 있습니다. 가게 말투에 맞게 고쳐 쓸 수 있습니다")
    left, right = st.columns(2, gap="medium")
    with left:
        _prompt_card("TEXT", view["prompts"]["TEXT"])
    with right:
        _prompt_card("IMAGE", view["prompts"]["IMAGE"])
    cfg = common.settings()
    st.html(f'<p class="fm-note" style="margin-top:16px">AI 가 만든 이미지와 화면 캡처는 {common.esc(common.relative_path(cfg.data_dir))} 에, '
            f'로그인 정보와 API 키는 {common.esc(common.relative_path(cfg.private_dir))} 에 둡니다. 둘 다 프로젝트 폴더 밖입니다.</p>')
