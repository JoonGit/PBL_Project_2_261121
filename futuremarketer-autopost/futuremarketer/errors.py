"""서비스가 화면에 돌려주는 업무 오류. code 로 구분하고, 화면은 message 를 그대로 보여 준다."""
from __future__ import annotations


class DomainError(Exception):
    """code: VALIDATION_ERROR, CONFLICT, BUSY, NOT_FOUND, NOT_READY, NOT_ALLOWED, STORAGE_ERROR"""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"DomainError({self.code!r}, {self.message!r})"
