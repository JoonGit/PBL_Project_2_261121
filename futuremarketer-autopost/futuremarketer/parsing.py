"""엑셀 읽기와 행 검사(순수 함수). DB 를 건드리지 않는다.

엑셀 형식 — 첫 시트, 첫 행이 머리글:
    상품코드 | 상품명 | 가격 | 상품정보 | 소식제목 | 대본 | 이미지
- 상품코드·상품명·가격은 꼭 있어야 한다.
- 대본이 있으면 그대로 소식 본문으로 쓴다. 비어 있으면 AI 가 상품정보와 사진으로 글을 쓴다.
- 이미지는 이미지 폴더 안의 파일 이름을 쉼표로 구분해 적는다. 비어 있으면 AI 가 이미지를 만든다.
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image, UnidentifiedImageError

from .errors import DomainError
from .util import payload_hash, sha256_bytes

HEADERS = {"상품코드": "code", "상품명": "name", "가격": "price", "상품정보": "info",
           "소식제목": "post_title", "대본": "script", "이미지": "images"}
REQUIRED_HEADERS = ("상품코드", "상품명", "가격")
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
IMAGE_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_PRICE = 2_000_000_000
MAX_SCRIPT_CHARS = 5000
MAX_INFO_CHARS = 5000


@dataclass
class ImageRef:
    name: str
    path: Path
    sha256: str
    mime: str
    width: int
    height: int


@dataclass
class ParsedRow:
    row_no: int
    code: str
    name: str
    price: int | None
    info: str | None
    post_title: str | None
    script: str | None
    image_names: list[str]
    images: list[ImageRef] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """행 내용이 바뀌었는지 가리는 지문. 사진은 이름이 아니라 내용 해시로 본다."""
        return payload_hash({"name": self.name, "price": self.price, "info": self.info,
                             "post_title": self.post_title, "script": self.script,
                             "image_names": self.image_names, "images": [i.sha256 for i in self.images],
                             "issues": self.issues})


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def parse_price(value) -> tuple[int | None, str | None]:
    """'950,000원', '950000', 950000.0 → 950000. 고칠 수 없으면 (None, 이유)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "가격이 비어 있습니다."
    if isinstance(value, bool):
        return None, "가격 형식이 올바르지 않습니다."
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return None, "가격은 원 단위 정수여야 합니다."
        number = int(value)
    else:
        cleaned = re.sub(r"[,\s]", "", str(value))
        cleaned = cleaned[:-1] if cleaned.endswith("원") else cleaned
        if not re.fullmatch(r"\d+", cleaned):
            return None, f"가격을 숫자로 읽을 수 없습니다: {str(value).strip()[:30]}"
        number = int(cleaned)
    if number <= 0:
        return None, "가격은 1원 이상이어야 합니다."
    if number > MAX_PRICE:
        return None, "가격이 너무 큽니다."
    return number, None


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def split_image_names(value: str | None) -> list[str]:
    if not value:
        return []
    return [_nfc(n.strip()) for n in re.split(r"[,\n;]", value) if n.strip()]


def inspect_image(path: Path, max_bytes: int) -> tuple[ImageRef | None, str | None]:
    """실제로 열리는 JPEG/PNG/WEBP 인지 확인한다. 확장자는 믿지 않는다."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, f"이미지를 읽을 수 없습니다: {path.name}"
    if len(data) > max_bytes:
        return None, f"이미지가 너무 큽니다({len(data) // (1024 * 1024)}MB): {path.name}"
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            fmt, (width, height) = img.format, img.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None, f"이미지 파일이 아니거나 손상됐습니다: {path.name}"
    if fmt not in IMAGE_MIME:
        return None, f"JPG·PNG·WEBP 만 쓸 수 있습니다: {path.name}"
    return ImageRef(path.name, path, sha256_bytes(data), IMAGE_MIME[fmt], width, height), None


def _resolve_images(names: list[str], image_dir: Path, max_images: int, max_bytes: int) -> tuple[list[ImageRef], list[str]]:
    images, issues = [], []
    if len(names) > max_images:
        issues.append(f"이미지는 최대 {max_images}장입니다(지금 {len(names)}장).")
        names = names[:max_images]
    root = image_dir.resolve()
    # 한글 파일 이름은 저장 방식(자모 결합/분리)이 달라도 같은 이름으로 본다
    on_disk = {_nfc(p.name): p for p in root.iterdir()} if root.is_dir() else {}
    seen = set()
    for name in names:
        if name in seen:
            issues.append(f"같은 이미지가 두 번 적혀 있습니다: {name}")
            continue
        seen.add(name)
        if "/" in name or "\\" in name or name.startswith("."):
            issues.append(f"이미지는 파일 이름만 적습니다: {name}")
            continue
        path = on_disk.get(name)
        if path is None or path.is_symlink() or not path.is_file():
            issues.append(f"이미지 폴더에 파일이 없습니다: {name}")
            continue
        ref, problem = inspect_image(path, max_bytes)
        if problem:
            issues.append(problem)
        else:
            images.append(ref)
    return images, issues


def read_products(data: bytes, image_dir: Path, *, max_rows: int = 100, max_images: int = 10,
                  max_image_bytes: int = 20 * 1024 * 1024) -> list[ParsedRow]:
    """엑셀을 읽어 행 목록을 돌려준다. 파일 자체를 읽을 수 없으면 DomainError."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:  # openpyxl 은 손상 파일에 여러 예외를 던진다
        raise DomainError("VALIDATION_ERROR", "엑셀(.xlsx) 파일을 열 수 없습니다.") from exc
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        raise DomainError("VALIDATION_ERROR", "엑셀이 비어 있습니다.")
    columns: dict[str, int] = {}
    for idx, cell in enumerate(header):
        label = (_text(cell) or "").replace(" ", "")
        if label in HEADERS and HEADERS[label] not in columns:
            columns[HEADERS[label]] = idx
    missing = [h for h in REQUIRED_HEADERS if HEADERS[h] not in columns]
    if missing:
        raise DomainError("VALIDATION_ERROR", f"머리글이 없습니다: {', '.join(missing)}",
                          hint="첫 행에 상품코드, 상품명, 가격, 상품정보, 소식제목, 대본, 이미지를 적어 주세요.")

    def cell(row, key):
        idx = columns.get(key)
        return row[idx] if idx is not None and idx < len(row) else None

    parsed: list[ParsedRow] = []
    for row_no, row in enumerate(rows, start=2):
        if row is None or all(_text(v) is None for v in row):
            continue
        if len(parsed) >= max_rows:
            raise DomainError("VALIDATION_ERROR", f"한 번에 {max_rows}개까지 불러올 수 있습니다.")
        issues: list[str] = []
        if any(isinstance(v, str) and v.startswith("=") for v in row):
            issues.append("수식 셀은 쓸 수 없습니다. 값으로 바꿔 주세요.")
        raw_code = cell(row, "code")
        code = _text(raw_code)
        if code is None:
            issues.append("상품코드가 비어 있습니다.")
            code = f"ROW-{row_no}"
        elif not CODE_RE.match(code):
            issues.append("상품코드는 영문·숫자·-·_ 로 40자까지 씁니다.")
            code = re.sub(r"[^A-Za-z0-9_-]", "", code)[:32] or f"ROW-{row_no}"
        name = _text(cell(row, "name")) or ""
        if not name:
            issues.append("상품명이 비어 있습니다.")
        elif len(name) > 100:
            issues.append("상품명은 100자까지입니다.")
            name = name[:100]
        price, price_problem = parse_price(cell(row, "price"))
        if price_problem:
            issues.append(price_problem)
        info = _text(cell(row, "info"))
        if info and len(info) > MAX_INFO_CHARS:
            issues.append(f"상품정보는 {MAX_INFO_CHARS}자까지입니다.")
        post_title = _text(cell(row, "post_title"))
        if post_title and len(post_title) > 100:
            issues.append("소식제목은 100자까지입니다.")
            post_title = post_title[:100]
        script = _text(cell(row, "script"))
        if script and len(script) > MAX_SCRIPT_CHARS:
            issues.append(f"대본은 {MAX_SCRIPT_CHARS}자까지입니다.")
        names = split_image_names(_text(cell(row, "images")))
        images, image_issues = _resolve_images(names, image_dir, max_images, max_image_bytes)
        issues.extend(image_issues)
        if not script and not info and not names:
            issues.append("대본이 없으면 AI 가 쓸 상품정보나 이미지가 있어야 합니다.")
        parsed.append(ParsedRow(row_no, code, name, price, info, post_title, script, names, images, issues))

    counts: dict[str, int] = {}
    for item in parsed:
        counts[item.code] = counts.get(item.code, 0) + 1
    seen: dict[str, int] = {}
    for item in parsed:
        if counts[item.code] > 1:
            item.issues.append(f"상품코드가 겹칩니다: {item.code}")
            seen[item.code] = seen.get(item.code, 0) + 1
            if seen[item.code] > 1:      # 저장 키가 겹치지 않게 뒤 행에는 행 번호를 붙인다
                item.code = f"{item.code[:30]}-R{item.row_no}"
    if not parsed:
        raise DomainError("VALIDATION_ERROR", "불러올 상품 행이 없습니다.")
    return parsed
