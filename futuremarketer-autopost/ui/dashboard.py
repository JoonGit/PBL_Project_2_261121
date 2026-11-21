"""대시보드: 연결 상태, 등록 현황, 작업 기록을 보여 준다. 상품을 불러오거나 등록하는 기능은 두지 않는다."""
from __future__ import annotations

import streamlit as st

from futuremarketer.defaults import STEP_LABEL
from futuremarketer.services import open_service, query_service
from ui import common

OUTCOME_LABEL = {"SUCCEEDED": ("DONE", "등록 완료"), "FAILED": ("FAILED", "실패"), "NEEDS_CHECK": ("NEEDS_CHECK", "확인 필요")}
HISTORY_SHOW = {"ALL": "전체", "PROBLEM": "실패·확인 필요", "SUCCEEDED": "완료"}
MODE_LABEL = {"API_KEY": "API 키", "SUBSCRIPTION": "구독 로그인", "NONE": ""}


def _open_button(label: str, url: str, key: str) -> None:
    """링크를 평소 브라우저가 아니라 로그인된 Aside 창으로 연다."""
    if st.button(label, key=key, icon=":material/open_in_new:"):
        code, message = open_service.open_in_aside(common.settings(), common.engine(), url)
        (st.toast if code == "OPENED" else st.warning)(message)


def _daangn_tile(conn: dict) -> str:
    status = conn["status"]
    if status == "CONNECTED":
        name = conn["biz_name"] or f"비즈프로필 {conn['biz_id']}"
        return common.status_tile("당근 계정", "ok", "연결됨", f"{name} · {common.kst(conn['connected_at'])} 연결")
    if status == "CONNECTING":
        return common.status_tile("당근 계정", "RUNNING", "연결 중", "Aside 창에서 로그인을 기다립니다")
    if status == "EXPIRED":
        return common.status_tile("당근 계정", "warn", "다시 연결 필요", "로그인이 풀렸습니다")
    return common.status_tile("당근 계정", "", "연결 안 됨", "상품을 등록하려면 먼저 연결합니다")


def _ai_tile(label: str, ready: bool, row: dict, need: str, mock: bool) -> str:
    if mock:
        return common.status_tile(label, "ok", "흉내 모드", "연결 없이 예시 결과를 만듭니다")
    if ready:
        return common.status_tile(label, "ok", "연결됨", MODE_LABEL.get(row["mode"], ""))
    return common.status_tile(label, "", "연결 안 됨", need)


def _body() -> None:
    cfg = common.settings()
    data = query_service.dashboard(common.engine(), common.secrets(), cfg)
    conn, env, counts = data["connection"], data["env"], data["counts"]
    mock = cfg.ai_mode == "mock"

    common.section("연결")
    cols = st.columns(3)
    cols[0].html(_daangn_tile(conn))
    cols[1].html(_ai_tile("글 작성 · Claude", env.claude_ready, data["ai"]["CLAUDE"], "대본이 없는 상품에 필요합니다", mock))
    cols[2].html(_ai_tile("이미지 생성 · GPT", env.gpt_ready, data["ai"]["GPT"], "이미지가 없는 상품에 필요합니다", mock))
    if data["manage_url"] or data["profile_url"]:
        left, right, _rest = st.columns([1, 1, 1])
        with left:
            if data["manage_url"]:
                _open_button("비즈니스 계정 열기", data["manage_url"], "open-manage")
        with right:
            if data["profile_url"]:
                _open_button("당근에서 업체 보기", data["profile_url"], "open-profile")
            else:
                st.caption("업체 프로필 주소는 다음 등록이나 연결 때 확인됩니다")
    if not (env.daangn_connected and env.claude_ready and env.gpt_ready):
        st.page_link(st.session_state["_pages"]["settings"], label="계정 설정에서 연결하기", icon=":material/arrow_forward:")

    st.html('<div style="height:16px"></div>')
    batch = data["batch"]
    common.section("상품 현황", f"{batch['file_name']} · {common.kst(batch['created_at'])}에 불러옴" if batch
                   else "아직 불러온 엑셀이 없습니다")
    attention = counts["FAILED"] + counts["NEEDS_CHECK"]
    cols = st.columns(5)
    cols[0].html(common.tile("불러온 상품", str(counts["total"]), "개"))
    cols[1].html(common.tile("등록 가능", str(counts["READY"]), "개"))
    cols[2].html(common.tile("등록 완료", str(counts["DONE"]), "개"))
    cols[3].html(common.tile("확인이 필요한 상품", str(attention), "개", "상품 등록에서 확인" if attention else ""))
    cols[4].html(common.tile("오늘 올린 소식", str(data["news_today"]), f"/ {data['news_limit']}", "하루 5개까지"))



def _history_row(row: dict) -> None:
    """작업 기록 한 줄: 끝난(멈춘) 순간의 화면 + 결과·상품·이유, 펼치면 그 시도의 진행 기록과 화면 전부."""
    state, label = OUTCOME_LABEL[row["outcome"]]
    if row["kind"] == "REMOVE" and row["outcome"] == "SUCCEEDED":
        label = "삭제 완료"
    elif row["kind"] == "REMOVE":
        label = f"삭제 {label}"
    stopped = (f" · {STEP_LABEL[row['step']]} 단계에서 멈춤"
               if row["outcome"] != "SUCCEEDED" and row["step"] in STEP_LABEL else "")
    practice = " · 연습용 화면" if row["target"] == "PRACTICE" else ""
    with st.container(border=True):
        left, right = st.columns([1, 3], gap="medium")
        with left:
            preview = common.preview(row["shot_key"])
            if preview:
                st.image(preview, width="stretch")
            else:
                st.html('<div class="fm-noshot">브라우저 화면 없음</div>')
        with right:
            note_class = "bad" if row["outcome"] == "FAILED" else ""
            st.html(
                f'<div class="fm-hist-head">{common.pill(state, label)}'
                f'<span class="fm-meta">{common.esc(common.kst(row["finished_at"]))}</span></div>'
                f'<p class="fm-name">{common.esc(row["name"])}</p>'
                f'<p class="fm-meta">{common.esc(row["code"])} · {common.esc(common.won(row["price"]))}'
                f'{common.esc(stopped)}{common.esc(practice)}</p>'
                f'<p class="fm-why {note_class}">{common.esc(row["message"])}</p>')
            if row["news_url"]:
                _open_button("등록된 소식 보기", row["news_url"], f"news-{row['id']}")
            detail = st.expander(f"진행 기록 {len(row['events'])}줄 · 화면 {len(row['screens'])}장",
                                 key=f"hist-{row['id']}", on_change="rerun")
            if detail.open:            # 펼쳤을 때만 그림을 불러온다
                with detail:
                    st.html("".join(
                        f'<p class="fm-log {common.esc(e["level"])}"><time>{common.esc(common.kst(e["created_at"], "%H:%M:%S"))}</time>'
                        f'<span>{common.esc((STEP_LABEL.get(e["step"]) + " · ") if e["step"] else "")}'
                        f'{common.esc(e["message"])}</span></p>' for e in row["events"]))
                    shots = [e for e in row["screens"] if common.store().exists(e["shot_key"])]
                    for start in range(0, len(shots), 2):
                        cols = st.columns(2)
                        for col, event in zip(cols, shots[start:start + 2]):
                            col.image(str(common.store().path_for(event["shot_key"])), width="stretch",
                                      caption=f'{common.kst(event["created_at"], "%H:%M:%S")} · {event["message"][:60]}')


def _history() -> None:
    left, right = st.columns([3, 2], vertical_alignment="center")
    with left:
        show = st.segmented_control("보기", list(HISTORY_SHOW), default="ALL", format_func=HISTORY_SHOW.get,
                                    key="hist_show", label_visibility="collapsed", required=True)
    limit = st.session_state.get("hist_limit", 10)
    data = query_service.history(common.engine(), limit=limit, show=show or "ALL", settings=common.settings())
    counts = data["counts"]
    summary = [f"완료 {counts.get('SUCCEEDED', 0)}", f"실패 {counts.get('FAILED', 0)}"]
    if counts.get("NEEDS_CHECK"):
        summary.append(f"확인 필요 {counts['NEEDS_CHECK']}")
    right.html(f'<p class="fm-meta fm-right">지금까지 {data["total"]}번 시도 · {" · ".join(summary)}</p>')
    if not data["rows"]:
        st.html('<p class="fm-empty">아직 기록이 없습니다. 상품 등록 메뉴에서 등록을 시작하면 시도마다 한 줄씩 남습니다.</p>'
                if data["total"] == 0 else '<p class="fm-empty">이 보기에 해당하는 기록이 없습니다.</p>')
    for row in data["rows"]:
        _history_row(row)
    if data["more"] and st.button("더 보기", key="hist_more"):
        st.session_state["hist_limit"] = limit + 10
        st.rerun(scope="fragment")


def run() -> None:
    common.page_header("대시보드", "연결 상태와 등록 현황, 작업 기록을 봅니다")
    st.fragment(run_every="5s")(_body)()
    st.html('<div style="height:16px"></div>')
    common.section("작업 기록", "등록을 시도할 때마다 한 줄씩 남습니다. 성공한 줄에는 마지막 화면이, "
                   "실패한 줄에는 멈춘 순간의 화면이 붙습니다.")
    st.fragment(run_every="10s")(_history)()
