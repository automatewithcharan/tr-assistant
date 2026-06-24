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
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import requests as _requests

from PKDevTools.classes import Archiver

_log = logging.getLogger(__name__)


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
        self.user_id = _normalize_secret("KITE_USER_ID", "KITE_CONNECT_USER_ID")
        self.password = _normalize_secret("KITE_PASSWORD", "KITE_CONNECT_PASSWORD")
        self.totp_secret = _normalize_secret("KITE_TOTP_SECRET", "KITE_CONNECT_TOTP_SECRET")
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

    # ------------------------------------------------------------------
    # TOTP auto-login
    # ------------------------------------------------------------------

    def can_auto_login(self) -> bool:
        """Return True when all credentials for automated TOTP login are set."""
        return bool(self.api_key and self.api_secret and self.user_id and self.password and self.totp_secret)

    def is_session_fresh(self) -> bool:
        """True if a saved access token exists and was created today."""
        payload = self._read_session_file()
        created = str(payload.get("created_at", "") or "").strip()
        if not created:
            return False
        try:
            created_date = datetime.fromisoformat(created).date()
            return created_date == date.today()
        except Exception:
            return False

    def auto_login(self) -> Dict[str, Any]:
        """Perform a full headless Kite Connect login using TOTP.

        Flow:
        1. POST user_id + password → get request_id
        2. Generate TOTP code from secret
        3. POST request_id + TOTP → get request_token via redirect
        4. Exchange request_token for access_token via official SDK
        """
        if not self.can_auto_login():
            raise ValueError(
                "Automated login requires KITE_API_KEY, KITE_API_SECRET, "
                "KITE_USER_ID, KITE_PASSWORD, and KITE_TOTP_SECRET."
            )

        try:
            import pyotp
        except ImportError as exc:
            raise RuntimeError("pyotp is required for TOTP auto-login. pip install pyotp") from exc

        _log.info("Starting automated Kite Connect login for %s …", self.user_id)

        sess = _requests.Session()

        # Step 1 — login with user_id + password
        resp = sess.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": self.user_id, "password": self.password},
            timeout=30,
        )
        if resp.status_code >= 400:
            message = ""
            try:
                payload = resp.json()
                message = str(payload.get("message") or payload.get("error_type") or "")
            except Exception:
                message = resp.text[:200]
            raise RuntimeError(
                f"Kite login step-1 failed ({resp.status_code}): {message or 'no details'}"
            )
        login_data = resp.json().get("data", {})
        request_id = login_data.get("request_id", "")
        if not request_id:
            raise RuntimeError(f"Kite login step-1 failed: {resp.text[:300]}")

        # Step 2 — submit TOTP. Kite sometimes changes the requested 2FA
        # method after account/security changes; do not burn attempts by
        # sending a TOTP code when Kite is explicitly asking for app_code/SMS.
        twofa_type = str(login_data.get("twofa_type", "totp") or "totp").strip()
        available_twofa = login_data.get("twofa_types", []) or []
        if twofa_type != "totp" and "totp" not in available_twofa:
            raise RuntimeError(
                "Kite login is not asking for TOTP right now "
                f"(requested {twofa_type or 'unknown'}, available {available_twofa}). "
                "Re-enable external authenticator/TOTP in Kite or use a manual "
                "Kite Connect redirect URL for today's session."
            )
        if twofa_type != "totp":
            twofa_type = "totp"

        totp_code = pyotp.TOTP(self.totp_secret).now()
        resp = sess.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": self.user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": twofa_type,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            message = ""
            try:
                payload = resp.json()
                message = str(payload.get("message") or payload.get("error_type") or "")
            except Exception:
                message = resp.text[:200]
            raise RuntimeError(
                f"Kite TOTP step failed ({resp.status_code}): {message or 'no details'}"
            )

        # Step 3 — extract request_token via the OAuth redirect chain.
        # Kite may redirect: login_url -> /connect/finish -> redirect_url?request_token=...
        # We follow redirects manually to catch request_token before hitting localhost.
        login_url = self.get_login_url()
        request_token = ""
        for _hop in range(5):
            resp = sess.get(login_url, allow_redirects=False, timeout=30)
            if resp.status_code not in (301, 302, 303):
                break
            redirect_location = resp.headers.get("Location", "")
            parsed = parse_qs(urlparse(redirect_location).query)
            tokens = parsed.get("request_token", [])
            if tokens and str(tokens[0]).strip():
                request_token = str(tokens[0]).strip()
                break
            # Follow to the next hop (but only if still on kite.zerodha.com)
            if redirect_location.startswith("/"):
                redirect_location = "https://kite.zerodha.com" + redirect_location
            if not redirect_location.startswith("https://kite.zerodha.com"):
                break
            login_url = redirect_location

        if not request_token:
            raise RuntimeError(
                "Could not extract request_token from Kite redirect chain."
            )

        # Step 4 — exchange for access_token via official SDK
        _log.info("Got request_token, exchanging for access_token …")
        return self.create_session(request_token)

    def ensure_fresh_session(self) -> Dict[str, Any]:
        """Ensure today's access token exists; auto-login if possible.

        Returns the connection report dict. Raises on fatal errors.
        """
        if self.is_session_fresh():
            report = self.get_connection_report()
            if not report.get("error"):
                _log.info("Kite session is fresh for today.")
                return report

        if self.can_auto_login():
            _log.info("Session is stale or missing — running TOTP auto-login …")
            self.auto_login()
            report = self.get_connection_report()
            if report.get("error"):
                raise RuntimeError(
                    f"Auto-login succeeded but session check failed: {report['error']}"
                )
            _log.info("TOTP auto-login complete. Session is ready.")
            return report

        report = self.get_connection_report()
        if report.get("error"):
            raise RuntimeError(
                "Kite session is stale and auto-login is not configured. "
                "Set KITE_USER_ID, KITE_PASSWORD, and KITE_TOTP_SECRET, "
                f"or log in manually. Error: {report['error']}"
            )
        return report

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
            "auto_login_configured": self.can_auto_login(),
            "session_fresh_today": self.is_session_fresh(),
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
        f"TOTP auto-login configured: {'Yes' if report.get('auto_login_configured') else 'No'}",
        f"Session fresh today: {'Yes' if report.get('session_fresh_today') else 'No'}",
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
