"""
Manual live-portfolio tracking for Discord/mobile-driven trading workflows.

This module reuses the proven paper-trade ledger format, but keeps a separate
live portfolio that only changes when the user explicitly adds cash or enters
buy/sell trades. It never auto-buys or auto-sells.

SQLite is the primary source of truth for the live portfolio. CSV/HTML outputs
are still exported as easy-to-open snapshots for dashboards and manual review.
"""

import os
import sqlite3
from typing import Dict, List, Optional, Tuple

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls

from pkscreener.classes.PaperTrading import PaperTradingRunner


class LivePortfolioStore:
    """SQLite-backed source of truth for the live portfolio."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_accounts (
                    account_name TEXT PRIMARY KEY,
                    universe TEXT,
                    initial_capital REAL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    last_run_at TEXT
                );

                CREATE TABLE IF NOT EXISTS live_cashflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    when_text TEXT,
                    type TEXT,
                    amount REAL,
                    note TEXT
                );

                CREATE TABLE IF NOT EXISTS live_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    trade_id INTEGER,
                    universe TEXT,
                    symbol TEXT,
                    side TEXT,
                    status TEXT,
                    entry_date TEXT,
                    entry_price REAL,
                    quantity REAL,
                    invested_amount REAL,
                    stop_loss REAL,
                    target1 REAL,
                    target2 REAL,
                    entry_signal TEXT,
                    entry_confidence_pct REAL,
                    entry_reason TEXT,
                    current_price REAL,
                    current_value REAL,
                    unrealized_pnl REAL,
                    unrealized_return_pct REAL,
                    last_action TEXT,
                    last_signal TEXT,
                    last_confidence_pct REAL,
                    last_reason TEXT,
                    last_updated_at TEXT,
                    exit_date TEXT,
                    exit_price REAL,
                    exit_value REAL,
                    exit_reason TEXT,
                    realized_pnl REAL,
                    realized_return_pct REAL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_accounts_name
                    ON live_accounts(account_name);
                CREATE INDEX IF NOT EXISTS idx_live_cashflows_account
                    ON live_cashflows(account_name, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_trades_account_trade
                    ON live_trades(account_name, trade_id);
                """
            )

    def reset_account(self, account_name: str) -> None:
        """Remove all live portfolio state for one account."""
        account = str(account_name or "").strip().lower()
        if not account:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM live_cashflows WHERE account_name = ?", (account,))
            connection.execute("DELETE FROM live_trades WHERE account_name = ?", (account,))
            connection.execute("DELETE FROM live_accounts WHERE account_name = ?", (account,))
            connection.commit()

    def load_initial_capital(self, account_name: str, default: float, legacy_summary_path: Optional[str] = None) -> float:
        """Load initial capital, falling back to legacy summary CSV once if needed."""
        account = str(account_name or "").strip().lower()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT initial_capital FROM live_accounts WHERE account_name = ?",
                (account,),
            ).fetchone()
            if row is not None and float(row["initial_capital"] or 0.0) > 0:
                return float(row["initial_capital"])

        initial_capital = float(default or 0.0)
        if legacy_summary_path and os.path.exists(legacy_summary_path):
            try:
                summary_df = pd.read_csv(legacy_summary_path)
                if not summary_df.empty and "InitialCapital" in summary_df.columns:
                    initial_capital = float(summary_df.iloc[0]["InitialCapital"] or initial_capital)
            except Exception:
                pass

        self.upsert_account(
            account_name=account,
            universe="",
            initial_capital=initial_capital,
            last_run_at="",
        )
        return initial_capital

    def upsert_account(self, account_name: str, universe: str, initial_capital: float, last_run_at: str) -> None:
        """Create or update account metadata."""
        account = str(account_name or "").strip().lower()
        now_text = str(last_run_at or "")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO live_accounts (
                    account_name, universe, initial_capital, created_at, updated_at, last_run_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_name) DO UPDATE SET
                    universe = excluded.universe,
                    initial_capital = CASE
                        WHEN live_accounts.initial_capital > 0 THEN live_accounts.initial_capital
                        ELSE excluded.initial_capital
                    END,
                    updated_at = excluded.updated_at,
                    last_run_at = excluded.last_run_at
                """,
                (
                    account,
                    str(universe or "").strip().lower(),
                    float(initial_capital or 0.0),
                    now_text,
                    now_text,
                    now_text,
                ),
            )
            connection.commit()

    def load_trades(
        self,
        account_name: str,
        trade_columns: List[str],
        numeric_columns: List[str],
        legacy_csv_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """Load trades from SQLite, importing the latest legacy CSV once if needed."""
        account = str(account_name or "").strip().lower()
        select_columns = [
            "trade_id AS TradeId",
            "universe AS Universe",
            "symbol AS Symbol",
            "side AS Side",
            "status AS Status",
            "entry_date AS EntryDate",
            "entry_price AS EntryPrice",
            "quantity AS Quantity",
            "invested_amount AS InvestedAmount",
            "stop_loss AS StopLoss",
            "target1 AS Target1",
            "target2 AS Target2",
            "entry_signal AS EntrySignal",
            "entry_confidence_pct AS EntryConfidencePct",
            "entry_reason AS EntryReason",
            "current_price AS CurrentPrice",
            "current_value AS CurrentValue",
            "unrealized_pnl AS UnrealizedPnL",
            "unrealized_return_pct AS UnrealizedReturnPct",
            "last_action AS LastAction",
            "last_signal AS LastSignal",
            "last_confidence_pct AS LastConfidencePct",
            "last_reason AS LastReason",
            "last_updated_at AS LastUpdatedAt",
            "exit_date AS ExitDate",
            "exit_price AS ExitPrice",
            "exit_value AS ExitValue",
            "exit_reason AS ExitReason",
            "realized_pnl AS RealizedPnL",
            "realized_return_pct AS RealizedReturnPct",
        ]
        with self._connect() as connection:
            rows_df = pd.read_sql_query(
                f"SELECT {', '.join(select_columns)} FROM live_trades WHERE account_name = ? ORDER BY trade_id, id",
                connection,
                params=(account,),
            )

        if rows_df.empty and legacy_csv_path and os.path.exists(legacy_csv_path):
            try:
                rows_df = pd.read_csv(legacy_csv_path)
                rows_df = self._normalize_df(rows_df, trade_columns, numeric_columns)
                if not rows_df.empty:
                    self.save_trades(account_name, rows_df, trade_columns)
            except Exception:
                rows_df = pd.DataFrame(columns=trade_columns)

        return self._normalize_df(rows_df, trade_columns, numeric_columns)

    def load_cashflows(
        self,
        account_name: str,
        cashflow_columns: List[str],
        legacy_csv_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """Load cashflows from SQLite, importing the latest legacy CSV once if needed."""
        account = str(account_name or "").strip().lower()
        with self._connect() as connection:
            rows_df = pd.read_sql_query(
                """
                SELECT
                    when_text AS [When],
                    type AS [Type],
                    amount AS [Amount],
                    note AS [Note]
                FROM live_cashflows
                WHERE account_name = ?
                ORDER BY id
                """,
                connection,
                params=(account,),
            )

        if rows_df.empty and legacy_csv_path and os.path.exists(legacy_csv_path):
            try:
                rows_df = pd.read_csv(legacy_csv_path)
                rows_df = self._normalize_cashflows_df(rows_df, cashflow_columns)
                if not rows_df.empty:
                    self.save_cashflows(account_name, rows_df, cashflow_columns)
            except Exception:
                rows_df = pd.DataFrame(columns=cashflow_columns)

        return self._normalize_cashflows_df(rows_df, cashflow_columns)

    def save_trades(self, account_name: str, trades_df: pd.DataFrame, trade_columns: List[str]) -> None:
        """Replace the stored trade ledger for one account."""
        account = str(account_name or "").strip().lower()
        normalized_df = self._normalize_df(trades_df, trade_columns, numeric_columns=[
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
        ])
        with self._connect() as connection:
            connection.execute("DELETE FROM live_trades WHERE account_name = ?", (account,))
            if not normalized_df.empty:
                rows = [
                    (
                        account,
                        int(float(row["TradeId"] or 0)),
                        str(row["Universe"]),
                        str(row["Symbol"]),
                        str(row["Side"]),
                        str(row["Status"]),
                        str(row["EntryDate"]),
                        float(row["EntryPrice"] or 0.0),
                        float(row["Quantity"] or 0.0),
                        float(row["InvestedAmount"] or 0.0),
                        float(row["StopLoss"] or 0.0),
                        float(row["Target1"] or 0.0),
                        float(row["Target2"] or 0.0),
                        str(row["EntrySignal"]),
                        float(row["EntryConfidencePct"] or 0.0),
                        str(row["EntryReason"]),
                        float(row["CurrentPrice"] or 0.0),
                        float(row["CurrentValue"] or 0.0),
                        float(row["UnrealizedPnL"] or 0.0),
                        float(row["UnrealizedReturnPct"] or 0.0),
                        str(row["LastAction"]),
                        str(row["LastSignal"]),
                        float(row["LastConfidencePct"] or 0.0),
                        str(row["LastReason"]),
                        str(row["LastUpdatedAt"]),
                        str(row["ExitDate"]),
                        float(row["ExitPrice"] or 0.0),
                        float(row["ExitValue"] or 0.0),
                        str(row["ExitReason"]),
                        float(row["RealizedPnL"] or 0.0),
                        float(row["RealizedReturnPct"] or 0.0),
                    )
                    for _, row in normalized_df.iterrows()
                ]
                connection.executemany(
                    """
                    INSERT INTO live_trades (
                        account_name, trade_id, universe, symbol, side, status,
                        entry_date, entry_price, quantity, invested_amount,
                        stop_loss, target1, target2, entry_signal, entry_confidence_pct,
                        entry_reason, current_price, current_value, unrealized_pnl,
                        unrealized_return_pct, last_action, last_signal,
                        last_confidence_pct, last_reason, last_updated_at,
                        exit_date, exit_price, exit_value, exit_reason,
                        realized_pnl, realized_return_pct
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            connection.commit()

    def save_cashflows(self, account_name: str, cashflows_df: pd.DataFrame, cashflow_columns: List[str]) -> None:
        """Replace the stored cashflow ledger for one account."""
        account = str(account_name or "").strip().lower()
        normalized_df = self._normalize_cashflows_df(cashflows_df, cashflow_columns)
        with self._connect() as connection:
            connection.execute("DELETE FROM live_cashflows WHERE account_name = ?", (account,))
            if not normalized_df.empty:
                rows = [
                    (
                        account,
                        str(row["When"]),
                        str(row["Type"]),
                        float(row["Amount"] or 0.0),
                        str(row["Note"]),
                    )
                    for _, row in normalized_df.iterrows()
                ]
                connection.executemany(
                    """
                    INSERT INTO live_cashflows (account_name, when_text, type, amount, note)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            connection.commit()

    def _normalize_df(self, df: pd.DataFrame, columns: List[str], numeric_columns: List[str]) -> pd.DataFrame:
        """Normalize a trade frame into the expected schema."""
        if df is None:
            df = pd.DataFrame(columns=columns)
        for column in columns:
            if column not in df.columns:
                df[column] = 0 if column in numeric_columns else ""
        for column in numeric_columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
        for column in [column for column in columns if column not in numeric_columns]:
            df[column] = df[column].fillna("").astype(str)
        if "TradeId" in df.columns and len(df) > 0:
            df["TradeId"] = pd.to_numeric(df["TradeId"], errors="coerce").fillna(0).astype(int)
        return df[columns].copy()

    def _normalize_cashflows_df(self, df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
        """Normalize a cashflow frame into the expected schema."""
        if df is None:
            df = pd.DataFrame(columns=columns)
        for column in columns:
            if column not in df.columns:
                df[column] = ""
        df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
        for column in [column for column in columns if column != "Amount"]:
            df[column] = df[column].fillna("").astype(str)
        return df[columns].copy()


class LivePortfolioRunner(PaperTradingRunner):
    """Separate on-disk live portfolio for manually confirmed trades."""

    def __init__(
        self,
        config_manager,
        account_name: str = "main",
        universe: str = "allstocks",
        add_cash_amount: float = 0.0,
        manual_buys: Optional[List[Dict]] = None,
        manual_sells: Optional[List[Dict]] = None,
        min_confidence: float = 10.0,
        risk_percent: float = 1.0,
        min_trade_value: float = 10000000.0,
        min_volume_ratio: float = 1.0,
        max_workers: int = 3,
        reset_account: bool = False,
    ):
        self.account_name = str(account_name or "main").strip().lower().replace(" ", "_")
        self._portfolio_output_dir = Archiver.get_user_data_dir()
        self.store = LivePortfolioStore(
            os.path.join(self._portfolio_output_dir, "assistant_portfolio.db")
        )
        self._live_reset_requested = bool(reset_account)
        self._live_reset_applied = False
        self.last_cash_events: List[str] = []
        self.last_buy_events: List[str] = []
        self.last_sell_events: List[str] = []
        super().__init__(
            config_manager=config_manager,
            universe=universe,
            limit=0,
            initial_capital=0.0,
            max_positions=999,
            min_confidence=min_confidence,
            reset_account=reset_account,
            add_cash_amount=add_cash_amount,
            alert_channels="none",
            manual_buys=manual_buys,
            manual_sells=manual_sells,
            risk_percent=risk_percent,
            min_trade_value=min_trade_value,
            min_volume_ratio=min_volume_ratio,
            max_workers=max_workers,
            use_live_kite=True,
        )

    def _load_initial_capital(self, default: float) -> float:
        """Load the stable live-account starting capital from SQLite."""
        if self._live_reset_requested and not self._live_reset_applied:
            self.store.reset_account(self.account_name)
            self._live_reset_applied = True
            return float(default)
        legacy_summary_path = os.path.join(
            self._portfolio_output_dir,
            f"livetrade_{self.account_name}_summary_latest.csv",
        )
        return self.store.load_initial_capital(
            account_name=self.account_name,
            default=float(default),
            legacy_summary_path=legacy_summary_path,
        )

    def _load_trades(self) -> pd.DataFrame:
        """Load the live trade ledger from SQLite, with one-time CSV migration."""
        if self._live_reset_requested:
            return pd.DataFrame(columns=self.TRADE_COLUMNS)
        legacy_path = os.path.join(
            self._portfolio_output_dir,
            f"livetrade_{self.account_name}_trades_latest.csv",
        )
        return self.store.load_trades(
            account_name=self.account_name,
            trade_columns=self.TRADE_COLUMNS,
            numeric_columns=self.NUMERIC_COLUMNS,
            legacy_csv_path=legacy_path,
        )

    def _load_cashflows(self) -> pd.DataFrame:
        """Load the live cashflow ledger from SQLite, with one-time CSV migration."""
        if self._live_reset_requested:
            return pd.DataFrame(columns=self.CASHFLOW_COLUMNS)
        legacy_path = os.path.join(
            self._portfolio_output_dir,
            f"livetrade_{self.account_name}_cashflows_latest.csv",
        )
        return self.store.load_cashflows(
            account_name=self.account_name,
            cashflow_columns=self.CASHFLOW_COLUMNS,
            legacy_csv_path=legacy_path,
        )

    def _load_ticks_market_map(self, symbols: List[str]) -> Dict[str, pd.Series]:
        """Build a fast market map from the saved ticks JSON file (no API call)."""
        import json
        ticks_path = os.path.join(self._portfolio_output_dir, "kiteticker_allstocks_ticks_latest.csv")
        json_path = os.path.join(self._portfolio_output_dir, "kiteticker_allstocks_ticks_latest.json")
        ticks: Dict = {}
        if os.path.isfile(json_path):
            try:
                with open(json_path, "r") as fh:
                    ticks = json.load(fh)
            except Exception:
                pass
        if not ticks and os.path.isfile(ticks_path):
            try:
                tdf = pd.read_csv(ticks_path)
                for _, row in tdf.iterrows():
                    sym = str(row.get("symbol", "")).strip().upper()
                    if sym:
                        ticks[sym] = row.to_dict()
            except Exception:
                pass

        market_map: Dict[str, pd.Series] = {}
        for sym in symbols:
            tick = ticks.get(sym.strip().upper())
            if not tick:
                continue
            ltp = float(tick.get("last_price", tick.get("LTP", 0)) or 0)
            if ltp <= 0:
                continue
            market_map[sym.strip().upper()] = pd.Series({
                "Symbol": sym,
                "LTP": ltp,
                "Action": "",
                "NowAction": "",
                "Signal": "",
                "ConfidencePct": 0.0,
                "Entry": 0.0,
                "StopLoss": 0.0,
                "Target1": 0.0,
                "Target2": 0.0,
                "Reason": "",
            })
        return market_map

    def run(self) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Refresh the live portfolio, apply manual changes, and persist results."""
        trades_df = self._load_trades()
        cashflows_df = self._load_cashflows()
        cashflows_df, cashflow_events = self._apply_cash_adjustment(cashflows_df)
        symbols_to_scan = self._build_scan_symbols(trades_df)

        # Fast path: read live prices directly from saved ticks file (no API call, no hang)
        market_map = self._load_ticks_market_map(symbols_to_scan)

        # Fall back to full OHLCV scan only for new manual buys where ticks are missing
        missing = [s for s in symbols_to_scan if s not in market_map]
        if missing:
            report_df, _ = self.actionable_runner.build_report(symbols=missing)
            if report_df is not None and not report_df.empty:
                for _, row in report_df.iterrows():
                    sym = str(row["Symbol"]).strip().upper()
                    market_map[sym] = row

        trades_df = self._refresh_open_positions(trades_df, market_map)
        trades_df, manual_sell_events = self._apply_manual_sells(trades_df)
        trades_df, manual_buy_events = self._apply_manual_buys(trades_df, market_map, cashflows_df)
        trades_df = self._recalculate_trade_pnl(trades_df)

        positions_df = self._build_positions_snapshot(trades_df)
        summary_df = self._build_summary(trades_df, cashflows_df)
        # build a minimal report_df from market_map for _save_state
        _report_for_save = pd.DataFrame(list(market_map.values())) if market_map else pd.DataFrame()
        self._save_state(_report_for_save, trades_df, cashflows_df, positions_df, summary_df)

        all_events = cashflow_events + manual_buy_events + manual_sell_events
        self.last_cash_events = cashflow_events
        self.last_buy_events = manual_buy_events
        self.last_sell_events = manual_sell_events
        self._print_summary(summary_df, positions_df, cashflow_events, manual_buy_events, manual_sell_events)
        if all_events:
            OutputControls().printOutput("  [+] Live portfolio updated.")
        return trades_df, positions_df, summary_df

    def _apply_manual_buys(
        self,
        trades_df: pd.DataFrame,
        market_map: Dict[str, pd.Series],
        cashflows_df: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, List[str]]:
        """Record manual live buys even when tracked cash is lower than broker cash."""
        events: List[str] = []
        if not self.manual_buys:
            return trades_df, events

        next_trade_id = self._next_trade_id(trades_df)
        new_rows = []
        tracked_cash_before = self._compute_cash(trades_df, cashflows_df)
        tracked_cash_after = float(tracked_cash_before)

        for order in self.manual_buys:
            symbol = str(order.get("symbol", "")).strip().upper()
            quantity = int(order.get("quantity", 0) or 0)
            entry_price = max(self._to_float(order.get("price")), 0.01)
            invested_amount = round(quantity * entry_price, 2)
            entry_charges = self._estimate_buy_charges(invested_amount)
            total_entry_debit = round(invested_amount + entry_charges, 2)

            if not symbol or quantity <= 0:
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

            tracked_cash_after = round(tracked_cash_after - total_entry_debit, 2)
            shortage_text = ""
            if total_entry_debit > tracked_cash_before:
                shortage_text = f" | tracked cash short by {total_entry_debit - max(tracked_cash_before, 0.0):.2f}"
            events.append(
                f"Manual BUY {symbol} x{quantity} at {entry_price:.2f} | est charges {entry_charges:.2f}{shortage_text}"
            )
            next_trade_id += 1

        if new_rows:
            trades_df = pd.concat(
                [trades_df, pd.DataFrame(new_rows, columns=self.TRADE_COLUMNS)],
                axis=0,
                ignore_index=True,
            )
            if tracked_cash_after < 0:
                events.append(
                    f"Tracked cash would go negative by {abs(tracked_cash_after):.2f}; trade was still recorded for live/manual tracking."
                )
        return trades_df[self.TRADE_COLUMNS].copy(), events

    def _build_scan_symbols(self, trades_df: pd.DataFrame) -> List[str]:
        """Refresh only currently open positions and any incoming buy symbols."""
        open_symbols = [
            str(symbol).strip().upper()
            for symbol in trades_df.loc[trades_df["Status"] == "OPEN", "Symbol"].tolist()
            if str(symbol).strip()
        ]
        incoming_buys = [
            str(order.get("symbol", "")).strip().upper()
            for order in self.manual_buys
            if str(order.get("symbol", "")).strip()
        ]
        return self._unique_symbols(open_symbols + incoming_buys)

    def _refresh_open_positions(
        self, trades_df: pd.DataFrame, market_map: Dict[str, pd.Series]
    ) -> pd.DataFrame:
        """Update open positions with the latest market snapshot without auto-closing them."""
        if trades_df is None or trades_df.empty:
            return trades_df

        trades_df = trades_df.copy()
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
            trades_df.loc[index, "StopLoss"] = self._to_float(market_row.get("StopLoss"), fallback=self._to_float(trade["StopLoss"]))
            trades_df.loc[index, "Target1"] = self._to_float(market_row.get("Target1"), fallback=self._to_float(trade["Target1"]))
            trades_df.loc[index, "Target2"] = self._to_float(market_row.get("Target2"), fallback=self._to_float(trade["Target2"]))
            trades_df.loc[index, "LastAction"] = str(
                market_row.get("NowAction", market_row.get("Action", ""))
            ).strip()
            trades_df.loc[index, "LastSignal"] = str(market_row.get("Signal", "")).strip()
            trades_df.loc[index, "LastConfidencePct"] = self._to_float(
                market_row.get("ConfidencePct")
            )
            trades_df.loc[index, "LastReason"] = str(market_row.get("Reason", "")).strip()
            trades_df.loc[index, "LastUpdatedAt"] = self.run_at_str

        return trades_df[self.TRADE_COLUMNS].copy()

    def _build_summary_simple_view(self, summary_df: pd.DataFrame) -> pd.DataFrame:
        """Create a plain-English summary tailored for live/manual trading."""
        if summary_df is None or summary_df.empty:
            return pd.DataFrame()

        row = summary_df.iloc[0]
        return pd.DataFrame(
            [
                {
                    "Account": self.account_name,
                    "Universe": row["Universe"],
                    "Total Money Put In": row.get("TotalMoneyAdded", 0.0),
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
        """Persist the live portfolio to SQLite first, then export CSV/HTML snapshots."""
        self.store.upsert_account(
            account_name=self.account_name,
            universe=self.universe,
            initial_capital=self.initial_capital,
            last_run_at=self.run_at_str,
        )
        self.store.save_trades(self.account_name, trades_df, self.TRADE_COLUMNS)
        self.store.save_cashflows(self.account_name, cashflows_df, self.CASHFLOW_COLUMNS)
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
        simple_signals_df = self.actionable_runner.build_simple_view(report_df) if not report_df.empty else pd.DataFrame()
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

        OutputControls().printOutput(f"  [+] Saved live summary: {paths['summary_latest']}")
        OutputControls().printOutput(f"  [+] Saved open live positions: {paths['positions_latest']}")
        OutputControls().printOutput(f"  [+] Saved live trade ledger: {paths['trades_latest']}")
        OutputControls().printOutput(f"  [+] Saved live cashflow ledger: {paths['cashflows_latest']}")
        OutputControls().printOutput(f"  [+] Saved live simple positions view: {paths['positions_simple_latest']}")
        OutputControls().printOutput(f"  [+] Saved live buy/sell view: {paths['signals_simple_latest']}")
        OutputControls().printOutput(f"  [+] Saved live dashboard: {paths['dashboard_latest']}")
        OutputControls().printOutput(f"  [+] Live portfolio DB: {self.store.db_path}")

    def _print_summary(
        self,
        summary_df: pd.DataFrame,
        positions_df: pd.DataFrame,
        cashflow_events: List[str],
        entry_events: List[str],
        exit_events: List[str],
    ) -> None:
        """Print a concise console summary with live/manual wording."""
        summary = summary_df.iloc[0].to_dict()
        OutputControls().printOutput(
            "  [+] Live portfolio: "
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
            OutputControls().printOutput("  [+] New live entries:")
            for event in entry_events[:8]:
                OutputControls().printOutput(f"    {event}")

        if exit_events:
            OutputControls().printOutput("  [+] Closed live positions:")
            for event in exit_events[:8]:
                OutputControls().printOutput(f"    {event}")

        if positions_df is None or positions_df.empty:
            OutputControls().printOutput("  [+] No open live positions right now.")
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

    def _latest_path(self, name: str) -> str:
        return f"{self.output_dir}\\livetrade_{self.account_name}_{name}_latest.csv"

    def _dated_path(self, name: str) -> str:
        return f"{self.output_dir}\\livetrade_{self.account_name}_{name}_{self.timestamp}.csv"

    def _dashboard_latest_path(self) -> str:
        return f"{self.output_dir}\\livetrade_{self.account_name}_dashboard_latest.html"

    def _dashboard_dated_path(self) -> str:
        return f"{self.output_dir}\\livetrade_{self.account_name}_dashboard_{self.timestamp}.html"
