"""Blind-buy alert paper trader.

Empirical basis (110 BLIND BUY alerts, 2026-04-06 – 2026-04-29):
  - Buy every BLIND BUY alert
  - Hold full quantity for 10 trading days
  - Exit at 10th-day close
  - Average net return (after 0.30% costs): +3.87% per alert
  - Total profit on 110 alerts × ₹50K: ≈ ₹2.13L

Key rules:
  1. Only trade BLIND BUY ALERT (skip Buy Now, Grade-A/B/C, Late Discovery)
  2. Entry at NEXT day's open (never chase intraday)
  3. Stop loss = price from the alert (always active)
  4. Optional partial profit: sell PARTIAL_PCT% of qty at +8% (disabled by default)
  5. Remaining quantity holds until 10th trading-day close
  6. Max concurrent positions = MAX_CONCURRENT (₹2L exposure at ₹50K each)
  7. Dedup: if same symbol fires multiple times same day, take highest-score only

Alert sources (in priority order):
  - results/Data/screener_alerts_from_beginning_analysis/unique_alerts_by_type.csv
  - results/Data/blind_buy_alerts.csv  (live ingestion going forward)
"""

from __future__ import annotations
import argparse, json, sys, warnings
warnings.filterwarnings("ignore")
from datetime import datetime, date as ddate, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"

# Historical parsed alerts (from Discord export analysis)
HISTORICAL_CSV = RESULTS / "screener_alerts_from_beginning_analysis" / "unique_alerts_by_type.csv"
# Live alert store — the live system appends here going forward
LIVE_ALERTS_CSV = RESULTS / "blind_buy_alerts.csv"

DAY_CACHE = RESULTS / "kite_history_cache" / "day"
DAY_BACKFILL = RESULTS / "kite_history_backfill" / "day"

JOURNAL_DIR = RESULTS / "blind_buy_alert_paper"
JOURNAL_PATH = JOURNAL_DIR / "journal.json"
TRADES_CSV = JOURNAL_DIR / "trades.csv"

# --- Strategy config --------------------------------------------------------
TOTAL_CAPITAL_INR    = 10_00_000.0  # ₹10L deployable capital
RISK_PER_TRADE_PCT   = 1.0          # 1% of capital at stop = ₹10K loss budget
MAX_POSITION_PCT     = 10.0         # cap any single position at 10% of capital = ₹1L
MIN_POSITION_INR     = 5_000.0      # below this, skip (cost/rounding noise)
DEFAULT_STOP_PCT     = 6.0          # assumed stop distance if alert had no explicit stop
MAX_HOLD_DAYS        = 10           # hold remaining qty until 10th trading-day close
PARTIAL_PCT          = 0            # % of qty to exit at +8% (0 = disabled)
PARTIAL_AT_PCT       = 8.0          # trigger for partial exit
DELIVERY_COST        = 0.30         # round-trip CNC delivery cost %
_NO_STOP             = False        # runtime override: skip stop logic when True

# Legacy aliases — referenced by older logs / journal; safe to remove once
# every active journal has migrated.
POSITION_INR   = 50_000.0
MAX_CONCURRENT = 999  # effectively unbounded; real cap is total-exposure check


def compute_position_size(entry: float, stop: float | None,
                          capital: float = TOTAL_CAPITAL_INR,
                          risk_pct: float = RISK_PER_TRADE_PCT,
                          max_pos_pct: float = MAX_POSITION_PCT,
                          min_pos: float = MIN_POSITION_INR) -> tuple[int, float]:
    """Risk-parity sizing.

    Returns (qty, position_inr). qty=0 means skip — either invalid stop or
    position would be below MIN_POSITION_INR after caps.
    """
    if entry is None or entry <= 0:
        return 0, 0.0
    if stop is None or stop <= 0 or stop >= entry:
        stop = entry * (1 - DEFAULT_STOP_PCT / 100.0)
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        return 0, 0.0
    risk_budget = capital * risk_pct / 100.0
    max_position = capital * max_pos_pct / 100.0
    qty_by_risk = int(risk_budget // per_share_risk)
    qty_by_max_pos = int(max_position // entry)
    qty = min(qty_by_risk, qty_by_max_pos)
    position = qty * entry
    if position < min_pos:
        return 0, 0.0
    return qty, position


# --- Price data helpers ------------------------------------------------------
_day_cache: dict[str, pd.DataFrame] = {}


def load_daily(symbol: str) -> pd.DataFrame:
    if symbol in _day_cache:
        return _day_cache[symbol]
    parts = []
    for path in [DAY_CACHE / f"{symbol}.csv"]:
        if path.exists():
            parts.append(pd.read_csv(path))
    for folder in sorted(DAY_BACKFILL.glob("*")):
        if folder.is_dir():
            p = folder / f"{symbol}.csv"
            if p.exists():
                parts.append(pd.read_csv(p))
    if not parts:
        _day_cache[symbol] = pd.DataFrame()
        return _day_cache[symbol]
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce").dt.tz_convert(None)
    df["date"] = df["datetime"].dt.date.astype(str)
    df = (df.dropna(subset=["datetime"])
            .drop_duplicates(subset=["date"])
            .sort_values("date")
            .reset_index(drop=True))
    _day_cache[symbol] = df
    return df


def next_open(symbol: str, after_date: str) -> tuple[str, float] | None:
    """Return (date, open_price) for the first trading day after after_date."""
    d = load_daily(symbol)
    if d.empty:
        return None
    future = d[d["date"] > after_date].sort_values("date")
    if future.empty:
        return None
    r = future.iloc[0]
    return (r["date"], float(r["open"]))


def day_bars(symbol: str, after_date: str, up_to: str) -> pd.DataFrame:
    """Return all daily bars for symbol in (after_date, up_to]."""
    d = load_daily(symbol)
    if d.empty:
        return pd.DataFrame()
    return d[(d["date"] > after_date) & (d["date"] <= up_to)].sort_values("date")


# --- Alert ingestion ---------------------------------------------------------

def load_all_blind_buy_alerts() -> pd.DataFrame:
    """Load all blind_buy alerts from both historical and live CSV sources."""
    parts: list[pd.DataFrame] = []

    if HISTORICAL_CSV.exists():
        hist = pd.read_csv(HISTORICAL_CSV)
        blind = hist[hist["alert_type"] == "blind_buy"].copy()
        blind = blind.rename(columns={
            "ts": "ts", "date": "date", "time": "time",
            "symbol": "symbol", "alert_price": "alert_price",
            "stop": "stop", "target1": "target1", "target2": "target2",
            "score": "score",
        })
        parts.append(blind[["date", "time", "symbol", "alert_price", "stop",
                             "target1", "target2", "score"]])

    if LIVE_ALERTS_CSV.exists():
        live = pd.read_csv(LIVE_ALERTS_CSV)
        # Expected columns: date, time, symbol, alert_price, stop, target1, target2, score
        parts.append(live[["date", "time", "symbol", "alert_price", "stop",
                            "target1", "target2", "score"]])

    if not parts:
        return pd.DataFrame()

    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0)
    df["alert_price"] = pd.to_numeric(df["alert_price"], errors="coerce")
    df["stop"] = pd.to_numeric(df["stop"], errors="coerce")
    df = df.dropna(subset=["alert_price"])
    df = df[df["alert_price"] > 0]

    # Dedup: same symbol on same day → keep highest score (fires multiple times)
    df = (df.sort_values(["date", "symbol", "score"], ascending=[True, True, False])
            .drop_duplicates(subset=["date", "symbol"], keep="first")
            .reset_index(drop=True))
    return df


# --- Journal -----------------------------------------------------------------

def load_journal() -> dict:
    if JOURNAL_PATH.exists():
        return json.loads(JOURNAL_PATH.read_text())
    return {
        "config": {
            "strategy": (
                f"blind_buy|entry=next_open|stop=alert_stop|"
                f"sizing=risk_parity|risk={RISK_PER_TRADE_PCT}%|"
                f"max_pos={MAX_POSITION_PCT}%|hold={MAX_HOLD_DAYS}d"
            ),
            "total_capital_inr": TOTAL_CAPITAL_INR,
            "risk_per_trade_pct": RISK_PER_TRADE_PCT,
            "max_position_pct": MAX_POSITION_PCT,
            "min_position_inr": MIN_POSITION_INR,
            "default_stop_pct": DEFAULT_STOP_PCT,
            "partial_pct": PARTIAL_PCT,
            "partial_at_pct": PARTIAL_AT_PCT,
            "max_hold_days": MAX_HOLD_DAYS,
            "delivery_cost_pct": DELIVERY_COST,
            "started": ddate.today().isoformat(),
        },
        "trades": [],
        "daily_log": [],
    }


def save_journal(j: dict):
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    JOURNAL_PATH.write_text(json.dumps(j, indent=2, default=str))


# --- Core logic --------------------------------------------------------------

def ingest_new_alerts(j: dict, today: str, lookback_days: int = 7) -> list[dict]:
    """Pick up blind-buy alerts not yet in journal (up to lookback_days old)."""
    all_alerts = load_all_blind_buy_alerts()
    if all_alerts.empty:
        return []

    cutoff = (ddate.fromisoformat(today) - timedelta(days=lookback_days)).isoformat()
    recent = all_alerts[(all_alerts["date"] >= cutoff) & (all_alerts["date"] <= today)]

    seen = {f"{t['alert_date']}::{t['symbol']}" for t in j["trades"]}
    new = []

    def _open_exposure() -> float:
        """Sum of ₹ committed to active + pending positions (planned for pending)."""
        total = 0.0
        for t in j["trades"]:
            if t.get("status") == "active":
                # Use actual position_inr if present, else fall back to entry × full_qty
                pos = t.get("position_inr")
                if pos is None:
                    pos = float(t.get("entry_price", 0)) * float(t.get("full_qty", 0))
                total += float(pos)
            elif t.get("status") == "pending":
                total += float(t.get("planned_position_inr", 0))
        return total

    for _, r in recent.iterrows():
        key = f"{r['date']}::{r['symbol'].upper()}"
        if key in seen:
            continue

        alert_px = float(r["alert_price"])
        stop_px = float(r["stop"]) if not pd.isna(r["stop"]) else None
        sym = r["symbol"].upper()

        planned_qty, planned_position = compute_position_size(alert_px, stop_px)
        if planned_qty == 0:
            j["trades"].append({
                "trade_id": f"BB{len(j['trades'])+1:05d}",
                "alert_date": r["date"], "alert_time": str(r.get("time", "")),
                "symbol": sym, "score": float(r["score"]),
                "alert_price": alert_px, "stop_price": stop_px,
                "target1": float(r["target1"]) if not pd.isna(r.get("target1")) else None,
                "target2": float(r["target2"]) if not pd.isna(r.get("target2")) else None,
                "status": "skipped", "skip_reason": "position_too_small",
            })
            seen.add(key)
            continue

        if _open_exposure() + planned_position > TOTAL_CAPITAL_INR:
            j["trades"].append({
                "trade_id": f"BB{len(j['trades'])+1:05d}",
                "alert_date": r["date"], "alert_time": str(r.get("time", "")),
                "symbol": sym, "score": float(r["score"]),
                "alert_price": alert_px, "stop_price": stop_px,
                "target1": float(r["target1"]) if not pd.isna(r.get("target1")) else None,
                "target2": float(r["target2"]) if not pd.isna(r.get("target2")) else None,
                "planned_qty": planned_qty,
                "planned_position_inr": round(planned_position, 2),
                "status": "skipped", "skip_reason": "capital_exhausted",
            })
            seen.add(key)
            continue

        j["trades"].append({
            "trade_id": f"BB{len(j['trades'])+1:05d}",
            "alert_date": r["date"], "alert_time": str(r.get("time", "")),
            "symbol": sym, "score": float(r["score"]),
            "alert_price": alert_px, "stop_price": stop_px,
            "target1": float(r["target1"]) if not pd.isna(r.get("target1")) else None,
            "target2": float(r["target2"]) if not pd.isna(r.get("target2")) else None,
            "planned_qty": planned_qty,
            "planned_position_inr": round(planned_position, 2),
            "status": "pending",
        })
        seen.add(key)
        new.append(j["trades"][-1])
    return new


def fill_pending_entries(j: dict, today: str) -> list[dict]:
    """Pending alerts → active positions when next-day open is available."""
    filled = []
    for t in j["trades"]:
        if t.get("status") != "pending":
            continue
        result = next_open(t["symbol"], t["alert_date"])
        if result is None:
            continue
        entry_date, entry_px = result
        if entry_date > today:
            continue
        if entry_px <= 0 or np.isnan(entry_px):
            t["status"] = "skipped"
            t["skip_reason"] = "no_entry_price"
            continue
        # Re-size at the actual entry price using the alert's stop. This keeps
        # the risk-budget honest even if the open gaps significantly from the
        # alert price (which is what the planned_position was computed against).
        full_qty, position_inr = compute_position_size(entry_px, t.get("stop_price"))
        if full_qty == 0:
            t["status"] = "skipped"
            t["skip_reason"] = "position_too_small_at_entry"
            continue

        partial_qty = int(full_qty * PARTIAL_PCT / 100) if PARTIAL_PCT > 0 else 0
        hold_qty = full_qty - partial_qty

        t["status"] = "active"
        t["entry_date"] = entry_date
        t["entry_price"] = round(entry_px, 4)
        t["full_qty"] = full_qty
        t["position_inr"] = round(position_inr, 2)
        t["partial_qty"] = partial_qty   # qty to sell at +8% (0 if disabled)
        t["hold_qty"] = hold_qty         # qty held to 10-day close
        t["partial_exited"] = False
        filled.append(t)
    return filled


def check_exits(j: dict, today: str) -> list[dict]:
    """Check active positions for stop / partial profit / max-hold exits."""
    exits = []
    for t in j["trades"]:
        if t.get("status") != "active":
            continue

        sym = t["symbol"]
        entry_date = t["entry_date"]
        entry_px = t["entry_price"]
        stop_px = t.get("stop_price")
        hold_qty = t["hold_qty"]
        partial_qty = t["partial_qty"]
        partial_exited = t.get("partial_exited", False)
        partial_at = entry_px * (1 + PARTIAL_AT_PCT / 100)

        bars = day_bars(sym, entry_date, today)
        if bars.empty:
            continue

        # --- Check each bar in chronological order ---
        stop_hit = False
        stop_date = None
        stop_px_hit = None
        partial_date = None

        for _, b in bars.iterrows():
            lo, hi = float(b["low"]), float(b["high"])
            bdate = b["date"]

            # Stop loss check (applies to full remaining position)
            if not _NO_STOP and stop_px is not None and lo <= stop_px:
                stop_hit = True
                stop_date = bdate
                stop_px_hit = stop_px
                break  # stop ends everything

            # Partial profit check (only on hold leg, only once)
            if PARTIAL_PCT > 0 and not partial_exited and partial_qty > 0:
                if hi >= partial_at:
                    t["partial_exited"] = True
                    partial_exited = True
                    partial_date = bdate
                    partial_gross = PARTIAL_AT_PCT
                    partial_net = round(partial_gross - DELIVERY_COST, 3)
                    t["partial_exit_date"] = partial_date
                    t["partial_exit_price"] = round(partial_at, 4)
                    t["partial_gross_pct"] = round(partial_gross, 3)
                    t["partial_net_pct"] = partial_net

        if stop_hit:
            # Exit entire remaining position at stop
            gross = (stop_px_hit / entry_px - 1) * 100
            net = gross - DELIVERY_COST
            t["status"] = "closed"
            t["exit_date"] = stop_date
            t["exit_price"] = round(stop_px_hit, 4)
            t["exit_reason"] = "stop"
            t["exit_qty"] = hold_qty  # remaining hold qty
            t["gross_return_pct"] = round(gross, 3)
            t["cost_pct"] = DELIVERY_COST
            t["net_return_pct"] = round(net, 3)
            t["hold_days"] = len(bars[bars["date"] <= stop_date])
            _finalize_blended_return(t, entry_px, partial_qty, partial_exited)
            exits.append(t)
            continue

        # Max hold check — exit hold_qty at 10th-bar close
        if len(bars) >= MAX_HOLD_DAYS:
            last_bar = bars.iloc[MAX_HOLD_DAYS - 1]
            exit_px = float(last_bar["close"])
            gross = (exit_px / entry_px - 1) * 100
            net = gross - DELIVERY_COST
            t["status"] = "closed"
            t["exit_date"] = last_bar["date"]
            t["exit_price"] = round(exit_px, 4)
            t["exit_reason"] = "max_hold"
            t["exit_qty"] = hold_qty
            t["gross_return_pct"] = round(gross, 3)
            t["cost_pct"] = DELIVERY_COST
            t["net_return_pct"] = round(net, 3)
            t["hold_days"] = MAX_HOLD_DAYS
            _finalize_blended_return(t, entry_px, partial_qty, partial_exited)
            exits.append(t)

    return exits


def _finalize_blended_return(t: dict, entry_px: float, partial_qty: int, partial_exited: bool):
    """Compute blended net return across partial + hold legs (if partial enabled)."""
    if PARTIAL_PCT == 0 or not partial_exited or partial_qty == 0:
        # No partial: blended = hold return (already set as net_return_pct)
        t["blended_net_pct"] = t["net_return_pct"]
        return
    full_qty = t["full_qty"]
    hold_qty = t["hold_qty"]
    partial_w = partial_qty / full_qty
    hold_w = hold_qty / full_qty
    blended = partial_w * t["partial_net_pct"] + hold_w * t["net_return_pct"]
    t["blended_net_pct"] = round(blended, 3)


# --- EOD runner --------------------------------------------------------------

def run_eod(target_date: str | None = None) -> dict:
    today = target_date or ddate.today().isoformat()
    j = load_journal()

    new_alerts = ingest_new_alerts(j, today)
    filled = fill_pending_entries(j, today)
    exits = check_exits(j, today)

    active_after = sum(1 for t in j["trades"] if t.get("status") == "active")
    pending_after = sum(1 for t in j["trades"] if t.get("status") == "pending")
    closed_today = sum(
        1 for t in j["trades"]
        if t.get("status") == "closed" and t.get("exit_date") == today
    )

    j["daily_log"].append({
        "date": today,
        "new_alerts": len(new_alerts),
        "filled_today": len(filled),
        "closed_today": closed_today,
        "active_after": active_after,
        "pending_after": pending_after,
    })
    save_journal(j)

    closed = [t for t in j["trades"] if t.get("status") == "closed"]
    if closed:
        pd.DataFrame(closed).to_csv(TRADES_CSV, index=False)

    return {
        "date": today,
        "new_alerts": len(new_alerts),
        "filled_today": len(filled),
        "closed_today": closed_today,
        "active_after": active_after,
        "pending_after": pending_after,
        "exits": exits,
    }


# --- Stats printer -----------------------------------------------------------

def print_stats():
    j = load_journal()
    trades = j["trades"]
    closed  = [t for t in trades if t.get("status") == "closed"]
    active  = [t for t in trades if t.get("status") == "active"]
    pending = [t for t in trades if t.get("status") == "pending"]
    skipped = [t for t in trades if t.get("status") == "skipped"]
    skip_capital = sum(1 for t in skipped if t.get("skip_reason") == "capital_exhausted")
    skip_small = sum(1 for t in skipped if "too_small" in t.get("skip_reason", ""))
    skip_nodata = sum(1 for t in skipped if "no_entry_price" in t.get("skip_reason", ""))
    skip_legacy_cap = sum(1 for t in skipped if t.get("skip_reason") == "max_concurrent_cap")

    print(f"\n{'='*65}")
    print(f"  BLIND-BUY PAPER TRADER — STATS")
    print(f"{'='*65}")
    print(f"  Config:  {j['config']['strategy']}")
    print(f"  Started: {j['config']['started']}")
    print(f"  Total tracked: {len(trades)}")
    print(f"    Closed:  {len(closed)}")
    print(f"    Active:  {len(active)}")
    print(f"    Pending: {len(pending)}")
    print(f"    Skipped: {len(skipped)}  "
          f"({skip_capital} capital-full, {skip_small} too-small, "
          f"{skip_nodata} no-data, {skip_legacy_cap} legacy-cap)")

    if closed:
        df = pd.DataFrame(closed)
        n = len(df)
        ret_col = "blended_net_pct" if "blended_net_pct" in df.columns else "net_return_pct"
        df[ret_col] = pd.to_numeric(df[ret_col], errors="coerce")
        wins = df[df[ret_col] > 0]

        # Real ₹ P&L: per-trade position × pct, summed across trades.
        # Falls back to POSITION_INR (legacy ₹50K) for trades that pre-date sizing.
        df["_pos"] = pd.to_numeric(df.get("position_inr"), errors="coerce").fillna(POSITION_INR)
        df["_pnl_inr"] = df["_pos"] * df[ret_col] / 100.0
        cum_inr = df["_pnl_inr"].sum()
        avg_pos = df["_pos"].mean()
        cum_pct = df[ret_col].sum()

        print(f"\n  CLOSED TRADES ({n}):")
        print(f"    Win rate:       {len(wins)/n*100:.1f}%")
        print(f"    Avg net/trade:  {df[ret_col].mean():+.3f}%")
        print(f"    Median:         {df[ret_col].median():+.3f}%")
        print(f"    Best trade:     {df[ret_col].max():+.2f}%  ({df.loc[df[ret_col].idxmax(), 'symbol']})")
        print(f"    Worst trade:    {df[ret_col].min():+.2f}%  ({df.loc[df[ret_col].idxmin(), 'symbol']})")
        print(f"    Avg position:   ₹{avg_pos:,.0f}")
        print(f"    Cumulative net: {cum_pct:+.2f}% (sum of trade %s)")
        print(f"    Cumulative ₹:   ₹{cum_inr:+,.0f}  (actual qty × price diff)")
        print(f"    Return on ₹{TOTAL_CAPITAL_INR/100000:.0f}L capital: {cum_inr/TOTAL_CAPITAL_INR*100:+.2f}%")

        print(f"\n  By exit reason:")
        print(df.groupby("exit_reason")[ret_col].agg(["count", "mean"]).round(3).to_string())

        if "score" in df.columns:
            df["score_bucket"] = pd.cut(
                pd.to_numeric(df["score"], errors="coerce"),
                bins=[0, 49, 69, 100], labels=["<50", "50-69", "70+"]
            )
            print(f"\n  By score bucket:")
            print(df.groupby("score_bucket")[ret_col].agg(["count", "mean"]).round(3).to_string())

    if active:
        print(f"\n  ACTIVE POSITIONS ({len(active)}):")
        for t in active[:10]:
            stop_str = f"stop=₹{t['stop_price']:>8.2f}" if t.get("stop_price") else "stop=n/a"
            print(f"    {t['symbol']:12s}  entry=₹{t['entry_price']:>9.2f}  {stop_str}"
                  f"  on {t['entry_date']}  score={t.get('score', '?'):.0f}")

    if pending:
        print(f"\n  PENDING ({len(pending)}) — waiting for next-day open:")
        for t in pending[:5]:
            print(f"    {t['symbol']:12s}  alert={t['alert_date']}  score={t.get('score', '?'):.0f}")


# --- add-alert helper --------------------------------------------------------

def add_live_alert(row: dict):
    """Append a single blind-buy alert to the live alerts CSV."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    df_new = pd.DataFrame([row])
    if LIVE_ALERTS_CSV.exists():
        existing = pd.read_csv(LIVE_ALERTS_CSV)
        # Dedup: same date + symbol
        key = f"{row['date']}::{row['symbol'].upper()}"
        existing_keys = existing["date"].astype(str) + "::" + existing["symbol"].str.upper()
        if key in existing_keys.values:
            print(f"[skip] {row['symbol']} already in live alerts for {row['date']}")
            return
        combined = pd.concat([existing, df_new], ignore_index=True)
    else:
        combined = df_new
    combined.to_csv(LIVE_ALERTS_CSV, index=False)
    print(f"[added] {row['symbol']} @ ₹{row['alert_price']}  stop=₹{row.get('stop', 'n/a')}  date={row['date']}")


# --- CLI ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Blind-buy alert paper trader",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("--date", help="Run EOD for YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill-from", metavar="DATE",
                    help="Replay daily from DATE through today (YYYY-MM-DD)")
    ap.add_argument("--max-concurrent", type=int, default=None,
                    help="Override MAX_CONCURRENT for this run (e.g. 200 = no cap)")
    ap.add_argument("--no-stop", action="store_true",
                    help="Ignore alert stop prices — hold all 10 days (matches original backtest)")
    ap.add_argument("--stats", action="store_true", help="Print stats and exit")
    ap.add_argument("--add-alert", action="store_true",
                    help="Add a single live blind-buy alert to the store")
    ap.add_argument("--symbol", help="Symbol for --add-alert")
    ap.add_argument("--price", type=float, help="Alert price for --add-alert")
    ap.add_argument("--stop", type=float, help="Stop loss price for --add-alert")
    ap.add_argument("--target1", type=float, help="Target 1 for --add-alert")
    ap.add_argument("--target2", type=float, help="Target 2 for --add-alert")
    ap.add_argument("--score", type=float, default=0.0, help="Score for --add-alert")
    ap.add_argument("--alert-date", help="Alert date YYYY-MM-DD for --add-alert (default: today)")
    ap.add_argument("--alert-time", default="", help="Alert time HH:MM for --add-alert")
    args = ap.parse_args()

    if args.max_concurrent is not None:
        global MAX_CONCURRENT
        MAX_CONCURRENT = args.max_concurrent
    if args.no_stop:
        global _NO_STOP
        _NO_STOP = True

    if args.add_alert:
        if not args.symbol or not args.price:
            ap.error("--add-alert requires --symbol and --price")
        add_live_alert({
            "date": args.alert_date or ddate.today().isoformat(),
            "time": args.alert_time,
            "symbol": args.symbol.upper(),
            "alert_price": args.price,
            "stop": args.stop,
            "target1": args.target1,
            "target2": args.target2,
            "score": args.score,
        })
        return 0

    if args.stats:
        print_stats()
        return 0

    if args.backfill_from:
        all_alerts = load_all_blind_buy_alerts()
        if all_alerts.empty:
            print("No blind-buy alerts found.")
            return 1
        alert_dates = sorted(all_alerts["date"].unique())
        replay_dates = [d for d in alert_dates if d >= args.backfill_from]
        # Add extra days to close out positions after the last alert
        last = max(replay_dates) if replay_dates else args.backfill_from
        extra = []
        cur = ddate.fromisoformat(last)
        for _ in range(MAX_HOLD_DAYS + 3):
            cur += timedelta(days=1)
            extra.append(cur.isoformat())
        all_run = replay_dates + extra
        for d in all_run:
            res = run_eod(d)
            print(f"[{d}]  new={res['new_alerts']:>3}  filled={res['filled_today']:>3}"
                  f"  closed={res['closed_today']:>3}  active={res['active_after']:>3}"
                  f"  pending={res['pending_after']:>3}")
        print_stats()
        return 0

    res = run_eod(args.date)
    print(f"[{res['date']}]  new_alerts={res['new_alerts']}  filled={res['filled_today']}"
          f"  closed_today={res['closed_today']}  active={res['active_after']}"
          f"  pending={res['pending_after']}")
    for ex in res.get("exits", []):
        ret_key = "blended_net_pct" if "blended_net_pct" in ex else "net_return_pct"
        print(f"  EXIT {ex['symbol']:12s}  {ex['exit_reason']:10s}"
              f"  entry=₹{ex['entry_price']:>9.2f} → exit=₹{ex['exit_price']:>9.2f}"
              f"  net={ex[ret_key]:+.2f}%  hold={ex['hold_days']}d")
    print_stats()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
