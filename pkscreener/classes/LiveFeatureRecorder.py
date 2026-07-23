"""Shadow recorder for live-only intraday features.

Historical 5-minute candles cannot tell us whether a live leader had stable
visible buy depth, tight spreads, or broad sector support at the moment we saw
it. This recorder captures those facts from Kite ticks without creating alerts
or orders.
"""

import csv
import json
import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

try:
    from pkscreener.classes.MarketContext import get_sector
except Exception:
    def get_sector(symbol: str) -> str:
        return "UNKNOWN"


class LiveFeatureRecorder:
    """Persist live microstructure/context rows for later research."""

    GENERATION = 3

    COLUMNS = [
        "When",
        "Bucket",
        "RecorderGeneration",
        "Symbol",
        "Sector",
        "LastPrice",
        "PrevClose",
        "Open",
        "DayHigh",
        "DayChangePct",
        "UniverseSize",
        "DayChangeRank",
        "DayChangeRankPct",
        "PreviousDayChangeRank",
        "RankImprovement",
        "RankVelocityPerMinute",
        "DayChangeDeltaPct",
        "EnteredTop100",
        "EnteredTop50",
        "RapidRankClimber",
        "OpenChangePct",
        "PullbackFromHighPct",
        "Volume",
        "TradedValue",
        "BuyQty",
        "SellQty",
        "BuyerSellerRatio",
        "BuyerSellerMedianWindow",
        "BuyerSellerMinWindow",
        "OrderBookStable",
        "BestBid",
        "BestAsk",
        "SpreadBps",
        "SpreadOk",
        "SectorCount",
        "SectorAvgChangePct",
        "SectorBreadthPct",
        "SectorTop2Count",
        "SectorTop5Count",
        "MarketBreadthPct",
        "IsTopGainerNow",
        "AlreadyAlertedToday",
        "MissedRunnerCandidate",
        "RecordReason",
    ]

    DECISION_COLUMNS = [
        "When",
        "Bucket",
        "Lane",
        "Stage",
        "Symbol",
        "Decision",
        "Reason",
        "SourceAction",
        "PriceNow",
        "TickLastPrice",
        "ChangePct",
        "OpenMovePct",
        "PullbackPct",
        "SignalStrengthPct",
        "VolumeVsAvg",
        "BuyerSellerRatio",
        "RSI",
        "DayHigh",
        "Volume",
        "VolumeDeltaPct",
        "TradedValue",
        "BuyQty",
        "SellQty",
        "BestBid",
        "BestAsk",
        "SpreadBps",
        "SpreadOk",
        "Checks",
        "Why",
    ]

    def __init__(
        self,
        output_dir: str,
        interval_seconds: float = 60.0,
        top_gainer_pct: float = 5.0,
        stability_window: int = 3,
        min_stable_bs_ratio: float = 2.0,
        spread_ok_bps: float = 35.0,
        max_rows_per_cycle: int = 200,
        leader_pool_size: int = 75,
        rapid_rank_improvement: int = 25,
        rapid_change_delta_pct: float = 0.15,
    ) -> None:
        self.output_dir = str(output_dir or "")
        self.interval_seconds = max(0.0, float(interval_seconds or 0.0))
        self.top_gainer_pct = float(top_gainer_pct or 5.0)
        self.stability_window = max(2, int(stability_window or 3))
        self.min_stable_bs_ratio = max(1.0, float(min_stable_bs_ratio or 2.0))
        self.spread_ok_bps = max(0.0, float(spread_ok_bps or 35.0))
        self.max_rows_per_cycle = max(1, int(max_rows_per_cycle or 200))
        self.leader_pool_size = max(1, int(leader_pool_size or 75))
        self.rapid_rank_improvement = max(
            1, int(rapid_rank_improvement or 25)
        )
        self.rapid_change_delta_pct = max(
            0.0, float(rapid_change_delta_pct or 0.15)
        )
        self._last_record_ts = 0.0
        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._rank_date = ""
        self._rank_state: Dict[str, Dict[str, Any]] = {}
        self._decision_seen: Set[Tuple[str, str, str, str, str, str]] = set()
        self._last_leader_pool_candidate_count = 0
        self._last_rapid_candidate_count = 0

    def record(
        self,
        ticks: Dict[str, Dict[str, Any]],
        alerted_symbols: Optional[Set[str]] = None,
        now: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Build and append one throttled live-feature snapshot."""
        now_dt = now or self._now()
        now_ts = now_dt.timestamp()
        if self.interval_seconds > 0 and self._last_record_ts:
            if now_ts - self._last_record_ts < self.interval_seconds:
                return pd.DataFrame(columns=self.COLUMNS)

        rows = self.build_rows(ticks, alerted_symbols=alerted_symbols, now=now_dt)
        self._last_record_ts = now_ts
        if not rows:
            return pd.DataFrame(columns=self.COLUMNS)

        df = pd.DataFrame(rows, columns=self.COLUMNS)
        path = self._path_for(now_dt)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_feature_schema(path)
            df.to_csv(path, mode="a", header=not path.exists(), index=False)
            self._write_rank_status(df, now_dt)
        except Exception as exc:
            default_logger().debug(f"Could not append live feature recorder rows: {exc}", exc_info=True)
        return df

    def _ensure_feature_schema(self, path: Path) -> None:
        """Atomically widen an existing daily CSV when recorder fields change."""
        if not path.exists() or path.stat().st_size <= 0:
            return
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            old_header = next(reader, [])
            if old_header == self.COLUMNS:
                return
            if not old_header or not set(old_header).issubset(set(self.COLUMNS)):
                raise ValueError(
                    f"unsupported live feature schema in {path}: {old_header}"
                )
            raw_rows = list(reader)

        migrated_rows = []
        for row in raw_rows:
            if len(row) == len(old_header):
                source = dict(zip(old_header, row))
            elif len(row) == len(self.COLUMNS):
                # A process upgraded during the session may have appended the
                # new row shape below the old header before this migration ran.
                source = dict(zip(self.COLUMNS, row))
            else:
                raise ValueError(
                    f"unsupported row width {len(row)} in {path}; expected "
                    f"{len(old_header)} or {len(self.COLUMNS)}"
                )
            migrated_rows.append([source.get(column, "") for column in self.COLUMNS])

        temporary = path.with_suffix(path.suffix + ".schema_tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.COLUMNS)
            writer.writerows(migrated_rows)
        os.replace(temporary, path)

    def _write_rank_status(self, frame: pd.DataFrame, now: datetime) -> None:
        """Persist a cheap operational view without rereading the daily tape."""
        status_path = (
            Path(self.output_dir)
            / "live_feature_recorder"
            / "full_universe_rank_status.json"
        )
        rapid = (
            int(frame["RapidRankClimber"].astype(bool).sum())
            if "RapidRankClimber" in frame.columns
            else 0
        )
        universe_size = (
            int(pd.to_numeric(frame["UniverseSize"], errors="coerce").max())
            if "UniverseSize" in frame.columns and not frame.empty
            else 0
        )
        leader_pool_recorded = 0
        if not frame.empty and {"DayChangeRank", "DayChangePct"}.issubset(
            frame.columns
        ):
            ranks = pd.to_numeric(frame["DayChangeRank"], errors="coerce")
            moves = pd.to_numeric(frame["DayChangePct"], errors="coerce")
            leader_pool_recorded = int(
                (ranks.le(self.leader_pool_size) & moves.ge(0.5)).sum()
            )
        leader_pool_candidates = int(
            getattr(self, "_last_leader_pool_candidate_count", 0) or 0
        )
        rapid_recorded = (
            int(frame["RapidRankClimber"].astype(bool).sum())
            if "RapidRankClimber" in frame.columns
            else 0
        )
        rapid_candidates = int(
            getattr(self, "_last_rapid_candidate_count", 0) or 0
        )
        existing = {}
        if status_path.exists():
            try:
                existing = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        generation_started_at = now.strftime("%Y-%m-%d %H:%M:%S")
        same_generation = (
            str(existing.get("date", "")) == now.strftime("%Y-%m-%d")
            and int(existing.get("recorder_generation") or 0)
            == int(self.GENERATION)
            and bool(existing.get("generation_started_at"))
        )
        if same_generation:
            generation_started_at = str(existing["generation_started_at"])
        first_recorded_at = generation_started_at
        try:
            first_time = datetime.strptime(
                first_recorded_at, "%Y-%m-%d %H:%M:%S"
            )
            full_session_eligible = (
                first_time.hour * 60 + first_time.minute <= 9 * 60 + 20
            )
        except Exception:
            full_session_eligible = False
        payload = {
            "status": "recording_full_universe_rank",
            "date": now.strftime("%Y-%m-%d"),
            "recorded_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "first_recorded_at": first_recorded_at,
            "generation_started_at": generation_started_at,
            "full_session_eligible": full_session_eligible,
            "universe_size": universe_size,
            "rows_this_snapshot": int(len(frame)),
            "rapid_rank_climbers_this_snapshot": rapid,
            "leader_pool_size": int(self.leader_pool_size),
            "leader_pool_candidates_this_snapshot": leader_pool_candidates,
            "leader_pool_recorded_this_snapshot": leader_pool_recorded,
            "leader_pool_recall_pct": round(
                leader_pool_recorded * 100.0 / leader_pool_candidates, 2
            )
            if leader_pool_candidates > 0
            else 100.0,
            "rapid_candidates_this_snapshot": rapid_candidates,
            "rapid_recorded_this_snapshot": rapid_recorded,
            "rapid_recall_pct": round(
                rapid_recorded * 100.0 / rapid_candidates, 2
            )
            if rapid_candidates > 0
            else 100.0,
            "recorder_generation": int(self.GENERATION),
            "interval_seconds": float(self.interval_seconds),
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def record_decisions(
        self,
        lane: str,
        decisions: List[Dict[str, Any]],
        ticks: Optional[Dict[str, Dict[str, Any]]] = None,
        now: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Append pass/reject lane decisions enriched with live tick context."""
        if not decisions:
            return pd.DataFrame(columns=self.DECISION_COLUMNS)

        now_dt = now or self._now()
        bucket = self._bucket(now_dt)
        lane_name = str(lane or "unknown").strip() or "unknown"
        normalized_ticks = {
            str(symbol).strip().upper(): tick
            for symbol, tick in (ticks or {}).items()
            if str(symbol).strip() and isinstance(tick, dict)
        }

        rows: List[Dict[str, Any]] = []
        day = now_dt.strftime("%Y-%m-%d")
        for decision in decisions:
            symbol = str(decision.get("Symbol", "") or "").strip().upper()
            if not symbol:
                continue
            decision_label = str(decision.get("Decision", "") or "").strip()
            reason = str(decision.get("Reason", "") or "").strip()
            key = (day, bucket, lane_name, symbol, decision_label, reason)
            if key in self._decision_seen:
                continue
            self._decision_seen.add(key)

            tick = normalized_ticks.get(symbol, {})
            price_now = self._to_float(decision.get("PriceNow")) or self._to_float(tick.get("last_price"))
            tick_last_price = self._to_float(tick.get("last_price"))
            day_high = self._to_float(tick.get("high")) or self._to_float(decision.get("DayHigh"))
            best_bid, best_ask = self.best_bid_ask(tick)
            spread_bps = self._spread_bps(best_bid, best_ask)
            buy_qty = self._to_float(tick.get("total_buy_quantity")) or self._to_float(decision.get("TotalBuyQty"))
            sell_qty = self._to_float(tick.get("total_sell_quantity")) or self._to_float(decision.get("TotalSellQty"))
            bs_ratio = self._to_float(decision.get("BuyerSellerRatio")) or self._buyer_seller_ratio(buy_qty, sell_qty)
            volume = self._to_float(tick.get("volume_traded")) or self._to_float(decision.get("Volume"))
            traded_value = price_now * volume if price_now > 0 and volume > 0 else self._to_float(decision.get("TradedValue"))
            change_pct = (
                self._to_float(decision.get("ChangePct"))
                or self._to_float(decision.get("DayChangePct"))
                or self._day_change_pct(tick)
            )
            pullback_pct = self._to_float(decision.get("PullbackPct"))
            if pullback_pct == 0.0 and day_high > 0 and price_now > 0:
                pullback_pct = max(0.0, (day_high - price_now) / price_now * 100.0)

            rows.append(
                {
                    "When": str(decision.get("When", "") or now_dt.strftime("%Y-%m-%d %H:%M:%S")),
                    "Bucket": bucket,
                    "Lane": lane_name,
                    "Stage": str(decision.get("Stage", "") or ""),
                    "Symbol": symbol,
                    "Decision": decision_label,
                    "Reason": reason,
                    "SourceAction": str(decision.get("Action", "") or decision.get("Stage", "") or ""),
                    "PriceNow": round(price_now, 2),
                    "TickLastPrice": round(tick_last_price, 2),
                    "ChangePct": round(change_pct, 3),
                    "OpenMovePct": round(self._to_float(decision.get("OpenMovePct")), 3),
                    "PullbackPct": round(max(0.0, pullback_pct), 3),
                    "SignalStrengthPct": round(
                        self._to_float(decision.get("SignalStrengthPct")) or self._to_float(decision.get("Score")),
                        3,
                    ),
                    "VolumeVsAvg": round(self._to_float(decision.get("VolumeVsAvg")), 3),
                    "BuyerSellerRatio": round(bs_ratio, 3),
                    "RSI": round(self._to_float(decision.get("RSI")), 3),
                    "DayHigh": round(day_high, 2),
                    "Volume": int(volume),
                    "VolumeDeltaPct": round(self._to_float(decision.get("VolumeDeltaPct")), 3),
                    "TradedValue": round(traded_value, 2),
                    "BuyQty": int(buy_qty),
                    "SellQty": int(sell_qty),
                    "BestBid": round(best_bid, 2),
                    "BestAsk": round(best_ask, 2),
                    "SpreadBps": round(spread_bps, 3),
                    "SpreadOk": (
                        best_bid > 0.0
                        and best_ask > best_bid
                        and 0.0 < spread_bps <= self.spread_ok_bps
                    ),
                    "Checks": str(decision.get("Checks", "") or ""),
                    "Why": str(decision.get("Why", "") or ""),
                }
            )

        if not rows:
            return pd.DataFrame(columns=self.DECISION_COLUMNS)

        df = pd.DataFrame(rows, columns=self.DECISION_COLUMNS)
        path = self._decision_path_for(now_dt)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, mode="a", header=not path.exists(), index=False)
        except Exception as exc:
            default_logger().debug(f"Could not append live decision journal rows: {exc}", exc_info=True)
        return df

    def build_rows(
        self,
        ticks: Dict[str, Dict[str, Any]],
        alerted_symbols: Optional[Set[str]] = None,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Return interesting live-feature rows without writing to disk."""
        if not ticks:
            return []
        now_dt = now or self._now()
        alerted = {str(sym).strip().upper() for sym in (alerted_symbols or set()) if str(sym).strip()}
        normalized_ticks = {
            str(symbol).strip().upper(): tick
            for symbol, tick in ticks.items()
            if str(symbol).strip() and isinstance(tick, dict)
        }
        day_key = now_dt.strftime("%Y-%m-%d")
        if self._rank_date != day_key:
            self._rank_date = day_key
            self._rank_state = {}
        ranked_symbols = []
        for symbol, tick in normalized_ticks.items():
            price = self._to_float(tick.get("last_price"))
            if price <= 0:
                continue
            ranked_symbols.append((symbol, self._day_change_pct(tick)))
        ranked_symbols.sort(key=lambda item: (-item[1], item[0]))
        universe_size = len(ranked_symbols)
        self._last_leader_pool_candidate_count = sum(
            1
            for rank, (_, change) in enumerate(ranked_symbols, start=1)
            if rank <= self.leader_pool_size and change >= 0.5
        )
        rank_map = {
            symbol: rank
            for rank, (symbol, _) in enumerate(ranked_symbols, start=1)
        }
        change_map = {symbol: change for symbol, change in ranked_symbols}
        sector_stats, market_breadth_pct = self._build_sector_stats(normalized_ticks)
        rows: List[Dict[str, Any]] = []

        for symbol, tick in normalized_ticks.items():
            price = self._to_float(tick.get("last_price"))
            if price <= 0:
                continue

            prev_close = self._to_float(tick.get("close"))
            day_open = self._to_float(tick.get("open"))
            day_high = self._to_float(tick.get("high"))
            day_change_pct = self._day_change_pct(tick)
            day_change_rank = int(rank_map.get(symbol, 0) or 0)
            day_change_rank_pct = (
                (universe_size - day_change_rank + 1) * 100.0 / universe_size
                if universe_size > 0 and day_change_rank > 0
                else 0.0
            )
            prior_rank = self._rank_state.get(symbol, {})
            previous_rank = int(prior_rank.get("rank", 0) or 0)
            rank_improvement = (
                previous_rank - day_change_rank
                if previous_rank > 0 and day_change_rank > 0
                else 0
            )
            day_change_delta_pct = (
                day_change_pct - self._to_float(prior_rank.get("change"))
                if previous_rank > 0
                else 0.0
            )
            elapsed_minutes = 0.0
            previous_when = prior_rank.get("when")
            if isinstance(previous_when, datetime):
                elapsed_minutes = max(
                    (now_dt - previous_when).total_seconds() / 60.0, 1e-6
                )
            rank_velocity = (
                rank_improvement / elapsed_minutes if elapsed_minutes > 0 else 0.0
            )
            entered_top100 = bool(
                day_change_rank > 0
                and day_change_rank <= 100
                and previous_rank > 100
            )
            entered_top50 = bool(
                day_change_rank > 0
                and day_change_rank <= 50
                and previous_rank > 50
            )
            rapid_rank_climber = bool(
                previous_rank > 0
                and rank_improvement >= self.rapid_rank_improvement
                and day_change_delta_pct >= self.rapid_change_delta_pct
                and 0.5 <= day_change_pct <= 5.0
            )
            open_change_pct = self._pct_change(price, day_open)
            pullback_pct = ((day_high - price) / price * 100.0) if day_high > 0 and price > 0 else 0.0
            volume = self._to_float(tick.get("volume_traded"))
            buy_qty = self._to_float(tick.get("total_buy_quantity"))
            sell_qty = self._to_float(tick.get("total_sell_quantity"))
            bs_ratio = self._buyer_seller_ratio(buy_qty, sell_qty)
            best_bid, best_ask = self.best_bid_ask(tick)
            spread_bps = self._spread_bps(best_bid, best_ask)
            history_stats = self._update_history(symbol, now_dt, bs_ratio, spread_bps)
            sector = get_sector(symbol) or "UNKNOWN"
            sector_row = sector_stats.get(sector, {})
            is_top_gainer_now = day_change_pct >= self.top_gainer_pct
            already_alerted = symbol in alerted
            missed_runner = is_top_gainer_now and not already_alerted
            ob_stable = bool(history_stats["stable"])
            spread_ok = (
                best_bid > 0.0
                and best_ask > best_bid
                and 0.0 < spread_bps <= self.spread_ok_bps
            )

            reasons = []
            if missed_runner:
                reasons.append("missed_runner")
            if is_top_gainer_now:
                reasons.append("top_gainer")
            if ob_stable:
                reasons.append("stable_order_book")
            if already_alerted:
                reasons.append("alerted_today")
            if day_change_pct >= 2.0 and int(sector_row.get("top2_count", 0) or 0) >= 2:
                reasons.append("sector_leader_cluster")
            if (
                day_change_rank > 0
                and day_change_rank <= self.leader_pool_size
                and day_change_pct >= 0.5
            ):
                reasons.append("full_universe_leader_pool")
            if rapid_rank_climber:
                reasons.append("rapid_rank_climber")
            if not reasons:
                continue

            rows.append(
                {
                    "When": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "Bucket": self._bucket(now_dt),
                    "RecorderGeneration": int(self.GENERATION),
                    "Symbol": symbol,
                    "Sector": sector,
                    "LastPrice": round(price, 2),
                    "PrevClose": round(prev_close, 2),
                    "Open": round(day_open, 2),
                    "DayHigh": round(day_high, 2),
                    "DayChangePct": round(day_change_pct, 3),
                    "UniverseSize": universe_size,
                    "DayChangeRank": day_change_rank,
                    "DayChangeRankPct": round(day_change_rank_pct, 3),
                    "PreviousDayChangeRank": previous_rank,
                    "RankImprovement": rank_improvement,
                    "RankVelocityPerMinute": round(rank_velocity, 3),
                    "DayChangeDeltaPct": round(day_change_delta_pct, 3),
                    "EnteredTop100": entered_top100,
                    "EnteredTop50": entered_top50,
                    "RapidRankClimber": rapid_rank_climber,
                    "OpenChangePct": round(open_change_pct, 3),
                    "PullbackFromHighPct": round(max(0.0, pullback_pct), 3),
                    "Volume": int(volume),
                    "TradedValue": round(price * volume, 2),
                    "BuyQty": int(buy_qty),
                    "SellQty": int(sell_qty),
                    "BuyerSellerRatio": round(bs_ratio, 3),
                    "BuyerSellerMedianWindow": round(float(history_stats["median"]), 3),
                    "BuyerSellerMinWindow": round(float(history_stats["min"]), 3),
                    "OrderBookStable": ob_stable,
                    "BestBid": round(best_bid, 2),
                    "BestAsk": round(best_ask, 2),
                    "SpreadBps": round(spread_bps, 3),
                    "SpreadOk": spread_ok,
                    "SectorCount": int(sector_row.get("count", 0) or 0),
                    "SectorAvgChangePct": round(float(sector_row.get("avg_change", 0.0) or 0.0), 3),
                    "SectorBreadthPct": round(float(sector_row.get("breadth_pct", 0.0) or 0.0), 3),
                    "SectorTop2Count": int(sector_row.get("top2_count", 0) or 0),
                    "SectorTop5Count": int(sector_row.get("top5_count", 0) or 0),
                    "MarketBreadthPct": round(market_breadth_pct, 3),
                    "IsTopGainerNow": is_top_gainer_now,
                    "AlreadyAlertedToday": already_alerted,
                    "MissedRunnerCandidate": missed_runner,
                    "RecordReason": ",".join(reasons),
                }
            )

        rows.sort(
            key=lambda row: (
                not bool(
                    int(row["DayChangeRank"] or 999999)
                    <= self.leader_pool_size
                    and float(row["DayChangePct"] or 0.0) >= 0.5
                ),
                not bool(row["RapidRankClimber"]),
                not bool(row["EnteredTop50"]),
                not bool(row["MissedRunnerCandidate"]),
                not bool(row["OrderBookStable"]),
                int(row["DayChangeRank"] or 999999),
                str(row["Symbol"]),
            )
        )
        self._rank_state = {
            symbol: {
                "rank": int(rank_map.get(symbol, 0) or 0),
                "change": float(change_map.get(symbol, 0.0) or 0.0),
                "when": now_dt,
            }
            for symbol in rank_map
        }
        mandatory_rows = [
            row
            for row in rows
            if bool(row["RapidRankClimber"])
            or (
                int(row["DayChangeRank"] or 999999) <= self.leader_pool_size
                and float(row["DayChangePct"] or 0.0) >= 0.5
            )
        ]
        self._last_rapid_candidate_count = sum(
            bool(row["RapidRankClimber"]) for row in rows
        )
        mandatory_ids = {id(row) for row in mandatory_rows}
        supplemental_rows = [
            row for row in rows if id(row) not in mandatory_ids
        ]
        supplemental_slots = max(
            0, self.max_rows_per_cycle - len(mandatory_rows)
        )
        return mandatory_rows + supplemental_rows[:supplemental_slots]

    @staticmethod
    def best_bid_ask(tick: Dict[str, Any]) -> Tuple[float, float]:
        """Extract best bid/ask from Kite tick variants."""
        best_bid = LiveFeatureRecorder._to_float(tick.get("best_bid") or tick.get("bid"))
        best_ask = LiveFeatureRecorder._to_float(tick.get("best_ask") or tick.get("ask"))
        depth = tick.get("depth") if isinstance(tick, dict) else {}
        if isinstance(depth, dict):
            buy_depth = depth.get("buy") or []
            sell_depth = depth.get("sell") or []
            if best_bid <= 0 and buy_depth:
                best_bid = LiveFeatureRecorder._to_float((buy_depth[0] or {}).get("price"))
            if best_ask <= 0 and sell_depth:
                best_ask = LiveFeatureRecorder._to_float((sell_depth[0] or {}).get("price"))
        if best_bid <= 0 or best_ask <= best_bid:
            return 0.0, 0.0
        return best_bid, best_ask

    def _build_sector_stats(self, ticks: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, float]], float]:
        sector_changes: Dict[str, List[float]] = {}
        market_advancers = 0
        market_decliners = 0
        for symbol, tick in ticks.items():
            change = self._day_change_pct(tick)
            if change > 0:
                market_advancers += 1
            elif change < 0:
                market_decliners += 1
            sector = get_sector(symbol) or "UNKNOWN"
            if sector == "UNKNOWN":
                continue
            sector_changes.setdefault(sector, []).append(change)

        stats: Dict[str, Dict[str, float]] = {}
        for sector, changes in sector_changes.items():
            if not changes:
                continue
            advancers = sum(1 for value in changes if value > 0)
            stats[sector] = {
                "count": float(len(changes)),
                "avg_change": float(sum(changes) / len(changes)),
                "breadth_pct": float(advancers * 100.0 / len(changes)),
                "top2_count": float(sum(1 for value in changes if value >= 2.0)),
                "top5_count": float(sum(1 for value in changes if value >= 5.0)),
            }

        breadth_denominator = market_advancers + market_decliners
        market_breadth_pct = (
            market_advancers * 100.0 / breadth_denominator
            if breadth_denominator > 0
            else 0.0
        )
        return stats, market_breadth_pct

    def _update_history(
        self,
        symbol: str,
        now: datetime,
        bs_ratio: float,
        spread_bps: float,
    ) -> Dict[str, float]:
        history = self._history.setdefault(symbol, [])
        cutoff = now - timedelta(minutes=5)
        history[:] = [row for row in history if row.get("when") and row["when"] >= cutoff]
        history.append({"when": now, "bs_ratio": bs_ratio, "spread_bps": spread_bps})
        window = [row["bs_ratio"] for row in history[-self.stability_window :] if row.get("bs_ratio", 0.0) > 0]
        if len(window) < self.stability_window:
            return {"stable": False, "median": window[-1] if window else 0.0, "min": min(window) if window else 0.0}
        median_value = statistics.median(window)
        min_value = min(window)
        stable = min_value >= self.min_stable_bs_ratio and median_value >= self.min_stable_bs_ratio
        return {"stable": stable, "median": median_value, "min": min_value}

    @staticmethod
    def _buyer_seller_ratio(buy_qty: float, sell_qty: float) -> float:
        if sell_qty > 0:
            return buy_qty / sell_qty
        if buy_qty > 0:
            return 99.0
        return 0.0

    @staticmethod
    def _day_change_pct(tick: Dict[str, Any]) -> float:
        explicit = LiveFeatureRecorder._to_float(tick.get("change"))
        if explicit != 0.0:
            return explicit
        price = LiveFeatureRecorder._to_float(tick.get("last_price"))
        prev_close = LiveFeatureRecorder._to_float(tick.get("close"))
        return LiveFeatureRecorder._pct_change(price, prev_close)

    @staticmethod
    def _spread_bps(best_bid: float, best_ask: float) -> float:
        if best_bid <= 0 or best_ask <= 0:
            return 0.0
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return 0.0
        return (best_ask - best_bid) / mid * 10_000.0

    @staticmethod
    def _pct_change(value: float, base: float) -> float:
        if value <= 0 or base <= 0:
            return 0.0
        return (value - base) / base * 100.0

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            if value in [None, "", "None"]:
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    def _path_for(self, now: datetime) -> Path:
        day = now.strftime("%Y-%m-%d")
        return Path(self.output_dir) / "live_feature_recorder" / f"live_features_{day}.csv"

    def _decision_path_for(self, now: datetime) -> Path:
        day = now.strftime("%Y-%m-%d")
        return Path(self.output_dir) / "live_feature_recorder" / f"lane_decisions_{day}.csv"

    def _bucket(self, now: datetime) -> str:
        if self.interval_seconds <= 0:
            return now.strftime("%Y-%m-%d %H:%M:%S")
        interval = int(max(1.0, self.interval_seconds))
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        bucket_seconds = seconds - (seconds % interval)
        bucket = now.replace(
            hour=bucket_seconds // 3600,
            minute=(bucket_seconds % 3600) // 60,
            second=bucket_seconds % 60,
            microsecond=0,
        )
        return bucket.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _now() -> datetime:
        try:
            return PKDateUtilities.currentDateTime()
        except Exception:
            return datetime.now()
