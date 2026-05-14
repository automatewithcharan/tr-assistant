"""
Intraday short-only paper trader with full execution-quality tracking.

Design rules (paper trading only — no real orders ever placed):
  - Fill simulation: next-bar open + half-spread (pessimistic for shorts)
  - Slippage = simulated_fill - intended_entry (positive = bad for short seller)
  - Circuit breakers: per-symbol bad-fill strikes + daily loss cap
  - Backtest comparison: alert when live slippage materially exceeds assumed baseline
  - All state is persisted to CSV for the daily report

Backtest assumptions (from forensic audit):
  fee_pct   = 0.05  (STT + exchange + brokerage, each leg)
  slippage  = 0.10  (assumed fill slippage per leg)
  Total round-trip cost = 2 × (0.05 + 0.10) = 0.30 %
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional

from PKDevTools.classes import Archiver
from PKDevTools.classes.PKDateUtilities import PKDateUtilities

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BACKTEST_SLIPPAGE_PCT      = 0.10   # assumed slippage per leg in forensic backtest
BACKTEST_FEE_PCT           = 0.05   # assumed fee per leg
SLIPPAGE_ALERT_MULT        = 2.0    # alert if live slippage > 2× backtest assumption

MAX_BAD_FILL_STRIKES       = 3      # disable symbol after N consecutive bad fills
BAD_FILL_THRESHOLD_PCT     = 0.25   # slippage > this is a "bad fill"

MAX_DAILY_LOSS_PCT         = 5.0    # halt strategy for the day above this drawdown
MAX_DAILY_TRADES           = 20     # hard cap per day to prevent runaway signals


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IntradayShortSignal:
    """Raw short signal received from the scanner."""
    symbol:          str
    signal_time:     str    # "HH:MM:SS"
    intended_entry:  float  # bid / last-traded price at signal time
    stop_loss:       float
    target:          float
    strategy:        str    # e.g. "gap_fade_short"
    avg_value_cr:    float  # liquidity at signal time
    spread:          float = 0.0   # ask - bid at signal time (0 if unknown)
    volume_ratio:    float = 1.0   # vol vs 20-day avg


@dataclass
class IntradayPaperTrade:
    """One simulated short trade from signal to exit."""
    # Identity
    date:             str
    symbol:           str
    strategy:         str

    # Signal info
    signal_time:      str
    intended_entry:   float
    stop_loss:        float
    target:           float
    volume_ratio:     float
    liquidity_at_signal_cr: float
    spread_at_signal: float

    # Shortability
    broker_would_allow: bool
    rejection_reason:   str

    # Simulated fill
    simulated_fill:   float = 0.0
    fill_slippage_pct: float = 0.0   # positive = worse than intended for shorts
    fill_quality:     str  = "pending"  # "good" | "slippage_alert" | "bad_fill" | "rejected"

    # Exit
    exit_price:       float = 0.0
    exit_time:        str   = ""
    exit_reason:      str   = ""       # "stop_hit" | "target_hit" | "eod" | "pending"

    # Outcome
    pnl_pct:          float = 0.0
    pnl_after_costs:  float = 0.0
    stop_outcome:     str   = "pending"   # "hit" | "escaped" | "pending"
    target_outcome:   str   = "pending"   # "hit" | "escaped" | "pending"
    max_adverse_move: float = 0.0  # worst intrabar move against position
    closed:           bool  = False


# ---------------------------------------------------------------------------
# Per-symbol circuit breaker
# ---------------------------------------------------------------------------

class SymbolCircuitBreaker:
    """Counts consecutive bad fills for a symbol; disables it after MAX_BAD_FILL_STRIKES."""

    def __init__(self) -> None:
        self._strikes: Dict[str, int] = {}
        self._disabled: Dict[str, str] = {}  # symbol → reason

    def record_fill(self, symbol: str, slippage_pct: float) -> None:
        if slippage_pct > BAD_FILL_THRESHOLD_PCT:
            self._strikes[symbol] = self._strikes.get(symbol, 0) + 1
            if self._strikes[symbol] >= MAX_BAD_FILL_STRIKES:
                self._disabled[symbol] = (
                    f"{MAX_BAD_FILL_STRIKES} consecutive bad fills "
                    f"(last slippage {slippage_pct:.2f}%)"
                )
                _log.warning(f"[SymbolCB] {symbol} disabled: {self._disabled[symbol]}")
        else:
            self._strikes[symbol] = 0   # reset on good fill

    def is_disabled(self, symbol: str) -> tuple[bool, str]:
        if symbol in self._disabled:
            return True, self._disabled[symbol]
        return False, ""

    def reset(self, symbol: str) -> None:
        self._strikes.pop(symbol, None)
        self._disabled.pop(symbol, None)

    def reset_all(self) -> None:
        self._strikes.clear()
        self._disabled.clear()


# ---------------------------------------------------------------------------
# Daily circuit breaker
# ---------------------------------------------------------------------------

class DailyCircuitBreaker:
    """Halts the strategy for the day when running P&L breaches MAX_DAILY_LOSS_PCT."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._date:          Optional[str] = None
        self._running_pnl:   float = 0.0
        self._trade_count:   int   = 0
        self._halted:        bool  = False
        self._halt_reason:   str   = ""

    def _check_date(self) -> None:
        today = date.today().isoformat()
        if self._date != today:
            self._date        = today
            self._running_pnl = 0.0
            self._trade_count = 0
            self._halted      = False
            self._halt_reason = ""

    def record_trade(self, pnl_after_costs: float) -> None:
        with self._lock:
            self._check_date()
            self._running_pnl  += pnl_after_costs
            self._trade_count  += 1
            if not self._halted:
                if self._running_pnl <= -MAX_DAILY_LOSS_PCT:
                    self._halted      = True
                    self._halt_reason = (
                        f"Daily loss {self._running_pnl:.2f}% breached "
                        f"{MAX_DAILY_LOSS_PCT}% cap after {self._trade_count} trades"
                    )
                    _log.warning(f"[DailyCB] Strategy HALTED: {self._halt_reason}")
                elif self._trade_count >= MAX_DAILY_TRADES:
                    self._halted      = True
                    self._halt_reason = f"Max daily trade count ({MAX_DAILY_TRADES}) reached"
                    _log.warning(f"[DailyCB] Strategy HALTED: {self._halt_reason}")

    def is_halted(self) -> tuple[bool, str]:
        with self._lock:
            self._check_date()
            return self._halted, self._halt_reason

    @property
    def running_pnl(self) -> float:
        with self._lock:
            self._check_date()
            return self._running_pnl

    @property
    def trade_count(self) -> int:
        with self._lock:
            self._check_date()
            return self._trade_count


# ---------------------------------------------------------------------------
# Main paper trader
# ---------------------------------------------------------------------------

class IntradayPaperTrader:
    """
    Intraday short-only paper trader.

    Typical flow per signal:
        1. accept_signal(signal, shortability_result)
           → checks shortability, circuit breakers, logs entry
        2. update_fill(symbol, next_bar_open, spread)
           → records simulated fill and slippage
        3. update_exit(symbol, exit_price, exit_time, exit_reason)
           → records P&L, updates circuit breakers
        4. close_day(eod_prices)
           → closes any still-open positions at EOD price
        5. get_today_trades() / load_trades_for_date()
           → used by the daily report generator
    """

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self._lock         = threading.RLock()  # reentrant — close_day holds lock then calls update_exit
        self._data_dir     = data_dir or os.path.join(Archiver.get_user_data_dir(), "intraday_paper")
        os.makedirs(self._data_dir, exist_ok=True)

        self._today:       Optional[str]                = None
        self._trades:      Dict[str, IntradayPaperTrade] = {}  # symbol → trade (one per symbol per day)
        self._symbol_cb    = SymbolCircuitBreaker()
        self._daily_cb     = DailyCircuitBreaker()
        self._slippage_alerts: List[str] = []

        self._today = self._now().strftime("%Y-%m-%d")
        self._load_day(self._today)

    # ------------------------------------------------------------------
    # Public: signal intake
    # ------------------------------------------------------------------

    def accept_signal(
        self,
        signal: IntradayShortSignal,
        shortability_allowed: bool,
        shortability_reason: str = "",
    ) -> tuple[bool, str]:
        """
        Attempt to accept a short signal into the paper book.

        Returns (accepted: bool, reason: str).
        Rejection reasons are logged but never raise exceptions.
        """
        sym = signal.symbol.strip().upper()
        now = self._now()
        today = now.strftime("%Y-%m-%d")

        with self._lock:
            self._roll_day_if_needed(today)

            if sym in self._trades:
                return False, f"{sym} already has an open paper trade today"

        # Circuit breaker: daily halt
        halted, halt_reason = self._daily_cb.is_halted()
        if halted:
            return False, f"Daily circuit breaker active: {halt_reason}"

        # Circuit breaker: symbol disabled
        sym_disabled, sym_reason = self._symbol_cb.is_disabled(sym)
        if sym_disabled:
            return False, f"Symbol circuit breaker: {sym_reason}"

        # Shortability gate
        if not shortability_allowed:
            reason = shortability_reason or f"{sym} broker/surveillance block"
            return False, reason

        # Accept
        trade = IntradayPaperTrade(
            date=today,
            symbol=sym,
            strategy=signal.strategy,
            signal_time=signal.signal_time,
            intended_entry=signal.intended_entry,
            stop_loss=signal.stop_loss,
            target=signal.target,
            volume_ratio=signal.volume_ratio,
            liquidity_at_signal_cr=signal.avg_value_cr,
            spread_at_signal=signal.spread,
            broker_would_allow=True,
            rejection_reason="",
        )

        with self._lock:
            self._trades[sym] = trade
            self._persist_day(today)

        _log.debug(
            f"[PaperTrader] Accepted SHORT {sym} @ ₹{signal.intended_entry:.2f} "
            f"SL={signal.stop_loss:.2f} T={signal.target:.2f} [{signal.strategy}]"
        )
        return True, "accepted"

    # ------------------------------------------------------------------
    # Public: fill simulation
    # ------------------------------------------------------------------

    def update_fill(
        self,
        symbol: str,
        next_bar_open: float,
        spread: float = 0.0,
    ) -> None:
        """
        Record simulated fill for an accepted signal.

        For a short, pessimistic fill = next bar open + half the spread
        (you're selling into the ask, not the mid).
        """
        sym = symbol.strip().upper()
        with self._lock:
            trade = self._trades.get(sym)
            if not trade or trade.simulated_fill > 0:
                return

            half_spread = (spread or trade.spread_at_signal) / 2
            fill = next_bar_open + half_spread  # pessimistic: costs you more to enter short

            # Slippage: positive means fill was worse than intended
            slippage_pct = (fill - trade.intended_entry) / trade.intended_entry * 100

            trade.simulated_fill    = round(fill, 2)
            trade.fill_slippage_pct = round(slippage_pct, 4)

            if slippage_pct > SLIPPAGE_ALERT_MULT * BACKTEST_SLIPPAGE_PCT:
                trade.fill_quality = "slippage_alert"
                alert = (
                    f"SLIPPAGE ALERT {sym}: live {slippage_pct:.2f}% vs "
                    f"backtest assumed {BACKTEST_SLIPPAGE_PCT:.2f}% "
                    f"(threshold {SLIPPAGE_ALERT_MULT}×)"
                )
                self._slippage_alerts.append(alert)
                _log.warning(f"[PaperTrader] {alert}")
            elif slippage_pct > BAD_FILL_THRESHOLD_PCT:
                trade.fill_quality = "bad_fill"
            else:
                trade.fill_quality = "good"

            self._symbol_cb.record_fill(sym, slippage_pct)
            self._persist_day(self._today or trade.date)

    # ------------------------------------------------------------------
    # Public: exit recording
    # ------------------------------------------------------------------

    def update_exit(
        self,
        symbol: str,
        exit_price: float,
        exit_time: str,
        exit_reason: str,
        day_high: float = 0.0,
        day_low: float = 0.0,
    ) -> None:
        """Record exit and close the trade."""
        sym = symbol.strip().upper()
        with self._lock:
            trade = self._trades.get(sym)
            if not trade or trade.closed:
                return

            entry = trade.simulated_fill or trade.intended_entry
            if entry <= 0:
                return

            trade.exit_price   = exit_price
            trade.exit_time    = exit_time
            trade.exit_reason  = exit_reason

            # P&L for short: profit when price falls
            pnl_pct           = (entry - exit_price) / entry * 100
            cost_pct          = 2 * (BACKTEST_FEE_PCT + BACKTEST_SLIPPAGE_PCT)
            pnl_after_costs   = pnl_pct - cost_pct

            trade.pnl_pct          = round(pnl_pct, 4)
            trade.pnl_after_costs  = round(pnl_after_costs, 4)

            # Stop / target outcome
            if day_high > 0:
                trade.max_adverse_move = round((day_high - entry) / entry * 100, 4)
            if trade.stop_loss > 0 and day_high >= trade.stop_loss:
                trade.stop_outcome = "hit"
            else:
                trade.stop_outcome = "escaped"

            if trade.target > 0 and day_low <= trade.target:
                trade.target_outcome = "hit"
            else:
                trade.target_outcome = "escaped"

            trade.closed = True
            self._persist_day(self._today or trade.date)

        # Update daily circuit breaker
        self._daily_cb.record_trade(pnl_after_costs)

    def close_day(self, eod_prices: Dict[str, float]) -> str:
        """
        Close all still-open paper trades at EOD price.
        Call at ~3:10 PM.  Returns a summary string.
        """
        with self._lock:
            today = self._now().strftime("%Y-%m-%d")
            self._roll_day_if_needed(today)
            if not self._trades:
                return ""

            price_map = {str(k).strip().upper(): float(v) for k, v in (eod_prices or {}).items()}
            closed_now = 0
            total_pnl  = 0.0
            winners    = 0

            for sym, trade in self._trades.items():
                if trade.closed:
                    continue
                eod = price_map.get(sym, 0.0)
                if eod <= 0:
                    continue
                self.update_exit(sym, eod, "15:10:00", "eod")
                closed_now += 1
                total_pnl  += trade.pnl_after_costs
                if trade.pnl_after_costs > 0:
                    winners += 1

        if closed_now == 0:
            return ""

        wr  = winners / closed_now * 100
        avg = total_pnl / closed_now
        return (
            f"\n**Short Paper — EOD Close {today}**\n"
            f"Trades: {closed_now} | WR: {wr:.0f}% | Avg P&L (after costs): {avg:+.2f}%\n"
        )

    # ------------------------------------------------------------------
    # Public: queries
    # ------------------------------------------------------------------

    def get_today_trades(self) -> List[IntradayPaperTrade]:
        with self._lock:
            return list(self._trades.values())

    def get_slippage_alerts(self) -> List[str]:
        return list(self._slippage_alerts)

    def get_daily_status(self) -> Dict:
        halted, halt_reason = self._daily_cb.is_halted()
        return {
            "date":            self._today,
            "trade_count":     self._daily_cb.trade_count,
            "running_pnl":     self._daily_cb.running_pnl,
            "halted":          halted,
            "halt_reason":     halt_reason,
            "slippage_alerts": len(self._slippage_alerts),
        }

    def load_trades_for_date(self, date_str: str) -> List[dict]:
        """Load trades from a saved CSV for any past date."""
        path = self._csv_path(date_str)
        if not os.path.isfile(path):
            return []
        rows = []
        try:
            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception as exc:
            _log.warning(f"[PaperTrader] load_trades_for_date failed: {exc}")
        return rows

    def load_recent_trades(self, days: int = 20) -> List[dict]:
        """Load trades from the last *days* CSV files (most recent first)."""
        import glob
        files = sorted(
            glob.glob(os.path.join(self._data_dir, "intraday_paper_*.csv")),
            reverse=True,
        )[:days]
        all_rows: List[dict] = []
        for fp in files:
            try:
                with open(fp) as f:
                    all_rows.extend(list(csv.DictReader(f)))
            except Exception:
                continue
        return all_rows

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        try:
            return PKDateUtilities.currentDateTime()
        except Exception:
            return datetime.now()

    def _roll_day_if_needed(self, today: str) -> None:
        if self._today != today:
            if self._today and self._trades:
                self._persist_day(self._today)
            self._trades.clear()
            self._today = today
            self._symbol_cb.reset_all()
            self._slippage_alerts.clear()
            self._load_day(today)

    def _csv_path(self, date_str: str) -> str:
        return os.path.join(self._data_dir, f"intraday_paper_{date_str}.csv")

    _FIELDS = [
        "date", "symbol", "strategy", "signal_time", "intended_entry",
        "stop_loss", "target", "volume_ratio", "liquidity_at_signal_cr",
        "spread_at_signal", "broker_would_allow", "rejection_reason",
        "simulated_fill", "fill_slippage_pct", "fill_quality",
        "exit_price", "exit_time", "exit_reason",
        "pnl_pct", "pnl_after_costs",
        "stop_outcome", "target_outcome", "max_adverse_move", "closed",
    ]

    def _persist_day(self, date_str: str) -> None:
        if not date_str or not self._trades:
            return
        try:
            with open(self._csv_path(date_str), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=self._FIELDS)
                w.writeheader()
                for t in self._trades.values():
                    w.writerow({k: getattr(t, k) for k in self._FIELDS})
        except Exception as exc:
            _log.warning(f"[PaperTrader] persist failed: {exc}")

    def _load_day(self, date_str: str) -> None:
        path = self._csv_path(date_str)
        if not os.path.isfile(path):
            return
        try:
            with open(path) as f:
                for row in csv.DictReader(f):
                    sym = str(row.get("symbol", "") or "").strip().upper()
                    if not sym:
                        continue
                    self._trades[sym] = IntradayPaperTrade(
                        date=str(row.get("date", date_str)),
                        symbol=sym,
                        strategy=str(row.get("strategy", "")),
                        signal_time=str(row.get("signal_time", "")),
                        intended_entry=_f(row.get("intended_entry")),
                        stop_loss=_f(row.get("stop_loss")),
                        target=_f(row.get("target")),
                        volume_ratio=_f(row.get("volume_ratio")),
                        liquidity_at_signal_cr=_f(row.get("liquidity_at_signal_cr")),
                        spread_at_signal=_f(row.get("spread_at_signal")),
                        broker_would_allow=_b(row.get("broker_would_allow")),
                        rejection_reason=str(row.get("rejection_reason", "")),
                        simulated_fill=_f(row.get("simulated_fill")),
                        fill_slippage_pct=_f(row.get("fill_slippage_pct")),
                        fill_quality=str(row.get("fill_quality", "pending")),
                        exit_price=_f(row.get("exit_price")),
                        exit_time=str(row.get("exit_time", "")),
                        exit_reason=str(row.get("exit_reason", "")),
                        pnl_pct=_f(row.get("pnl_pct")),
                        pnl_after_costs=_f(row.get("pnl_after_costs")),
                        stop_outcome=str(row.get("stop_outcome", "pending")),
                        target_outcome=str(row.get("target_outcome", "pending")),
                        max_adverse_move=_f(row.get("max_adverse_move")),
                        closed=_b(row.get("closed")),
                    )
            _log.debug(f"[PaperTrader] Loaded {len(self._trades)} trades from {path}")
        except Exception as exc:
            _log.warning(f"[PaperTrader] load_day failed: {exc}")


# ---------------------------------------------------------------------------
# Tiny parsing helpers (no deps)
# ---------------------------------------------------------------------------

def _f(v) -> float:
    try:
        return 0.0 if v in (None, "", "None") else float(v)
    except Exception:
        return 0.0


def _b(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1", "true", "yes", "y"}
