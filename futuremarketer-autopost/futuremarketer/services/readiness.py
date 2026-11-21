"""유효성 검사: 이 상품을 지금 등록할 수 있는가.

파일 내용 문제(불러올 때 기록)와 지금의 연결 상태를 함께 본다.
- 당근 계정이 연결되어 있어야 한다.
- 대본이 없으면 글을 쓸 Claude 연결이 있어야 한다.
- 이미지가 없으면 그림을 만들 GPT 연결이 있어야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..adapters.image_ai import CodexCliImageGenerator
from ..adapters.text_ai import ClaudeCliTextGenerator
from ..config import Settings
from ..repositories import core
from ..storage import SecretStore

SECRET_NAME = {"CLAUDE": "anthropic_api_key", "GPT": "openai_api_key"}
CLI_AVAILABLE = {"CLAUDE": ClaudeCliTextGenerator.available, "GPT": CodexCliImageGenerator.available}


@dataclass(frozen=True)
class Env:
    daangn_connected: bool
    claude_ready: bool
    gpt_ready: bool


def ai_ready(row: dict | None, provider: str, secrets: SecretStore, settings: Settings) -> bool:
    if settings.ai_mode == "mock":
        return True
    mode = (row or {}).get("mode") or "NONE"
    if mode == "API_KEY":
        return secrets.has(SECRET_NAME[provider])
    if mode == "SUBSCRIPTION":
        return CLI_AVAILABLE[provider]()
    return False


def environment(session: Session, secrets: SecretStore, settings: Settings) -> Env:
    conn = core.connection(session)
    ai = core.ai_connections(session)
    return Env(daangn_connected=conn["status"] == "CONNECTED" and bool(conn["biz_id"]),
               claude_ready=ai_ready(ai.get("CLAUDE"), "CLAUDE", secrets, settings),
               gpt_ready=ai_ready(ai.get("GPT"), "GPT", secrets, settings))


def connected_place(conn: dict) -> tuple[str, str] | None:
    """지금 연결된 곳(대상, 비즈프로필). 연결돼 있지 않으면 None."""
    if conn["status"] == "CONNECTED" and conn["biz_id"]:
        return conn["target"], conn["biz_id"]
    return None


def known_place(conn: dict) -> tuple[str, str] | None:
    """연결 기록이 가리키는 곳. 로그인이 풀려 「다시 연결 필요」여도 어디였는지는 안다."""
    return (conn["target"], conn["biz_id"]) if conn["biz_id"] else None


def place_status(product: dict, conn: dict, registration: dict | None) -> str:
    """지금 연결된 곳 기준의 등록 상태.

    - 진행 중인 작업(대기·등록 중·확인 필요)은 어디 것이든 그대로 보여 준다(상품당 하나뿐이다).
    - 연결 기록이 가리키는 곳이 있으면 그곳의 등록 기록을 본다. 연습용에 올린 기록은 실제 당근의 「등록 완료」가 아니다.
    - 연결한 적이 없으면(해제했으면) 어디 기록인지 가릴 수 없으니 가장 최근 상태를 보여 준다.
    registration 은 known_place(conn) 의 기록이어야 한다.
    """
    if product["reg_status"] in ("QUEUED", "RUNNING", "NEEDS_CHECK"):
        return product["reg_status"]
    if known_place(conn):
        return registration["status"] if registration else "NONE"
    return product["reg_status"]


def changed_after_registration(product: dict, registration: dict | None) -> bool:
    """이곳에 등록한 뒤에 엑셀 내용이 바뀌었는가."""
    return bool(registration and registration["status"] == "DONE" and registration["fingerprint"]
                and registration["fingerprint"] != product["source_fingerprint"])


def blockers(product: dict, local_image_count: int, env: Env) -> list[str]:
    """등록을 막는 이유 목록. 비어 있으면 등록 가능."""
    reasons = list(product.get("data_issues_json") or [])
    if not env.daangn_connected:
        reasons.append("당근 계정이 연결되어 있지 않습니다.")
    if not product.get("script") and not env.claude_ready:
        reasons.append("대본이 없어 글을 쓸 Claude 연결이 필요합니다.")
    if local_image_count == 0 and not env.gpt_ready:
        reasons.append("이미지가 없어 그림을 만들 GPT 연결이 필요합니다.")
    return reasons
