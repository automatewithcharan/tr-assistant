"""
Kite Connect order placement and position management.

Places real orders via the official Kite Connect API,
tracks open positions, monitors P&L, and generates sell signals
based on stop-loss, target, and performance criteria.
"""

from __future__ import annotations

import json
import logging
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


def _ist_now() -> datetime:
    try:
        import pytz
        return datetime.now(pytz.timezone("Asia/Kolkata"))
    except Exception:
        from datetime import timezone
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _is_market_open() -> bool:
    now = _ist_now()
    now_min = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= now_min <= 15 * 60 + 30


def _round_to_nse_tick(price: float) -> float:
    """Round a price UP to the nearest NSE tick size (₹0.05).

    NSE requires all order prices to be multiples of 0.05.
    Rounding up avoids placing limit orders below the intended level.
    Adapted from sdoosa-algo-trade-python.
    """
    import math
    if price <= 0:
        return 0.0
    return round(math.ceil(price * 20) / 20, 2)


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
    REVERSAL = "intraday_reversal"
    REGIME_BREAK = "cusum_regime_break"
    MANUAL = "manual_sell"


# ---------------------------------------------------------------------------
# Position record (persisted to JSON)
# ---------------------------------------------------------------------------

class TrackedPosition:
    """A position tracked from buy to sell."""

    def __init__(self, symbol: str, qty: int, buy_price: float,
                 buy_order_id: str = "", stop_loss: float = 0.0,
                 target1: float = 0.0, target2: float = 0.0,
                 blind_score: float = 0.0, reasons: str = ""):
        self.symbol = symbol
        self.qty = qty
        self.buy_price = buy_price
        self.buy_order_id = buy_order_id
        self.stop_loss = stop_loss
        self.target1 = target1
        self.target2 = target2
        self.blind_score = blind_score
        self.reasons = reasons
        self.buy_time = _ist_now().isoformat(timespec="seconds")
        self.highest_price = buy_price
        self.trailing_stop = 0.0
        self.t1_booked = False  # True after 50% sold at T1
        self.sell_order_id = ""
        self.sell_price = 0.0
        self.sell_time = ""
        self.sell_reason = ""
        self.status = "open"  # open | pending_sell | closed

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
        KITE_TRAILING_STOP_PCT=3.0      Trailing stop activation (% above buy)
        KITE_TRAILING_STOP_TRAIL=1.0    Trail distance (% below highest) — tighter for higher capture
        KITE_MAX_HOLD_DAYS=5            Max days to hold before time-decay exit
        KITE_DRY_RUN=0                  1=log orders without placing them
        KITE_PRODUCT_TYPE=MIS            MIS=intraday margin (5x leverage), CNC=delivery (full capital)
    """

    def __init__(self, output_dir: str = ""):
        self.output_dir = output_dir or Archiver.get_user_outputs_dir()
        self._positions_file = Path(self.output_dir) / "kite_tracked_positions.json"
        self._orders_log_file = Path(self.output_dir) / "kite_orders_log.csv"
        self._lock = threading.Lock()

        # Config from env
        self.auto_trade = _get_secret("KITE_AUTO_TRADE", "0") == "1"
        self.dry_run = _get_secret("KITE_DRY_RUN", "0") == "1"
        self.max_order_value = float(_get_secret("KITE_MAX_ORDER_VALUE", "5000"))
        self.max_daily_orders = int(_get_secret("KITE_MAX_DAILY_ORDERS", "10"))
        self.max_open_positions = int(_get_secret("KITE_MAX_OPEN_POSITIONS", "8"))
        self.trailing_stop_pct = float(_get_secret("KITE_TRAILING_STOP_PCT", "3.0"))
        self.trailing_trail_pct = float(_get_secret("KITE_TRAILING_STOP_TRAIL", "1.0"))
        self.max_hold_days = int(_get_secret("KITE_MAX_HOLD_DAYS", "5"))
        # Max positions from the same sector (0 = disabled)
        self.max_sector_concentration = int(_get_secret("KITE_MAX_SECTOR_POSITIONS", "3"))
        # MIS = intraday margin (5x leverage, auto-squared off at 3:20 PM by broker)
        # CNC = delivery (full capital, no auto-squareoff)
        _product = _get_secret("KITE_PRODUCT_TYPE", "MIS").strip().upper()
        self.product_type = _product if _product in ("MIS", "CNC", "NRML") else "MIS"

        self._positions: Dict[str, TrackedPosition] = {}
        self._daily_order_count = 0
        self._daily_order_date = ""
        self._client = None
        self._load_positions()

    # ------------------------------------------------------------------
    # Kite client
    # ------------------------------------------------------------------

    def _get_client(self):
        """Get or create authenticated KiteConnect client."""
        if self._client is not None:
            return self._client
        from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager
        mgr = KiteConnectSessionManager()
        self._client = mgr._build_client()
        return self._client

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

    def _log_order(self, result: OrderResult):
        """Append order to CSV log."""
        try:
            row = result.to_dict()
            df = pd.DataFrame([row])
            header = not self._orders_log_file.exists()
            df.to_csv(self._orders_log_file, mode="a", header=header, index=False)
        except Exception as exc:
            _log.warning("Failed to log order: %s", exc)

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
            quote = client.quote(f"NSE:{symbol}")
            data = quote.get(f"NSE:{symbol}", {})
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

        # Safety checks
        if not self.auto_trade:
            return OrderResult(False, message="Auto-trade is disabled (set KITE_AUTO_TRADE=1)")

        if not _is_market_open():
            return OrderResult(False, symbol=symbol, message="Market is closed")

        if not self._check_daily_limit():
            return OrderResult(
                False, symbol=symbol,
                message=f"Daily order limit reached ({self.max_daily_orders})"
            )

        open_count = sum(1 for p in self._positions.values() if p.status == "open")
        if open_count >= self.max_open_positions:
            return OrderResult(
                False, symbol=symbol,
                message=f"Max open positions reached ({self.max_open_positions})"
            )

        if symbol in self._positions and self._positions[symbol].status == "open":
            return OrderResult(False, symbol=symbol, message="Already holding this stock")

        # Circuit limit check — avoid orders on locked stocks
        circuit_blocked, circuit_reason = self._is_at_circuit_limit(symbol)
        if circuit_blocked:
            return OrderResult(
                False, symbol=symbol,
                message=f"Circuit limit: {circuit_reason}"
            )

        # Sector concentration check — avoid correlated positions
        sector_blocked, sector_reason = self._check_sector_concentration(symbol)
        if sector_blocked:
            return OrderResult(
                False, symbol=symbol,
                message=f"Blocked: {sector_reason}"
            )

        order_value = qty * (price if price > 0 else 0)
        if price > 0 and order_value > self.max_order_value * 1.1:
            return OrderResult(
                False, symbol=symbol,
                message=f"Order value ₹{order_value:,.0f} exceeds limit ₹{self.max_order_value:,.0f}"
            )

        if qty <= 0:
            return OrderResult(False, symbol=symbol, message="Invalid quantity")

        # Dry run mode
        if self.dry_run:
            _log.info("[DRY RUN] BUY %s x%d @ %s", symbol, qty, price or "MARKET")
            result = OrderResult(
                True, order_id=f"DRY-{int(time.time())}",
                symbol=symbol, side="BUY", qty=qty, price=price,
                status="COMPLETE", message="Dry run — no real order placed"
            )
            self._record_buy(result, stop_loss, target1, target2, blind_score, reasons)
            return result

        # Place real order via Kite
        try:
            client = self._get_client()
            kite_order_type = "MARKET" if order_type == "MARKET" or price <= 0 else "LIMIT"

            order_params = {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "transaction_type": "BUY",
                "quantity": qty,
                "order_type": kite_order_type,
                "product": self.product_type,  # MIS=intraday margin (5x), CNC=delivery
                "variety": "regular",
            }
            if kite_order_type == "LIMIT" and price > 0:
                order_params["price"] = _round_to_nse_tick(price)

            order_id = client.place_order(**order_params)
            order_id = str(order_id or "")

            _log.info("BUY order placed: %s x%d, order_id=%s", symbol, qty, order_id)

            result = OrderResult(
                True, order_id=order_id, symbol=symbol, side="BUY",
                qty=qty, price=price, status="PLACED",
                message=f"Order placed successfully (ID: {order_id})"
            )
            self._record_buy(result, stop_loss, target1, target2, blind_score, reasons)
            self._daily_order_count += 1
            return result

        except Exception as exc:
            _log.error("BUY order failed for %s: %s", symbol, exc)
            result = OrderResult(
                False, symbol=symbol, side="BUY", qty=qty, price=price,
                status="FAILED", message=str(exc)
            )
            self._log_order(result)
            return result

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

        pos = self._positions[symbol]
        sell_qty = qty if qty > 0 else pos.qty

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
            kite_order_type = "MARKET" if order_type == "MARKET" or price <= 0 else "LIMIT"

            order_params = {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "transaction_type": "SELL",
                "quantity": sell_qty,
                "order_type": kite_order_type,
                "product": self.product_type,
                "variety": "regular",
            }
            if kite_order_type == "LIMIT" and price > 0:
                order_params["price"] = _round_to_nse_tick(price)

            order_id = client.place_order(**order_params)
            order_id = str(order_id or "")

            _log.info("SELL order placed: %s x%d, order_id=%s — %s", symbol, sell_qty, order_id, reason)

            result = OrderResult(
                True, order_id=order_id, symbol=symbol, side="SELL",
                qty=sell_qty, price=price, status="PLACED",
                message=f"Sell order placed (ID: {order_id}) — {reason}"
            )
            self._record_sell(result, reason)
            return result

        except Exception as exc:
            _log.error("SELL order failed for %s: %s", symbol, exc)
            result = OrderResult(
                False, symbol=symbol, side="SELL", qty=sell_qty, price=price,
                status="FAILED", message=str(exc)
            )
            self._log_order(result)
            return result

    # ------------------------------------------------------------------
    # Record keeping
    # ------------------------------------------------------------------

    def _record_buy(self, result: OrderResult, stop_loss: float,
                    target1: float, target2: float, blind_score: float, reasons: str):
        with self._lock:
            pos = TrackedPosition(
                symbol=result.symbol, qty=result.qty,
                buy_price=result.price, buy_order_id=result.order_id,
                stop_loss=stop_loss, target1=target1, target2=target2,
                blind_score=blind_score, reasons=reasons,
            )
            self._positions[result.symbol] = pos
            self._save_positions()
        self._log_order(result)

    def _record_sell(self, result: OrderResult, reason: str):
        with self._lock:
            if result.symbol in self._positions:
                pos = self._positions[result.symbol]
                # Partial sell: reduce qty, keep position open
                if reason == SellReason.PARTIAL_T1 and result.qty < pos.qty:
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

    # ------------------------------------------------------------------
    # Position monitoring & sell signals
    # ------------------------------------------------------------------

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

            tick = ticks.get(sym, {})
            live_price = float(tick.get("last_price", 0))
            if live_price <= 0:
                continue

            # Update highest price for trailing stop
            if live_price > pos.highest_price:
                pos.highest_price = live_price

            pnl_pct = ((live_price - pos.buy_price) / pos.buy_price * 100) if pos.buy_price > 0 else 0

            # 1. Stop loss hit
            if pos.stop_loss > 0 and live_price <= pos.stop_loss:
                signals.append((sym, SellReason.STOP_LOSS, live_price, 0))
                continue

            # 2. Target 2 hit (max profit)
            if pos.target2 > 0 and live_price >= pos.target2:
                signals.append((sym, SellReason.TARGET_2, live_price, 0))
                continue

            # 3. Target 1 hit — sell 50% to lock in profits, trail the rest
            if pos.target1 > 0 and live_price >= pos.target1 and not pos.t1_booked:
                # Partial profit booking: sell half, trail the other half
                partial_qty = max(1, pos.qty // 2)
                signals.append((sym, SellReason.PARTIAL_T1, live_price, partial_qty))
                pos.t1_booked = True
                # Set trailing stop on remainder at breakeven (buy price)
                pos.trailing_stop = max(pos.buy_price, pos.target1 * (1 - self.trailing_trail_pct / 100))
                _log.info("%s hit T1 (₹%.2f): selling %d of %d, trailing rest from ₹%.2f",
                          sym, pos.target1, partial_qty, pos.qty, pos.trailing_stop)

            # 4. Trailing stop — activates after stock rises > trailing_stop_pct above buy
            if pos.trailing_stop <= 0 and pnl_pct >= self.trailing_stop_pct:
                pos.trailing_stop = pos.highest_price * (1 - self.trailing_trail_pct / 100)

            if pos.trailing_stop > 0:
                # Update trailing stop upward
                new_trail = pos.highest_price * (1 - self.trailing_trail_pct / 100)
                if new_trail > pos.trailing_stop:
                    pos.trailing_stop = new_trail
                if live_price <= pos.trailing_stop:
                    signals.append((sym, SellReason.TRAILING_STOP, live_price, 0))
                    continue

            # 5. Time decay — held too long without meaningful gain
            try:
                buy_date = datetime.fromisoformat(pos.buy_time).date()
                days_held = (now.date() - buy_date).days
                if days_held >= self.max_hold_days and pnl_pct < 2.0:
                    signals.append((sym, SellReason.TIME_DECAY, live_price, 0))
                    continue
            except Exception:
                pass

            # 6. Intraday reversal — stock was up >3% but reversed to negative
            if pnl_pct < -1.0 and pos.highest_price > pos.buy_price * 1.03:
                signals.append((sym, SellReason.REVERSAL, live_price, 0))
                continue

            # 7. CUSUM regime break — detect intraday structural shifts
            #    Uses recent 5-min candle returns to identify when momentum reverses
            #    Threshold 0.05 tuned via backtest sweep (0.02 was too trigger-happy)
            tick_data = tick.get("depth", {})
            ohlc = tick.get("ohlc", {})
            if ohlc and pnl_pct > 1.5:  # only check when solidly profitable (>1.5%)
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
        signals = self.check_sell_signals(ticks)
        results = []

        for sym, reason, price, qty in signals:
            result = self.place_sell_order(symbol=sym, qty=qty, reason=reason)
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
    ) -> OrderResult:
        """Auto-buy a stock from a blind-buy candidate alert.

        Uses the same position sizing logic as the alerting engine so execution
        matches the quantity shown to the user.
        """
        if live_price <= 0:
            return OrderResult(False, symbol=symbol, message="Invalid price")

        try:
            from pkscreener.classes.AutoPilotInvestor import _compute_suggested_qty

            qty, _ = _compute_suggested_qty(
                live_price,
                stop_loss=stop_loss,
                score=blind_score,
                max_position=self.max_order_value,
            )
        except Exception:
            qty = int(self.max_order_value // live_price) if live_price > 0 else 0
        if qty <= 0:
            return OrderResult(
                False,
                symbol=symbol,
                message=f"Starter position exceeds limit ₹{self.max_order_value:,.0f}",
            )
        return self.place_buy_order(
            symbol=symbol, qty=qty, price=0.0,  # market order
            stop_loss=stop_loss, target1=target1, target2=target2,
            blind_score=blind_score, reasons=reasons,
            order_type="MARKET",
        )

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
            lines.append(
                f"{emoji} `{p.symbol}` x{p.qty} | buy ₹{p.buy_price:.0f} → ₹{live:.0f} "
                f"| P/L ₹{pnl:+,.0f} ({pnl_pct:+.1f}%)"
                f" | SL ₹{p.stop_loss:.0f}{trail_info}"
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
            SellReason.REVERSAL: "🔻 Intraday reversal detected",
            SellReason.REGIME_BREAK: "📉 CUSUM regime break — momentum reversed",
            SellReason.MANUAL: "👤 Manual sell",
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

        Returns a report of any discrepancies.
        """
        try:
            client = self._get_client()
            kite_positions = client.positions() or {}
            net_positions = kite_positions.get("net", []) if isinstance(kite_positions, dict) else []
        except Exception as exc:
            return {"error": str(exc), "reconciled": False}

        broker_holdings = {}
        for p in net_positions:
            sym = str(p.get("tradingsymbol", "")).strip().upper()
            qty = int(p.get("quantity", 0))
            if sym and qty != 0:
                broker_holdings[sym] = {
                    "qty": qty,
                    "avg_price": float(p.get("average_price", 0)),
                    "last_price": float(p.get("last_price", 0)),
                    "pnl": float(p.get("pnl", 0)),
                }

        report = {"broker_positions": len(broker_holdings), "tracked_positions": 0,
                  "mismatches": [], "untracked": [], "reconciled": True}

        tracked_open = {sym: pos for sym, pos in self._positions.items() if pos.status == "open"}
        report["tracked_positions"] = len(tracked_open)

        for sym, pos in tracked_open.items():
            bp = broker_holdings.get(sym)
            if bp is None:
                report["mismatches"].append(f"{sym}: tracked but not in broker")
            elif bp["qty"] != pos.qty:
                report["mismatches"].append(
                    f"{sym}: tracked qty={pos.qty}, broker qty={bp['qty']}"
                )

        for sym in broker_holdings:
            if sym not in tracked_open:
                report["untracked"].append(sym)

        return report

    # ------------------------------------------------------------------
    # Order status check
    # ------------------------------------------------------------------

    def check_order_status(self, order_id: str) -> Dict[str, Any]:
        """Check the status of a placed order."""
        try:
            client = self._get_client()
            order_history = client.order_history(order_id)
            if order_history:
                latest = order_history[-1]
                return {
                    "order_id": order_id,
                    "status": latest.get("status", "UNKNOWN"),
                    "filled_qty": latest.get("filled_quantity", 0),
                    "avg_price": latest.get("average_price", 0),
                    "pending_qty": latest.get("pending_quantity", 0),
                }
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
    ) -> Dict[str, Any]:
        """Place a Kite GTT OCO (One-Cancels-Other) order for stop-loss AND target.

        This puts TWO SELL triggers on Kite simultaneously:
          - If price drops to stop_loss → SELL at market
          - If price rises to target    → SELL at limit (target price)
        Whichever fires first cancels the other.

        Args:
            symbol:     NSE trading symbol (e.g. RELIANCE)
            qty:        Shares to sell (must match your holding)
            buy_price:  Your entry price (used as Kite's last_price reference)
            stop_loss:  SL trigger price (below buy_price)
            target:     Profit target price (above buy_price)
            exchange:   Usually "NSE" (default)

        Returns:
            dict with keys: success (bool), trigger_id (str), message (str)
        """
        symbol = symbol.strip().upper()
        if stop_loss <= 0 or target <= 0:
            return {"success": False, "message": "stop_loss and target must both be > 0"}
        if stop_loss >= buy_price:
            return {"success": False, "message": f"stop_loss {stop_loss} must be below buy_price {buy_price}"}
        if target <= buy_price:
            return {"success": False, "message": f"target {target} must be above buy_price {buy_price}"}

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
            # Kite OCO: trigger_values = [stop_loss, target] (ascending order required by API)
            trigger_id = client.place_gtt(
                trigger_type=client.GTT_TYPE_OCO,
                tradingsymbol=symbol,
                exchange=exchange,
                trigger_values=[_round_to_nse_tick(stop_loss), _round_to_nse_tick(target)],
                last_price=_round_to_nse_tick(buy_price),
                orders=[
                    # Leg 1: stop-loss — fires when price <= stop_loss
                    {
                        "transaction_type": "SELL",
                        "quantity": qty,
                        "order_type": "MARKET",
                        "product": self.product_type,
                    },
                    # Leg 2: target — fires when price >= target
                    {
                        "transaction_type": "SELL",
                        "quantity": qty,
                        "order_type": "LIMIT",
                        "product": self.product_type,
                        "price": _round_to_nse_tick(target),
                    },
                ],
            )
            trigger_id = str(trigger_id or "")
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
