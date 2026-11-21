"""화면 진입점. 메뉴는 세 개: 대시보드 · 상품 등록 · 계정 설정.

실행: python -m streamlit run streamlit_app.py
화면이 뜨면 워커(자동화 엔진)도 함께 뜬다. 화면을 끄면 워커도 꺼진다.
"""
import sys

sys.dont_write_bytecode = True      # 캐시 파일(.pyc)에는 내 PC 의 경로가 들어간다. 프로젝트 폴더에 남기지 않는다.

import streamlit as st  # noqa: E402

from futuremarketer.worker import worker_running  # noqa: E402
from ui import common, dashboard, register_page, settings_page  # noqa: E402

st.set_page_config(page_title="퓨처마케터 당근 자동 등록", page_icon=":material/storefront:", layout="wide")

PAGES = {
    "dashboard": st.Page(dashboard.run, title="대시보드", icon=":material/space_dashboard:", url_path="dashboard",
                         default=True),
    "register": st.Page(register_page.run, title="상품 등록", icon=":material/inventory_2:", url_path="register"),
    "settings": st.Page(settings_page.run, title="계정 설정", icon=":material/tune:", url_path="settings"),
}
st.session_state["_pages"] = PAGES
current = st.navigation(list(PAGES.values()), position="hidden")

ready = common.boot()

with st.sidebar:
    st.html('<p class="fm-brand">퓨처마케터</p><p class="fm-brand-sub">당근 자동 등록</p>')
    for page in PAGES.values():
        st.page_link(page)
    if ready:
        cfg = common.settings()
        running = worker_running(cfg.data_dir)
        lines = [f'<p class="fm-side {"on" if running else ""}"><span class="fm-dot"></span>'
                 f'자동화 엔진 {"실행 중" if running else "꺼짐"}</p>']
        if cfg.daangn_target == "practice":
            lines.append('<p class="fm-side">연습용 화면에 등록합니다</p>')
        if cfg.ai_mode == "mock":
            lines.append('<p class="fm-side">AI 는 흉내 모드입니다</p>')
        st.html(f'<div class="fm-side-box">{"".join(lines)}</div>')
        if not running and st.button("자동화 엔진 켜기", width="stretch"):
            common.start_worker(wait=True)
            st.rerun()

if ready:
    current.run()
