"""NSE event-feed layer.

Pulls two NSE endpoints and joins them against today's alert universe so
the paper trader can distinguish event-driven moves from clean technical
breakouts:

  1. /api/event-calendar             — forward-looking board meetings,
                                       results, dividends, splits. The
                                       *predictive* layer.
  2. /api/corporate-announcements    — material disclosures filed today.
                                       The *enrichment* layer (most are
                                       follow-ups; useful as a noise tag).

Outputs (under results/Data/nse_event_feed/):
  event_calendar_<YYYY-MM-DD>.json   raw snapshot
  announcements_<YYYY-MM-DD>.json    raw snapshot
  alert_tags_<YYYY-MM-DD>.csv        joined view per alerted symbol
  report_<YYYY-MM-DD>.txt            human summary
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, date as ddate, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
OUT_DIR = RESULTS / "nse_event_feed"

NSE_BASE = "https://www.nseindia.com"
EVENT_CAL_URL = f"{NSE_BASE}/api/event-calendar"
ANNOUNCE_URL = f"{NSE_BASE}/api/corporate-announcements"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


def _nse_session() -> requests.Session:
    s = requests.Session()
    # Prime cookies — NSE blocks unprimed sessions.
    s.get(NSE_BASE, headers=_HEADERS, timeout=10)
    return s


def _retry_get(session: requests.Session, url: str, attempts: int = 3) -> Optional[Any]:
    for i in range(attempts):
        try:
            r = session.get(url, headers=_HEADERS, timeout=20)
            if r.status_code == 200 and r.text:
                return r.json()
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def fetch_event_calendar(session: requests.Session) -> List[Dict]:
    data = _retry_get(session, EVENT_CAL_URL)
    return data if isinstance(data, list) else []


def fetch_announcements(
    session: requests.Session,
    from_date: ddate,
    to_date: ddate,
) -> List[Dict]:
    fmt = "%d-%m-%Y"
    url = (
        f"{ANNOUNCE_URL}?index=equities"
        f"&from_date={from_date.strftime(fmt)}"
        f"&to_date={to_date.strftime(fmt)}"
    )
    data = _retry_get(session, url)
    return data if isinstance(data, list) else []


# --- Storage ----------------------------------------------------------------

def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def update(target_date: ddate) -> Tuple[int, int]:
    """Pull both feeds and persist today's snapshot. Returns (events, filings)."""
    session = _nse_session()

    events = fetch_event_calendar(session)
    _write_json(OUT_DIR / f"event_calendar_{target_date.isoformat()}.json", events)

    filings = fetch_announcements(session, target_date, target_date)
    _write_json(OUT_DIR / f"announcements_{target_date.isoformat()}.json", filings)

    return len(events), len(filings)


# --- Tag layer --------------------------------------------------------------

_NOISE_DESCS = {
    # Filings that almost never carry market-moving information.
    "Copy of Newspaper Publication",
    "ESOP/ESOS/ESPS",
    "Change in Company Secretary/Compliance Officer",
    "Change in Auditors",
    "Disclosure under SEBI Takeover Regulations",
    "Updates",
    "General Updates",
}

_MATERIAL_DESCS = {
    # Filings that historically precede or accompany meaningful price moves.
    "Outcome of Board Meeting",
    "Press Release",
    "Press Release (Revised)",
    "Investor Presentation",
    "Dividend",
    "Record Date",
    "Allotment of Securities",
    "Acquisition",
    "Agreements",
    "Action(s) initiated or orders passed",
    "Clarification - Financial Results",
    "Financial Results",
    "Order",
    "Resignation of Director/KMP/SMP",
    "Change in Management",
    "Shareholders meeting",
}


def _classify_desc(desc: str) -> str:
    """Coarse triage. Conservative: anything not in noise/material falls 'other'."""
    if not desc:
        return "other"
    if desc in _NOISE_DESCS:
        return "noise"
    if desc in _MATERIAL_DESCS:
        return "material"
    for k in _MATERIAL_DESCS:
        if desc.startswith(k):
            return "material"
    return "other"


def _parse_event_date(s: str) -> Optional[ddate]:
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _load_today_alerts(target_date: ddate) -> List[str]:
    """Today's blind-buy alert symbols (one-per-line text file)."""
    p = RESULTS / f"alerted_buys_{target_date.isoformat()}.txt"
    if not p.exists():
        return []
    return [s.strip() for s in p.read_text().split() if s.strip()]


def tag_alerts(target_date: ddate, window_days: int = 7) -> Path:
    """Join today's alert symbols against event-calendar (forward) and
    announcements (today + trailing window). Writes alert_tags_<date>.csv."""
    import csv

    ev_path = OUT_DIR / f"event_calendar_{target_date.isoformat()}.json"
    fl_path = OUT_DIR / f"announcements_{target_date.isoformat()}.json"
    if not ev_path.exists() or not fl_path.exists():
        raise FileNotFoundError(
            f"Run --update first; missing {ev_path.name} or {fl_path.name}"
        )

    events = json.loads(ev_path.read_text())
    filings_today = json.loads(fl_path.read_text())

    # Pull a trailing window of filings so we can flag "had material filing
    # in last N days" — captures pre-event positioning.
    session = _nse_session()
    win_start = target_date - timedelta(days=window_days)
    filings_window = fetch_announcements(session, win_start, target_date)

    # Index events by symbol → list of (date, purpose)
    by_sym_events: Dict[str, List[Tuple[ddate, str]]] = defaultdict(list)
    for e in events:
        sym = (e.get("symbol") or "").strip().upper()
        if not sym:
            continue
        ed = _parse_event_date(e.get("date", ""))
        if ed:
            by_sym_events[sym].append((ed, e.get("purpose", "")))

    # Index filings (windowed) by symbol → list of (datetime, desc, kind)
    by_sym_filings: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for f in filings_window:
        sym = (f.get("symbol") or "").strip().upper()
        if not sym:
            continue
        desc = (f.get("desc") or "").strip()
        kind = _classify_desc(desc)
        by_sym_filings[sym].append((f.get("sort_date", ""), desc, kind))

    alerts = _load_today_alerts(target_date)
    out_path = OUT_DIR / f"alert_tags_{target_date.isoformat()}.csv"

    with out_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "symbol",
            "next_event_date",
            "next_event_days_out",
            "next_event_purpose",
            "filings_today",
            "filings_window",
            "material_count_window",
            "noise_count_window",
            "first_material_desc",
            "verdict",
        ])
        for sym in alerts:
            su = sym.upper()
            evs = sorted(by_sym_events.get(su, []), key=lambda x: x[0])
            future_ev = next((e for e in evs if e[0] >= target_date), None)
            fls_window = by_sym_filings.get(su, [])
            fls_today = [f for f in fls_window if f[0].startswith(target_date.isoformat())]

            material = [f for f in fls_window if f[2] == "material"]
            noise = [f for f in fls_window if f[2] == "noise"]

            verdict = _verdict(future_ev, target_date, material, fls_today)

            w.writerow([
                sym,
                future_ev[0].isoformat() if future_ev else "",
                (future_ev[0] - target_date).days if future_ev else "",
                future_ev[1] if future_ev else "",
                len(fls_today),
                len(fls_window),
                len(material),
                len(noise),
                material[0][1] if material else "",
                verdict,
            ])

    return out_path


def _verdict(
    future_ev: Optional[Tuple[ddate, str]],
    today: ddate,
    material: List,
    fls_today: List,
) -> str:
    """One-word triage to feed downstream filters."""
    if future_ev and (future_ev[0] - today).days <= 1:
        return "event_imminent"
    if any(f[2] == "material" and f[0].startswith(today.isoformat()) for f in fls_today):
        return "material_today"
    if material:
        return "material_recent"
    if future_ev and (future_ev[0] - today).days <= 5:
        return "event_this_week"
    return "clean"


# --- Reporting --------------------------------------------------------------

def write_report(target_date: ddate) -> Path:
    import csv as _csv

    tags_path = OUT_DIR / f"alert_tags_{target_date.isoformat()}.csv"
    if not tags_path.exists():
        raise FileNotFoundError(tags_path)

    rows = list(_csv.DictReader(tags_path.open()))
    n = len(rows)
    by_verdict: Dict[str, int] = defaultdict(int)
    for r in rows:
        by_verdict[r["verdict"]] += 1

    lines = [
        f"NSE event-feed tagging — {target_date.isoformat()}",
        "=" * 60,
        f"Alerts tagged: {n}",
        "",
        "Verdict breakdown:",
    ]
    for v, c in sorted(by_verdict.items(), key=lambda x: -x[1]):
        lines.append(f"  {v:<20} {c:>4}  ({c/n*100:.1f}%)" if n else f"  {v}  {c}")

    flagged = [r for r in rows if r["verdict"] != "clean"]
    if flagged:
        lines += ["", f"Flagged alerts ({len(flagged)}):"]
        for r in flagged[:50]:
            tag = r["verdict"]
            detail = r.get("first_material_desc") or r.get("next_event_purpose") or ""
            lines.append(f"  {r['symbol']:<14} [{tag}]  {detail[:60]}")

    out = OUT_DIR / f"report_{target_date.isoformat()}.txt"
    out.write_text("\n".join(lines) + "\n")
    return out


# --- CLI --------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="NSE event-feed layer")
    p.add_argument("--date", default=ddate.today().isoformat(),
                   help="Target date YYYY-MM-DD (default: today)")
    p.add_argument("--update", action="store_true",
                   help="Fetch + persist event calendar and announcements")
    p.add_argument("--tag", action="store_true",
                   help="Join feeds against today's alert list -> alert_tags CSV")
    p.add_argument("--report", action="store_true",
                   help="Print human-readable report to stdout + file")
    p.add_argument("--window-days", type=int, default=7,
                   help="Trailing window for filings lookback (default 7)")
    args = p.parse_args()

    target = ddate.fromisoformat(args.date)

    if not any([args.update, args.tag, args.report]):
        # Default: do all three.
        args.update = args.tag = args.report = True

    if args.update:
        ev_n, fl_n = update(target)
        print(f"[update] {target}: events={ev_n}  filings={fl_n}")

    if args.tag:
        path = tag_alerts(target, window_days=args.window_days)
        print(f"[tag] wrote {path.relative_to(REPO)}")

    if args.report:
        path = write_report(target)
        print(f"[report] wrote {path.relative_to(REPO)}")
        print()
        print(path.read_text())

    return 0


if __name__ == "__main__":
    sys.exit(main())
