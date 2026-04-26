"""
Daily truth report for live alerts and Kite execution.

The report joins signal outcomes, engine auto-buy decisions, and Kite orders so
we can see which alerts were bought, why others were skipped, and whether the
skips were actually protective.
"""

from __future__ import annotations

import json
import os
import html
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import pandas as pd

from PKDevTools.classes import Archiver

from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


@dataclass
class TruthReportPaths:
    detail: str
    summary: str
    benchmarks: str
    dashboard: str
    detail_latest: str
    summary_latest: str
    benchmarks_latest: str
    dashboard_latest: str


class DailyTruthReport:
    """Build a daily post-market report from saved runtime artifacts."""

    DETAIL_COLUMNS = [
        "Date",
        "Symbol",
        "AlertTime",
        "SignalAction",
        "EntryPrice",
        "SuggestedQty",
        "SignalStrengthPct",
        "Checks",
        "Why",
        "AutoEligible",
        "AutoStatus",
        "AutoReason",
        "AutoBuyOutcome",
        "Bought",
        "OrderId",
        "OrderStatus",
        "OrderMessage",
        "FillPrice",
        "Qty",
        "Invested",
        "ExitReferencePrice",
        "ReturnEODPct",
        "ReturnNextDayPct",
        "MaxFavorablePct",
        "MaxAdversePct",
        "StopHit",
        "Target1Hit",
        "Target2Hit",
        "GrossPnL",
        "EstimatedCharges",
        "EstimatedSlippageCost",
        "EstimatedSTCGTax",
        "NetPnLPreTax",
        "NetPnLAfterTax",
        "WhatIfNetPnLAfterTax",
        "LearningVerdict",
        "LearningNote",
    ]

    SUMMARY_COLUMNS = [
        "Date",
        "TotalAlerts",
        "UniqueAlertSymbols",
        "AutoEligibleAlerts",
        "BoughtCount",
        "UniqueBoughtSymbols",
        "SkippedCount",
        "UniqueSkippedSymbols",
        "SkippedWinners",
        "GateProtectedCount",
        "BoughtWinners",
        "BoughtLosers",
        "GrossPnL",
        "EstimatedCharges",
        "EstimatedSTCGTax",
        "NetPnLAfterTax",
        "WhatIfSkippedNetPnLAfterTax",
        "DeployedCapital",
        "AvgBoughtReturnEODPct",
        "AvgAllAlertReturnEODPct",
        "MarketRegime",
        "RegimeReason",
        "GeneratedAt",
    ]

    BENCHMARK_COLUMNS = [
        "Date",
        "Benchmark",
        "SymbolUsed",
        "ReturnPct",
        "EquivalentPnLOnDeployed",
        "Status",
    ]

    BENCHMARK_CANDIDATES = {
        "Nifty 50": ("NIFTY 50", "NIFTYBEES"),
        "Nifty 500": ("NIFTY 500", "NIFTY500"),
        "Nifty Midcap": ("NIFTY MIDCAP 150", "MID150BEES"),
        "Nifty Smallcap": ("NIFTY SMALLCAP 250", "SMALLCAP"),
        "Simple index SIP alternative": ("NIFTYBEES", "NIFTY 50"),
    }

    def __init__(
        self,
        report_date: Optional[str] = None,
        universe: str = "allstocks",
        account_name: str = "main",
        output_dir: str = "",
        fetch_benchmarks: bool = False,
    ) -> None:
        self.output_dir = output_dir or Archiver.get_user_data_dir()
        self.universe = str(universe or "allstocks").strip().lower()
        self.account_name = str(account_name or "main").strip()
        self.report_date = self._parse_date(report_date)
        self.fetch_benchmarks = bool(fetch_benchmarks)
        self.generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.history_provider = KiteHistoricalDataProvider()
        self.buy_cost_pct = _env_float("CHARAN_EST_BUY_COST_PCT", 0.00118)
        self.sell_cost_pct = _env_float("CHARAN_EST_SELL_COST_PCT", 0.00104)
        self.slippage_bps = _env_float("CHARAN_EST_SLIPPAGE_BPS", 0.0)
        self.stcg_tax_pct = _env_float("CHARAN_STCG_TAX_PCT", 0.20)

    def build(self) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, TruthReportPaths]:
        alerts_df = self._load_signal_ledger()
        engine_df = self._load_engine_decisions()
        orders_df = self._load_orders()
        positions = self._load_positions()

        detail_df = self._build_detail(alerts_df, engine_df, orders_df, positions)
        bought_df = self._unique_bought_rows(detail_df)
        benchmarks_df = self._build_benchmarks(float(bought_df["Invested"].sum() or 0.0))
        summary_df = self._build_summary(detail_df, benchmarks_df)
        paths = self._save(detail_df, summary_df, benchmarks_df)
        return detail_df, summary_df, benchmarks_df, paths

    def _build_detail(
        self,
        alerts_df: pd.DataFrame,
        engine_df: pd.DataFrame,
        orders_df: pd.DataFrame,
        positions: Dict[str, dict],
    ) -> pd.DataFrame:
        rows = []
        for _, alert in alerts_df.iterrows():
            symbol = str(alert.get("Symbol", "")).strip().upper()
            alert_time = self._to_datetime(alert.get("AlertTime"))
            engine = self._nearest_symbol_row(engine_df, symbol, alert_time)
            order = self._nearest_symbol_row(orders_df, symbol, alert_time, side="BUY")

            bought = bool(self._truthy(order.get("success")) if order is not None else False)
            order_id = str(order.get("order_id", "") if order is not None else "").strip()
            order_status = str(order.get("status", "") if order is not None else "").strip()
            order_message = str(order.get("message", "") if order is not None else "").strip()
            fill_price = self._first_positive(order.get("price") if order is not None else 0, alert.get("EntryPrice"))
            qty = int(self._to_float(order.get("qty") if order is not None else 0, 0))
            if qty <= 0:
                qty = int(self._to_float(alert.get("SuggestedQty"), 0) if bought else 0)

            return_eod = self._to_float(alert.get("ReturnEODPct"), None)
            exit_reference = self._exit_reference_price(symbol, fill_price, return_eod, positions)
            economics = self._trade_economics(fill_price, exit_reference, qty)

            suggested_qty = int(self._to_float(alert.get("SuggestedQty"), 0) or 0)
            what_if_qty = 0 if bought else suggested_qty
            what_if = self._trade_economics(
                self._to_float(alert.get("EntryPrice"), 0),
                self._exit_reference_price(symbol, self._to_float(alert.get("EntryPrice"), 0), return_eod, positions),
                what_if_qty,
            )
            max_fav, max_adv = self._max_intraday_excursion(symbol, alert_time, self._to_float(alert.get("EntryPrice"), 0))
            auto_eligible = self._truthy(engine.get("AutoEligible")) if engine is not None else False
            auto_status = str(engine.get("AutoStatus", "") if engine is not None else "").strip()
            auto_reason = str(engine.get("AutoReason", "") if engine is not None else "").strip()
            auto_outcome = str(engine.get("AutoBuyOutcome", "") if engine is not None else "").strip()
            verdict, note = self._learning_verdict(
                bought=bought,
                auto_eligible=auto_eligible,
                return_eod=return_eod,
                net_pnl=economics["net_after_tax"],
                what_if_net=what_if["net_after_tax"],
                order_status=order_status,
                auto_reason=auto_reason,
            )

            rows.append(
                {
                    "Date": self.report_date.isoformat(),
                    "Symbol": symbol,
                    "AlertTime": self._format_dt(alert_time),
                    "SignalAction": str(alert.get("SignalAction", "")).strip(),
                    "EntryPrice": self._to_float(alert.get("EntryPrice"), 0),
                    "SuggestedQty": suggested_qty,
                    "SignalStrengthPct": self._to_float(alert.get("SignalStrengthPct"), 0),
                    "Checks": str(alert.get("Checks", "")).strip(),
                    "Why": str(alert.get("Why", "")).strip(),
                    "AutoEligible": auto_eligible,
                    "AutoStatus": auto_status,
                    "AutoReason": auto_reason,
                    "AutoBuyOutcome": auto_outcome,
                    "Bought": bought,
                    "OrderId": order_id,
                    "OrderStatus": order_status,
                    "OrderMessage": order_message,
                    "FillPrice": round(fill_price, 2),
                    "Qty": qty,
                    "Invested": round(fill_price * qty, 2),
                    "ExitReferencePrice": round(exit_reference, 2),
                    "ReturnEODPct": return_eod,
                    "ReturnNextDayPct": self._to_float(alert.get("ReturnNextDayPct"), None),
                    "MaxFavorablePct": max_fav,
                    "MaxAdversePct": max_adv,
                    "StopHit": self._truthy(alert.get("StopHit")),
                    "Target1Hit": self._truthy(alert.get("Target1Hit")),
                    "Target2Hit": self._truthy(alert.get("Target2Hit")),
                    "GrossPnL": economics["gross"],
                    "EstimatedCharges": economics["charges"],
                    "EstimatedSlippageCost": economics["slippage"],
                    "EstimatedSTCGTax": economics["tax"],
                    "NetPnLPreTax": economics["net_pre_tax"],
                    "NetPnLAfterTax": economics["net_after_tax"],
                    "WhatIfNetPnLAfterTax": what_if["net_after_tax"],
                    "LearningVerdict": verdict,
                    "LearningNote": note,
                }
            )

        if not rows:
            return pd.DataFrame(columns=self.DETAIL_COLUMNS)
        return pd.DataFrame(rows, columns=self.DETAIL_COLUMNS)

    def _build_summary(self, detail_df: pd.DataFrame, benchmarks_df: pd.DataFrame) -> pd.DataFrame:
        if detail_df is None or detail_df.empty:
            return pd.DataFrame(
                [
                    {
                        "Date": self.report_date.isoformat(),
                        "TotalAlerts": 0,
                        "GeneratedAt": self.generated_at,
                    }
                ],
                columns=self.SUMMARY_COLUMNS,
            )

        bought_df = self._unique_bought_rows(detail_df)
        skipped_df = detail_df[~detail_df["Bought"].astype(bool)].copy()
        regime, reason = self._market_regime(benchmarks_df)
        row = {
            "Date": self.report_date.isoformat(),
            "TotalAlerts": int(len(detail_df)),
            "UniqueAlertSymbols": int(detail_df["Symbol"].astype(str).str.upper().nunique()),
            "AutoEligibleAlerts": int(detail_df["AutoEligible"].astype(bool).sum()),
            "BoughtCount": int(len(bought_df)),
            "UniqueBoughtSymbols": int(bought_df["Symbol"].astype(str).str.upper().nunique()) if not bought_df.empty else 0,
            "SkippedCount": int(len(skipped_df)),
            "UniqueSkippedSymbols": int(skipped_df["Symbol"].astype(str).str.upper().nunique()) if not skipped_df.empty else 0,
            "SkippedWinners": int(detail_df["LearningVerdict"].astype(str).eq("SKIPPED_WINNER_REVIEW_GATE").sum()),
            "GateProtectedCount": int(detail_df["LearningVerdict"].astype(str).eq("GATE_PROTECTED_CAPITAL").sum()),
            "BoughtWinners": int((bought_df["NetPnLAfterTax"] > 0).sum()) if not bought_df.empty else 0,
            "BoughtLosers": int((bought_df["NetPnLAfterTax"] < 0).sum()) if not bought_df.empty else 0,
            "GrossPnL": round(float(bought_df["GrossPnL"].sum() or 0.0), 2),
            "EstimatedCharges": round(float(bought_df["EstimatedCharges"].sum() or 0.0), 2),
            "EstimatedSTCGTax": round(float(bought_df["EstimatedSTCGTax"].sum() or 0.0), 2),
            "NetPnLAfterTax": round(float(bought_df["NetPnLAfterTax"].sum() or 0.0), 2),
            "WhatIfSkippedNetPnLAfterTax": round(float(detail_df.loc[~detail_df["Bought"].astype(bool), "WhatIfNetPnLAfterTax"].sum() or 0.0), 2),
            "DeployedCapital": round(float(bought_df["Invested"].sum() or 0.0), 2),
            "AvgBoughtReturnEODPct": self._avg(bought_df["ReturnEODPct"]) if not bought_df.empty else None,
            "AvgAllAlertReturnEODPct": self._avg(detail_df["ReturnEODPct"]),
            "MarketRegime": regime,
            "RegimeReason": reason,
            "GeneratedAt": self.generated_at,
        }
        return pd.DataFrame([row], columns=self.SUMMARY_COLUMNS)

    def _unique_bought_rows(self, detail_df: pd.DataFrame) -> pd.DataFrame:
        if detail_df is None or detail_df.empty:
            return pd.DataFrame(columns=self.DETAIL_COLUMNS)
        bought_df = detail_df[detail_df["Bought"].astype(bool)].copy()
        if bought_df.empty:
            return bought_df
        keys = []
        for _, row in bought_df.iterrows():
            order_id = str(row.get("OrderId", "") or "").strip()
            if order_id:
                keys.append(f"order:{order_id}")
            else:
                keys.append(
                    "fallback:"
                    + "|".join(
                        [
                            str(row.get("Symbol", "")),
                            str(row.get("FillPrice", "")),
                            str(row.get("Qty", "")),
                        ]
                    )
                )
        bought_df["_UniqueBuyKey"] = keys
        return bought_df.drop_duplicates("_UniqueBuyKey").drop(columns=["_UniqueBuyKey"])

    def _build_benchmarks(self, deployed_capital: float) -> pd.DataFrame:
        rows = []
        for name, candidates in self.BENCHMARK_CANDIDATES.items():
            symbol_used = ""
            ret = None
            status = "missing cached history"
            for symbol in candidates:
                ret = self._benchmark_return(symbol)
                if ret is not None:
                    symbol_used = symbol
                    status = "ok"
                    break
            rows.append(
                {
                    "Date": self.report_date.isoformat(),
                    "Benchmark": name,
                    "SymbolUsed": symbol_used,
                    "ReturnPct": ret,
                    "EquivalentPnLOnDeployed": round(deployed_capital * (ret or 0.0) / 100.0, 2),
                    "Status": status,
                }
            )
        return pd.DataFrame(rows, columns=self.BENCHMARK_COLUMNS)

    def _save(self, detail_df: pd.DataFrame, summary_df: pd.DataFrame, benchmarks_df: pd.DataFrame) -> TruthReportPaths:
        out = Path(self.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = self.report_date.strftime("%Y%m%d")
        detail = out / f"daily_truth_report_{stamp}.csv"
        summary = out / f"daily_truth_summary_{stamp}.csv"
        benchmarks = out / f"daily_truth_benchmarks_{stamp}.csv"
        dashboard = out / f"daily_truth_dashboard_{stamp}.html"
        detail_latest = out / "daily_truth_report_latest.csv"
        summary_latest = out / "daily_truth_summary_latest.csv"
        benchmarks_latest = out / "daily_truth_benchmarks_latest.csv"
        dashboard_latest = out / "daily_truth_dashboard_latest.html"
        detail_df.to_csv(detail, index=False)
        summary_df.to_csv(summary, index=False)
        benchmarks_df.to_csv(benchmarks, index=False)
        detail_df.to_csv(detail_latest, index=False)
        summary_df.to_csv(summary_latest, index=False)
        benchmarks_df.to_csv(benchmarks_latest, index=False)
        dashboard_html = self._dashboard_html(detail_df, summary_df, benchmarks_df)
        dashboard.write_text(dashboard_html, encoding="utf-8")
        dashboard_latest.write_text(dashboard_html, encoding="utf-8")
        return TruthReportPaths(
            detail=str(detail),
            summary=str(summary),
            benchmarks=str(benchmarks),
            dashboard=str(dashboard),
            detail_latest=str(detail_latest),
            summary_latest=str(summary_latest),
            benchmarks_latest=str(benchmarks_latest),
            dashboard_latest=str(dashboard_latest),
        )

    def _dashboard_html(self, detail_df: pd.DataFrame, summary_df: pd.DataFrame, benchmarks_df: pd.DataFrame) -> str:
        row = summary_df.iloc[0].to_dict() if summary_df is not None and not summary_df.empty else {}
        bought_df = self._unique_bought_rows(detail_df) if detail_df is not None and not detail_df.empty else pd.DataFrame()
        skipped_winners_df = (
            detail_df[detail_df["LearningVerdict"].astype(str).eq("SKIPPED_WINNER_REVIEW_GATE")].copy()
            if detail_df is not None and not detail_df.empty
            else pd.DataFrame()
        )
        gate_reasons_df = pd.DataFrame()
        if detail_df is not None and not detail_df.empty and "AutoReason" in detail_df.columns:
            gate_reasons_df = (
                detail_df.assign(AutoReason=detail_df["AutoReason"].fillna("").replace("", "not_logged"))
                .groupby("AutoReason", dropna=False)
                .size()
                .reset_index(name="Count")
                .sort_values("Count", ascending=False)
                .head(12)
            )

        def money(value) -> str:
            try:
                return f"Rs {float(value):,.0f}"
            except Exception:
                return "Rs 0"

        def pct(value) -> str:
            try:
                if value is None or pd.isna(value):
                    return "-"
                return f"{float(value):+.2f}%"
            except Exception:
                return "-"

        cards = [
            ("Net P/L after tax est.", money(row.get("NetPnLAfterTax", 0))),
            ("Bought / unique symbols", f"{int(row.get('BoughtCount', 0) or 0)} / {int(row.get('UniqueBoughtSymbols', 0) or 0)}"),
            ("Skipped winners to review", str(int(row.get("SkippedWinners", 0) or 0))),
            ("Gate protected capital", str(int(row.get("GateProtectedCount", 0) or 0))),
            ("Deployed capital", money(row.get("DeployedCapital", 0))),
            ("Market regime", str(row.get("MarketRegime", "UNKNOWN") or "UNKNOWN")),
        ]

        def table(df: pd.DataFrame, columns: list, limit: int = 15) -> str:
            if df is None or df.empty:
                return "<p class='muted'>No rows.</p>"
            view = df.loc[:, [c for c in columns if c in df.columns]].head(limit).copy()
            head = "".join(f"<th>{html.escape(str(c))}</th>" for c in view.columns)
            body_rows = []
            for _, data_row in view.iterrows():
                cells = "".join(f"<td>{html.escape(str(data_row.get(c, '')))}</td>" for c in view.columns)
                body_rows.append(f"<tr>{cells}</tr>")
            return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"

        benchmark_table = table(benchmarks_df, ["Benchmark", "SymbolUsed", "ReturnPct", "EquivalentPnLOnDeployed", "Status"], 10)
        bought_table = table(
            bought_df.sort_values("NetPnLAfterTax", ascending=False) if not bought_df.empty else bought_df,
            ["Symbol", "FillPrice", "Qty", "Invested", "ReturnEODPct", "NetPnLAfterTax", "OrderStatus", "LearningVerdict"],
            12,
        )
        skipped_table = table(
            skipped_winners_df.sort_values("WhatIfNetPnLAfterTax", ascending=False) if not skipped_winners_df.empty else skipped_winners_df,
            ["Symbol", "EntryPrice", "SuggestedQty", "ReturnEODPct", "WhatIfNetPnLAfterTax", "AutoReason", "LearningNote"],
            12,
        )
        reasons_table = table(gate_reasons_df, ["AutoReason", "Count"], 12)
        card_html = "".join(
            f"<div class='card'><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
            for label, value in cards
        )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Truth Dashboard {html.escape(self.report_date.isoformat())}</title>
  <style>
    :root {{ --ink:#18212f; --muted:#647084; --line:#dbe3ee; --bg:#f5f7fb; --card:#ffffff; --accent:#0f766e; }}
    body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1180px; margin:0 auto; padding:28px 18px 44px; }}
    h1 {{ margin:0 0 6px; font-size:30px; }}
    h2 {{ margin:28px 0 12px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(170px,1fr)); gap:12px; margin:22px 0; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px; box-shadow:0 10px 28px rgba(24,33,47,.06); }}
    .card span {{ display:block; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    .card strong {{ display:block; margin-top:8px; font-size:22px; }}
    .panel {{ background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px; overflow:auto; box-shadow:0 10px 28px rgba(24,33,47,.05); }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th, td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }}
    .trust {{ border-left:4px solid var(--accent); }}
  </style>
</head>
<body>
<main>
  <h1>Daily Truth Dashboard</h1>
  <p class="muted">{html.escape(self.report_date.isoformat())} | Generated {html.escape(self.generated_at)} | {html.escape(str(row.get("RegimeReason", "")))}</p>
  <section class="cards">{card_html}</section>
  <section class="panel trust">
    <h2>Trust Summary</h2>
    <p>Total alerts: {int(row.get("TotalAlerts", 0) or 0)} across {int(row.get("UniqueAlertSymbols", 0) or 0)} symbols. Avg bought EOD return: {pct(row.get("AvgBoughtReturnEODPct"))}. Avg all-alert EOD return: {pct(row.get("AvgAllAlertReturnEODPct"))}. This page separates actual filled buys from skipped ideas so we can improve gates without storytelling.</p>
  </section>
  <h2>Benchmark Check</h2><section class="panel">{benchmark_table}</section>
  <h2>Filled Buys</h2><section class="panel">{bought_table}</section>
  <h2>Skipped Winners To Review</h2><section class="panel">{skipped_table}</section>
  <h2>Top Skip Reasons</h2><section class="panel">{reasons_table}</section>
</main>
</body>
</html>"""

    def _load_signal_ledger(self) -> pd.DataFrame:
        candidates = [
            Path(self.output_dir) / f"signalperf_{self.universe}_ledger_latest.csv",
            Path(self.output_dir) / "signalperf_all_ledger_latest.csv",
            Path(self.output_dir) / "signalperf_allstocks_ledger_latest.csv",
        ]
        df = self._read_first_csv(candidates)
        if df.empty:
            return pd.DataFrame()
        df["AlertDate"] = pd.to_datetime(df.get("AlertTime"), errors="coerce").dt.date
        return df[df["AlertDate"] == self.report_date].copy()

    def _load_engine_decisions(self) -> pd.DataFrame:
        path = Path(self.output_dir) / f"engine_confirmed_buys_{self.account_name}.csv"
        if not path.exists():
            return pd.DataFrame()
        df = self._read_csv(path)
        if df.empty:
            return df
        df["DecisionTime"] = self._datetime_series(df.get("Timestamp"))
        df["DecisionDate"] = df["DecisionTime"].dt.date
        return df[df["DecisionDate"] == self.report_date].copy()

    def _load_orders(self) -> pd.DataFrame:
        path = Path(self.output_dir) / "kite_orders_log.csv"
        if not path.exists():
            return pd.DataFrame()
        df = self._read_csv(path)
        if df.empty:
            return df
        df["OrderTime"] = self._datetime_series(df.get("timestamp"))
        df["OrderDate"] = df["OrderTime"].dt.date
        df["symbol"] = df.get("symbol", "").astype(str).str.strip().str.upper()
        return df[df["OrderDate"] == self.report_date].copy()

    def _load_positions(self) -> Dict[str, dict]:
        path = Path(self.output_dir) / "kite_tracked_positions.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {str(k).upper(): v for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _nearest_symbol_row(
        self,
        df: pd.DataFrame,
        symbol: str,
        alert_time: Optional[pd.Timestamp],
        side: str = "",
    ) -> Optional[pd.Series]:
        if df is None or df.empty or not symbol:
            return None
        symbol_col = "symbol" if "symbol" in df.columns else "Symbol"
        rows = df[df[symbol_col].astype(str).str.upper() == symbol].copy()
        if side and "side" in rows.columns:
            rows = rows[rows["side"].astype(str).str.upper() == side.upper()]
        if rows.empty:
            return None
        time_col = "OrderTime" if "OrderTime" in rows.columns else "DecisionTime"
        if alert_time is None or time_col not in rows.columns:
            return rows.iloc[0]
        rows["_DistanceSec"] = (rows[time_col] - alert_time).abs().dt.total_seconds()
        return rows.sort_values("_DistanceSec").iloc[0]

    def _exit_reference_price(
        self,
        symbol: str,
        entry_price: float,
        return_eod_pct: Optional[float],
        positions: Dict[str, dict],
    ) -> float:
        pos = positions.get(str(symbol or "").upper(), {})
        sell_price = self._to_float(pos.get("sell_price"), 0) if pos else 0
        if sell_price > 0:
            return sell_price
        if return_eod_pct is not None and entry_price > 0:
            return entry_price * (1.0 + (return_eod_pct / 100.0))
        current_price = self._to_float(pos.get("highest_price"), 0) if pos else 0
        return current_price if current_price > 0 else entry_price

    def _trade_economics(self, entry_price: float, exit_price: float, qty: int) -> Dict[str, float]:
        if entry_price <= 0 or exit_price <= 0 or qty <= 0:
            return {"gross": 0.0, "charges": 0.0, "slippage": 0.0, "tax": 0.0, "net_pre_tax": 0.0, "net_after_tax": 0.0}
        slip = max(0.0, self.slippage_bps) / 10000.0
        exec_entry = entry_price * (1.0 + slip)
        exec_exit = exit_price * (1.0 - slip)
        entry_turnover = exec_entry * qty
        exit_turnover = exec_exit * qty
        gross = (exit_price - entry_price) * qty
        charges = (entry_turnover * max(0.0, self.buy_cost_pct)) + (exit_turnover * max(0.0, self.sell_cost_pct))
        slippage_cost = ((exec_entry - entry_price) + (exit_price - exec_exit)) * qty
        net_pre_tax = (exec_exit - exec_entry) * qty - charges
        tax = max(0.0, net_pre_tax) * max(0.0, self.stcg_tax_pct)
        return {
            "gross": round(gross, 2),
            "charges": round(charges, 2),
            "slippage": round(slippage_cost, 2),
            "tax": round(tax, 2),
            "net_pre_tax": round(net_pre_tax, 2),
            "net_after_tax": round(net_pre_tax - tax, 2),
        }

    def _max_intraday_excursion(
        self,
        symbol: str,
        alert_time: Optional[pd.Timestamp],
        entry_price: float,
    ) -> Tuple[Optional[float], Optional[float]]:
        if not symbol or alert_time is None or entry_price <= 0:
            return None, None
        df = self.history_provider.get_cached_stock_data(symbol, interval="5minute", count=5000)
        if df is None or df.empty:
            return None, None
        try:
            day_df = df[(df.index.date == self.report_date) & (df.index >= alert_time)]
        except Exception:
            return None, None
        if day_df.empty:
            return None, None
        high = float(day_df["high"].max()) if "high" in day_df.columns else float(day_df["close"].max())
        low = float(day_df["low"].min()) if "low" in day_df.columns else float(day_df["close"].min())
        return round((high - entry_price) * 100.0 / entry_price, 2), round((low - entry_price) * 100.0 / entry_price, 2)

    def _benchmark_return(self, symbol: str) -> Optional[float]:
        df = self.history_provider.get_cached_stock_data(symbol, interval="day", count=30)
        if (df is None or df.empty) and self.fetch_benchmarks:
            df = self.history_provider.get_stock_data(symbol, interval="day", count=30)
        if df is None or df.empty or "close" not in df.columns:
            return None
        df = df.sort_index()
        rows = df[df.index.date <= self.report_date].tail(2)
        if len(rows) < 2:
            return None
        prev_close = float(rows["close"].iloc[0])
        close = float(rows["close"].iloc[1])
        if prev_close <= 0 or close <= 0:
            return None
        return round((close - prev_close) * 100.0 / prev_close, 2)

    def _market_regime(self, benchmarks_df: pd.DataFrame) -> Tuple[str, str]:
        if benchmarks_df is None or benchmarks_df.empty:
            return "UNKNOWN", "No benchmark data available"
        usable = benchmarks_df[benchmarks_df["ReturnPct"].notna()].copy()
        if usable.empty:
            return "UNKNOWN", "No benchmark return data available"
        nifty = self._benchmark_value(usable, "Nifty 50")
        median = float(usable["ReturnPct"].median())
        if nifty is not None and nifty <= -1.0:
            return "CRASH_RISK", f"Nifty 50 down {nifty:.2f}%"
        if median <= -0.75:
            return "CRASH_RISK", f"Median benchmark down {median:.2f}%"
        if nifty is not None and nifty >= 0.4 and median > 0:
            return "BULLISH", f"Nifty 50 {nifty:.2f}%, median benchmark {median:.2f}%"
        return "CHOPPY", f"Nifty 50 {nifty if nifty is not None else 0:.2f}%, median benchmark {median:.2f}%"

    def _learning_verdict(
        self,
        bought: bool,
        auto_eligible: bool,
        return_eod: Optional[float],
        net_pnl: float,
        what_if_net: float,
        order_status: str,
        auto_reason: str,
    ) -> Tuple[str, str]:
        if bought:
            if net_pnl > 0:
                return "BOUGHT_WINNER", "Good execution; keep this profile in the preferred bucket."
            if net_pnl < 0:
                return "BOUGHT_LOSER_REVIEW_FILTERS", "Bought setup lost after costs; review timing, regime, and fade risk."
            return "BOUGHT_FLAT", "Bought setup was roughly flat after costs."
        if auto_eligible and "FAILED" in str(order_status).upper():
            return "EXECUTION_FAILURE", "Setup was eligible but broker execution failed."
        if return_eod is not None and return_eod > 1.0 and what_if_net > 0:
            return "SKIPPED_WINNER_REVIEW_GATE", f"Skipped setup worked; review gate reason: {auto_reason or 'unknown'}."
        if return_eod is not None and return_eod <= 0:
            return "GATE_PROTECTED_CAPITAL", "Skip avoided a weak or losing setup."
        if auto_eligible:
            return "ELIGIBLE_NOT_BOUGHT_REVIEW", "Eligible setup was not bought; inspect broker/order constraints."
        return "SKIPPED_NEUTRAL", f"Skipped setup needs more data; gate reason: {auto_reason or 'not logged'}."

    def _benchmark_value(self, df: pd.DataFrame, benchmark: str) -> Optional[float]:
        rows = df[df["Benchmark"].astype(str) == benchmark]
        if rows.empty:
            return None
        value = rows["ReturnPct"].iloc[0]
        return None if pd.isna(value) else float(value)

    def _read_first_csv(self, paths: Iterable[Path]) -> pd.DataFrame:
        for path in paths:
            if path.exists():
                df = self._read_csv(path)
                if not df.empty:
                    return df
        return pd.DataFrame()

    def _read_csv(self, path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()

    def _parse_date(self, value: Optional[str]) -> date:
        if value:
            parsed = pd.to_datetime(value, errors="coerce")
            if not pd.isna(parsed):
                return parsed.date()
        return datetime.now().date()

    def _to_datetime(self, value) -> Optional[pd.Timestamp]:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        try:
            return parsed.tz_localize(None) if getattr(parsed, "tzinfo", None) is not None else parsed
        except Exception:
            return parsed

    def _datetime_series(self, values) -> pd.Series:
        return pd.to_datetime(values, errors="coerce", utc=True).dt.tz_convert(None)

    def _format_dt(self, value: Optional[pd.Timestamp]) -> str:
        if value is None or pd.isna(value):
            return ""
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")

    def _to_float(self, value, fallback: Optional[float] = 0.0) -> Optional[float]:
        try:
            if value in [None, "", "None"] or pd.isna(value):
                return fallback
            return float(value)
        except Exception:
            return fallback

    def _first_positive(self, *values) -> float:
        for value in values:
            numeric = self._to_float(value, 0)
            if numeric and numeric > 0:
                return numeric
        return 0.0

    def _truthy(self, value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "complete", "placed"}

    def _avg(self, series: pd.Series) -> Optional[float]:
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return None
        return round(float(numeric.mean()), 2)
