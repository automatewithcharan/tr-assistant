#!/usr/bin/env python3
"""Swing strategy paper trader — momentum_10d, bull regime, long-only.

Purpose: measure whether actual overnight execution friction is 0.10% or 0.20%.
No real orders are placed. This is a pure simulation journal.

Config:
  family:       momentum_10d
  target:       12%
  stop:         3%
  min_ret_pct:  12  (10-day return >= 12%)
  regime:       bull  (Nifty close > Nifty EMA-50)
  max_positions: 2  (never hold more than 2 simultaneous positions)
  hold:         10 trading days max

Key measurements recorded per trade:
  signal_close    — close[T] when signal fired (last price you saw)
  intended_open   — close[T] itself (your expectation of next-day open)
  actual_open     — open[T+1] (what the market actually opened at)
  overnight_gap   — (actual_open / signal_close - 1) * 100%
  fill_price      — actual_open (assumed market order at open auction)

This lets us compute whether overnight friction is ~0.10% or ~0.20%, which is
the single remaining unknown before Stage 1 viability can be claimed.

Usage:
  # Replay last 20 cached trading days to seed the journal
  python scripts/swing_paper_trader.py --seed

  # Check current status and open positions
  python scripts/swing_paper_trader.py --status

  # Advance one day forward (run EOD each evening)
  python scripts/swing_paper_trader.py --eod --date 2026-05-12

  # Full replay over a date range (for seeding)
  python scripts/swing_paper_trader.py --replay --from 2026-04-15 --to 2026-05-12
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.research_kite_swing import (
    DAY_CACHE_DIR,
    SwingConfig,
    add_daily_features,
    generate_signals,
    indian_delivery_fee_pct,
    load_all_daily,
    load_daily_symbol,
)

# Daily-refreshed live cache (populated by daily_prewarm.sh at 8 AM IST).
# Paper trader pulls fresh OHLCV from here, filtered to the 201-symbol
# swing research universe defined by the static backfill cache.
LIVE_CACHE_DIR      = REPO_ROOT / "results" / "Data" / "kite_history_cache" / "day"
UNIVERSE_REF_DIR    = DAY_CACHE_DIR   # backfill folder serves as the universe-of-symbols reference
NIFTY_FILE          = "NIFTY 50"

PAPER_DIR = REPO_ROOT / "results" / "Data" / "swing_paper_trades"
JOURNAL_PATH = PAPER_DIR / "paper_journal.json"
TRADES_CSV   = PAPER_DIR / "paper_trades.csv"
LOG_PATH     = PAPER_DIR / "paper_log.txt"

# Strategy config — fixed for this paper trading run
STRATEGY_CFG = SwingConfig(
    family="momentum_10d",
    side="long",
    params={
        "min_value_cr": 25.0,
        "min_price": 40.0,
        "min_ret_pct": 12.0,
        "nifty_regime": "bull",
    },
    target_pct=12.0,
    stop_pct=3.0,
    max_hold_days=10,
)

MAX_POSITIONS = 2
MIN_PAPER_DAYS_FOR_STAGE1 = 20
MIN_CLOSED_TRADES_GATE    = 10    # hard minimum before evaluating
PREFERRED_CLOSED_TRADES   = 30   # preferred before making any viability claim

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PaperTrade:
    trade_id: str
    symbol: str
    signal_date: str           # date T: signal fired at close
    signal_close: float        # close[T] — last observable price
    intended_open: float       # close[T] — our expectation for T+1 open
    actual_open: Optional[float] = None   # open[T+1] — actual fill
    overnight_gap_pct: Optional[float] = None  # (actual_open/signal_close - 1)*100
    fill_price: Optional[float] = None    # = actual_open (market order at auction)
    target_price: Optional[float] = None  # fill * 1.12
    stop_price: Optional[float] = None    # fill * 0.97
    entry_date: Optional[str] = None
    status: str = "pending"   # pending | active | closed | skipped
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None    # target | stop | max_hold
    hold_days: Optional[int] = None
    gross_return_pct: Optional[float] = None
    cost_pct: Optional[float] = None
    net_return_pct: Optional[float] = None
    skip_reason: Optional[str] = None    # why it was skipped

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PaperTrade":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Journal:
    config: dict = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)   # list of PaperTrade dicts
    daily_log: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def save(self, path: Path = JOURNAL_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "config": self.config,
            "trades": self.trades,
            "daily_log": self.daily_log,
            "stats": self.stats,
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = JOURNAL_PATH) -> "Journal":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            config=raw.get("config", {}),
            trades=raw.get("trades", []),
            daily_log=raw.get("daily_log", []),
            stats=raw.get("stats", {}),
        )

    def active_trades(self) -> list[PaperTrade]:
        return [PaperTrade.from_dict(t) for t in self.trades if t.get("status") == "active"]

    def closed_trades(self) -> list[PaperTrade]:
        return [PaperTrade.from_dict(t) for t in self.trades if t.get("status") == "closed"]

    def pending_trades(self) -> list[PaperTrade]:
        return [PaperTrade.from_dict(t) for t in self.trades if t.get("status") == "pending"]

    def upsert(self, trade: PaperTrade) -> None:
        for i, t in enumerate(self.trades):
            if t.get("trade_id") == trade.trade_id:
                self.trades[i] = trade.to_dict()
                return
        self.trades.append(trade.to_dict())

    def next_trade_id(self) -> str:
        return f"ST{len(self.trades) + 1:04d}"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _log(msg: str, path: Path = LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{pd.Timestamp.now().isoformat(timespec='seconds')}] {msg}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _send_discord(msg: str) -> None:
    if not DISCORD_WEBHOOK:
        return
    try:
        import urllib.request
        data = json.dumps({"content": msg}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _load_universe_symbols() -> list[str]:
    """The 201-symbol swing research universe (filenames in the backfill cache)."""
    return sorted(f.stem for f in UNIVERSE_REF_DIR.glob("*.csv") if f.stem != NIFTY_FILE)


def load_features() -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    """Load features from the daily-refreshed live cache, filtered to the
    swing research universe. Falls back to the static backfill cache if the
    live cache is missing.
    """
    if LIVE_CACHE_DIR.exists() and any(LIVE_CACHE_DIR.glob("*.csv")):
        symbols = _load_universe_symbols()
        parts: list[pd.DataFrame] = []
        for sym in symbols:
            df = load_daily_symbol(sym, LIVE_CACHE_DIR)
            if not df.empty:
                parts.append(df)
        universe = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        nifty = load_daily_symbol(NIFTY_FILE, LIVE_CACHE_DIR)
    else:
        # Fallback: static backfill cache
        universe, nifty = load_all_daily(DAY_CACHE_DIR)

    features = add_daily_features(universe, nifty)
    features = features.dropna(subset=["high_20d", "vol_avg20"]).copy()
    trading_dates = sorted(pd.to_datetime(features["date"]).dropna().unique().tolist())
    return features, trading_dates


def get_signals_for_date(features: pd.DataFrame, date: pd.Timestamp) -> list[dict]:
    """Return signals that fired at close of `date` — entries to be placed next open."""
    day_data = features[pd.to_datetime(features["date"]) == date].copy()
    if day_data.empty:
        return []
    sig_mask = generate_signals(day_data, STRATEGY_CFG)
    signal_rows = day_data[sig_mask]
    result = []
    for _, row in signal_rows.iterrows():
        result.append({
            "symbol": str(row["symbol"]),
            "signal_close": float(row["close"]),
            "avg_value_cr": float(row.get("avg_value20_cr", 0.0)),
        })
    return result


def get_open_price(features: pd.DataFrame, symbol: str, date: pd.Timestamp) -> Optional[float]:
    """Return open price of `symbol` on `date` (the entry day)."""
    row = features[(pd.to_datetime(features["date"]) == date) & (features["symbol"] == symbol)]
    if row.empty:
        return None
    return float(row.iloc[0]["open"])


def check_exits(features: pd.DataFrame, active: list[PaperTrade],
                eod_date: pd.Timestamp) -> list[PaperTrade]:
    """Scan bars from entry up to eod_date to check if target/stop/max_hold was reached."""
    updated = []
    for trade in active:
        sym_df = features[features["symbol"] == trade.symbol].sort_values("date").reset_index(drop=True)
        entry_dt = pd.Timestamp(trade.entry_date)
        after = sym_df[
            (pd.to_datetime(sym_df["date"]) >= entry_dt) &
            (pd.to_datetime(sym_df["date"]) <= eod_date)
        ].reset_index(drop=True)
        if after.empty:
            updated.append(trade)
            continue

        fill = trade.fill_price
        target = fill * (1 + STRATEGY_CFG.target_pct / 100.0)
        stop   = fill * (1 - STRATEGY_CFG.stop_pct / 100.0)

        exit_date = exit_price = exit_reason = None
        for i, bar in after.iterrows():
            lo = float(bar["low"])
            hi = float(bar["high"])
            hold = i  # days since entry (0-indexed)
            if lo <= stop:
                exit_price = stop
                exit_reason = "stop"
                exit_date = pd.Timestamp(bar["date"])
                break
            if hi >= target:
                exit_price = target
                exit_reason = "target"
                exit_date = pd.Timestamp(bar["date"])
                break
            if hold >= STRATEGY_CFG.max_hold_days:
                exit_price = float(bar["close"])
                exit_reason = "max_hold"
                exit_date = pd.Timestamp(bar["date"])
                break

        if exit_price is not None:
            hold_days = (exit_date - entry_dt).days
            gross = (exit_price / fill - 1.0) * 100.0
            cost  = indian_delivery_fee_pct(0.0)
            net   = gross - cost
            trade.exit_date = str(exit_date.date())
            trade.exit_price = round(exit_price, 4)
            trade.exit_reason = exit_reason
            trade.hold_days = hold_days
            trade.gross_return_pct = round(gross, 4)
            trade.cost_pct = round(cost, 4)
            trade.net_return_pct = round(net, 4)
            trade.status = "closed"
            _log(f"CLOSED {trade.symbol} [{exit_reason}] "
                 f"entry={fill:.2f} exit={exit_price:.2f} net={net:+.2f}% hold={hold_days}d")
            _send_discord(
                f"📊 **Paper trade closed** | {trade.symbol} | {exit_reason.upper()} | "
                f"net={net:+.2f}% | hold={hold_days}d"
            )
        updated.append(trade)
    return updated


# ---------------------------------------------------------------------------
# Day runner
# ---------------------------------------------------------------------------

def run_eod(features: pd.DataFrame, date: pd.Timestamp, journal: Journal) -> Journal:
    """Run end-of-day step for `date`:
    1. Check if any active positions need to be exited.
    2. Generate signals for entries on date+1 open.
    3. Accept up to MAX_POSITIONS - active_count new positions.
    """
    date_str = str(date.date())
    _log(f"--- EOD {date_str} ---")

    # Step 1: fill actual_open for any pending trades whose entry_date == date
    for t in journal.pending_trades():
        if t.entry_date and pd.Timestamp(t.entry_date).date() == date.date():
            actual_open = get_open_price(features, t.symbol, date)
            if actual_open is None:
                _log(f"  SKIP fill {t.symbol}: no open price for {date_str}")
                t.status = "skipped"
                t.skip_reason = f"no open price on entry date {date_str}"
                journal.upsert(t)
                continue
            gap = (actual_open / t.signal_close - 1.0) * 100.0
            t.actual_open = round(actual_open, 4)
            t.overnight_gap_pct = round(gap, 4)
            t.fill_price = round(actual_open, 4)
            t.target_price = round(actual_open * (1 + STRATEGY_CFG.target_pct / 100.0), 4)
            t.stop_price   = round(actual_open * (1 - STRATEGY_CFG.stop_pct / 100.0), 4)
            t.status = "active"
            journal.upsert(t)
            _log(f"  FILLED {t.symbol}: open={actual_open:.2f}  "
                 f"gap={gap:+.3f}%  target={t.target_price:.2f}  stop={t.stop_price:.2f}")
            _send_discord(
                f"🟢 **Paper entry** | {t.symbol} | fill={actual_open:.2f} | "
                f"gap={gap:+.3f}% | target={t.target_price:.2f} | stop={t.stop_price:.2f}"
            )

    # Step 2: check exits for all active positions (only bars up to today)
    active = journal.active_trades()
    updated = check_exits(features, active, date)
    for t in updated:
        journal.upsert(t)

    # Step 3: generate new signals for tomorrow's open
    signals = get_signals_for_date(features, date)
    _log(f"  Signals: {len(signals)} (active positions: {len(journal.active_trades())})")

    active_count = len(journal.active_trades())
    slots = MAX_POSITIONS - active_count
    # Avoid re-entering symbols already held
    active_symbols = {t.symbol for t in journal.active_trades()}

    added = 0
    for sig in signals:
        sym = sig["symbol"]
        if sym in active_symbols:
            continue
        if slots <= 0:
            _log(f"  QUEUED {sym} (no slot available, max_positions={MAX_POSITIONS})")
            continue
        # Find the next trading date after `date`
        sym_df = features[features["symbol"] == sym].sort_values("date")
        after = sym_df[pd.to_datetime(sym_df["date"]) > date]
        if after.empty:
            continue
        next_date = pd.Timestamp(after.iloc[0]["date"])
        trade = PaperTrade(
            trade_id=journal.next_trade_id(),
            symbol=sym,
            signal_date=date_str,
            signal_close=sig["signal_close"],
            intended_open=sig["signal_close"],   # our naive expectation
            entry_date=str(next_date.date()),
            status="pending",
        )
        journal.upsert(trade)
        active_symbols.add(sym)
        slots -= 1
        added += 1
        _log(f"  SIGNAL {sym}: close={sig['signal_close']:.2f}  "
             f"entry_date={next_date.date()}  (pending fill)")

    # Daily log entry
    journal.daily_log.append({
        "date": date_str,
        "signals_fired": len(signals),
        "new_entries": added,
        "active_after": len(journal.active_trades()),
        "closed_today": sum(1 for t in updated if t.status == "closed"),
    })

    return journal


# ---------------------------------------------------------------------------
# Slippage analysis report
# ---------------------------------------------------------------------------

def print_slippage_report(journal: Journal) -> None:
    closed = journal.closed_trades()
    all_trades = [PaperTrade.from_dict(t) for t in journal.trades
                  if t.get("overnight_gap_pct") is not None]
    if not all_trades:
        print("No fills yet — no slippage data.")
        return

    gaps = [t.overnight_gap_pct for t in all_trades if t.overnight_gap_pct is not None]
    abs_gaps = [abs(g) for g in gaps]

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║      SWING PAPER TRADING — SLIPPAGE ANALYSIS            ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Fills analysed:         {len(gaps)}")
    print(f"  Mean overnight gap:     {sum(gaps)/len(gaps):>+.3f}%")
    print(f"  Mean abs overnight gap: {sum(abs_gaps)/len(abs_gaps):>.3f}%")
    print(f"  Max gap up:             {max(gaps):>+.3f}%")
    print(f"  Max gap down:           {min(gaps):>+.3f}%")
    print(f"  Model assumption:        0.200%  (conservative)")
    print(f"  Revised estimate:        {sum(abs_gaps)/len(abs_gaps):.3f}%")
    print()
    verdict = "LOWER than model" if sum(abs_gaps)/len(abs_gaps) < 0.15 else "CONSISTENT with model"
    print(f"  Verdict: overnight friction is {verdict}")

    if closed:
        nets = [t.net_return_pct for t in closed if t.net_return_pct is not None]
        wins = [n for n in nets if n > 0]
        print()
        print(f"  Closed trades:          {len(closed)}")
        print(f"  Win rate:               {len(wins)/len(nets)*100:.1f}%")
        print(f"  Avg net return:         {sum(nets)/len(nets):>+.3f}%")
        by_reason = {}
        for t in closed:
            r = t.exit_reason or "?"
            by_reason.setdefault(r, []).append(t.net_return_pct or 0.0)
        for reason, rets in sorted(by_reason.items()):
            print(f"  {reason:<10s}  n={len(rets)}  avg={sum(rets)/len(rets):>+.3f}%")

    n_days = len(journal.daily_log)
    stage1_ready = n_days >= MIN_PAPER_DAYS_FOR_STAGE1 and len(closed) >= 10
    print()
    print(f"  Paper days run:         {n_days} / {MIN_PAPER_DAYS_FOR_STAGE1} minimum")
    print(f"  Stage 1 gate:           {'ELIGIBLE TO EVALUATE' if stage1_ready else 'NOT YET (need 20 days + 10 closed trades)'}")
    print("══════════════════════════════════════════════════════════")


def stage1_validate(journal: Journal) -> None:
    """Formal Stage 1 validation report. Run after thresholds are met."""
    closed  = journal.closed_trades()
    filled  = [PaperTrade.from_dict(t) for t in journal.trades
               if t.get("overnight_gap_pct") is not None]
    n_days  = len(journal.daily_log)
    n_closed = len(closed)

    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║         STAGE 1 VALIDATION — momentum_10d bull regime       ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # --- Threshold gates ---
    gate_days   = n_days  >= MIN_PAPER_DAYS_FOR_STAGE1
    gate_trades = n_closed >= MIN_CLOSED_TRADES_GATE
    gate_pref   = n_closed >= PREFERRED_CLOSED_TRADES

    print(f"  Paper days:     {n_days:>4d}  ({'✓' if gate_days else '✗'} need {MIN_PAPER_DAYS_FOR_STAGE1})")
    print(f"  Closed trades:  {n_closed:>4d}  ({'✓' if gate_pref else ('gate ✓' if gate_trades else '✗')} "
          f"min={MIN_CLOSED_TRADES_GATE}, preferred={PREFERRED_CLOSED_TRADES})")
    print()

    if not (gate_days and gate_trades):
        print("  NOT READY: minimum thresholds not met. Continue EOD tracking.")
        print("══════════════════════════════════════════════════════════════")
        return

    # --- Overnight friction ---
    gaps     = [t.overnight_gap_pct for t in filled if t.overnight_gap_pct is not None]
    abs_gaps = [abs(g) for g in gaps]
    mean_abs = sum(abs_gaps) / len(abs_gaps)
    model_friction = 0.200
    friction_verdict = (
        "LOWER than model → revise cost model down"
        if mean_abs < 0.150 else
        "CLOSE to model (0.200%) → model is realistic"
        if mean_abs < 0.250 else
        "HIGHER than model → cost model is optimistic"
    )

    print(f"  ── OVERNIGHT FRICTION ──────────────────────────────────────")
    print(f"  Fills measured:           {len(gaps)}")
    print(f"  Mean overnight gap (abs): {mean_abs:.3f}%")
    print(f"  Model assumption:         {model_friction:.3f}%")
    print(f"  Verdict: {friction_verdict}")
    print()

    # --- Performance ---
    nets  = [t.net_return_pct for t in closed if t.net_return_pct is not None]
    wins  = [n for n in nets if n > 0]
    wr    = len(wins) / len(nets) * 100 if nets else 0.0
    avg   = sum(nets) / len(nets) if nets else 0.0
    gross_sum = sum(n for n in nets if n > 0)
    loss_sum  = abs(sum(n for n in nets if n < 0))
    pf    = gross_sum / loss_sum if loss_sum > 0 else float("inf")

    print(f"  ── PERFORMANCE ─────────────────────────────────────────────")
    print(f"  Win rate:     {wr:.1f}%  (backtest expected: ~28–34%)")
    print(f"  Avg net:      {avg:>+.3f}%")
    print(f"  Profit factor:{pf:.2f}")
    by_reason: dict = {}
    for t in closed:
        r = t.exit_reason or "?"
        by_reason.setdefault(r, []).append(t.net_return_pct or 0.0)
    for reason, rets in sorted(by_reason.items()):
        print(f"    {reason:<10s}  n={len(rets)}  avg={sum(rets)/len(rets):>+.3f}%")
    print()

    # --- Revised edge ---
    revised_friction = mean_abs
    raw_edge = 0.001          # momentum_10d gross from backtest
    modelled_cost = 0.086
    infra_gap = 0.050
    revised_net = raw_edge - modelled_cost - revised_friction - infra_gap
    model_net   = raw_edge - modelled_cost - model_friction  - infra_gap

    print(f"  ── REVISED GAP VS EDGE ─────────────────────────────────────")
    print(f"  Raw edge (backtest gross):  {raw_edge:>+.3f}%")
    print(f"  Modelled cost (fees+slip):  {modelled_cost:.3f}%")
    print(f"  Overnight friction:         {revised_friction:.3f}%  (was {model_friction:.3f}% assumed)")
    print(f"  Infrastructure gap:         {infra_gap:.3f}%")
    print(f"  Net viable edge (model):    {model_net:>+.3f}%")
    print(f"  Net viable edge (revised):  {revised_net:>+.3f}%  ← key number")
    print()

    # --- Decision ---
    print(f"  ── DECISION OPTIONS ────────────────────────────────────────")
    if revised_net > 0.05:
        print("  → CONTINUE: revised edge is positive. Proceed to Stage 1 live trial.")
    elif revised_net > -0.05:
        print("  → MARGINAL: edge is near zero. Consider adding RS filter before Stage 1.")
    elif mean_abs < 0.150:
        print("  → ADJUST COST MODEL: friction lower than assumed. Re-run survivability.")
    else:
        print("  → RETIRE: friction too high to overcome. Strategy class unviable.")

    print()
    print("  Options on the table:")
    print("    1. continue            — edge confirmed, proceed to live trial")
    print("    2. adjust cost model   — if friction < 0.15%, re-run research_kite_swing.py")
    print("    3. add RS filter        — add relative strength vs Nifty to strengthen signal")
    print("    4. retire              — friction >= 0.30% consistently, class unviable")
    print("══════════════════════════════════════════════════════════════")


def print_status(journal: Journal) -> None:
    active  = journal.active_trades()
    pending = journal.pending_trades()
    closed  = journal.closed_trades()

    print()
    print("=== SWING PAPER TRADER — STATUS ===")
    print(f"  Config: {STRATEGY_CFG.label()}")
    print(f"  Active positions:  {len(active)}")
    print(f"  Pending (awaiting fill): {len(pending)}")
    print(f"  Closed trades:     {len(closed)}")
    print(f"  Paper days logged: {len(journal.daily_log)}")
    print()

    if active:
        print("  ACTIVE POSITIONS:")
        for t in active:
            print(f"    {t.symbol:<15s} fill={t.fill_price:.2f}  "
                  f"target={t.target_price:.2f}  stop={t.stop_price:.2f}  "
                  f"entry={t.entry_date}  gap={t.overnight_gap_pct:>+.3f}%")
    if pending:
        print("  PENDING (signal fired, awaiting next open):")
        for t in pending:
            print(f"    {t.symbol:<15s} signal_close={t.signal_close:.2f}  entry_date={t.entry_date}")

    print_slippage_report(journal)

    if closed:
        print()
        print("  CLOSED TRADES:")
        for t in sorted(closed, key=lambda x: x.signal_date or ""):
            print(f"    {t.trade_id}  {t.symbol:<12s} {t.exit_reason:<10s} "
                  f"net={t.net_return_pct:>+.2f}%  gap={t.overnight_gap_pct:>+.3f}%  "
                  f"hold={t.hold_days}d  [{t.signal_date} → {t.exit_date}]")


# ---------------------------------------------------------------------------
# Replay mode (seed from cached data)
# ---------------------------------------------------------------------------

def replay(features: pd.DataFrame, trading_dates: list[pd.Timestamp],
           from_date: str, to_date: str, journal: Journal) -> Journal:
    """Walk day-by-day through [from_date, to_date] running EOD logic each day."""
    start = pd.Timestamp(from_date)
    end   = pd.Timestamp(to_date)
    dates = [d for d in trading_dates if start <= d <= end]
    _log(f"Replay: {len(dates)} trading days from {from_date} to {to_date}")
    for date in dates:
        journal = run_eod(features, date, journal)
        journal.save()
    return journal


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(journal: Journal, path: Path = TRADES_CSV) -> None:
    if not journal.trades:
        return
    df = pd.DataFrame(journal.trades)
    df.to_csv(path, index=False)
    _log(f"Exported {len(df)} trades → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Swing paper trader — momentum_10d bull regime")
    parser.add_argument("--seed", action="store_true",
                        help="Seed journal with last 20 cached trading days")
    parser.add_argument("--replay", action="store_true",
                        help="Replay a date range")
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to",   dest="to_date",   default=None)
    parser.add_argument("--eod",  action="store_true",
                        help="Run EOD step for --date (default: most recent cached date)")
    parser.add_argument("--date", default=None)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--validate", action="store_true",
                        help="Run Stage 1 validation report (use after 20 days + 10 closed trades)")
    parser.add_argument("--reset", action="store_true",
                        help="Delete journal and start fresh")
    args = parser.parse_args()

    PAPER_DIR.mkdir(parents=True, exist_ok=True)

    if args.reset:
        if JOURNAL_PATH.exists():
            JOURNAL_PATH.unlink()
        _log("Journal reset.")
        return 0

    journal = Journal.load()
    if not journal.config:
        journal.config = {
            "strategy": STRATEGY_CFG.label(),
            "params": STRATEGY_CFG.params,
            "target_pct": STRATEGY_CFG.target_pct,
            "stop_pct": STRATEGY_CFG.stop_pct,
            "max_hold_days": STRATEGY_CFG.max_hold_days,
            "max_positions": MAX_POSITIONS,
            "purpose": "measure overnight execution friction (0.10% vs 0.20%)",
            "started": str(pd.Timestamp.now().date()),
        }

    if args.status:
        print_status(journal)
        return 0

    if args.validate:
        stage1_validate(journal)
        return 0

    print("Loading features...", flush=True)
    features, trading_dates = load_features()
    print(f"  {features['symbol'].nunique()} symbols, {len(trading_dates)} dates", flush=True)

    if args.seed:
        # Replay last 20 trading days from the cache
        last_n = trading_dates[-20:]
        from_date = str(last_n[0].date())
        to_date   = str(last_n[-1].date())
        journal = replay(features, trading_dates, from_date, to_date, journal)
        export_csv(journal, TRADES_CSV)
        print_status(journal)
        return 0

    if args.replay:
        if not args.from_date or not args.to_date:
            print("--replay requires --from YYYY-MM-DD --to YYYY-MM-DD", file=sys.stderr)
            return 1
        journal = replay(features, trading_dates, args.from_date, args.to_date, journal)
        export_csv(journal, TRADES_CSV)
        print_status(journal)
        return 0

    if args.eod:
        if args.date:
            date = pd.Timestamp(args.date)
        else:
            date = trading_dates[-1]
        journal = run_eod(features, date, journal)
        journal.save()
        export_csv(journal, TRADES_CSV)
        print_status(journal)
        return 0

    # Default: show status
    print_status(journal)
    return 0


if __name__ == "__main__":
    sys.exit(main())
