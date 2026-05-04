"""
SQLite-backed data store for the wealth basket engine.

All tables carry a `wb_` prefix to avoid collision with intraday portfolio
tables in the same assistant_portfolio.db database.

Schema overview
---------------
wb_fundamentals    — fundamental snapshot per stock per fetch date
wb_scores          — computed multi-factor scores per stock per month
wb_holdings        — current basket holdings (one row per stock)
wb_sip_tranches    — each monthly capital deployment (planned + executed)
wb_cashflows       — monthly deposits into the basket
wb_benchmark       — daily NIFTY 50 / NIFTY 500 / flat-12% prices
wb_performance     — rolling basket vs benchmark performance snapshots
"""

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class WealthBasketDB:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                -- Fundamental snapshot per stock. Refreshed at most once per 30 days.
                -- trust_level: LOW=yfinance only, MEDIUM=two sources agree, CONFLICT=sources disagree
                -- missing_fields_json: JSON list of field names that returned None
                -- conflict_fields: pipe-separated field names where sources disagreed
                CREATE TABLE IF NOT EXISTS wb_fundamentals (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol               TEXT NOT NULL,
                    fetch_date           TEXT NOT NULL,
                    source               TEXT DEFAULT 'yfinance',
                    trust_level          TEXT DEFAULT 'LOW',
                    secondary_available  INTEGER DEFAULT 0,
                    conflict_fields      TEXT DEFAULT '',
                    fields_populated     INTEGER DEFAULT 0,
                    fields_total         INTEGER DEFAULT 0,
                    missing_fields_json  TEXT DEFAULT '[]',
                    market_cap_cr        REAL,
                    pe_ratio             REAL,
                    pb_ratio             REAL,
                    roe_pct              REAL,
                    roce_pct             REAL,
                    debt_equity          REAL,
                    revenue_growth_yoy   REAL,
                    profit_growth_yoy    REAL,
                    gross_margin_pct     REAL,
                    operating_margin_pct REAL,
                    net_margin_pct       REAL,
                    current_ratio        REAL,
                    sector               TEXT,
                    industry             TEXT,
                    raw_json             TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_wb_fund_sym_date
                    ON wb_fundamentals(symbol, fetch_date DESC);

                -- Computed multi-factor scores per stock per scoring run.
                CREATE TABLE IF NOT EXISTS wb_scores (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    score_date      TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    sector          TEXT,
                    quality_score   REAL,
                    momentum_score  REAL,
                    valuation_score REAL,
                    combined_score  REAL,
                    trust_level     TEXT,
                    action          TEXT,
                    action_reason   TEXT,
                    risks           TEXT,
                    confidence      TEXT,
                    holding_period  TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_wb_scores_date_sym
                    ON wb_scores(score_date, symbol);

                -- Current basket holdings. One row per symbol; updated on each add/reduce/exit.
                CREATE TABLE IF NOT EXISTS wb_holdings (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT NOT NULL UNIQUE,
                    sector          TEXT,
                    first_buy_date  TEXT,
                    last_buy_date   TEXT,
                    total_qty       REAL DEFAULT 0,
                    avg_cost        REAL DEFAULT 0,
                    total_invested  REAL DEFAULT 0,
                    current_price   REAL DEFAULT 0,
                    current_value   REAL DEFAULT 0,
                    unrealized_pnl  REAL DEFAULT 0,
                    unrealized_pct  REAL DEFAULT 0,
                    last_action     TEXT DEFAULT 'HOLD',
                    last_score      REAL DEFAULT 0,
                    active          INTEGER DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_wb_holdings_active
                    ON wb_holdings(active, symbol);

                -- One row per stock per monthly SIP deployment (planned and/or executed).
                CREATE TABLE IF NOT EXISTS wb_sip_tranches (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    tranche_date TEXT NOT NULL,
                    symbol       TEXT NOT NULL,
                    sector       TEXT,
                    action       TEXT,
                    qty          REAL,
                    price        REAL,
                    amount       REAL,
                    score        REAL,
                    confidence   TEXT,
                    reason       TEXT,
                    executed     INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_wb_sip_date
                    ON wb_sip_tranches(tranche_date DESC, symbol);

                -- Monthly capital deposits.
                CREATE TABLE IF NOT EXISTS wb_cashflows (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    flow_date    TEXT NOT NULL,
                    amount       REAL NOT NULL,
                    deployed     REAL DEFAULT 0,
                    held_cash    REAL DEFAULT 0,
                    reason       TEXT DEFAULT 'monthly_sip'
                );

                -- Daily benchmark prices (NIFTY 50, NIFTY 500, flat 12% line).
                CREATE TABLE IF NOT EXISTS wb_benchmark (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    price_date TEXT NOT NULL UNIQUE,
                    nifty50    REAL,
                    nifty500   REAL,
                    flat_12pct REAL
                );

                -- Rolling performance vs benchmarks, computed monthly.
                CREATE TABLE IF NOT EXISTS wb_performance (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    calc_date        TEXT NOT NULL UNIQUE,
                    basket_1m        REAL,
                    basket_3m        REAL,
                    basket_6m        REAL,
                    basket_12m       REAL,
                    nifty50_1m       REAL,
                    nifty50_3m       REAL,
                    nifty50_6m       REAL,
                    nifty50_12m      REAL,
                    nifty500_1m      REAL,
                    nifty500_3m      REAL,
                    nifty500_6m      REAL,
                    nifty500_12m     REAL,
                    underperform_6m  INTEGER DEFAULT 0,
                    underperform_12m INTEGER DEFAULT 0,
                    notes            TEXT
                );

                -- Monthly portfolio value snapshots.
                -- Used to compute actual basket returns vs benchmarks.
                -- basket_total = holdings market value + cash_held
                -- nifty50_value / nifty500_value: index level on snapshot date
                -- monthly_sip: capital added this month (for TWR calculation)
                CREATE TABLE IF NOT EXISTS wb_portfolio_snapshots (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date       TEXT NOT NULL UNIQUE,
                    total_invested      REAL DEFAULT 0,
                    holdings_value      REAL DEFAULT 0,
                    cash_held           REAL DEFAULT 0,
                    basket_total        REAL DEFAULT 0,
                    nifty50_value       REAL,
                    nifty500_value      REAL,
                    monthly_sip         REAL DEFAULT 0,
                    cumulative_invested REAL DEFAULT 0,
                    holdings_json       TEXT DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_wb_snapshots_date
                    ON wb_portfolio_snapshots(snapshot_date DESC);
            """)
            # Additive migrations — safe to run on any existing DB.
            # Each ALTER TABLE is wrapped in a try/except so re-running is idempotent.
            _migrations = [
                "ALTER TABLE wb_fundamentals ADD COLUMN roce_pct             REAL",
                "ALTER TABLE wb_fundamentals ADD COLUMN secondary_available  INTEGER DEFAULT 0",
                "ALTER TABLE wb_fundamentals ADD COLUMN conflict_fields       TEXT DEFAULT ''",
                "ALTER TABLE wb_fundamentals ADD COLUMN fields_populated      INTEGER DEFAULT 0",
                "ALTER TABLE wb_fundamentals ADD COLUMN fields_total          INTEGER DEFAULT 0",
                "ALTER TABLE wb_fundamentals ADD COLUMN missing_fields_json   TEXT DEFAULT '[]'",
                # D/E normalization (added after discovering yfinance %-scale issue)
                "ALTER TABLE wb_fundamentals ADD COLUMN raw_debt_equity       REAL",
                "ALTER TABLE wb_fundamentals ADD COLUMN is_financial          INTEGER DEFAULT 0",
            ]
            for stmt in _migrations:
                try:
                    conn.execute(stmt)
                except Exception:
                    pass   # column already exists — expected on second run

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    def save_fundamentals(self, symbol: str, data: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wb_fundamentals (
                    symbol, fetch_date, source, trust_level,
                    secondary_available, conflict_fields,
                    fields_populated, fields_total, missing_fields_json,
                    market_cap_cr, pe_ratio, pb_ratio, roe_pct, roce_pct,
                    raw_debt_equity, debt_equity, revenue_growth_yoy, profit_growth_yoy,
                    gross_margin_pct, operating_margin_pct, net_margin_pct,
                    current_ratio, sector, industry, is_financial, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(symbol).upper(),
                    _utc_today(),
                    data.get("source", "yfinance"),
                    data.get("trust_level", "LOW"),
                    1 if data.get("secondary_available") else 0,
                    data.get("conflict_fields", ""),
                    data.get("fields_populated", 0),
                    data.get("fields_total", 0),
                    json.dumps(data.get("missing_fields", [])),
                    data.get("market_cap_cr"),
                    data.get("pe_ratio"),
                    data.get("pb_ratio"),
                    data.get("roe_pct"),
                    data.get("roce_pct"),
                    data.get("raw_debt_equity"),
                    data.get("debt_equity"),
                    data.get("revenue_growth_yoy"),
                    data.get("profit_growth_yoy"),
                    data.get("gross_margin_pct"),
                    data.get("operating_margin_pct"),
                    data.get("net_margin_pct"),
                    data.get("current_ratio"),
                    data.get("sector"),
                    data.get("industry"),
                    1 if data.get("is_financial") else 0,
                    json.dumps(data.get("raw", {})),
                ),
            )

    def get_fundamentals(self, symbol: str, max_age_days: int = 30) -> Optional[Dict]:
        """Return most recent fundamentals if fresh enough, else None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wb_fundamentals WHERE symbol=? ORDER BY fetch_date DESC, id DESC LIMIT 1",
                (str(symbol).upper(),),
            ).fetchone()
        if not row:
            return None
        age = (date.today() - date.fromisoformat(row["fetch_date"])).days
        if max_age_days == 0 or age > max_age_days:
            return None
        d = dict(row)
        # Deserialise JSON fields
        try:
            d["missing_fields"] = json.loads(d.get("missing_fields_json") or "[]")
        except (ValueError, TypeError):
            d["missing_fields"] = []
        return d

    # ------------------------------------------------------------------
    # Scores
    # ------------------------------------------------------------------

    def save_score(self, score_date: str, symbol: str, sector: str,
                   quality: float, momentum: float, valuation: float,
                   combined: float, trust: str, action: str,
                   reason: str, risks: str, confidence: str,
                   holding_period: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wb_scores (
                    score_date, symbol, sector,
                    quality_score, momentum_score, valuation_score, combined_score,
                    trust_level, action, action_reason, risks, confidence, holding_period
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(score_date, symbol) DO UPDATE SET
                    sector=excluded.sector,
                    quality_score=excluded.quality_score,
                    momentum_score=excluded.momentum_score,
                    valuation_score=excluded.valuation_score,
                    combined_score=excluded.combined_score,
                    trust_level=excluded.trust_level,
                    action=excluded.action,
                    action_reason=excluded.action_reason,
                    risks=excluded.risks,
                    confidence=excluded.confidence,
                    holding_period=excluded.holding_period
                """,
                (score_date, str(symbol).upper(), sector,
                 quality, momentum, valuation, combined,
                 trust, action, reason, risks, confidence, holding_period),
            )

    def get_latest_scores(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM wb_scores
                WHERE score_date = (SELECT MAX(score_date) FROM wb_scores)
                ORDER BY combined_score DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_scores_for_date(self, score_date: str) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wb_scores WHERE score_date=? ORDER BY combined_score DESC",
                (score_date,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Holdings
    # ------------------------------------------------------------------

    def upsert_holding(self, symbol: str, sector: str, qty_delta: float,
                       price: float, action: str, score: float) -> None:
        """Add or update a holding after a SIP tranche is executed."""
        sym = str(symbol).upper()
        today = _utc_today()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM wb_holdings WHERE symbol=?", (sym,)
            ).fetchone()
            if existing:
                old_qty = float(existing["total_qty"] or 0)
                old_invested = float(existing["total_invested"] or 0)
                new_qty = old_qty + qty_delta
                new_invested = old_invested + qty_delta * price
                new_avg = new_invested / max(new_qty, 0.001)
                conn.execute(
                    """
                    UPDATE wb_holdings SET
                        last_buy_date=?, total_qty=?, avg_cost=?,
                        total_invested=?, last_action=?, last_score=?, active=1
                    WHERE symbol=?
                    """,
                    (today, new_qty, new_avg, new_invested, action, score, sym),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO wb_holdings (
                        symbol, sector, first_buy_date, last_buy_date,
                        total_qty, avg_cost, total_invested,
                        last_action, last_score
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (sym, sector, today, today,
                     qty_delta, price, qty_delta * price, action, score),
                )

    def update_holding_prices(self, prices: Dict[str, float]) -> None:
        """Update current_price, current_value, unrealized P&L for all holdings."""
        with self._connect() as conn:
            for sym, price in prices.items():
                row = conn.execute(
                    "SELECT total_qty, total_invested FROM wb_holdings WHERE symbol=? AND active=1",
                    (str(sym).upper(),),
                ).fetchone()
                if not row:
                    continue
                qty = float(row["total_qty"] or 0)
                invested = float(row["total_invested"] or 0)
                curr_val = qty * price
                pnl = curr_val - invested
                pnl_pct = (pnl / invested * 100) if invested > 0 else 0
                conn.execute(
                    """
                    UPDATE wb_holdings SET
                        current_price=?, current_value=?,
                        unrealized_pnl=?, unrealized_pct=?
                    WHERE symbol=?
                    """,
                    (price, curr_val, pnl, pnl_pct, str(sym).upper()),
                )

    def get_holdings(self, active_only: bool = True) -> List[Dict]:
        with self._connect() as conn:
            where = "WHERE active=1" if active_only else ""
            rows = conn.execute(
                f"SELECT * FROM wb_holdings {where} ORDER BY total_invested DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # SIP tranches
    # ------------------------------------------------------------------

    def log_sip_tranche(self, tranche_date: str, symbol: str, sector: str,
                        action: str, amount: float, price: float,
                        qty: float, score: float, confidence: str,
                        reason: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO wb_sip_tranches (
                    tranche_date, symbol, sector, action,
                    qty, price, amount, score, confidence, reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (tranche_date, str(symbol).upper(), sector, action,
                 qty, price, amount, score, confidence, reason),
            )
            return int(cur.lastrowid or 0)

    def get_sip_tranches(self, tranche_date: str = None) -> List[Dict]:
        with self._connect() as conn:
            if tranche_date:
                rows = conn.execute(
                    "SELECT * FROM wb_sip_tranches WHERE tranche_date=? ORDER BY amount DESC",
                    (tranche_date,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wb_sip_tranches ORDER BY tranche_date DESC, amount DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Cashflows
    # ------------------------------------------------------------------

    def log_cashflow(self, amount: float, deployed: float = 0,
                     held_cash: float = 0, reason: str = "monthly_sip") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO wb_cashflows (flow_date, amount, deployed, held_cash, reason) VALUES (?,?,?,?,?)",
                (_utc_today(), amount, deployed, held_cash, reason),
            )

    def total_deployed(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT SUM(deployed) as s FROM wb_cashflows").fetchone()
        return float(row["s"] or 0) if row else 0.0

    # ------------------------------------------------------------------
    # Benchmark
    # ------------------------------------------------------------------

    def save_benchmark_day(self, price_date: str, nifty50: float,
                           nifty500: Optional[float], flat_12pct: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wb_benchmark (price_date, nifty50, nifty500, flat_12pct)
                VALUES (?,?,?,?)
                ON CONFLICT(price_date) DO UPDATE SET
                    nifty50=excluded.nifty50,
                    nifty500=excluded.nifty500,
                    flat_12pct=excluded.flat_12pct
                """,
                (price_date, nifty50, nifty500, flat_12pct),
            )

    def get_benchmark_history(self, from_date: str = None) -> List[Dict]:
        with self._connect() as conn:
            if from_date:
                rows = conn.execute(
                    "SELECT * FROM wb_benchmark WHERE price_date>=? ORDER BY price_date",
                    (from_date,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wb_benchmark ORDER BY price_date"
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Performance
    # ------------------------------------------------------------------

    def save_performance(self, data: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wb_performance (
                    calc_date,
                    basket_1m, basket_3m, basket_6m, basket_12m,
                    nifty50_1m, nifty50_3m, nifty50_6m, nifty50_12m,
                    nifty500_1m, nifty500_3m, nifty500_6m, nifty500_12m,
                    underperform_6m, underperform_12m, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(calc_date) DO UPDATE SET
                    basket_1m=excluded.basket_1m,
                    basket_3m=excluded.basket_3m,
                    basket_6m=excluded.basket_6m,
                    basket_12m=excluded.basket_12m,
                    nifty50_1m=excluded.nifty50_1m,
                    nifty50_6m=excluded.nifty50_6m,
                    nifty50_12m=excluded.nifty50_12m,
                    underperform_6m=excluded.underperform_6m,
                    underperform_12m=excluded.underperform_12m,
                    notes=excluded.notes
                """,
                (
                    data.get("calc_date", _utc_today()),
                    data.get("basket_1m"), data.get("basket_3m"),
                    data.get("basket_6m"), data.get("basket_12m"),
                    data.get("nifty50_1m"), data.get("nifty50_3m"),
                    data.get("nifty50_6m"), data.get("nifty50_12m"),
                    data.get("nifty500_1m"), data.get("nifty500_3m"),
                    data.get("nifty500_6m"), data.get("nifty500_12m"),
                    1 if data.get("underperform_6m") else 0,
                    1 if data.get("underperform_12m") else 0,
                    data.get("notes", ""),
                ),
            )

    def get_latest_performance(self) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wb_performance ORDER BY calc_date DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Portfolio snapshots
    # ------------------------------------------------------------------

    def save_portfolio_snapshot(
        self,
        snapshot_date: str,
        total_invested: float,
        holdings_value: float,
        cash_held: float,
        basket_total: float,
        nifty50_value: Optional[float],
        nifty500_value: Optional[float],
        monthly_sip: float,
        cumulative_invested: float,
        holdings: List[Dict],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wb_portfolio_snapshots (
                    snapshot_date, total_invested, holdings_value, cash_held,
                    basket_total, nifty50_value, nifty500_value,
                    monthly_sip, cumulative_invested, holdings_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    total_invested=excluded.total_invested,
                    holdings_value=excluded.holdings_value,
                    cash_held=excluded.cash_held,
                    basket_total=excluded.basket_total,
                    nifty50_value=excluded.nifty50_value,
                    nifty500_value=excluded.nifty500_value,
                    monthly_sip=excluded.monthly_sip,
                    cumulative_invested=excluded.cumulative_invested,
                    holdings_json=excluded.holdings_json
                """,
                (
                    snapshot_date,
                    round(total_invested, 2),
                    round(holdings_value, 2),
                    round(cash_held, 2),
                    round(basket_total, 2),
                    nifty50_value,
                    nifty500_value,
                    round(monthly_sip, 2),
                    round(cumulative_invested, 2),
                    json.dumps(holdings),
                ),
            )

    def get_portfolio_snapshots(self, limit: int = 24) -> List[Dict]:
        """Return portfolio snapshots sorted ascending (oldest first) for TWR calc."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wb_portfolio_snapshots ORDER BY snapshot_date ASC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["holdings"] = json.loads(d.get("holdings_json") or "[]")
            except (ValueError, TypeError):
                d["holdings"] = []
            result.append(d)
        return result

    def get_cumulative_invested(self) -> float:
        """Total capital deployed into the basket from cashflows."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(amount) as s FROM wb_cashflows"
            ).fetchone()
        return float(row["s"] or 0) if row else 0.0
