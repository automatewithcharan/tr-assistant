"""Paper-only ranked confirmation lane for Grade-B intraday alerts.

This module never imports or calls the broker order manager. It observes live
ticks after an alert, requires evidence to persist, ranks eligible candidates,
and records simulated entries/exits with conservative costs.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PKDevTools.classes.PKDateUtilities import PKDateUtilities


def _float(value: Any) -> float:
    try:
        if value in (None, "", "None"):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class RankedPaperConfig:
    budget_per_trade: float = 50_000.0
    max_open_positions: int = 2
    max_trades_per_day: int = 5
    max_daily_loss_amount: float = 1_000.0
    max_consecutive_losses: int = 2
    entry_window_seconds: int = 15 * 60
    min_observation_seconds: int = 60
    min_observations: int = 4
    min_confirm_pct: float = 0.15
    max_entry_extension_pct: float = 0.45
    min_open_move_pct: float = 0.50
    max_open_move_pct: float = 3.00
    min_day_change_pct: float = 0.50
    max_day_change_pct: float = 5.00
    max_pullback_pct: float = 0.75
    min_volume_growth_pct: float = 0.50
    min_current_bs_ratio: float = 1.50
    min_median_bs_ratio: float = 1.30
    min_bs_ratio: float = 0.90
    max_spread_bps: float = 20.0
    target_pct: float = 1.00
    stop_pct: float = 0.75
    fast_failure_age_seconds: int = 180
    fast_failure_drop_pct: float = 0.35
    fast_failure_sell_ratio: float = 1.30
    fee_pct_per_side: float = 0.05
    slippage_bps_per_side: float = 5.0
    latest_entry_minutes: int = 14 * 60 + 45
    eod_exit_minutes: int = 15 * 60 + 10

    @classmethod
    def from_env(cls) -> "RankedPaperConfig":
        def f(name: str, default: float) -> float:
            return _float(os.environ.get(name, default)) or default

        def i(name: str, default: int) -> int:
            return max(0, int(f(name, float(default))))

        return cls(
            budget_per_trade=f("SIGNALMONITOR_RANKED_PAPER_BUDGET", 50_000.0),
            max_open_positions=i("SIGNALMONITOR_RANKED_PAPER_MAX_OPEN", 2),
            max_trades_per_day=i("SIGNALMONITOR_RANKED_PAPER_MAX_TRADES", 5),
            max_daily_loss_amount=f("SIGNALMONITOR_RANKED_PAPER_MAX_DAILY_LOSS", 1_000.0),
            max_consecutive_losses=i("SIGNALMONITOR_RANKED_PAPER_MAX_CONSECUTIVE_LOSSES", 2),
            entry_window_seconds=i("SIGNALMONITOR_RANKED_PAPER_ENTRY_WINDOW_SECONDS", 900),
            min_observation_seconds=i("SIGNALMONITOR_RANKED_PAPER_MIN_OBSERVATION_SECONDS", 60),
            min_observations=i("SIGNALMONITOR_RANKED_PAPER_MIN_OBSERVATIONS", 4),
            min_confirm_pct=f("SIGNALMONITOR_RANKED_PAPER_MIN_CONFIRM_PCT", 0.15),
            max_entry_extension_pct=f("SIGNALMONITOR_RANKED_PAPER_MAX_EXTENSION_PCT", 0.45),
            min_open_move_pct=f("SIGNALMONITOR_RANKED_PAPER_MIN_OPEN_MOVE_PCT", 0.50),
            max_open_move_pct=f("SIGNALMONITOR_RANKED_PAPER_MAX_OPEN_MOVE_PCT", 3.00),
            min_day_change_pct=f("SIGNALMONITOR_RANKED_PAPER_MIN_DAY_CHANGE_PCT", 0.50),
            max_day_change_pct=f("SIGNALMONITOR_RANKED_PAPER_MAX_DAY_CHANGE_PCT", 5.00),
            max_pullback_pct=f("SIGNALMONITOR_RANKED_PAPER_MAX_PULLBACK_PCT", 0.75),
            min_volume_growth_pct=f("SIGNALMONITOR_RANKED_PAPER_MIN_VOLUME_GROWTH_PCT", 0.50),
            min_current_bs_ratio=f("SIGNALMONITOR_RANKED_PAPER_MIN_CURRENT_BS", 1.50),
            min_median_bs_ratio=f("SIGNALMONITOR_RANKED_PAPER_MIN_MEDIAN_BS", 1.30),
            min_bs_ratio=f("SIGNALMONITOR_RANKED_PAPER_MIN_BS", 0.90),
            max_spread_bps=f("SIGNALMONITOR_RANKED_PAPER_MAX_SPREAD_BPS", 20.0),
            target_pct=f("SIGNALMONITOR_RANKED_PAPER_TARGET_PCT", 1.00),
            stop_pct=f("SIGNALMONITOR_RANKED_PAPER_STOP_PCT", 0.75),
            fast_failure_age_seconds=i("SIGNALMONITOR_RANKED_PAPER_FAST_FAILURE_AGE_SECONDS", 180),
            fast_failure_drop_pct=f("SIGNALMONITOR_RANKED_PAPER_FAST_FAILURE_DROP_PCT", 0.35),
            fast_failure_sell_ratio=f("SIGNALMONITOR_RANKED_PAPER_FAST_FAILURE_SELL_RATIO", 1.30),
            fee_pct_per_side=f("SIGNALMONITOR_RANKED_PAPER_FEE_PCT_PER_SIDE", 0.05),
            slippage_bps_per_side=f("SIGNALMONITOR_RANKED_PAPER_SLIPPAGE_BPS", 5.0),
            latest_entry_minutes=i("SIGNALMONITOR_RANKED_PAPER_LATEST_ENTRY_MINUTES", 14 * 60 + 45),
            eod_exit_minutes=i("SIGNALMONITOR_RANKED_PAPER_EOD_EXIT_MINUTES", 15 * 60 + 10),
        )


@dataclass
class CandidateObservation:
    timestamp: str
    price: float
    volume: float
    buy_sell_ratio: float
    spread_bps: float
    vwap: float
    open_price: float
    prev_close: float
    day_high: float
    best_bid: float
    best_ask: float
    buy_qty: float
    sell_qty: float


@dataclass
class RankedPaperCandidate:
    date: str
    symbol: str
    alert_time: str
    alert_timestamp: float
    alert_price: float
    strength: float
    checks: str
    status: str = "observing"
    status_reason: str = "waiting_for_sustained_confirmation"
    observations: List[CandidateObservation] = field(default_factory=list)
    last_decision_at: str = ""


@dataclass
class RankedPaperTrade:
    date: str
    symbol: str
    strategy: str
    alert_time: str
    entry_time: str
    alert_price: float
    intended_entry: float
    entry_price: float
    qty: int
    strength: float
    rank_score: float
    target_price: float
    stop_price: float
    entry_open_move_pct: float
    entry_day_change_pct: float
    entry_pullback_pct: float
    entry_bs_ratio: float
    entry_bs_median: float
    entry_volume_growth_pct: float
    entry_spread_bps: float
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    net_pnl_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    closed: bool = False


class RankedConfirmationPaperTrader:
    """Stateful paper executor for sustained, ranked Grade-B confirmations."""

    EVENT_COLUMNS = [
        "timestamp", "event", "symbol", "status", "reason", "price",
        "rank_score", "qty", "gross_pnl", "costs", "net_pnl", "details_json",
    ]

    def __init__(
        self,
        output_dir: str,
        config: Optional[RankedPaperConfig] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        persist: bool = True,
    ) -> None:
        self.config = config or RankedPaperConfig.from_env()
        self.persist = bool(persist)
        self.output_dir = Path(output_dir) / "ranked_confirmation_paper"
        if self.persist:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.output_dir / "state.json"
        self.trades_path = self.output_dir / "trades_latest.csv"
        self.candidates_path = self.output_dir / "candidates_latest.csv"
        self.events_path = self.output_dir / "events.csv"
        self._observation_paths_with_headers: set[str] = set()
        self._now_fn = now_fn or PKDateUtilities.currentDateTime
        self._lock = threading.RLock()
        self._date = self._now().strftime("%Y-%m-%d")
        self.candidates: Dict[str, RankedPaperCandidate] = {}
        self.trades: Dict[str, RankedPaperTrade] = {}
        self._daily_summary_emitted = False
        if self.persist:
            self._load_state()

    def process(
        self,
        alerts: Any,
        ticks: Dict[str, Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Register alerts, observe candidates, rank entries, and manage exits."""
        now = now or self._now()
        events: List[Dict[str, Any]] = []
        with self._lock:
            self._roll_day(now, ticks, events)
            self._register_alerts(alerts, now, events)
            self._observe_candidates(ticks, now)
            self._update_open_trades(ticks, now, events)
            self._expire_candidates(now, events)
            self._enter_ranked_candidates(ticks, now, events)
            self._maybe_emit_daily_summary(now, events)
            if self.persist:
                self._save_state()
                self._write_trades()
                self._write_candidates()
        return events

    def _register_alerts(self, alerts: Any, now: datetime, events: List[Dict[str, Any]]) -> None:
        if alerts is None:
            return
        if hasattr(alerts, "to_dict"):
            try:
                rows = alerts.to_dict(orient="records")
            except TypeError:
                rows = []
        elif isinstance(alerts, list):
            rows = alerts
        else:
            rows = []
        today = now.strftime("%Y-%m-%d")
        for row in rows:
            action = str(row.get("NowAction", "") or "")
            if "grade-b" not in action.lower():
                continue
            symbol = str(row.get("Symbol", "") or "").strip().upper()
            alert_price = _float(row.get("PriceNow")) or _float(row.get("BuyAbove"))
            if not symbol or alert_price <= 0 or symbol in self.candidates or symbol in self.trades:
                continue
            candidate = RankedPaperCandidate(
                date=today,
                symbol=symbol,
                alert_time=now.isoformat(timespec="seconds"),
                alert_timestamp=now.timestamp(),
                alert_price=round(alert_price, 4),
                strength=_float(row.get("SignalStrengthPct")),
                checks=str(row.get("Checks", "") or ""),
            )
            self.candidates[symbol] = candidate
            events.append(self._event(now, "candidate_registered", symbol, "observing", candidate.status_reason, alert_price))

    def _observe_candidates(self, ticks: Dict[str, Dict[str, Any]], now: datetime) -> None:
        symbols = {
            symbol
            for symbol, candidate in self.candidates.items()
            if candidate.status in {"observing", "entered"}
        }
        symbols.update(
            symbol for symbol, trade in self.trades.items() if not trade.closed
        )
        for symbol in sorted(symbols):
            candidate = self.candidates.get(symbol)
            tick = ticks.get(symbol, {}) if isinstance(ticks, dict) else {}
            observation = self._observation(tick, now)
            if observation is None:
                continue
            if (
                candidate is not None
                and candidate.observations
                and candidate.observations[-1].timestamp == observation.timestamp
            ):
                continue
            if candidate is not None and candidate.status == "observing":
                candidate.observations.append(observation)
                candidate.observations = candidate.observations[-180:]
            if self.persist:
                self._append_observation_tape(candidate, observation)

    def _enter_ranked_candidates(
        self,
        ticks: Dict[str, Dict[str, Any]],
        now: datetime,
        events: List[Dict[str, Any]],
    ) -> None:
        minute_of_day = now.hour * 60 + now.minute
        if minute_of_day >= self.config.latest_entry_minutes:
            for candidate in self.candidates.values():
                if candidate.status == "observing":
                    candidate.status_reason = "latest_entry_cutoff_reached"
                    candidate.last_decision_at = now.isoformat(timespec="seconds")
            return
        if self._guardrail_reason():
            return
        open_count = sum(1 for trade in self.trades.values() if not trade.closed)
        slots = max(0, self.config.max_open_positions - open_count)
        trades_left = max(0, self.config.max_trades_per_day - len(self.trades))
        slots = min(slots, trades_left)
        if slots <= 0:
            return

        eligible: List[Tuple[float, RankedPaperCandidate, Dict[str, float]]] = []
        for candidate in self.candidates.values():
            if candidate.status != "observing":
                continue
            accepted, reason, metrics = self._entry_decision(candidate, now)
            candidate.status_reason = reason
            candidate.last_decision_at = now.isoformat(timespec="seconds")
            if accepted:
                eligible.append((metrics["rank_score"], candidate, metrics))
        eligible.sort(key=lambda item: (-item[0], item[1].symbol))

        for rank_score, candidate, metrics in eligible[:slots]:
            if self._guardrail_reason():
                break
            tick = ticks.get(candidate.symbol, {})
            intended = _float(tick.get("best_ask")) or _float(tick.get("last_price"))
            if intended <= 0:
                continue
            entry = intended * (1.0 + self.config.slippage_bps_per_side / 10_000.0)
            qty = int(self.config.budget_per_trade // entry)
            if qty < 1:
                candidate.status_reason = "budget_cannot_afford_one_share"
                continue
            trade = RankedPaperTrade(
                date=self._date,
                symbol=candidate.symbol,
                strategy="ranked_sustained_grade_b_v1",
                alert_time=candidate.alert_time,
                entry_time=now.isoformat(timespec="seconds"),
                alert_price=candidate.alert_price,
                intended_entry=round(intended, 4),
                entry_price=round(entry, 4),
                qty=qty,
                strength=candidate.strength,
                rank_score=round(rank_score, 4),
                target_price=round(entry * (1.0 + self.config.target_pct / 100.0), 4),
                stop_price=round(entry * (1.0 - self.config.stop_pct / 100.0), 4),
                entry_open_move_pct=metrics["open_move_pct"],
                entry_day_change_pct=metrics["day_change_pct"],
                entry_pullback_pct=metrics["pullback_pct"],
                entry_bs_ratio=metrics["current_bs"],
                entry_bs_median=metrics["median_bs"],
                entry_volume_growth_pct=metrics["volume_growth_pct"],
                entry_spread_bps=metrics["spread_bps"],
                highest_price=round(entry, 4),
                lowest_price=round(entry, 4),
            )
            self.trades[candidate.symbol] = trade
            candidate.status = "entered"
            candidate.status_reason = "ranked_sustained_confirmation"
            events.append(
                self._event(
                    now, "paper_entry", candidate.symbol, "open",
                    candidate.status_reason, trade.entry_price,
                    rank_score=trade.rank_score, qty=qty,
                    details={"target": trade.target_price, "stop": trade.stop_price, **metrics},
                )
            )

    def _entry_decision(
        self,
        candidate: RankedPaperCandidate,
        now: datetime,
    ) -> Tuple[bool, str, Dict[str, float]]:
        observations = candidate.observations
        if len(observations) < self.config.min_observations:
            return False, "insufficient_observations", {}
        age = now.timestamp() - candidate.alert_timestamp
        if age < self.config.min_observation_seconds:
            return False, "minimum_observation_window_not_met", {}

        first = observations[0]
        current = observations[-1]
        price = current.price
        if price <= 0 or first.price <= 0:
            return False, "missing_live_price", {}
        confirm_price = candidate.alert_price * (1.0 + self.config.min_confirm_pct / 100.0)
        max_entry = candidate.alert_price * (1.0 + self.config.max_entry_extension_pct / 100.0)
        if price < confirm_price:
            return False, "price_below_confirmation", {}
        if price > max_entry:
            return False, "price_overextended_from_alert", {}

        recent_cutoff = now.timestamp() - min(30, self.config.min_observation_seconds)
        recent_prices = [
            obs.price for obs in observations
            if self._parse_time(obs.timestamp).timestamp() >= recent_cutoff
        ]
        if len(recent_prices) < 2 or min(recent_prices) < confirm_price:
            return False, "confirmation_not_held", {}

        open_move = (price / current.open_price - 1.0) * 100.0 if current.open_price > 0 else 0.0
        day_change = (price / current.prev_close - 1.0) * 100.0 if current.prev_close > 0 else 0.0
        pullback = (current.day_high / price - 1.0) * 100.0 if current.day_high > 0 else 0.0
        if not (self.config.min_open_move_pct <= open_move <= self.config.max_open_move_pct):
            return False, "open_move_outside_early_window", {}
        if day_change < self.config.min_day_change_pct:
            return False, "day_move_below_positive_floor", {}
        if day_change > self.config.max_day_change_pct:
            return False, "day_move_too_extended", {}
        if pullback > self.config.max_pullback_pct:
            return False, "too_far_below_day_high", {}
        if current.vwap <= 0 or price < current.vwap:
            return False, "price_below_vwap", {}

        ratios = [obs.buy_sell_ratio for obs in observations if obs.buy_sell_ratio > 0]
        current_bs = current.buy_sell_ratio
        median_bs = statistics.median(ratios) if ratios else 0.0
        min_bs = min(ratios) if ratios else 0.0
        if current_bs < self.config.min_current_bs_ratio:
            return False, "current_buy_pressure_too_weak", {}
        if median_bs < self.config.min_median_bs_ratio or min_bs < self.config.min_bs_ratio:
            return False, "buy_pressure_not_stable", {}

        if first.volume <= 0 or current.volume <= first.volume:
            return False, "volume_not_growing", {}
        volume_growth = (current.volume / first.volume - 1.0) * 100.0
        if volume_growth < self.config.min_volume_growth_pct:
            return False, "post_alert_volume_growth_too_low", {}
        if current.spread_bps <= 0 or current.spread_bps > self.config.max_spread_bps:
            return False, "spread_not_tradeable", {}

        extension = (price / candidate.alert_price - 1.0) * 100.0
        rank_score = (
            candidate.strength
            + min(median_bs, 5.0) * 6.0
            + min(volume_growth, 10.0) * 1.5
            - pullback * 12.0
            - extension * 8.0
            - current.spread_bps * 0.2
        )
        metrics = {
            "rank_score": round(rank_score, 4),
            "open_move_pct": round(open_move, 4),
            "day_change_pct": round(day_change, 4),
            "pullback_pct": round(pullback, 4),
            "current_bs": round(current_bs, 4),
            "median_bs": round(median_bs, 4),
            "min_bs": round(min_bs, 4),
            "volume_growth_pct": round(volume_growth, 4),
            "spread_bps": round(current.spread_bps, 4),
            "extension_pct": round(extension, 4),
        }
        return True, "ranked_sustained_confirmation", metrics

    def _update_open_trades(
        self,
        ticks: Dict[str, Dict[str, Any]],
        now: datetime,
        events: List[Dict[str, Any]],
    ) -> None:
        minute_of_day = now.hour * 60 + now.minute
        for symbol, trade in list(self.trades.items()):
            if trade.closed:
                continue
            tick = ticks.get(symbol, {}) if isinstance(ticks, dict) else {}
            price = _float(tick.get("last_price"))
            if price <= 0:
                continue
            trade.highest_price = max(trade.highest_price or price, price)
            trade.lowest_price = min(trade.lowest_price or price, price)
            trade.mfe_pct = round((trade.highest_price / trade.entry_price - 1.0) * 100.0, 4)
            trade.mae_pct = round((trade.lowest_price / trade.entry_price - 1.0) * 100.0, 4)

            reason = ""
            if price <= trade.stop_price:
                reason = "stop_hit"
            elif price >= trade.target_price:
                reason = "target_hit"
            elif minute_of_day >= self.config.eod_exit_minutes:
                reason = "eod_exit"
            else:
                entry_at = self._parse_time(trade.entry_time)
                age = (now - entry_at).total_seconds()
                vwap = _float(tick.get("average_traded_price"))
                buy = _float(tick.get("total_buy_quantity"))
                sell = _float(tick.get("total_sell_quantity"))
                sell_ratio = sell / buy if buy > 0 else 0.0
                pnl_pct = (price / trade.entry_price - 1.0) * 100.0
                if (
                    age >= self.config.fast_failure_age_seconds
                    and pnl_pct <= -self.config.fast_failure_drop_pct
                    and vwap > 0
                    and price < vwap
                    and sell_ratio >= self.config.fast_failure_sell_ratio
                ):
                    reason = "failed_confirmation_exit"
            if reason:
                self._close_trade(trade, tick, price, now, reason, events)

    def _close_trade(
        self,
        trade: RankedPaperTrade,
        tick: Dict[str, Any],
        last_price: float,
        now: datetime,
        reason: str,
        events: List[Dict[str, Any]],
    ) -> None:
        intended_exit = _float(tick.get("best_bid")) or last_price
        exit_price = intended_exit * (1.0 - self.config.slippage_bps_per_side / 10_000.0)
        gross = (exit_price - trade.entry_price) * trade.qty
        costs = (
            (trade.entry_price + exit_price)
            * trade.qty
            * self.config.fee_pct_per_side
            / 100.0
        )
        net = gross - costs
        trade.exit_time = now.isoformat(timespec="seconds")
        trade.exit_price = round(exit_price, 4)
        trade.exit_reason = reason
        trade.gross_pnl = round(gross, 2)
        trade.costs = round(costs, 2)
        trade.net_pnl = round(net, 2)
        trade.net_pnl_pct = round(net / (trade.entry_price * trade.qty) * 100.0, 4)
        trade.closed = True
        events.append(
            self._event(
                now, "paper_exit", trade.symbol, "closed", reason,
                trade.exit_price, rank_score=trade.rank_score, qty=trade.qty,
                gross_pnl=trade.gross_pnl, costs=trade.costs, net_pnl=trade.net_pnl,
                details={"mfe_pct": trade.mfe_pct, "mae_pct": trade.mae_pct},
            )
        )

    def _guardrail_reason(self) -> str:
        closed = sorted(
            (trade for trade in self.trades.values() if trade.closed),
            key=lambda trade: trade.exit_time,
        )
        daily_pnl = sum(trade.net_pnl for trade in closed)
        if self.config.max_daily_loss_amount > 0 and daily_pnl <= -self.config.max_daily_loss_amount:
            return "daily_loss_limit_reached"
        streak = 0
        for trade in reversed(closed):
            if trade.net_pnl < 0:
                streak += 1
            else:
                break
        if self.config.max_consecutive_losses > 0 and streak >= self.config.max_consecutive_losses:
            return "consecutive_loss_limit_reached"
        return ""

    def _maybe_emit_daily_summary(
        self,
        now: datetime,
        events: List[Dict[str, Any]],
    ) -> None:
        if self._daily_summary_emitted:
            return
        minute_of_day = now.hour * 60 + now.minute
        if minute_of_day < self.config.eod_exit_minutes:
            return
        if any(not trade.closed for trade in self.trades.values()):
            return
        trades = list(self.trades.values())
        closed = [trade for trade in trades if trade.closed]
        wins = sum(1 for trade in closed if trade.net_pnl > 0)
        losses = sum(1 for trade in closed if trade.net_pnl < 0)
        net_pnl = round(sum(trade.net_pnl for trade in closed), 2)
        details = {
            "date": self._date,
            "candidates": len(self.candidates),
            "trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / len(closed) * 100.0, 2) if closed else 0.0,
            "net_pnl": net_pnl,
            "targets": sum(1 for trade in closed if trade.exit_reason == "target_hit"),
            "stops": sum(1 for trade in closed if trade.exit_reason == "stop_hit"),
            "failed_confirmations": sum(
                1 for trade in closed if trade.exit_reason == "failed_confirmation_exit"
            ),
            "eod_exits": sum(1 for trade in closed if trade.exit_reason == "eod_exit"),
        }
        events.append(
            self._event(
                now,
                "paper_daily_summary",
                "PORTFOLIO",
                "complete",
                "eod_summary",
                0.0,
                net_pnl=net_pnl,
                details=details,
            )
        )
        self._daily_summary_emitted = True

    def _expire_candidates(self, now: datetime, events: List[Dict[str, Any]]) -> None:
        for candidate in self.candidates.values():
            if candidate.status != "observing":
                continue
            age = now.timestamp() - candidate.alert_timestamp
            if age <= self.config.entry_window_seconds:
                continue
            candidate.status = "expired"
            candidate.status_reason = candidate.status_reason or "entry_window_expired"
            events.append(
                self._event(
                    now, "candidate_expired", candidate.symbol, "expired",
                    candidate.status_reason, candidate.observations[-1].price if candidate.observations else candidate.alert_price,
                )
            )

    def _roll_day(
        self,
        now: datetime,
        ticks: Dict[str, Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> None:
        today = now.strftime("%Y-%m-%d")
        if today == self._date:
            return
        for trade in self.trades.values():
            if not trade.closed:
                tick = ticks.get(trade.symbol, {}) if isinstance(ticks, dict) else {}
                price = _float(tick.get("last_price")) or trade.entry_price
                self._close_trade(trade, tick, price, now, "stale_overnight_recovery", events)
        self._write_trades()
        self._write_candidates()
        self.candidates = {}
        self.trades = {}
        self._date = today
        self._daily_summary_emitted = False

    def _observation(self, tick: Dict[str, Any], now: datetime) -> Optional[CandidateObservation]:
        if not isinstance(tick, dict):
            return None
        price = _float(tick.get("last_price"))
        if price <= 0:
            return None
        buy = _float(tick.get("total_buy_quantity"))
        sell = _float(tick.get("total_sell_quantity"))
        ratio = buy / sell if sell > 0 else (10.0 if buy > 0 else 0.0)
        bid = _float(tick.get("best_bid"))
        ask = _float(tick.get("best_ask"))
        spread_bps = (ask - bid) / price * 10_000.0 if ask > 0 and bid > 0 and ask >= bid else 0.0
        return CandidateObservation(
            timestamp=now.isoformat(timespec="seconds"),
            price=price,
            volume=_float(tick.get("volume_traded")),
            buy_sell_ratio=round(min(ratio, 10.0), 4),
            spread_bps=round(spread_bps, 4),
            vwap=_float(tick.get("average_traded_price")),
            open_price=_float(tick.get("open")),
            prev_close=_float(tick.get("close")),
            day_high=_float(tick.get("high")),
            best_bid=bid,
            best_ask=ask,
            buy_qty=buy,
            sell_qty=sell,
        )

    def _append_observation_tape(
        self,
        candidate: Optional[RankedPaperCandidate],
        observation: CandidateObservation,
    ) -> None:
        """Append replay-complete live evidence for one candidate/trade tick."""
        symbol = candidate.symbol if candidate is not None else ""
        if not symbol:
            return
        path = self.output_dir / f"observations_{self._date}.csv"
        columns = [
            "date", "symbol", "alert_time", "alert_price", "strength", "checks",
            "candidate_status", *CandidateObservation.__dataclass_fields__.keys(),
        ]
        row = {
            "date": self._date,
            "symbol": symbol,
            "alert_time": candidate.alert_time,
            "alert_price": candidate.alert_price,
            "strength": candidate.strength,
            "checks": candidate.checks,
            "candidate_status": candidate.status,
            **asdict(observation),
        }
        path_key = str(path)
        write_header = path_key not in self._observation_paths_with_headers and not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        self._observation_paths_with_headers.add(path_key)

    def _event(
        self,
        now: datetime,
        event: str,
        symbol: str,
        status: str,
        reason: str,
        price: float,
        rank_score: float = 0.0,
        qty: int = 0,
        gross_pnl: float = 0.0,
        costs: float = 0.0,
        net_pnl: float = 0.0,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        row = {
            "timestamp": now.isoformat(timespec="seconds"),
            "event": event,
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "price": round(price, 4),
            "rank_score": round(rank_score, 4),
            "qty": int(qty),
            "gross_pnl": round(gross_pnl, 2),
            "costs": round(costs, 2),
            "net_pnl": round(net_pnl, 2),
            "details_json": json.dumps(details or {}, sort_keys=True, default=str),
        }
        if self.persist:
            self._append_event(row)
        return row

    def _append_event(self, row: Dict[str, Any]) -> None:
        write_header = not self.events_path.exists()
        with self.events_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.EVENT_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow({column: row.get(column, "") for column in self.EVENT_COLUMNS})

    def _write_trades(self) -> None:
        columns = list(RankedPaperTrade.__dataclass_fields__.keys())
        rows = sorted(self.trades.values(), key=lambda value: (value.date, value.entry_time, value.symbol))
        paths = [self.trades_path]
        if rows:
            paths.append(self.output_dir / f"trades_{rows[0].date}.csv")
        for path in paths:
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for trade in rows:
                    writer.writerow(asdict(trade))

    def _write_candidates(self) -> None:
        columns = [
            "date", "symbol", "alert_time", "alert_price", "strength", "status",
            "status_reason", "last_decision_at", "observation_count", "last_price",
            "open_move_pct", "day_change_pct", "pullback_pct", "current_bs",
            "median_bs", "min_bs", "volume_growth_pct", "spread_bps", "vwap",
        ]
        rows = []
        for candidate in sorted(self.candidates.values(), key=lambda value: (value.alert_time, value.symbol)):
            observations = candidate.observations
            first = observations[0] if observations else None
            last = observations[-1] if observations else None
            ratios = [obs.buy_sell_ratio for obs in observations if obs.buy_sell_ratio > 0]
            rows.append(
                {
                    "date": candidate.date,
                    "symbol": candidate.symbol,
                    "alert_time": candidate.alert_time,
                    "alert_price": candidate.alert_price,
                    "strength": candidate.strength,
                    "status": candidate.status,
                    "status_reason": candidate.status_reason,
                    "last_decision_at": candidate.last_decision_at,
                    "observation_count": len(observations),
                    "last_price": last.price if last else 0.0,
                    "open_move_pct": (
                        round((last.price / last.open_price - 1.0) * 100.0, 4)
                        if last and last.open_price > 0 else 0.0
                    ),
                    "day_change_pct": (
                        round((last.price / last.prev_close - 1.0) * 100.0, 4)
                        if last and last.prev_close > 0 else 0.0
                    ),
                    "pullback_pct": (
                        round((last.day_high / last.price - 1.0) * 100.0, 4)
                        if last and last.day_high > 0 and last.price > 0 else 0.0
                    ),
                    "current_bs": last.buy_sell_ratio if last else 0.0,
                    "median_bs": round(statistics.median(ratios), 4) if ratios else 0.0,
                    "min_bs": round(min(ratios), 4) if ratios else 0.0,
                    "volume_growth_pct": (
                        round((last.volume / first.volume - 1.0) * 100.0, 4)
                        if first and last and first.volume > 0 else 0.0
                    ),
                    "spread_bps": last.spread_bps if last else 0.0,
                    "vwap": last.vwap if last else 0.0,
                }
            )
        paths = [self.candidates_path]
        if rows:
            paths.append(self.output_dir / f"candidates_{rows[0]['date']}.csv")
        for path in paths:
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)

    def _save_state(self) -> None:
        payload = {
            "date": self._date,
            "daily_summary_emitted": self._daily_summary_emitted,
            "candidates": {symbol: asdict(value) for symbol, value in self.candidates.items()},
            "trades": {symbol: asdict(value) for symbol, value in self.trades.items()},
        }
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.state_path)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if str(payload.get("date", "")) != self._date:
                return
            self._daily_summary_emitted = bool(payload.get("daily_summary_emitted", False))
            self.candidates = {
                symbol: RankedPaperCandidate(
                    **{
                        **row,
                        "observations": [CandidateObservation(**obs) for obs in row.get("observations", [])],
                    }
                )
                for symbol, row in payload.get("candidates", {}).items()
            }
            self.trades = {
                symbol: RankedPaperTrade(**row)
                for symbol, row in payload.get("trades", {}).items()
            }
        except Exception:
            self.candidates = {}
            self.trades = {}

    def _now(self) -> datetime:
        try:
            return self._now_fn()
        except Exception:
            return datetime.now()

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(str(value))


__all__ = [
    "RankedConfirmationPaperTrader",
    "RankedPaperConfig",
    "RankedPaperCandidate",
    "RankedPaperTrade",
]
