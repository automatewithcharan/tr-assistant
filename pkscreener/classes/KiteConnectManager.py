"""
Official Kite Connect session helpers for PKScreener.

This module keeps the official Kite Connect authentication flow separate from
PKBrokers-specific login helpers. It focuses on the supported developer flow:

1. Build a login URL from your Kite Connect API key
2. Exchange the returned request_token for an access_token
3. Persist the short-lived session locally for the current trading day
4. Verify the connection with profile/holdings/positions calls
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from PKDevTools.classes import Archiver


def _get_secret(name: str, default: str = "") -> str:
    """Load a secret from process env first, then PKEnvironment if available."""
    value = str(os.environ.get(name, default) or default).strip()
    if value:
        return value
    try:
        from PKDevTools.classes.Environment import PKEnvironment

        env = PKEnvironment()
        return str(env.allSecrets.get(name, default) or default).strip()
    except Exception:
        return str(default).strip()


def _normalize_secret(*names: str) -> str:
    """Return the first non-empty secret from a list of candidate names."""
    for name in names:
        value = _get_secret(name, "")
        if value:
            return value
    return ""


class KiteConnectSessionManager:
    """Small official Kite Connect session manager."""

    def __init__(self) -> None:
        self.api_key = _normalize_secret("KITE_API_KEY", "KITE_CONNECT_API_KEY")
        self.api_secret = _normalize_secret("KITE_API_SECRET", "KITE_CONNECT_API_SECRET")
        self.redirect_url = _normalize_secret("KITE_REDIRECT_URL", "KITE_CONNECT_REDIRECT_URL")
        self.access_token_override = _normalize_secret("KITE_ACCESS_TOKEN", "KITE_CONNECT_ACCESS_TOKEN")
        self.session_file_path = Path(Archiver.get_user_data_dir()) / "kite_connect_session.json"

    def _load_client_class(self):
        try:
            from kiteconnect import KiteConnect

            return KiteConnect
        except Exception as exc:
            raise RuntimeError(
                "Official Kite Connect support needs the 'kiteconnect' package. "
                "Install it with 'pip install kiteconnect'."
            ) from exc

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise ValueError("KITE_API_KEY is not configured.")

    def _ensure_api_credentials(self) -> None:
        self._ensure_api_key()
        if not self.api_secret:
            raise ValueError("KITE_API_SECRET is not configured.")

    def get_login_url(self) -> str:
        """Return the official Kite Connect login URL."""
        self._ensure_api_key()
        KiteConnect = self._load_client_class()
        client = KiteConnect(api_key=self.api_key)
        return str(client.login_url())

    def _read_session_file(self) -> Dict[str, Any]:
        try:
            if self.session_file_path.exists():
                return json.loads(self.session_file_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _write_session_file(self, payload: Dict[str, Any]) -> Path:
        self.session_file_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_file_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return self.session_file_path

    def clear_saved_session(self) -> bool:
        """Remove the locally saved access token file if it exists."""
        try:
            if self.session_file_path.exists():
                self.session_file_path.unlink()
                return True
        except Exception:
            return False
        return False

    def _parse_request_token(self, token_or_url: str) -> str:
        candidate = str(token_or_url or "").strip()
        if not candidate:
            raise ValueError("Request token or redirect URL is required.")

        if "request_token=" in candidate:
            parsed = urlparse(candidate)
            request_tokens = parse_qs(parsed.query).get("request_token", [])
            if request_tokens and str(request_tokens[0]).strip():
                return str(request_tokens[0]).strip()

        return candidate

    def _get_saved_access_token(self) -> str:
        if self.access_token_override:
            return self.access_token_override
        payload = self._read_session_file()
        return str(payload.get("access_token", "") or "").strip()

    def _build_client(self):
        self._ensure_api_key()
        access_token = self._get_saved_access_token()
        if not access_token:
            raise ValueError(
                "No Kite Connect access token is available yet. "
                "Generate one with --kiteconnectsession first."
            )
        KiteConnect = self._load_client_class()
        client = KiteConnect(api_key=self.api_key)
        client.set_access_token(access_token)
        return client

    def create_session(self, request_token_or_url: str) -> Dict[str, Any]:
        """Exchange a request token for an access token and persist it locally."""
        self._ensure_api_credentials()
        request_token = self._parse_request_token(request_token_or_url)
        KiteConnect = self._load_client_class()

        client = KiteConnect(api_key=self.api_key)
        session = client.generate_session(request_token, api_secret=self.api_secret)
        access_token = str(session.get("access_token", "") or "").strip()
        if not access_token:
            raise RuntimeError("Kite Connect did not return an access token.")

        client.set_access_token(access_token)
        profile = {}
        try:
            profile = client.profile() or {}
        except Exception:
            profile = {}

        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "api_key": self.api_key,
            "redirect_url": self.redirect_url,
            "session": session,
            "profile": profile,
            "access_token": access_token,
            "user_id": session.get("user_id") or profile.get("user_id"),
        }
        self._write_session_file(payload)
        return payload

    def get_profile(self) -> Dict[str, Any]:
        """Return the authenticated user profile."""
        client = self._build_client()
        return client.profile() or {}

    def get_connection_report(self) -> Dict[str, Any]:
        """Run a simple official Kite Connect health check."""
        session_payload = self._read_session_file()
        access_token = self._get_saved_access_token()
        report: Dict[str, Any] = {
            "api_key_configured": bool(self.api_key),
            "api_secret_configured": bool(self.api_secret),
            "redirect_url_configured": bool(self.redirect_url),
            "access_token_available": bool(access_token),
            "session_file_exists": self.session_file_path.exists(),
            "session_file_path": str(self.session_file_path),
            "saved_user_id": session_payload.get("user_id") or session_payload.get("session", {}).get("user_id"),
            "profile_ok": False,
            "holdings_ok": False,
            "positions_ok": False,
            "error": "",
        }

        if not self.api_key:
            report["error"] = "KITE_API_KEY is missing."
            return report

        if not access_token:
            report["error"] = (
                "No access token is saved yet. Use --kiteconnectloginurl, complete the Zerodha login, "
                "then pass the redirect URL or request token to --kiteconnectsession."
            )
            return report

        try:
            client = self._build_client()
            profile = client.profile() or {}
            report["profile_ok"] = True
            report["profile"] = profile
            try:
                holdings = client.holdings() or []
                report["holdings_ok"] = True
                report["holdings_count"] = len(holdings)
            except Exception as exc:
                report["holdings_error"] = str(exc)
            try:
                positions = client.positions() or {}
                day_positions = positions.get("day", []) if isinstance(positions, dict) else []
                net_positions = positions.get("net", []) if isinstance(positions, dict) else []
                report["positions_ok"] = True
                report["day_positions_count"] = len(day_positions)
                report["net_positions_count"] = len(net_positions)
            except Exception as exc:
                report["positions_error"] = str(exc)
        except Exception as exc:
            report["error"] = str(exc)

        return report


def format_connection_report(report: Dict[str, Any]) -> str:
    """Render a CLI-friendly doctor report."""
    lines = [
        f"Official Kite API key configured: {'Yes' if report.get('api_key_configured') else 'No'}",
        f"Official Kite API secret configured: {'Yes' if report.get('api_secret_configured') else 'No'}",
        f"Redirect URL configured: {'Yes' if report.get('redirect_url_configured') else 'No'}",
        f"Access token available: {'Yes' if report.get('access_token_available') else 'No'}",
        f"Session file exists: {'Yes' if report.get('session_file_exists') else 'No'}",
        f"Session file: {report.get('session_file_path', '')}",
    ]

    saved_user_id = str(report.get("saved_user_id", "") or "").strip()
    if saved_user_id:
        lines.append(f"Saved user id: {saved_user_id}")

    if report.get("profile_ok"):
        profile = report.get("profile", {}) or {}
        lines.append(
            "Profile check: OK"
            + (
                f" ({profile.get('user_name') or profile.get('user_id') or 'authenticated'})"
            )
        )
    else:
        lines.append("Profile check: Not ready")

    if report.get("holdings_ok"):
        lines.append(f"Holdings API: OK ({int(report.get('holdings_count', 0) or 0)} rows)")
    elif report.get("holdings_error"):
        lines.append(f"Holdings API: {report.get('holdings_error')}")

    if report.get("positions_ok"):
        lines.append(
            "Positions API: OK "
            f"(day={int(report.get('day_positions_count', 0) or 0)}, "
            f"net={int(report.get('net_positions_count', 0) or 0)})"
        )
    elif report.get("positions_error"):
        lines.append(f"Positions API: {report.get('positions_error')}")

    if report.get("error"):
        lines.append(f"Status: {report['error']}")
    else:
        lines.append("Status: Official Kite Connect session looks usable.")

    return "\n".join(lines)
