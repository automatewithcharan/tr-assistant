"""Live paper executor for the prior-session-frozen microstructure ranker.

The class consumes rows already written by ``LiveFeatureRecorder``. It has no
broker dependency and cannot place orders or emit trade alerts.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from pkscreener.classes.FrozenMicrostructureModel import (
    CONTRACT,
    MODEL_FEATURES,
    load_model_artifact,
)


MOVE_BANDS = (
    (0.5, 1.0, "0.5-1", "band_0_5_1"),
    (1.0, 2.0, "1-2", "band_1_2"),
    (2.0, 3.0, "2-3", "band_2_3"),
    (3.0, 5.000001, "3-5", "band_3_5"),
)
MIN_HISTORY_SAMPLE_SECONDS = 50.0


def _float(value: Any) -> float:
    try:
        if value in (None, "", "None"):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "1", "1.0", "true", "yes", "y", "on",
    }


class FrozenMicrostructurePaperTrader:
    """Timestamp predictions and simulate one-slot executable paper trades."""

    PREDICTION_COLUMNS = [
        "date", "when", "symbol", "move_band", "expected_return_pct",
        "selected", "selection_reason", "entry_bid", "entry_ask",
        "recorder_generation", "exchange", "model_trained_through",
        "model_source_sha256", *MODEL_FEATURES,
    ]
    TRADE_COLUMNS = [
        "date", "symbol", "contract", "signal_time", "entry_time", "exit_time",
        "expected_return_pct", "entry_price", "exit_price", "qty", "deployed_capital",
        "target_price", "stop_price", "exit_reason", "gross_pnl", "costs",
        "net_pnl", "net_return_pct", "model_trained_through", "closed",
    ]

    def __init__(
        self,
        output_dir: str,
        model_path: str,
        *,
        capital: float = 500_000.0,
        threshold: float = 0.20,
        max_trades_per_day: int = 10,
        target_pct: float = 1.5,
        stop_pct: float = 1.0,
        fee_pct: float = 0.10,
        horizon_minutes: int = 60,
        persist: bool = True,
    ) -> None:
        self.output_dir = Path(output_dir) / "frozen_microstructure_paper"
        self.model_path = Path(model_path)
        self.capital = float(capital)
        self.threshold = float(threshold)
        self.max_trades_per_day = max(1, int(max_trades_per_day))
        self.target_pct = float(target_pct)
        self.stop_pct = float(stop_pct)
        self.fee_pct = float(fee_pct)
        self.horizon_minutes = max(1, int(horizon_minutes))
        self.persist = bool(persist)
        self.model, self.metadata = load_model_artifact(self.model_path)
        self._model_mtime = self.model_path.stat().st_mtime
        self._date = ""
        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._seen_bands: set[str] = set()
        self._traded_symbols: set[str] = set()
        self.trades: List[Dict[str, Any]] = []
        self.latest_predictions: List[Dict[str, Any]] = []
        self._prediction_keys: set[str] = set()
        if self.persist:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def process(
        self,
        feature_rows: Optional[pd.DataFrame],
        ticks: Dict[str, Dict[str, Any]],
        *,
        now: datetime,
    ) -> List[Dict[str, Any]]:
        today = now.strftime("%Y-%m-%d")
        self._roll_day(today)
        self.latest_predictions = []
        events = self._update_open_trade(ticks, now)
        if not self._artifact_valid_for(today):
            return events

        candidates = []
        if feature_rows is not None and not feature_rows.empty:
            for raw in feature_rows.to_dict(orient="records"):
                candidate = self._candidate(raw, now)
                if candidate is not None:
                    candidates.append(candidate)
        if candidates:
            frame = pd.DataFrame(candidates)
            frame["expected_return_pct"] = self.model.predict(frame[MODEL_FEATURES])
            selected_index = self._selected_index(frame)
            prediction_rows = []
            for index, row in frame.iterrows():
                selected = selected_index is not None and index == selected_index
                prediction = row.to_dict()
                prediction.update(
                    {
                        "date": today,
                        "when": now.isoformat(timespec="seconds"),
                        "expected_return_pct": round(float(row["expected_return_pct"]), 6),
                        "selected": bool(selected),
                        "selection_reason": (
                            "highest_visible_score_slot_available" if selected
                            else self._rejection_reason(row)
                        ),
                        "model_trained_through": self.metadata["trained_through"],
                        "model_source_sha256": self.metadata["source_sha256"],
                    }
                )
                prediction_rows.append(prediction)
            self.latest_predictions = prediction_rows
            self._append_predictions(prediction_rows)
            if selected_index is not None:
                event = self._enter(frame.loc[selected_index].to_dict(), now)
                if event:
                    events.append(event)
        self._persist_trades()
        return events

    def _artifact_valid_for(self, today: str) -> bool:
        trained_through = str(self.metadata.get("trained_through", ""))
        valid_from = str(self.metadata.get("valid_from", ""))
        return bool(
            self.metadata.get("contract") == CONTRACT
            and trained_through
            and trained_through < today
            and (not valid_from or valid_from <= today)
            and self.metadata.get("real_orders_allowed") is False
        )

    def _roll_day(self, today: str) -> None:
        if self._date == today:
            return
        try:
            current_mtime = self.model_path.stat().st_mtime
            if current_mtime != self._model_mtime:
                self.model, self.metadata = load_model_artifact(self.model_path)
                self._model_mtime = current_mtime
        except OSError:
            pass
        self._date = today
        self._history = {}
        self._seen_bands = set()
        self._traded_symbols = set()
        self.trades = []
        self._prediction_keys = set()
        if not self.persist:
            return
        trade_path = self.output_dir / f"trades_{today}.csv"
        if trade_path.exists():
            try:
                existing = pd.read_csv(trade_path).fillna("")
                self.trades = existing.to_dict(orient="records")
                for row in self.trades:
                    row["closed"] = _truthy(row.get("closed"))
                self._traded_symbols = {
                    str(row.get("symbol", "")).strip().upper()
                    for row in self.trades
                    if str(row.get("symbol", "")).strip()
                }
            except (OSError, pd.errors.EmptyDataError):
                self.trades = []
        prediction_path = self.output_dir / f"predictions_{today}.csv"
        if prediction_path.exists():
            try:
                existing = pd.read_csv(prediction_path, usecols=["symbol", "move_band"])
                self._prediction_keys = {
                    f"{today}:{str(row.symbol).strip().upper()}:{str(row.move_band)}"
                    for row in existing.itertuples(index=False)
                }
                self._seen_bands = {
                    f"{str(row.symbol).strip().upper()}:{str(row.move_band)}"
                    for row in existing.itertuples(index=False)
                }
            except (OSError, pd.errors.EmptyDataError, ValueError):
                self._prediction_keys = set()

    def _candidate(self, raw: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        symbol = str(raw.get("Symbol", "") or "").strip().upper()
        if not symbol:
            return None
        current = dict(raw)
        current["_when"] = pd.Timestamp(raw.get("When") or now)
        history = self._history.setdefault(symbol, [])
        if history and history[-1]["_when"] == current["_when"]:
            return None
        if history:
            sample_age = (
                current["_when"] - history[-1]["_when"]
            ).total_seconds()
            if sample_age < MIN_HISTORY_SAMPLE_SECONDS:
                return None
        history.append(current)
        history[:] = history[-8:]
        if len(history) < 3:
            return None
        prior = history[-3]
        elapsed = (current["_when"] - prior["_when"]).total_seconds() / 60.0
        if not 1.0 <= elapsed <= 5.0:
            return None

        signal_minute = current["_when"].hour * 60 + current["_when"].minute
        move = _float(current.get("OpenChangePct"))
        band = next((item for item in MOVE_BANDS if item[0] <= move < item[1]), None)
        if band is None or not 9 * 60 + 18 <= signal_minute <= 14 * 60 + 15:
            return None
        band_key = f"{symbol}:{band[2]}"
        if band_key in self._seen_bands:
            return None
        price = _float(current.get("LastPrice"))
        traded_value = _float(current.get("TradedValue"))
        best_bid = _float(current.get("BestBid"))
        best_ask = _float(current.get("BestAsk"))
        spread = _float(current.get("SpreadBps"))
        if not (
            price >= 50.0
            and traded_value >= 30_000_000.0
            and best_bid > 0.0
            and best_ask > best_bid
            and 0.01 <= spread <= 20.0
        ):
            return None
        self._seen_bands.add(band_key)

        prior_move = _float(prior.get("OpenChangePct"))
        prior_bs = _float(prior.get("BuyerSellerRatio"))
        acceleration = move - prior_move
        bs = _float(current.get("BuyerSellerRatio"))
        row = {
            "symbol": symbol,
            "move_band": band[2],
            "entry_bid": best_bid,
            "entry_ask": best_ask,
            "recorder_generation": int(
                _float(current.get("RecorderGeneration"))
            ),
            "exchange": str(
                current.get("Exchange", "") or ""
            ).strip().upper(),
            "signal_minute": float(signal_minute),
            "DayChangePct": _float(current.get("DayChangePct")),
            "OpenChangePct": move,
            "PullbackFromHighPct": _float(current.get("PullbackFromHighPct")),
            "log_traded_value": math.log1p(max(0.0, traded_value)),
            "BuyerSellerRatio": bs,
            "BuyerSellerMedianWindow": _float(current.get("BuyerSellerMedianWindow")),
            "BuyerSellerMinWindow": _float(current.get("BuyerSellerMinWindow")),
            "OrderBookStable": float(_truthy(current.get("OrderBookStable"))),
            "SpreadBps": spread,
            "SectorAvgChangePct": _float(current.get("SectorAvgChangePct")),
            "SectorBreadthPct": _float(current.get("SectorBreadthPct")),
            "SectorTop2Count": _float(current.get("SectorTop2Count")),
            "SectorTop5Count": _float(current.get("SectorTop5Count")),
            "MarketBreadthPct": _float(current.get("MarketBreadthPct")),
            "IsTopGainerNow": float(_truthy(current.get("IsTopGainerNow"))),
            "MissedRunnerCandidate": float(_truthy(current.get("MissedRunnerCandidate"))),
            "SnapshotSpanMinutes": elapsed,
            "MoveAccelerationPct": acceleration,
            "MoveVelocityPctPerMinute": acceleration / elapsed,
            "BuyerSellerSurge": bs / prior_bs if prior_bs > 0 else np.nan,
            "SectorRelativePct": move - _float(current.get("SectorAvgChangePct")),
            "band_0_5_1": 0.0,
            "band_1_2": 0.0,
            "band_2_3": 0.0,
            "band_3_5": 0.0,
        }
        row[band[3]] = 1.0
        return row

    def contract_entry_events(self) -> List[Dict[str, Any]]:
        """Expose score-qualified labels to the paper-only contract observer."""
        events = []
        model_hash = str(self.metadata.get("source_sha256", "") or "")
        for row in self.latest_predictions:
            score = _float(row.get("expected_return_pct"))
            ask = _float(row.get("entry_ask"))
            if score < self.threshold or ask <= 0:
                continue
            quantity = int(self.capital // ask)
            when = pd.Timestamp(row.get("when"))
            symbol = str(row.get("symbol", "") or "").strip().upper()
            move_band = str(row.get("move_band", "") or "")
            if quantity < 1 or not symbol or pd.isna(when):
                continue
            events.append(
                {
                    "event": "entry",
                    "EventId": (
                        f"{row.get('date')}:{symbol}:{move_band}:"
                        f"{model_hash[:16] or 'unknown-model'}"
                    ),
                    "Date": str(row.get("date", "") or ""),
                    "EntryAt": when.isoformat(),
                    "EntryTs": when.timestamp(),
                    "RecorderGeneration": int(
                        _float(row.get("recorder_generation"))
                    ),
                    "Exchange": str(
                        row.get("exchange", "") or ""
                    ).strip().upper(),
                    "Symbol": symbol,
                    "Cohort": f"score_ge_{self.threshold:.2f}",
                    "Source": "prior_session_frozen_ranker",
                    "EntryAsk": ask,
                    "EntryBid": _float(row.get("entry_bid")),
                    "Quantity": quantity,
                    "Capital": round(ask * quantity, 2),
                    "DayChangePct": _float(row.get("DayChangePct")),
                    "DayChangeRank": 0,
                    "RankImprovement": 0,
                    "RankVelocityPerMinute": 0.0,
                    "BuyerSellerRatio": _float(
                        row.get("BuyerSellerRatio")
                    ),
                    "EntrySpreadBps": _float(row.get("SpreadBps")),
                    "ExpectedReturnPct": score,
                    "MoveBand": move_band,
                    "ModelTrainedThrough": str(
                        row.get("model_trained_through", "") or ""
                    ),
                    "ModelSourceSha256": model_hash,
                }
            )
        return events

    def _selected_index(self, frame: pd.DataFrame) -> Optional[int]:
        if self._open_trade() is not None or len(self.trades) >= self.max_trades_per_day:
            return None
        eligible = frame[
            (frame["expected_return_pct"] >= self.threshold)
            & ~frame["symbol"].isin(self._traded_symbols)
        ]
        if eligible.empty:
            return None
        return int(eligible.sort_values(
            ["expected_return_pct", "symbol"], ascending=[False, True]
        ).index[0])

    def _rejection_reason(self, row: pd.Series) -> str:
        if float(row["expected_return_pct"]) < self.threshold:
            return "score_below_frozen_threshold"
        if str(row["symbol"]) in self._traded_symbols:
            return "symbol_already_traded_today"
        if self._open_trade() is not None:
            return "single_slot_occupied"
        if len(self.trades) >= self.max_trades_per_day:
            return "daily_trade_cap_reached"
        return "lower_rank_in_visible_batch"

    def _enter(self, candidate: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        price = _float(candidate.get("entry_ask"))
        qty = int(self.capital // price) if price > 0 else 0
        if qty < 1:
            return None
        trade = {
            "date": self._date,
            "symbol": str(candidate["symbol"]),
            "contract": CONTRACT,
            "signal_time": now.isoformat(timespec="seconds"),
            "entry_time": now.isoformat(timespec="seconds"),
            "exit_time": "",
            "expected_return_pct": round(_float(candidate.get("expected_return_pct")), 6),
            "entry_price": round(price, 4),
            "exit_price": 0.0,
            "qty": qty,
            "deployed_capital": round(price * qty, 2),
            "target_price": round(price * (1.0 + self.target_pct / 100.0), 4),
            "stop_price": round(price * (1.0 - self.stop_pct / 100.0), 4),
            "exit_reason": "",
            "gross_pnl": 0.0,
            "costs": 0.0,
            "net_pnl": 0.0,
            "net_return_pct": 0.0,
            "model_trained_through": self.metadata["trained_through"],
            "closed": False,
        }
        self.trades.append(trade)
        self._traded_symbols.add(trade["symbol"])
        return {"event": "paper_entry", **trade}

    def _update_open_trade(
        self, ticks: Dict[str, Dict[str, Any]], now: datetime
    ) -> List[Dict[str, Any]]:
        trade = self._open_trade()
        if trade is None:
            return []
        tick = ticks.get(str(trade["symbol"]), {}) if isinstance(ticks, dict) else {}
        bid = _float(tick.get("best_bid") or tick.get("bid"))
        if bid <= 0:
            depth = tick.get("depth") if isinstance(tick, dict) else None
            buy = depth.get("buy", []) if isinstance(depth, dict) else []
            bid = _float((buy[0] or {}).get("price")) if buy else 0.0
        if bid <= 0:
            return []
        age_minutes = (
            pd.Timestamp(now) - pd.Timestamp(trade["entry_time"])
        ).total_seconds() / 60.0
        minute = now.hour * 60 + now.minute
        reason = ""
        exit_price = bid
        if bid <= _float(trade["stop_price"]):
            reason = "stop"
        elif bid >= _float(trade["target_price"]):
            reason = "target"
            exit_price = _float(trade["target_price"])
        elif age_minutes >= self.horizon_minutes:
            reason = "horizon"
        elif minute >= 15 * 60 + 15:
            reason = "eod"
        if not reason:
            return []
        deployed = _float(trade["deployed_capital"])
        gross = (exit_price - _float(trade["entry_price"])) * int(trade["qty"])
        costs = deployed * self.fee_pct / 100.0
        net = gross - costs
        trade.update(
            {
                "exit_time": now.isoformat(timespec="seconds"),
                "exit_price": round(exit_price, 4),
                "exit_reason": reason,
                "gross_pnl": round(gross, 2),
                "costs": round(costs, 2),
                "net_pnl": round(net, 2),
                "net_return_pct": round(net / deployed * 100.0, 6),
                "closed": True,
            }
        )
        self._persist_trades()
        return [{"event": "paper_exit", **trade}]

    def _open_trade(self) -> Optional[Dict[str, Any]]:
        return next((trade for trade in self.trades if not trade.get("closed")), None)

    def _append_predictions(self, rows: List[Dict[str, Any]]) -> None:
        if not self.persist or not rows:
            return
        unique = []
        for row in rows:
            key = f"{row['date']}:{row['symbol']}:{row['move_band']}"
            if key in self._prediction_keys:
                continue
            self._prediction_keys.add(key)
            unique.append(row)
        if not unique:
            return
        path = self.output_dir / f"predictions_{self._date}.csv"
        pd.DataFrame(unique, columns=self.PREDICTION_COLUMNS).to_csv(
            path, mode="a", header=not path.exists(), index=False
        )

    def _persist_trades(self) -> None:
        if not self.persist:
            return
        frame = pd.DataFrame(self.trades, columns=self.TRADE_COLUMNS)
        frame.to_csv(self.output_dir / f"trades_{self._date}.csv", index=False)
        frame.to_csv(self.output_dir / "trades_latest.csv", index=False)
        status = {
            "status": "paper_only_live_predictions",
            "date": self._date,
            "contract": CONTRACT,
            "model_trained_through": self.metadata.get("trained_through"),
            "model_source_sha256": self.metadata.get("source_sha256"),
            "threshold": self.threshold,
            "capital": self.capital,
            "trades": len(self.trades),
            "closed_trades": sum(bool(row.get("closed")) for row in self.trades),
            "net_pnl": round(sum(_float(row.get("net_pnl")) for row in self.trades), 2),
            "paper_only": True,
            "real_orders_allowed": False,
        }
        (self.output_dir / "status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
