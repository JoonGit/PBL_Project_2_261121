"""이미지 생성(GPT).

- OpenAIImageGenerator: API 키로 OpenAI Images API 를 부른다.
- CodexCliImageGenerator: 이 컴퓨터에 로그인된 Codex(ChatGPT 구독)를 명령줄로 불러 이미지를 만든다.
- MockImageGenerator: 연결 없이 단색 안내 그림을 만든다(시험·오프라인 연습용).

모두 generate(guideline, name, info) → ImageResult(PNG 바이트) 로 같다.
만든 그림에는 앱이 작은 「AI 생성 이미지」 표시를 넣는다(실제 사진으로 오해하지 않게).
"""
from __future__ import annotations

import base64
import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import ai_cli_path

from .text_ai import AiError

AI_LABEL = "AI 생성 이미지"
FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/malgun.ttf",
]


@dataclass
class ImageResult:
    data: bytes
    model: str


def build_image_prompt(guideline: str, name: str, info: str | None) -> str:
    return f"{guideline.strip()}\n\n상품명: {name}\n상품 정보:\n{(info or '(적힌 정보 없음)').strip()[:1500]}"


def _font(size: int) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def add_ai_label(data: bytes) -> bytes:
    """오른쪽 아래에 작은 표시를 넣고 PNG 로 돌려준다."""
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    size = max(16, img.width // 48)
    font = _font(size)
    left, top, right, bottom = draw.textbbox((0, 0), AI_LABEL, font=font)
    pad = size // 2
    w, h = right - left + pad * 2, bottom - top + pad * 2
    x, y = img.width - w - size, img.height - h - size
    draw.rounded_rectangle((x, y, x + w, y + h), radius=pad, fill=(27, 27, 29, 170))
    draw.text((x + pad - left, y + pad - top), AI_LABEL, font=font, fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class MockImageGenerator:
    model = "mock-image"

    def generate(self, guideline: str, name: str, info: str | None) -> ImageResult:
        img = Image.new("RGB", (1536, 1024), (244, 244, 245))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((448, 232, 1088, 792), radius=48, fill=(255, 255, 255), outline=(225, 225, 228), width=4)
        font = _font(56)
        text = name[:18]
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        draw.text(((1536 - (right - left)) / 2 - left, (1024 - (bottom - top)) / 2 - top), text, font=font, fill=(27, 27, 29))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return ImageResult(buf.getvalue(), self.model)


class OpenAIImageGenerator:
    def __init__(self, api_key: str, model: str = "gpt-image-2.5-sunburst"):
        import openai  # 키 없이 쓰는 환경에서도 앱이 뜨도록 지연 import

        self._openai = openai
        self.client = openai.OpenAI(api_key=api_key, max_retries=1, timeout=180.0)
        self.model = model

    def _call(self, prompt: str, **extra):
        return self.client.images.generate(model=self.model, prompt=prompt, **extra)

    def generate(self, guideline: str, name: str, info: str | None) -> ImageResult:
        o = self._openai
        prompt = build_image_prompt(guideline, name, info)
        try:
            try:
                result = self._call(prompt, size="1536x1024")
            except o.BadRequestError:
                result = self._call(prompt)      # 모델이 이 크기를 받지 않으면 기본 크기로
        except o.AuthenticationError as exc:
            raise AiError("AI_AUTH", "OpenAI API 키가 올바르지 않습니다.") from exc
        except o.PermissionDeniedError as exc:
            raise AiError("AI_AUTH", "이 키로는 이미지 모델을 쓸 수 없습니다(조직 인증이 필요할 수 있습니다).") from exc
        except (o.RateLimitError, o.APIConnectionError) as exc:
            raise AiError("AI_TEMPORARY", f"OpenAI 일시 오류({type(exc).__name__})", transient=True) from exc
        except o.BadRequestError as exc:
            raise AiError("AI_BAD_REQUEST", f"OpenAI 요청 오류: {getattr(exc, 'message', exc)}"[:300]) from exc
        except o.APIStatusError as exc:
            raise AiError("AI_STATUS", f"OpenAI API 오류({exc.status_code})", transient=exc.status_code >= 500) from exc
        b64 = result.data[0].b64_json if result.data else None
        if not b64:
            raise AiError("AI_EMPTY", "OpenAI 응답에 이미지가 없습니다.")
        return ImageResult(base64.b64decode(b64), self.model)

    def check(self) -> str:
        o = self._openai
        try:
            self.client.models.retrieve(self.model)
        except o.AuthenticationError as exc:
            raise AiError("AI_AUTH", "OpenAI API 키가 올바르지 않습니다.") from exc
        except o.NotFoundError as exc:
            raise AiError("AI_BAD_REQUEST", f"이 키로는 {self.model} 모델을 찾을 수 없습니다.") from exc
        except o.APIError as exc:
            raise AiError("AI_STATUS", f"OpenAI 연결 확인 실패({type(exc).__name__})", transient=True) from exc
        return f"API 키 확인됨 · {self.model}"


class CodexCliImageGenerator:
    """Codex 명령줄을 한 번 실행해 이미지를 받는다. 로그인은 Codex 가 가진 것(ChatGPT 구독)을 쓴다."""

    model = "codex-cli"
    OUT_NAME = "out.png"

    def __init__(self, executable: str | None = None, timeout: int = 300):
        self.executable = executable or ai_cli_path("codex")
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return ai_cli_path("codex") is not None

    def _require(self) -> str:
        if not self.executable:
            raise AiError("AI_NOT_INSTALLED", "이 컴퓨터에서 Codex(codex 명령)를 찾지 못했습니다.")
        return self.executable

    def generate(self, guideline: str, name: str, info: str | None) -> ImageResult:
        exe = self._require()
        prompt = (
            "이미지 생성 도구로 아래 조건의 이미지를 정확히 한 장 만드세요. "
            f"만든 이미지 파일을 현재 폴더에 {self.OUT_NAME} 이름으로 저장하고, 저장한 경로만 답하세요. "
            "다른 파일은 만들거나 고치지 마세요.\n\n" + build_image_prompt(guideline, name, info)
        )
        with tempfile.TemporaryDirectory(prefix="fm-codex-") as tmp:
            try:
                proc = subprocess.run(
                    [exe, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "--ephemeral", "-C", tmp, prompt],
                    cwd=tmp, capture_output=True, encoding="utf-8", errors="replace", timeout=self.timeout, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired as exc:
                raise AiError("AI_TEMPORARY", "Codex 응답이 너무 오래 걸립니다.", transient=True) from exc
            out = Path(tmp) / self.OUT_NAME
            if not out.is_file():
                tail = (proc.stderr or proc.stdout or "").strip()[-200:]
                code = "AI_AUTH" if "login" in tail.lower() else "AI_STATUS"
                raise AiError(code, f"Codex 가 이미지를 만들지 못했습니다: {tail or proc.returncode}",
                              transient=code == "AI_STATUS")
            data = out.read_bytes()
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except Exception as exc:
            raise AiError("AI_SCHEMA", "Codex 가 저장한 파일이 이미지가 아닙니다.") from exc
        return ImageResult(data, self.model)

    def check(self) -> str:
        exe = self._require()
        proc = subprocess.run([exe, "login", "status"], capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                              stdin=subprocess.DEVNULL)
        text = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0 or "logged in" not in text.lower():
            raise AiError("AI_AUTH", "Codex 에 로그인되어 있지 않습니다. 터미널에서 `codex login` 을 먼저 실행하세요.")
        return "Codex 로그인 확인됨"
