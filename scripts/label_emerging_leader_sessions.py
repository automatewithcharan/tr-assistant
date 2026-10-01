#!/usr/bin/env python3
"""Create offline future-rank labels from a completed recorder session.

Raw snapshots are read only.  Labels are written to a separate SQLite file.
Price outcomes remain NULL because V1 does not record executable minute OHLCV.
"""
from __future__ import annotations
import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path

from pkscreener.classes.NGP5EarlyWatchPublisher import IST

REQUIRED_COVERAGE = 80.0


def label_session(root: Path, session_date: str, now: datetime | None = None) -> int:
    now = now or datetime.now(IST)
    if date.fromisoformat(session_date) >= now.date():
        raise ValueError("labels may only be generated after the completed session date")
    source = root / session_date / "emerging_leader.sqlite"
    if not source.exists():
        raise FileNotFoundError(source)
    destination = root / session_date / "emerging_leader_labels.sqlite"
    read = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    write = sqlite3.connect(destination)
    write.executescript("""
      CREATE TABLE IF NOT EXISTS future_labels (
        session_date TEXT, timestamp TEXT, symbol TEXT,
        entered_top100_later INTEGER, entered_top50_later INTEGER, entered_top20_later INTEGER, entered_top10_later INTEGER,
        minutes_to_top100 REAL, minutes_to_top50 REAL, minutes_to_top20 REAL, minutes_to_top10 REAL,
        best_future_rank INTEGER, eod_rank INTEGER, eod_top100 INTEGER, eod_top50 INTEGER, eod_top20 INTEGER,
        top100_persistence_minutes REAL, eod_coverage_pct REAL, eod_expected_count INTEGER, eod_fresh_count INTEGER,
        eod_label_timestamp TEXT, eod_label_valid INTEGER,
        forward_return_5m REAL, forward_return_10m REAL, forward_return_20m REAL, forward_return_30m REAL, forward_return_60m REAL,
        return_to_eod REAL, mfe_5m REAL, mfe_10m REAL, mfe_20m REAL, mfe_30m REAL, mfe_60m REAL,
        mae_5m REAL, mae_10m REAL, mae_20m REAL, mae_30m REAL, mae_60m REAL,
        PRIMARY KEY(session_date,timestamp,symbol)
      );
    """)
    contexts = read.execute("SELECT timestamp,fresh_nse_count,expected_nse_count,broad_coverage_pct FROM market_context WHERE broad_coverage_pct>=? ORDER BY timestamp", (REQUIRED_COVERAGE,)).fetchall()
    eod = contexts[-1] if contexts else None
    observations = read.execute("SELECT session_date,timestamp,symbol,broad_rank FROM snapshots ORDER BY symbol,timestamp").fetchall()
    by_symbol: dict[str, list[tuple]] = {}
    for row in observations: by_symbol.setdefault(row[2], []).append(row)
    rows = []
    for symbol, series in by_symbol.items():
        for index, observation in enumerate(series):
            session, timestamp, _, rank = observation
            later = series[index + 1:]
            def hit(level: int):
                # Already inside is context/control, never an "early entry" success.
                if rank is not None and rank <= level:
                    return None, None
                candidate = next((entry for entry in later if entry[3] is not None and entry[3] <= level), None)
                if not candidate:
                    return None, None
                minutes = (datetime.fromisoformat(candidate[1]) - datetime.fromisoformat(timestamp)).total_seconds() / 60
                return 1, minutes
            h100,m100=hit(100); h50,m50=hit(50); h20,m20=hit(20); h10,m10=hit(10)
            future_ranks=[entry[3] for entry in later if entry[3] is not None]
            eod_rank = None
            valid = int(eod is not None)
            if eod:
                eod_rank = next((entry[3] for entry in reversed(series) if entry[1] <= eod[0]), None)
            persistence = sum(1 for entry in later if entry[3] is not None and entry[3] <= 100)  # one-minute snapshots
            rows.append((session,timestamp,symbol,h100,h50,h20,h10,m100,m50,m20,m10,min(future_ranks) if future_ranks else None,
                         eod_rank, int(eod_rank<=100) if eod_rank is not None else None, int(eod_rank<=50) if eod_rank is not None else None,
                         int(eod_rank<=20) if eod_rank is not None else None, persistence if later else None,
                         eod[3] if eod else None,eod[2] if eod else None,eod[1] if eod else None,eod[0] if eod else None,valid,
                         *([None]*16)))
    with write:
        write.executemany("INSERT OR REPLACE INTO future_labels VALUES (" + ",".join("?" for _ in range(38)) + ")", rows)
    read.close(); write.close()
    return len(rows)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True); parser.add_argument("--session-date",required=True)
    args=parser.parse_args(); print({"labels":label_session(args.root,args.session_date)}); return 0

if __name__ == "__main__": raise SystemExit(main())
