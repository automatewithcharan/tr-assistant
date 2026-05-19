"""High-Value Alert Notifier — guaranteed delivery for the alerts that matter.

Reads anomaly_history.csv every cycle (cron every 5 min during market hours)
and pushes any NEW Tier C / Strong Buy Now / OBS alert to Discord. Maintains
a "seen" file so each alert is sent exactly once.

Why this exists:
  SignalMonitor's Discord message is capped at 6 alerts and ranks Strong Buy >
  Buy > Momentum > Anomaly. When Buy Now floods (6+ per cycle), anomaly tier C
  alerts — the ONLY positive-expectancy tier — get silently dropped.

What it does NOT do:
  This is a notifier only. It does not act on alerts and does not paper-trade
  them — that's handled by scripts/intraday_alert_paper_trader.py.

Run every 5 minutes during market hours via cron:
  */5 9-15 * * 1-5 .../scripts/high_value_alert_notifier.py
"""

from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, time as dtime, date as ddate
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
ANOMALY_CSV = RESULTS / "anomaly_history.csv"
SEEN_FILE = RESULTS / "high_value_alerts_seen.json"
LOG_FILE = RESULTS / "high_value_alerts_log.csv"

import time
import urllib.request
import urllib.error

DISCORD_RATE_DELAY_SEC = 1.5  # Discord allows 5 webhook posts / 2 sec — stay safe


def _load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()).get("seen", []))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps({"seen": sorted(seen)}, indent=2))


def _post_discord(message: str) -> bool:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    data = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"[!] Discord HTTP {e.code}: {e.reason}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[!] Discord error: {e}", file=sys.stderr)
        return False


def _format_anomaly_alert(row: dict) -> str:
    """Build a Discord message for a Tier C / Strong Buy alert."""
    tier = row.get("Tier", "?")
    action = row.get("Action", "")
    sym = row.get("Symbol", "?")
    when = str(row.get("When", ""))[:19]
    price = row.get("PriceNow", "?")
    t1 = row.get("TakeProfit1", "?")
    t2 = row.get("TakeProfit2", "?")
    stop = row.get("SellIfBelow", "?")
    why = str(row.get("Why", ""))[:200]
    checks = str(row.get("Checks", ""))[:120]
    qual = row.get("QualityScore", "?")
    change_pct = row.get("ChangePct", "?")
    emoji = "🎯" if tier == "C" else "⚡"
    return (
        f"{emoji} **HIGH-VALUE ALERT — `{sym}` [Tier {tier}]**\n"
        f"Action: {action}\n"
        f"At alert: ₹{price}  (Day {change_pct})\n"
        f"Target 1: ₹{t1}  |  Target 2: ₹{t2}  |  Stop: ₹{stop}\n"
        f"Quality: {qual}/100\n"
        f"Checks: `{checks}`\n"
        f"Why: {why}\n"
        f"Time: {when} IST"
    )


def _log_alert(row: dict):
    """Append the alert to a persistent CSV log."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "logged_at": datetime.utcnow().isoformat(),
        "alert_when": row.get("When"),
        "date": row.get("Date"),
        "symbol": row.get("Symbol"),
        "tier": row.get("Tier"),
        "action": row.get("Action"),
        "price_now": row.get("PriceNow"),
        "change_pct": row.get("ChangePct"),
        "target1": row.get("TakeProfit1"),
        "target2": row.get("TakeProfit2"),
        "stop": row.get("SellIfBelow"),
        "quality_score": row.get("QualityScore"),
        "checks": row.get("Checks"),
    }
    df = pd.DataFrame([line])
    if LOG_FILE.exists():
        df.to_csv(LOG_FILE, mode="a", header=False, index=False)
    else:
        df.to_csv(LOG_FILE, index=False)


def _alert_key(row: dict) -> str:
    """Stable identifier so we don't double-send."""
    return f"{row.get('Date')}::{row.get('Symbol')}::{row.get('When')}"


def process(dry_run: bool = False, send_all_today: bool = False) -> int:
    if not ANOMALY_CSV.exists():
        print(f"[!] {ANOMALY_CSV} missing")
        return 1

    seen = _load_seen()
    df = pd.read_csv(ANOMALY_CSV)

    # Filter to high-value:
    #   Tier C
    #   OR Action contains "Strong Buy Now"
    #   OR Action contains specific OBS / fade markers (kept for forward compat)
    df = df[
        (df["Tier"].astype(str) == "C")
        | (df["Action"].astype(str).str.contains("Strong Buy Now", case=False, na=False))
        | (df["Action"].astype(str).str.contains("Pre-Move", case=False, na=False))
    ].copy()

    if df.empty:
        print("[i] No Tier C / Strong Buy / Pre-Move alerts in anomaly_history")
        return 0

    # Only today's alerts by default (or all if --send-all-today is set with empty seen)
    today = ddate.today().isoformat()
    if not send_all_today:
        df_today = df[df["Date"].astype(str) == today].copy()
    else:
        df_today = df.copy()

    sent = 0
    new_seen = set(seen)
    for _, row in df_today.iterrows():
        rd = row.to_dict()
        key = _alert_key(rd)
        if key in seen:
            continue
        msg = _format_anomaly_alert(rd)
        if dry_run:
            print(f"[DRY RUN] Would send:\n{msg}\n")
            sent += 1
        else:
            ok = _post_discord(msg)
            if ok:
                _log_alert(rd)
                new_seen.add(key)
                sent += 1
                print(f"[+] Sent: {rd['Symbol']} Tier {rd['Tier']} @ {rd['When']}")
                time.sleep(DISCORD_RATE_DELAY_SEC)
            else:
                print(f"[-] Failed to send: {rd['Symbol']} @ {rd['When']}")
                time.sleep(DISCORD_RATE_DELAY_SEC * 2)  # back off on errors

    if not dry_run:
        _save_seen(new_seen)

    print(f"[i] Total sent this run: {sent}  |  total seen: {len(new_seen)}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print messages, don't send")
    ap.add_argument("--send-all-today", action="store_true", help="Send all matching from history (use once for seeding)")
    ap.add_argument("--reset-seen", action="store_true", help="Clear the 'seen' set")
    ap.add_argument("--seed-historical-as-seen", action="store_true", help="Mark all historical Tier C alerts as already-sent (do this once before going live)")
    args = ap.parse_args()

    # Load .env.dev for DISCORD_WEBHOOK_URL if not in environment
    env_path = REPO / ".env.dev"
    if env_path.exists() and not os.environ.get("DISCORD_WEBHOOK_URL"):
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    if args.reset_seen:
        if SEEN_FILE.exists():
            SEEN_FILE.unlink()
        print("[+] Reset seen set")
        return 0

    if args.seed_historical_as_seen:
        if not ANOMALY_CSV.exists():
            print(f"[!] {ANOMALY_CSV} missing")
            return 1
        df = pd.read_csv(ANOMALY_CSV)
        df = df[
            (df["Tier"].astype(str) == "C")
            | (df["Action"].astype(str).str.contains("Strong Buy Now", case=False, na=False))
            | (df["Action"].astype(str).str.contains("Pre-Move", case=False, na=False))
        ]
        keys = {f"{r['Date']}::{r['Symbol']}::{r['When']}" for _, r in df.iterrows()}
        _save_seen(keys)
        print(f"[+] Marked {len(keys)} historical high-value alerts as seen. Going forward, only NEW alerts will be sent.")
        return 0

    return process(dry_run=args.dry_run, send_all_today=args.send_all_today)


if __name__ == "__main__":
    raise SystemExit(main())
