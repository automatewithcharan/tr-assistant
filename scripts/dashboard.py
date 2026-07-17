#!/usr/bin/env python3
"""Local status dashboard for tr-assistant.

Reads the REAL journals + REAL Kite positions directly — never the Discord
self-reports. One honest place to answer "is it working and where do I stand".

Run it:
    .venv/bin/python3 scripts/dashboard.py
    # then open http://localhost:8501 in a browser ON this machine

View it from your laptop securely (no public exposure, no firewall change):
    ssh -L 8501:localhost:8501 <user>@<vm-ip>
    # then open http://localhost:8501 on your laptop

By default it binds to 127.0.0.1 (local only). To expose on the VM's network
interface instead (understand the risk — this page shows your real positions):
    DASH_HOST=0.0.0.0 .venv/bin/python3 scripts/dashboard.py
"""
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "results" / "Data"
IST = ZoneInfo("Asia/Kolkata")

HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8501"))

# Lanes intentionally paused (cron disabled). Their journal is frozen history,
# not live measurement — shown greyed so a stale number isn't mistaken for a live one.
PAUSED_LANES = {"Intraday (Tier C)"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import live_alert_watch
except Exception:
    live_alert_watch = None

_WATCH_CACHE = {"ts": 0.0, "data": None}


def get_watch():
    """Live-alert watch, recomputed at most every 10 min (reads many files)."""
    if live_alert_watch is None:
        return None
    if _WATCH_CACHE["data"] is None or time.time() - _WATCH_CACHE["ts"] > 600:
        try:
            _WATCH_CACHE["data"] = live_alert_watch.compute_watch()
            _WATCH_CACHE["ts"] = time.time()
        except Exception:
            pass
    return _WATCH_CACHE["data"]


# Stage-1 gate for blind-buy: 30 closed trades accumulated FORWARD (i.e. closed
# on/after the freeze baseline). The 23 backfill-seed trades all exited by
# 2026-05-14, so this date cleanly separates seed from real forward evidence.
BLIND_BUY_JOURNAL = DATA / "blind_buy_alert_paper" / "journal.json"
FORWARD_START = "2026-05-28"
STAGE1_GATE = 30


def blind_buy_gate():
    j = load_json(BLIND_BUY_JOURNAL, {})
    trades = j.get("trades", [])
    closed = [t for t in trades if t.get("status") == "closed"]
    fwd = [t for t in closed if str(t.get("exit_date") or "") >= FORWARD_START]
    seed = [t for t in closed if str(t.get("exit_date") or "") < FORWARD_START]
    active = [t for t in trades if t.get("status") == "active"]
    fwd_rets = [t["net_return_pct"] for t in fwd if "net_return_pct" in t]
    seed_rets = [t["net_return_pct"] for t in seed if "net_return_pct" in t]
    return {
        "fwd": len(fwd),
        "gate": STAGE1_GATE,
        "active": len(active),
        "fwd_avg": (statistics.mean(fwd_rets) if fwd_rets else None),
        "fwd_wr": (sum(1 for r in fwd_rets if r > 0) / len(fwd_rets) * 100 if fwd_rets else None),
        "seed": len(seed),
        "seed_avg": (statistics.mean(seed_rets) if seed_rets else None),
    }


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_csv_rows(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def truthy(value):
    return str(value or "").strip().lower() in {"1", "1.0", "true", "yes", "y", "on"}


def ranked_confirmation_stats():
    root = DATA / "ranked_confirmation_paper"
    rows = load_csv_rows(root / "trades_latest.csv")
    closed = [row for row in rows if truthy(row.get("closed"))]
    open_rows = [row for row in rows if not truthy(row.get("closed"))]
    pnls = [float(row.get("net_pnl") or 0.0) for row in closed]
    wins = sum(1 for value in pnls if value > 0)
    losses = sum(1 for value in pnls if value < 0)
    streak = 0
    for value in reversed(pnls):
        if value < 0:
            streak += 1
        else:
            break
    candidates = load_csv_rows(root / "candidates_latest.csv")
    return {
        "date": str(rows[0].get("date", "")) if rows else "",
        "closed": len(closed),
        "open": len(open_rows),
        "candidates": len(candidates),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(closed) * 100.0 if closed else 0.0,
        "net_pnl": sum(pnls),
        "targets": sum(1 for row in closed if row.get("exit_reason") == "target_hit"),
        "stops": sum(1 for row in closed if row.get("exit_reason") == "stop_hit"),
        "consecutive_losses": streak,
        "entry_paused": streak >= 2,
        "trades": rows,
    }


def evidence_readiness_stats():
    rows = load_csv_rows(DATA / "live_feature_recorder" / "evidence_readiness.csv")
    eligible = [row for row in rows if truthy(row.get("StrategyEligibleBucket"))]

    def number(row, field):
        try:
            return float(row.get(field) or 0.0)
        except Exception:
            return 0.0

    best = max(eligible, key=lambda row: number(row, "StressAvgDailyReturnPct"), default={})
    status_paths = sorted((DATA / "live_feature_recorder" / "eod").glob("*/status.json"))
    if not status_paths:
        status_paths = sorted((DATA / "live_feature_recorder" / "bootstrap").glob("*/status.json"))
    status = load_json(status_paths[-1], {}) if status_paths else {}
    return {
        "rows": len(rows),
        "sessions": max((int(number(row, "Sessions")) for row in rows), default=0),
        "shadow_candidates": sum(1 for row in rows if truthy(row.get("ShadowCandidate"))),
        "goal_passes": sum(1 for row in rows if truthy(row.get("GoalEvidencePassed"))),
        "best_bucket": str(best.get("FeatureBucket", "")),
        "best_contract": str(best.get("Contract", "")),
        "best_coverage": number(best, "CoveragePct"),
        "best_stress_return": number(best, "StressAvgDailyReturnPct"),
        "best_stress_pnl": number(best, "ProjectedStressDailyPnl"),
        "best_lower_bound": number(best, "StressReturnLower95Pct"),
        "coverage": float(status.get("combined_coverage_pct") or 0.0),
        "coverage_passed": bool(status.get("coverage_gate_passed", False)),
        "evidence_accepted": False,
        "status_date": str(status.get("date") or status.get("end_date") or ""),
    }


def ranked_replay_readiness_stats():
    rows = load_csv_rows(DATA / "ranked_confirmation_replay" / "readiness.csv")

    def number(row, field):
        try:
            return float(row.get(field) or 0.0)
        except Exception:
            return 0.0

    best = max(
        rows,
        key=lambda row: (
            truthy(row.get("ShadowCandidate")),
            number(row, "StressLower95DailyPnl"),
            number(row, "StressAvgDailyPnl"),
        ),
        default={},
    )
    return {
        "configs": len(rows),
        "complete_days": max((int(number(row, "CompleteDays")) for row in rows), default=0),
        "closed_trades": int(number(best, "ClosedTrades")),
        "wins": int(number(best, "Wins")),
        "best_config": str(best.get("ConfigName", "")),
        "stress_avg_daily_pnl": number(best, "StressAvgDailyPnl"),
        "stress_lower95_daily_pnl": number(best, "StressLower95DailyPnl"),
        "profit_factor": number(best, "ProfitFactor"),
        "shadow_candidates": sum(1 for row in rows if truthy(row.get("ShadowCandidate"))),
        "goal_passes": sum(1 for row in rows if truthy(row.get("GoalEvidencePassed"))),
        "paper_promotion_allowed": any(truthy(row.get("PaperPromotionAllowed")) for row in rows),
        "real_orders_allowed": False,
    }


# ---------------------------------------------------------------- paper lanes

def lane_stats(path, label, closed_filter, ret_field="net_return_pct"):
    j = load_json(path, {})
    cfg = j.get("config", {})
    trades = j.get("trades", [])
    closed = [t for t in trades if closed_filter(t)]
    active = [t for t in trades if t.get("status") in ("active", "open")]
    pending = [t for t in trades if t.get("status") == "pending"]
    rets = [t.get(ret_field, 0.0) for t in closed if ret_field in t]
    # Account-level booked P&L = per-trade position × return, summed. Only meaningful
    # when positions are ₹-sized and a capital base is known (else show "—").
    cap = cfg.get("total_capital_inr")
    sized = [t for t in closed if t.get("position_inr") and ret_field in t]
    cum_inr = sum(t["position_inr"] * t[ret_field] / 100.0 for t in sized) if sized else None
    on_cap = (cum_inr / cap * 100) if (cum_inr is not None and cap) else None
    if rets:
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100
        avg = statistics.mean(rets)
    else:
        wr = avg = 0.0
    return {
        "label": label,
        "closed": len(closed),
        "active": len(active),
        "pending": len(pending),
        "wr": wr,
        "avg": avg,
        "has_data": bool(rets),
        "cum_inr": cum_inr,
        "cap": cap,
        "on_cap": on_cap,
        "started": cfg.get("started"),
    }


def all_lanes():
    return [
        lane_stats(
            DATA / "blind_buy_alert_paper" / "journal.json",
            "Blind-Buy",
            lambda t: t.get("status") == "closed",
        ),
        lane_stats(
            DATA / "intraday_alert_paper" / "journal.json",
            "Intraday (Tier C)",
            lambda t: not t.get("skipped") and "net_return_pct" in t,
        ),
        lane_stats(
            DATA / "swing_alert_paper" / "journal.json",
            "Swing-Alert",
            lambda t: t.get("status") == "closed",
        ),
        lane_stats(
            DATA / "swing_paper_trades" / "paper_journal.json",
            "Swing-Momentum",
            lambda t: t.get("status") == "closed",
        ),
    ]


# ------------------------------------------------------------- real positions

def real_positions():
    pos = load_json(DATA / "kite_tracked_positions.json", {})
    ticks = load_json(DATA / "kiteticker_all_ticks_latest.json", {})
    rows = []
    for sym, p in pos.items():
        if not isinstance(p, dict) or p.get("status") != "open":
            continue
        qty = p.get("qty", 0) or 0
        buy = p.get("buy_price", 0) or 0
        tick = ticks.get(sym, {})
        cur = tick.get("last_price") or tick.get("close") or 0
        invested = qty * buy
        value = qty * cur
        pnl = value - invested
        pnlpct = (cur / buy - 1) * 100 if buy and cur else 0.0
        stop = p.get("stop_loss", 0) or 0
        rows.append({
            "sym": sym,
            "qty": qty,
            "buy": buy,
            "cur": cur,
            "invested": invested,
            "value": value,
            "pnl": pnl,
            "pnlpct": pnlpct,
            "stop": stop,
            "gtt": p.get("gtt_status", ""),
            "stage": p.get("swing_stage", ""),
            "has_price": bool(cur),
            "blown": bool(cur and stop and cur < stop),
            "unprotected": p.get("gtt_status", "") == "missing",
            "last_trade": tick.get("last_trade_time", ""),
        })
    rows.sort(key=lambda r: r["pnlpct"])  # worst first
    return rows


# -------------------------------------------------------------------- render

def money(x):
    value = float(x or 0.0)
    sign = "-" if value < 0 else ""
    return f"{sign}₹{abs(value):,.0f}"


def render():
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    positions = real_positions()
    lanes = all_lanes()
    ranked = ranked_confirmation_stats()
    evidence = evidence_readiness_stats()
    ranked_replay = ranked_replay_readiness_stats()

    tot_inv = sum(r["invested"] for r in positions)
    tot_val = sum(r["value"] for r in positions)
    tot_pnl = tot_val - tot_inv
    tot_pct = (tot_val / tot_inv - 1) * 100 if tot_inv else 0.0

    pos_rows = ""
    for r in positions:
        cls = "loss" if r["pnl"] < 0 else "gain"
        badges = ""
        if r["blown"]:
            badges += '<span class="badge red">STOP BLOWN</span>'
        if r["unprotected"]:
            badges += '<span class="badge orange">UNPROTECTED</span>'
        cur_disp = f"{r['cur']:.2f}" if r["has_price"] else "<i>no live price</i>"
        pos_rows += f"""<tr class="{cls}">
            <td class="sym">{escape(r['sym'])} {badges}</td>
            <td class="num">{r['qty']}</td>
            <td class="num">{r['buy']:.2f}</td>
            <td class="num">{cur_disp}</td>
            <td class="num">{money(r['invested'])}</td>
            <td class="num {cls}">{r['pnlpct']:+.1f}%</td>
            <td class="num {cls}">{money(r['pnl'])}</td>
            <td class="num">{r['stop']:.2f}</td>
        </tr>"""
    if not positions:
        pos_rows = '<tr><td colspan="8" class="muted">No open real positions tracked.</td></tr>'

    lane_rows = ""
    for s in lanes:
        paused = s["label"] in PAUSED_LANES
        badge = ' <span class="badge grey">PAUSED</span>' if paused else ""
        row_cls = ' class="paused"' if paused else ""
        since = (f' <span class="muted" style="font-weight:400">since {s["started"][5:]}</span>'
                 if s.get("started") else "")
        label_cell = f'<td class="sym">{escape(s["label"])}{badge}{since}</td>'
        if s["on_cap"] is not None:
            oc_cls = "muted" if paused else ("gain" if s["on_cap"] >= 0 else "loss")
            oncap_cell = (f'<td class="num {oc_cls}">{s["on_cap"]:+.1f}% '
                          f'<span class="muted">(₹{s["cum_inr"]:+,.0f})</span></td>')
        else:
            oncap_cell = '<td class="num muted">—</td>'
        if s["has_data"]:
            cls = "muted" if paused else ("gain" if s["avg"] >= 0 else "loss")
            lane_rows += f"""<tr{row_cls}>
                {label_cell}
                <td class="num">{s['closed']}</td>
                <td class="num">{s['active']}</td>
                <td class="num">{s['pending']}</td>
                <td class="num">{s['wr']:.0f}%</td>
                <td class="num {cls}">{s['avg']:+.2f}%</td>
                {oncap_cell}
            </tr>"""
        else:
            lane_rows += f"""<tr{row_cls}>
                {label_cell}
                <td class="num">0</td><td class="num">{s['active']}</td>
                <td class="num">{s['pending']}</td>
                <td class="muted" colspan="3">no closed trades yet</td>
            </tr>"""

    # Blind-buy Stage-1 gate progress
    g = blind_buy_gate()
    pct = min(100, g["fwd"] / g["gate"] * 100) if g["gate"] else 0
    if g["fwd"]:
        fwd_line = (f'<span class="big {"gain" if (g["fwd_avg"] or 0) >= 0 else "loss"}">'
                    f'{g["fwd_avg"]:+.2f}% avg</span> · {g["fwd_wr"]:.0f}% win so far')
    else:
        fwd_line = ('<span class="muted">no forward trades closed yet — first batch of '
                    f'{g["active"]} open positions closes ~mid-June</span>')
    seed_avg = f'{g["seed_avg"]:+.2f}%' if g["seed_avg"] is not None else "n/a"
    gate_html = f"""
  <h2>Blind-Buy &rarr; Stage-1 gate progress</h2>
  <div class="summary">
    Forward trades: <span class="big">{g['fwd']} / {g['gate']}</span>
    &nbsp;<span class="bar"><span class="bar-fill" style="width:{pct:.0f}%"></span></span><br>
    <div style="margin-top:8px">{fwd_line}</div>
  </div>
  <div class="updated" style="margin-top:6px">
    Gate = {g['gate']} forward closed trades (closed on/after {FORWARD_START}) sustained &ge;0.3%/day net &rarr; then consider real capital.
    {g['active']} positions in flight. Seed: {g['seed']} backfill trades (avg {seed_avg}) shown as prior context only &mdash; NOT counted toward the gate.
  </div>"""

    # Live-alert watch section (observational; the bot's chase-the-move feed)
    watch_html = ""
    w = get_watch()
    if w and w["agg"]["total_alerts"]:
        a = w["agg"]
        if a["closed"]:
            avg_cls = "gain" if (a["avg_net"] or 0) >= 0 else "loss"
            verdict = (f'<span class="big {avg_cls}">{a["avg_net"]:+.2f}% avg</span> · '
                       f'{a["win_rate"]:.0f}% win · {a["targets"]} hit target / {a["stops"]} stopped '
                       f'· {a["closed"]} scored')
        else:
            verdict = '<span class="muted">no alerts scored yet — all too recent</span>'
        wrows = ""
        for r in w["rows"][:10]:
            if r["status"] == "closed":
                rc = "gain" if r["net_pct"] >= 0 else "loss"
                out = f'<span class="{rc}">{r["net_pct"]:+.2f}%</span> <span class="muted">({escape(str(r["exit_reason"]))})</span>'
            else:
                out = f'<span class="muted">{escape(r["status"])}</span>'
            wrows += (f'<tr><td>{escape(r["date"])}</td>'
                      f'<td class="sym">{escape(r["symbol"])}</td>'
                      f'<td class="num">{out}</td></tr>')
        watch_html = f"""
  <h2>Live BUY alerts — watch only (NOT traded)</h2>
  <div class="summary">{verdict}</div>
  <div class="updated" style="margin-top:6px">
    &#9888; Best-case read: scored from the point-in-time actionable board, so alerts that fired and faded intraday (like ATGL today) are NOT captured &mdash; the real edge is likely worse. {a['pending']} too recent to score. Entry at alert price, {a['hold_days']}-day forward outcome on daily bars, net ~0.25% cost. Shown to judge whether acting on these would pay &mdash; they are not traded.
  </div>
  <table>
    <tr><th>Alert date</th><th>Symbol</th><th class="num">If you'd bought it</th></tr>
    {wrows}
  </table>"""

    ranked_rows = ""
    for row in ranked["trades"]:
        closed = truthy(row.get("closed"))
        pnl = float(row.get("net_pnl") or 0.0)
        pnl_class = "gain" if pnl > 0 else ("loss" if pnl < 0 else "muted")
        status = escape(str(row.get("exit_reason") or ("open" if not closed else "closed")))
        ranked_rows += f"""<tr>
            <td class="sym">{escape(str(row.get('symbol', '')))}</td>
            <td class="num">{float(row.get('entry_price') or 0.0):.2f}</td>
            <td class="num">{float(row.get('exit_price') or 0.0):.2f}</td>
            <td>{status}</td>
            <td class="num {pnl_class}">{money(pnl)}</td>
        </tr>"""
    if not ranked_rows:
        ranked_rows = '<tr><td colspan="5" class="muted">No ranked paper trades yet.</td></tr>'
    pause_badge = (
        '<span class="badge red">ENTRY PAUSED: 2 LOSSES</span>'
        if ranked["entry_paused"]
        else '<span class="badge grey">PAPER ONLY</span>'
    )
    ranked_pnl_class = "gain" if ranked["net_pnl"] >= 0 else "loss"
    ranked_html = f"""
  <h2>Ranked Grade-B confirmation (paper only)</h2>
  <div class="summary">
    <span>Date <span class="big">{escape(ranked['date'] or 'n/a')}</span></span>
    <span>Closed <span class="big">{ranked['closed']}</span></span>
    <span>Win rate <span class="big">{ranked['win_rate']:.0f}%</span></span>
    <span>Net <span class="big {ranked_pnl_class}">{money(ranked['net_pnl'])}</span></span>
    {pause_badge}
  </div>
  <div class="updated" style="margin-top:6px">
    {ranked['candidates']} candidates observed; {ranked['targets']} targets, {ranked['stops']} stops.
    Simulated fills include fees and slippage. No broker orders.
  </div>
  <table>
    <tr><th>Symbol</th><th class="num">Entry</th><th class="num">Exit</th><th>Reason</th><th class="num">Net P&amp;L</th></tr>
    {ranked_rows}
  </table>"""

    replay_class = "gain" if ranked_replay["stress_avg_daily_pnl"] > 0 else "loss"
    replay_verdict = "SHADOW CANDIDATE" if ranked_replay["shadow_candidates"] else "NOT READY"
    ranked_replay_html = f"""
  <h2>Ranked exact-tape readiness</h2>
  <div class="summary">
    <span>Complete days <span class="big">{ranked_replay['complete_days']}</span></span>
    <span>Fixed configs <span class="big">{ranked_replay['configs']}</span></span>
    <span>Shadow candidates <span class="big">{ranked_replay['shadow_candidates']}</span></span>
    <span>&#8377;10k passes <span class="big">{ranked_replay['goal_passes']}</span></span>
    <span class="badge {'grey' if ranked_replay['shadow_candidates'] else 'red'}">{replay_verdict}</span>
  </div>
  <div class="updated" style="margin-top:6px">
    Best fixed configuration: <b>{escape(ranked_replay['best_config'] or 'none')}</b>;
    {ranked_replay['closed_trades']} closed trades, {ranked_replay['wins']} winners,
    stressed average <span class="{replay_class}">{money(ranked_replay['stress_avg_daily_pnl'])}/day</span>,
    lower confidence bound {money(ranked_replay['stress_lower95_daily_pnl'])}/day,
    profit factor {ranked_replay['profit_factor']:.2f}.
    Paper promotion: <b>{'YES' if ranked_replay['paper_promotion_allowed'] else 'NO'}</b>;
    real orders: <b>NO</b>.
  </div>"""

    evidence_class = "gain" if evidence["best_stress_return"] > 0 else "loss"
    coverage_class = "gain" if evidence["coverage_passed"] else "loss"
    evidence_html = f"""
  <h2>Live evidence readiness</h2>
  <div class="summary">
    <span>Sessions <span class="big">{evidence['sessions']}</span></span>
    <span>Coverage <span class="big {coverage_class}">{evidence['coverage']:.1f}%</span></span>
    <span>Shadow candidates <span class="big">{evidence['shadow_candidates']}</span></span>
    <span>&#8377;10k passes <span class="big">{evidence['goal_passes']}</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Latest evidence through {escape(evidence['status_date'] or 'n/a')}.
    Best eligible research row: <b>{escape(evidence['best_bucket'] or 'none')}</b> /
    {escape(evidence['best_contract'] or 'none')} at
    <span class="{evidence_class}">{evidence['best_stress_return']:+.3f}% stressed daily
    ({money(evidence['best_stress_pnl'])})</span>, coverage {evidence['best_coverage']:.1f}%,
    lower bound {evidence['best_lower_bound']:+.3f}%.
    Evidence accepted: <b>NO</b>; paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    tot_cls = "gain" if tot_pnl >= 0 else "loss"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>tr-assistant status</title>
<style>
  body {{ background:#0f1115; color:#e6e6e6; font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:28px 0 8px; color:#9aa4b2; text-transform:uppercase; letter-spacing:.5px; }}
  .updated {{ color:#6b7280; font-size:13px; margin-bottom:8px; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th,td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #20242c; }}
  th {{ color:#9aa4b2; font-weight:600; font-size:12px; text-transform:uppercase; }}
  td.num,th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .sym {{ font-weight:600; }}
  .gain {{ color:#4ade80; }}
  .loss {{ color:#f87171; }}
  .muted {{ color:#6b7280; font-style:italic; }}
  .badge {{ font-size:10px; padding:2px 6px; border-radius:4px; margin-left:6px; font-weight:700; vertical-align:middle; }}
  .badge.red {{ background:#7f1d1d; color:#fecaca; }}
  .badge.orange {{ background:#7c2d12; color:#fed7aa; }}
  .badge.grey {{ background:#374151; color:#cbd5e1; }}
  tr.paused td {{ color:#6b7280; }}
  .bar {{ background:#20242c; border-radius:6px; height:18px; width:320px; display:inline-block; vertical-align:middle; overflow:hidden; }}
  .bar-fill {{ background:#4ade80; height:100%; }}
  .summary {{ background:#171a21; border-radius:8px; padding:14px 18px; display:inline-block; margin-top:6px; }}
  .summary span {{ margin-right:24px; }}
  .big {{ font-size:18px; font-weight:700; }}
</style></head>
<body>
  <h1>tr-assistant — status</h1>
  <div class="updated">updated {now} · auto-refreshes every 60s</div>

  <h2>Real positions (your money)</h2>
  <div class="summary">
    <span>Invested <span class="big">{money(tot_inv)}</span></span>
    <span>Now <span class="big">{money(tot_val)}</span></span>
    <span>P&amp;L <span class="big {tot_cls}">{money(tot_pnl)} ({tot_pct:+.1f}%)</span></span>
    <span>Open <span class="big">{len(positions)}</span></span>
  </div>
  <table>
    <tr><th>Symbol</th><th class="num">Qty</th><th class="num">Buy</th><th class="num">Now</th>
        <th class="num">Invested</th><th class="num">P&amp;L %</th><th class="num">P&amp;L &#8377;</th><th class="num">Stop</th></tr>
    {pos_rows}
  </table>

  <h2>Paper lanes (measurement only — no real money)</h2>
  <table>
    <tr><th>Lane</th><th class="num">Closed</th><th class="num">Active</th><th class="num">Pending</th>
        <th class="num">Win rate</th><th class="num">Avg/trade</th><th class="num">On &#8377;capital</th></tr>
    {lane_rows}
  </table>
{ranked_html}
{ranked_replay_html}
{evidence_html}
{gate_html}
{watch_html}
  <div class="updated" style="margin-top:18px">
    Numbers read live from results/Data journals + kite_tracked_positions.json. "On &#8377;capital" = booked P&amp;L &#247; deployable capital (the real account number, not a sum of per-trade %s). "since" = journal start date — if it jumps, the lane was reset/re-backfilled. Prices from last Kite tick (may be stale outside market hours).
  </div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = render().encode("utf-8")
        except Exception as e:
            body = f"<pre>dashboard error: {escape(str(e))}</pre>".encode("utf-8")
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet


def main():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"tr-assistant dashboard at http://{HOST}:{PORT}  (Ctrl-C to stop)")
    if HOST == "127.0.0.1":
        print("Local only. From your laptop:  ssh -L "
              f"{PORT}:localhost:{PORT} <user>@<vm-ip>  then open http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
