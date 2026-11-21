"""프로세스가 종료되거나 핸들이 닫히면 운영체제가 해제하는 파일 잠금."""
from __future__ import annotations

import os
from typing import BinaryIO


def lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
