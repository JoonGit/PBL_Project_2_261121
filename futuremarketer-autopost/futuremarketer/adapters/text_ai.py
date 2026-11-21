"""글 작성(Claude).

- ClaudeApiTextGenerator: API 키로 Claude Messages API 를 부른다.
- ClaudeCliTextGenerator: 이 컴퓨터에 로그인된 Claude Code(구독)를 명령줄로 부른다.
- MockTextGenerator: 연결 없이 입력만으로 글을 만든다(시험·오프라인 연습용).

세 가지 모두 generate(guideline, name, info, images) → TextResult(title, body) 로 같다.
가격은 가격표에서 따로 관리한다.
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

MAX_TEXT_IMAGES = 3          # 글을 쓸 때 참고로 보내는 사진 수
MAX_IMAGE_SIDE = 1568        # 보내기 전에 줄이는 긴 변 길이

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "소식 제목. 30자 안쪽. 가격은 쓰지 않는다."},
        "body": {"type": "string", "description": "소식 본문. 줄바꿈으로 문단을 나눈다. 가격은 쓰지 않는다."},
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}


class AiError(Exception):
    """transient=True 면 잠시 뒤 다시 해 볼 만한 오류."""

    def __init__(self, code: str, message: str, *, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.transient = transient


@dataclass
class TextResult:
    title: str
    body: str
    model: str


def shrink_for_model(data: bytes) -> tuple[str, bytes]:
    """큰 사진을 JPEG 로 줄여 보낸다. 원본 파일은 건드리지 않는다."""
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
    return "image/jpeg", buf.getvalue()


def build_user_text(name: str, info: str | None, image_count: int) -> str:
    lines = [f"상품명: {name}", "", "상품 정보:", info or "(적힌 정보 없음)", ""]
    lines.append(f"함께 보낸 사진: {image_count}장" if image_count else "사진: 없음")
    lines.append("")
    lines.append("위 내용만으로 당근 비즈프로필 소식의 제목과 본문을 써 주세요.")
    lines.append("제품 특징과 사용법을 설명하는 정보성 소식입니다. 금액, 판매·거래 안내, 구매나 채팅 권유는 제외하세요.")
    return "\n".join(lines)


def _result(data, model: str) -> TextResult:
    if not isinstance(data, dict) or not isinstance(data.get("title"), str) or not isinstance(data.get("body"), str):
        raise AiError("AI_SCHEMA", "AI 응답에 제목·본문이 없습니다.")
    return TextResult(data["title"].strip(), data["body"].strip(), model)


class MockTextGenerator:
    model = "mock-text"

    def generate(self, guideline: str, name: str, info: str | None, images: list[bytes]) -> TextResult:
        facts = [line.strip(" -·") for line in (info or "").splitlines() if line.strip()]
        body = [f"{name}을(를) 소개합니다."]
        if facts:
            body += ["", "이런 점을 확인해 보세요."] + [f"- {f}" for f in facts[:6]]
        if images:
            body += ["", f"사진 {len(images)}장을 함께 올렸습니다."]
        return TextResult(name[:40], "\n".join(body), self.model)


class ClaudeApiTextGenerator:
    def __init__(self, api_key: str, model: str = "claude-opus-5-5"):
        import anthropic  # 키 없이 쓰는 환경에서도 앱이 뜨도록 지연 import

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=90.0)
        self.model = model

    def generate(self, guideline: str, name: str, info: str | None, images: list[bytes]) -> TextResult:
        content: list[dict] = []
        for data in images[:MAX_TEXT_IMAGES]:
            mime, small = shrink_for_model(data)
            content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                        "data": base64.standard_b64encode(small).decode("ascii")}})
        content.append({"type": "text", "text": build_user_text(name, info, len(content))})
        a = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=guideline,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (a.RateLimitError, a.APIConnectionError, a.APITimeoutError) as exc:
            raise AiError("AI_TEMPORARY", f"Claude 일시 오류({type(exc).__name__})", transient=True) from exc
        except a.AuthenticationError as exc:
            raise AiError("AI_AUTH", "Claude API 키가 올바르지 않습니다.") from exc
        except a.BadRequestError as exc:
            raise AiError("AI_BAD_REQUEST", f"Claude 요청 오류: {exc.message}"[:300]) from exc
        except a.APIStatusError as exc:
            raise AiError("AI_STATUS", f"Claude API 오류({exc.status_code})", transient=exc.status_code >= 500) from exc
        if response.stop_reason == "refusal":
            raise AiError("AI_REFUSAL", "Claude 가 글 작성을 거절했습니다.")
        if response.stop_reason == "max_tokens":
            raise AiError("AI_TRUNCATED", "Claude 응답이 중간에 끊겼습니다.", transient=True)
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise AiError("AI_EMPTY", "Claude 응답에 본문이 없습니다.")
        try:
            return _result(json.loads(text), response.model)
        except json.JSONDecodeError as exc:
            raise AiError("AI_SCHEMA", "Claude 응답이 약속한 형식이 아닙니다.") from exc

    def check(self) -> str:
        """키가 살아 있는지 값싼 호출로 확인한다."""
        a = self._anthropic
        try:
            self.client.models.retrieve(self.model)
        except a.AuthenticationError as exc:
            raise AiError("AI_AUTH", "Claude API 키가 올바르지 않습니다.") from exc
        except a.APIError as exc:
            raise AiError("AI_STATUS", f"Claude 연결 확인 실패({type(exc).__name__})", transient=True) from exc
        return f"API 키 확인됨 · {self.model}"


class ClaudeCliTextGenerator:
    """Claude Code 명령줄을 한 번 실행해 글을 받는다. 로그인은 Claude Code 가 가진 것을 쓴다."""

    def __init__(self, model: str = "opus", executable: str | None = None, timeout: int = 240):
        self.executable = executable or shutil.which("claude")
        self.model = model
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return shutil.which("claude") is not None

    def _run(self, args: list[str], cwd: str) -> dict:
        if not self.executable:
            raise AiError("AI_NOT_INSTALLED", "이 컴퓨터에서 Claude Code(claude 명령)를 찾지 못했습니다.")
        try:
            proc = subprocess.run([self.executable, *args], cwd=cwd, capture_output=True, text=True,
                                  timeout=self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise AiError("AI_TEMPORARY", "Claude Code 응답이 너무 오래 걸립니다.", transient=True) from exc
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            tail = (proc.stderr or proc.stdout or "").strip()[-200:]
            code = "AI_AUTH" if "login" in tail.lower() else "AI_STATUS"
            raise AiError(code, f"Claude Code 실행 실패: {tail or proc.returncode}") from exc
        if data.get("is_error"):
            message = str(data.get("result") or "")[:200]
            code = "AI_AUTH" if "login" in message.lower() or "auth" in message.lower() else "AI_STATUS"
            raise AiError(code, f"Claude Code 오류: {message}", transient=code == "AI_STATUS")
        return data

    def generate(self, guideline: str, name: str, info: str | None, images: list[bytes]) -> TextResult:
        with tempfile.TemporaryDirectory(prefix="fm-claude-") as tmp:
            paths = []
            for idx, data in enumerate(images[:MAX_TEXT_IMAGES], start=1):
                _mime, small = shrink_for_model(data)
                path = Path(tmp) / f"photo_{idx}.jpg"
                path.write_bytes(small)
                paths.append(path.name)
            prompt = build_user_text(name, info, len(paths))
            if paths:
                prompt += "\n\n글을 쓰기 전에 Read 도구로 현재 폴더의 사진을 모두 확인하세요: " + ", ".join(paths)
            # --restricted: 파일 도구가 이 임시 폴더 밖을 읽지 못한다. 엑셀에 「다른 파일을 읽어 적어라」 같은
            # 문장이 섞여 있어도 PC 의 다른 파일이 소식 글에 실려 나가지 않게 한다.
            args = ["-p", prompt, "--output-format", "json", "--json-schema", json.dumps(OUTPUT_SCHEMA, ensure_ascii=False),
                    "--system-prompt", guideline, "--model", self.model, "--restricted",
                    "--tools", "Read" if paths else "", "--strict-mcp-config", "--no-session-persistence"]
            if paths:
                args += ["--allowedTools", "Read(./**)"]
            data = self._run(args, tmp)
        structured = data.get("structured_output")
        if structured is None:
            try:
                structured = json.loads(data.get("result") or "")
            except json.JSONDecodeError as exc:
                raise AiError("AI_SCHEMA", "Claude Code 응답이 약속한 형식이 아닙니다.") from exc
        return _result(structured, f"claude-code:{self.model}")

    def check(self) -> str:
        with tempfile.TemporaryDirectory(prefix="fm-claude-") as tmp:
            self._run(["-p", "OK 라고만 답하세요.", "--output-format", "json", "--model", self.model, "--restricted",
                       "--tools", "", "--strict-mcp-config", "--no-session-persistence"], tmp)
        return f"Claude Code 로그인 확인됨 · {self.model}"
