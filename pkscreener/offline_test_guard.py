"""Offline-only guard installed by the repository test bootstrap.

Nothing in this module is active unless ``PKSCREENER_TEST_OFFLINE=1`` exactly.
It intentionally raises ``BaseException`` so broad production ``except
Exception`` handlers cannot turn a prohibited connection into a silent pass.
"""

from __future__ import annotations

import inspect
import json
import os
import socket
import subprocess
import time
import shutil
import sys
import shlex
import http.client
import stat
from pathlib import Path
from typing import Any


class OfflineNetworkAttempt(BaseException):
    """A connection or network-capable transport was attempted during a test."""


def enabled() -> bool:
    return os.environ.get("PKSCREENER_TEST_OFFLINE", "") == "1"


def _test_root() -> Path | None:
    root = os.environ.get("PKSCREENER_TEST_ROOT", "")
    return Path(root).resolve() if root else None


_SECRET_EXACT = {
    "DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID",
    "DISCORD_ALLOWED_USER_ID", "DISCORD_CHANNEL_ID", "TOKEN", "CHAT_ID",
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "KITE_API_KEY", "KITE_API_SECRET",
    "KITE_ACCESS_TOKEN", "KITE_REQUEST_TOKEN", "KITE_USER_ID", "KITE_PASSWORD",
    "KITE_TOTP", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT", "DATABASE_URL", "POSTGRES_PASSWORD", "POSTGRES_USER",
    "MYSQL_PASSWORD", "REDIS_URL", "REDIS_PASSWORD", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
}
_SECRET_PREFIXES = (
    "DISCORD_", "TELEGRAM_", "SLACK_", "EMAIL_", "SMTP_", "TWILIO_",
    "KITE_API_", "KITE_ACCESS_", "KITE_SECRET_", "KITE_USER_", "AZURE_",
    "OAUTH_", "CLIENT_SECRET", "WEBHOOK_", "GOOGLE_OAUTH_", "GCP_",
)


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return (upper in _SECRET_EXACT or upper.startswith(_SECRET_PREFIXES) or
            any(token in upper for token in ("PASSWORD", "TOKEN", "SECRET", "WEBHOOK", "PRIVATE_KEY")))


def controlled_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the only environment repository test subprocesses may receive."""
    root = _test_root()
    if root is None:
        raise RuntimeError("offline test subprocess root is missing")
    repo = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PKSCREENER_TEST_OFFLINE": "1", "PKSCREENER_TEST_ROOT": str(root),
        "HOME": str(root / "home"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"), "PYTHONNOUSERSITE": "1",
        "NO_PROXY": "*", "KITE_EXECUTION_MODE": "paper", "KITE_DRY_RUN": "1",
        "KITE_AUTO_TRADE": "0", "LOKY_MAX_CPU_COUNT": "1",
        "PYTHONPATH": f"{repo / 'tests/offline_bootstrap'}:{repo}",
    }
    for name, value in (extra or {}).items():
        if is_secret_name(name):
            continue
        env[name] = value
    # These cannot be weakened by a test's caller-supplied environment.
    env.update({"PKSCREENER_TEST_OFFLINE": "1", "KITE_EXECUTION_MODE": "paper",
                "KITE_DRY_RUN": "1", "KITE_AUTO_TRADE": "0", "LOKY_MAX_CPU_COUNT": "1",
                "PYTHONPATH": f"{repo / 'tests/offline_bootstrap'}:{repo}"})
    return env


def _record(kind: str) -> None:
    root = _test_root()
    if root is None:
        return
    try:
        root.mkdir(parents=True, exist_ok=True)
        frame = inspect.stack()[2]
        event = {
            "kind": kind,
            "pid": os.getpid(),
            "source": f"{Path(frame.filename).name}:{frame.lineno}",
            "timestamp": time.time(),
        }
        with (root / "offline_network_attempts.jsonl").open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, sort_keys=True) + "\n")
    except Exception:
        pass


def _blocked(kind: str):
    def deny(*_args: Any, **_kwargs: Any) -> None:
        _record(kind)
        raise OfflineNetworkAttempt(f"offline test guard blocked {kind}")
    return deny


def _trusted_lscpu_invocation(args: Any, popen_kwargs: dict[str, Any]) -> bool:
    """Allow only NumPy's exact local, read-only CPU feature probe."""
    if args != "lscpu" or popen_kwargs.get("shell", False):
        return False
    allowed_keys = {"stdout", "stderr", "universal_newlines", "text"}
    if set(popen_kwargs) - allowed_keys:
        return False
    if popen_kwargs.get("stdout") != subprocess.PIPE or popen_kwargs.get("stderr") != subprocess.PIPE:
        return False
    if popen_kwargs.get("text") is not True and popen_kwargs.get("universal_newlines") is not True:
        return False
    resolved = shutil.which("lscpu")
    if not resolved:
        return False
    canonical = Path(os.path.realpath(resolved))
    trusted_roots = (Path("/usr/bin"), Path("/usr/sbin"), Path("/bin"), Path("/sbin"))
    if not any(canonical.parent == root for root in trusted_roots):
        return False
    try:
        mode = canonical.stat().st_mode
    except OSError:
        return False
    if not stat.S_ISREG(mode) or not os.access(canonical, os.X_OK):
        return False
    forbidden = {Path.cwd().resolve()}
    root = _test_root()
    if root:
        forbidden.add(root)
    forbidden.add(Path.home().resolve())
    if any(canonical == item or item in canonical.parents for item in forbidden):
        return False
    return True


def assert_runtime_path(path: str | os.PathLike[str]) -> Path:
    """Permit test runtime files only inside the assigned temporary root."""
    resolved = Path(path).expanduser().resolve()
    if not enabled():
        return resolved
    root = _test_root()
    if root is None:
        raise RuntimeError("offline test runtime root is missing")
    if resolved == root or root in resolved.parents:
        return resolved
    raise RuntimeError("offline test mode rejects runtime paths outside PKSCREENER_TEST_ROOT")


def install() -> None:
    if not enabled() or getattr(install, "_installed", False):
        return
    install._installed = True  # type: ignore[attr-defined]
    socket.create_connection = _blocked("socket.create_connection")  # type: ignore[assignment]
    socket.getaddrinfo = _blocked("socket.getaddrinfo")  # type: ignore[assignment]
    socket.socket.connect = _blocked("socket.connect")  # type: ignore[assignment]
    socket.socket.connect_ex = _blocked("socket.connect_ex")  # type: ignore[assignment]
    try:
        import requests
        requests.sessions.Session.request = _blocked("requests.Session.request")  # type: ignore[assignment]
        requests.sessions.Session.send = _blocked("requests.Session.send")  # type: ignore[assignment]
        requests.request = _blocked("requests.request")  # type: ignore[assignment]
    except Exception:
        pass
    try:
        http.client.HTTPConnection.connect = _blocked("http.client.HTTPConnection.connect")  # type: ignore[assignment]
        http.client.HTTPSConnection.connect = _blocked("http.client.HTTPSConnection.connect")  # type: ignore[assignment]
    except Exception:
        pass
    try:
        import urllib3
        urllib3.connection.HTTPConnection.connect = _blocked("urllib3.HTTPConnection.connect")  # type: ignore[assignment]
        urllib3.connection.HTTPSConnection.connect = _blocked("urllib3.HTTPSConnection.connect")  # type: ignore[assignment]
        urllib3.connectionpool.HTTPConnectionPool._make_request = _blocked("urllib3.HTTPConnectionPool._make_request")  # type: ignore[assignment]
        urllib3.connectionpool.HTTPSConnectionPool._make_request = _blocked("urllib3.HTTPSConnectionPool._make_request")  # type: ignore[assignment]
    except Exception:
        pass
    try:
        import urllib.request
        urllib.request.urlopen = _blocked("urllib.request.urlopen")  # type: ignore[assignment]
    except Exception:
        pass
    try:
        import smtplib
        smtplib.SMTP.connect = _blocked("smtplib.SMTP.connect")  # type: ignore[assignment]
        smtplib.SMTP_SSL.connect = _blocked("smtplib.SMTP_SSL.connect")  # type: ignore[assignment]
    except Exception:
        pass
    original_popen = subprocess.Popen
    allowed_basenames = {"python", "python3", Path(sys.executable).name, "bash", "sh", "git", "echo", "printf", "true", "false"}

    class SafePopen(original_popen):
        def __init__(self, args: Any, *popen_args: Any, **popen_kwargs: Any):
            command = args[0] if isinstance(args, (list, tuple)) and args else args
            if popen_kwargs.get("shell"):
                command_text = str(command)
                first = shlex.split(command_text)[0] if command_text.strip() else ""
                command = first
            name = Path(str(command)).name.lower()
            resolved = shutil.which(str(command)) if not os.path.isabs(str(command)) else str(command)
            resolved_path = Path(resolved).resolve() if resolved else None
            allowed = name in allowed_basenames and resolved_path is not None and (
                str(resolved_path).startswith("/usr/") or str(resolved_path).startswith("/bin/") or
                resolved_path == Path(sys.executable).resolve())
            if name == "lscpu" and _trusted_lscpu_invocation(args, popen_kwargs):
                allowed = True
            if not allowed:
                _record(f"subprocess:{name}")
                raise OfflineNetworkAttempt(f"offline test guard blocked subprocess {name}")
            popen_kwargs["env"] = controlled_subprocess_env(popen_kwargs.get("env"))
            super().__init__(args, *popen_args, **popen_kwargs)

    subprocess.Popen = SafePopen  # type: ignore[assignment]
