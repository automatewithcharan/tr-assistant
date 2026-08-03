"""
Risk guardrails for live/manual trading workflows.

This module reads the latest live portfolio snapshots and decides whether
new buy-side ideas should be paused for the rest of the current trading day.
"""

from __future__ import annotations

import os
import json
import time
import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
if os.environ.get("PKSCREENER_TEST_OFFLINE") == "1":
    from pkscreener.offline_test_guard import assert_runtime_path, enabled as _offline_test_mode
else:
    def assert_runtime_path(path):
        return path
    def _offline_test_mode():
        return False


@dataclass
class GuardrailStatus:
    """Simple immutable result for buy-side guardrail checks."""

    account_name: str
    enabled: bool
    buy_paused: bool
    daily_realized_pnl: float
    consecutive_losses: int
    reasons: List[str]
    evidence_valid: bool = True
    evidence_reasons: List[str] = None

    def as_dict(self) -> Dict:
        return {
            "account_name": self.account_name,
            "enabled": self.enabled,
            "buy_paused": self.buy_paused,
            "daily_realized_pnl": self.daily_realized_pnl,
            "consecutive_losses": self.consecutive_losses,
            "reasons": list(self.reasons),
            "evidence_valid": self.evidence_valid,
            "evidence_reasons": list(self.evidence_reasons or []),
        }


class LiveTradingGuardrails:
    """Evaluate live account snapshots and pause buy-side risk when needed."""

    def __init__(
        self,
        account_name: str = "main",
        max_daily_loss_amount: float = 0.0,
        max_daily_profit_amount: float = 0.0,
        max_consecutive_losses: int = 0,
        require_valid_evidence: bool = False,
        evidence_max_age_seconds: int = 900,
        evidence_path: Optional[str] = None,
        evidence_source: Optional[str] = None,
    ) -> None:
        self.account_name = str(account_name or "main").strip().lower().replace(" ", "_")
        self.max_daily_loss_amount = max(0.0, float(max_daily_loss_amount or 0.0))
        self.max_daily_profit_amount = max(0.0, float(max_daily_profit_amount or 0.0))
        self.max_consecutive_losses = max(0, int(max_consecutive_losses or 0))
        self.require_valid_evidence = bool(require_valid_evidence)
        self.evidence_max_age_seconds = max(1, int(evidence_max_age_seconds or 900))
        self.output_dir = Archiver.get_user_data_dir()
        self.today_text = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d")
        self.evidence_path = evidence_path
        self.evidence_source = str(
            evidence_source or os.environ.get("KITE_RISK_EVIDENCE_SOURCE", "live_portfolio")
        )

    def _now_ist(self) -> datetime:
        return datetime.now(ZoneInfo("Asia/Kolkata"))

    def _risk_evidence_file(self) -> Path:
        path = Path(
            self.evidence_path
            or os.path.join(self.output_dir, f"risk_evidence_{self.account_name}.json")
        )
        return assert_runtime_path(path) if _offline_test_mode() else path

    def _risk_evidence_state_file(self) -> Path:
        path = self._risk_evidence_file()
        return path.with_name(f".{path.name}.accepted.json")

    @staticmethod
    def _evidence_digest(payload: Dict[str, Any]) -> str:
        stable = {k: v for k, v in payload.items() if k != "generated_at"}
        return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _check_evidence_watermark(self, payload: Dict[str, Any]) -> List[str]:
        """Reject sequence rollback/reuse and backwards completion watermarks."""
        path = self._risk_evidence_state_file()
        digest = self._evidence_digest(payload)
        current = {
            "account": self.account_name, "trading_date": payload.get("trading_date"),
            "sequence": payload.get("sequence"), "complete_through": payload.get("complete_through"),
            "digest": digest,
        }
        try:
            prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            if prior:
                if prior.get("account") != self.account_name:
                    return ["risk evidence acceptance state account mismatch"]
                if prior.get("trading_date") == payload.get("trading_date"):
                    if int(payload["sequence"]) < int(prior.get("sequence", -1)):
                        return ["risk evidence sequence moved backwards"]
                    if int(payload["sequence"]) == int(prior.get("sequence", -1)) and digest != prior.get("digest"):
                        return ["risk evidence sequence was reused with different contents"]
                    if str(payload.get("complete_through", "")) < str(prior.get("complete_through", "")):
                        return ["risk evidence completion watermark moved backwards"]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(current, fp, sort_keys=True)
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
            return []
        except Exception as exc:
            return [f"risk evidence acceptance state unavailable: {type(exc).__name__}"]

    def write_evidence_contract(
        self, *, daily_realized_pnl: float, consecutive_losses: int,
        sequence: int, session_state: str, zero_trade: bool,
        complete_through: Optional[datetime] = None,
    ) -> Path:
        """Atomically publish the authoritative risk snapshot, including zero trades."""
        now = self._now_ist()
        complete = (complete_through or now).astimezone(ZoneInfo("Asia/Kolkata"))
        payload = {
            "schema_version": 1,
            "trading_date": now.date().isoformat(),
            "generated_at": now.isoformat(timespec="seconds"),
            "complete_through": complete.isoformat(timespec="seconds"),
            "source": self.evidence_source,
            "account": self.account_name,
            "sequence": int(sequence),
            "session_state": str(session_state),
            "zero_trade": bool(zero_trade),
            "daily_realized_pnl": float(daily_realized_pnl),
            "consecutive_losses": int(consecutive_losses),
        }
        path = self._risk_evidence_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp, sort_keys=True)
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
            if os.name != "nt":
                fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if tmp.exists():
                tmp.unlink()
        return path

    @staticmethod
    def _parse_ist_timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} is missing")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=5, minutes=30):
            raise ValueError(f"{field} must include an Asia/Kolkata +05:30 offset")
        return parsed.astimezone(ZoneInfo("Asia/Kolkata"))

    def _load_risk_evidence_contract(self) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """Load the single authoritative, current-day realised-P&L contract."""
        path = self._risk_evidence_file()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, [f"missing {path.name}"]
        except Exception as exc:
            return None, [f"malformed or unreadable {path.name}: {type(exc).__name__}"]
        reasons: List[str] = []
        if not os.access(path, os.R_OK) or not os.access(path.parent, os.W_OK):
            reasons.append("risk evidence is unreadable or cannot be atomically replaced")
        if not isinstance(payload, dict):
            return None, ["risk evidence root must be an object"]
        if payload.get("schema_version") != 1:
            reasons.append("unsupported schema_version")
        if payload.get("source") != self.evidence_source:
            reasons.append("source identity mismatch")
        if payload.get("account") != self.account_name:
            reasons.append("account identity mismatch")
        now = self._now_ist()
        if payload.get("trading_date") != now.date().isoformat():
            reasons.append("trading_date is not current in Asia/Kolkata")
        try:
            generated = self._parse_ist_timestamp(payload.get("generated_at"), "generated_at")
            complete = self._parse_ist_timestamp(payload.get("complete_through"), "complete_through")
            if generated > now + timedelta(minutes=5) or complete > now + timedelta(minutes=5):
                reasons.append("future-dated evidence")
            session_state = payload.get("session_state")
            if session_state not in {"open", "preopen", "closed", "holiday"}:
                reasons.append("invalid session_state")
            active_now = now.weekday() < 5 and (9 * 60 + 15) <= now.hour * 60 + now.minute <= 15 * 60 + 30
            if active_now and session_state != "holiday":
                if session_state != "open":
                    reasons.append("session_state is inconsistent with active trading hours")
                if (now - complete).total_seconds() > self.evidence_max_age_seconds:
                    reasons.append("stale complete_through watermark during active trading")
            if complete > generated:
                reasons.append("complete_through is later than generated_at")
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            reasons.append("sequence must be a non-negative integer")
        zero_trade = payload.get("zero_trade")
        if not isinstance(zero_trade, bool):
            reasons.append("zero_trade must be boolean")
        pnl = payload.get("daily_realized_pnl")
        losses = payload.get("consecutive_losses")
        if not isinstance(pnl, (int, float)) or isinstance(pnl, bool):
            reasons.append("daily_realized_pnl must be numeric")
        if not isinstance(losses, int) or isinstance(losses, bool) or losses < 0:
            reasons.append("consecutive_losses must be a non-negative integer")
        if zero_trade and (pnl != 0 or losses != 0):
            reasons.append("zero_trade evidence must report zero P&L and zero losses")
        if not reasons and isinstance(sequence, int) and not isinstance(sequence, bool):
            reasons.extend(self._check_evidence_watermark(payload))
        return payload, list(dict.fromkeys(reasons))

    def evaluate(self) -> GuardrailStatus:
        """Return the current buy-side guardrail status for the configured account."""
        enabled = self.require_valid_evidence or (
            self.max_daily_loss_amount > 0
            or self.max_daily_profit_amount > 0
            or self.max_consecutive_losses > 0
        )
        if not enabled:
            return GuardrailStatus(
                account_name=self.account_name,
                enabled=False,
                buy_paused=False,
                daily_realized_pnl=0.0,
                consecutive_losses=0,
                reasons=[],
            )

        if self.require_valid_evidence:
            contract, evidence_reasons = self._load_risk_evidence_contract()
            evidence_valid = not evidence_reasons
            daily_realized_pnl = float((contract or {}).get("daily_realized_pnl", 0.0) or 0.0)
            consecutive_losses = int((contract or {}).get("consecutive_losses", 0) or 0)
        else:
            trades_df, evidence_reasons = self._load_trades_with_evidence()
            evidence_valid = not evidence_reasons
            daily_realized_pnl = self._daily_realized_pnl(trades_df)
            consecutive_losses = self._consecutive_losses(trades_df)
        reasons: List[str] = []
        if self.require_valid_evidence and not evidence_valid:
            reasons.extend(f"risk evidence invalid: {reason}" for reason in evidence_reasons)

        if self.max_daily_loss_amount > 0 and daily_realized_pnl <= (-1.0 * self.max_daily_loss_amount):
            reasons.append(
                f"daily realized P&L {daily_realized_pnl:.2f} breached loss limit {self.max_daily_loss_amount:.2f}"
            )
        if self.max_daily_profit_amount > 0 and daily_realized_pnl >= self.max_daily_profit_amount:
            reasons.append(
                f"daily realized P&L {daily_realized_pnl:.2f} reached profit target {self.max_daily_profit_amount:.2f}"
            )
        if self.max_consecutive_losses > 0 and consecutive_losses >= self.max_consecutive_losses:
            reasons.append(
                f"consecutive losing exits {consecutive_losses} reached limit {self.max_consecutive_losses}"
            )

        return GuardrailStatus(
            account_name=self.account_name,
            enabled=enabled,
            buy_paused=bool(reasons),
            daily_realized_pnl=round(daily_realized_pnl, 2),
            consecutive_losses=int(consecutive_losses),
            reasons=reasons,
            evidence_valid=evidence_valid,
            evidence_reasons=evidence_reasons,
        )

    def _validate_file(self, path: str, required_columns: set) -> tuple[pd.DataFrame, List[str]]:
        """Validate current-day, writable P&L evidence without changing it."""
        reasons: List[str] = []
        if not os.path.exists(path):
            return pd.DataFrame(), [f"missing {os.path.basename(path)}"]
        try:
            stat = os.stat(path)
            if stat.st_size <= 0:
                reasons.append(f"empty {os.path.basename(path)}")
            if time.time() - stat.st_mtime > self.evidence_max_age_seconds:
                reasons.append(f"stale {os.path.basename(path)}")
            if not os.access(path, os.R_OK | os.W_OK):
                reasons.append(f"unreadable or unwritable {os.path.basename(path)}")
            frame = pd.read_csv(path)
        except Exception as exc:
            return pd.DataFrame(), [f"malformed or unreadable {os.path.basename(path)}: {type(exc).__name__}"]
        if frame is None or frame.empty:
            reasons.append(f"empty {os.path.basename(path)}")
            return pd.DataFrame(), list(dict.fromkeys(reasons))
        missing = sorted(required_columns.difference(frame.columns))
        if missing:
            reasons.append(f"schema missing {','.join(missing)} in {os.path.basename(path)}")
        return frame, list(dict.fromkeys(reasons))

    def _load_trades_with_evidence(self) -> tuple[pd.DataFrame, List[str]]:
        ledger_path = os.path.join(self.output_dir, f"livetrade_{self.account_name}_trades_latest.csv")
        journal_path = os.path.join(self.output_dir, "autotrade_event_journal.csv")
        ledger, ledger_reasons = self._validate_file(
            ledger_path, {"Status", "ExitDate", "TradeId", "RealizedPnL"}
        )
        journal, journal_reasons = self._validate_file(
            journal_path, {"timestamp", "event", "symbol", "side", "qty", "price", "status"}
        )
        if not journal_reasons:
            usable = journal.copy()
            usable["qty"] = pd.to_numeric(usable["qty"], errors="coerce")
            usable["price"] = pd.to_numeric(usable["price"], errors="coerce")
            usable = usable[
                usable["event"].fillna("").astype(str).str.lower().eq("order_result")
                & usable["side"].fillna("").astype(str).str.upper().isin({"BUY", "SELL"})
                & usable["status"].fillna("").astype(str).str.upper().isin({"COMPLETE", "PARTIAL_FILLED_CANCELLED"})
                & usable["qty"].gt(0)
                & usable["price"].gt(0)
            ]
            if usable.empty:
                journal_reasons.append("execution journal has no usable completed fills")
        ledger_valid = not ledger_reasons
        journal_valid = not journal_reasons
        if ledger_valid and journal_valid:
            journal_trades = self._journal_to_trades(journal)
            ledger_pnl = self._daily_realized_pnl(ledger)
            journal_pnl = self._daily_realized_pnl(journal_trades)
            if abs(ledger_pnl - journal_pnl) > 0.01:
                return pd.DataFrame(), [
                    f"ledger/journal realised P&L mismatch ({ledger_pnl:.2f} != {journal_pnl:.2f})"
                ]
            return ledger, []
        if ledger_valid:
            return ledger, []
        if journal_valid:
            return self._journal_to_trades(journal), []
        return pd.DataFrame(), list(dict.fromkeys(ledger_reasons + journal_reasons))

    def _load_trades(self) -> pd.DataFrame:
        path = os.path.join(
            self.output_dir,
            f"livetrade_{self.account_name}_trades_latest.csv",
        )
        if os.path.exists(path):
            try:
                trades_df = pd.read_csv(path)
                if trades_df is not None and not trades_df.empty:
                    return trades_df
            except Exception:
                pass
        return self._load_autotrade_journal_trades()

    def _load_autotrade_journal_trades(self) -> pd.DataFrame:
        """Reconstruct realized bot P&L when the portfolio trade CSV is absent.

        The controlled SignalMonitor lane writes every completed broker fill to
        ``autotrade_event_journal.csv``. Using only ``order_result`` rows avoids
        double counting the companion ``entry_result`` and protective-stop
        lifecycle events.
        """
        path = os.path.join(self.output_dir, "autotrade_event_journal.csv")
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            journal = pd.read_csv(path)
        except Exception:
            return pd.DataFrame()
        required = {"timestamp", "event", "symbol", "side", "qty", "price", "status"}
        if journal is None or journal.empty or not required.issubset(journal.columns):
            return pd.DataFrame()

        return self._journal_to_trades(journal)

    def _journal_to_trades(self, journal: pd.DataFrame) -> pd.DataFrame:
        rows = journal.copy()
        rows["event"] = rows["event"].fillna("").astype(str).str.strip().str.lower()
        rows["side"] = rows["side"].fillna("").astype(str).str.strip().str.upper()
        rows["status"] = rows["status"].fillna("").astype(str).str.strip().str.upper()
        rows["symbol"] = rows["symbol"].fillna("").astype(str).str.strip().str.upper()
        rows["qty"] = pd.to_numeric(rows["qty"], errors="coerce").fillna(0).astype(int)
        rows["price"] = pd.to_numeric(rows["price"], errors="coerce").fillna(0.0)
        rows["parsed_at"] = pd.to_datetime(rows["timestamp"], errors="coerce")
        rows = rows[
            rows["event"].eq("order_result")
            & rows["side"].isin({"BUY", "SELL"})
            & rows["status"].isin({"COMPLETE", "PARTIAL_FILLED_CANCELLED"})
            & rows["parsed_at"].notna()
            & rows["qty"].gt(0)
            & rows["price"].gt(0)
        ].sort_values("parsed_at")
        if rows.empty:
            return pd.DataFrame()

        open_lots = defaultdict(deque)
        realized_rows = []
        trade_id = 0
        for row in rows.to_dict(orient="records"):
            symbol = row["symbol"]
            qty = int(row["qty"])
            price = float(row["price"])
            if row["side"] == "BUY":
                open_lots[symbol].append([qty, price])
                continue
            remaining = qty
            realized_pnl = 0.0
            matched_qty = 0
            while remaining > 0 and open_lots[symbol]:
                lot = open_lots[symbol][0]
                matched = min(remaining, int(lot[0]))
                realized_pnl += (price - float(lot[1])) * matched
                matched_qty += matched
                remaining -= matched
                lot[0] -= matched
                if lot[0] <= 0:
                    open_lots[symbol].popleft()
            if matched_qty <= 0:
                continue
            trade_id += 1
            parsed_at = row["parsed_at"]
            realized_rows.append(
                {
                    "Status": "CLOSED",
                    "ExitDate": parsed_at.strftime("%Y-%m-%d"),
                    "TradeId": trade_id,
                    "RealizedPnL": round(realized_pnl, 2),
                    "Symbol": symbol,
                    "Qty": matched_qty,
                    "Source": "autotrade_event_journal",
                }
            )
        return pd.DataFrame(realized_rows)

    def _daily_realized_pnl(self, trades_df: pd.DataFrame) -> float:
        if trades_df is None or trades_df.empty:
            return 0.0
        df = trades_df.copy()
        if "ExitDate" not in df.columns or "RealizedPnL" not in df.columns:
            return 0.0
        df["ExitDate"] = df["ExitDate"].fillna("").astype(str).str.strip()
        df["Status"] = df.get("Status", "").fillna("").astype(str).str.strip().str.upper()
        df["RealizedPnL"] = pd.to_numeric(df["RealizedPnL"], errors="coerce").fillna(0.0)
        closed_today_df = df[
            (df["Status"] == "CLOSED")
            & (df["ExitDate"] == self.today_text)
        ].copy()
        if closed_today_df.empty:
            return 0.0
        return round(float(closed_today_df["RealizedPnL"].sum()), 2)

    def _consecutive_losses(self, trades_df: pd.DataFrame) -> int:
        if trades_df is None or trades_df.empty:
            return 0
        df = trades_df.copy()
        required_columns = {"Status", "ExitDate", "TradeId", "RealizedPnL"}
        if not required_columns.issubset(set(df.columns)):
            return 0
        df["Status"] = df["Status"].fillna("").astype(str).str.strip().str.upper()
        df["ExitDate"] = df["ExitDate"].fillna("").astype(str).str.strip()
        df["TradeId"] = pd.to_numeric(df["TradeId"], errors="coerce").fillna(0).astype(int)
        df["RealizedPnL"] = pd.to_numeric(df["RealizedPnL"], errors="coerce").fillna(0.0)
        closed_df = df[df["Status"] == "CLOSED"].copy()
        if closed_df.empty:
            return 0
        closed_df.sort_values(["ExitDate", "TradeId"], ascending=[False, False], inplace=True)
        streak = 0
        for _, row in closed_df.iterrows():
            pnl = float(row.get("RealizedPnL", 0.0) or 0.0)
            if pnl < 0:
                streak += 1
                continue
            break
        return int(streak)
