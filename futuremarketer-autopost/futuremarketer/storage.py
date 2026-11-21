"""파일 보관.

- FileStore: 앱 데이터 폴더(엑셀 사본, 사진 사본, AI 이미지, 화면 캡처). 기본 위치는 프로젝트 밖 개인 폴더.
- SecretStore: 프로젝트 밖 개인 폴더의 API 키 파일(소유자만 읽기).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import DomainError
from .util import sha256_bytes


class FileStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve()
        for sub in ("uploads", "products", "generated", "shots", "logs"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise DomainError("STORAGE_ERROR", "저장 위치가 앱 데이터 폴더를 벗어납니다.", key=key)
        return path

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def read(self, key: str) -> bytes:
        path = self.path_for(key)
        if not path.is_file():
            raise DomainError("STORAGE_ERROR", "저장된 파일이 없습니다.", key=key)
        return path.read_bytes()

    def write(self, key: str, data: bytes) -> str:
        """임시 파일에 쓴 뒤 이름을 바꿔, 쓰다 만 파일이 남지 않게 한다."""
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return key

    def put_by_hash(self, folder: str, data: bytes, suffix: str) -> tuple[str, str]:
        """내용 해시를 파일 이름으로 쓴다. 같은 내용은 한 번만 저장된다."""
        digest = sha256_bytes(data)
        key = f"{folder}/{digest}{suffix}"
        if not self.exists(key):
            self.write(key, data)
        return key, digest


class SecretStore:
    """API 키 보관. 파일은 프로젝트 밖에 있고 소유자만 읽을 수 있다. DB·로그에는 넣지 않는다."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, name: str) -> str | None:
        return self._load().get(name) or None

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._save(data)

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp)
        os.replace(tmp, self.path)


def mask(value: str | None) -> str:
    """화면에 보여 줄 때는 앞뒤 몇 글자만."""
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 14 else "설정됨"
