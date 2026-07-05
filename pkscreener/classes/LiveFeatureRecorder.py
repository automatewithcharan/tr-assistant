"""Shadow recorder for live-only intraday features.

Historical 5-minute candles cannot tell us whether a live leader had stable
visible buy depth, tight spreads, or broad sector support at the moment we saw
it. This recorder captures those facts from Kite ticks without creating alerts
or orders.
"""

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

    COLUMNS = [
        "When",
        "Bucket",
        "Symbol",
        "Sector",
        "LastPrice",
        "PrevClose",
        "Open",
        "DayHigh",
        "DayChangePct",
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

    def __init__(
        self,
        output_dir: str,
        interval_seconds: float = 60.0,
        top_gainer_pct: float = 5.0,
        stability_window: int = 3,
        min_stable_bs_ratio: float = 2.0,
        spread_ok_bps: float = 35.0,
        max_rows_per_cycle: int = 200,
    ) -> None:
        self.output_dir = str(output_dir or "")
        self.interval_seconds = max(0.0, float(interval_seconds or 0.0))
        self.top_gainer_pct = float(top_gainer_pct or 5.0)
        self.stability_window = max(2, int(stability_window or 3))
        self.min_stable_bs_ratio = max(1.0, float(min_stable_bs_ratio or 2.0))
        self.spread_ok_bps = max(0.0, float(spread_ok_bps or 35.0))
        self.max_rows_per_cycle = max(1, int(max_rows_per_cycle or 200))
        self._last_record_ts = 0.0
        self._history: Dict[str, List[Dict[str, Any]]] = {}

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
            df.to_csv(path, mode="a", header=not path.exists(), index=False)
        except Exception as exc:
            default_logger().debug(f"Could not append live feature recorder rows: {exc}", exc_info=True)
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
            spread_ok = spread_bps == 0.0 or spread_bps <= self.spread_ok_bps

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
            if not reasons:
                continue

            rows.append(
                {
                    "When": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "Bucket": self._bucket(now_dt),
                    "Symbol": symbol,
                    "Sector": sector,
                    "LastPrice": round(price, 2),
                    "PrevClose": round(prev_close, 2),
                    "Open": round(day_open, 2),
                    "DayHigh": round(day_high, 2),
                    "DayChangePct": round(day_change_pct, 3),
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
                not bool(row["MissedRunnerCandidate"]),
                not bool(row["OrderBookStable"]),
                -float(row["DayChangePct"]),
                str(row["Symbol"]),
            )
        )
        return rows[: self.max_rows_per_cycle]

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
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
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
