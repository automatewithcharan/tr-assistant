"""
Risk guardrails for live/manual trading workflows.

This module reads the latest live portfolio snapshots and decides whether
new buy-side ideas should be paused for the rest of the current trading day.
"""

from __future__ import annotations

import os
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.PKDateUtilities import PKDateUtilities


@dataclass
class GuardrailStatus:
    """Simple immutable result for buy-side guardrail checks."""

    account_name: str
    enabled: bool
    buy_paused: bool
    daily_realized_pnl: float
    consecutive_losses: int
    reasons: List[str]

    def as_dict(self) -> Dict:
        return {
            "account_name": self.account_name,
            "enabled": self.enabled,
            "buy_paused": self.buy_paused,
            "daily_realized_pnl": self.daily_realized_pnl,
            "consecutive_losses": self.consecutive_losses,
            "reasons": list(self.reasons),
        }


class LiveTradingGuardrails:
    """Evaluate live account snapshots and pause buy-side risk when needed."""

    def __init__(
        self,
        account_name: str = "main",
        max_daily_loss_amount: float = 0.0,
        max_daily_profit_amount: float = 0.0,
        max_consecutive_losses: int = 0,
    ) -> None:
        self.account_name = str(account_name or "main").strip().lower().replace(" ", "_")
        self.max_daily_loss_amount = max(0.0, float(max_daily_loss_amount or 0.0))
        self.max_daily_profit_amount = max(0.0, float(max_daily_profit_amount or 0.0))
        self.max_consecutive_losses = max(0, int(max_consecutive_losses or 0))
        self.output_dir = Archiver.get_user_data_dir()
        self.today_text = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d")

    def evaluate(self) -> GuardrailStatus:
        """Return the current buy-side guardrail status for the configured account."""
        enabled = (
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

        trades_df = self._load_trades()
        daily_realized_pnl = self._daily_realized_pnl(trades_df)
        consecutive_losses = self._consecutive_losses(trades_df)
        reasons: List[str] = []

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
        )

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
