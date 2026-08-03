"""Fail-closed offline boundary for every pytest test in this repository."""

from __future__ import annotations

import os
import threading
import multiprocessing
from pathlib import Path

import pytest
from pkscreener.offline_test_guard import OfflineNetworkAttempt, install, is_secret_name, controlled_subprocess_env


_EXACT_SECRET_NAMES = {
    "DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID",
    "DISCORD_ALLOWED_USER_ID", "DISCORD_CHANNEL_ID", "TOKEN", "CHAT_ID",
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "KITE_API_KEY", "KITE_API_SECRET",
    "KITE_ACCESS_TOKEN", "KITE_REQUEST_TOKEN", "KITE_USER_ID", "KITE_PASSWORD",
    "KITE_TOTP", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
}
_SECRET_PREFIXES = (
    "DISCORD_", "TELEGRAM_", "SLACK_", "EMAIL_", "SMTP_", "TWILIO_",
    "KITE_API_", "KITE_ACCESS_", "KITE_SECRET_", "KITE_USER_",
)


def _secret_environment_names() -> tuple[str, ...]:
    return tuple(sorted(name for name in os.environ if is_secret_name(name)))


SANITIZED_ENVIRONMENT_NAMES = _secret_environment_names()
for _name in SANITIZED_ENVIRONMENT_NAMES:
    os.environ.pop(_name, None)
os.environ["PKSCREENER_TEST_OFFLINE"] = "1"
os.environ["NO_PROXY"] = "*"
os.environ["LOKY_MAX_CPU_COUNT"] = "1"


install()


try:
    import dotenv
    dotenv.load_dotenv = lambda *_args, **_kwargs: False
except Exception:
    pass


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def offline_subprocess_env(tmp_path):
    """Controlled environment for every subprocess a test intentionally launches."""
    def build(extra=None):
        os.environ["PKSCREENER_TEST_ROOT"] = str(tmp_path)
        return controlled_subprocess_env(extra)
    return build

try:
    import PKDevTools.classes.Environment as _environment_module

    class _NoProductionEnvironment:
        allSecrets = {}

    _environment_module.PKEnvironment = _NoProductionEnvironment
except Exception:
    pass


@pytest.fixture(autouse=True)
def offline_test_boundary(monkeypatch, tmp_path):
    """Keep runtime files and credentials isolated for each test."""
    for name in _secret_environment_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PKSCREENER_TEST_OFFLINE", "1")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("LOKY_MAX_CPU_COUNT", "1")
    monkeypatch.setenv("PKSCREENER_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from PKDevTools.classes import Archiver
    monkeypatch.setattr(Archiver, "get_user_data_dir", lambda: str(tmp_path))
    before_threads = {thread.ident for thread in threading.enumerate() if thread.is_alive()}
    before_children = {child.pid for child in multiprocessing.active_children()}
    yield
    leaked = [
        thread.name for thread in threading.enumerate()
        if thread.is_alive() and thread.ident not in before_threads
    ]
    assert not leaked, f"test leaked worker thread(s): {leaked}"
    leaked_children = [
        child.pid for child in multiprocessing.active_children()
        if child.pid not in before_children
    ]
    assert not leaked_children, f"test leaked child process(es): {leaked_children}"
