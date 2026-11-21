"""로컬 JSON 저장소. SQL 조회와 제약 검사는 트랜잭션 동안 메모리에서만 실행한다."""
from __future__ import annotations

import contextlib
import errno
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Iterator

from .errors import DomainError
from .filelock import lock_file

FORMAT_VERSION = 1
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "local_schema.sql"
TABLES = ("channel_connection", "ai_connection", "prompt_template", "import_batch",
          "product", "product_image", "product_registration", "task_job", "job_event")
JSON_COLUMNS = {"data_issues_json", "steps_json", "request_json", "result_json", "profiles_json"}


class LocalSchemaOutdated(Exception):
    pass


class JsonDatabase:
    def __init__(self, path: Path):
        self.path = path

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+b") as lock:
            deadline = time.monotonic() + 15
            while True:
                try:
                    lock_file(lock)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise DomainError("LOCAL_STORAGE_BUSY", "다른 작업이 로컬 기록을 저장 중입니다. 잠시 뒤 다시 시도하세요.") from exc
                    time.sleep(0.04)
            yield

    @contextlib.contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        # 화면과 워커는 별도 프로세스다. 읽기부터 교체까지 잠가 같은 상태를 기준으로 실행한다.
        with self._locked(), contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.create_function("JSON_UNQUOTE", 1, lambda value: None if value is None else str(value))
            connection.executescript(SCHEMA.read_text(encoding="utf-8"))
            columns = {table: [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]
                       for table in TABLES}
            self._restore(connection, columns)
            before = connection.total_changes
            with connection:
                yield connection
            if connection.total_changes != before:
                self._save(connection, columns)

    def _restore(self, connection: sqlite3.Connection, columns: dict[str, list[str]]) -> None:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, UnicodeError) as exc:
            raise DomainError("LOCAL_STORAGE_INVALID", "로컬 JSON을 읽을 수 없습니다. 기존 파일을 백업한 뒤 확인하세요.") from exc
        if not isinstance(state, dict) or state.get("version") != FORMAT_VERSION:
            raise LocalSchemaOutdated()
        try:
            tables = state["tables"]
            for name, fields in columns.items():
                rows = tables[name]
                if not isinstance(rows, list):
                    raise ValueError(name)
                values = ([json.dumps(row[k], ensure_ascii=False) if k in JSON_COLUMNS and row[k] is not None
                           else row[k] for k in fields] for row in rows)
                connection.executemany(f"INSERT INTO {name} ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                                       values)
            sequence = state.get("event_sequence", 0)
            if type(sequence) is not int or sequence < 0:
                raise ValueError("event_sequence")
            updated = connection.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='job_event'", (sequence,))
            if not updated.rowcount:
                connection.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('job_event', ?)", (sequence,))
            connection.commit()
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            raise DomainError("LOCAL_STORAGE_INVALID", "로컬 JSON의 저장 구조를 확인할 수 없습니다. 기존 파일을 백업한 뒤 확인하세요.") from exc

    def _save(self, connection: sqlite3.Connection, columns: dict[str, list[str]]) -> None:
        tables = {}
        for name, fields in columns.items():
            tables[name] = [
                {k: json.loads(value) if k in JSON_COLUMNS and value is not None else value
                 for k, value in dict(row).items()}
                for row in connection.execute(f"SELECT {', '.join(fields)} FROM {name}")
            ]
        sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='job_event'").fetchone()
        state = {"version": FORMAT_VERSION, "event_sequence": sequence[0] if sequence else 0, "tables": tables}
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".app-state-", suffix=".tmp", delete=False) as handle:
                pending = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self.path)
        finally:
            if pending:
                pending.unlink(missing_ok=True)

    def reset(self) -> None:
        with self._locked():
            self.path.unlink(missing_ok=True)
