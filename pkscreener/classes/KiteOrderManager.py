"""
Kite Connect order placement and position management.

Places real orders via the official Kite Connect API,
tracks open positions, monitors P&L, and generates sell signals
based on stop-loss, target, and performance criteria.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from PKDevTools.classes import Archiver

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_secret(name: str, default: str = "") -> str:
    value = str(os.environ.get(name, default) or default).strip()
    if value:
        return value
    try:
        from PKDevTools.classes.Environment import PKEnvironment
        return str(PKEnvironment().allSecrets.get(name, default) or default).strip()
    except Exception:
        return str(default).strip()


def _secret_truthy(name: str, default: bool = False) -> bool:
    raw = _get_secret(name, "1" if default else "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _secret_float(name: str, default: float = 0.0) -> float:
    try:
        return float(_get_secret(name, str(default)) or default)
    except Exception:
        return float(default)


def _ist_now() -> datetime:
    try:
        import pytz
        return datetime.now(pytz.timezone("Asia/Kolkata"))
    except Exception:
        from datetime import timezone
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _is_market_open() -> bool:
    now = _ist_now()
    if now.weekday() >= 5:
        return False
    now_min = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= now_min <= 15 * 60 + 30


def _round_to_tick(price: float, tick_size: float = 0.05, *, direction: str = "up") -> float:
    """Round a price to the nearest valid tick.

    Defaults to ₹0.05, but some NSE/BSE names require ₹0.10, ₹0.50, etc.
    """
    if price <= 0:
        return 0.0
    tick = float(tick_size or 0.05)
    if tick <= 0:
        tick = 0.05
    scaled = price / tick
    if direction == "down":
        rounded = math.floor(scaled)
    elif direction == "nearest":
        rounded = round(scaled)
    else:
        rounded = math.ceil(scaled)
    decimals = max(2, len(f"{tick:.8f}".rstrip("0").split(".")[-1]))
    return round(rounded * tick, decimals)


def _normalize_gtt_trigger_id(value: Any) -> str:
    """Normalize client GTT responses to a plain trigger id string."""
    if isinstance(value, dict):
        for key in ("trigger_id", "id"):
            nested = value.get(key)
            if nested is not None and str(nested).strip():
                return str(nested).strip()
        return str(value).strip()
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Discord alert helper (module-level, deduplication via a daily set)
# ---------------------------------------------------------------------------

_discord_alerted_today: set = set()
_discord_alerted_date: str = ""


def _send_discord_alert(message: str, dedup_key: str = "") -> None:
    """Send one Discord webhook message. Skips duplicate keys within the same day."""
    global _discord_alerted_today, _discord_alerted_date
    today = date.today().isoformat()
    if today != _discord_alerted_date:
        _discord_alerted_today = set()
        _discord_alerted_date = today
    if dedup_key and dedup_key in _discord_alerted_today:
        return
    if dedup_key:
        _discord_alerted_today.add(dedup_key)
    webhook_url = _get_secret("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        import requests
        requests.post(webhook_url, json={"content": message[:1900]}, timeout=10)
    except Exception as exc:
        _log.debug("Discord alert failed: %s", exc)


# ---------------------------------------------------------------------------
# Order result dataclass-like dict
# ---------------------------------------------------------------------------

class OrderResult:
    """Lightweight container for order placement results."""

    __slots__ = ("success", "order_id", "symbol", "side", "qty", "price",
                 "status", "message", "timestamp")

    def __init__(self, success: bool, order_id: str = "", symbol: str = "",
                 side: str = "", qty: int = 0, price: float = 0.0,
                 status: str = "", message: str = ""):
        self.success = success
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.price = price
        self.status = status
        self.message = message
        self.timestamp = _ist_now().isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


# ---------------------------------------------------------------------------
# Sell signal reasons
# ---------------------------------------------------------------------------

class SellReason:
    STOP_LOSS = "stop_loss_hit"
    TARGET_1 = "target_1_hit"
    PARTIAL_T1 = "partial_t1_profit"  # sell 50% at T1, trail rest
    TARGET_2 = "target_2_hit"
    TRAILING_STOP = "trailing_stop_hit"
    TIME_DECAY = "held_too_long"
    EOD_SQUAREOFF = "eod_squareoff"
    REVERSAL = "intraday_reversal"
    REGIME_BREAK = "cusum_regime_break"
    FAILED_BREAKOUT = "failed_breakout_fast_exit"
    MANUAL = "manual_sell"
    STALE_TRACKED = "stale_tracked_position"
    # Swing-specific exits
    SWING_FAILED_STRUCTURE = "swing_failed_structure"  # broke entry level same day
    MOMENTUM_FADE = "momentum_fade"        # profit slipping + volume fading
    SECTOR_WEAKNESS = "sector_weakness"    # sector turned significantly weak
    MARKET_WEAKNESS = "market_weakness"    # broad market turned weak
    GAP_RISK_EXIT = "gap_risk_exit"        # gap-down broke thesis
    RUNNER_EXIT = "runner_exit"            # deliberate runner position closed
    BREAKEVEN_EXIT = "breakeven_exit"      # exited at breakeven to protect capital


# ---------------------------------------------------------------------------
# Position record (persisted to JSON)
# ---------------------------------------------------------------------------

class TrackedPosition:
    """A position tracked from buy to sell."""

    def __init__(self, symbol: str, qty: int, buy_price: float,
                 buy_order_id: str = "", stop_loss: float = 0.0,
                 target1: float = 0.0, target2: float = 0.0,
                 blind_score: float = 0.0, reasons: str = "",
                 product_type: str = "MIS", holding_horizon: str = "intraday",
                 max_hold_days_override: int = 0):
        self.symbol = symbol
        self.qty = qty
        self.buy_price = buy_price
        self.buy_order_id = buy_order_id
        self.stop_loss = stop_loss
        self.target1 = target1
        self.target2 = target2
        self.blind_score = blind_score
        self.reasons = reasons
        self.product_type = str(product_type or "MIS").upper()
        self.holding_horizon = str(holding_horizon or ("intraday" if str(product_type or "MIS").upper() == "MIS" else "swing"))
        self.max_hold_days_override = int(max_hold_days_override or 0)
        self.buy_time = _ist_now().isoformat(timespec="seconds")
        self.highest_price = buy_price
        self.trailing_stop = 0.0
        self.t1_booked = False  # True after 50% sold at T1
        self.sell_order_id = ""
        self.sell_price = 0.0
        self.sell_time = ""
        self.sell_reason = ""
        self.status = "open"  # open | pending_sell | closed
        self.gtt_trigger_id = ""
        self.gtt_status = ""
        self.gtt_expires_at = ""
        self.gtt_updated_at = ""
        self.gtt_last_error = ""
        self.sell_block_reason = ""
        self.sell_blocked_at = ""
        # Swing position lifecycle tracking (CNC/NRML only)
        self.swing_stage: str = "NEW"  # NEW/PROTECTED/CONFIRMED/PARTIAL_BOOKED/BREAKEVEN_PROTECTED/RUNNER/WEAKENING/EXITED
        self.initial_qty: int = qty    # original entry qty — never mutated
        self.gtt_needs_reissue: bool = False  # True after T1 partial; triggers cancel+reissue in audit

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrackedPosition":
        pos = cls(
            symbol=d["symbol"], qty=d["qty"], buy_price=d["buy_price"],
            buy_order_id=d.get("buy_order_id", ""),
            stop_loss=d.get("stop_loss", 0.0),
            target1=d.get("target1", 0.0),
            target2=d.get("target2", 0.0),
            blind_score=d.get("blind_score", 0.0),
            reasons=d.get("reasons", ""),
            product_type=d.get("product_type", "MIS"),
            holding_horizon=d.get("holding_horizon") or ("intraday" if d.get("product_type", "MIS") == "MIS" else "swing"),
            max_hold_days_override=d.get("max_hold_days_override", 0),
        )
        pos.buy_time = d.get("buy_time", pos.buy_time)
        pos.highest_price = d.get("highest_price", pos.buy_price)
        pos.trailing_stop = d.get("trailing_stop", 0.0)
        pos.t1_booked = d.get("t1_booked", False)
        pos.sell_order_id = d.get("sell_order_id", "")
        pos.sell_price = d.get("sell_price", 0.0)
        pos.sell_time = d.get("sell_time", "")
        pos.sell_reason = d.get("sell_reason", "")
        pos.status = d.get("status", "open")
        pos.gtt_trigger_id = str(d.get("gtt_trigger_id", "") or "")
        pos.gtt_status = str(d.get("gtt_status", "") or "")
        pos.gtt_expires_at = str(d.get("gtt_expires_at", "") or "")
        pos.gtt_updated_at = str(d.get("gtt_updated_at", "") or "")
        pos.gtt_last_error = str(d.get("gtt_last_error", "") or "")
        pos.sell_block_reason = str(d.get("sell_block_reason", "") or "")
        pos.sell_blocked_at = str(d.get("sell_blocked_at", "") or "")
        pos.swing_stage = str(d.get("swing_stage", "NEW") or "NEW")
        pos.initial_qty = int(d.get("initial_qty", pos.qty) or pos.qty)
        pos.gtt_needs_reissue = bool(d.get("gtt_needs_reissue", False))
        return pos

    @property
    def pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.qty if self.sell_price > 0 else 0.0

    @property
    def pnl_pct(self) -> float:
        if self.buy_price <= 0:
            return 0.0
        price = self.sell_price if self.sell_price > 0 else self.buy_price
        return ((price - self.buy_price) / self.buy_price) * 100


# ---------------------------------------------------------------------------
# Main Order Manager
# ---------------------------------------------------------------------------

class KiteOrderManager:
    """Places orders via Kite Connect and tracks positions with sell signals.

    Configuration via environment variables:
        KITE_AUTO_TRADE=1               Enable auto order placement
        KITE_MAX_ORDER_VALUE=5000       Max value per order (INR)
        KITE_MAX_DAILY_ORDERS=10        Max orders per day
        KITE_MAX_OPEN_POSITIONS=8       Max simultaneous open positions
        KITE_SWING_CAPITAL=65000        Total CNC/NRML capital bucket
        KITE_SWING_MAX_ORDER_VALUE=25000 Max CNC/NRML value per order
        KITE_SWING_MAX_OPEN_POSITIONS=3 Max CNC/NRML simultaneous positions
        KITE_INTRADAY_CAPITAL=25000     Total MIS notional bucket
        KITE_INTRADAY_MAX_ORDER_VALUE=15000 Max MIS notional per order
        KITE_INTRADAY_MAX_OPEN_POSITIONS=2 Max MIS simultaneous positions
        KITE_TIME_SLICED_CAPITAL=1      Reserve capital for later high-quality setups
        KITE_TRAILING_STOP_PCT=3.0      Intraday trailing stop activation (% above buy)
        KITE_TRAILING_STOP_TRAIL=1.0    Intraday trail distance (% below highest)
        KITE_SWING_TRAIL_ACTIVATE_PCT=5.0  Swing trail activates after this % gain (default 5%)
        KITE_SWING_TRAIL_DIST_PCT=4.0      Swing trail distance % below peak (default 4%)
        KITE_SWING_MIN_TARGET2_PCT=5.0  Minimum target2 for CNC orders (% above entry)
        KITE_SWING_MAX_TARGET2_PCT=20.0 Maximum target2 for CNC orders (% above entry)
        KITE_SWING_DEFAULT_SL_PCT=5.0   Default stop-loss % below entry if signal has none
        KITE_SWING_FAILED_MIN_AGE_SEC=900    Swing failed-structure: min seconds held before eligible (default 15 min)
        KITE_SWING_FAILED_MIN_DROP_PCT=1.5   Swing failed-structure: min % drop below entry to trigger (default 1.5%)
        KITE_MAX_HOLD_DAYS=10           Max days to hold losing position before time-decay exit
        KITE_DRY_RUN=0                  1=log orders without placing them
        KITE_PRODUCT_TYPE=MIS            MIS=intraday margin (5x leverage), CNC=delivery (full capital)
    """

    def __init__(self, output_dir: str = ""):
        self.output_dir = output_dir or Archiver.get_user_outputs_dir()
        self._positions_file = Path(self.output_dir) / "kite_tracked_positions.json"
        self._orders_log_file = Path(self.output_dir) / "kite_orders_log.csv"
        self._pending_buys_file = Path(self.output_dir) / "kite_pending_buys.json"
        self._lock = threading.Lock()

        # Config from env
        self.auto_trade = _get_secret("KITE_AUTO_TRADE", "0") == "1"
        self.dry_run = _get_secret("KITE_DRY_RUN", "0") == "1"
        self.max_order_value = float(_get_secret("KITE_MAX_ORDER_VALUE", "5000"))
        self.max_daily_orders = int(_get_secret("KITE_MAX_DAILY_ORDERS", "10"))
        self.max_open_positions = int(_get_secret("KITE_MAX_OPEN_POSITIONS", "8"))
        self.swing_capital = float(_get_secret("KITE_SWING_CAPITAL", "0") or 0)
        self.swing_max_order_value = float(
            _get_secret("KITE_SWING_MAX_ORDER_VALUE", str(self.max_order_value)) or self.max_order_value
        )
        self.swing_max_open_positions = int(
            _get_secret("KITE_SWING_MAX_OPEN_POSITIONS", str(self.max_open_positions)) or self.max_open_positions
        )
        self.intraday_capital = float(_get_secret("KITE_INTRADAY_CAPITAL", "0") or 0)
        self.intraday_max_order_value = float(
            _get_secret("KITE_INTRADAY_MAX_ORDER_VALUE", str(self.max_order_value)) or self.max_order_value
        )
        self.intraday_max_open_positions = int(
            _get_secret("KITE_INTRADAY_MAX_OPEN_POSITIONS", str(self.max_open_positions)) or self.max_open_positions
        )
        self.time_sliced_capital = _secret_truthy("KITE_TIME_SLICED_CAPITAL", True)
        self.order_fill_wait_seconds = max(0.0, _secret_float("KITE_ORDER_FILL_WAIT_SEC", 4.0))
        self.cancel_unfilled_limit_orders = _secret_truthy("KITE_CANCEL_UNFILLED_LIMIT", True)
        self.max_buy_price_stale_drop_pct = max(0.0, _secret_float("KITE_MAX_BUY_PRICE_STALE_DROP_PCT", 1.0))
        self.confirm_stop_loss_with_broker = _secret_truthy("KITE_CONFIRM_STOP_LOSS_WITH_BROKER", True)
        self.trailing_stop_pct = float(_get_secret("KITE_TRAILING_STOP_PCT", "3.0"))
        self.trailing_trail_pct = float(_get_secret("KITE_TRAILING_STOP_TRAIL", "1.0"))
        self.swing_trail_activate_pct = float(_get_secret("KITE_SWING_TRAIL_ACTIVATE_PCT", "5.0"))
        self.swing_trail_dist_pct = float(_get_secret("KITE_SWING_TRAIL_DIST_PCT", "4.0"))
        # Swing failed-structure exit: if a CNC/NRML trade breaks out and then
        # immediately falls back through entry on the same calendar day, exit
        # rather than waiting for the full stop. Set either to 0 to disable.
        self.swing_failed_min_age_sec = max(0.0, _secret_float("KITE_SWING_FAILED_MIN_AGE_SEC", 900.0))
        self.swing_failed_min_drop_pct = max(0.0, _secret_float("KITE_SWING_FAILED_MIN_DROP_PCT", 1.5))
        # Failed-breakout fast exit (intraday only). Applies inside a narrow
        # window after entry: if the trade lost VWAP, is below entry by a clear
        # margin, and the order book shows real sell pressure, exit before the
        # full stop-loss is reached. Disabled by setting any threshold to 0.
        self.failed_breakout_min_age_sec = max(0.0, _secret_float("KITE_FAILED_BREAKOUT_MIN_AGE_SEC", 180.0))
        self.failed_breakout_max_age_sec = max(0.0, _secret_float("KITE_FAILED_BREAKOUT_MAX_AGE_SEC", 1800.0))
        self.failed_breakout_min_drop_pct = max(0.0, _secret_float("KITE_FAILED_BREAKOUT_MIN_DROP_PCT", 0.4))
        self.failed_breakout_sell_imbalance = max(1.0, _secret_float("KITE_FAILED_BREAKOUT_SELL_IMBALANCE", 1.3))
        self.max_hold_days = int(_get_secret("KITE_MAX_HOLD_DAYS", "10"))
        # Max positions from the same sector (0 = disabled)
        self.max_sector_concentration = int(_get_secret("KITE_MAX_SECTOR_POSITIONS", "3"))
        # MIS = intraday margin (5x leverage, auto-squared off at 3:20 PM by broker)
        # CNC = delivery (full capital, no auto-squareoff)
        _product = _get_secret("KITE_PRODUCT_TYPE", "MIS").strip().upper()
        self.product_type = _product if _product in ("MIS", "CNC", "NRML") else "MIS"

        self._positions: Dict[str, TrackedPosition] = {}
        self._pending_buys: Dict[str, Dict[str, Any]] = {}
        self._daily_order_count = 0
        self._daily_order_date = ""
        self._client = None
        self._instrument_cache: Dict[str, Dict[str, Any]] = {}
        self._instrument_cache_loaded = False
        self._dead_symbols: set = set()  # symbols that failed with "expired/does not exist" today
        self._load_positions()
        self._load_pending_buys()

    # ------------------------------------------------------------------
    # Kite client
    # ------------------------------------------------------------------

    def _get_client(self):
        """Get or create authenticated KiteConnect client."""
        if self._client is not None:
            return self._client
        from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager
        mgr = KiteConnectSessionManager()
        mgr.ensure_fresh_session()
        self._client = mgr._build_client()
        return self._client

    def _load_instrument_cache(self) -> None:
        if self._instrument_cache_loaded:
            return
        try:
            client = self._get_client()
            combined: Dict[str, Dict[str, Any]] = {}
            for exchange in ("NSE", "BSE"):
                try:
                    instruments = client.instruments(exchange) or []
                except Exception as exc:
                    _log.debug("Instrument dump failed for %s: %s", exchange, exc)
                    continue
                for row in instruments:
                    sym = str((row or {}).get("tradingsymbol", "") or "").strip().upper()
                    if not sym:
                        continue
                    tick_size = float((row or {}).get("tick_size", 0.05) or 0.05)
                    entry = {
                        "exchange": exchange,
                        "tick_size": tick_size,
                    }
                    current = combined.get(sym)
                    if current is None or current.get("exchange") != "NSE":
                        combined[sym] = entry
            if combined:
                self._instrument_cache = combined
                self._instrument_cache_loaded = True
        except Exception as exc:
            _log.debug("Instrument cache load failed: %s", exc)

    def _resolve_exchange_and_tick_size(self, symbol: str) -> Tuple[str, float]:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return "NSE", 0.05
        self._load_instrument_cache()
        meta = self._instrument_cache.get(symbol) or {}
        exchange = str(meta.get("exchange", "NSE") or "NSE").upper()
        tick_size = float(meta.get("tick_size", 0.05) or 0.05)
        return exchange, tick_size

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_positions(self):
        try:
            if self._positions_file.exists():
                data = json.loads(self._positions_file.read_text(encoding="utf-8"))
                self._positions = {
                    k: TrackedPosition.from_dict(v) for k, v in data.items()
                }
                _log.info("Loaded %d tracked positions", len(self._positions))
        except Exception as exc:
            _log.warning("Failed to load positions: %s", exc)

    def _save_positions(self):
        try:
            self._positions_file.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.to_dict() for k, v in self._positions.items()}
            self._positions_file.write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:
            _log.warning("Failed to save positions: %s", exc)

    def update_position_gtt(
        self,
        symbol: str,
        *,
        trigger_id: str = "",
        status: str = "",
        expires_at: str = "",
        error: str = "",
    ) -> None:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None:
                return
            if trigger_id:
                pos.gtt_trigger_id = str(trigger_id)
            if status:
                pos.gtt_status = str(status)
            if expires_at:
                pos.gtt_expires_at = str(expires_at)
            if error or status:
                pos.gtt_last_error = str(error or "")
            pos.gtt_updated_at = _ist_now().isoformat(timespec="seconds")
            self._save_positions()

    def _load_pending_buys(self):
        try:
            if self._pending_buys_file.exists():
                self._pending_buys = json.loads(
                    self._pending_buys_file.read_text(encoding="utf-8")
                )
        except Exception as exc:
            _log.warning("Failed to load pending buys: %s", exc)
            self._pending_buys = {}

    def _save_pending_buys(self):
        try:
            self._pending_buys_file.parent.mkdir(parents=True, exist_ok=True)
            self._pending_buys_file.write_text(
                json.dumps(self._pending_buys, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:
            _log.warning("Failed to save pending buys: %s", exc)

    def _log_order(self, result: OrderResult):
        """Append order to CSV log."""
        try:
            row = result.to_dict()
            df = pd.DataFrame([row])
            header = not self._orders_log_file.exists()
            df.to_csv(self._orders_log_file, mode="a", header=header, index=False)
        except Exception as exc:
            _log.warning("Failed to log order: %s", exc)

    def _return_logged_result(self, result: OrderResult) -> OrderResult:
        """Log and return a non-position-mutating order result."""
        self._log_order(result)
        return result

    def _order_status_from_history(self, order_id: str, history: Any) -> Dict[str, Any]:
        """Normalize Kite order history into the fields we need for truth logs."""
        latest = history[-1] if isinstance(history, list) and history else {}
        filled_qty = int(latest.get("filled_quantity", 0) or 0)
        avg_price = float(latest.get("average_price", 0) or 0)
        status = str(latest.get("status", "UNKNOWN") or "UNKNOWN").upper()
        message = str(
            latest.get("status_message")
            or latest.get("status_message_raw")
            or latest.get("order_status")
            or ""
        ).strip()
        return {
            "order_id": str(order_id or ""),
            "status": status,
            "filled_qty": filled_qty,
            "avg_price": avg_price,
            "pending_qty": int(latest.get("pending_quantity", 0) or 0),
            "message": message,
        }

    def _wait_for_order_status(self, client: Any, order_id: str) -> Dict[str, Any]:
        """Poll Kite briefly so PLACED does not masquerade as FILLED."""
        deadline = time.time() + self.order_fill_wait_seconds
        status: Dict[str, Any] = {"order_id": str(order_id or ""), "status": "UNKNOWN"}
        while True:
            try:
                status = self._order_status_from_history(order_id, client.order_history(order_id))
            except Exception as exc:
                status = {"order_id": str(order_id or ""), "status": "ERROR", "message": str(exc)}
            if status.get("status") in {"COMPLETE", "REJECTED", "CANCELLED"}:
                return status
            if time.time() >= deadline:
                return status
            time.sleep(0.5)

    def _cancel_order_best_effort(self, client: Any, order_id: str) -> bool:
        """Cancel an unfilled regular order without hiding failures."""
        try:
            client.cancel_order(variety="regular", order_id=order_id)
            return True
        except Exception as exc:
            _log.warning("Could not cancel unfilled order %s: %s", order_id, exc)
            return False

    def _looks_like_funds_shortage(self, message: str) -> bool:
        text = str(message or "").lower()
        patterns = (
            "insufficient funds",
            "insufficient margin",
            "margin",
            "rms",
            "available margin",
            "available cash",
            "funds are insufficient",
            "not enough balance",
            "insufficient balance",
        )
        return any(p in text for p in patterns)

    def _looks_like_cdsl_authorization_required(self, message: str) -> bool:
        text = str(message or "").lower()
        patterns = (
            "cdsl",
            "authorise shares",
            "authorize shares",
            "authorised at cdsl",
            "authorized at cdsl",
            "demat depository",
            "edis",
            "tpin",
        )
        return any(pattern in text for pattern in patterns)

    def _mark_cdsl_authorization_required(self, symbol: str, message: str = "") -> None:
        symbol = str(symbol or "").strip().upper()
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return
            pos.sell_block_reason = "CDSL_AUTH_REQUIRED"
            pos.sell_blocked_at = _ist_now().isoformat(timespec="seconds")
            pos.gtt_last_error = str(message or "CDSL authorisation required before selling CNC/delivery shares.")
            self._save_positions()

    def _looks_like_sell_temporarily_blocked(self, message: str) -> bool:
        text = str(message or "").lower()
        patterns = (
            "lower circuit limit",
            "periodic call auction",
            "place your orders in this window",
            "traded in periodic call auction",
        )
        return any(pattern in text for pattern in patterns)

    def _mark_sell_temporarily_blocked(self, symbol: str, message: str = "") -> None:
        symbol = str(symbol or "").strip().upper()
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return
            pos.sell_block_reason = "TEMPORARY_MARKET_BLOCK"
            pos.sell_blocked_at = _ist_now().isoformat(timespec="seconds")
            pos.gtt_last_error = str(message or "Sell blocked by exchange trading rules or circuit limits.")
            self._save_positions()

    def clear_sell_block(self, symbol: str) -> None:
        """Clear a manual sell block after the user has handled broker-side auth."""
        symbol = str(symbol or "").strip().upper()
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return
            pos.sell_block_reason = ""
            pos.sell_blocked_at = ""
            self._save_positions()

    def _extract_available_funds(self) -> float:
        try:
            client = self._get_client()
            margins = client.margins() or {}
            equity = margins.get("equity", {}) if isinstance(margins, dict) else {}
            available = equity.get("available", {}) if isinstance(equity, dict) else {}
            keys = (
                "live_balance",
                "cash",
                "opening_balance",
                "adhoc_margin",
                "collateral",
                "utilised_debits",
                "net",
            )
            for key in keys:
                try:
                    value = float(available.get(key, 0) or 0)
                    if value > 0:
                        return value
                except Exception:
                    continue
            # Last fallback: equity net if available
            try:
                value = float(equity.get("net", 0) or 0)
                if value > 0:
                    return value
            except Exception:
                pass
        except Exception as exc:
            _log.debug("Funds lookup failed: %s", exc)
        return 0.0

    def _latest_tick_price(self, symbol: str, max_stale_seconds: int = 300) -> float:
        """Best-effort live price lookup from the saved tick snapshot.

        Returns 0.0 if the tick for this symbol is older than max_stale_seconds
        (default 5 min) — caller should fall back to broker quote in that case.
        """
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return 0.0
        try:
            ticks = self._load_ticks()
            tick = ticks.get(symbol, {}) if isinstance(ticks, dict) else {}
            price = float(tick.get("last_price") or 0.0)
            if price <= 0:
                return 0.0
            # Reject ticks that are too old — stale file from yesterday causes
            # false "price faded" blocks (e.g. previous close ₹686 vs live ₹745).
            received_at_str = tick.get("received_at") or ""
            if received_at_str and max_stale_seconds > 0:
                try:
                    received_dt = datetime.fromisoformat(received_at_str)
                    age_seconds = (datetime.now() - received_dt).total_seconds()
                    if age_seconds > max_stale_seconds:
                        _log.debug(
                            "%s tick is %.0fs stale (received_at=%s); ignoring",
                            symbol, age_seconds, received_at_str,
                        )
                        return 0.0
                except Exception:
                    pass
            return price
        except Exception:
            return 0.0

    def _broker_last_price(self, symbol: str) -> float:
        """Best-effort fresh broker LTP for safety-critical exits."""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return 0.0
        try:
            client = self._get_client()
            exchange, _tick_size = self._resolve_exchange_and_tick_size(symbol)
            key = f"{exchange}:{symbol}"
            quote = client.quote(key) or {}
            data = quote.get(key, {}) if isinstance(quote, dict) else {}
            return float(data.get("last_price") or 0.0)
        except Exception as exc:
            _log.debug("Broker LTP lookup failed for %s: %s", symbol, exc)
            return 0.0

    def _current_open_delivery_value(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            if pos.status != "open":
                continue
            pos_product = str(getattr(pos, "product_type", self.product_type) or self.product_type).upper()
            if pos_product in ("CNC", "NRML"):
                total += float(getattr(pos, "buy_price", 0.0) or 0.0) * int(getattr(pos, "qty", 0) or 0)
        return round(total, 2)

    def _normalize_product_type(self, product_type: str = "") -> str:
        normalized = str(product_type or self.product_type or "MIS").upper()
        if normalized not in ("MIS", "CNC", "NRML"):
            normalized = self.product_type
        return normalized

    def _capital_bucket_for_product(self, product_type: str = "") -> str:
        return "intraday" if self._normalize_product_type(product_type) == "MIS" else "swing"

    def _capital_limit_for_product(self, product_type: str = "") -> float:
        return self.intraday_capital if self._capital_bucket_for_product(product_type) == "intraday" else self.swing_capital

    def _capital_limit_available_now_for_product(self, product_type: str = "") -> float:
        """Return the capital bucket currently available to new entries.

        The full bucket is intentionally not released at the open. This prevents
        mediocre early alerts from consuming all buying power before cleaner
        9:30-10:30 continuation setups appear.
        """
        capital_limit = self._capital_limit_for_product(product_type)
        if capital_limit <= 0 or not self.time_sliced_capital:
            return capital_limit

        now = _ist_now()
        now_min = now.hour * 60 + now.minute
        bucket = self._capital_bucket_for_product(product_type)

        if bucket == "swing":
            if now_min < 9 * 60 + 25:
                fraction = 0.40
            elif now_min < 9 * 60 + 45:
                fraction = 0.70
            elif now_min < 10 * 60 + 30:
                fraction = 0.85
            else:
                fraction = 1.00
        else:
            if now_min < 9 * 60 + 25:
                fraction = 0.50
            elif now_min < 9 * 60 + 45:
                fraction = 0.80
            elif now_min < 10 * 60 + 15:
                fraction = 0.90
            else:
                fraction = 1.00

        return round(capital_limit * fraction, 2)

    def _per_order_limit_for_product(self, product_type: str = "") -> float:
        return (
            self.intraday_max_order_value
            if self._capital_bucket_for_product(product_type) == "intraday"
            else self.swing_max_order_value
        )

    def _max_positions_for_product(self, product_type: str = "") -> int:
        return (
            self.intraday_max_open_positions
            if self._capital_bucket_for_product(product_type) == "intraday"
            else self.swing_max_open_positions
        )

    def _current_open_value_for_product(self, product_type: str = "") -> float:
        target_bucket = self._capital_bucket_for_product(product_type)
        total = 0.0
        for pos in self._positions.values():
            if pos.status != "open":
                continue
            pos_product = self._normalize_product_type(getattr(pos, "product_type", self.product_type))
            if self._capital_bucket_for_product(pos_product) != target_bucket:
                continue
            total += float(getattr(pos, "buy_price", 0.0) or 0.0) * int(getattr(pos, "qty", 0) or 0)
        return round(total, 2)

    def _current_open_count_for_product(self, product_type: str = "") -> int:
        target_bucket = self._capital_bucket_for_product(product_type)
        count = 0
        for pos in self._positions.values():
            if pos.status != "open":
                continue
            pos_product = self._normalize_product_type(getattr(pos, "product_type", self.product_type))
            if self._capital_bucket_for_product(pos_product) == target_bucket:
                count += 1
        return count

    def _remaining_capital_for_product(self, product_type: str = "") -> float:
        capital_limit = self._capital_limit_available_now_for_product(product_type)
        if capital_limit <= 0:
            return 0.0
        deployed = self._current_open_value_for_product(product_type)
        return round(max(0.0, capital_limit - deployed), 2)

    def estimate_affordable_qty(
        self,
        price: float,
        desired_qty: int,
        product_type: str = "",
    ) -> Tuple[int, float]:
        """Estimate a broker-affordable quantity for the given product type.

        For CNC/NRML we cap quantity to effective free cash after open delivery
        exposure. For MIS we keep the desired quantity unchanged because the
        actual broker margin varies by instrument and leverage policy.
        """
        try:
            price = float(price or 0.0)
        except Exception:
            price = 0.0
        try:
            desired_qty = int(desired_qty or 0)
        except Exception:
            desired_qty = 0
        if price <= 0 or desired_qty <= 0:
            return 0, 0.0

        order_product = self._normalize_product_type(product_type)
        per_order_limit = self._per_order_limit_for_product(order_product)
        if per_order_limit > 0:
            per_order_qty = int(per_order_limit // price) if price > 0 else 0
            desired_qty = min(desired_qty, per_order_qty) if per_order_qty > 0 else 0
        if desired_qty <= 0:
            return 0, 0.0

        capital_limit = self._capital_limit_for_product(order_product)
        remaining_pool = self._remaining_capital_for_product(order_product)
        if capital_limit > 0:
            pool_qty = int(remaining_pool // price) if remaining_pool > 0 else 0
            desired_qty = min(desired_qty, pool_qty) if pool_qty > 0 else 0
            if desired_qty <= 0:
                return 0, 0.0
        if order_product == "MIS":
            return desired_qty, round(desired_qty * price, 0)

        available_cash = self._extract_available_funds()
        if available_cash <= 0:
            return desired_qty, round(desired_qty * price, 0)

        open_delivery_value = self._current_open_delivery_value()
        if open_delivery_value > 0 and available_cash >= open_delivery_value:
            free_delivery_cash = max(0.0, available_cash - open_delivery_value)
        else:
            free_delivery_cash = available_cash
        if capital_limit > 0:
            free_delivery_cash = min(free_delivery_cash, remaining_pool)
        affordable_qty = int(free_delivery_cash // price) if free_delivery_cash > 0 else 0
        capped_qty = min(desired_qty, affordable_qty) if affordable_qty > 0 else 0
        return capped_qty, round(capped_qty * price, 0)

    def _queue_pending_buy(
        self,
        symbol: str,
        qty: int,
        live_price: float,
        stop_loss: float = 0.0,
        target1: float = 0.0,
        target2: float = 0.0,
        blind_score: float = 0.0,
        reasons: str = "",
        product_type: str = "",
        holding_horizon: str = "",
        max_hold_days: int = 0,
        error_message: str = "",
        retry_minutes: int = 20,
    ) -> None:
        now = _ist_now()
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "live_price": float(live_price),
            "stop_loss": float(stop_loss or 0),
            "target1": float(target1 or 0),
            "target2": float(target2 or 0),
            "blind_score": float(blind_score or 0),
            "reasons": str(reasons or ""),
            "product_type": str(product_type or ""),
            "holding_horizon": str(holding_horizon or ""),
            "max_hold_days": int(max_hold_days or 0),
            "last_error": str(error_message or ""),
            "queued_at": now.isoformat(timespec="seconds"),
            "retry_after": now.isoformat(timespec="seconds"),
            "retry_until": (now + timedelta(minutes=retry_minutes)).isoformat(timespec="seconds"),
            "attempts": int(self._pending_buys.get(symbol, {}).get("attempts", 0) or 0),
        }
        self._pending_buys[symbol] = payload
        self._save_pending_buys()

    def _drop_pending_buy(self, symbol: str) -> None:
        if symbol in self._pending_buys:
            self._pending_buys.pop(symbol, None)
            self._save_pending_buys()

    def retry_pending_buys(self, max_per_cycle: int = 2) -> List[OrderResult]:
        results: List[OrderResult] = []
        if not self._pending_buys or not _is_market_open():
            return results
        now = _ist_now()
        symbols = sorted(self._pending_buys.keys())
        for symbol in symbols:
            if len(results) >= max_per_cycle:
                break
            item = self._pending_buys.get(symbol) or {}
            try:
                retry_until = datetime.fromisoformat(str(item.get("retry_until", "")))
            except Exception:
                retry_until = now + timedelta(minutes=10)
            try:
                retry_after = datetime.fromisoformat(str(item.get("retry_after", "")))
            except Exception:
                retry_after = now
            if now < retry_after:
                continue
            if now > retry_until:
                result = self._return_logged_result(
                    OrderResult(
                        False,
                        symbol=symbol,
                        side="BUY",
                        qty=int(item.get("qty", 0) or 0),
                        price=float(item.get("live_price", 0) or 0),
                        status="EXPIRED",
                        message="Pending auto-buy expired before funds became available",
                    )
                )
                self._drop_pending_buy(symbol)
                results.append(result)
                continue

            live_price = float(item.get("live_price", 0) or 0)
            qty = int(item.get("qty", 0) or 0)
            result = self.place_buy_order(
                symbol=symbol,
                qty=qty,
                price=live_price,
                stop_loss=float(item.get("stop_loss", 0) or 0),
                target1=float(item.get("target1", 0) or 0),
                target2=float(item.get("target2", 0) or 0),
                blind_score=float(item.get("blind_score", 0) or 0),
                reasons=str(item.get("reasons", "")),
                order_type="MARKET",
                product_type_override=str(item.get("product_type", "") or ""),
                holding_horizon=str(item.get("holding_horizon", "") or ""),
                max_hold_days_override=int(item.get("max_hold_days", 0) or 0),
            )
            if result.success:
                self._drop_pending_buy(symbol)
            elif self._looks_like_funds_shortage(result.message):
                item["attempts"] = int(item.get("attempts", 0) or 0) + 1
                item["retry_after"] = (now + timedelta(seconds=45)).isoformat(timespec="seconds")
                self._pending_buys[symbol] = item
                self._save_pending_buys()
                result.status = "FUNDS_SHORT"
                if live_price > 0:
                    needed = max(0.0, qty * live_price - self._extract_available_funds())
                    if needed > 0:
                        result.message = f"Still short on funds/margin. Add about ₹{needed:,.0f}; retry queued automatically."
                    else:
                        result.message = "Still short on funds/margin; retry queued automatically."
            else:
                self._drop_pending_buy(symbol)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Daily order count
    # ------------------------------------------------------------------

    def _check_daily_limit(self) -> bool:
        today = _ist_now().strftime("%Y-%m-%d")
        if self._daily_order_date != today:
            self._daily_order_date = today
            self._daily_order_count = 0
        return self._daily_order_count < self.max_daily_orders

    # ------------------------------------------------------------------
    # Circuit limit pre-check (adapted from sdoosa-algo-trade-python)
    # ------------------------------------------------------------------

    def _is_at_circuit_limit(self, symbol: str) -> Tuple[bool, str]:
        """Check if a stock is at upper/lower circuit limit via Kite quotes.

        Returns (is_blocked, reason). Buying at upper circuit is pointless
        (can't fill), selling at lower circuit is pointless.
        """
        try:
            client = self._get_client()
            exchange, _tick_size = self._resolve_exchange_and_tick_size(symbol)
            key = f"{exchange}:{symbol}"
            quote = client.quote(key)
            data = quote.get(key, {})
            if not data:
                return False, ""
            ltp = float(data.get("last_price", 0))
            upper = float(data.get("ohlc", {}).get("high", 0))  # day high
            lower = float(data.get("ohlc", {}).get("low", 0))   # day low
            upper_circuit = float(data.get("upper_circuit_limit", 0))
            lower_circuit = float(data.get("lower_circuit_limit", 0))
            if upper_circuit > 0 and ltp >= upper_circuit * 0.999:
                return True, f"at upper circuit ₹{upper_circuit:.2f}"
            if lower_circuit > 0 and ltp <= lower_circuit * 1.001:
                return True, f"at lower circuit ₹{lower_circuit:.2f}"
            return False, ""
        except Exception as exc:
            _log.debug("Circuit check failed for %s: %s", symbol, exc)
            return False, ""

    # ------------------------------------------------------------------
    # Portfolio sector concentration guard (from pysystemtrade IDM concept)
    # ------------------------------------------------------------------

    def _check_sector_concentration(self, symbol: str) -> Tuple[bool, str]:
        """Block buy if too many open positions are in the same sector.

        Prevents portfolio blow-up when a whole sector reverses.
        Returns (is_blocked, reason).
        """
        if self.max_sector_concentration <= 0:
            return False, ""
        try:
            from pkscreener.classes.MarketContext import get_sector
            new_sector = get_sector(symbol)
            if not new_sector or new_sector == "UNKNOWN":
                return False, ""
            sector_count = 0
            for pos in self._positions.values():
                if pos.status == "open":
                    pos_sector = get_sector(pos.symbol)
                    if pos_sector == new_sector:
                        sector_count += 1
            if sector_count >= self.max_sector_concentration:
                return True, (
                    f"sector concentration: already {sector_count} open "
                    f"positions in {new_sector} (max {self.max_sector_concentration})"
                )
        except Exception as exc:
            _log.debug("Sector check failed for %s: %s", symbol, exc)
        return False, ""

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_buy_order(
        self,
        symbol: str,
        qty: int,
        price: float = 0.0,
        stop_loss: float = 0.0,
        target1: float = 0.0,
        target2: float = 0.0,
        blind_score: float = 0.0,
        reasons: str = "",
        order_type: str = "MARKET",
        product_type_override: str = "",
        holding_horizon: str = "",
        max_hold_days_override: int = 0,
    ) -> OrderResult:
        """Place a buy order on NSE via Kite Connect.

        Args:
            symbol: Trading symbol (e.g., RELIANCE)
            qty: Number of shares
            price: Limit price (0 = market order)
            stop_loss: Stop loss level for position tracking
            target1: First profit target
            target2: Max profit target
            blind_score: Score from BlindBuyScorer
            reasons: Why this stock was picked
            order_type: MARKET or LIMIT
        """
        symbol = str(symbol).strip().upper()
        order_product = self._normalize_product_type(product_type_override)

        if symbol in self._dead_symbols:
            return self._return_logged_result(
                OrderResult(
                    False, symbol=symbol, side="BUY", qty=qty, price=price,
                    status="SKIPPED",
                    message=f"Skipped {symbol}: instrument marked dead/expired for today",
                )
            )

        # Safety checks
        if not self.auto_trade:
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="DISABLED",
                    message="Auto-trade is disabled (set KITE_AUTO_TRADE=1)",
                )
            )

        if not _is_market_open():
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message="Market is closed",
                )
            )

        if not self._check_daily_limit():
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message=f"Daily order limit reached ({self.max_daily_orders})",
                )
            )

        bucket_open_count = self._current_open_count_for_product(order_product)
        bucket_max_positions = self._max_positions_for_product(order_product)
        if bucket_max_positions > 0 and bucket_open_count >= bucket_max_positions:
            bucket_label = "intraday" if self._capital_bucket_for_product(order_product) == "intraday" else "swing"
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message=f"Max {bucket_label} positions reached ({bucket_max_positions})",
                )
            )

        if symbol in self._positions and self._positions[symbol].status == "open":
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message="Already holding this stock",
                )
            )

        configured_capital_limit = self._capital_limit_for_product(order_product)
        capital_limit = self._capital_limit_available_now_for_product(order_product)
        remaining_pool = self._remaining_capital_for_product(order_product)
        if configured_capital_limit > 0 and price > 0:
            affordable_qty_by_pool = int(remaining_pool // price) if remaining_pool > 0 else 0
            if affordable_qty_by_pool <= 0:
                bucket_label = "intraday" if self._capital_bucket_for_product(order_product) == "intraday" else "swing"
                return self._return_logged_result(
                    OrderResult(
                        False,
                        symbol=symbol,
                        side="BUY",
                        qty=qty,
                        price=price,
                        status="SKIPPED",
                        message=(
                            f"{bucket_label.title()} capital pool reached. "
                            f"Available now ₹{capital_limit:,.0f} of configured ₹{configured_capital_limit:,.0f}, "
                            f"already deployed ₹{self._current_open_value_for_product(order_product):,.0f}."
                        ),
                    )
                )
            if affordable_qty_by_pool < qty:
                _log.info(
                    "%s capital pool cap: reducing %s qty from %d to %d (remaining_pool=₹%.2f, price=₹%.2f)",
                    self._capital_bucket_for_product(order_product),
                    symbol,
                    qty,
                    affordable_qty_by_pool,
                    remaining_pool,
                    price,
                )
                qty = affordable_qty_by_pool

        # For CNC/NRML entries, size against actual Kite idle/available cash.
        # Kite's equity.available.live_balance is the LIVE balance net of
        # already-utilised funds (delivery, MIS, etc.) — trust it directly.
        # The previous heuristic that subtracted our tracked position value was
        # double-counting and starved the bot when broker cash was healthy.
        if order_product in ("CNC", "NRML") and price > 0:
            available_cash = self._extract_available_funds()
            if available_cash > 0:
                free_delivery_cash = available_cash
                if configured_capital_limit > 0:
                    free_delivery_cash = min(free_delivery_cash, remaining_pool)
                affordable_qty = int(free_delivery_cash // price) if free_delivery_cash > 0 else 0
                if affordable_qty <= 0:
                    return self._return_logged_result(
                        OrderResult(
                            False,
                            symbol=symbol,
                            side="BUY",
                            qty=qty,
                            price=price,
                            status="FUNDS_SHORT",
                            message=(
                                f"Insufficient idle cash in Kite for this delivery buy. "
                                f"Available ₹{free_delivery_cash:,.0f} < price ₹{price:,.0f}."
                            ),
                        )
                    )
                if affordable_qty < qty:
                    _log.info(
                        "CNC cash cap: reducing %s qty from %d to %d "
                        "(available_cash=₹%.2f, price=₹%.2f)",
                        symbol,
                        qty,
                        affordable_qty,
                        available_cash,
                        price,
                    )
                    qty = affordable_qty

        per_order_limit = self._per_order_limit_for_product(order_product)
        if price > 0 and per_order_limit > 0:
            affordable_qty_by_order = int(per_order_limit // price) if per_order_limit > 0 else 0
            if affordable_qty_by_order <= 0:
                return self._return_logged_result(
                    OrderResult(
                        False,
                        symbol=symbol,
                        side="BUY",
                        qty=qty,
                        price=price,
                        status="SKIPPED",
                        message=f"Starter position exceeds {self._capital_bucket_for_product(order_product)} per-trade cap ₹{per_order_limit:,.0f}",
                    )
                )
            if affordable_qty_by_order < qty:
                qty = affordable_qty_by_order

        # Circuit limit check — avoid orders on locked stocks
        circuit_blocked, circuit_reason = self._is_at_circuit_limit(symbol)
        if circuit_blocked:
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message=f"Circuit limit: {circuit_reason}",
                )
            )

        # Sector concentration check — avoid correlated positions
        sector_blocked, sector_reason = self._check_sector_concentration(symbol)
        if sector_blocked:
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message=f"Blocked: {sector_reason}",
                )
            )

        order_value = qty * (price if price > 0 else 0)
        if price > 0 and order_value > per_order_limit * 1.1:
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message=(
                        f"Order value ₹{order_value:,.0f} exceeds "
                        f"{self._capital_bucket_for_product(order_product)} limit ₹{per_order_limit:,.0f}"
                    ),
                )
            )

        if qty <= 0:
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    price=price,
                    status="FAILED",
                    message="Invalid quantity",
                )
            )

        latest_price = self._latest_tick_price(symbol) if price > 0 else 0.0
        if price > 0 and self.max_buy_price_stale_drop_pct > 0:
            _is_cnc = order_product in ("CNC", "NRML")
            _stale_threshold = self.max_buy_price_stale_drop_pct * 3 if _is_cnc else self.max_buy_price_stale_drop_pct

            # If tick is missing/stale, confirm via broker quote before proceeding
            if latest_price <= 0:
                latest_price = self._broker_last_price(symbol)
                if latest_price > 0:
                    _log.info(
                        "%s tick stale/missing — confirmed via broker quote ₹%.2f",
                        symbol, latest_price,
                    )

            if latest_price > 0:
                stale_drop_pct = ((price - latest_price) / price) * 100
                # If tick shows a large drop, confirm with broker before blocking —
                # stale ticks can show yesterday's close and falsely block valid buys.
                if stale_drop_pct > _stale_threshold:
                    broker_price = self._broker_last_price(symbol)
                    if broker_price > 0:
                        broker_drop_pct = ((price - broker_price) / price) * 100
                        if broker_drop_pct <= _stale_threshold:
                            _log.info(
                                "%s tick ₹%.2f was stale (%.1f%% drop); broker confirms ₹%.2f (%.1f%% drop) — proceeding",
                                symbol, latest_price, stale_drop_pct, broker_price, broker_drop_pct,
                            )
                            latest_price = broker_price
                            stale_drop_pct = broker_drop_pct
                        else:
                            # Both tick and broker agree price has dropped — honour the block
                            latest_price = broker_price
                            stale_drop_pct = broker_drop_pct
                    if stale_drop_pct > _stale_threshold:
                        return self._return_logged_result(
                            OrderResult(
                                False,
                                symbol=symbol,
                                side="BUY",
                                qty=qty,
                                price=price,
                                status="SKIPPED",
                                message=(
                                    f"Live price faded {stale_drop_pct:.1f}% from signal "
                                    f"(signal ₹{price:.2f}, now ₹{latest_price:.2f}); "
                                    f"threshold {_stale_threshold:.1f}% for {order_product}"
                                ),
                            )
                        )
            if latest_price < price:
                _log.info(
                    "%s buy reference refreshed from signal ₹%.2f to live ₹%.2f",
                    symbol,
                    price,
                    latest_price,
                )
                price = latest_price
            elif latest_price > price:
                _log.info(
                    "%s buy reference lifted from stale signal ₹%.2f to live ₹%.2f",
                    symbol,
                    price,
                    latest_price,
                )
                price = latest_price

        # Dry run mode
        if self.dry_run:
            _log.info("[DRY RUN] BUY %s x%d @ %s", symbol, qty, price or "MARKET")
            result = OrderResult(
                True, order_id=f"DRY-{int(time.time())}",
                symbol=symbol, side="BUY", qty=qty, price=price,
                status="COMPLETE", message="Dry run — no real order placed"
            )
            self._record_buy(
                result, stop_loss, target1, target2, blind_score, reasons,
                product_type=product_type_override or self.product_type,
                holding_horizon=holding_horizon,
                max_hold_days_override=max_hold_days_override,
            )
            return result

        # Place real order via Kite
        try:
            client = self._get_client()
            reference_price = float(price or 0.0)
            if reference_price <= 0 and order_type == "MARKET":
                reference_price = self._latest_tick_price(symbol)
            exchange, tick_size = self._resolve_exchange_and_tick_size(symbol)
            # Some brokers/accounts reject API MARKET buys without market protection.
            # Use a disciplined LIMIT at the refreshed live reference price instead
            # of paying an extra premium above the market.
            aggressive_limit_price = 0.0
            if reference_price > 0 and order_type == "MARKET":
                aggressive_limit_price = _round_to_tick(reference_price, tick_size, direction="up")
                kite_order_type = "LIMIT"
            else:
                kite_order_type = "MARKET" if order_type == "MARKET" or price <= 0 else "LIMIT"

            order_params = {
                "tradingsymbol": symbol,
                "exchange": exchange,
                "transaction_type": "BUY",
                "quantity": qty,
                "order_type": kite_order_type,
                "product": order_product,  # MIS=intraday margin (5x), CNC=delivery
                "variety": "regular",
            }
            if kite_order_type == "LIMIT" and reference_price > 0:
                order_params["price"] = aggressive_limit_price or _round_to_tick(reference_price, tick_size, direction="up")

            order_id = client.place_order(**order_params)
            order_id = str(order_id or "")
            fill_status = self._wait_for_order_status(client, order_id)

            _log.info(
                "BUY order placed: %s x%d, order_id=%s, type=%s%s, fill_status=%s",
                symbol,
                qty,
                order_id,
                kite_order_type,
                f", price={order_params.get('price')}" if "price" in order_params else "",
                fill_status.get("status"),
            )

            filled_qty = int(fill_status.get("filled_qty", 0) or 0)
            avg_price = float(fill_status.get("avg_price", 0) or 0)
            status_text = str(fill_status.get("status", "UNKNOWN") or "UNKNOWN").upper()
            if filled_qty > 0:
                if status_text != "COMPLETE" and kite_order_type == "LIMIT" and self.cancel_unfilled_limit_orders:
                    cancelled = self._cancel_order_best_effort(client, order_id)
                    status_text = "PARTIAL_FILLED_CANCELLED" if cancelled else f"{status_text}_PARTIAL_FILLED"
                fill_label = "filled" if status_text == "COMPLETE" else "partially filled"
                result = OrderResult(
                    True, order_id=order_id, symbol=symbol, side="BUY",
                    qty=filled_qty, price=avg_price or reference_price or price,
                    status=status_text,
                    message=f"Order {fill_label} (ID: {order_id}) x{filled_qty} @ ₹{(avg_price or reference_price or price):.2f}"
                )
                self._record_buy(
                    result, stop_loss, target1, target2, blind_score, reasons,
                    product_type=order_product,
                    holding_horizon=holding_horizon,
                    max_hold_days_override=max_hold_days_override,
                )
                self._daily_order_count += 1
                return result

            if kite_order_type == "LIMIT" and self.cancel_unfilled_limit_orders and status_text not in {"REJECTED", "CANCELLED"}:
                cancelled = self._cancel_order_best_effort(client, order_id)
                status_text = "CANCELLED_UNFILLED" if cancelled else f"{status_text}_UNFILLED"

            result = OrderResult(
                False, order_id=order_id, symbol=symbol, side="BUY",
                qty=filled_qty or qty, price=avg_price or reference_price or price,
                status=status_text,
                message=(
                    f"Order submitted but not filled (ID: {order_id}; final_status={status_text}; "
                    f"initial_status={fill_status.get('status', 'UNKNOWN')}). "
                    f"{fill_status.get('message', '')}".strip()
                ),
            )
            self._daily_order_count += 1
            return self._return_logged_result(result)

        except Exception as exc:
            _log.error("BUY order failed for %s: %s", symbol, exc)
            msg = str(exc)
            result = OrderResult(
                False, symbol=symbol, side="BUY", qty=qty, price=price,
                status="FAILED", message=msg
            )
            if any(phrase in msg.lower() for phrase in ("expired", "does not exist", "instrument you are placing")):
                self._dead_symbols.add(symbol)
                _log.warning("Marking %s as dead for today: %s", symbol, msg)
            if self._looks_like_funds_shortage(result.message):
                return result
            return self._return_logged_result(result)

    def place_sell_order(
        self,
        symbol: str,
        qty: int = 0,
        price: float = 0.0,
        reason: str = "",
        order_type: str = "MARKET",
    ) -> OrderResult:
        """Place a sell order for a tracked position.

        Args:
            symbol: Trading symbol
            qty: Shares to sell (0 = sell all)
            price: Limit price (0 = market)
            reason: Why selling
            order_type: MARKET or LIMIT
        """
        symbol = str(symbol).strip().upper()

        if symbol not in self._positions or self._positions[symbol].status != "open":
            return OrderResult(False, symbol=symbol, message="No open position found")

        if not _is_market_open():
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="SELL",
                    qty=qty,
                    price=price,
                    status="SKIPPED",
                    message="Market is closed",
                )
            )

        pos = self._positions[symbol]
        sell_qty = qty if qty > 0 else pos.qty
        if str(getattr(pos, "sell_block_reason", "") or "") == "CDSL_AUTH_REQUIRED":
            if str(reason or "").lower() == SellReason.MANUAL or str(reason or "").lower() == "manual_sell":
                self.clear_sell_block(symbol)
            else:
                return self._return_logged_result(
                    OrderResult(
                        False,
                        symbol=symbol,
                        side="SELL",
                        qty=sell_qty,
                        price=price,
                        status="CDSL_AUTH_REQUIRED",
                        message=(
                            "CDSL authorisation required before selling CNC/delivery shares. "
                            "Authorise in Kite/CDSL, then retry the sell."
                        ),
                    )
                )
        if str(getattr(pos, "sell_block_reason", "") or "") == "TEMPORARY_MARKET_BLOCK":
            if str(reason or "").lower() in {SellReason.MANUAL, "manual_sell"}:
                self.clear_sell_block(symbol)
            else:
                return self._return_logged_result(
                    OrderResult(
                        False,
                        symbol=symbol,
                        side="SELL",
                        qty=sell_qty,
                        price=price,
                        status="SKIPPED",
                        message=str(
                            getattr(pos, "gtt_last_error", "")
                            or "Sell temporarily blocked by exchange trading rules or circuit limits."
                        ),
                    )
                )

        if sell_qty > pos.qty:
            return OrderResult(
                False, symbol=symbol,
                message=f"Cannot sell {sell_qty}, only holding {pos.qty}"
            )

        if self.dry_run:
            _log.info("[DRY RUN] SELL %s x%d @ %s — %s", symbol, sell_qty, price or "MARKET", reason)
            result = OrderResult(
                True, order_id=f"DRY-{int(time.time())}",
                symbol=symbol, side="SELL", qty=sell_qty, price=price,
                status="COMPLETE", message=f"Dry run sell — {reason}"
            )
            self._record_sell(result, reason)
            return result

        try:
            client = self._get_client()
            circuit_blocked, circuit_reason = self._is_at_circuit_limit(symbol)
            if circuit_blocked:
                block_message = f"Circuit limit: {circuit_reason}"
                self._mark_sell_temporarily_blocked(symbol, block_message)
                return self._return_logged_result(
                    OrderResult(
                        False,
                        symbol=symbol,
                        side="SELL",
                        qty=sell_qty,
                        price=price,
                        status="SKIPPED",
                        message=block_message,
                    )
                )
            reference_price = float(price or 0.0)
            exchange, tick_size = self._resolve_exchange_and_tick_size(symbol)
            if reference_price <= 0:
                # Try tick snapshot first, fall back to live broker quote
                reference_price = self._latest_tick_price(symbol)
                if reference_price <= 0:
                    reference_price = self._broker_last_price(symbol)
                if reference_price <= 0 and hasattr(pos, "buy_price") and float(pos.buy_price or 0) > 0:
                    # Last resort: use entry price (aggressive limit 3% below to guarantee fill)
                    reference_price = float(pos.buy_price)
            # Always use an aggressive LIMIT order for sells — bare MARKET orders are
            # rejected by Kite API when market_protection is not a valid positive %.
            if reference_price > 0:
                aggressive_limit_price = _round_to_tick(max(reference_price * 0.993, tick_size), tick_size, direction="down")
                kite_order_type = "LIMIT"
            else:
                aggressive_limit_price = 0.0
                kite_order_type = "LIMIT" if order_type != "MARKET" else "MARKET"

            order_params = {
                "tradingsymbol": symbol,
                "exchange": exchange,
                "transaction_type": "SELL",
                "quantity": sell_qty,
                "order_type": kite_order_type,
                "product": getattr(pos, "product_type", self.product_type),
                "variety": "regular",
            }
            if kite_order_type == "LIMIT" and reference_price > 0:
                order_params["price"] = aggressive_limit_price or _round_to_tick(reference_price, tick_size, direction="down")
            
            order_id = client.place_order(**order_params)
            order_id = str(order_id or "")
            fill_status = self._wait_for_order_status(client, order_id)

            _log.info(
                "SELL order placed: %s x%d, order_id=%s, fill_status=%s — %s",
                symbol,
                sell_qty,
                order_id,
                fill_status.get("status"),
                reason,
            )

            filled_qty = int(fill_status.get("filled_qty", 0) or 0)
            avg_price = float(fill_status.get("avg_price", 0) or 0)
            status_text = str(fill_status.get("status", "UNKNOWN") or "UNKNOWN").upper()
            if filled_qty > 0:
                if status_text != "COMPLETE" and kite_order_type == "LIMIT" and self.cancel_unfilled_limit_orders:
                    cancelled = self._cancel_order_best_effort(client, order_id)
                    status_text = "PARTIAL_FILLED_CANCELLED" if cancelled else f"{status_text}_PARTIAL_FILLED"
                fill_label = "filled" if status_text == "COMPLETE" else "partially filled"
                result = OrderResult(
                    True, order_id=order_id, symbol=symbol, side="SELL",
                    qty=filled_qty, price=avg_price or reference_price or price,
                    status=status_text,
                    message=f"Sell {fill_label} (ID: {order_id}) x{filled_qty} @ ₹{(avg_price or reference_price or price):.2f} — {reason}"
                )
                self._record_sell(result, reason)
                return result

            if kite_order_type == "LIMIT" and self.cancel_unfilled_limit_orders and status_text not in {"REJECTED", "CANCELLED"}:
                cancelled = self._cancel_order_best_effort(client, order_id)
                status_text = "CANCELLED_UNFILLED" if cancelled else f"{status_text}_UNFILLED"

            result = OrderResult(
                False, order_id=order_id, symbol=symbol, side="SELL",
                qty=filled_qty or sell_qty, price=avg_price or reference_price or price,
                status=status_text,
                message=(
                    f"Sell submitted but not filled (ID: {order_id}; final_status={status_text}; "
                    f"initial_status={fill_status.get('status', 'UNKNOWN')}). "
                    f"{fill_status.get('message', '')}".strip()
                ),
            )
            if self._looks_like_cdsl_authorization_required(result.message):
                result.status = "CDSL_AUTH_REQUIRED"
                result.message = (
                    "CDSL authorisation required before selling CNC/delivery shares. "
                    "Authorise the shares in Kite/CDSL and place a fresh sell order."
                )
                self._mark_cdsl_authorization_required(symbol, result.message)
                _send_discord_alert(
                    f"🔐 **CDSL AUTH REQUIRED — {symbol}**\n"
                    f"Cannot sell `{symbol}` x{sell_qty} — CDSL TPIN not authorized.\n"
                    f"**Action:** Open Kite app → Holdings → Authorize → enter TPIN.\n"
                    f"Reason triggered: `{reason}`",
                    dedup_key=f"cdsl_{symbol}_{date.today().isoformat()}",
                )
            elif self._looks_like_sell_temporarily_blocked(result.message):
                result.status = "SELL_TEMP_BLOCKED"
                self._mark_sell_temporarily_blocked(symbol, result.message)
            else:
                _send_discord_alert(
                    f"🚨 **SELL FAILED — {symbol}**\n"
                    f"Tried to sell `{symbol}` x{sell_qty} (reason: `{reason}`) but order was NOT filled.\n"
                    f"Status: `{result.status}` | Message: {result.message[:200]}",
                    dedup_key=f"sellfail_{symbol}_{date.today().isoformat()}",
                )
            return self._return_logged_result(result)

        except Exception as exc:
            _log.error("SELL order failed for %s: %s", symbol, exc)
            result = OrderResult(
                False, symbol=symbol, side="SELL", qty=sell_qty, price=price,
                status="FAILED", message=str(exc)
            )
            if self._looks_like_cdsl_authorization_required(result.message):
                result.status = "CDSL_AUTH_REQUIRED"
                result.message = (
                    "CDSL authorisation required before selling CNC/delivery shares. "
                    "Authorise the shares in Kite/CDSL and place a fresh sell order."
                )
                self._mark_cdsl_authorization_required(symbol, result.message)
                _send_discord_alert(
                    f"🔐 **CDSL AUTH REQUIRED — {symbol}**\n"
                    f"Cannot sell `{symbol}` x{sell_qty} — CDSL TPIN not authorized.\n"
                    f"**Action:** Open Kite app → Holdings → Authorize → enter TPIN.\n"
                    f"Reason triggered: `{reason}`",
                    dedup_key=f"cdsl_{symbol}_{date.today().isoformat()}",
                )
            elif self._looks_like_sell_temporarily_blocked(result.message):
                result.status = "SELL_TEMP_BLOCKED"
                self._mark_sell_temporarily_blocked(symbol, result.message)
            else:
                _send_discord_alert(
                    f"🚨 **SELL FAILED (exception) — {symbol}**\n"
                    f"Tried to sell `{symbol}` x{sell_qty} (reason: `{reason}`) but got exception.\n"
                    f"Error: {str(exc)[:300]}",
                    dedup_key=f"sellfail_{symbol}_{date.today().isoformat()}",
                )
            self._log_order(result)
            return result

    # ------------------------------------------------------------------
    # Record keeping
    # ------------------------------------------------------------------

    def _record_buy(self, result: OrderResult, stop_loss: float,
                    target1: float, target2: float, blind_score: float, reasons: str,
                    product_type: str = "", holding_horizon: str = "",
                    max_hold_days_override: int = 0):
        with self._lock:
            pos = TrackedPosition(
                symbol=result.symbol, qty=result.qty,
                buy_price=result.price, buy_order_id=result.order_id,
                stop_loss=stop_loss, target1=target1, target2=target2,
                blind_score=blind_score, reasons=reasons,
                product_type=product_type or self.product_type,
                holding_horizon=holding_horizon or ("intraday" if (product_type or self.product_type) == "MIS" else "swing"),
                max_hold_days_override=max_hold_days_override,
            )
            self._positions[result.symbol] = pos
            self._save_positions()
        self._log_order(result)

    def _record_sell(self, result: OrderResult, reason: str):
        with self._lock:
            if result.symbol in self._positions:
                pos = self._positions[result.symbol]
                # Partial fills must keep the remaining broker position visible.
                if result.qty < pos.qty:
                    pos.qty -= result.qty
                    _log.info("Partial sell %s: sold %d, remaining %d",
                              result.symbol, result.qty, pos.qty)
                    self._save_positions()
                else:
                    # Full sell: close position
                    pos.sell_order_id = result.order_id
                    pos.sell_price = result.price
                    pos.sell_time = _ist_now().isoformat(timespec="seconds")
                    pos.sell_reason = reason
                    pos.status = "closed"
                    self._save_positions()
        self._log_order(result)

    def _mark_position_stale(self, symbol: str, reason: str = SellReason.STALE_TRACKED) -> None:
        """Close a tracked position that is no longer present at the broker."""
        symbol = str(symbol or "").strip().upper()
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None or pos.status != "open":
                return
            pos.sell_time = _ist_now().isoformat(timespec="seconds")
            pos.sell_reason = reason
            pos.status = "closed"
            pos.gtt_status = "not_held"
            pos.gtt_last_error = "Position missing at broker; marked stale locally"
            pos.gtt_updated_at = _ist_now().isoformat(timespec="seconds")
            self._save_positions()

    # ------------------------------------------------------------------
    # Position monitoring & sell signals
    # ------------------------------------------------------------------

    def _compute_swing_stage(self, pos: "TrackedPosition", pnl_pct: float) -> str:
        """Derive lifecycle stage of a CNC/NRML position from its current state."""
        if pos.status != "open":
            return "EXITED"
        if pos.t1_booked:
            if pnl_pct >= 10.0:
                return "RUNNER"
            ts = float(getattr(pos, "trailing_stop", 0) or 0)
            if ts > 0 and ts >= float(getattr(pos, "buy_price", 0) or 0):
                return "BREAKEVEN_PROTECTED"
            return "PARTIAL_BOOKED"
        if pnl_pct >= 2.0:
            return "CONFIRMED"
        if str(getattr(pos, "gtt_trigger_id", "") or ""):
            return "PROTECTED"
        return "NEW"

    def check_sell_signals(self, ticks: Dict[str, Dict] = None) -> List[Tuple[str, str, float, int]]:
        """Check all open positions for sell conditions.

        Returns list of (symbol, reason, current_price, qty) for positions that
        should be sold. qty=0 means sell all, qty>0 means partial sell.
        """
        if ticks is None:
            ticks = self._load_ticks()

        signals = []
        now = _ist_now()

        for sym, pos in list(self._positions.items()):
            if pos.status != "open":
                continue
            if str(getattr(pos, "sell_block_reason", "") or "") in {"CDSL_AUTH_REQUIRED", "TEMPORARY_MARKET_BLOCK"}:
                continue

            tick = ticks.get(sym, {})
            live_price = float(tick.get("last_price", 0))
            if live_price <= 0:
                live_price = self._broker_last_price(sym)
            if live_price <= 0:
                continue

            # Update highest price for trailing stop
            if live_price > pos.highest_price:
                pos.highest_price = live_price

            pnl_pct = ((live_price - pos.buy_price) / pos.buy_price * 100) if pos.buy_price > 0 else 0
            is_intraday = getattr(pos, "product_type", self.product_type) == "MIS" or getattr(pos, "holding_horizon", "") == "intraday"
            hold_days_limit = int(getattr(pos, "max_hold_days_override", 0) or 0) or self.max_hold_days
            now_min = now.hour * 60 + now.minute

            # Update swing lifecycle stage (CNC/NRML only; intraday positions skip)
            if not is_intraday:
                _new_stage = self._compute_swing_stage(pos, pnl_pct)
                if _new_stage != getattr(pos, "swing_stage", "NEW"):
                    _log.info("%s swing stage: %s → %s (P/L %.1f%%)",
                              sym, getattr(pos, "swing_stage", "NEW"), _new_stage, pnl_pct)
                    pos.swing_stage = _new_stage

            # 0. Hard intraday square-off before broker auto square-off.
            if is_intraday and now_min >= 15 * 60 + 10:
                signals.append((sym, SellReason.EOD_SQUAREOFF, live_price, 0))
                continue

            # 1. Stop loss hit
            if pos.stop_loss > 0 and live_price <= pos.stop_loss:
                if self.confirm_stop_loss_with_broker:
                    broker_price = self._broker_last_price(sym)
                    if broker_price > 0 and broker_price > pos.stop_loss:
                        _log.warning(
                            "Ignoring false stop tick for %s: tick ₹%.2f <= SL ₹%.2f, broker LTP ₹%.2f",
                            sym,
                            live_price,
                            pos.stop_loss,
                            broker_price,
                        )
                        continue
                    if broker_price > 0:
                        live_price = broker_price
                signals.append((sym, SellReason.STOP_LOSS, live_price, 0))
                continue

            # 2. Target 2 hit (max profit)
            if pos.target2 > 0 and live_price >= pos.target2:
                signals.append((sym, SellReason.TARGET_2, live_price, 0))
                continue

            # 3. Target 1 hit — sell 50% to lock in profits, trail the rest
            if pos.target1 > 0 and live_price >= pos.target1 and not pos.t1_booked:
                partial_qty = max(1, pos.qty // 2)
                signals.append((sym, SellReason.PARTIAL_T1, live_price, partial_qty))
                pos.t1_booked = True
                _t1_trail_dist = (self.trailing_trail_pct / 100) if is_intraday else (self.swing_trail_dist_pct / 100)
                pos.trailing_stop = max(pos.buy_price, pos.target1 * (1 - _t1_trail_dist))
                if not is_intraday:
                    # GTT was placed for original qty; remaining is pos.qty // 2.
                    # Flag for cancel + reissue on next audit cycle.
                    pos.gtt_needs_reissue = True
                _log.info("%s hit T1 (₹%.2f): selling %d of %d, trailing rest from ₹%.2f",
                          sym, pos.target1, partial_qty, pos.qty, pos.trailing_stop)

            # 3b. Failed-breakout fast exit (intraday only). Catches breakouts
            # that immediately fail: lost VWAP, dropped under entry, with real
            # sell-side pressure — exit before the 3% stop turns it into a
            # bigger loser. Conservative by design:
            #   * skips once T1 has already been booked (trade is in profit lane)
            #   * needs all four signals at once (no single noisy tick can fire)
            #   * fails safe when VWAP / depth data is missing
            if (
                is_intraday
                and not pos.t1_booked
                and self.failed_breakout_min_age_sec > 0
                and self.failed_breakout_max_age_sec > 0
            ):
                age_sec = -1.0
                try:
                    age_sec = (now - datetime.fromisoformat(pos.buy_time)).total_seconds()
                except Exception:
                    age_sec = -1.0
                if (
                    age_sec >= self.failed_breakout_min_age_sec
                    and age_sec <= self.failed_breakout_max_age_sec
                    and live_price < pos.buy_price
                    and pnl_pct <= -self.failed_breakout_min_drop_pct
                ):
                    vwap = float(tick.get("average_traded_price") or 0.0)
                    total_buy = float(tick.get("total_buy_quantity") or 0.0)
                    total_sell = float(tick.get("total_sell_quantity") or 0.0)
                    has_vwap = vwap > 0
                    has_depth = total_buy > 0 and total_sell > 0
                    lost_vwap = has_vwap and live_price < vwap
                    sell_pressure_confirmed = (
                        has_depth
                        and (total_sell / max(total_buy, 1.0)) >= self.failed_breakout_sell_imbalance
                    )
                    if lost_vwap and sell_pressure_confirmed:
                        signals.append((sym, SellReason.FAILED_BREAKOUT, live_price, 0))
                        _log.info(
                            "%s failed-breakout fast exit: age=%.0fs P/L=%.1f%% LTP=%.2f<VWAP=%.2f sell/buy=%.1fx",
                            sym, age_sec, pnl_pct, live_price, vwap,
                            (total_sell / max(total_buy, 1.0)) if has_depth else 0.0,
                        )
                        continue

            # 3c. Swing failed-structure exit (CNC/NRML only).
            # If a breakout immediately reverses on the same calendar day, exit
            # rather than waiting for the full 5% stop. Does not require VWAP or
            # depth data — price action and trade age are sufficient.
            # Disabled by setting KITE_SWING_FAILED_MIN_AGE_SEC=0 or MIN_DROP_PCT=0.
            if (
                not is_intraday
                and not pos.t1_booked
                and self.swing_failed_min_age_sec > 0
                and self.swing_failed_min_drop_pct > 0
            ):
                try:
                    _entry_date = datetime.fromisoformat(pos.buy_time).date()
                    _age_sec = (now - datetime.fromisoformat(pos.buy_time)).total_seconds()
                    _same_day = now.date() == _entry_date
                except Exception:
                    _same_day = False
                    _age_sec = -1.0
                if (
                    _same_day
                    and _age_sec >= self.swing_failed_min_age_sec
                    and pnl_pct <= -self.swing_failed_min_drop_pct
                ):
                    signals.append((sym, SellReason.SWING_FAILED_STRUCTURE, live_price, 0))
                    _log.info(
                        "%s swing failed-structure exit: age=%.0fs P/L=%.1f%%",
                        sym, _age_sec, pnl_pct,
                    )
                    continue

            # 4. Trailing stop — intraday uses tight trail; swing uses wide trail to let winners run
            # Swing: activates at swing_trail_activate_pct (default 5%), trails swing_trail_dist_pct (default 4%)
            # Intraday: activates at trailing_stop_pct (default 3%), trails trailing_trail_pct% (tighter)
            if is_intraday:
                _trail_activate_pct = self.trailing_stop_pct
                _trail_dist_pct = self.trailing_trail_pct / 100
            else:
                _trail_activate_pct = self.swing_trail_activate_pct
                _trail_dist_pct = self.swing_trail_dist_pct / 100

            if pos.trailing_stop <= 0 and pnl_pct >= _trail_activate_pct:
                pos.trailing_stop = max(pos.buy_price, pos.highest_price * (1 - _trail_dist_pct))

            if pos.trailing_stop > 0:
                new_trail = pos.highest_price * (1 - _trail_dist_pct)
                if new_trail > pos.trailing_stop:
                    pos.trailing_stop = new_trail
                if live_price <= pos.trailing_stop:
                    signals.append((sym, SellReason.TRAILING_STOP, live_price, 0))
                    continue

            # 5. Time decay — exit only if still losing after max hold period
            # Don't exit positive positions on time alone; let trailing stop handle them.
            try:
                buy_date = datetime.fromisoformat(pos.buy_time).date()
                days_held = (now.date() - buy_date).days
                if days_held >= hold_days_limit and pnl_pct < 0.0:
                    signals.append((sym, SellReason.TIME_DECAY, live_price, 0))
                    continue
            except Exception:
                pass

            # 6. Intraday reversal — stock was up >3% but reversed to negative
            if is_intraday and pnl_pct < -1.0 and pos.highest_price > pos.buy_price * 1.03:
                signals.append((sym, SellReason.REVERSAL, live_price, 0))
                continue

            # 7. CUSUM regime break — detect intraday structural shifts
            #    Uses recent 5-min candle returns to identify when momentum reverses
            #    Threshold 0.05 tuned via backtest sweep (0.02 was too trigger-happy)
            tick_data = tick.get("depth", {})
            ohlc = tick.get("ohlc", {})
            if is_intraday and ohlc and pnl_pct > 1.5:  # only check when solidly profitable (>1.5%)
                try:
                    from pkscreener.classes.AdvancedFilters import CUSUMBreakDetector
                    from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider
                    import numpy as _np
                    provider = KiteHistoricalDataProvider()
                    # Fetch last ~2 hours of 5-minute candles (24 candles)
                    candles = provider.get_stock_data(sym, interval="5minute", count=24)
                    if candles is not None and len(candles) >= 10:
                        closes = candles["close"].values
                        returns = _np.diff(closes) / closes[:-1]
                        cusum = CUSUMBreakDetector(threshold=0.05)
                        should_exit, reason_text = cusum.should_exit_long(returns)
                        if should_exit:
                            signals.append((sym, SellReason.REGIME_BREAK, live_price, 0))
                            _log.info("%s CUSUM exit: %s (P/L %.1f%%)", sym, reason_text, pnl_pct)
                            continue
                except Exception as exc:
                    _log.debug("CUSUM check failed for %s: %s", sym, exc)

        # Save updated trailing stops / highest prices
        with self._lock:
            self._save_positions()

        return signals

    def execute_sell_signals(self, ticks: Dict[str, Dict] = None) -> List[OrderResult]:
        """Check for sell signals and execute sell orders.

        Returns list of OrderResults for executed sells.
        Supports partial sells (e.g. 50% at T1) via qty parameter.
        """
        if not _is_market_open():
            return []

        signals = self.check_sell_signals(ticks)
        results = []

        for sym, reason, price, qty in signals:
            result = self.place_sell_order(symbol=sym, qty=qty, price=price, reason=reason)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Auto-buy from alert candidates
    # ------------------------------------------------------------------

    def auto_buy_from_candidate(
        self,
        symbol: str,
        live_price: float,
        stop_loss: float = 0.0,
        target1: float = 0.0,
        target2: float = 0.0,
        blind_score: float = 0.0,
        reasons: str = "",
        product_type: str = "",
        holding_horizon: str = "",
        max_hold_days: int = 0,
    ) -> OrderResult:
        """Auto-buy a stock from a blind-buy candidate alert.

        Uses the same position sizing logic as the alerting engine so execution
        matches the quantity shown to the user.
        """
        if live_price <= 0:
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    price=live_price,
                    status="FAILED",
                    message="Invalid price",
                )
            )

        try:
            from pkscreener.classes.AutoPilotInvestor import _compute_suggested_qty

            order_product = self._normalize_product_type(product_type)
            per_order_limit = self._per_order_limit_for_product(order_product)
            qty, _ = _compute_suggested_qty(
                live_price,
                stop_loss=stop_loss,
                score=blind_score,
                max_position=per_order_limit,
            )
        except Exception:
            order_product = self._normalize_product_type(product_type)
            per_order_limit = self._per_order_limit_for_product(order_product)
            qty = int(per_order_limit // live_price) if live_price > 0 else 0
        qty, _ = self.estimate_affordable_qty(
            price=live_price,
            desired_qty=qty,
            product_type=product_type,
        )
        if qty <= 0:
            bucket = self._capital_bucket_for_product(product_type)
            per_order_limit = self._per_order_limit_for_product(product_type)
            remaining_pool = self._remaining_capital_for_product(product_type)
            return self._return_logged_result(
                OrderResult(
                    False,
                    symbol=symbol,
                    side="BUY",
                    price=live_price,
                    status="SKIPPED",
                    message=(
                        f"No affordable {bucket} size available now. "
                        f"Per-trade cap ₹{per_order_limit:,.0f}, remaining pool ₹{remaining_pool:,.0f}."
                    ),
                )
            )
        # Rescue stale signal targets: if the stock ran since the signal scan, target2
        # may be only 1-2% above current price. Floor at 5% so the GTT has room.
        # pkscreener's valid technical targets (6-15%) are left untouched.
        order_product = self._normalize_product_type(product_type)
        is_swing_order = order_product in ("CNC", "NRML")
        if is_swing_order and live_price > 0:
            _min_t2_pct = float(os.environ.get("KITE_SWING_MIN_TARGET2_PCT", "8.0"))
            _max_t2_pct = float(os.environ.get("KITE_SWING_MAX_TARGET2_PCT", "20.0"))
            _min_t2 = live_price * (1 + _min_t2_pct / 100)
            _max_t2 = live_price * (1 + _max_t2_pct / 100)
            if target2 <= 0 or target2 < _min_t2:
                target2 = round(_min_t2, 2)
                target1 = round(live_price * (1 + _min_t2_pct / 2 / 100), 2)
            elif target2 > _max_t2:
                target2 = round(_max_t2, 2)
            if stop_loss <= 0 or stop_loss >= live_price:
                _sl_pct = float(os.environ.get("KITE_SWING_DEFAULT_SL_PCT", "5.0"))
                stop_loss = round(live_price * (1 - _sl_pct / 100), 2)
        result = self.place_buy_order(
            symbol=symbol, qty=qty, price=live_price,
            stop_loss=stop_loss, target1=target1, target2=target2,
            blind_score=blind_score, reasons=reasons,
            order_type="MARKET",
            product_type_override=product_type,
            holding_horizon=holding_horizon,
            max_hold_days_override=max_hold_days,
        )
        if (not result.success) and self._looks_like_funds_shortage(result.message):
            available = self._extract_available_funds()
            needed = max(0.0, qty * live_price - available)
            self._queue_pending_buy(
                symbol=symbol,
                qty=qty,
                live_price=live_price,
                stop_loss=stop_loss,
                target1=target1,
                target2=target2,
                blind_score=blind_score,
                reasons=reasons,
                product_type=product_type,
                holding_horizon=holding_horizon,
                max_hold_days=max_hold_days,
                error_message=result.message,
            )
            result.status = "FUNDS_SHORT"
            if needed > 0:
                result.message = (
                    f"Insufficient funds/margin. Add about ₹{needed:,.0f}; "
                    f"auto-retry queued for the next 20 minutes."
                )
            else:
                result.message = "Insufficient funds/margin; auto-retry queued for the next 20 minutes."
            self._log_order(result)
        return result

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_open_positions(self) -> List[TrackedPosition]:
        return [p for p in self._positions.values() if p.status == "open"]

    def get_closed_positions(self) -> List[TrackedPosition]:
        return [p for p in self._positions.values() if p.status == "closed"]

    def get_position(self, symbol: str) -> Optional[TrackedPosition]:
        return self._positions.get(str(symbol).strip().upper())

    def get_total_pnl(self) -> float:
        return sum(p.pnl for p in self._positions.values() if p.status == "closed")

    def get_open_value(self, ticks: Dict[str, Dict] = None) -> float:
        if ticks is None:
            ticks = self._load_ticks()
        total = 0.0
        for p in self._positions.values():
            if p.status == "open":
                price = float(ticks.get(p.symbol, {}).get("last_price", p.buy_price))
                total += price * p.qty
        return total

    def format_positions_summary(self, ticks: Dict[str, Dict] = None) -> str:
        """Format open positions for Discord display."""
        if ticks is None:
            ticks = self._load_ticks()

        open_pos = self.get_open_positions()
        if not open_pos:
            return "No open Kite positions."

        lines = [f"**📊 Kite Positions ({len(open_pos)})**"]
        total_pnl = 0.0
        for p in open_pos:
            live = float(ticks.get(p.symbol, {}).get("last_price", p.buy_price))
            pnl = (live - p.buy_price) * p.qty
            pnl_pct = ((live - p.buy_price) / p.buy_price * 100) if p.buy_price > 0 else 0
            total_pnl += pnl
            emoji = "🟢" if pnl >= 0 else "🔴"
            trail_info = f" | trail ₹{p.trailing_stop:.0f}" if p.trailing_stop > 0 else ""
            horizon_info = f" | {getattr(p, 'holding_horizon', 'intraday')}/{getattr(p, 'product_type', self.product_type)}"
            block_info = ""
            if str(getattr(p, "sell_block_reason", "") or "") == "CDSL_AUTH_REQUIRED":
                block_info = " | ⚠️ CDSL auth needed before sell"
            elif str(getattr(p, "sell_block_reason", "") or "") == "TEMPORARY_MARKET_BLOCK":
                detail = str(getattr(p, "gtt_last_error", "") or "exchange/circuit blocked")
                block_info = f" | ⚠️ Sell blocked: {detail[:60]}"
            lines.append(
                f"{emoji} `{p.symbol}` x{p.qty} | buy ₹{p.buy_price:.0f} → ₹{live:.0f} "
                f"| P/L ₹{pnl:+,.0f} ({pnl_pct:+.1f}%)"
                f" | SL ₹{p.stop_loss:.0f}{trail_info}{horizon_info}{block_info}"
            )
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{emoji} **Total running P/L: ₹{total_pnl:+,.0f}**")
        return "\n".join(lines)

    def format_sell_reason(self, reason: str) -> str:
        """Human-readable sell reason."""
        return {
            SellReason.STOP_LOSS: "🔴 Stop loss hit",
            SellReason.TARGET_1: "🟢 Target 1 reached",
            SellReason.PARTIAL_T1: "🟢 Partial profit booked (50% at T1)",
            SellReason.TARGET_2: "🟢🟢 Max target reached!",
            SellReason.TRAILING_STOP: "🟡 Trailing stop triggered",
            SellReason.TIME_DECAY: "⏰ Held too long without gain",
            SellReason.EOD_SQUAREOFF: "🕒 End-of-day MIS square-off",
            SellReason.REVERSAL: "🔻 Intraday reversal detected",
            SellReason.REGIME_BREAK: "📉 CUSUM regime break — momentum reversed",
            SellReason.FAILED_BREAKOUT: "⚠️ Failed breakout — lost VWAP under entry with sell pressure",
            SellReason.MANUAL: "👤 Manual sell",
            SellReason.STALE_TRACKED: "🧹 Removed stale tracked position",
            SellReason.SWING_FAILED_STRUCTURE: "⚠️ Swing failed-structure — broke entry same day",
            SellReason.MOMENTUM_FADE: "📉 Momentum fading — profit slipping with weak volume",
            SellReason.SECTOR_WEAKNESS: "📊 Sector turned weak",
            SellReason.MARKET_WEAKNESS: "🌐 Broad market weakness",
            SellReason.GAP_RISK_EXIT: "📭 Gap-down broke trade thesis",
            SellReason.RUNNER_EXIT: "🏁 Runner position closed",
            SellReason.BREAKEVEN_EXIT: "⚖️ Exited at breakeven to protect capital",
        }.get(reason, reason)

    # ------------------------------------------------------------------
    # Tick loading (reuse from KiteTickerStream output)
    # ------------------------------------------------------------------

    def _load_ticks(self) -> Dict[str, Dict]:
        """Load latest tick snapshot from KiteTickerStream output."""
        for universe in ("allstocks", "nifty500", "nifty100", "nifty50", "main"):
            tick_file = os.path.join(self.output_dir, f"kiteticker_{universe}_ticks_latest.json")
            if os.path.exists(tick_file):
                try:
                    with open(tick_file, "r") as f:
                        return json.load(f)
                except Exception:
                    continue
        return {}

    # ------------------------------------------------------------------
    # Reconcile with Kite positions (real broker state)
    # ------------------------------------------------------------------

    def reconcile_with_broker(self) -> Dict[str, Any]:
        """Fetch real positions from Kite and reconcile with tracked ones.

        For MIS positions that are tracked as open but absent from the broker,
        this marks them closed — they were auto-squared off by Zerodha at 3:20 PM.

        Returns a report of actions taken and any remaining discrepancies.
        """
        try:
            broker_holdings = self._get_broker_exposure()
        except Exception as exc:
            return {"error": str(exc), "reconciled": False}

        report = {"broker_positions": len(broker_holdings), "tracked_positions": 0,
                  "auto_closed": [], "mismatches": [], "untracked": [], "reconciled": True}

        with self._lock:
            tracked_open = {sym: pos for sym, pos in self._positions.items() if pos.status == "open"}
            report["tracked_positions"] = len(tracked_open)

            changed = False
            for sym, pos in tracked_open.items():
                bp = broker_holdings.get(sym)
                if bp is None:
                    # Position not at broker at all — it was sold (GTT triggered, manual
                    # sell, or auto-squareoff) without our system recording the sell.
                    sell_reason = "broker_auto_square_off" if pos.product_type == "MIS" else "gtt_or_manual_sell"
                    # Best-effort sell price: fetch fresh quote, fall back to entry price
                    sell_price = self._broker_last_price(sym) or float(pos.buy_price or 0)
                    pos.status = "closed"
                    pos.sell_reason = sell_reason
                    pos.sell_price = sell_price
                    if not pos.sell_time:
                        pos.sell_time = _ist_now().isoformat(timespec="seconds")
                    report["auto_closed"].append(sym)
                    changed = True
                    _log.info("Reconcile: closed ghost position %s (reason=%s, est_sell=%.2f)", sym, sell_reason, sell_price)
                    # Write a synthetic sell record to the order log so portfolio_report.py stays accurate
                    self._log_order(OrderResult(
                        True, symbol=sym, side="SELL", qty=pos.qty, price=sell_price,
                        status="RECONCILED",
                        message=f"Reconciled: position not found in broker ({sell_reason}). Estimated sell @ {sell_price:.2f}",
                    ))
                elif bp["qty"] != pos.qty:
                    report["mismatches"].append(
                        f"{sym}: tracked qty={pos.qty}, broker qty={bp['qty']}"
                    )

            if changed:
                self._save_positions()

        for sym in broker_holdings:
            if sym not in tracked_open:
                report["untracked"].append(sym)

        return report

    def get_broker_exposure(self) -> Dict[str, Dict[str, Any]]:
        """Return the current broker-side live exposure across holdings and positions."""
        return self._get_broker_exposure()

    def _get_broker_exposure(self) -> Dict[str, Dict[str, Any]]:
        """Return broker-side live exposure across holdings and open positions."""
        client = self._get_client()
        exposure: Dict[str, Dict[str, Any]] = {}

        # Delivery holdings, including unsettled T1 quantity.
        try:
            holdings = client.holdings() or []
            for row in holdings:
                sym = str(row.get("tradingsymbol", "")).strip().upper()
                qty = int(row.get("quantity", 0) or 0) + int(row.get("t1_quantity", 0) or 0)
                if not sym or qty <= 0:
                    continue
                exposure[sym] = {
                    "qty": qty,
                    "avg_price": float(row.get("average_price", 0) or 0),
                    "last_price": float(row.get("last_price", 0) or 0),
                    "pnl": float(row.get("pnl", 0) or 0),
                    "product": "CNC",
                    "source": "holdings",
                }
        except Exception as exc:
            _log.debug("Holdings lookup failed: %s", exc)

        # Net positions for intraday / derivatives.
        try:
            kite_positions = client.positions() or {}
            net_positions = kite_positions.get("net", []) if isinstance(kite_positions, dict) else []
            for row in net_positions:
                sym = str(row.get("tradingsymbol", "")).strip().upper()
                qty = int(row.get("quantity", 0) or 0)
                if not sym or qty == 0:
                    continue
                exposure[sym] = {
                    "qty": qty,
                    "avg_price": float(row.get("average_price", 0) or 0),
                    "last_price": float(row.get("last_price", 0) or 0),
                    "pnl": float(row.get("pnl", 0) or 0),
                    "product": str(row.get("product", "") or ""),
                    "source": "positions",
                }
        except Exception as exc:
            _log.debug("Positions lookup failed: %s", exc)

        return exposure

    # ------------------------------------------------------------------
    # Order status check
    # ------------------------------------------------------------------

    def check_order_status(self, order_id: str) -> Dict[str, Any]:
        """Check the status of a placed order."""
        try:
            client = self._get_client()
            order_history = client.order_history(order_id)
            if order_history:
                return self._order_status_from_history(order_id, order_history)
        except Exception as exc:
            return {"order_id": order_id, "status": "ERROR", "error": str(exc)}
        return {"order_id": order_id, "status": "UNKNOWN"}

    def update_order_fills(self):
        """Check pending buy orders and update with actual fill prices."""
        for sym, pos in list(self._positions.items()):
            if pos.status == "open" and pos.buy_order_id and pos.buy_price <= 0:
                status = self.check_order_status(pos.buy_order_id)
                if status.get("status") == "COMPLETE" and status.get("avg_price", 0) > 0:
                    pos.buy_price = float(status["avg_price"])
                    pos.highest_price = pos.buy_price
                    _log.info("%s fill confirmed: ₹%.2f", sym, pos.buy_price)
                elif status.get("status") in ("CANCELLED", "REJECTED"):
                    pos.status = "closed"
                    pos.sell_reason = f"Order {status['status'].lower()}"
                    _log.info("%s order %s", sym, status["status"])
        with self._lock:
            self._save_positions()

    # ------------------------------------------------------------------
    # GTT (Good Till Triggered) — auto stop-loss + target on Kite
    # ------------------------------------------------------------------

    def place_gtt_oco(
        self,
        symbol: str,
        qty: int,
        buy_price: float,
        stop_loss: float,
        target: float,
        exchange: str = "NSE",
        product_type: str = "",
        current_price: float = 0.0,
    ) -> Dict[str, Any]:
        """Place a Kite GTT OCO (One-Cancels-Other) order for stop-loss AND target.

        This puts TWO SELL triggers on Kite simultaneously:
          - If price drops to stop_loss → SELL at market
          - If price rises to target    → SELL at limit (target price)
        Whichever fires first cancels the other.

        Args:
            symbol:        NSE trading symbol (e.g. RELIANCE)
            qty:           Shares to sell (must match your holding)
            buy_price:     Your entry price
            stop_loss:     SL trigger price (can be above buy_price for trailing stops)
            target:        Profit target price (above current price)
            exchange:      Usually "NSE" (default)
            current_price: Live market price (used as Kite last_price reference).
                           Falls back to buy_price if not provided.

        Returns:
            dict with keys: success (bool), trigger_id (str), message (str)
        """
        symbol = symbol.strip().upper()
        if stop_loss <= 0 or target <= 0:
            return {"success": False, "message": "stop_loss and target must both be > 0"}
        if stop_loss >= target:
            return {"success": False, "message": f"stop_loss {stop_loss} must be below target {target}"}

        # Kite's last_price must sit between stop_loss and target for the OCO
        # condition to be evaluatable. Use live price when available (trailing
        # stop reissues often have stop_loss > original buy_price).
        last_price_ref = current_price if current_price > 0 else buy_price
        if last_price_ref <= stop_loss or last_price_ref >= target:
            # Clamp into valid range as a safety fallback
            last_price_ref = (stop_loss + target) / 2

        if self.dry_run:
            _log.info(
                "[DRY RUN] GTT OCO %s x%d: SL=%.2f TARGET=%.2f (entry ₹%.2f)",
                symbol, qty, stop_loss, target, buy_price,
            )
            return {
                "success": True,
                "trigger_id": f"DRY-GTT-{int(time.time())}",
                "message": (
                    f"[Dry run] GTT OCO set — SL ₹{stop_loss:.2f} | Target ₹{target:.2f}"
                ),
            }

        try:
            client = self._get_client()
            order_product = self._normalize_product_type(product_type)
            resolved_exchange, tick_size = self._resolve_exchange_and_tick_size(symbol)
            if not exchange or exchange == "NSE":
                exchange = resolved_exchange
            # Kite OCO: trigger_values = [stop_loss, target] (ascending order required by API)
            trigger_response = client.place_gtt(
                trigger_type=client.GTT_TYPE_OCO,
                tradingsymbol=symbol,
                exchange=exchange,
                trigger_values=[
                    _round_to_tick(stop_loss, tick_size, direction="down"),
                    _round_to_tick(target, tick_size, direction="up"),
                ],
                last_price=_round_to_tick(last_price_ref, tick_size, direction="nearest"),
                orders=[
                    # Leg 1: stop-loss — fires when price <= stop_loss
                    {
                        "transaction_type": "SELL",
                        "quantity": qty,
                        "order_type": "MARKET",
                        "product": order_product,
                        # The installed Kite client validates `price` on every GTT leg,
                        # even for MARKET orders.
                        "price": _round_to_tick(stop_loss, tick_size, direction="down"),
                    },
                    # Leg 2: target — fires when price >= target
                    {
                        "transaction_type": "SELL",
                        "quantity": qty,
                        "order_type": "LIMIT",
                        "product": order_product,
                        "price": _round_to_tick(target, tick_size, direction="up"),
                    },
                ],
            )
            trigger_id = _normalize_gtt_trigger_id(trigger_response)
            _log.info(
                "GTT OCO placed for %s x%d: SL=%.2f TARGET=%.2f — trigger_id=%s",
                symbol, qty, stop_loss, target, trigger_id,
            )
            return {
                "success": True,
                "trigger_id": trigger_id,
                "message": (
                    f"✅ GTT set: SL ₹{stop_loss:.2f} | Target ₹{target:.2f} (ID {trigger_id})"
                ),
            }
        except Exception as exc:
            _log.error("GTT OCO failed for %s: %s", symbol, exc)
            return {"success": False, "message": str(exc)}

    def get_gtts(self) -> List[Dict]:
        """List all active GTT triggers on Kite."""
        try:
            client = self._get_client()
            return client.get_gtts() or []
        except Exception as exc:
            _log.error("get_gtts failed: %s", exc)
            return []

    def get_gtt(self, trigger_id: str) -> Dict[str, Any]:
        """Fetch a single GTT trigger by id."""
        try:
            trigger_id = str(trigger_id or "").strip()
            if not trigger_id:
                return {}
            client = self._get_client()
            data = client.get_gtt(trigger_id)
            return data or {}
        except Exception as exc:
            _log.error("get_gtt failed for %s: %s", trigger_id, exc)
            return {}

    def delete_gtt(self, trigger_id: str) -> bool:
        """Delete a broker-side GTT trigger by id."""
        try:
            trigger_id = str(trigger_id or "").strip()
            if not trigger_id:
                return False
            client = self._get_client()
            client.delete_gtt(trigger_id)
            return True
        except Exception as exc:
            _log.error("delete_gtt failed for %s: %s", trigger_id, exc)
            return False

    def audit_open_position_gtts(self, max_repairs: int = 2) -> List[Dict[str, Any]]:
        """Audit broker-side GTT protection for open swing positions.

        Returns a list of audit actions with keys:
            symbol, action, ok, message, trigger_id
        """
        actions: List[Dict[str, Any]] = []
        repaired = 0
        open_positions = [p for p in self._positions.values() if p.status == "open"]
        if not open_positions:
            return actions

        try:
            all_gtts = self.get_gtts()
        except Exception:
            all_gtts = []
        try:
            broker_exposure = self._get_broker_exposure()
        except Exception as exc:
            _log.error("Broker exposure lookup failed during GTT audit: %s", exc)
            broker_exposure = {}

        gtt_by_id = {}
        active_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for item in all_gtts or []:
            item_id = str(item.get("id", "") or "")
            if item_id:
                gtt_by_id[item_id] = item
            if str(item.get("status", "") or "").lower() == "active":
                sym = str((item.get("condition", {}) or {}).get("tradingsymbol", "") or "").strip().upper()
                if sym:
                    active_by_symbol.setdefault(sym, []).append(item)

        for pos in open_positions:
            symbol = str(pos.symbol or "").strip().upper()
            product = self._normalize_product_type(getattr(pos, "product_type", ""))
            if product == "MIS":
                continue
            if float(getattr(pos, "stop_loss", 0) or 0) <= 0 or float(getattr(pos, "target2", 0) or 0) <= 0:
                continue
            broker_qty = int((broker_exposure.get(symbol) or {}).get("qty", 0) or 0)
            if broker_qty <= 0:
                self._mark_position_stale(symbol)
                self.update_position_gtt(
                    symbol,
                    status="not_held",
                    error="Symbol not present in broker holdings/positions; skipped GTT repair",
                )
                actions.append({
                    "symbol": symbol,
                    "action": "not_held",
                    "ok": False,
                    "message": "Skipped GTT audit because symbol is not present in broker portfolio",
                    "trigger_id": str(getattr(pos, "gtt_trigger_id", "") or ""),
                })
                continue

            # If T1 partial was booked on a swing position, the existing GTT still
            # covers the original (larger) qty. Cancel it and reissue for the
            # remaining qty before the normal "verified / repair" path runs.
            # This is not counted toward max_repairs — it is a deliberate reissue.
            if getattr(pos, "gtt_needs_reissue", False):
                old_id = str(getattr(pos, "gtt_trigger_id", "") or "")
                if old_id:
                    self.delete_gtt(old_id)
                    _log.info("%s: cancelled stale GTT %s (qty mismatch after T1 partial)", symbol, old_id)
                _new_sl = float(getattr(pos, "trailing_stop", 0) or 0)
                _orig_sl = float(getattr(pos, "stop_loss", 0) or 0)
                if _new_sl <= 0 or _new_sl < _orig_sl:
                    _new_sl = _orig_sl
                _live_price = self._latest_tick_price(symbol)
                _reissue = self.place_gtt_oco(
                    symbol=symbol,
                    qty=int(getattr(pos, "qty", 0) or 0),
                    buy_price=float(getattr(pos, "buy_price", 0) or 0),
                    stop_loss=_new_sl,
                    target=float(getattr(pos, "target2", 0) or 0),
                    product_type=product,
                    current_price=_live_price,
                )
                if _reissue.get("success"):
                    _new_id = str(_reissue.get("trigger_id", "") or "")
                    pos.gtt_needs_reissue = False
                    self.update_position_gtt(symbol, trigger_id=_new_id, status="active", expires_at="", error="")
                    self._save_positions()
                    actions.append({
                        "symbol": symbol,
                        "action": "reissued_after_partial",
                        "ok": True,
                        "message": f"GTT reissued for remaining {pos.qty} shares after T1 partial (ID {_new_id})",
                        "trigger_id": _new_id,
                    })
                else:
                    _err = str(_reissue.get("message", "") or "unknown error")
                    self.update_position_gtt(symbol, status="reissue_failed", error=_err)
                    actions.append({
                        "symbol": symbol,
                        "action": "reissue_failed",
                        "ok": False,
                        "message": f"GTT reissue after T1 partial failed: {_err}",
                        "trigger_id": old_id,
                    })
                continue

            trigger_id = str(getattr(pos, "gtt_trigger_id", "") or "")
            gtt_data = gtt_by_id.get(trigger_id) if trigger_id else {}
            if not gtt_data and trigger_id:
                gtt_data = self.get_gtt(trigger_id)

            if not gtt_data:
                same_symbol = sorted(
                    active_by_symbol.get(symbol, []),
                    key=lambda x: str(x.get("updated_at", "") or x.get("created_at", "") or x.get("id", "")),
                    reverse=True,
                )
                if same_symbol:
                    adopted = same_symbol[0]
                    adopted_id = str(adopted.get("id", "") or "")
                    adopted_status = str(adopted.get("status", "") or "").lower()
                    adopted_expiry = str(adopted.get("expires_at", "") or "")
                    self.update_position_gtt(
                        symbol,
                        trigger_id=adopted_id,
                        status=adopted_status,
                        expires_at=adopted_expiry,
                        error="",
                    )
                    actions.append({
                        "symbol": symbol,
                        "action": "adopted_existing",
                        "ok": True,
                        "message": f"Adopted already-active broker GTT (ID {adopted_id})",
                        "trigger_id": adopted_id,
                    })
                    continue

            status = str(gtt_data.get("status", "") or "").lower()
            expires_at = str(gtt_data.get("expires_at", "") or "")

            if gtt_data and status == "active":
                self.update_position_gtt(
                    symbol,
                    trigger_id=str(gtt_data.get("id", "") or trigger_id),
                    status=status,
                    expires_at=expires_at,
                    error="",
                )
                actions.append({
                    "symbol": symbol,
                    "action": "verified",
                    "ok": True,
                    "message": f"Active GTT verified (ID {str(gtt_data.get('id', '') or trigger_id)})",
                    "trigger_id": str(gtt_data.get("id", "") or trigger_id),
                })
                continue

            if repaired >= max_repairs:
                reason = status or "missing"
                self.update_position_gtt(symbol, status=reason, expires_at=expires_at, error=f"GTT {reason}; repair deferred by per-cycle cap")
                actions.append({
                    "symbol": symbol,
                    "action": "deferred",
                    "ok": False,
                    "message": f"GTT {reason}; deferred repair because max repairs reached",
                    "trigger_id": trigger_id,
                })
                continue

            _live_price_repair = self._latest_tick_price(symbol)
            # Use trailing_stop if it was raised above original SL (breakeven protection)
            _repair_sl = float(getattr(pos, "trailing_stop", 0) or 0)
            if _repair_sl <= 0 or _repair_sl < float(getattr(pos, "stop_loss", 0) or 0):
                _repair_sl = float(getattr(pos, "stop_loss", 0) or 0)
            recreate = self.place_gtt_oco(
                symbol=symbol,
                qty=int(getattr(pos, "qty", 0) or 0),
                buy_price=float(getattr(pos, "buy_price", 0) or 0),
                stop_loss=_repair_sl,
                target=float(getattr(pos, "target2", 0) or 0),
                product_type=product,
                current_price=_live_price_repair,
            )
            repaired += 1
            if recreate.get("success"):
                new_id = str(recreate.get("trigger_id", "") or "")
                self.update_position_gtt(
                    symbol,
                    trigger_id=new_id,
                    status="active",
                    expires_at="",
                    error="",
                )
                actions.append({
                    "symbol": symbol,
                    "action": "recreated",
                    "ok": True,
                    "message": f"GTT recreated successfully (ID {new_id})",
                    "trigger_id": new_id,
                })
            else:
                err = str(recreate.get("message", "") or "unknown error")
                reason = status or "missing"
                self.update_position_gtt(symbol, status=reason, expires_at=expires_at, error=err)
                actions.append({
                    "symbol": symbol,
                    "action": "repair_failed",
                    "ok": False,
                    "message": f"GTT {reason}; recreate failed: {err}",
                    "trigger_id": trigger_id,
                })

        return actions
