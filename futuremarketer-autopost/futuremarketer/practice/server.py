"""연습용 화면: 내 PC 안에서만 뜨는 가짜 가게 관리 화면.

실제 당근 비즈니스가 아니다. 자동화가 화면을 어떻게 조작하는지 안전하게 보려고 만든 것으로,
주소 모양과 입력란·버튼 글자를 실제 화면과 같게 맞췄다. 그래서 같은 자동화 코드가 그대로 돈다.

    /login
    /biz_accounts/1001/manager/home       등록된 가격·소식 보기
    /biz_accounts/1001/manager/pricing    가격 추가(사진·항목·정가·추가 설명) 또는 수정 → 완료 → 저장
    /biz_accounts/1001/manager/posts/new  사진(사진 추가 → 내 앨범에서 선택) → 제목·본문 → 다음 → 완료

실제 화면과 같게 맞춘 점: 칸별 글자 수(항목 15자, 추가 설명 50자, 소식 제목 30자, 사진 10장),
쓰다 만 소식이 있으면 「작성 중인 소식이 있어요」 물음, 화면 구석의 채팅 버튼이 「다음」「완료」를 가리는 것.
"""
from __future__ import annotations

import html
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BIZ_ID = "1001"
BIZ_NAME = "연습용 가게"
PUBLIC_ID = "7001"          # 손님이 보는 업체 프로필 번호(실제 화면처럼 비즈프로필 번호와 다르다)
# 한 로그인으로 관리하는 비즈프로필이 여럿인 경우를 연습하려고 가게를 둘 둔다: 번호 → (이름, 손님이 보는 업체 번호)
PROFILES = {BIZ_ID: (BIZ_NAME, PUBLIC_ID), "1002": ("연습용 가게 2호점", "7002")}
MANAGER_RE = re.compile(r"^/biz_accounts/(\d+)/manager(/.*)?$")
COOKIE = "practice_session=ok"
MANAGER = f"/biz_accounts/{BIZ_ID}/manager"
MAX_UPLOAD = 25 * 1024 * 1024

STYLE = """
:root{--bg:#f7f7f8;--card:#fff;--ink:#1b1b1d;--sub:#55555a;--muted:#77777c;--line:rgba(27,27,29,.1);--accent:#ff5f1f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
.banner{background:var(--ink);color:#fff;padding:8px 24px;font-size:13px}
.wrap{max-width:720px;margin:0 auto;padding:24px}
h1{font-size:24px;margin:8px 0 20px}h2{font-size:16px;margin:28px 0 12px}h3{font-size:18px;margin:0 0 16px}
nav a{margin-right:16px;color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin-bottom:8px}
.row{display:flex;justify-content:space-between;gap:16px}.muted{color:var(--muted);font-size:13px}
input[type=text],input:not([type]),textarea{width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font:inherit;background:#fff}
.editor{min-height:220px;padding:12px;border:1px solid var(--line);border-radius:10px;background:#fff;white-space:pre-wrap;outline:none}
.editor:empty:before{content:attr(data-placeholder);color:var(--muted)}
button{font:inherit;padding:10px 18px;border-radius:10px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);font-weight:600}
button:disabled{opacity:.4;cursor:default}
.field{margin-bottom:16px}.field>span{display:block;font-size:13px;color:var(--sub);margin-bottom:6px}
.actions{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
.overlay{position:fixed;inset:0;background:rgba(27,27,29,.45);display:flex;align-items:center;justify-content:center}
.dialog{background:#fff;border-radius:16px;padding:24px;width:440px;max-width:92vw}
.toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:var(--ink);color:#fff;padding:12px 20px;border-radius:10px}
.thumbs{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.thumbs img{width:88px;height:88px;object-fit:cover;border-radius:8px;border:1px solid var(--line)}
.post img{width:120px;height:120px;object-fit:cover;border-radius:8px;margin:8px 8px 0 0}
.post pre{white-space:pre-wrap;font:inherit;margin:8px 0 0;color:var(--sub)}
.wrap{padding-bottom:120px}
.bar{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:flex-end;gap:8px;padding:16px 24px;background:#fff;border-top:1px solid var(--line)}
.chat{position:fixed;right:16px;bottom:12px;width:64px;height:64px;border-radius:32px;padding:0;background:var(--accent);border:0;color:#fff;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.pick{width:72px;height:72px;display:inline-flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:0;font-size:12px;color:var(--sub)}
.busy{border:1px dashed var(--line);border-radius:10px}
.pic{position:relative;width:72px;height:72px}.pic img{width:72px;height:72px;object-fit:cover;border-radius:10px}
.pic button{position:absolute;top:-8px;right:-8px;width:24px;height:24px;padding:0;border-radius:12px}
.photos{display:flex;gap:8px;align-items:flex-start;margin-bottom:16px}.photos .thumbs{margin-top:0}
.ph img{width:72px;height:72px;object-fit:cover;border-radius:10px;border:1px solid var(--line)}
.label{display:flex;gap:6px;align-items:baseline;margin-bottom:8px}h3.sub{font-size:15px;margin:0}
.count{text-align:right;margin:-8px 0 12px}.item{align-items:center}.grow{flex:1}.grow p{margin:0}
.pthumb img{width:56px;height:56px;object-fit:cover;border-radius:8px}.pthumb{width:56px}
.opt{display:flex;flex-direction:column;align-items:flex-start;gap:2px;width:100%;text-align:left;margin-bottom:8px}
[hidden]{display:none!important}
"""


def page(title: str, body: str, script: str = "") -> bytes:
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{html.escape(title)} · 연습용 화면</title>
<style>{STYLE}</style></head><body>
<div class="banner">연습용 화면 — 이 컴퓨터 안에서만 보이며 실제 당근 비즈니스가 아닙니다</div>
<div class="wrap">{body}</div>
<script>fetch('/api/me');{script}</script></body></html>""".encode("utf-8")


POSTS_MENU = """
<div id="sheet" class="overlay" hidden><div class="dialog" role="dialog" aria-modal="true">
 <button type="button" class="opt">공유하기</button><button type="button" class="opt">소식 수정</button>
 <button type="button" class="opt" id="askDelete" style="color:#d33">삭제</button>
 <button type="button" class="opt" id="sheetClose">닫기</button></div></div>
<div id="confirm" class="overlay" hidden><div class="dialog" role="dialog" aria-modal="true">
 <h3>소식을 삭제할까요?</h3><p class="muted">삭제한 소식은 되돌릴 수 없어요.</p>
 <div class="actions"><button type="button" id="no">취소</button><button type="button" id="yes" class="primary">삭제</button></div></div></div>
"""
POSTS_SCRIPT = r"""
const el=id=>document.getElementById(id);let target=-1;
document.querySelectorAll('.kebab').forEach(b=>b.onclick=()=>{target=Number(b.dataset.index);el('sheet').hidden=false});
el('sheetClose').onclick=()=>{el('sheet').hidden=true};
el('askDelete').onclick=()=>{el('sheet').hidden=true;el('confirm').hidden=false};
el('no').onclick=()=>{el('confirm').hidden=true};
el('yes').onclick=async()=>{const r=await fetch('/api/posts/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:target})});
 if(r.ok){location.reload()}else{alert('소식을 삭제하지 못했어요')}};
"""

NAV = (f'<nav><a href="{MANAGER}/home">홈</a><a href="{MANAGER}/pricing">가격</a>'
       f'<a href="{MANAGER}/posts/new">소식 쓰기</a><a href="{MANAGER}/posts">소식</a></nav>')

LOGIN_BODY = """
<h1>연습용 로그인</h1>
<p>실제 계정 정보는 필요 없습니다. 아래 버튼을 누르면 연습용 가게로 로그인됩니다.</p>
<form method="post" action="/login"><button class="primary" type="submit">연습 계정으로 로그인</button></form>
"""

CAMERA = ('<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
          'aria-hidden="true"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>')

PRICING_SCRIPT = r"""
const fmt=n=>Number(n).toLocaleString('ko-KR')+'원';let items=[],editing=-1,draft=null,dirty=false;
const el=id=>document.getElementById(id);
function render(){
 el('list').innerHTML=items.map((it,i)=>`<div class="card row item"><div class="pthumb">${it.image?`<img src="/files/${it.image}" alt="">`:''}</div>
  <div class="grow"><p class="name"></p><p class="muted desc"></p><p class="price">${fmt(it.price)}</p></div>
  <div><button type="button" data-edit="${i}">수정</button></div></div>`).join('')||'<p class="muted">제공하는 서비스/제품의 가격을 입력하세요.</p>';
 document.querySelectorAll('#list .name').forEach((n,i)=>n.textContent=items[i].name);
 document.querySelectorAll('#list .desc').forEach((n,i)=>n.textContent=items[i].description||'');
 document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openDialog(Number(b.dataset.edit)));
 el('save').disabled=!dirty}
function photoBox(){
 const box=el('photoBox');
 if(draft.uploading){box.innerHTML='<div class="pick busy" aria-label="올리는 중">올리는 중…</div>';return}
 if(draft.image){box.innerHTML=`<div class="pic"><img alt="가격 이미지" src="/files/${draft.image}"><button type="button" id="photoDel" aria-label="사진 지우기">×</button></div>`;
  el('photoDel').onclick=()=>{draft.image=null;photoBox();check()};return}
 box.innerHTML='<button type="button" id="photoPick" class="pick">CAMERA</button>';
 el('photoPick').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='image/*';inp.hidden=true;document.body.appendChild(inp);
  inp.onchange=async()=>{const f=inp.files[0];inp.remove();if(!f)return;draft.uploading=true;photoBox();check();
   const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',headers:{'Content-Type':f.type||'application/octet-stream'},body:f});
   await new Promise(ok=>setTimeout(ok,400));      // 실제 화면처럼 올리는 데 잠깐 걸린다
   draft.uploading=false;if(r.ok){draft.image=(await r.json()).id}else{alert('사진을 올리지 못했어요')}photoBox();check()};
  inp.click()}}
const check=()=>{el('nameCount').textContent=el('name').value.length;const p=el('price').value.replace(/,/g,'');
 el('done').disabled=!(el('name').value.trim()&&/^\d+$/.test(p)&&Number(p)>0)||draft.uploading};
function openDialog(i){editing=i;const it=i>=0?items[i]:{name:'',price:'',description:'',image:null};draft={image:it.image||null,uploading:false};
 el('dlgTitle').textContent=i>=0?'가격 수정':'가격 추가';el('del').hidden=i<0;el('name').value=it.name;
 el('price').value=it.price?Number(it.price).toLocaleString('ko-KR'):'';el('desc').value=it.description||'';
 photoBox();check();el('modal').hidden=false}
el('add').onclick=()=>openDialog(-1);
el('del').onclick=()=>{if(editing>=0){items.splice(editing,1);dirty=true;el('modal').hidden=true;render()}};
el('name').oninput=check;el('price').oninput=check;
el('done').onclick=()=>{const it={name:el('name').value,price:Number(el('price').value.replace(/,/g,'')),description:el('desc').value.trim(),image:draft.image||null};
 if(editing>=0)items[editing]={...items[editing],...it};else items.push(it);dirty=true;el('modal').hidden=true;render()};
document.addEventListener('keydown',e=>{if(e.key==='Escape')el('modal').hidden=true});
el('save').onclick=async()=>{el('save').disabled=true;const r=await fetch('/api/pricing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})});
 if(r.ok){items=(await r.json()).prices;dirty=false;render();el('toast').hidden=false;setTimeout(()=>el('toast').hidden=true,4000)}else{el('save').disabled=false;alert('저장하지 못했어요')}};
fetch('/api/state').then(r=>r.json()).then(s=>{items=s.prices;render()});
""".replace("CAMERA", CAMERA)

PRICING_BODY = NAV + """
<h1>가격</h1>
<div class="row" style="align-items:center"><h2>가격 목록</h2><button id="add" type="button">가격 추가</button></div>
<div id="list"></div>
<div class="bar"><button id="save" class="primary" type="button" disabled>저장</button></div>
<div id="modal" class="overlay" hidden><div class="dialog" role="dialog" aria-modal="true">
 <h3 id="dlgTitle">가격 추가</h3>
 <div class="field"><div class="label"><h3 class="sub">사진</h3><span class="muted">(선택)</span></div><div id="photoBox"></div></div>
 <label class="field"><span>항목</span><input id="name" type="text" placeholder="항목명을 입력하세요. (예: 커피)" maxlength="15"></label>
 <p class="muted count"><span id="nameCount">0</span>/15</p>
 <div class="field"><span class="label-text">가격</span>
  <label><input type="radio" name="ptype" checked> 정가</label>
  <label><input type="radio" name="ptype" disabled> 범위</label>
  <label><input type="radio" name="ptype" disabled> 별도 문의</label>
  <div style="margin-top:8px"><input id="price" type="text" inputmode="numeric" placeholder="가격을 입력하세요."></div></div>
 <label class="field"><span>추가 설명 (선택)</span><textarea id="desc" rows="2" maxlength="50" placeholder="가격에 대한 설명이 필요하다면 적어주세요."></textarea></label>
 <p style="margin:0"><button id="del" type="button" style="border:0;background:none;padding:0;color:#d33;text-decoration:underline">삭제하기</button></p>
 <div class="actions"><button id="done" class="primary" type="button" disabled>완료</button></div>
</div></div>
<div id="toast" class="toast" hidden>가격표 정보를 수정했어요</div>
"""

POST_SCRIPT = r"""
const el=id=>document.getElementById(id);const KEY='practice-post-draft';let images=[],uploading=0,bodyText='';
const counter=()=>{el('cnt').textContent=el('thumbs').children.length;el('photo').disabled=uploading>0};
const ready=()=>{el('next').disabled=!(el('title').value.trim().length>=2&&el('body').innerText.trim().length>=10&&uploading===0)};
const keep=()=>{try{localStorage.setItem(KEY,JSON.stringify({title:el('title').value,body:el('body').innerText}))}catch(e){}};
el('title').oninput=()=>{ready();keep()};el('body').addEventListener('input',()=>{ready();keep()});
try{const d=JSON.parse(localStorage.getItem(KEY)||'null');if(d&&(d.title||d.body).trim()){
 el('askText').textContent=`'${(d.title||'제목 없음').slice(0,20)}..' 글을 이어서 쓸까요?`;el('ask').hidden=false;
 el('fresh').onclick=()=>{localStorage.removeItem(KEY);el('ask').hidden=true};
 el('resume').onclick=()=>{el('title').value=d.title||'';el('body').innerText=d.body||'';el('ask').hidden=true;ready()}}}catch(e){}
el('photo').onclick=()=>{el('sheet').hidden=false};
el('sheetClose').onclick=()=>{el('sheet').hidden=true};
el('album').onclick=()=>{el('sheet').hidden=true;const inp=document.createElement('input');inp.type='file';inp.accept='image/*';inp.multiple=true;inp.hidden=true;
 document.body.appendChild(inp);inp.onchange=()=>{const files=[...inp.files].slice(0,10-el('thumbs').children.length);inp.remove();files.forEach(upload)};inp.click()};
function upload(f){
 const box=document.createElement('div');box.setAttribute('role','button');box.tabIndex=0;box.setAttribute('aria-roledescription','sortable');box.className='ph';
 const img=document.createElement('img');box.appendChild(img);el('thumbs').appendChild(box);uploading++;counter();ready();
 const reader=new FileReader();reader.onload=()=>{if(!img.src)img.src=reader.result};reader.readAsDataURL(f);   // 올라가기 전에는 내 PC 의 그림으로 보인다
 fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',headers:{'Content-Type':f.type||'application/octet-stream'},body:f})
  .then(r=>r.ok?r.json():Promise.reject()).then(j=>new Promise(ok=>setTimeout(()=>ok(j),300)))
  .then(j=>{images.push(j.id);img.src='/files/'+j.id;box.dataset.id=j.id})
  .catch(()=>{box.remove();alert('사진 업로드에 실패했어요')}).finally(()=>{uploading--;counter();ready()})}
el('next').onclick=()=>{bodyText=el('body').innerText.replace(/ /g,' ').trim();history.pushState({},'','?step=setting');
 el('step1').hidden=true;el('step2').hidden=false;el('next').hidden=true;el('finish').hidden=false;
 el('summary').textContent=el('title').value.trim()+' · 사진 '+images.length+'장'};
el('finish').onclick=async()=>{el('finish').disabled=true;
 const order=[...el('thumbs').children].map(b=>b.dataset.id).filter(Boolean);
 const r=await fetch('/api/posts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:el('title').value.trim(),body:bodyText,images:order})});
 if(r.ok){try{localStorage.removeItem(KEY)}catch(e){}location.href='MANAGER/posts/?postId='+((await r.json()).id||'')}else{el('finish').disabled=false;alert('소식 작성을 실패했어요')}};
el('chat').onclick=()=>alert('연습용 화면에는 채팅이 없습니다.');
""".replace("MANAGER", MANAGER)

POST_BODY = NAV + """
<h1>소식 작성</h1>
<div id="step1">
 <div class="photos"><button id="photo" type="button" class="pick">""" + CAMERA + """<span><span id="cnt">0</span>/10</span></button><div id="thumbs" class="thumbs"></div></div>
 <div class="field"><input id="title" type="text" placeholder="소식 제목" maxlength="30"></div>
 <div id="body" class="editor" contenteditable="plaintext-only" role="textbox" aria-multiline="true" data-placeholder="가게 소식을 자유롭게 써 주세요."></div>
</div>
<div id="step2" hidden>
 <div class="card"><p>소식 태그 <strong>소식</strong> · 단골 알림 <strong>알림 보내기</strong> · 채팅 및 댓글 <strong>채팅 받기, 댓글 받기</strong></p>
 <p class="muted">기본값 그대로 둡니다</p><div id="summary"></div></div>
</div>
<div class="bar"><button type="button">임시 저장</button><button id="next" class="primary" type="button" disabled>다음</button>
 <button id="finish" class="primary" type="button" hidden>완료</button></div>
<button id="chat" class="chat" type="button" aria-label="채팅 열기">채팅</button>
<div id="ask" class="overlay" hidden><div class="dialog" role="dialog" aria-modal="true">
 <h3>작성 중인 소식이 있어요</h3><p class="muted" id="askText"></p>
 <div class="actions"><button id="fresh" type="button">새로 쓰기</button><button id="resume" class="primary" type="button">이어서 쓰기</button></div>
</div></div>
<div id="sheet" class="overlay" hidden><div class="dialog" role="dialog" aria-modal="true">
 <h3>사진 추가</h3>
 <button id="album" type="button" class="opt"><span>내 앨범에서 선택</span><span class="muted">앨범에 있는 사진을 새로 추가해요.</span></button>
 <div class="actions"><button id="sheetClose" type="button">닫기</button></div>
</div></div>
"""


class PracticeState:
    """가게마다 가격·소식을 파일 하나에 기록한다(첫 가게는 state.json, 다른 가게는 state-<번호>.json).
    지우려면 앱 데이터 폴더의 practice 를 지우면 된다."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.lock = threading.Lock()

    def path_for(self, biz: str = BIZ_ID) -> Path:
        return self.path if biz == BIZ_ID else self.root / f"state-{biz}.json"

    def load(self, biz: str = BIZ_ID) -> dict:
        try:
            return json.loads(self.path_for(biz).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"prices": [], "posts": []}

    def save(self, data: dict, biz: str = BIZ_ID) -> None:
        path = self.path_for(biz)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


def make_handler(state: PracticeState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PracticeSite/1"

        def log_message(self, *_args):  # 콘솔을 조용하게
            pass

        # --- 응답 도우미 ---
        def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200):
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _redirect(self, location: str, headers: dict | None = None):
            self._send(302, b"", headers={"Location": location, **(headers or {})})

        def _authed(self) -> bool:
            return COOKIE in (self.headers.get("Cookie") or "")

        biz = BIZ_ID

        def _cookie_biz(self) -> str:
            """화면이 지금 보고 있는 가게(관리 화면을 열 때 쿠키로 남긴다). API 요청은 이 가게에 기록된다."""
            match = re.search(r"practice_biz=(\d+)", self.headers.get("Cookie") or "")
            return match[1] if match and match[1] in PROFILES else BIZ_ID

        def _body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                return None
            return self.rfile.read(length) if length else b""

        def _same_origin(self) -> bool:
            """다른 사이트가 이 연습용 화면으로 요청을 보내지 못하게 한다."""
            origin = self.headers.get("Origin")
            return origin is None or origin == f"http://{self.headers.get('Host')}"

        # --- GET ---
        def do_GET(self):
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            self.biz = self._cookie_biz()
            if path == "/api/me":
                name = PROFILES[self.biz][0]
                return self._json({"biz_id": self.biz, "name": name}) if self._authed() else self._json({"error": "unauthorized"}, 401)
            if path in ("/", "/login"):
                if self._authed():
                    return self._redirect(f"{MANAGER}/home")
                return self._send(200, page("로그인", LOGIN_BODY))
            if path == "/logout":
                return self._redirect("/login", {"Set-Cookie": "practice_session=; Path=/; Max-Age=0"})
            public = {pid: biz for biz, (_name, pid) in PROFILES.items()}
            if path.startswith("/kr/local-profile/") and path.rsplit("/", 1)[1] in public:   # 손님이 보는 업체 프로필
                self.biz = public[path.rsplit("/", 1)[1]]
                name = PROFILES[self.biz][0]
                return self._send(200, page(name, f"<h1>{html.escape(name)}</h1><p class=\"muted\">연습용 동네 업체</p>"
                                            + self._posts_html()))
            if path.startswith("/kr/business-posts/"):                   # 공유된 소식
                post_id = path.rsplit("/", 1)[1]
                post = next((p for biz in PROFILES for p in state.load(biz)["posts"] if p.get("id") == post_id), None)
                if post is None:
                    return self._send(404, page("없는 소식", "<h1>페이지를 찾을 수 없어요</h1>"))
                images = "".join(f'<img src="/kr/files/{html.escape(i)}" alt="">' for i in post.get("images", []))
                return self._send(200, page(post["title"], f'<div class="card post"><strong>{html.escape(post["title"])}</strong>'
                                            f'<pre>{html.escape(post["body"])}</pre>{images}</div>'))
            if path.startswith("/kr/files/"):
                path = path[3:]
                name = path.split("/", 2)[2]
                target = state.files / name
                if not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|webp)", name) or not target.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
                return self._send(200, target.read_bytes(), ctype)
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401) if path.startswith("/api/") else self._redirect("/login")
            if path == "/api/state":
                return self._json(state.load(self.biz))
            if path.startswith("/files/"):
                name = path.split("/", 2)[2]
                target = state.files / name
                if not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|webp)", name) or not target.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
                return self._send(200, target.read_bytes(), ctype)
            if path == "/select_profile":                     # 이 로그인으로 관리하는 비즈프로필 고르기
                links = "".join(f'<a class="card row" href="/biz_accounts/{biz}/manager/home"><strong>{html.escape(name)}</strong>'
                                f'<span class="muted">ID {biz}</span></a>' for biz, (name, _pid) in PROFILES.items())
                return self._send(200, page("비즈프로필 선택", "<h1>관리할 비즈프로필을 선택하세요</h1>" + links))
            match = MANAGER_RE.match(path)
            if match and match[1] in PROFILES and match[1] != BIZ_ID:    # 다른 가게: 같은 화면을 그 가게 주소로 보여 준다
                self.biz = match[1]
                inner = f"{MANAGER}{match[2] or ''}"
                return self._manager(inner, f"/biz_accounts/{self.biz}/")
            if match and match[1] == BIZ_ID:
                self.biz = BIZ_ID
                return self._manager(path, None)
            return self._send(404, page("없는 화면", NAV + "<h1>없는 화면입니다</h1>"))

        def _manager(self, path: str, swap: str | None):
            """관리 화면 하나. swap 이 있으면 화면 속 첫 가게 주소를 그 가게 주소로 바꿔 보낸다."""
            routes = {f"{MANAGER}/home": lambda: page("홈", self._home()),
                      f"{MANAGER}/home/update/base_info": lambda: page("기본 정보", NAV +
                          '<section><div><h3>증빙 서류</h3></div><div>' +
                          ('사업자등록증 · 인증 완료' if self.biz == BIZ_ID else '<button>＋ 사업자등록증 추가</button>') +
                          '</div><div>활동 증빙</div></section>'),
                      f"{MANAGER}/pricing": lambda: page("가격", PRICING_BODY, PRICING_SCRIPT),
                      f"{MANAGER}/posts/new": lambda: page("소식 작성", POST_BODY, POST_SCRIPT),
                      f"{MANAGER}/posts": lambda: page("소식", NAV + "<h1>소식</h1>" + self._posts_html(menu=True)
                                                       + POSTS_MENU, POSTS_SCRIPT)}
            make = routes.get(path)
            if make is None:
                return self._send(404, page("없는 화면", NAV + "<h1>없는 화면입니다</h1>"))
            body = make()
            if swap:
                body = body.replace(f"/biz_accounts/{BIZ_ID}/".encode(), swap.encode())
            return self._send(200, body, headers={"Set-Cookie": f"practice_biz={self.biz}; Path=/; SameSite=Lax"})

        def _posts_html(self, menu: bool = False) -> str:
            posts = state.load(self.biz)["posts"]
            if not posts:
                return '<p class="muted">아직 올린 소식이 없습니다.</p>'
            out = []
            for index in range(len(posts) - 1, -1, -1):
                post = posts[index]
                images = "".join(f'<img src="/files/{html.escape(i)}" alt="">' for i in post.get("images", []))
                more = (f'<button type="button" class="kebab" data-index="{index}" style="float:right">'
                        '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="2"/>'
                        '<circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg></button>') if menu else ""
                out.append(f'<div class="card post">{more}<strong>{html.escape(post["title"])}</strong>'
                           f'<div class="muted">{html.escape(post["created_at"])}</div>'
                           f'<pre>{html.escape(post["body"])}</pre>{images}</div>')
            return "".join(out)

        def _home(self) -> str:
            data = state.load(self.biz)
            prices = "".join(
                '<div class="card row item"><div class="pthumb">'
                + (f'<img src="/files/{html.escape(p["image"])}" alt="">' if p.get("image") else "")
                + f'</div><div class="grow"><p>{html.escape(p["name"])}</p><p class="muted">{html.escape(p.get("description") or "")}</p></div>'
                f'<span>{int(p["price"]):,}원</span></div>'
                for p in data["prices"]) or '<p class="muted">등록된 가격이 없습니다.</p>'
            name, public_id = PROFILES[self.biz]
            return (NAV + f'<h1 data-biz-name>{html.escape(name)}</h1>'
                    f'<button type="button" hidden data-log-click-params=\'{{"biz_id":"{self.biz}","lp_id":"{public_id}"}}\'>프로필 가기</button>'
                    f'<h2>가격 {len(data["prices"])}개</h2>{prices}'
                    f'<h2>소식 {len(data["posts"])}개</h2>{self._posts_html()}'
                    '<p class="muted" style="margin-top:32px"><a href="/logout">연습 계정 로그아웃</a></p>')

        # --- POST ---
        def do_POST(self):
            url = urlparse(self.path)
            path = url.path.rstrip("/")
            self.biz = self._cookie_biz()
            if not self._same_origin():
                return self._json({"error": "forbidden"}, 403)
            body = self._body()
            if body is None:
                return self._json({"error": "too large"}, 413)
            if path == "/login":
                return self._redirect(f"{MANAGER}/home",
                                      {"Set-Cookie": f"{COOKIE}; Path=/; HttpOnly; SameSite=Lax"})
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            if path == "/api/upload":
                return self._upload(body, parse_qs(url.query).get("name", ["image"])[0])
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if path == "/api/pricing":
                return self._save_prices(payload)
            if path == "/api/posts":
                return self._save_post(payload)
            if path == "/api/posts/delete":
                return self._delete_post(payload)
            if path == "/api/reset":
                with state.lock:
                    state.save({"prices": [], "posts": []}, self.biz)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

        def _upload(self, body: bytes, name: str):
            import hashlib
            import io

            from PIL import Image

            try:
                with Image.open(io.BytesIO(body)) as img:
                    fmt = img.format
                    img.verify()
            except Exception:
                return self._json({"error": "not an image"}, 400)
            ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(fmt)
            if not ext:
                return self._json({"error": "unsupported"}, 400)
            file_id = f"{hashlib.sha256(body).hexdigest()[:16]}.{ext}"
            (state.files / file_id).write_bytes(body)
            return self._json({"id": file_id, "name": name[:100]})

        def _save_prices(self, payload: dict):
            """가격 목록 전체를 저장한다(실제 화면처럼 「저장」은 목록을 통째로 바꾼다)."""
            items = payload.get("items")
            if not isinstance(items, list):
                return self._json({"error": "no items"}, 400)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            clean = []
            for item in items:
                name = str(item.get("name") or "").strip()[:15]
                price = item.get("price")
                image = item.get("image")
                if not name or not isinstance(price, int) or price <= 0:
                    return self._json({"error": "invalid item"}, 400)
                if image is not None and not (isinstance(image, str) and (state.files / image).is_file()):
                    return self._json({"error": "unknown image"}, 400)
                clean.append({"name": name, "price": price, "description": str(item.get("description") or "")[:50],
                              "image": image, "created_at": item.get("created_at") or now, "updated_at": now})
            with state.lock:
                data = state.load(self.biz)
                data["prices"] = clean
                state.save(data, self.biz)
            return self._json(data)

        def _delete_post(self, payload: dict):
            index = payload.get("index")
            with state.lock:
                data = state.load(self.biz)
                if not isinstance(index, int) or not 0 <= index < len(data["posts"]):
                    return self._json({"error": "no such post"}, 404)
                data["posts"].pop(index)
                state.save(data, self.biz)
            return self._json({"ok": True, "count": len(data["posts"])})

        def _save_post(self, payload: dict):
            title = str(payload.get("title") or "").strip()[:30]
            body = str(payload.get("body") or "").strip()[:5000]
            images = [i for i in payload.get("images") or [] if isinstance(i, str) and (state.files / i).is_file()][:10]
            if len(title) < 2 or len(body) < 10:
                return self._json({"error": "too short"}, 400)
            with state.lock:
                data = state.load(self.biz)
                post_id = secrets.token_hex(12)
                data["posts"].append({"id": post_id, "title": title, "body": body, "images": images,
                                      "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                state.save(data, self.biz)
            return self._json({"ok": True, "id": post_id, "count": len(data["posts"])})

    return Handler


class PracticeServer:
    def __init__(self, data_dir: Path, port: int):
        self.state = PracticeState(Path(data_dir) / "practice")
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "PracticeServer":
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(self.state))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="practice-site", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
