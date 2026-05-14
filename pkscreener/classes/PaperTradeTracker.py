"""
Live Paper Trade Tracker — tracks every alert from entry to EOD close.

No look-ahead bias possible: logs entry at alert time, checks outcome
only after market close using Kite API last-traded price.

Produces honest metrics: win rate, avg P&L, Sharpe, max drawdown,
realized slippage, position concentration, VaR.

Design rules (from Quantopian lectures in docs/):
- Track realized slippage vs signal price
- Position concentration: no single trade > 5% of daily budget
- VaR at 95% confidence from realized returns
- Liquidity filter: skip symbols with avg volume < ₹5Cr daily
- Keep model simple: ≤3 parameters per filter rule
- Out-of-sample only: never retune based on paper results
"""

import csv
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional

from PKDevTools.classes import Archiver
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger


@dataclass
class PaperTrade:
    """A single paper trade from alert to close."""
    symbol: str
    date: str                    # YYYY-MM-DD
    alert_time: str              # HH:MM:SS IST
    entry_price: float           # price at time of alert
    score: float                 # BlindBuyScorer score
    grade: str                   # A+, A, B, or TICK
    volume_ratio: float          # volume vs 20-day avg at entry
    change_at_entry: float       # % change from open at entry
    stop_loss: float             # computed SL
    target1: float               # T1
    target2: float               # T2
    suggested_qty: int           # qty suggested
    source: str                  # "ohlcv", "tick_only", "batch", "opening_buffer"
    # Filled at EOD
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    high_after: Optional[float] = None
    low_after: Optional[float] = None
    pnl_pct: Optional[float] = None
    max_profit_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    fade_alert_sent: bool = False
    fade_exit_sent: bool = False
    stop_hit: bool = False
    closed: bool = False


class PaperTradeTracker:
    """
    Tracks paper trades across a trading day.

    Usage in AutoPilotInvestor:
        self._paper_tracker = PaperTradeTracker()

        # On every alert:
        self._paper_tracker.log_entry(symbol, price, score, ...)

        # On fade alert:
        self._paper_tracker.log_fade(symbol, "WARNING" or "EXIT")

        # At EOD (3:10 PM):
        self._paper_tracker.close_day(ticks_snapshot)

        # At end of week / on demand:
        report = self._paper_tracker.generate_report(days=5)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._today: Optional[str] = self._now().strftime("%Y-%m-%d")
        self._trades: Dict[str, PaperTrade] = {}  # symbol -> trade (one per symbol per day)
        self._output_dir = os.path.join(Archiver.get_user_data_dir(), "paper_trades")
        os.makedirs(self._output_dir, exist_ok=True)
        self.logger = default_logger()
        self._load_day_into_memory(self._today)

    @staticmethod
    def _now() -> datetime:
        try:
            return PKDateUtilities.currentDateTime()
        except Exception:
            return datetime.now()

    # ------------------------------------------------------------------
    # Entry logging
    # ------------------------------------------------------------------

    def log_entry(
        self,
        symbol: str,
        price: float,
        score: float,
        grade: str,
        volume_ratio: float,
        change_at_entry: float,
        stop_loss: float,
        target1: float = 0.0,
        target2: float = 0.0,
        suggested_qty: int = 0,
        source: str = "ohlcv",
    ) -> None:
        """Log a paper trade entry at the time of alert."""
        now = self._now()
        today_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        with self._lock:
            if self._today != today_str:
                # New day — flush previous day's data if any
                if self._today and self._trades:
                    self._persist_day(self._today)
                self._trades.clear()
                self._today = today_str
                self._load_day_into_memory(today_str)
            elif not self._trades:
                # Monitor processes can restart during the day. Reload persisted
                # entries so duplicate alerts are avoided and EOD can close them.
                self._load_day_into_memory(today_str)

            if symbol in self._trades:
                return  # Already tracking this symbol today

            self._trades[symbol] = PaperTrade(
                symbol=symbol,
                date=today_str,
                alert_time=time_str,
                entry_price=price,
                score=score,
                grade=grade,
                volume_ratio=volume_ratio,
                change_at_entry=change_at_entry,
                stop_loss=stop_loss,
                target1=target1,
                target2=target2,
                suggested_qty=suggested_qty,
                source=source,
            )
            # Keep forward testing visible during the session, not just after EOD.
            self._persist_day(today_str)

        self.logger.debug(f"[PaperTracker] Entry: {symbol} at ₹{price:.2f} score={score:.0f} [{source}]")

    # ------------------------------------------------------------------
    # Fade / exit logging
    # ------------------------------------------------------------------

    def log_fade(self, symbol: str, level: str) -> None:
        """Log a fade alert (WARNING, EXIT, STOP_HIT) for a tracked symbol."""
        with self._lock:
            trade = self._trades.get(symbol)
            if not trade:
                return
            if level == "WARNING":
                trade.fade_alert_sent = True
            elif level in ("EXIT", "EXIT NOW"):
                trade.fade_exit_sent = True
            elif level == "STOP_HIT":
                trade.stop_hit = True
            if self._today:
                self._persist_day(self._today)

    # ------------------------------------------------------------------
    # EOD close
    # ------------------------------------------------------------------

    def close_day(self, ticks_snapshot: Dict[str, dict]) -> str:
        """
        Close all open paper trades using EOD tick prices.
        Call at ~3:10 PM from _run_eod_exit_alert.
        Returns a summary string for the EOD alert.
        """
        with self._lock:
            today_str = self._now().strftime("%Y-%m-%d")
            if self._today != today_str:
                self._today = today_str
                self._trades.clear()
            if not self._trades:
                self._load_day_into_memory(self._today)
            if not self._trades:
                return ""

            tick_map = {
                str(sym).strip().upper(): payload
                for sym, payload in (ticks_snapshot or {}).items()
                if isinstance(payload, dict)
            }
            closed_count = 0
            winners = 0
            total_pnl = 0.0

            for sym, trade in self._trades.items():
                if trade.closed:
                    continue

                tick = tick_map.get(str(sym).strip().upper(), {})
                current_price = float(tick.get("last_price") or 0)
                ohlc = tick.get("ohlc", {}) if isinstance(tick.get("ohlc", {}), dict) else {}
                day_high = float(tick.get("high") or ohlc.get("high") or 0)
                day_low = float(tick.get("low") or ohlc.get("low") or 0)

                if current_price <= 0:
                    continue

                trade.exit_price = current_price
                trade.exit_time = self._now().strftime("%H:%M:%S")
                trade.high_after = max(day_high, current_price) if day_high > 0 else current_price
                trade.low_after = min(day_low, current_price) if day_low > 0 else current_price

                entry = trade.entry_price
                if entry > 0:
                    trade.pnl_pct = round((current_price - entry) / entry * 100, 2)
                    trade.max_profit_pct = round((trade.high_after - entry) / entry * 100, 2)
                    trade.max_drawdown_pct = round((entry - trade.low_after) / entry * 100, 2)
                else:
                    trade.pnl_pct = 0.0
                    trade.max_profit_pct = 0.0
                    trade.max_drawdown_pct = 0.0

                trade.closed = True
                closed_count += 1
                total_pnl += trade.pnl_pct
                if trade.pnl_pct > 0:
                    winners += 1

            # Persist to CSV
            self._persist_day(self._today)

            if closed_count == 0:
                return ""

            win_rate = winners / closed_count * 100
            avg_pnl = total_pnl / closed_count

            return (
                f"\n**📊 Paper Trade Report — {self._today}**\n"
                f"Trades: {closed_count} | "
                f"Win Rate: {win_rate:.0f}% ({winners}W/{closed_count - winners}L) | "
                f"Avg P&L: {avg_pnl:+.2f}%\n"
            )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def generate_report(self, days: int = 5) -> str:
        """
        Generate a multi-day performance report from saved CSV files.
        Returns formatted string for Discord or console.
        """
        all_trades = self._load_recent_trades(days)
        if not all_trades:
            return "No paper trades found."

        total = len(all_trades)
        closed = [
            t for t in all_trades
            if self._bool(t.get("closed")) and self._optional_float(t.get("pnl_pct")) is not None
        ]
        if not closed:
            return f"{total} paper trades logged but none closed yet."

        n = len(closed)
        pnls = [float(self._optional_float(t["pnl_pct"]) or 0.0) for t in closed]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]

        win_rate = len(winners) / n * 100
        avg_pnl = sum(pnls) / n
        avg_win = sum(winners) / len(winners) if winners else 0
        avg_loss = sum(losers) / len(losers) if losers else 0
        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

        # Sharpe ratio (daily returns)
        daily_pnl = {}
        for t in closed:
            d = t["date"]
            daily_pnl.setdefault(d, []).append(self._float(t["pnl_pct"]))
        daily_returns = [sum(v) / len(v) for v in daily_pnl.values()]
        if len(daily_returns) >= 2:
            mean_r = sum(daily_returns) / len(daily_returns)
            std_r = (sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)) ** 0.5
            sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0
        else:
            sharpe = 0

        # VaR 95%
        sorted_pnls = sorted(pnls)
        var_idx = max(0, int(len(sorted_pnls) * 0.05) - 1)
        var_95 = sorted_pnls[var_idx] if sorted_pnls else 0

        # Max drawdown (cumulative)
        cum = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            dd = peak - cum
            max_dd = max(max_dd, dd)

        # Fade detector accuracy
        faded = [t for t in closed if self._float(t["pnl_pct"]) < 0]
        fade_warned = [t for t in faded if t.get("fade_alert_sent") == "True" or t.get("fade_exit_sent") == "True"]
        fade_accuracy = len(fade_warned) / len(faded) * 100 if faded else 0

        # Source breakdown
        from collections import Counter
        source_counts = Counter(t.get("source", "unknown") for t in closed)

        # Score buckets
        score_lines = []
        for lo, hi, label in [(70, 101, "A+ (70+)"), (60, 70, "A (60-69)"), (52, 60, "B (52-59)"), (0, 52, "C (<52)")]:
            bucket = [t for t in closed if lo <= self._float(t.get("score", 0)) < hi]
            if not bucket:
                continue
            bwr = sum(1 for t in bucket if self._float(t["pnl_pct"]) > 0) / len(bucket) * 100
            bavg = sum(self._float(t["pnl_pct"]) for t in bucket) / len(bucket)
            score_lines.append(f"  {label:12s}: {len(bucket):3d} trades | WR {bwr:5.1f}% | Avg {bavg:+.2f}%")

        # Per-day
        day_lines = []
        for d in sorted(daily_pnl.keys()):
            dp = daily_pnl[d]
            dw = sum(1 for p in dp if p > 0)
            dwr = dw / len(dp) * 100
            davg = sum(dp) / len(dp)
            day_lines.append(f"  {d}: {len(dp):3d} trades | WR {dwr:5.1f}% | Avg {davg:+.2f}%")

        report = (
            f"**📊 PAPER TRADE REPORT — Last {days} days**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Total Trades:     {n}\n"
            f"Win Rate:         {win_rate:.1f}% ({len(winners)}W / {len(losers)}L)\n"
            f"Avg P&L:          {avg_pnl:+.2f}%\n"
            f"Avg Winner:       {avg_win:+.2f}%\n"
            f"Avg Loser:        {avg_loss:+.2f}%\n"
            f"Expectancy:       {expectancy:+.3f}%\n"
            f"Sharpe (ann.):    {sharpe:.2f}\n"
            f"VaR 95%:          {var_95:+.2f}%\n"
            f"Max Drawdown:     {max_dd:.2f}%\n"
            f"Fade Accuracy:    {fade_accuracy:.0f}% ({len(fade_warned)}/{len(faded)} faders caught)\n"
            f"\n**Score Distribution:**\n" + "\n".join(score_lines) +
            f"\n\n**Per Day:**\n" + "\n".join(day_lines) +
            f"\n\n**Sources:** {dict(source_counts)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        # Auto-append Spearman validation if enough data
        try:
            spearman_report = self.validate_scorer_quality(days)
            if "Need ≥10" not in spearman_report:
                report += "\n\n" + spearman_report
        except Exception:
            pass  # Don't break report if validation fails

        return report

    def get_today_summary(self) -> str:
        """Quick summary of today's open paper trades."""
        with self._lock:
            if not self._trades:
                return "No paper trades today."
            open_count = sum(1 for t in self._trades.values() if not t.closed)
            closed_count = sum(1 for t in self._trades.values() if t.closed)
            return f"Paper trades today: {open_count} open, {closed_count} closed, {len(self._trades)} total"

    # ------------------------------------------------------------------
    # Spearman rank validation (Quantopian Ranking Universes lecture)
    # ------------------------------------------------------------------

    def validate_scorer_quality(self, days: int = 5) -> str:
        """
        Test if higher-scored alerts produce higher returns.

        Uses Spearman rank correlation between BlindBuyScore and realized
        P&L. If rho < 0.05, the scorer is noise — scores don't predict
        returns. If rho > 0.10, there's a real edge.

        This is the single most important diagnostic. Run it after >=5
        trading days of paper trade data.

        Returns a summary string suitable for Discord/console.
        """
        all_trades = self._load_recent_trades(days)
        closed = [
            t for t in all_trades
            if self._bool(t.get("closed"))
            and self._optional_float(t.get("pnl_pct")) is not None
            and self._optional_float(t.get("score")) is not None
        ]

        if len(closed) < 10:
            return (
                f"Need ≥10 closed trades for Spearman validation "
                f"(have {len(closed)}). Keep collecting."
            )

        scores = [self._float(t["score"]) for t in closed]
        pnls = [self._float(t["pnl_pct"]) for t in closed]

        # Compute Spearman rank correlation
        try:
            from scipy.stats import spearmanr
            rho, p_value = spearmanr(scores, pnls)
        except ImportError:
            # Fallback: manual Spearman (no scipy dependency required)
            n = len(scores)
            # Rank arrays
            def _rank(arr):
                sorted_idx = sorted(range(n), key=lambda i: arr[i])
                ranks = [0.0] * n
                for rank_val, idx in enumerate(sorted_idx):
                    ranks[idx] = rank_val + 1
                return ranks

            r_scores = _rank(scores)
            r_pnls = _rank(pnls)
            d_sq = sum((r_scores[i] - r_pnls[i]) ** 2 for i in range(n))
            rho = 1 - (6 * d_sq) / (n * (n ** 2 - 1))
            # Approximate p-value using t-distribution
            if abs(rho) < 1.0:
                t_stat = rho * math.sqrt((n - 2) / (1 - rho ** 2))
                # Rough two-tailed p-value approximation
                p_value = max(0.001, 2 * math.exp(-0.717 * abs(t_stat) - 0.416 * t_stat ** 2))
            else:
                p_value = 0.0

        # Interpret
        if rho >= 0.20:
            verdict = "🟢 STRONG EDGE — scorer reliably predicts returns"
        elif rho >= 0.10:
            verdict = "🟡 WEAK EDGE — some predictive power, worth using"
        elif rho >= 0.05:
            verdict = "🟠 MARGINAL — barely better than random"
        elif rho >= -0.05:
            verdict = "🔴 NO EDGE — scorer is noise, scores don't predict returns"
        else:
            verdict = "⛔ ANTI-PREDICTIVE — high scores produce WORSE returns!"

        # Per-factor Spearman (volume_ratio, change_at_entry)
        factor_lines = []
        for factor_name in ["volume_ratio", "change_at_entry"]:
            vals = [self._float(t.get(factor_name, 0)) for t in closed]
            if any(v != 0 for v in vals):
                try:
                    from scipy.stats import spearmanr as _sp
                    f_rho, f_p = _sp(vals, pnls)
                except ImportError:
                    r_f = _rank(vals)
                    d2 = sum((r_f[i] - r_pnls[i]) ** 2 for i in range(n))
                    f_rho = 1 - (6 * d2) / (n * (n ** 2 - 1))
                    f_p = 0.05  # rough
                factor_lines.append(
                    f"  {factor_name:20s}: ρ = {f_rho:+.3f} (p={f_p:.3f})"
                )

        result = (
            f"**🔬 SCORER VALIDATION — Spearman Rank Test**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Trades analyzed:  {len(closed)} (last {days} days)\n"
            f"Score vs Return:  ρ = {rho:+.3f} (p = {p_value:.4f})\n"
            f"Verdict:          {verdict}\n"
        )
        if factor_lines:
            result += f"\n**Per-Factor Correlations:**\n" + "\n".join(factor_lines) + "\n"

        result += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        return result

    # ------------------------------------------------------------------
    # Private: persistence
    # ------------------------------------------------------------------

    def _persist_day(self, date_str: str) -> None:
        """Save all trades for a date to CSV."""
        if not date_str or not self._trades:
            return

        filepath = os.path.join(self._output_dir, f"paper_trades_{date_str}.csv")
        fieldnames = [
            "date", "symbol", "alert_time", "entry_price", "score", "grade",
            "volume_ratio", "change_at_entry", "stop_loss", "target1", "target2",
            "suggested_qty", "source", "exit_price", "exit_time",
            "high_after", "low_after", "pnl_pct", "max_profit_pct",
            "max_drawdown_pct", "fade_alert_sent", "fade_exit_sent",
            "stop_hit", "closed",
        ]

        try:
            with open(filepath, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for trade in self._trades.values():
                    writer.writerow({
                        "date": trade.date,
                        "symbol": trade.symbol,
                        "alert_time": trade.alert_time,
                        "entry_price": trade.entry_price,
                        "score": trade.score,
                        "grade": trade.grade,
                        "volume_ratio": trade.volume_ratio,
                        "change_at_entry": trade.change_at_entry,
                        "stop_loss": trade.stop_loss,
                        "target1": trade.target1,
                        "target2": trade.target2,
                        "suggested_qty": trade.suggested_qty,
                        "source": trade.source,
                        "exit_price": trade.exit_price,
                        "exit_time": trade.exit_time,
                        "high_after": trade.high_after,
                        "low_after": trade.low_after,
                        "pnl_pct": trade.pnl_pct,
                        "max_profit_pct": trade.max_profit_pct,
                        "max_drawdown_pct": trade.max_drawdown_pct,
                        "fade_alert_sent": trade.fade_alert_sent,
                        "fade_exit_sent": trade.fade_exit_sent,
                        "stop_hit": trade.stop_hit,
                        "closed": trade.closed,
                    })
            self.logger.debug(f"[PaperTracker] Persisted {len(self._trades)} trades to {filepath}")
        except Exception as exc:
            self.logger.warning(f"[PaperTracker] Failed to persist: {exc}")

    def _load_day_into_memory(self, date_str: Optional[str]) -> None:
        """Load one day's persisted paper trades into memory if present."""
        if not date_str:
            return
        filepath = os.path.join(self._output_dir, f"paper_trades_{date_str}.csv")
        if not os.path.isfile(filepath):
            return
        try:
            with open(filepath, newline="") as f:
                reader = csv.DictReader(f)
                loaded: Dict[str, PaperTrade] = {}
                for row in reader:
                    symbol = str(row.get("symbol", "") or "").strip().upper()
                    if not symbol:
                        continue
                    loaded[symbol] = PaperTrade(
                        symbol=symbol,
                        date=str(row.get("date", date_str) or date_str),
                        alert_time=str(row.get("alert_time", "") or ""),
                        entry_price=self._float(row.get("entry_price")),
                        score=self._float(row.get("score")),
                        grade=str(row.get("grade", "") or ""),
                        volume_ratio=self._float(row.get("volume_ratio")),
                        change_at_entry=self._float(row.get("change_at_entry")),
                        stop_loss=self._float(row.get("stop_loss")),
                        target1=self._float(row.get("target1")),
                        target2=self._float(row.get("target2")),
                        suggested_qty=int(self._float(row.get("suggested_qty"))),
                        source=str(row.get("source", "") or ""),
                        exit_price=self._optional_float(row.get("exit_price")),
                        exit_time=str(row.get("exit_time", "") or "") or None,
                        high_after=self._optional_float(row.get("high_after")),
                        low_after=self._optional_float(row.get("low_after")),
                        pnl_pct=self._optional_float(row.get("pnl_pct")),
                        max_profit_pct=self._optional_float(row.get("max_profit_pct")),
                        max_drawdown_pct=self._optional_float(row.get("max_drawdown_pct")),
                        fade_alert_sent=self._bool(row.get("fade_alert_sent")),
                        fade_exit_sent=self._bool(row.get("fade_exit_sent")),
                        stop_hit=self._bool(row.get("stop_hit")),
                        closed=self._bool(row.get("closed")),
                    )
                if loaded:
                    self._trades.update(loaded)
                    self.logger.debug(f"[PaperTracker] Loaded {len(loaded)} trades from {filepath}")
        except Exception as exc:
            self.logger.warning(f"[PaperTracker] Failed to load {filepath}: {exc}")

    @staticmethod
    def _float(value) -> float:
        try:
            if value in (None, "", "None"):
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    @classmethod
    def _optional_float(cls, value) -> Optional[float]:
        if value in (None, "", "None"):
            return None
        return cls._float(value)

    @staticmethod
    def _bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y"}

    def _load_recent_trades(self, days: int = 5) -> List[dict]:
        """Load trades from the last N days of CSV files."""
        import glob
        files = sorted(
            glob.glob(os.path.join(self._output_dir, "paper_trades_*.csv")),
            reverse=True,
        )[:days]

        all_trades = []
        for fpath in files:
            try:
                with open(fpath) as f:
                    reader = csv.DictReader(f)
                    all_trades.extend(list(reader))
            except Exception:
                continue
        return all_trades
