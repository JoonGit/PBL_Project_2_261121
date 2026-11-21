"""공통 도우미: UUID, 해시, 고정 JSON 직렬화, UTC 시각."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """DB 에 저장하는 시각. DATETIME(6) 은 시간대를 담지 않으므로 UTC naive 로 통일한다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value) -> str:
    """키 정렬 + UTF-8 + 공백 없는 직렬화. payload_hash·지문 계산의 고정 규칙."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)
