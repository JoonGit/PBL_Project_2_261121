"""계정 설정: 당근 계정 연결·해제 요청, AI 연결, 지침(프롬프트).

브라우저는 워커만 연다. 화면은 「연결해 달라」는 요청을 DB 에 남기고 결과를 읽기만 한다.
"""
from __future__ import annotations

from ..adapters.image_ai import CodexCliImageGenerator, OpenAIImageGenerator
from ..adapters.text_ai import AiError, ClaudeApiTextGenerator, ClaudeCliTextGenerator
from ..config import Settings
from ..db import execute, fetch_one, session_scope
from ..defaults import DEFAULT_PROMPTS, PROMPT_MAX_CHARS
from ..errors import DomainError
from ..repositories import core
from ..storage import SecretStore, mask
from ..util import new_id, utcnow
from . import readiness

PROVIDERS = ("CLAUDE", "GPT")
MODES = ("NONE", "API_KEY", "SUBSCRIPTION")


# --- 당근 계정 ---------------------------------------------------------------
def _has_active_jobs(session) -> bool:
    return fetch_one(session, "SELECT id FROM task_job WHERE status IN ('QUEUED','RUNNING') LIMIT 1") is not None


def _request_control(engine, action: str) -> dict:
    now = utcnow()
    with session_scope(engine) as s:
        conn = core.connection(s, lock=True)
        if conn["control_status"] in ("REQUESTED", "RUNNING"):
            raise DomainError("BUSY", "이미 브라우저 작업이 진행 중입니다. 잠시 뒤 다시 시도하세요.")
        if _has_active_jobs(s):
            raise DomainError("BUSY", "상품 등록이 진행 중입니다. 끝난 뒤에 다시 시도하세요.")
        token = new_id()
        status = "CONNECTING" if action == "CONNECT" else conn["status"]
        execute(s, "UPDATE channel_connection SET status=:st, control_action=:a, control_token=:t, control_status='REQUESTED',"
                   " control_message=NULL, control_requested_at=:now, control_updated_at=:now, updated_at=:now WHERE id=1",
                st=status, a=action, t=token, now=now)
        return {"token": token}


def request_connect(engine) -> dict:
    """「당근 계정 연결」: 워커에게 로그인 창을 띄워 달라고 한다."""
    return _request_control(engine, "CONNECT")


def request_disconnect(engine) -> dict:
    """「연결 해제」: 워커에게 저장된 로그인 정보를 지워 달라고 한다."""
    return _request_control(engine, "DISCONNECT")


def cancel_connect(engine) -> None:
    """로그인 대기를 그만둔다. 워커는 표식이 바뀐 것을 보고 창을 닫는다."""
    now = utcnow()
    with session_scope(engine) as s:
        conn = core.connection(s, lock=True)
        if conn["control_action"] == "CONNECT" and conn["control_status"] in ("REQUESTED", "RUNNING"):
            execute(s, "UPDATE channel_connection SET status=:st, control_status='FAILED', control_token=NULL,"
                       " control_message='연결을 취소했습니다.', control_updated_at=:now, updated_at=:now WHERE id=1",
                    st="EXPIRED" if conn["biz_id"] else "DISCONNECTED", now=now)


def sync_target(engine, target_code: str) -> bool:
    """설정의 등록 대상(실제/연습용)이 DB 의 연결 기록과 다르면 연결을 「연결 안 됨」으로 되돌린다.

    화면이 뜰 때와 워커가 뜰 때 모두 부른다. 화면이 먼저 맞춰 두므로, 사용자가 누른 연결 요청을
    뒤늦게 뜬 워커가 지워 버리는 일이 없다. 바꿨으면 True.
    """
    with session_scope(engine) as s:
        conn = core.connection(s, lock=True)
        if conn["target"] == target_code:
            return False
        execute(s, "UPDATE channel_connection SET target=:tg, status='DISCONNECTED', biz_id=NULL, biz_name=NULL, public_id=NULL,"
                   " connected_at=NULL, verified_at=NULL, control_action=NULL, control_token=NULL,"
                   " control_status=NULL, control_message=NULL, updated_at=:now WHERE id=1",
                tg=target_code, now=utcnow())
        return True


# --- AI 연결 -----------------------------------------------------------------
def save_ai(engine, secrets: SecretStore, provider: str, *, mode: str, api_key: str | None = None) -> None:
    """연결 방식을 저장한다. API 키는 DB 가 아니라 비밀 파일에 둔다. 빈 키는 「기존 키 유지」."""
    if provider not in PROVIDERS or mode not in MODES:
        raise DomainError("VALIDATION_ERROR", "연결 방식이 올바르지 않습니다.")
    secret_name = readiness.SECRET_NAME[provider]
    key = (api_key or "").strip()
    if mode == "API_KEY":
        if key:
            if len(key) < 20 or any(c.isspace() for c in key):
                raise DomainError("VALIDATION_ERROR", "API 키 형식이 올바르지 않습니다.")
            secrets.set(secret_name, key)
        elif not secrets.has(secret_name):
            raise DomainError("VALIDATION_ERROR", "API 키를 입력해 주세요.")
    with session_scope(engine) as s:
        execute(s, "UPDATE ai_connection SET mode=:m, status='UNKNOWN', detail=NULL, checked_at=NULL, updated_at=:now"
                   " WHERE provider=:p", m=mode, now=utcnow(), p=provider)


def delete_ai_key(engine, secrets: SecretStore, provider: str) -> None:
    if provider not in PROVIDERS:
        raise DomainError("VALIDATION_ERROR", "대상이 올바르지 않습니다.")
    secrets.delete(readiness.SECRET_NAME[provider])
    with session_scope(engine) as s:
        row = core.ai_connections(s)[provider]
        if row["mode"] == "API_KEY":
            execute(s, "UPDATE ai_connection SET mode='NONE', status='UNKNOWN', detail=NULL, checked_at=NULL,"
                       " updated_at=:now WHERE provider=:p", now=utcnow(), p=provider)


def _checker(provider: str, mode: str, secrets: SecretStore, settings: Settings):
    key = secrets.get(readiness.SECRET_NAME[provider])
    if mode == "API_KEY":
        if not key:
            raise AiError("AI_AUTH", "API 키가 저장되어 있지 않습니다.")
        return (ClaudeApiTextGenerator(key, settings.claude_api_model) if provider == "CLAUDE"
                else OpenAIImageGenerator(key, settings.openai_image_model))
    if mode == "SUBSCRIPTION":
        return ClaudeCliTextGenerator(settings.claude_cli_model) if provider == "CLAUDE" else CodexCliImageGenerator()
    raise AiError("AI_NOT_CONNECTED", "연결 방식을 먼저 고르세요.")


def check_ai(engine, secrets: SecretStore, settings: Settings, provider: str) -> dict:
    """저장된 방식으로 실제로 한 번 불러 본다. 결과는 화면에 보여 줄 한 줄로 남긴다."""
    if provider not in PROVIDERS:
        raise DomainError("VALIDATION_ERROR", "대상이 올바르지 않습니다.")
    with session_scope(engine) as s:
        mode = core.ai_connections(s)[provider]["mode"]
    try:
        detail, status = _checker(provider, mode, secrets, settings).check(), "OK"
    except AiError as exc:
        detail, status = exc.message, "ERROR"
    except Exception as exc:      # 명령줄 실행 시간 초과 등: 화면에 오류 창 대신 한 줄로 알린다
        detail, status = f"연결을 확인하지 못했습니다({type(exc).__name__}).", "ERROR"
    with session_scope(engine) as s:
        execute(s, "UPDATE ai_connection SET status=:st, detail=:d, checked_at=:now, updated_at=:now WHERE provider=:p",
                st=status, d=detail[:300], now=utcnow(), p=provider)
    return {"status": status, "detail": detail}


# --- 지침(프롬프트) -----------------------------------------------------------
def save_prompt(engine, kind: str, body: str) -> dict:
    """새 판으로 저장하고 바로 쓴다. 이미 접수된 작업은 접수할 때의 사본을 쓴다."""
    if kind not in DEFAULT_PROMPTS:
        raise DomainError("VALIDATION_ERROR", "지침 종류가 올바르지 않습니다.")
    body = (body or "").replace("\r\n", "\n").strip()
    if len(body) < 20:
        raise DomainError("VALIDATION_ERROR", "지침이 너무 짧습니다. 20자 이상 적어 주세요.")
    if len(body) > PROMPT_MAX_CHARS:
        raise DomainError("VALIDATION_ERROR", f"지침은 {PROMPT_MAX_CHARS}자까지입니다.")
    with session_scope(engine) as s:
        core.connection(s, lock=True)        # 저장이 겹치지 않게 한 줄로 세운다
        current = core.active_prompt(s, kind)
        if current["body"].strip() == body:
            return {"result": "UNCHANGED", "version_no": current["version_no"]}
        latest = fetch_one(s, "SELECT MAX(version_no) AS n FROM prompt_template WHERE kind = :k", k=kind)["n"]
        execute(s, "UPDATE prompt_template SET is_active = 0 WHERE kind = :k AND is_active = 1", k=kind)
        execute(s, "INSERT INTO prompt_template (id, kind, version_no, body, is_default, is_active, created_at)"
                   " VALUES (:id, :k, :v, :body, :d, 1, :now)",
                id=new_id(), k=kind, v=latest + 1, body=body, d=1 if body == DEFAULT_PROMPTS[kind].strip() else 0,
                now=utcnow())
        return {"result": "SAVED", "version_no": latest + 1}


def reset_prompt(engine, kind: str) -> dict:
    if kind not in DEFAULT_PROMPTS:
        raise DomainError("VALIDATION_ERROR", "지침 종류가 올바르지 않습니다.")
    return save_prompt(engine, kind, DEFAULT_PROMPTS[kind])


# --- 화면용 한 번에 읽기 --------------------------------------------------------
def overview(engine, secrets: SecretStore, settings: Settings) -> dict:
    with session_scope(engine) as s:
        conn = core.connection(s)
        ai = core.ai_connections(s)
        prompts = {kind: core.active_prompt(s, kind) for kind in DEFAULT_PROMPTS}
        env = readiness.environment(s, secrets, settings)
    providers = {}
    for provider in PROVIDERS:
        row = ai[provider]
        providers[provider] = {
            "mode": row["mode"], "status": row["status"], "detail": row["detail"], "checked_at": row["checked_at"],
            "key_hint": mask(secrets.get(readiness.SECRET_NAME[provider])),
            "cli_available": readiness.CLI_AVAILABLE[provider](),
            "ready": env.claude_ready if provider == "CLAUDE" else env.gpt_ready,
        }
    return {
        "connection": conn, "providers": providers, "env": env, "ai_mode": settings.ai_mode,
        "prompts": {k: {"body": v["body"], "version_no": v["version_no"],
                        "is_default": v["body"].strip() == DEFAULT_PROMPTS[k].strip()} for k, v in prompts.items()},
    }
