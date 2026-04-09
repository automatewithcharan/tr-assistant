"""
Local paper-trading engine for PKScreener actionable signals.

This module keeps a simple long-only paper account on disk so repeated runs can:
- open new paper positions from high-confidence BUY signals
- update held positions with the latest signal snapshot
- close positions on stop-loss, target, or sell-side signal changes
"""

import html
import os
import subprocess
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import requests

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

from pkscreener.classes.ActionableReport import ActionableReportRunner


class PaperTradingRunner:
    """Simple local paper-trading account driven by actionable PKScreener signals."""

    CASHFLOW_COLUMNS = [
        "When",
        "Type",
        "Amount",
        "Note",
    ]

    TRADE_COLUMNS = [
        "TradeId",
        "Universe",
        "Symbol",
        "Side",
        "Status",
        "EntryDate",
        "EntryPrice",
        "Quantity",
        "InvestedAmount",
        "StopLoss",
        "Target1",
        "Target2",
        "EntrySignal",
        "EntryConfidencePct",
        "EntryReason",
        "CurrentPrice",
        "CurrentValue",
        "UnrealizedPnL",
        "UnrealizedReturnPct",
        "LastAction",
        "LastSignal",
        "LastConfidencePct",
        "LastReason",
        "LastUpdatedAt",
        "ExitDate",
        "ExitPrice",
        "ExitValue",
        "ExitReason",
        "RealizedPnL",
        "RealizedReturnPct",
    ]

    NUMERIC_COLUMNS = [
        "TradeId",
        "EntryPrice",
        "Quantity",
        "InvestedAmount",
        "StopLoss",
        "Target1",
        "Target2",
        "EntryConfidencePct",
        "CurrentPrice",
        "CurrentValue",
        "UnrealizedPnL",
        "UnrealizedReturnPct",
        "LastConfidencePct",
        "ExitPrice",
        "ExitValue",
        "RealizedPnL",
        "RealizedReturnPct",
    ]

    def __init__(
        self,
        config_manager,
        universe: str = "nifty50",
        limit: int = 50,
        initial_capital: float = 100000.0,
        max_positions: int = 5,
        min_confidence: float = 15.0,
        reset_account: bool = False,
        add_cash_amount: float = 0.0,
        alert_channels: str = "none",
        manual_buys: Optional[List[Dict]] = None,
        manual_sells: Optional[List[Dict]] = None,
        risk_percent: float = 1.0,
        min_trade_value: float = 10000000.0,
        min_volume_ratio: float = 1.0,
        max_workers: int = 3,
        use_live_kite: bool = False,
    ):
        self.config_manager = config_manager
        self.universe = universe
        self.limit = limit
        self.initial_capital = float(initial_capital)
        self.max_positions = max(1, int(max_positions))
        self.min_confidence = float(min_confidence)
        self.reset_account = bool(reset_account)
        self.add_cash_amount = float(add_cash_amount or 0.0)
        self.alert_channels = self._normalize_alert_channels(alert_channels)
        self.manual_buys = manual_buys or []
        self.manual_sells = manual_sells or []
        self.risk_percent = float(risk_percent)
        self.min_trade_value = float(min_trade_value)
        self.min_volume_ratio = float(min_volume_ratio)
        self.max_workers = max(1, int(max_workers or 1))
        self.estimated_buy_cost_pct = self._load_charge_pct(
            env_name="CHARAN_EST_BUY_COST_PCT",
            default=0.00118,
        )
        self.estimated_sell_cost_pct = self._load_charge_pct(
            env_name="CHARAN_EST_SELL_COST_PCT",
            default=0.00104,
        )
        self.output_dir = Archiver.get_user_data_dir()
        self.run_at = PKDateUtilities.currentDateTime()
        self.run_at_str = self.run_at.strftime("%Y-%m-%d %H:%M:%S")
        self.run_date = self.run_at.strftime("%Y-%m-%d")
        self.timestamp = self.run_at.strftime("%Y%m%d_%H%M%S")
        self.actionable_runner = ActionableReportRunner(
            config_manager=config_manager,
            universe=universe,
            limit=limit,
            min_confidence=min_confidence,
            risk_percent=risk_percent,
            min_trade_value=min_trade_value,
            min_volume_ratio=min_volume_ratio,
            max_workers=max_workers,
            use_live_kite=use_live_kite,
        )
        self.initial_capital = self._load_initial_capital(default=self.initial_capital)

    def run(self) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Run a complete paper-trading cycle and persist results."""
        trades_df = self._load_trades()
        cashflows_df = self._load_cashflows()
        cashflows_df, cashflow_events = self._apply_cash_adjustment(cashflows_df)
        symbols_to_scan = self._build_scan_symbols(trades_df)
        OutputControls().printOutput(
            f"  [+] Updating paper account from {len(symbols_to_scan)} symbols in {self.universe}..."
        )
        report_df, _ = self.actionable_runner.build_report(symbols=symbols_to_scan)
        if report_df is None:
            report_df = pd.DataFrame(
                columns=[
                    "Symbol", "Action", "Signal", "ConfidencePct", "LTP",
                    "Entry", "StopLoss", "Target1", "Target2", "Reason", "RunAt"
                ]
            )

        market_map = {
            str(row["Symbol"]).strip().upper(): row
            for _, row in report_df.iterrows()
        }

        trades_df, exit_events, recently_closed = self._update_open_positions(
            trades_df, market_map, cashflows_df
        )
        trades_df, manual_sell_events = self._apply_manual_sells(trades_df)
        trades_df, manual_buy_events = self._apply_manual_buys(trades_df, market_map, cashflows_df)
        trades_df, entry_events = self._open_new_positions(
            trades_df, report_df, cashflows_df, exclude_symbols=recently_closed
        )
        trades_df = self._recalculate_trade_pnl(trades_df)

        positions_df = self._build_positions_snapshot(trades_df)
        summary_df = self._build_summary(trades_df, cashflows_df)

        self._save_state(report_df, trades_df, cashflows_df, positions_df, summary_df)
        all_entry_events = manual_buy_events + entry_events
        all_exit_events = manual_sell_events + exit_events
        self._print_summary(summary_df, positions_df, cashflow_events, all_entry_events, all_exit_events)
        self._send_alerts(cashflow_events, all_entry_events, all_exit_events, summary_df)
        return trades_df, positions_df, summary_df

    def _build_scan_symbols(self, trades_df: pd.DataFrame) -> List[str]:
        """Scan the candidate universe plus any currently open positions."""
        base_symbols = self.actionable_runner.get_universe_symbols()
        if self.limit and self.limit > 0:
            base_symbols = base_symbols[: self.limit]

        open_symbols = [
            str(symbol).strip().upper()
            for symbol in trades_df.loc[trades_df["Status"] == "OPEN", "Symbol"].tolist()
            if str(symbol).strip()
        ]
        return self._unique_symbols(base_symbols + open_symbols)

    def _load_initial_capital(self, default: float) -> float:
        """Keep the first configured paper capital stable across later runs."""
        if self.reset_account:
            return float(default)
        summary_path = self._latest_path("summary")
        if os.path.exists(summary_path):
            try:
                summary_df = pd.read_csv(summary_path)
                if "InitialCapital" in summary_df.columns and len(summary_df) > 0:
                    value = float(summary_df.iloc[0]["InitialCapital"])
                    if value > 0:
                        return value
            except Exception as exc:
                default_logger().debug(f"Failed loading paper capital: {exc}", exc_info=True)
        return float(default)

    def _load_trades(self) -> pd.DataFrame:
        """Load or initialize the persistent trade ledger."""
        if self.reset_account:
            return pd.DataFrame(columns=self.TRADE_COLUMNS)
        path = self._latest_path("trades")
        if os.path.exists(path):
            try:
                trades_df = pd.read_csv(path)
            except Exception as exc:
                default_logger().debug(f"Failed loading paper trades: {exc}", exc_info=True)
                trades_df = pd.DataFrame(columns=self.TRADE_COLUMNS)
        else:
            trades_df = pd.DataFrame(columns=self.TRADE_COLUMNS)

        for column in self.TRADE_COLUMNS:
            if column not in trades_df.columns:
                trades_df[column] = 0 if column in self.NUMERIC_COLUMNS else ""

        for column in self.NUMERIC_COLUMNS:
            trades_df[column] = pd.to_numeric(trades_df[column], errors="coerce").fillna(0.0)

        string_columns = [column for column in self.TRADE_COLUMNS if column not in self.NUMERIC_COLUMNS]
        for column in string_columns:
            trades_df[column] = trades_df[column].fillna("").astype(str)

        if len(trades_df) > 0:
            trades_df["TradeId"] = trades_df["TradeId"].astype(int)
        return trades_df[self.TRADE_COLUMNS].copy()

    def _load_cashflows(self) -> pd.DataFrame:
        """Load or initialize the cashflow ledger."""
        if self.reset_account:
            return pd.DataFrame(columns=self.CASHFLOW_COLUMNS)

        path = self._latest_path("cashflows")
        if os.path.exists(path):
            try:
                cashflows_df = pd.read_csv(path)
            except Exception as exc:
                default_logger().debug(f"Failed loading paper cashflows: {exc}", exc_info=True)
                cashflows_df = pd.DataFrame(columns=self.CASHFLOW_COLUMNS)
        else:
            cashflows_df = pd.DataFrame(columns=self.CASHFLOW_COLUMNS)

        for column in self.CASHFLOW_COLUMNS:
            if column not in cashflows_df.columns:
                cashflows_df[column] = ""
        cashflows_df["Amount"] = pd.to_numeric(cashflows_df["Amount"], errors="coerce").fillna(0.0)
        for column in ["When", "Type", "Note"]:
            cashflows_df[column] = cashflows_df[column].fillna("").astype(str)
        return cashflows_df[self.CASHFLOW_COLUMNS].copy()

    def _apply_cash_adjustment(self, cashflows_df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
        """Apply any new user-requested cash top-up for this run."""
        events: List[str] = []
        if self.add_cash_amount > 0:
            cashflows_df = pd.concat(
                [
                    cashflows_df,
                    pd.DataFrame(
                        [
                            {
                                "When": self.run_at_str,
                                "Type": "ADD_CASH",
                                "Amount": round(self.add_cash_amount, 2),
                                "Note": "Manual cash top-up",
                            }
                        ],
                        columns=self.CASHFLOW_COLUMNS,
                    ),
                ],
                axis=0,
                ignore_index=True,
            )
            events.append(f"Added cash: {self.add_cash_amount:.2f}")
        return cashflows_df[self.CASHFLOW_COLUMNS].copy(), events

    def _update_open_positions(
        self, trades_df: pd.DataFrame, market_map: Dict[str, pd.Series], cashflows_df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, List[str], Set[str]]:
        """Refresh open positions and close them if stops, targets, or sell signals fire."""
        exit_events: List[str] = []
        recently_closed: Set[str] = set()
        open_indices = trades_df.index[trades_df["Status"] == "OPEN"].tolist()

        for index in open_indices:
            trade = trades_df.loc[index]
            symbol = str(trade["Symbol"]).strip().upper()
            market_row = market_map.get(symbol)
            if market_row is None:
                continue

            quantity = int(self._to_float(trade["Quantity"]))
            entry_price = self._to_float(trade["EntryPrice"])
            invested_amount = self._to_float(trade["InvestedAmount"])
            current_price = self._to_float(market_row.get("LTP"), fallback=entry_price)
            current_value = round(current_price * quantity, 2)
            unrealized_pnl = round(current_value - invested_amount, 2)
            unrealized_return = (
                round((unrealized_pnl * 100) / invested_amount, 2)
                if invested_amount > 0
                else 0.0
            )

            trades_df.loc[index, "CurrentPrice"] = current_price
            trades_df.loc[index, "CurrentValue"] = current_value
            trades_df.loc[index, "UnrealizedPnL"] = unrealized_pnl
            trades_df.loc[index, "UnrealizedReturnPct"] = unrealized_return
            trades_df.loc[index, "LastAction"] = str(
                market_row.get("NowAction", market_row.get("Action", ""))
            ).strip()
            trades_df.loc[index, "LastSignal"] = str(market_row.get("Signal", "")).strip()
            trades_df.loc[index, "LastConfidencePct"] = self._to_float(
                market_row.get("ConfidencePct")
            )
            trades_df.loc[index, "LastReason"] = str(market_row.get("Reason", "")).strip()
            trades_df.loc[index, "LastUpdatedAt"] = self.run_at_str

            stop_loss = self._to_float(trade["StopLoss"])
            target_1 = self._to_float(trade["Target1"])
            target_2 = self._to_float(trade["Target2"])
            current_action = str(
                market_row.get("NowAction", market_row.get("Action", ""))
            ).strip().upper()

            exit_reason = None
            if stop_loss > 0 and current_price <= stop_loss:
                exit_reason = f"Stop loss hit ({stop_loss:.2f})"
            elif target_2 > 0 and current_price >= target_2:
                exit_reason = f"Target 2 hit ({target_2:.2f})"
            elif target_1 > 0 and current_price >= target_1:
                exit_reason = f"Target 1 hit ({target_1:.2f})"
            elif current_action in ["SELL/EXIT", "WATCH SELL"]:
                exit_reason = f"{current_action} signal"

            if exit_reason is None:
                continue

            realized_return = (
                round((unrealized_pnl * 100) / invested_amount, 2)
                if invested_amount > 0
                else 0.0
            )
            trades_df.loc[index, "Status"] = "CLOSED"
            trades_df.loc[index, "ExitDate"] = self.run_date
            trades_df.loc[index, "ExitPrice"] = current_price
            trades_df.loc[index, "ExitValue"] = current_value
            trades_df.loc[index, "ExitReason"] = exit_reason
            trades_df.loc[index, "RealizedPnL"] = unrealized_pnl
            trades_df.loc[index, "RealizedReturnPct"] = realized_return
            trades_df.loc[index, "CurrentValue"] = 0.0
            trades_df.loc[index, "UnrealizedPnL"] = 0.0
            trades_df.loc[index, "UnrealizedReturnPct"] = 0.0

            exit_events.append(
                f"EXIT {symbol} x{quantity} at {current_price:.2f} | {exit_reason}"
            )
            recently_closed.add(symbol)

        return trades_df, exit_events, recently_closed

    def _open_new_positions(
        self,
        trades_df: pd.DataFrame,
        report_df: pd.DataFrame,
        cashflows_df: pd.DataFrame,
        exclude_symbols: Optional[Set[str]] = None,
    ) -> Tuple[pd.DataFrame, List[str]]:
        """Open new paper positions from current high-confidence BUY signals."""
        entry_events: List[str] = []
        exclude_symbols = exclude_symbols or set()
        open_symbols = set(
            trades_df.loc[trades_df["Status"] == "OPEN", "Symbol"].astype(str).str.upper().tolist()
        )
        slots_remaining = self.max_positions - len(open_symbols)
        cash_remaining = self._compute_cash(trades_df, cashflows_df)

        if slots_remaining <= 0 or cash_remaining <= 0:
            return trades_df, entry_events

        candidates = report_df[
            (report_df["NowAction"] == "Buy Now")
            & (report_df["ConfidencePct"] >= self.min_confidence)
        ].copy()
        if candidates.empty:
            return trades_df, entry_events

        next_trade_id = self._next_trade_id(trades_df)
        new_rows = []

        for _, row in candidates.iterrows():
            symbol = str(row["Symbol"]).strip().upper()
            if symbol in open_symbols or symbol in exclude_symbols:
                continue
            if slots_remaining <= 0 or cash_remaining <= 0:
                break

            entry_price = max(self._to_float(row.get("Entry")), 0.01)
            current_price = self._to_float(row.get("LTP"), fallback=entry_price)
            if current_price < entry_price:
                continue

            allocation = cash_remaining / slots_remaining
            per_share_cost = entry_price * (1.0 + self.estimated_buy_cost_pct)
            affordable_qty = int(allocation // max(per_share_cost, 0.01))
            risk_per_share = max(self._to_float(row.get("RiskPerShare")), 0.01)
            risk_capital = max((cash_remaining * self.risk_percent / 100.0), 0.0)
            risk_qty = int(risk_capital // risk_per_share) if risk_per_share > 0 else affordable_qty
            quantity = min(affordable_qty, risk_qty if risk_qty > 0 else affordable_qty)
            if quantity < 1:
                continue

            invested_amount = round(entry_price * quantity, 2)
            entry_charges = self._estimate_buy_charges(invested_amount)
            total_entry_debit = round(invested_amount + entry_charges, 2)
            if total_entry_debit > cash_remaining:
                continue
            current_value = round(current_price * quantity, 2)
            unrealized_pnl = round(current_value - invested_amount, 2)
            unrealized_return = (
                round((unrealized_pnl * 100) / invested_amount, 2)
                if invested_amount > 0
                else 0.0
            )

            new_rows.append(
                {
                    "TradeId": next_trade_id,
                    "Universe": self.universe,
                    "Symbol": symbol,
                    "Side": "LONG",
                    "Status": "OPEN",
                    "EntryDate": self.run_date,
                    "EntryPrice": entry_price,
                    "Quantity": quantity,
                    "InvestedAmount": invested_amount,
                    "StopLoss": self._to_float(row.get("StopLoss")),
                    "Target1": self._to_float(row.get("Target1")),
                    "Target2": self._to_float(row.get("Target2")),
                    "EntrySignal": str(row.get("Signal", "")).strip(),
                    "EntryConfidencePct": self._to_float(row.get("ConfidencePct")),
                    "EntryReason": str(row.get("Reason", "")).strip(),
                    "CurrentPrice": current_price,
                    "CurrentValue": current_value,
                    "UnrealizedPnL": unrealized_pnl,
                    "UnrealizedReturnPct": unrealized_return,
                    "LastAction": str(row.get("NowAction", row.get("Action", ""))).strip(),
                    "LastSignal": str(row.get("Signal", "")).strip(),
                    "LastConfidencePct": self._to_float(row.get("ConfidencePct")),
                    "LastReason": str(row.get("Reason", "")).strip(),
                    "LastUpdatedAt": self.run_at_str,
                    "ExitDate": "",
                    "ExitPrice": 0.0,
                    "ExitValue": 0.0,
                    "ExitReason": "",
                    "RealizedPnL": 0.0,
                    "RealizedReturnPct": 0.0,
                }
            )

            entry_events.append(
                f"BUY {symbol} x{quantity} at {entry_price:.2f} | stop {self._to_float(row.get('StopLoss')):.2f} | risk {risk_capital:.2f} | est charges {entry_charges:.2f}"
            )
            next_trade_id += 1
            cash_remaining = round(cash_remaining - total_entry_debit, 2)
            slots_remaining -= 1
            open_symbols.add(symbol)

        if new_rows:
            new_df = pd.DataFrame(new_rows, columns=self.TRADE_COLUMNS)
            trades_df = pd.concat([trades_df, new_df], axis=0, ignore_index=True)

        return trades_df[self.TRADE_COLUMNS].copy(), entry_events

    def _apply_manual_buys(
        self,
        trades_df: pd.DataFrame,
        market_map: Dict[str, pd.Series],
        cashflows_df: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, List[str]]:
        """Add user-entered buy trades to the paper account."""
        events: List[str] = []
        if not self.manual_buys:
            return trades_df, events

        next_trade_id = self._next_trade_id(trades_df)
        new_rows = []
        cash_remaining = self._compute_cash(trades_df, cashflows_df)

        for order in self.manual_buys:
            symbol = str(order.get("symbol", "")).strip().upper()
            quantity = int(order.get("quantity", 0) or 0)
            entry_price = max(self._to_float(order.get("price")), 0.01)
            invested_amount = round(quantity * entry_price, 2)
            entry_charges = self._estimate_buy_charges(invested_amount)
            total_entry_debit = round(invested_amount + entry_charges, 2)

            if not symbol or quantity <= 0:
                continue
            if total_entry_debit > cash_remaining:
                events.append(f"Skipped manual buy for {symbol}: not enough cash")
                continue

            market_row = market_map.get(symbol)
            current_price = self._to_float(
                market_row.get("LTP") if market_row is not None else entry_price,
                fallback=entry_price,
            )
            current_value = round(current_price * quantity, 2)
            unrealized_pnl = round(current_value - invested_amount, 2)
            unrealized_return = (
                round((unrealized_pnl * 100) / invested_amount, 2)
                if invested_amount > 0
                else 0.0
            )

            new_rows.append(
                {
                    "TradeId": next_trade_id,
                    "Universe": self.universe,
                    "Symbol": symbol,
                    "Side": "LONG",
                    "Status": "OPEN",
                    "EntryDate": self.run_date,
                    "EntryPrice": entry_price,
                    "Quantity": quantity,
                    "InvestedAmount": invested_amount,
                    "StopLoss": self._to_float(market_row.get("StopLoss")) if market_row is not None else 0.0,
                    "Target1": self._to_float(market_row.get("Target1")) if market_row is not None else 0.0,
                    "Target2": self._to_float(market_row.get("Target2")) if market_row is not None else 0.0,
                    "EntrySignal": str(market_row.get("Signal", "MANUAL BUY")).strip() if market_row is not None else "MANUAL BUY",
                    "EntryConfidencePct": self._to_float(market_row.get("ConfidencePct")) if market_row is not None else 0.0,
                    "EntryReason": "Manual buy entry",
                    "CurrentPrice": current_price,
                    "CurrentValue": current_value,
                    "UnrealizedPnL": unrealized_pnl,
                    "UnrealizedReturnPct": unrealized_return,
                    "LastAction": str(market_row.get("NowAction", market_row.get("Action", "MANUAL BUY"))).strip() if market_row is not None else "MANUAL BUY",
                    "LastSignal": str(market_row.get("Signal", "MANUAL BUY")).strip() if market_row is not None else "MANUAL BUY",
                    "LastConfidencePct": self._to_float(market_row.get("ConfidencePct")) if market_row is not None else 0.0,
                    "LastReason": "Manual buy entry",
                    "LastUpdatedAt": self.run_at_str,
                    "ExitDate": "",
                    "ExitPrice": 0.0,
                    "ExitValue": 0.0,
                    "ExitReason": "",
                    "RealizedPnL": 0.0,
                    "RealizedReturnPct": 0.0,
                }
            )

            events.append(f"Manual BUY {symbol} x{quantity} at {entry_price:.2f} | est charges {entry_charges:.2f}")
            next_trade_id += 1
            cash_remaining = round(cash_remaining - total_entry_debit, 2)

        if new_rows:
            trades_df = pd.concat(
                [trades_df, pd.DataFrame(new_rows, columns=self.TRADE_COLUMNS)],
                axis=0,
                ignore_index=True,
            )
        return trades_df[self.TRADE_COLUMNS].copy(), events

    def _apply_manual_sells(self, trades_df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
        """Apply user-entered sell orders to existing open paper positions."""
        events: List[str] = []
        if not self.manual_sells or trades_df is None or trades_df.empty:
            return trades_df, events

        trades_df = trades_df.copy()
        next_trade_id = self._next_trade_id(trades_df)
        new_rows = []

        for order in self.manual_sells:
            symbol = str(order.get("symbol", "")).strip().upper()
            requested_qty = int(order.get("quantity", 0) or 0)
            exit_price = self._to_float(order.get("price"))
            if not symbol or requested_qty <= 0 or exit_price <= 0:
                continue

            open_indices = trades_df.index[
                (trades_df["Status"] == "OPEN")
                & (trades_df["Symbol"].astype(str).str.upper() == symbol)
            ].tolist()
            remaining = requested_qty

            if not open_indices:
                events.append(f"Skipped manual sell for {symbol}: no open shares")
                continue

            for index in open_indices:
                if remaining <= 0:
                    break
                trade = trades_df.loc[index].copy()
                available_qty = int(self._to_float(trade["Quantity"]))
                sell_qty = min(available_qty, remaining)
                if sell_qty <= 0:
                    continue

                closed_trade = self._create_closed_trade_record(
                    trade=trade,
                    sell_qty=sell_qty,
                    exit_price=exit_price,
                    exit_reason="Manual sell entry",
                    trade_id=next_trade_id,
                )

                if sell_qty >= available_qty:
                    for key, value in closed_trade.items():
                        trades_df.loc[index, key] = value
                    next_trade_id += 1
                else:
                    remaining_qty = available_qty - sell_qty
                    entry_price = self._to_float(trade["EntryPrice"])
                    current_price = self._to_float(trade.get("CurrentPrice"), fallback=entry_price)
                    invested_amount = round(entry_price * remaining_qty, 2)
                    current_value = round(current_price * remaining_qty, 2)
                    unrealized_pnl = round(current_value - invested_amount, 2)
                    unrealized_return = (
                        round((unrealized_pnl * 100) / invested_amount, 2)
                        if invested_amount > 0
                        else 0.0
                    )

                    trades_df.loc[index, "Quantity"] = remaining_qty
                    trades_df.loc[index, "InvestedAmount"] = invested_amount
                    trades_df.loc[index, "CurrentValue"] = current_value
                    trades_df.loc[index, "UnrealizedPnL"] = unrealized_pnl
                    trades_df.loc[index, "UnrealizedReturnPct"] = unrealized_return
                    trades_df.loc[index, "LastUpdatedAt"] = self.run_at_str
                    new_rows.append(closed_trade)
                    next_trade_id += 1

                remaining -= sell_qty
                events.append(
                    f"Manual SELL {symbol} x{sell_qty} at {exit_price:.2f} | est charges {self._estimate_sell_charges(exit_price * sell_qty):.2f}"
                )

            if remaining > 0:
                events.append(f"Could not sell full quantity for {symbol}. Pending qty: {remaining}")

        if new_rows:
            trades_df = pd.concat(
                [trades_df, pd.DataFrame(new_rows, columns=self.TRADE_COLUMNS)],
                axis=0,
                ignore_index=True,
            )

        return trades_df[self.TRADE_COLUMNS].copy(), events

    def _create_closed_trade_record(
        self,
        trade: pd.Series,
        sell_qty: int,
        exit_price: float,
        exit_reason: str,
        trade_id: int,
    ) -> Dict:
        """Create a closed-trade record from an existing open trade row."""
        entry_price = self._to_float(trade["EntryPrice"])
        invested_amount = round(entry_price * sell_qty, 2)
        exit_value = round(exit_price * sell_qty, 2)
        realized_pnl = round(exit_value - invested_amount, 2)
        realized_return = (
            round((realized_pnl * 100) / invested_amount, 2)
            if invested_amount > 0
            else 0.0
        )

        closed_trade = trade.to_dict()
        closed_trade["TradeId"] = trade_id
        closed_trade["Status"] = "CLOSED"
        closed_trade["Quantity"] = sell_qty
        closed_trade["InvestedAmount"] = invested_amount
        closed_trade["CurrentPrice"] = exit_price
        closed_trade["CurrentValue"] = 0.0
        closed_trade["UnrealizedPnL"] = 0.0
        closed_trade["UnrealizedReturnPct"] = 0.0
        closed_trade["LastAction"] = "MANUAL SELL"
        closed_trade["LastSignal"] = "MANUAL SELL"
        closed_trade["LastReason"] = exit_reason
        closed_trade["LastUpdatedAt"] = self.run_at_str
        closed_trade["ExitDate"] = self.run_date
        closed_trade["ExitPrice"] = exit_price
        closed_trade["ExitValue"] = exit_value
        closed_trade["ExitReason"] = exit_reason
        closed_trade["RealizedPnL"] = realized_pnl
        closed_trade["RealizedReturnPct"] = realized_return
        return {column: closed_trade.get(column, "") for column in self.TRADE_COLUMNS}

    def _build_positions_snapshot(self, trades_df: pd.DataFrame) -> pd.DataFrame:
        """Create a focused open-position view for quick review."""
        positions_df = trades_df[trades_df["Status"] == "OPEN"].copy()
        if positions_df.empty:
            return positions_df

        positions_df.sort_values(
            by=["EntryConfidencePct", "UnrealizedPnL", "Symbol"],
            ascending=[False, False, True],
            inplace=True,
        )
        columns = [
            "TradeId",
            "Symbol",
            "EntryDate",
            "Quantity",
            "EntryPrice",
            "InvestedAmount",
            "CurrentPrice",
            "CurrentValue",
            "StopLoss",
            "Target1",
            "Target2",
            "UnrealizedPnL",
            "UnrealizedReturnPct",
            "LastAction",
            "LastSignal",
            "LastConfidencePct",
            "LastUpdatedAt",
        ]
        return positions_df[columns].copy()

    def _build_summary(self, trades_df: pd.DataFrame, cashflows_df: pd.DataFrame) -> pd.DataFrame:
        """Summarize the paper account state."""
        trades_df = self._recalculate_trade_pnl(trades_df)
        open_df = trades_df[trades_df["Status"] == "OPEN"].copy()
        closed_df = trades_df[trades_df["Status"] == "CLOSED"].copy()
        added_cash = round(self._cash_added(cashflows_df), 2)
        total_money_added = round(self.initial_capital + added_cash, 2)

        # Live/manual portfolios can start from migrated trade rows without a prior
        # funding event in SQLite (initial_capital=0 and no cashflow rows). In that
        # case, total_money_added would be 0 and reported P/L becomes misleading
        # (TotalPnL ~= Equity). Infer a baseline funding amount from ledger economics.
        # Rearranged from cash equation with cash floored at 0:
        # funding ~= invested_total + entry_charges - exit_total + exit_charges.
        if total_money_added <= 0 and not trades_df.empty:
            invested_total_for_funding = round(trades_df["InvestedAmount"].sum(), 2)
            exit_total_for_funding = round(trades_df["ExitValue"].sum(), 2)
            entry_charges_for_funding = round(
                sum(self._entry_charges_for_trade(row) for _, row in trades_df.iterrows()),
                2,
            )
            exit_charges_for_funding = round(
                sum(
                    self._exit_charges_for_trade(self._to_float(row["ExitValue"]))
                    for _, row in trades_df.loc[trades_df["Status"] == "CLOSED"].iterrows()
                ),
                2,
            )
            inferred_funding = round(
                invested_total_for_funding
                + entry_charges_for_funding
                - exit_total_for_funding
                + exit_charges_for_funding,
                2,
            )
            total_money_added = max(inferred_funding, 0.0)

        cash = self._compute_cash(trades_df, cashflows_df)
        invested = round(open_df["InvestedAmount"].sum(), 2)
        market_value = round(open_df["CurrentValue"].sum(), 2)
        gross_realized_pnl = round((closed_df["ExitValue"] - closed_df["InvestedAmount"]).sum(), 2)
        gross_unrealized_pnl = round((open_df["CurrentValue"] - open_df["InvestedAmount"]).sum(), 2)
        entry_charges_paid = round(
            sum(self._entry_charges_for_trade(row) for _, row in trades_df.iterrows()),
            2,
        )
        exit_charges_paid = round(
            sum(self._exit_charges_for_trade(self._to_float(row["ExitValue"])) for _, row in closed_df.iterrows()),
            2,
        )
        open_exit_charges_est = round(
            sum(self._exit_charges_for_trade(self._to_float(row["CurrentValue"])) for _, row in open_df.iterrows()),
            2,
        )
        charges_paid = round(entry_charges_paid + exit_charges_paid, 2)
        total_charges_est = round(charges_paid + open_exit_charges_est, 2)
        realized_pnl = round(closed_df["RealizedPnL"].sum(), 2)
        unrealized_pnl = round(open_df["UnrealizedPnL"].sum(), 2)
        gross_equity = round(cash + market_value, 2)
        equity = round(gross_equity - open_exit_charges_est, 2)
        gross_total_pnl = round(gross_realized_pnl + gross_unrealized_pnl, 2)
        total_pnl = round(equity - total_money_added, 2)
        total_return_pct = (
            round((total_pnl * 100) / total_money_added, 2)
            if total_money_added > 0
            else 0.0
        )
        wins = int((closed_df["RealizedPnL"] > 0).sum())
        losses = int((closed_df["RealizedPnL"] < 0).sum())
        closed_count = int(len(closed_df))
        win_rate_pct = round((wins * 100) / closed_count, 2) if closed_count > 0 else 0.0

        return pd.DataFrame(
            [
                {
                    "Universe": self.universe,
                    "InitialCapital": round(self.initial_capital, 2),
                    "AddedCash": added_cash,
                    "TotalMoneyAdded": total_money_added,
                    "Cash": cash,
                    "InvestedAmount": invested,
                    "MarketValue": market_value,
                    "GrossEquity": gross_equity,
                    "Equity": equity,
                    "GrossRealizedPnL": gross_realized_pnl,
                    "GrossUnrealizedPnL": gross_unrealized_pnl,
                    "GrossTotalPnL": gross_total_pnl,
                    "EntryChargesPaid": entry_charges_paid,
                    "ExitChargesPaid": exit_charges_paid,
                    "ChargesPaid": charges_paid,
                    "OpenExitChargesEstimate": open_exit_charges_est,
                    "EstimatedTotalCharges": total_charges_est,
                    "RealizedPnL": realized_pnl,
                    "UnrealizedPnL": unrealized_pnl,
                    "TotalPnL": total_pnl,
                    "TotalReturnPct": total_return_pct,
                    "OpenPositions": int(len(open_df)),
                    "ClosedTrades": closed_count,
                    "Wins": wins,
                    "Losses": losses,
                    "WinRatePct": win_rate_pct,
                    "LastRunAt": self.run_at_str,
                }
            ]
        )

    def _build_positions_simple_view(self, positions_df: pd.DataFrame) -> pd.DataFrame:
        """Create a plain-English view for open positions."""
        if positions_df is None or positions_df.empty:
            return pd.DataFrame()

        gross_running = (
            pd.to_numeric(positions_df["CurrentValue"], errors="coerce").fillna(0.0)
            - pd.to_numeric(positions_df["InvestedAmount"], errors="coerce").fillna(0.0)
        )
        est_exit_charges = pd.to_numeric(positions_df["CurrentValue"], errors="coerce").fillna(0.0).apply(
            self._estimate_sell_charges
        )
        return pd.DataFrame(
            {
                "Stock": positions_df["Symbol"],
                "Shares Held": positions_df["Quantity"],
                "Bought At": positions_df["EntryPrice"],
                "Price Now": positions_df["CurrentPrice"],
                "Gross Profit/Loss": gross_running,
                "Est. Exit Charges": est_exit_charges,
                "Profit/Loss Now": positions_df["UnrealizedPnL"],
                "Return %": positions_df["UnrealizedReturnPct"],
                "Sell If Below": positions_df["StopLoss"],
                "Take Profit 1": positions_df["Target1"],
                "Take Profit 2": positions_df["Target2"],
                "What To Do Now": positions_df.apply(self._what_to_do_now, axis=1),
                "Signal Status": positions_df["LastAction"],
                "Why": positions_df["LastSignal"].astype(str) + " | " + positions_df["LastAction"].astype(str),
                "Checked At": positions_df["LastUpdatedAt"],
            }
        )

    def _build_summary_simple_view(self, summary_df: pd.DataFrame) -> pd.DataFrame:
        """Create a plain-English portfolio summary."""
        if summary_df is None or summary_df.empty:
            return pd.DataFrame()

        row = summary_df.iloc[0]
        return pd.DataFrame(
            [
                {
                    "Universe": row["Universe"],
                    "Starting Money": row["InitialCapital"],
                    "Extra Cash Added": row.get("AddedCash", 0.0),
                    "Total Money Put In": row.get("TotalMoneyAdded", row["InitialCapital"]),
                    "Cash Left": row["Cash"],
                    "Money In Open Trades": row["InvestedAmount"],
                    "Current Value Of Open Trades": row["MarketValue"],
                    "Value Before Exit Costs": row.get("GrossEquity", row["Equity"]),
                    "Total Account Value": row["Equity"],
                    "Gross Booked Profit/Loss": row.get("GrossRealizedPnL", row["RealizedPnL"]),
                    "Gross Running Profit/Loss": row.get("GrossUnrealizedPnL", row["UnrealizedPnL"]),
                    "Estimated Charges Paid": row.get("ChargesPaid", 0.0),
                    "Extra Charges If Sold Now": row.get("OpenExitChargesEstimate", 0.0),
                    "Estimated Total Charges": row.get("EstimatedTotalCharges", 0.0),
                    "Booked Profit/Loss": row["RealizedPnL"],
                    "Running Profit/Loss": row["UnrealizedPnL"],
                    "Total Profit/Loss": row["TotalPnL"],
                    "Total Return %": row["TotalReturnPct"],
                    "Open Trades": row["OpenPositions"],
                    "Closed Trades": row["ClosedTrades"],
                    "Last Checked": row["LastRunAt"],
                }
            ]
        )

    def _save_state(
        self,
        report_df: pd.DataFrame,
        trades_df: pd.DataFrame,
        cashflows_df: pd.DataFrame,
        positions_df: pd.DataFrame,
        summary_df: pd.DataFrame,
    ) -> None:
        """Persist the paper-trading outputs for future runs."""
        paths = {
            "signals_latest": self._latest_path("signals"),
            "signals_dated": self._dated_path("signals"),
            "signals_simple_latest": self._latest_path("signals_simple"),
            "signals_simple_dated": self._dated_path("signals_simple"),
            "trades_latest": self._latest_path("trades"),
            "trades_dated": self._dated_path("trades"),
            "cashflows_latest": self._latest_path("cashflows"),
            "cashflows_dated": self._dated_path("cashflows"),
            "positions_latest": self._latest_path("positions"),
            "positions_dated": self._dated_path("positions"),
            "positions_simple_latest": self._latest_path("positions_simple"),
            "positions_simple_dated": self._dated_path("positions_simple"),
            "summary_latest": self._latest_path("summary"),
            "summary_dated": self._dated_path("summary"),
            "summary_simple_latest": self._latest_path("summary_simple"),
            "summary_simple_dated": self._dated_path("summary_simple"),
            "dashboard_latest": self._dashboard_latest_path(),
            "dashboard_dated": self._dashboard_dated_path(),
        }

        report_df.drop(columns=["ActionRank"], inplace=True, errors="ignore")
        report_df.drop(columns=["NowActionRank"], inplace=True, errors="ignore")
        simple_signals_df = self.actionable_runner.build_simple_view(report_df)
        simple_positions_df = self._build_positions_simple_view(positions_df)
        simple_summary_df = self._build_summary_simple_view(summary_df)

        trades_df.to_csv(paths["trades_latest"], index=False)
        trades_df.to_csv(paths["trades_dated"], index=False)
        cashflows_df.to_csv(paths["cashflows_latest"], index=False)
        cashflows_df.to_csv(paths["cashflows_dated"], index=False)
        positions_df.to_csv(paths["positions_latest"], index=False)
        positions_df.to_csv(paths["positions_dated"], index=False)
        simple_positions_df.to_csv(paths["positions_simple_latest"], index=False)
        simple_positions_df.to_csv(paths["positions_simple_dated"], index=False)
        summary_df.to_csv(paths["summary_latest"], index=False)
        summary_df.to_csv(paths["summary_dated"], index=False)
        simple_summary_df.to_csv(paths["summary_simple_latest"], index=False)
        simple_summary_df.to_csv(paths["summary_simple_dated"], index=False)
        report_df.to_csv(paths["signals_latest"], index=False)
        report_df.to_csv(paths["signals_dated"], index=False)
        simple_signals_df.to_csv(paths["signals_simple_latest"], index=False)
        simple_signals_df.to_csv(paths["signals_simple_dated"], index=False)
        self._save_dashboard(
            latest_path=paths["dashboard_latest"],
            dated_path=paths["dashboard_dated"],
            summary_simple_df=simple_summary_df,
            positions_simple_df=simple_positions_df,
            signals_simple_df=simple_signals_df,
            trades_df=trades_df,
            cashflows_df=cashflows_df,
        )

        OutputControls().printOutput(f"  [+] Saved paper summary: {paths['summary_latest']}")
        OutputControls().printOutput(f"  [+] Saved open positions: {paths['positions_latest']}")
        OutputControls().printOutput(f"  [+] Saved trade ledger: {paths['trades_latest']}")
        OutputControls().printOutput(f"  [+] Saved cashflow ledger: {paths['cashflows_latest']}")
        OutputControls().printOutput(f"  [+] Saved simple positions view: {paths['positions_simple_latest']}")
        OutputControls().printOutput(f"  [+] Saved simple buy/sell view: {paths['signals_simple_latest']}")
        OutputControls().printOutput(f"  [+] Saved local dashboard: {paths['dashboard_latest']}")

    def _print_summary(
        self,
        summary_df: pd.DataFrame,
        positions_df: pd.DataFrame,
        cashflow_events: List[str],
        entry_events: List[str],
        exit_events: List[str],
    ) -> None:
        """Print a concise console summary after each paper-trading cycle."""
        summary = summary_df.iloc[0].to_dict()
        OutputControls().printOutput(
            "  [+] Paper account: "
            f"Money In {summary.get('TotalMoneyAdded', summary['InitialCapital']):.2f} | "
            f"Cash {summary['Cash']:.2f} | "
            f"Market Value {summary['MarketValue']:.2f} | "
            f"Net Equity {summary['Equity']:.2f} | "
            f"Net P&L {summary['TotalPnL']:.2f} ({summary['TotalReturnPct']:.2f}%)"
        )
        OutputControls().printOutput(
            "  [+] Trades: "
            f"Open {int(summary['OpenPositions'])} | "
            f"Closed {int(summary['ClosedTrades'])} | "
            f"Net Realized {summary['RealizedPnL']:.2f} | "
            f"Net Unrealized {summary['UnrealizedPnL']:.2f} | "
            f"Charges {summary.get('EstimatedTotalCharges', 0.0):.2f}"
        )

        if cashflow_events:
            OutputControls().printOutput("  [+] Cash updates:")
            for event in cashflow_events[:8]:
                OutputControls().printOutput(f"    {event}")

        if entry_events:
            OutputControls().printOutput("  [+] New paper entries:")
            for event in entry_events[:8]:
                OutputControls().printOutput(f"    {event}")

        if exit_events:
            OutputControls().printOutput("  [+] Closed paper positions:")
            for event in exit_events[:8]:
                OutputControls().printOutput(f"    {event}")

        if positions_df is None or positions_df.empty:
            OutputControls().printOutput("  [+] No open paper positions right now.")
            return

        preview_cols = [
            "Stock",
            "Shares Held",
            "Bought At",
            "Price Now",
            "Gross Profit/Loss",
            "Est. Exit Charges",
            "Profit/Loss Now",
            "Sell If Below",
            "Take Profit 1",
            "Take Profit 2",
            "What To Do Now",
        ]
        simple_positions_df = self._build_positions_simple_view(positions_df)
        OutputControls().printOutput(
            simple_positions_df[preview_cols].head(10).to_string(index=False),
            enableMultipleLineOutput=True,
        )

    def _send_alerts(
        self,
        cashflow_events: List[str],
        entry_events: List[str],
        exit_events: List[str],
        summary_df: pd.DataFrame,
    ) -> None:
        """Send optional desktop and/or Telegram alerts for important events."""
        if not self.alert_channels:
            return

        events = []
        events.extend([f"[Cash] {event}" for event in cashflow_events])
        events.extend([f"[Buy] {event}" for event in entry_events])
        events.extend([f"[Sell] {event}" for event in exit_events])
        if not events:
            return

        summary = summary_df.iloc[0].to_dict() if summary_df is not None and not summary_df.empty else {}
        title = f"PKScreener {self.universe} alerts"
        message = "\n".join(
            [
                f"{title} @ {self.run_at_str}",
                *events[:10],
                f"Cash: {summary.get('Cash', 0.0):.2f} | Equity: {summary.get('Equity', 0.0):.2f} | P&L: {summary.get('TotalPnL', 0.0):.2f}",
            ]
        )

        if "desktop" in self.alert_channels:
            self._send_desktop_alert(title, "\n".join(events[:5]))
        if "telegram" in self.alert_channels:
            self._send_telegram_alert(message)
        if "discord" in self.alert_channels:
            self._send_discord_alert(
                self._build_discord_portfolio_message(
                    cashflow_events=cashflow_events,
                    entry_events=entry_events,
                    exit_events=exit_events,
                    summary=summary,
                )
            )

    def _build_discord_portfolio_message(
        self,
        cashflow_events: List[str],
        entry_events: List[str],
        exit_events: List[str],
        summary: Dict,
    ) -> str:
        """Build a phone-friendly Discord portfolio alert."""
        sections = [
            f"**PKScreener Portfolio Alert**",
            f"Universe `{self.universe}` | Time `{self.run_at_str}`",
            (
                f"Cash `{self._to_float(summary.get('Cash', 0.0)):.2f}` | "
                f"Equity `{self._to_float(summary.get('Equity', 0.0)):.2f}` | "
                f"P&L `{self._to_float(summary.get('TotalPnL', 0.0)):.2f}`"
            ),
        ]

        if cashflow_events:
            cash_lines = "\n".join([f"- {event}" for event in cashflow_events[:3]])
            sections.append(f"**Cash Updates**\n{cash_lines}")
        if entry_events:
            buy_lines = "\n".join([f"- {event}" for event in entry_events[:4]])
            sections.append(f"**New Buys**\n{buy_lines}")
        if exit_events:
            sell_lines = "\n".join([f"- {event}" for event in exit_events[:4]])
            sections.append(f"**New Sells**\n{sell_lines}")

        return "\n\n".join(sections)[:1900]

    def _send_desktop_alert(self, title: str, message: str) -> None:
        """Show a desktop alert on Windows using built-in PowerShell support."""
        try:
            safe_title = str(title).replace("'", "''")
            safe_message = str(message).replace("'", "''")
            command = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$notify = New-Object System.Windows.Forms.NotifyIcon; "
                "$notify.Icon = [System.Drawing.SystemIcons]::Information; "
                f"$notify.BalloonTipTitle = '{safe_title}'; "
                f"$notify.BalloonTipText = '{safe_message}'; "
                "$notify.Visible = $true; "
                "$notify.ShowBalloonTip(5000); "
                "Start-Sleep -Seconds 6; "
                "$notify.Dispose();"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            default_logger().debug(f"Desktop alert failed: {exc}", exc_info=True)

    def _send_telegram_alert(self, message: str) -> None:
        """Send a Telegram text alert when bot credentials are configured."""
        token = os.environ.get("TOKEN", "").strip()
        chat_id = os.environ.get("CHAT_ID", "").strip()
        if not token or not chat_id:
            try:
                from PKDevTools.classes.Environment import PKEnvironment
                env = PKEnvironment()
                token = str(env.allSecrets.get("TOKEN", token)).strip()
                chat_id = str(env.allSecrets.get("CHAT_ID", chat_id)).strip()
            except Exception:
                pass

        if not token or not chat_id:
            OutputControls().printOutput(
                "  [+] Telegram alerts skipped because TOKEN / CHAT_ID are not configured."
            )
            return

        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message},
                timeout=10,
            )
        except Exception as exc:
            default_logger().debug(f"Telegram alert failed: {exc}", exc_info=True)

    def _send_discord_alert(self, message: str) -> None:
        """Send a Discord webhook alert when configured."""
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook_url:
            try:
                from PKDevTools.classes.Environment import PKEnvironment
                env = PKEnvironment()
                webhook_url = str(env.allSecrets.get("DISCORD_WEBHOOK_URL", webhook_url)).strip()
            except Exception:
                pass

        if not webhook_url:
            OutputControls().printOutput(
                "  [+] Discord alerts skipped because DISCORD_WEBHOOK_URL is not configured."
            )
            return

        try:
            requests.post(
                webhook_url,
                json={"content": message[:1900]},
                timeout=10,
            )
        except Exception as exc:
            default_logger().debug(f"Discord alert failed: {exc}", exc_info=True)

    def _compute_cash(self, trades_df: pd.DataFrame, cashflows_df: pd.DataFrame) -> float:
        """Compute remaining cash from entry costs and closed-trade exit values."""
        invested_total = round(trades_df["InvestedAmount"].sum(), 2)
        exit_total = round(trades_df["ExitValue"].sum(), 2)
        entry_charges_total = round(
            sum(self._entry_charges_for_trade(row) for _, row in trades_df.iterrows()),
            2,
        )
        exit_charges_total = round(
            sum(
                self._exit_charges_for_trade(self._to_float(row["ExitValue"]))
                for _, row in trades_df.loc[trades_df["Status"] == "CLOSED"].iterrows()
            ),
            2,
        )
        total_funding = round(self.initial_capital + self._cash_added(cashflows_df), 2)
        cash = round(
            total_funding
            - invested_total
            - entry_charges_total
            + exit_total
            - exit_charges_total,
            2,
        )
        return max(cash, 0.0)

    def _next_trade_id(self, trades_df: pd.DataFrame) -> int:
        """Get the next trade ID for the ledger."""
        if trades_df is None or trades_df.empty:
            return 1
        return int(trades_df["TradeId"].max()) + 1

    def _latest_path(self, name: str) -> str:
        return os.path.join(self.output_dir, f"papertrade_{self.universe}_{name}_latest.csv")

    def _dated_path(self, name: str) -> str:
        return os.path.join(self.output_dir, f"papertrade_{self.universe}_{name}_{self.timestamp}.csv")

    def _dashboard_latest_path(self) -> str:
        return os.path.join(self.output_dir, f"papertrade_{self.universe}_dashboard_latest.html")

    def _dashboard_dated_path(self) -> str:
        return os.path.join(self.output_dir, f"papertrade_{self.universe}_dashboard_{self.timestamp}.html")

    def _unique_symbols(self, symbols: List[str]) -> List[str]:
        """Remove duplicates while preserving symbol order."""
        ordered: List[str] = []
        seen = set()
        for symbol in symbols:
            normalized = str(symbol).strip().upper()
            if not normalized or normalized in seen:
                continue
            ordered.append(normalized)
            seen.add(normalized)
        return ordered

    def _load_charge_pct(self, env_name: str, default: float) -> float:
        """Load an editable estimated cost percentage for net-profit calculations."""
        raw_value = os.environ.get(env_name, default)
        try:
            value = float(raw_value)
        except Exception:
            value = float(default)
        return max(0.0, value)

    def _estimate_buy_charges(self, turnover: float) -> float:
        """Estimate buy-side costs for Indian cash-equity style trades."""
        return round(max(0.0, self._to_float(turnover)) * self.estimated_buy_cost_pct, 2)

    def _estimate_sell_charges(self, turnover: float) -> float:
        """Estimate sell-side costs for Indian cash-equity style trades."""
        return round(max(0.0, self._to_float(turnover)) * self.estimated_sell_cost_pct, 2)

    def _entry_charges_for_trade(self, trade: pd.Series) -> float:
        """Estimate the already-paid entry charges for one trade row."""
        return self._estimate_buy_charges(self._to_float(trade.get("InvestedAmount")))

    def _exit_charges_for_trade(self, turnover: float) -> float:
        """Estimate the sell-side charges for an exit value."""
        return self._estimate_sell_charges(turnover)

    def _recalculate_trade_pnl(self, trades_df: pd.DataFrame) -> pd.DataFrame:
        """Recompute trade P&L as net-of-costs so reports match likely take-home results."""
        if trades_df is None or trades_df.empty:
            return trades_df

        trades_df = trades_df.copy()
        for index in trades_df.index.tolist():
            trade = trades_df.loc[index]
            status = str(trade.get("Status", "")).strip().upper()
            invested_amount = round(self._to_float(trade.get("InvestedAmount")), 2)
            entry_charges = self._entry_charges_for_trade(trade)

            if status == "OPEN":
                current_value = round(self._to_float(trade.get("CurrentValue")), 2)
                exit_charges_est = self._exit_charges_for_trade(current_value)
                net_unrealized = round(current_value - invested_amount - entry_charges - exit_charges_est, 2)
                net_unrealized_return = (
                    round((net_unrealized * 100) / max(invested_amount + entry_charges, 0.01), 2)
                    if invested_amount > 0
                    else 0.0
                )
                trades_df.loc[index, "UnrealizedPnL"] = net_unrealized
                trades_df.loc[index, "UnrealizedReturnPct"] = net_unrealized_return
                trades_df.loc[index, "RealizedPnL"] = 0.0
                trades_df.loc[index, "RealizedReturnPct"] = 0.0
                continue

            exit_value = round(self._to_float(trade.get("ExitValue")), 2)
            exit_charges = self._exit_charges_for_trade(exit_value)
            net_realized = round(exit_value - invested_amount - entry_charges - exit_charges, 2)
            net_realized_return = (
                round((net_realized * 100) / max(invested_amount + entry_charges, 0.01), 2)
                if invested_amount > 0
                else 0.0
            )
            trades_df.loc[index, "CurrentValue"] = 0.0
            trades_df.loc[index, "UnrealizedPnL"] = 0.0
            trades_df.loc[index, "UnrealizedReturnPct"] = 0.0
            trades_df.loc[index, "RealizedPnL"] = net_realized
            trades_df.loc[index, "RealizedReturnPct"] = net_realized_return

        return trades_df[self.TRADE_COLUMNS].copy()

    def _to_float(self, value, fallback: float = 0.0) -> float:
        """Best-effort numeric conversion helper."""
        try:
            numeric = float(value)
            if numeric == numeric:
                return numeric
        except Exception:
            pass
        return float(fallback)

    def _what_to_do_now(self, row: pd.Series) -> str:
        """Translate the open-position state into a simple next step."""
        current_price = self._to_float(row.get("CurrentPrice"))
        stop_loss = self._to_float(row.get("StopLoss"))
        target_1 = self._to_float(row.get("Target1"))
        last_action = str(row.get("LastAction", "")).strip().upper()

        if stop_loss > 0 and current_price <= stop_loss:
            return "Sell now"
        if target_1 > 0 and current_price >= target_1:
            return "Book some profit"
        if last_action in ["SELL/EXIT", "WATCH SELL", "SELL NOW", "WATCH FOR SELL"]:
            return "Think about exit"
        if last_action in ["BUY", "WATCH BUY", "BUY NOW", "WATCH FOR BUY"]:
            return "Hold for now"
        return "Wait"

    def _cash_added(self, cashflows_df: pd.DataFrame) -> float:
        """Return net manually added cash."""
        if cashflows_df is None or cashflows_df.empty or "Amount" not in cashflows_df.columns:
            return 0.0
        return float(pd.to_numeric(cashflows_df["Amount"], errors="coerce").fillna(0.0).sum())

    def _normalize_alert_channels(self, value: str) -> Set[str]:
        """Normalize requested alert channels into a set."""
        normalized = str(value or "none").strip().lower()
        if normalized in ["", "none"]:
            return set()
        if normalized == "both":
            return {"desktop", "telegram"}

        separators = [",", "+", " "]
        pieces = [normalized]
        for separator in separators:
            if separator in normalized:
                pieces = [part.strip() for part in normalized.replace("+", ",").replace(" ", ",").split(",") if part.strip()]
                break

        allowed = {"desktop", "telegram", "discord"}
        selected = {piece for piece in pieces if piece in allowed}
        return selected

    def _save_dashboard(
        self,
        latest_path: str,
        dated_path: str,
        summary_simple_df: pd.DataFrame,
        positions_simple_df: pd.DataFrame,
        signals_simple_df: pd.DataFrame,
        trades_df: pd.DataFrame,
        cashflows_df: pd.DataFrame,
    ) -> None:
        """Save an auto-refreshing local HTML dashboard."""
        html_text = self._build_dashboard_html(
            summary_simple_df=summary_simple_df,
            positions_simple_df=positions_simple_df,
            signals_simple_df=signals_simple_df,
            trades_df=trades_df,
            cashflows_df=cashflows_df,
        )
        with open(latest_path, "w", encoding="utf-8") as handle:
            handle.write(html_text)
        with open(dated_path, "w", encoding="utf-8") as handle:
            handle.write(html_text)

    def _build_dashboard_html(
        self,
        summary_simple_df: pd.DataFrame,
        positions_simple_df: pd.DataFrame,
        signals_simple_df: pd.DataFrame,
        trades_df: pd.DataFrame,
        cashflows_df: pd.DataFrame,
    ) -> str:
        """Render a lightweight local dashboard as a single HTML file."""
        summary_row = (
            summary_simple_df.iloc[0].to_dict()
            if summary_simple_df is not None and not summary_simple_df.empty
            else {}
        )
        recent_trades_df = trades_df.copy() if trades_df is not None else pd.DataFrame()
        if not recent_trades_df.empty:
            recent_trades_df = recent_trades_df.sort_values(
                by=["TradeId"], ascending=[False]
            ).head(20)
            recent_trades_df = recent_trades_df[
                [
                    "TradeId",
                    "Symbol",
                    "Status",
                    "Quantity",
                    "EntryPrice",
                    "ExitPrice",
                    "RealizedPnL",
                    "LastUpdatedAt",
                ]
            ].copy()
            recent_trades_df.columns = [
                "Trade",
                "Stock",
                "Status",
                "Qty",
                "Bought At",
                "Sold At",
                "Booked P/L",
                "Updated",
            ]

        cash_table_df = cashflows_df.copy() if cashflows_df is not None else pd.DataFrame()
        if not cash_table_df.empty:
            cash_table_df = cash_table_df.tail(20).copy()
            cash_table_df.columns = ["When", "Type", "Amount", "Note"]

        cards = [
            ("Total Money In", summary_row.get("Total Money Put In", 0.0)),
            ("Cash Left", summary_row.get("Cash Left", 0.0)),
            ("Account Value", summary_row.get("Total Account Value", 0.0)),
            ("Total P/L", summary_row.get("Total Profit/Loss", 0.0)),
            ("Open Trades", summary_row.get("Open Trades", 0)),
            ("Closed Trades", summary_row.get("Closed Trades", 0)),
        ]

        card_html = "".join(
            [
                f"<div class='card'><div class='label'>{html.escape(str(label))}</div>"
                f"<div class='value'>{html.escape(str(value))}</div></div>"
                for label, value in cards
            ]
        )

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="20">
  <title>PKScreener Local Dashboard</title>
  <style>
    :root {{
      --bg: #f4f1ea;
      --panel: #fffaf1;
      --ink: #1f2933;
      --muted: #5b6b7a;
      --line: #d9cfbf;
      --accent: #0f766e;
      --good: #166534;
      --bad: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Segoe UI", serif;
      background: radial-gradient(circle at top, #fff8eb 0%, var(--bg) 60%);
      color: var(--ink);
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }}
    .hero {{
      background: linear-gradient(135deg, #fffdf8 0%, #f3ead9 100%);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 22px;
      box-shadow: 0 12px 40px rgba(0,0,0,0.05);
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0 0 8px 0;
      font-size: 32px;
      letter-spacing: 0.02em;
    }}
    .sub {{
      color: var(--muted);
      font-size: 15px;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin: 18px 0 6px 0;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
    }}
    .label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .value {{
      font-size: 24px;
      font-weight: 700;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }}
    .panel {{
      background: rgba(255,255,255,0.78);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      overflow: hidden;
    }}
    .panel h2 {{
      margin: 0 0 12px 0;
      font-size: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 10px 8px;
      border-bottom: 1px solid #eadfce;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #f8f2e6;
      position: sticky;
      top: 0;
    }}
    .note {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 8px;
    }}
    @media (max-width: 720px) {{
      .wrap {{ padding: 12px; }}
      h1 {{ font-size: 24px; }}
      .value {{ font-size: 20px; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>PKScreener Local Dashboard</h1>
      <div class="sub">Universe: {html.escape(self.universe)} | Updated: {html.escape(self.run_at_str)} | Auto-refresh: 20 sec</div>
      <div class="cards">{card_html}</div>
      <div class="note">"Watch for Buy" means the stock looks interesting, but price has not crossed the actual buy trigger yet. "Buy Now" appears only after trigger confirmation.</div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Open Positions</h2>
        {self._table_html(positions_simple_df, empty_text="No open positions right now.")}
      </div>
      <div class="panel">
        <h2>Buy / Sell Board</h2>
        {self._table_html(signals_simple_df.head(30) if signals_simple_df is not None else pd.DataFrame(), empty_text="No signal rows available.")}
      </div>
      <div class="panel">
        <h2>Recent Trades</h2>
        {self._table_html(recent_trades_df, empty_text="No trades yet.")}
      </div>
      <div class="panel">
        <h2>Cash History</h2>
        {self._table_html(cash_table_df, empty_text="No extra cash added yet.")}
      </div>
    </section>
  </div>
</body>
</html>"""

    def _table_html(self, df: pd.DataFrame, empty_text: str) -> str:
        """Render a DataFrame into dashboard HTML."""
        if df is None or df.empty:
            return f"<div class='note'>{html.escape(empty_text)}</div>"
        return df.to_html(index=False, classes="data-table", border=0, justify="left")
