"""Append-only, WATCH-only coverage evidence from raw Kite snapshots."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pkscreener.classes.NGP5EarlyWatchPublisher import IST


def _symbols(path: Path, column: str, eq_only: bool = False) -> set[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {str(row.get(column) or "").strip().upper() for row in csv.DictReader(handle)
                if row.get(column) and (not eq_only or str(row.get("SERIES") or "").upper() == "EQ")}


class MarketObservationLedger:
    """Records observation state only; it deliberately has no alert/order API."""
    def __init__(self, root: Path, eq_path: Path, nifty_path: Path, tokens_path: Path, clock=datetime.now):
        self.root, self.eq_path, self.nifty_path, self.tokens_path, self.clock = Path(root), Path(eq_path), Path(nifty_path), Path(tokens_path), clock

    def process(self, ticks_path: Path) -> dict[str, Any]:
        now = self.clock().astimezone(IST)
        day = now.date().isoformat(); base = self.root / day; base.mkdir(parents=True, exist_ok=True)
        eq, nifty = _symbols(self.eq_path, "SYMBOL", True), _symbols(self.nifty_path, "Symbol")
        subscribed = _symbols(self.tokens_path, "Symbol") if self.tokens_path.exists() else set()
        ticks = list(csv.DictReader(Path(ticks_path).open(newline="", encoding="utf-8")))
        fresh = []; ranks = []
        for row in ticks:
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol not in eq or str(row.get("exchange") or "").upper() != "NSE": continue
            try:
                received = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
                trade_day = datetime.fromisoformat(row["last_trade_time"]).date()
                price, close = float(row["last_price"]), float(row["close"])
            except (KeyError, TypeError, ValueError): continue
            if trade_day != now.date() or abs((now - received.astimezone(IST)).total_seconds()) > 120 or close <= 0: continue
            fresh.append(symbol); ranks.append((symbol, (price / close - 1) * 100))
        ranks.sort(key=lambda x: (-x[1], x[0])); rank_map = {s: i + 1 for i, (s, _) in enumerate(ranks)}
        state_path = base / "state.json"; state = json.loads(state_path.read_text()) if state_path.exists() else {"symbols": {}}
        events = []
        for symbol in sorted(eq):
            previous = state["symbols"].get(symbol, {}); rank = rank_map.get(symbol)
            fresh_now = symbol in fresh; record = {"expected": True, "subscribed": symbol in subscribed, "fresh": fresh_now, "rank": rank}
            for threshold in (100, 50, 20, 10, 5):
                key = f"first_top{threshold}_at"
                if rank and rank <= threshold and not previous.get(key): record[key] = now.isoformat(); events.append({"symbol": symbol, "event": key, "at": now.isoformat(), "rank": rank})
                elif previous.get(key): record[key] = previous[key]
            if fresh_now and not previous.get("first_fresh_at"): record["first_fresh_at"] = now.isoformat(); events.append({"symbol": symbol, "event": "first_fresh_at", "at": now.isoformat(), "rank": rank})
            elif previous.get("first_fresh_at"): record["first_fresh_at"] = previous["first_fresh_at"]
            if previous.get("fresh") != fresh_now: events.append({"symbol": symbol, "event": "fresh" if fresh_now else "stale", "at": now.isoformat(), "rank": rank})
            if symbol in nifty and rank and previous.get("rank") and rank < previous["rank"]: events.append({"symbol": symbol, "event": "nifty_rank_improved", "at": now.isoformat(), "rank": rank, "velocity": previous["rank"]-rank})
            state["symbols"][symbol] = record
        state_path.write_text(json.dumps(state, sort_keys=True))
        with (base / "events.jsonl").open("a", encoding="utf-8") as handle:
            for event in events: handle.write(json.dumps(event, sort_keys=True) + "\n")
        coverage = {"at": now.isoformat(), "expected_nse_eq": len(eq), "subscribed_nse_eq": len(eq & subscribed), "fresh_nse_eq": len(fresh), "expected_nifty50": len(nifty), "subscribed_nifty50": len(nifty & subscribed), "fresh_nifty50": len(nifty & set(fresh)), "missing_nse_eq": len(eq-subscribed), "stale_nse_eq": len(eq-set(fresh))}
        with (base / "coverage.jsonl").open("a", encoding="utf-8") as handle: handle.write(json.dumps(coverage, sort_keys=True) + "\n")
        return {"status": "observed", **coverage, "events": len(events)}
