#!/usr/bin/env python3
"""
Portfolio Performance Report
==============================
Reads kite_orders_log.csv → calculates real P&L → compares vs NIFTY 500.

Usage:
    python scripts/portfolio_report.py
    python scripts/portfolio_report.py --watch   # refresh every 60s
"""

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT      = Path(__file__).parent.parent
_LOG_PATH  = _ROOT / "results" / "Data" / "kite_orders_log.csv"
_OUT_PATH  = _ROOT / "results" / "Data" / "portfolio_report_latest.md"


def _load_orders() -> List[Dict]:
    if not _LOG_PATH.exists():
        return []
    rows = list(csv.DictReader(open(_LOG_PATH)))
    return [r for r in rows if r.get("success") == "True"]


def _current_price(symbol: str) -> Optional[float]:
    try:
        import yfinance as yf
        t = yf.Ticker(f"{symbol}.NS")
        h = t.history(period="2d")
        if h is None or len(h) == 0:
            return None
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def _nifty500_return(start_date: str, end_date: str = None) -> Optional[float]:
    """NIFTY 500 return between two dates."""
    try:
        import yfinance as yf
        end = end_date or date.today().isoformat()
        h = yf.download("^CRSLDX", start=start_date, end=end,
                         auto_adjust=True)
        if h is None or len(h) < 2:
            return None
        start_price = float(h["Close"].iloc[0])
        end_price   = float(h["Close"].iloc[-1])
        return (end_price - start_price) / start_price * 100
    except Exception:
        return None


def _calc_pnl(orders: List[Dict]) -> Tuple[Dict, Dict, List[Dict]]:
    """
    FIFO matching of buys → sells.
    Returns:
        open_positions: {symbol: {qty, avg_cost, invested}}
        closed_trades: [{symbol, buy_date, sell_date, qty, buy_price, sell_price, pnl, pnl_pct}]
        all_trades: flat list
    """
    # Separate buys and sells per symbol
    buy_queue: Dict[str, List] = defaultdict(list)   # FIFO queue of (qty, price, date)
    closed_trades: List[Dict]  = []

    # Sort all orders by timestamp
    orders_sorted = sorted(orders, key=lambda r: r.get("timestamp", ""))

    for row in orders_sorted:
        sym   = row["symbol"]
        qty   = float(row["qty"] or 0)
        price = float(row["price"] or 0)
        ts    = row.get("timestamp", "")[:10]
        side  = row["side"]

        if qty <= 0 or price <= 0:
            continue

        if side == "BUY":
            buy_queue[sym].append({"qty": qty, "price": price, "date": ts})

        elif side == "SELL":
            remaining_sell = qty
            while remaining_sell > 0 and buy_queue[sym]:
                buy = buy_queue[sym][0]
                matched = min(buy["qty"], remaining_sell)
                pnl     = matched * (price - buy["price"])
                pnl_pct = (price - buy["price"]) / buy["price"] * 100

                closed_trades.append({
                    "symbol":     sym,
                    "buy_date":   buy["date"],
                    "sell_date":  ts,
                    "qty":        matched,
                    "buy_price":  round(buy["price"], 2),
                    "sell_price": round(price, 2),
                    "pnl":        round(pnl, 2),
                    "pnl_pct":    round(pnl_pct, 2),
                    "won":        pnl > 0,
                })
                buy["qty"] -= matched
                remaining_sell -= matched
                if buy["qty"] <= 0:
                    buy_queue[sym].pop(0)

    # Open positions = remaining in buy queues
    open_positions: Dict[str, Dict] = {}
    for sym, queue in buy_queue.items():
        if not queue:
            continue
        total_qty  = sum(b["qty"] for b in queue)
        total_cost = sum(b["qty"] * b["price"] for b in queue)
        if total_qty > 0:
            open_positions[sym] = {
                "qty":      total_qty,
                "avg_cost": total_cost / total_qty,
                "invested": total_cost,
                "entry_date": queue[0]["date"],
            }

    return open_positions, closed_trades


def _print_and_save(report: str):
    print(report)
    _OUT_PATH.write_text(report, encoding="utf-8")


def build_report(verbose: bool = True) -> str:
    orders = _load_orders()
    if not orders:
        return "No successful orders found in kite_orders_log.csv"

    open_pos, closed = _calc_pnl(orders)

    # ── Realized P&L ──────────────────────────────────────────────────────────
    total_realized   = sum(t["pnl"] for t in closed)
    total_invested_c = sum(t["qty"] * t["buy_price"] for t in closed)
    wins  = [t for t in closed if t["won"]]
    losses= [t for t in closed if not t["won"]]
    win_rate = len(wins)/len(closed)*100 if closed else 0
    avg_win  = sum(t["pnl_pct"] for t in wins)/len(wins)     if wins   else 0
    avg_loss = sum(t["pnl_pct"] for t in losses)/len(losses) if losses else 0
    payoff   = abs(avg_win/avg_loss) if avg_loss else None

    # ── Open positions (fetch live prices) ────────────────────────────────────
    total_unrealized = 0.0
    total_invested_o = 0.0
    open_rows        = []

    for sym, pos in open_pos.items():
        live = _current_price(sym)
        if live:
            unreal_pnl  = (live - pos["avg_cost"]) * pos["qty"]
            unreal_pct  = (live - pos["avg_cost"]) / pos["avg_cost"] * 100
        else:
            unreal_pnl  = 0.0
            unreal_pct  = 0.0
            live        = pos["avg_cost"]

        total_unrealized += unreal_pnl
        total_invested_o += pos["invested"]
        open_rows.append({
            "symbol":    sym,
            "qty":       pos["qty"],
            "avg_cost":  pos["avg_cost"],
            "live":      live,
            "invested":  pos["invested"],
            "unreal_pnl": unreal_pnl,
            "unreal_pct": unreal_pct,
            "entry_date": pos["entry_date"],
        })
    open_rows.sort(key=lambda x: -abs(x["unreal_pnl"]))

    # ── Total ──────────────────────────────────────────────────────────────────
    total_pnl      = total_realized + total_unrealized
    total_deployed = total_invested_c + total_invested_o
    total_pnl_pct  = total_pnl / total_deployed * 100 if total_deployed > 0 else 0

    # ── Benchmark (NIFTY 500 since first trade) ────────────────────────────────
    first_date = min((t["buy_date"] for t in closed), default=None)
    if not first_date and open_rows:
        first_date = min(r["entry_date"] for r in open_rows)
    n500_ret = _nifty500_return(first_date) if first_date else None

    today      = date.today().isoformat()
    days_since = (date.today() - date.fromisoformat(first_date)).days if first_date else 0

    # ── Format report ──────────────────────────────────────────────────────────
    def _sgn(v): return f"+{v:.2f}" if v >= 0 else f"{v:.2f}"

    lines = [
        f"# Portfolio Performance Report",
        f"",
        f"**Generated:** {today}  |  **Trading since:** {first_date}  ({days_since} days)",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total deployed | ₹{total_deployed:,.0f} |",
        f"| Realized P&L | ₹{_sgn(total_realized)} ({_sgn(total_realized/total_invested_c*100 if total_invested_c else 0)}%) |",
        f"| Unrealized P&L | ₹{_sgn(total_unrealized)} |",
        f"| **Total P&L** | **₹{_sgn(total_pnl)} ({_sgn(total_pnl_pct)}%)** |",
        f"| Closed trades | {len(closed)} |",
        f"| Open positions | {len(open_pos)} |",
        f"| Win rate | {win_rate:.1f}% ({len(wins)}W / {len(losses)}L) |",
        f"| Avg win | {_sgn(avg_win)}% |",
        f"| Avg loss | {avg_loss:.2f}% |",
        f"| Payoff ratio | {f'{payoff:.2f}x' if payoff else '—'} |",
        f"",
    ]

    # Benchmark comparison
    if n500_ret is not None:
        beat = total_pnl_pct > n500_ret
        lines += [
            f"## vs Benchmark (since {first_date})",
            f"",
            f"| | Your portfolio | NIFTY 500 |",
            f"|-|---------------|-----------|",
            f"| Return | {_sgn(total_pnl_pct)}% | {_sgn(n500_ret)}% |",
            f"| Beating? | {'✅ YES' if beat else '❌ NO'} | — |",
            f"",
        ]

    # Closed trades
    if closed:
        lines += [
            f"## Closed Trades ({len(closed)})",
            f"",
            f"| Symbol | Buy | Sell | Qty | Buy ₹ | Sell ₹ | P&L ₹ | P&L% |",
            f"|--------|-----|------|-----|-------|--------|-------|------|",
        ]
        for t in sorted(closed, key=lambda x: x["sell_date"]):
            icon = "✅" if t["won"] else "❌"
            lines.append(
                f"| {icon} {t['symbol']} | {t['buy_date']} | {t['sell_date']} | "
                f"{t['qty']:.0f} | ₹{t['buy_price']:,.2f} | ₹{t['sell_price']:,.2f} | "
                f"₹{_sgn(t['pnl'])} | {_sgn(t['pnl_pct'])}% |"
            )
        lines.append("")

    # Open positions
    if open_rows:
        lines += [
            f"## Open Positions ({len(open_rows)})",
            f"",
            f"| Symbol | Qty | Avg Cost | Live Price | Invested | Unrealized P&L |",
            f"|--------|-----|----------|------------|----------|----------------|",
        ]
        for r in open_rows:
            icon = "🟢" if r["unreal_pnl"] >= 0 else "🔴"
            lines.append(
                f"| {icon} {r['symbol']} | {r['qty']:.0f} | ₹{r['avg_cost']:,.2f} | "
                f"₹{r['live']:,.2f} | ₹{r['invested']:,.0f} | "
                f"₹{_sgn(r['unreal_pnl'])} ({_sgn(r['unreal_pct'])}%) |"
            )
        lines.append("")

    lines += [
        "---",
        "*Generated by tr-assistant portfolio report*",
    ]

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true",
                        help="Refresh every 60 seconds")
    args = parser.parse_args()

    if args.watch:
        while True:
            report = build_report()
            print("\033[2J\033[H", end="")   # clear screen
            _print_and_save(report)
            print(f"\n[auto-refresh every 60s — Ctrl+C to stop]")
            time.sleep(60)
    else:
        report = build_report()
        _print_and_save(report)
        print(f"\nSaved to: {_OUT_PATH}")


if __name__ == "__main__":
    main()
