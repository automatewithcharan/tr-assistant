"""Per-stock outcome tracker — learns from real forward paper trading.

Aggregates closed trades from ALL paper traders by symbol:
  - swing_alert_paper/journal.json
  - intraday_alert_paper/journal.json
  - swing_paper_trades/paper_journal.json

For each stock, tracks:
  - Total trades
  - Win/loss split
  - Average return per trade
  - Best / worst single trade
  - Last trade date
  - Status: PROVEN_WINNER | NEUTRAL | WATCH | BLACKLISTED

Provides is_blacklisted(symbol) for paper traders to filter future entries.

Blacklist criteria (conservative — needs real evidence):
  - 3+ closed trades AND win rate < 20% AND avg net <= -2%
  - OR 5+ closed trades AND win rate < 30% AND avg net < 0%

Re-evaluated on every trade closure. A blacklisted stock can be un-flagged
if its next trade is a winner (gives it a chance to recover).
"""

from __future__ import annotations
import argparse, json
from datetime import datetime, date as ddate
from pathlib import Path
from collections import defaultdict
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
JOURNALS = [
    RESULTS / "swing_alert_paper" / "journal.json",
    RESULTS / "intraday_alert_paper" / "journal.json",
    RESULTS / "swing_paper_trades" / "paper_journal.json",
]
TRACKER_PATH = RESULTS / "per_stock_tracker" / "tracker.json"
TRACKER_CSV = RESULTS / "per_stock_tracker" / "tracker.csv"

# Blacklist rules — conservative
BLACKLIST_MIN_TRADES_TIGHT = 3      # Smaller sample if very bad
BLACKLIST_MIN_WR_TIGHT = 20.0
BLACKLIST_MIN_AVG_TIGHT = -2.0

BLACKLIST_MIN_TRADES_LOOSE = 5      # Larger sample if moderately bad
BLACKLIST_MIN_WR_LOOSE = 30.0
BLACKLIST_MIN_AVG_LOOSE = 0.0

# Promote-to-winner criteria
WINNER_MIN_TRADES = 2
WINNER_MIN_WR = 75.0
WINNER_MIN_AVG = 5.0


def _ret_field(trade: dict) -> float | None:
    """Pick whichever return field exists in this trade record."""
    for k in ("net_return_pct", "trade_return_pct", "gross_return_pct"):
        v = trade.get(k)
        if v is not None:
            try: return float(v)
            except: continue
    return None


def aggregate_trades() -> dict[str, list[dict]]:
    """Walk all paper journals and collect closed trades grouped by symbol."""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for jp in JOURNALS:
        if not jp.exists(): continue
        try:
            j = json.loads(jp.read_text())
        except Exception:
            continue
        for t in j.get("trades", []):
            if t.get("status") != "closed":
                continue
            sym = str(t.get("symbol", "")).strip().upper()
            if not sym:
                continue
            ret = _ret_field(t)
            if ret is None:
                continue
            entry_date = t.get("entry_date") or t.get("signal_date") or "?"
            exit_date = t.get("exit_date", "?")
            by_sym[sym].append({
                "source": jp.parent.name,
                "tier": t.get("tier", "?"),
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "net_return_pct": round(ret, 3),
                "exit_reason": t.get("exit_reason", "?"),
            })
    return dict(by_sym)


def compute_track_record() -> dict:
    """Build the full per-stock track record with status flags."""
    by_sym = aggregate_trades()
    record = {}
    for sym, trades in by_sym.items():
        trades_sorted = sorted(trades, key=lambda x: str(x.get("exit_date", "")))
        n = len(trades_sorted)
        rets = [t["net_return_pct"] for t in trades_sorted]
        wins = sum(1 for r in rets if r > 0)
        wr = wins / n * 100
        avg = sum(rets) / n
        best = max(rets); worst = min(rets)

        # Determine status
        status = "NEUTRAL"
        reason = ""
        if n >= WINNER_MIN_TRADES and wr >= WINNER_MIN_WR and avg >= WINNER_MIN_AVG:
            status = "PROVEN_WINNER"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}%"
        elif n >= BLACKLIST_MIN_TRADES_TIGHT and wr <= BLACKLIST_MIN_WR_TIGHT and avg <= BLACKLIST_MIN_AVG_TIGHT:
            status = "BLACKLISTED"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}% (tight rule)"
        elif n >= BLACKLIST_MIN_TRADES_LOOSE and wr <= BLACKLIST_MIN_WR_LOOSE and avg <= BLACKLIST_MIN_AVG_LOOSE:
            status = "BLACKLISTED"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}% (loose rule)"
        elif n >= 3 and (wr < 40 or avg < 0):
            status = "WATCH"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}%"
        elif n >= 2 and wr >= 60 and avg >= 2:
            status = "WATCH_POSITIVE"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}%"

        record[sym] = {
            "n": n, "wins": wins, "win_rate_pct": round(wr, 1),
            "avg_net_pct": round(avg, 3), "best_pct": round(best, 2), "worst_pct": round(worst, 2),
            "total_cum_pct": round(sum(rets), 2),
            "first_trade": trades_sorted[0].get("exit_date", "?"),
            "last_trade": trades_sorted[-1].get("exit_date", "?"),
            "status": status, "status_reason": reason,
            "trades": trades_sorted,
        }
    return record


def save_tracker(record: dict):
    TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRACKER_PATH.write_text(json.dumps(record, indent=2, default=str))
    # Flat CSV
    rows = [{
        "symbol": s, "n": r["n"], "wins": r["wins"], "wr": r["win_rate_pct"],
        "avg_net": r["avg_net_pct"], "total_cum": r["total_cum_pct"],
        "best": r["best_pct"], "worst": r["worst_pct"],
        "first": r["first_trade"], "last": r["last_trade"],
        "status": r["status"], "reason": r["status_reason"],
    } for s, r in record.items()]
    pd.DataFrame(rows).sort_values("total_cum", ascending=False).to_csv(TRACKER_CSV, index=False)


def load_tracker() -> dict:
    if TRACKER_PATH.exists():
        return json.loads(TRACKER_PATH.read_text())
    return {}


def is_blacklisted(symbol: str) -> tuple[bool, str]:
    """For paper traders to call. Returns (blocked, reason)."""
    record = load_tracker()
    r = record.get(str(symbol).strip().upper())
    if not r:
        return False, ""
    if r["status"] == "BLACKLISTED":
        return True, r.get("status_reason", "blacklisted")
    return False, ""


def is_proven_winner(symbol: str) -> tuple[bool, str]:
    record = load_tracker()
    r = record.get(str(symbol).strip().upper())
    if not r:
        return False, ""
    if r["status"] == "PROVEN_WINNER":
        return True, r.get("status_reason", "proven winner")
    return False, ""


def print_report():
    record = compute_track_record()
    if not record:
        print("[i] No closed trades in any journal yet.")
        return
    save_tracker(record)
    print(f"[+] Tracker updated: {len(record)} symbols")
    print(f"    → {TRACKER_PATH}")
    print(f"    → {TRACKER_CSV}")

    by_status = defaultdict(list)
    for s, r in record.items():
        by_status[r["status"]].append((s, r))

    print(f"\n=== STATUS SUMMARY ===")
    for status in ["PROVEN_WINNER", "WATCH_POSITIVE", "NEUTRAL", "WATCH", "BLACKLISTED"]:
        items = by_status.get(status, [])
        print(f"  {status:18}  {len(items)} symbols")

    if by_status.get("PROVEN_WINNER"):
        print(f"\n🟢 PROVEN WINNERS (forward-validated):")
        for s, r in sorted(by_status["PROVEN_WINNER"], key=lambda x: -x[1]["total_cum_pct"]):
            print(f"  {s:12} n={r['n']:>2} WR={r['win_rate_pct']:>5.1f}% avg={r['avg_net_pct']:+6.2f}% total={r['total_cum_pct']:+6.2f}% reason: {r['status_reason']}")

    if by_status.get("BLACKLISTED"):
        print(f"\n🔴 BLACKLISTED (will be skipped by paper trader):")
        for s, r in sorted(by_status["BLACKLISTED"], key=lambda x: x[1]["total_cum_pct"]):
            print(f"  {s:12} n={r['n']:>2} WR={r['win_rate_pct']:>5.1f}% avg={r['avg_net_pct']:+6.2f}% total={r['total_cum_pct']:+6.2f}% reason: {r['status_reason']}")

    if by_status.get("WATCH"):
        print(f"\n⚠️  WATCH (mixed performance, monitoring):")
        for s, r in sorted(by_status["WATCH"], key=lambda x: x[1]["total_cum_pct"])[:10]:
            print(f"  {s:12} n={r['n']:>2} WR={r['win_rate_pct']:>5.1f}% avg={r['avg_net_pct']:+6.2f}% total={r['total_cum_pct']:+6.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", help="Check status of one symbol")
    args = ap.parse_args()
    if args.check:
        r = load_tracker().get(args.check.upper())
        if not r:
            print(f"{args.check}: no trade history yet")
        else:
            print(json.dumps(r, indent=2, default=str))
        return 0
    print_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
