"""Shadow-only, causal recorder for the emerging-leader research question.

This module deliberately has no dependency on SignalMonitor, strategy filters,
publishers, or order-management code.  It reads the same atomic Kite snapshot
as the observation ledger and fails closed for a bad snapshot, while callers
are expected to fail open for production observation.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from pkscreener.classes.NGP5EarlyWatchPublisher import IST

SCHEMA_VERSION = "emerging_leader_radar_v1"
SNAPSHOT_SECONDS = 60
FRESH_SECONDS = 120
CLOCK_SKEW_SECONDS = 10
RANK_MILESTONES = (1500, 1000, 750, 500, 400, 300, 250, 200, 150, 125, 100, 75, 50, 20, 10, 5)


def _symbols(path: Path, column: str, eq: bool = False) -> set[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {
            str(row.get(column) or "").strip().upper()
            for row in csv.DictReader(handle)
            if row.get(column) and (not eq or str(row.get("SERIES") or "").upper() == "EQ")
        }


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _utcish(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(IST)
    except (TypeError, ValueError):
        return None


def _percentile(values: dict[str, float]) -> dict[str, float]:
    """Ascending cross-sectional percentile; ties receive their mean rank."""
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if not ordered:
        return {}
    result: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        pct = 100.0 if len(ordered) == 1 else ((index + end) / 2) / (len(ordered) - 1) * 100
        for symbol, _ in ordered[index:end + 1]:
            result[symbol] = pct
        index = end + 1
    return result


class EmergingLeaderRecorder:
    """Persist causal full-universe features and descriptive rank crossings."""

    def __init__(self, root: Path | str, eq_path: Path | str, tokens_path: Path | str,
                 recorder_git_sha: str, clock=datetime.now):
        self.root = Path(root)
        self.eq_path, self.tokens_path = Path(eq_path), Path(tokens_path)
        self.recorder_git_sha, self.clock = recorder_git_sha, clock
        self.history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=80))
        self.previous_rank: dict[str, int | None] = {}
        self.last_snapshot_at: datetime | None = None
        self.cycles = 0
        self.source_failures: list[str] = []

    def _ticks(self, path: Path | str) -> list[dict[str, Any]]:
        path = Path(path)
        atomic = path.with_suffix(".json")
        if not atomic.exists():
            raise FileNotFoundError(f"atomic Kite JSON snapshot unavailable: {atomic}")
        payload = json.loads(atomic.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return [value for value in payload.values() if isinstance(value, dict)]
        if isinstance(payload, list):
            return [value for value in payload if isinstance(value, dict)]
        raise ValueError("atomic Kite JSON snapshot has unsupported shape")

    @staticmethod
    def _past(history: deque[dict[str, Any]], now: datetime, seconds: int) -> dict[str, Any] | None:
        """Return a sufficiently close observation at/before T-horizon, never after it."""
        target = now.timestamp() - seconds
        candidates = [item for item in history if item["at"].timestamp() <= target]
        if not candidates:
            return None
        candidate = candidates[-1]
        # A gap is missing data, not an invitation to interpolate or use stale state.
        return candidate if target - candidate["at"].timestamp() <= 30 else None

    @staticmethod
    def _change(current: float | None, old: float | None) -> float | None:
        return None if current is None or old is None else current - old

    @staticmethod
    def _return(current: float | None, old: float | None) -> float | None:
        return None if current is None or old in (None, 0) else (current / old - 1.0) * 100.0

    def _db(self, date: str) -> sqlite3.Connection:
        day = self.root / date
        day.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(day / "emerging_leader.sqlite")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
          session_date TEXT NOT NULL, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
          fresh INTEGER NOT NULL, token_mapped INTEGER NOT NULL, data_age_seconds REAL,
          schema_version TEXT NOT NULL, recorder_git_sha TEXT NOT NULL,
          last_price REAL, previous_close REAL, current_pct_change REAL, broad_rank INTEGER,
          broad_percentile REAL, rank_1m_ago INTEGER, rank_3m_ago INTEGER, rank_5m_ago INTEGER, rank_10m_ago INTEGER,
          rank_improvement_1m REAL, rank_improvement_3m REAL, rank_improvement_5m REAL, rank_improvement_10m REAL,
          rank_velocity_1m REAL, rank_velocity_5m REAL, rank_acceleration REAL,
          return_1m REAL, return_3m REAL, return_5m REAL, return_10m REAL,
          cumulative_volume REAL, volume_change_1m REAL, volume_change_3m REAL, volume_change_5m REAL,
          volume_rate_1m REAL, volume_rate_5m REAL, volume_rate_acceleration REAL,
          approx_turnover REAL, turnover_change_1m REAL, turnover_change_5m REAL, turnover_acceleration REAL,
          distance_from_day_high_pct REAL, distance_from_day_low_pct REAL, distance_from_open_pct REAL,
          best_bid REAL, best_ask REAL, spread_bps REAL, causal_vwap REAL, price_vs_vwap_pct REAL,
          nifty_pct_change REAL, rs_vs_nifty REAL, rs_change_1m REAL, rs_change_5m REAL,
          rvol_at_time REAL, price_momentum_percentile REAL, rank_velocity_percentile REAL,
          volume_rate_percentile REAL, volume_acceleration_percentile REAL, turnover_percentile REAL,
          turnover_acceleration_percentile REAL, rs_percentile REAL,
          pct_change_gap_to_top100 REAL, pct_change_gap_to_top50 REAL, pct_change_gap_to_top20 REAL, pct_change_gap_to_top10 REAL,
          PRIMARY KEY(session_date, timestamp, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time ON snapshots(session_date, symbol, timestamp);
        CREATE TABLE IF NOT EXISTS rank_events (
          session_date TEXT NOT NULL, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
          milestone INTEGER NOT NULL, broad_rank INTEGER NOT NULL, pct_change REAL,
          rank_universe_size INTEGER NOT NULL, expected_universe_size INTEGER NOT NULL,
          coverage_pct REAL NOT NULL, context_json TEXT NOT NULL,
          PRIMARY KEY(session_date, symbol, milestone)
        );
        CREATE TABLE IF NOT EXISTS market_context (
          session_date TEXT NOT NULL, timestamp TEXT NOT NULL, fresh_nse_count INTEGER NOT NULL,
          expected_nse_count INTEGER NOT NULL, broad_coverage_pct REAL NOT NULL, nifty_fresh INTEGER NOT NULL,
          nifty_pct_change REAL, positive_count INTEGER NOT NULL, negative_count INTEGER NOT NULL,
          median_pct_change REAL, top100_cutoff_pct_change REAL, top50_cutoff_pct_change REAL,
          top20_cutoff_pct_change REAL, top10_cutoff_pct_change REAL, return_dispersion REAL,
          PRIMARY KEY(session_date, timestamp)
        );
        """)
        return connection

    def _manifest(self, date: str, expected: int, fresh_count: int, nifty_fresh: bool, now: datetime) -> None:
        path = self.root / date / "manifest.json"
        old = json.loads(path.read_text()) if path.exists() else {}
        coverages = old.get("fresh_coverage_samples", [])[-1500:] + [fresh_count / max(1, expected) * 100]
        db_path = self.root / date / "emerging_leader.sqlite"
        with sqlite3.connect(db_path) as connection:
            database_row_counts = {
                "snapshots": connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                "rank_events": connection.execute("SELECT COUNT(*) FROM rank_events").fetchone()[0],
                "market_context": connection.execute("SELECT COUNT(*) FROM market_context").fetchone()[0],
            }
        payload = {
            "date": date, "schema_version": SCHEMA_VERSION, "recorder_git_sha": self.recorder_git_sha,
            "recorder_start_time": old.get("recorder_start_time", now.isoformat()), "recorder_end_time": now.isoformat(),
            "expected_cycles": 1500, "completed_cycles": self.cycles, "missed_cycles": max(0, 1500 - self.cycles),
            "symbols_expected": expected, "fresh_coverage_min": min(coverages),
            "fresh_coverage_median": statistics.median(coverages), "fresh_coverage_max": max(coverages),
            "nifty_fresh_last": nifty_fresh, "fresh_coverage_samples": coverages,
            "database_row_counts": database_row_counts, "event_counts": {"rank_crossings": database_row_counts["rank_events"]},
            "source_failures": self.source_failures, "valid_for_research": bool(coverages and max(coverages) >= 80),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def process(self, ticks_path: Path | str) -> dict[str, Any]:
        started = time.perf_counter()
        now = self.clock().astimezone(IST)
        date = now.date().isoformat()
        self.cycles += 1
        try:
            ticks = self._ticks(ticks_path)
        except Exception as exc:
            self.source_failures.append(str(exc))
            return {"status": "source_unavailable", "error": str(exc)}
        expected = _symbols(self.eq_path, "SYMBOL", eq=True)
        mapped = _symbols(self.tokens_path, "Symbol") if self.tokens_path.exists() else set()
        raw: dict[str, dict[str, Any]] = {}
        nifty_tick: dict[str, Any] | None = None
        for tick in ticks:
            symbol = str(tick.get("symbol") or "").strip().upper()
            received = _utcish(tick.get("received_at"))
            if symbol == "NIFTY 50":
                nifty_tick = tick
            if symbol not in expected or str(tick.get("exchange") or "").upper() != "NSE":
                continue
            trade = _utcish(tick.get("last_trade_time"))
            price, close = _number(tick.get("last_price")), _number(tick.get("close"))
            age = (now - received).total_seconds() if received else None
            if received and trade and trade.date() == now.date() and age is not None and -CLOCK_SKEW_SECONDS <= age <= FRESH_SECONDS and price is not None and close not in (None, 0):
                raw[symbol] = {"tick": tick, "received": received, "age": age, "price": price,
                               "close": close, "pct": (price / close - 1) * 100}
        ranked = sorted(raw.items(), key=lambda item: (-item[1]["pct"], item[0]))
        ranks = {symbol: index + 1 for index, (symbol, _) in enumerate(ranked)}
        cutoff = {n: ranked[n - 1][1]["pct"] if len(ranked) >= n else None for n in (100, 50, 20, 10)}
        nifty_pct = None
        nifty_fresh = False
        if nifty_tick:
            received, trade = _utcish(nifty_tick.get("received_at")), _utcish(nifty_tick.get("last_trade_time"))
            price, close = _number(nifty_tick.get("last_price")), _number(nifty_tick.get("close"))
            age = (now - received).total_seconds() if received else None
            if received and trade and trade.date() == now.date() and age is not None and -CLOCK_SKEW_SECONDS <= age <= FRESH_SECONDS and price is not None and close not in (None, 0):
                nifty_pct, nifty_fresh = (price / close - 1) * 100, True
        coverage = len(raw) / max(1, len(expected)) * 100
        current: dict[str, dict[str, Any]] = {}
        for symbol, item in raw.items():
            tick, rank, price, volume = item["tick"], ranks[symbol], item["price"], _number(item["tick"].get("volume_traded"))
            turnover = price * volume if volume is not None else None
            current[symbol] = {"at": now, "rank": rank, "price": price, "volume": volume, "turnover": turnover,
                               "pct": item["pct"], "rs": item["pct"] - nifty_pct if nifty_pct is not None else None}
        # Update high-resolution causal state first; snapshots use only retained values <= T.
        for symbol, item in current.items():
            self.history[symbol].append(item)
        event_count = 0
        connection = self._db(date)
        try:
            with connection:
                for symbol, item in current.items():
                    previous = self.previous_rank.get(symbol)
                    for milestone in RANK_MILESTONES:
                        if item["rank"] <= milestone and (previous is None or previous > milestone):
                            cursor = connection.execute(
                                "INSERT OR IGNORE INTO rank_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (date, now.isoformat(), symbol, milestone, item["rank"], item["pct"], len(raw), len(expected), coverage,
                                 json.dumps({"last_price": item["price"], "cumulative_volume": item["volume"], "approx_turnover": item["turnover"]}, sort_keys=True)),
                            )
                            event_count += cursor.rowcount
                    self.previous_rank[symbol] = item["rank"]
            if self.last_snapshot_at and (now - self.last_snapshot_at).total_seconds() < SNAPSHOT_SECONDS:
                self._manifest(date, len(expected), len(raw), nifty_fresh, now)
                return {"status": "events_only", "events": event_count, "fresh": len(raw), "expected": len(expected), "processing_ms": round((time.perf_counter()-started)*1000, 3)}
            self.last_snapshot_at = now
            rows: dict[str, dict[str, Any]] = {}
            for symbol in expected:
                item = current.get(symbol)
                tick = raw[symbol]["tick"] if symbol in raw else {}
                history = self.history[symbol]
                past = {m: self._past(history, now, m * 60) for m in (1, 3, 5, 10)}
                rank = item["rank"] if item else None
                rank_improvement = {m: self._change(past[m]["rank"] if past[m] else None, rank) for m in past}
                velocity = {m: (rank_improvement[m] / m if rank_improvement[m] is not None else None) for m in (1, 5)}
                acceleration = None
                if velocity[1] is not None and velocity[5] is not None:
                    acceleration = (velocity[1] - velocity[5]) / 4.0
                high, low, opening = _number(tick.get("high")), _number(tick.get("low")), _number(tick.get("open"))
                bid, ask, vwap = _number(tick.get("best_bid")), _number(tick.get("best_ask")), _number(tick.get("average_traded_price"))
                price = item["price"] if item else None
                spread = ((ask - bid) / ((ask + bid) / 2) * 10000) if bid and ask and ask >= bid else None
                row = {"session_date": date, "timestamp": now.isoformat(), "symbol": symbol, "fresh": int(item is not None),
                       "token_mapped": int(symbol in mapped), "data_age_seconds": raw[symbol]["age"] if symbol in raw else None,
                       "schema_version": SCHEMA_VERSION, "recorder_git_sha": self.recorder_git_sha, "last_price": price,
                       "previous_close": raw[symbol]["close"] if symbol in raw else None, "current_pct_change": item["pct"] if item else None,
                       "broad_rank": rank, "broad_percentile": ((len(raw)-rank)/(len(raw)-1)*100 if rank and len(raw)>1 else None),
                       "rank_acceleration": acceleration, "cumulative_volume": item["volume"] if item else None,
                       "approx_turnover": item["turnover"] if item else None,
                       "distance_from_day_high_pct": self._return(price, high), "distance_from_day_low_pct": self._return(price, low),
                       "distance_from_open_pct": self._return(price, opening), "best_bid": bid, "best_ask": ask, "spread_bps": spread,
                       "causal_vwap": vwap, "price_vs_vwap_pct": self._return(price, vwap), "nifty_pct_change": nifty_pct,
                       "rs_vs_nifty": item["rs"] if item else None, "rvol_at_time": None}
                for m in (1, 3, 5, 10):
                    old = past[m]
                    row[f"rank_{m}m_ago"] = old["rank"] if old else None
                    row[f"rank_improvement_{m}m"] = rank_improvement[m]
                    row[f"return_{m}m"] = self._return(price, old["price"] if old else None)
                    if m in (1, 3, 5): row[f"volume_change_{m}m"] = self._change(item["volume"] if item else None, old["volume"] if old else None)
                row["rank_velocity_1m"], row["rank_velocity_5m"] = velocity[1], velocity[5]
                row["volume_rate_1m"] = row.get("volume_change_1m")
                row["volume_rate_5m"] = (row.get("volume_change_5m") / 5 if row.get("volume_change_5m") is not None else None)
                row["volume_rate_acceleration"] = (row["volume_rate_1m"] - row["volume_rate_5m"]) / 4 if row["volume_rate_1m"] is not None and row["volume_rate_5m"] is not None else None
                row["turnover_change_1m"] = self._change(item["turnover"] if item else None, past[1]["turnover"] if past[1] else None)
                row["turnover_change_5m"] = self._change(item["turnover"] if item else None, past[5]["turnover"] if past[5] else None)
                row["turnover_acceleration"] = (row["turnover_change_1m"] - row["turnover_change_5m"] / 5) / 4 if row["turnover_change_1m"] is not None and row["turnover_change_5m"] is not None else None
                row["rs_change_1m"] = self._change(row["rs_vs_nifty"], past[1]["rs"] if past[1] else None)
                row["rs_change_5m"] = self._change(row["rs_vs_nifty"], past[5]["rs"] if past[5] else None)
                for n in (100, 50, 20, 10): row[f"pct_change_gap_to_top{n}"] = item["pct"] - cutoff[n] if item and cutoff[n] is not None else None
                rows[symbol] = row
            metric_columns = {"price_momentum_percentile": "current_pct_change", "rank_velocity_percentile": "rank_velocity_1m", "volume_rate_percentile": "volume_rate_1m", "volume_acceleration_percentile": "volume_rate_acceleration", "turnover_percentile": "approx_turnover", "turnover_acceleration_percentile": "turnover_acceleration", "rs_percentile": "rs_vs_nifty"}
            for percentile_column, source in metric_columns.items():
                percentiles = _percentile({s: row[source] for s, row in rows.items() if row["fresh"] and row[source] is not None})
                for symbol, value in percentiles.items(): rows[symbol][percentile_column] = value
            columns = list(next(iter(rows.values())).keys())
            with connection:
                connection.executemany(f"INSERT OR REPLACE INTO snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [tuple(row.get(column) for column in columns) for row in rows.values()])
                changes = [item["pct"] for item in current.values()]
                connection.execute("INSERT OR REPLACE INTO market_context VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (date, now.isoformat(), len(raw), len(expected), coverage, int(nifty_fresh), nifty_pct, sum(x > 0 for x in changes), sum(x < 0 for x in changes), statistics.median(changes) if changes else None, cutoff[100], cutoff[50], cutoff[20], cutoff[10], statistics.pstdev(changes) if len(changes)>1 else None))
            self._manifest(date, len(expected), len(raw), nifty_fresh, now)
            return {"status": "recorded", "events": event_count, "rows": len(rows), "fresh": len(raw), "expected": len(expected), "processing_ms": round((time.perf_counter()-started)*1000, 3)}
        finally:
            connection.close()
