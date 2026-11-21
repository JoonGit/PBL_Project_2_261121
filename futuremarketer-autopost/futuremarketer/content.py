"""소식에 올릴 제목·본문을 정하는 규칙(순수 함수).

- 대본이 있으면 한 글자도 바꾸지 않고 본문으로 쓴다.
- AI 가 쓴 글에는 가격을 맡기지 않는다. 가격 줄은 앱이 엑셀의 값으로 붙인다.
- AI 글에 입력에 없던 금액이 섞여 있으면 등록하지 않는다.
"""
from __future__ import annotations

import re

TITLE_MAX = 30          # 당근 비즈프로필 소식 제목 칸은 30자까지 받는다
BODY_MAX = 3000
UNIT = {"천": 1_000, "만": 10_000, "천만": 10_000_000, "억": 100_000_000}
NUMBER = r"\d[\d,]*"
# 「950,000원」「500,000 원」「190만 원」「1만5천원」「1억 5천만 원」처럼 숫자(와 단위)가 이어지다 「원」으로 끝나는 표현.
# 「원형·원목·원단」처럼 돈이 아닌 낱말의 「원」은 뺀다.
# 앞쪽 숫자는 단위가 붙어 있을 때만 이어 읽는다(「1만 5천」). 「Px8 950,000원」의 8 은 금액이 아니다.
AMOUNT_RE = re.compile(rf"((?:{NUMBER}\s*(?:천만|만|천|억)\s*)*{NUMBER}\s*(?:천만|만|천|억)?\s*)원(?![형목단산료색피룸석두래조본칙통])")
# 「180~190만 원대」: 앞 숫자에도 뒤의 단위를 적용한다.
RANGE_RE = re.compile(rf"({NUMBER})\s*[~\-–]\s*({NUMBER})\s*(천만|만|천|억)?\s*원")
PART_RE = re.compile(rf"({NUMBER})\s*(천만|만|천|억)?")


def fit_text(text: str | None, limit: int) -> str:
    """글자 수 제한이 있는 칸에 넣을 글. 줄여야 하면 낱말 중간이 아니라 띄어쓰기에서 끊는다.

    「크롬하츠 부바(BUBBA) 블랙/실버 안경」 → 15자: 「크롬하츠 부바(BUBBA)」
    끝에 짝이 맞지 않는 여는 괄호가 남으면 그 앞에서 끊는다.
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    window = text[:limit + 1]                 # 바로 뒤 글자가 띄어쓰기면 낱말이 거기서 끝난 것
    space = window.rfind(" ")
    head = window[:space] if space >= limit // 2 else text[:limit]
    for opening, closing in (("(", ")"), ("[", "]")):
        if head.count(opening) > head.count(closing):
            head = head[:head.rfind(opening)]
    return head.rstrip(" -–·,/") or text[:limit]


def format_price(price: int) -> str:
    return f"{int(price):,}원"


def post_from_script(name: str, post_title: str | None, script: str) -> tuple[str, str]:
    """대본 그대로. 제목은 소식제목 칸이 있으면 그것, 없으면 상품명(제목 칸 길이에 맞춘다)."""
    return fit_text(post_title or name, TITLE_MAX), script.strip()


def _number(digits: str) -> int:
    return int(digits.replace(",", ""))


def _amounts(text: str | None) -> set[int]:
    """글에 나온 금액들(원 단위)."""
    text = text or ""
    found = set()
    for low, high, unit in RANGE_RE.findall(text):
        scale = UNIT.get(unit, 1)
        found.update({_number(low) * scale, _number(high) * scale})
    for match in AMOUNT_RE.finditer(text):
        parts = PART_RE.findall(match.group(1))
        plain_with_space = len(parts) == 1 and not parts[0][1] and match.group(1) != match.group(1).rstrip()
        if plain_with_space and _number(parts[0][0]) < 100:
            continue                    # 「지름 30 원형」처럼 작은 숫자 뒤에 띄어 쓴 「원」은 돈으로 보지 않는다
        found.add(sum(_number(digits) * UNIT.get(unit, 1) for digits, unit in parts))
    return found


def foreign_amounts(body: str, *, price: int, info: str | None, name: str) -> list[int]:
    """본문에 나온 금액 가운데 상품 가격도 아니고 입력에도 없던 것."""
    allowed = {price} | _amounts(info) | _amounts(name)
    return sorted(a for a in _amounts(body) if a not in allowed)


def post_from_ai(name: str, price: int, info: str | None, post_title: str | None,
                 ai_title: str, ai_body: str) -> tuple[str, str]:
    """AI 결과를 검사하고 가격 줄을 붙인다. 문제가 있으면 ValueError(사람이 읽을 이유)."""
    title = (post_title or ai_title or "").strip()
    body = (ai_body or "").strip()
    if not title:
        raise ValueError("AI 가 제목을 만들지 못했습니다.")
    if not body:
        raise ValueError("AI 가 본문을 만들지 못했습니다.")
    title = fit_text(title, TITLE_MAX)
    wrong = foreign_amounts(f"{title}\n{body}", price=price, info=info, name=name)
    if wrong:
        shown = ", ".join(format_price(a) for a in wrong[:3])
        raise ValueError(f"AI 글에 입력에 없던 금액이 있습니다: {shown}")
    body = f"{body}\n\n판매가격: {format_price(price)}"
    if len(body) > BODY_MAX:
        raise ValueError(f"AI 가 쓴 본문이 너무 깁니다({len(body)}자).")
    return title, body
