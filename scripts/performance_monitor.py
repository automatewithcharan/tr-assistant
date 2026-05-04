#!/usr/bin/env python3
"""
Self-Monitoring Performance Report
====================================
Runs at market close (or on demand). Analyses the order log, calculates
what went well / what went wrong, and posts a structured feedback report
to Discord. The output is designed to be pasted into the AI chat to
iterate on the trading strategy.

Usage:
    python scripts/performance_monitor.py              # full report
    python scripts/performance_monitor.py --today      # today only
    python scripts/performance_monitor.py --no-discord # print only
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT     = Path(__file__).parent.parent
_LOG_PATH = _ROOT / "results" / "Data" / "kite_orders_log.csv"
_HIST_PATH = _ROOT / "results" / "Data" / "performance_history.jsonl"
_OUT_PATH  = _ROOT / "results" / "Data" / "performance_report_latest.md"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_orders(since: str = None) -> List[Dict]:
    if not _LOG_PATH.exists():
        return []
    rows = list(csv.DictReader(open(_LOG_PATH)))
    rows = [r for r in rows if r.get("success") == "True"]
    if since:
        rows = [r for r in rows if r.get("timestamp", "")[:10] >= since]
    return rows


def _load_all_orders_raw() -> List[Dict]:
    """All rows including failures — for failure analysis."""
    if not _LOG_PATH.exists():
        return []
    return list(csv.DictReader(open(_LOG_PATH)))


def _current_price(symbol: str) -> float:
    try:
        import yfinance as yf
        t = yf.Ticker(f"{symbol}.NS")
        h = t.history(period="2d")
        if h is None or len(h) == 0:
            return 0.0
        return float(h["Close"].iloc[-1])
    except Exception:
        return 0.0


def _nifty500_return(start_date: str) -> Optional[float]:
    try:
        import yfinance as yf
        from datetime import date
        end = date.today().isoformat()
        h = yf.download("^CRSLDX", start=start_date, end=end, auto_adjust=True)
        if h is None or len(h) < 2:
            return None
        return (float(h["Close"].iloc[-1]) - float(h["Close"].iloc[0])) / float(h["Close"].iloc[0]) * 100
    except Exception:
        return None


def _fifo_pnl(orders: List[Dict]) -> Tuple[Dict, List[Dict]]:
    buy_queue: Dict[str, List] = defaultdict(list)
    closed: List[Dict] = []
    for row in sorted(orders, key=lambda r: r.get("timestamp", "")):
        sym   = row["symbol"]
        qty   = float(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        ts    = row.get("timestamp", "")[:10]
        side  = row.get("side", "")
        if qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            buy_queue[sym].append({"qty": qty, "price": price, "date": ts})
        elif side == "SELL":
            remaining = qty
            while remaining > 0 and buy_queue[sym]:
                b = buy_queue[sym][0]
                matched = min(b["qty"], remaining)
                pnl = matched * (price - b["price"])
                closed.append({
                    "symbol":     sym,
                    "buy_date":   b["date"],
                    "sell_date":  ts,
                    "qty":        matched,
                    "buy_price":  b["price"],
                    "sell_price": price,
                    "pnl":        round(pnl, 2),
                    "pnl_pct":    round((price - b["price"]) / b["price"] * 100, 2),
                    "won":        pnl > 0,
                    "hold_days":  (datetime.fromisoformat(ts) - datetime.fromisoformat(b["date"])).days,
                })
                b["qty"] -= matched
                remaining -= matched
                if b["qty"] <= 0:
                    buy_queue[sym].pop(0)

    open_pos = {}
    for sym, queue in buy_queue.items():
        if not queue:
            continue
        total_qty  = sum(b["qty"] for b in queue)
        total_cost = sum(b["qty"] * b["price"] for b in queue)
        if total_qty > 0:
            open_pos[sym] = {
                "qty":        total_qty,
                "avg_cost":   total_cost / total_qty,
                "invested":   total_cost,
                "entry_date": queue[0]["date"],
            }
    return open_pos, closed


# ─────────────────────────────────────────────────────────────────────────────
# Failure analysis
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_failures(all_rows: List[Dict]) -> Dict:
    failed = [r for r in all_rows if r.get("success") != "True"]
    by_symbol: Dict[str, Dict] = defaultdict(lambda: {"count": 0, "reasons": defaultdict(int)})
    reason_totals: Dict[str, int] = defaultdict(int)

    for r in failed:
        sym = r.get("symbol", "UNKNOWN")
        msg = (r.get("message") or r.get("error") or "unknown error")[:120]
        # Bucket common errors
        if "market protection" in msg.lower() or "market_protection" in msg.lower():
            bucket = "MARKET_ORDER_REJECTED"
        elif "cdsl" in msg.lower() or "tpin" in msg.lower() or "authoris" in msg.lower():
            bucket = "CDSL_AUTH_REQUIRED"
        elif "insufficient" in msg.lower() or "funds" in msg.lower():
            bucket = "INSUFFICIENT_FUNDS"
        elif "market is closed" in msg.lower() or "closed" in msg.lower():
            bucket = "MARKET_CLOSED"
        elif "token" in msg.lower() or "login" in msg.lower() or "session" in msg.lower():
            bucket = "AUTH_TOKEN_EXPIRED"
        else:
            bucket = "OTHER"
        by_symbol[sym]["count"] += 1
        by_symbol[sym]["reasons"][bucket] += 1
        reason_totals[bucket] += 1

    return {
        "total_failures": len(failed),
        "by_reason":      dict(reason_totals),
        "top_symbols":    sorted(
            [{"symbol": s, "failures": v["count"], "top_reason": max(v["reasons"], key=v["reasons"].get)}
             for s, v in by_symbol.items()],
            key=lambda x: -x["failures"]
        )[:10],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Open-position health
# ─────────────────────────────────────────────────────────────────────────────

def _position_health(open_pos: Dict) -> List[Dict]:
    rows = []
    for sym, pos in open_pos.items():
        live = _current_price(sym)
        if live <= 0:
            live = pos["avg_cost"]
        pnl_pct = (live - pos["avg_cost"]) / pos["avg_cost"] * 100
        hold_days = (date.today() - date.fromisoformat(pos["entry_date"])).days
        sl_level  = pos["avg_cost"] * 0.95
        trail_activated = pnl_pct >= 5.0
        status = "OK"
        if pnl_pct <= -5:
            status = "PAST_STOP_LOSS"   # should have been exited already
        elif pnl_pct <= -3:
            status = "NEAR_STOP_LOSS"
        elif hold_days >= 10 and pnl_pct < 0:
            status = "OVERHELD_LOSER"   # past max hold days, still losing
        elif trail_activated:
            status = "TRAILING_ACTIVE"
        elif pnl_pct >= 10:
            status = "NEAR_TARGET"
        rows.append({
            "symbol":     sym,
            "qty":        pos["qty"],
            "avg_cost":   pos["avg_cost"],
            "live":       live,
            "pnl_pct":    round(pnl_pct, 2),
            "hold_days":  hold_days,
            "sl_level":   round(sl_level, 2),
            "invested":   pos["invested"],
            "status":     status,
            "entry_date": pos["entry_date"],
        })
    return sorted(rows, key=lambda x: x["pnl_pct"])


# ─────────────────────────────────────────────────────────────────────────────
# Feedback generator
# ─────────────────────────────────────────────────────────────────────────────

def _generate_feedback(closed: List[Dict], open_health: List[Dict], failures: Dict) -> List[str]:
    """
    Produce actionable plain-English feedback lines that can be fed back into
    AI chat to improve the bot's parameters and logic.
    """
    fb = []

    # Win rate and payoff
    if closed:
        wins   = [t for t in closed if t["won"]]
        losses = [t for t in closed if not t["won"]]
        wr = len(wins) / len(closed) * 100
        avg_win  = sum(t["pnl_pct"] for t in wins)  / max(len(wins),  1)
        avg_loss = sum(t["pnl_pct"] for t in losses) / max(len(losses), 1)
        payoff   = abs(avg_win / avg_loss) if avg_loss else None
        exp = (wr/100) * avg_win + (1 - wr/100) * avg_loss

        if wr >= 60:
            fb.append(f"✅ Win rate is good ({wr:.0f}%) — direction picks are correct.")
        else:
            fb.append(f"❌ Win rate low ({wr:.0f}%) — signals not reliable enough yet.")

        if payoff and payoff < 0.8:
            fb.append(
                f"⚠️  Payoff ratio is {payoff:.2f}x (avg win {avg_win:+.1f}% vs avg loss {avg_loss:.1f}%). "
                f"Winners are too small or losses are too large. "
                f"Fix: raise minimum target to ≥8% (edit KITE_SWING_MIN_TARGET2_PCT), "
                f"tighten stop to 3% for first 2 days (ATR-based), or cut positions earlier."
            )
        elif payoff and payoff >= 1.5:
            fb.append(f"✅ Payoff ratio strong ({payoff:.2f}x). Risk/reward is working.")

        if exp < 0:
            fb.append(
                f"❌ Expectancy is NEGATIVE ({exp:+.2f}% per trade). "
                f"Even with {wr:.0f}% wins, the average trade loses money. "
                f"Must improve payoff ratio before scaling capital."
            )
        else:
            fb.append(f"✅ Expectancy positive ({exp:+.2f}% per trade). Edge exists.")

        # Hold time analysis
        avg_hold_wins  = sum(t["hold_days"] for t in wins)  / max(len(wins), 1)
        avg_hold_loss  = sum(t["hold_days"] for t in losses) / max(len(losses), 1)
        if avg_hold_loss < avg_hold_wins:
            fb.append(
                f"ℹ️  Losing trades close faster ({avg_hold_loss:.1f} days) than winners ({avg_hold_wins:.1f} days). "
                f"This is ideal — let winners run."
            )
        else:
            fb.append(
                f"⚠️  Losing trades held longer ({avg_hold_loss:.1f} days) than winners ({avg_hold_wins:.1f} days). "
                f"Cutting winners early and holding losers too long. "
                f"Fix: implement trailing stop that activates earlier (change KITE_SWING_TRAIL_ACTIVATE_PCT)."
            )

    # Open position health
    past_sl    = [p for p in open_health if p["status"] == "PAST_STOP_LOSS"]
    overheld   = [p for p in open_health if p["status"] == "OVERHELD_LOSER"]
    near_sl    = [p for p in open_health if p["status"] == "NEAR_STOP_LOSS"]
    trailing   = [p for p in open_health if p["status"] == "TRAILING_ACTIVE"]
    near_tgt   = [p for p in open_health if p["status"] == "NEAR_TARGET"]

    if past_sl:
        syms = ", ".join(p["symbol"] for p in past_sl)
        losses_est = sum(p["invested"] * abs(p["pnl_pct"]) / 100 for p in past_sl)
        fb.append(
            f"🚨 STUCK POSITIONS past stop-loss (need manual exit): {syms}. "
            f"Estimated loss if held: ₹{losses_est:,.0f}. "
            f"Root cause: stop-loss orders were failing silently (fixed in latest commit). "
            f"Manual action: sell these via Kite app now."
        )

    if overheld:
        syms = ", ".join(p["symbol"] for p in overheld)
        fb.append(
            f"⚠️  Positions held past max hold days (10d) and still losing: {syms}. "
            f"Exit these — time-based stop should have triggered."
        )

    if trailing:
        syms = ", ".join(f"{p['symbol']} ({p['pnl_pct']:+.1f}%)" for p in trailing)
        fb.append(f"✅ Trailing stop active on: {syms}. Let these run to target.")

    if near_tgt:
        syms = ", ".join(f"{p['symbol']} ({p['pnl_pct']:+.1f}%)" for p in near_tgt)
        fb.append(f"🎯 Near target: {syms}. Monitor for exit signal.")

    # Failure analysis
    fail_reasons = failures.get("by_reason", {})
    if fail_reasons.get("MARKET_ORDER_REJECTED", 0) > 10:
        fb.append(
            f"🐛 BUG (now fixed): {fail_reasons['MARKET_ORDER_REJECTED']} MARKET order rejections. "
            f"Sell orders used market_protection=-1 (invalid). Fixed to use aggressive LIMIT orders."
        )
    if fail_reasons.get("CDSL_AUTH_REQUIRED", 0) > 0:
        fb.append(
            f"⚠️  CDSL authorization blocked {fail_reasons['CDSL_AUTH_REQUIRED']} sell attempts. "
            f"For CNC delivery: you need to authorize TPIN once per day at market open in Kite. "
            f"Fix: add a daily reminder or auto-trigger CDSL auth flow."
        )
    if fail_reasons.get("AUTH_TOKEN_EXPIRED", 0) > 10:
        fb.append(
            f"⚠️  {fail_reasons['AUTH_TOKEN_EXPIRED']} failures from expired Kite token. "
            f"Check TOTP auto-login is working — it should refresh daily at 9 AM."
        )

    return fb


# ─────────────────────────────────────────────────────────────────────────────
# Report builder
# ─────────────────────────────────────────────────────────────────────────────

def build_report(today_only: bool = False) -> str:
    since = date.today().isoformat() if today_only else None
    orders    = _load_orders(since)
    all_rows  = _load_all_orders_raw()
    if since:
        all_rows = [r for r in all_rows if r.get("timestamp", "")[:10] >= since]

    open_pos, closed = _fifo_pnl(orders)
    failures  = _analyse_failures(all_rows)
    health    = _position_health(open_pos)
    feedback  = _generate_feedback(closed, health, failures)

    today = date.today().isoformat()
    period = "Today" if today_only else "All time"

    # P&L summary
    total_real  = sum(t["pnl"] for t in closed)
    total_inv_c = sum(t["qty"] * t["buy_price"] for t in closed)
    total_inv_o = sum(p["invested"] for p in health)
    unreal      = sum(p["invested"] * p["pnl_pct"] / 100 for p in health)
    total_pnl   = total_real + unreal
    total_inv   = total_inv_c + total_inv_o
    pnl_pct     = total_pnl / total_inv * 100 if total_inv else 0

    wins   = [t for t in closed if t["won"]]
    losses = [t for t in closed if not t["won"]]
    wr     = len(wins) / len(closed) * 100 if closed else 0
    avg_w  = sum(t["pnl_pct"] for t in wins)  / max(len(wins), 1)
    avg_l  = sum(t["pnl_pct"] for t in losses) / max(len(losses), 1)
    payoff = abs(avg_w / avg_l) if avg_l else None
    exp    = (wr/100) * avg_w + (1 - wr/100) * avg_l if closed else 0

    first_date = min(
        [t["buy_date"] for t in closed] + [p.get("entry_date", today) for p in health],
        default=today
    )
    n500 = _nifty500_return(first_date) if not today_only else None

    def _sgn(v): return f"+{v:.2f}" if v >= 0 else f"{v:.2f}"

    lines = [
        f"# 🤖 Self-Monitoring Performance Report",
        f"**{period}**  |  Generated: {today}  |  Since: {first_date}",
        f"",
        f"---",
        f"## 📊 P&L Summary",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Deployed | ₹{total_inv:,.0f} |",
        f"| Realized P&L ({len(closed)} trades) | ₹{_sgn(total_real)} ({_sgn(total_real/total_inv_c*100 if total_inv_c else 0)}%) |",
        f"| Unrealized P&L ({len(health)} open) | ₹{_sgn(unreal)} |",
        f"| **Total P&L** | **₹{_sgn(total_pnl)} ({_sgn(pnl_pct)}%)** |",
        f"| Win rate | {wr:.0f}% ({len(wins)}W / {len(losses)}L) |",
        f"| Avg win / Avg loss | {_sgn(avg_w)}% / {avg_l:.2f}% |",
        f"| Payoff ratio | {f'{payoff:.2f}x' if payoff else '—'} |",
        f"| Expectancy / trade | {_sgn(exp)}% |",
    ]
    if n500:
        beat = pnl_pct > n500
        lines += [
            f"| NIFTY 500 (same period) | {_sgn(n500)}% |",
            f"| Beating benchmark? | {'✅ YES' if beat else '❌ NO'} |",
        ]
    lines.append("")

    # Order failure summary
    lines += [
        f"## ⚠️  Order Failures ({failures['total_failures']} total)",
        f"| Error Type | Count |",
        f"|-----------|-------|",
    ]
    for reason, count in sorted(failures["by_reason"].items(), key=lambda x: -x[1]):
        lines.append(f"| {reason} | {count} |")

    top_fail = failures["top_symbols"][:5]
    if top_fail:
        lines += ["", "**Most-failed symbols:**"]
        for s in top_fail:
            lines.append(f"- {s['symbol']}: {s['failures']} failures ({s['top_reason']})")
    lines.append("")

    # Position health
    if health:
        lines += [
            f"## 🏥 Open Position Health ({len(health)})",
            f"| Symbol | P&L% | Hold | Status | Action |",
            f"|--------|------|------|--------|--------|",
        ]
        action_map = {
            "PAST_STOP_LOSS":   "🚨 EXIT NOW via Kite app",
            "OVERHELD_LOSER":   "⏰ Exit — past max hold days",
            "NEAR_STOP_LOSS":   "👀 Watch — near 5% stop",
            "TRAILING_ACTIVE":  "✅ Hold — trail running",
            "NEAR_TARGET":      "🎯 Watch — near target",
            "OK":               "✅ Hold",
        }
        for p in health:
            action = action_map.get(p["status"], "—")
            lines.append(
                f"| {p['symbol']} | {_sgn(p['pnl_pct'])}% | {p['hold_days']}d "
                f"| {p['status']} | {action} |"
            )
        lines.append("")

    # Closed trades
    if closed:
        lines += [
            f"## 📋 Closed Trades ({len(closed)})",
            f"| | Symbol | Buy | Sell | Hold | P&L% |",
            f"|-|--------|-----|------|------|------|",
        ]
        for t in sorted(closed, key=lambda x: x["sell_date"]):
            icon = "✅" if t["won"] else "❌"
            lines.append(
                f"| {icon} | {t['symbol']} | {t['buy_date']} | {t['sell_date']} "
                f"| {t['hold_days']}d | {_sgn(t['pnl_pct'])}% |"
            )
        lines.append("")

    # AI Feedback
    lines += [
        f"## 🧠 Automated Feedback (paste into AI chat to improve the bot)",
        f"",
    ]
    for i, fb_line in enumerate(feedback, 1):
        lines.append(f"{i}. {fb_line}")
    lines.append("")

    lines += [
        "---",
        "*Generated by tr-assistant self-monitoring system*",
        f"*Run `python scripts/performance_monitor.py` to refresh*",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Discord sender
# ─────────────────────────────────────────────────────────────────────────────

def _send_discord(report: str) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        env_path = _ROOT / ".env.dev"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DISCORD_WEBHOOK_URL="):
                    webhook_url = line.split("=", 1)[1].strip()
                    break
    if not webhook_url:
        print("[warn] DISCORD_WEBHOOK_URL not set — skipping Discord.")
        return

    try:
        import requests
        # Discord message limit is 2000 chars; split into chunks
        chunks = []
        current = ""
        for line in report.splitlines():
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            chunks.append(current)

        for chunk in chunks:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=10)
            resp.raise_for_status()
        print(f"[ok] Sent {len(chunks)} Discord message(s).")
    except Exception as exc:
        print(f"[warn] Discord send failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# History logger — append one record per run for trend tracking
# ─────────────────────────────────────────────────────────────────────────────

def _append_history(report_text: str) -> None:
    orders    = _load_orders()
    all_rows  = _load_all_orders_raw()
    open_pos, closed = _fifo_pnl(orders)
    failures  = _analyse_failures(all_rows)
    health    = _position_health(open_pos)

    wins   = [t for t in closed if t["won"]]
    losses = [t for t in closed if not t["won"]]
    wr     = len(wins) / len(closed) * 100 if closed else 0
    avg_w  = sum(t["pnl_pct"] for t in wins)  / max(len(wins), 1)
    avg_l  = sum(t["pnl_pct"] for t in losses) / max(len(losses), 1)
    payoff = abs(avg_w / avg_l) if avg_l else None
    exp    = (wr/100) * avg_w + (1 - wr/100) * avg_l if closed else 0
    total_real = sum(t["pnl"] for t in closed)
    unreal     = sum(p["invested"] * p["pnl_pct"] / 100 for p in health)

    record = {
        "date":           date.today().isoformat(),
        "closed_trades":  len(closed),
        "open_positions": len(health),
        "win_rate_pct":   round(wr, 1),
        "avg_win_pct":    round(avg_w, 2),
        "avg_loss_pct":   round(avg_l, 2),
        "payoff_ratio":   round(payoff, 2) if payoff else None,
        "expectancy_pct": round(exp, 3),
        "realized_pnl":   round(total_real, 2),
        "unrealized_pnl": round(unreal, 2),
        "total_failures": failures["total_failures"],
        "market_order_rejections": failures["by_reason"].get("MARKET_ORDER_REJECTED", 0),
    }
    with open(_HIST_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--today",      action="store_true", help="Only analyse today's activity")
    parser.add_argument("--no-discord", action="store_true", help="Print only, don't send to Discord")
    args = parser.parse_args()

    report = build_report(today_only=args.today)
    print(report)
    _OUT_PATH.write_text(report, encoding="utf-8")
    print(f"\nSaved to: {_OUT_PATH}")

    _append_history(report)

    if not args.no_discord:
        _send_discord(report)


if __name__ == "__main__":
    main()
