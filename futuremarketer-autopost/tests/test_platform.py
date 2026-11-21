from __future__ import annotations

from futuremarketer import config
from futuremarketer.worker import acquire_lock, worker_running


def test_windows_aside_installation_is_discovered(tmp_path, monkeypatch):
    local_app_data = tmp_path / "Local AppData"
    executable = local_app_data / "Aside" / "Application" / "aside.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.delenv("FM_ASIDE_PATH", raising=False)
    monkeypatch.delenv("FM_PRIVATE_DIR", raising=False)
    monkeypatch.delenv("FM_DATA_DIR", raising=False)
    settings = config.load_settings()
    assert settings.aside_path == executable
    assert settings.private_dir == local_app_data / config.APP_DIR_NAME


def test_worker_lock_is_exclusive_and_releases_when_closed(tmp_path):
    assert not worker_running(tmp_path)
    lock = acquire_lock(tmp_path)
    assert lock is not None
    with lock:
        assert worker_running(tmp_path)
        assert acquire_lock(tmp_path) is None
    assert not worker_running(tmp_path)
    with acquire_lock(tmp_path):
        assert worker_running(tmp_path)


def test_windows_ai_cli_uses_native_executable(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(config.shutil, "which", lambda command: calls.append(command) or "Claude.exe")
    assert config.ai_cli_path("claude") == "Claude.exe"
    assert calls == ["claude.exe"]
    monkeypatch.setattr(config.shutil, "which", lambda command: "codex.cmd")
    assert config.ai_cli_path("codex") is None
