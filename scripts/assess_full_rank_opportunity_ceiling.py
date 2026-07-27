#!/usr/bin/env python3
"""Clairvoyant capacity ceiling for exact full-rank paper outcomes.

This diagnostic answers whether the recorded candidate universe contains enough
executable opportunity. It uses future outcomes and can never promote trading.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkscreener.classes.FullUniverseRankContractMatrixObserver import CONTRACTS
from scripts.assess_full_rank_contract_matrix import (
    load_matrix_events,
    matrix_session_coverage,
)
from scripts.assess_full_rank_capital_allocation import SLOT_COUNTS
from scripts.assess_exact_full_rank_paper import SELECTORS, replay_one_slot
from scripts.assess_full_universe_rank_strategy import raw_session_coverage


def _finite_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def trace_selector_regret(
    events: pd.DataFrame,
    dates: Iterable[str],
    *,
    selector: str,
    capital: float = 500_000.0,
    max_trades: int = 10,
    session_scope: str = "eligible_full_session",
) -> pd.DataFrame:
    """Explain each causal pick and each future-profitable candidate it skipped."""
    if selector not in SELECTORS:
        raise ValueError(f"unknown selector: {selector}")
    if events is None or events.empty:
        return pd.DataFrame()
    config = SELECTORS[selector]
    rows = []
    for date in sorted({str(value) for value in dates}):
        day = events[events["date"].astype(str).eq(date)].copy()
        if day.empty:
            continue
        day["entry_batch"] = pd.to_datetime(
            day["entry_time"], errors="coerce"
        ).dt.floor("min")
        day.sort_values(
            ["entry_batch", *config["columns"]],
            ascending=[True, *config["ascending"]],
            inplace=True,
        )
        active_exit = None
        traded_symbols: set[str] = set()
        day_trades = 0
        for batch, visible in day.groupby("entry_batch", sort=True):
            visible = visible.copy()
            visible["priority_in_batch"] = range(1, len(visible) + 1)
            if active_exit is not None and pd.Timestamp(active_exit) > pd.Timestamp(batch):
                decisions = [(row, "blocked_active_position", None) for _, row in visible.iterrows()]
            elif day_trades >= int(max_trades):
                decisions = [(row, "blocked_daily_trade_cap", None) for _, row in visible.iterrows()]
            else:
                repeat_mask = visible["Symbol"].astype(str).isin(traded_symbols)
                eligible = visible[~repeat_mask]
                selected_index = eligible.index[0] if not eligible.empty else None
                selected_return = (
                    float(visible.loc[selected_index, "NetReturnPct"])
                    if selected_index is not None
                    else None
                )
                decisions = []
                for index, row in visible.iterrows():
                    if bool(repeat_mask.loc[index]):
                        reason = "blocked_symbol_already_traded"
                    elif index == selected_index:
                        reason = "selected"
                    else:
                        reason = "lower_causal_priority_same_minute"
                    decisions.append((row, reason, selected_return))
                if selected_index is not None:
                    pick = visible.loc[selected_index]
                    active_exit = pick["exit_time"]
                    traded_symbols.add(str(pick["Symbol"]))
                    day_trades += 1

            for row, decision, selected_return in decisions:
                net_return = float(row["NetReturnPct"])
                # Keep every actual pick plus skipped winners. Skipped losing
                # rows add volume but no information to the regret diagnosis.
                if decision != "selected" and net_return <= 0.0:
                    continue
                selected_loss_ahead = bool(
                    decision == "lower_causal_priority_same_minute"
                    and selected_return is not None
                    and selected_return <= 0.0
                    and net_return > 0.0
                )
                rows.append(
                    {
                        "date": date,
                        "session_scope": session_scope,
                        "contract": str(row.get("Contract", "")),
                        "selector": selector,
                        "symbol": str(row.get("Symbol", "")),
                        "entry_time": row.get("entry_time"),
                        "exit_time": row.get("exit_time"),
                        "entry_batch": batch,
                        "priority_in_batch": int(row["priority_in_batch"]),
                        "decision": decision,
                        "selected": decision == "selected",
                        "future_positive": net_return > 0.0,
                        "selected_loss_ahead": selected_loss_ahead,
                        "net_return_pct": round(net_return, 6),
                        "diagnostic_pnl": round(
                            float(capital) * net_return / 100.0, 2
                        ),
                        "selected_return_pct": (
                            round(float(selected_return), 6)
                            if selected_return is not None
                            else pd.NA
                        ),
                        "day_change_rank": int(
                            _finite_number(row.get("DayChangeRank", 0))
                        ),
                        "day_change_pct": round(
                            _finite_number(row.get("DayChangePct", 0.0)), 6
                        ),
                        "rank_improvement": round(
                            _finite_number(row.get("RankImprovement", 0.0)), 6
                        ),
                        "rank_velocity_per_minute": round(
                            _finite_number(
                                row.get("RankVelocityPerMinute", 0.0)
                            ),
                            6,
                        ),
                        "entry_spread_bps": round(
                            _finite_number(row.get("EntrySpreadBps", 0.0)), 6
                        ),
                        "buyer_seller_ratio": round(
                            _finite_number(row.get("BuyerSellerRatio", 0.0)), 6
                        ),
                        "uses_future_outcome": True,
                        "diagnostic_only": True,
                        "can_promote_strategy": False,
                    }
                )
    return pd.DataFrame(rows)


def summarize_selector_regret(
    decisions: pd.DataFrame,
    *,
    expected_sessions: int | None = None,
) -> pd.DataFrame:
    """Aggregate causal mistakes without treating skipped P&L as additive."""
    if decisions is None or decisions.empty:
        return pd.DataFrame()
    rows = []
    keys = ["session_scope", "contract", "selector"]
    for key, group in decisions.groupby(keys, sort=True):
        selected = group[group["selected"].astype(bool)]
        missed = group[
            ~group["selected"].astype(bool)
            & group["future_positive"].astype(bool)
        ]
        selected_pnl = pd.to_numeric(
            selected["diagnostic_pnl"], errors="coerce"
        ).fillna(0.0)
        selected_return = pd.to_numeric(
            selected["net_return_pct"], errors="coerce"
        ).fillna(0.0)
        reason_counts = missed["decision"].value_counts().to_dict()
        sessions = (
            max(0, int(expected_sessions))
            if expected_sessions is not None
            else int(group["date"].astype(str).nunique())
        )
        selected_net_pnl = round(float(selected_pnl.sum()), 2)
        rows.append(
            {
                "session_scope": key[0],
                "contract": key[1],
                "selector": key[2],
                "sessions": sessions,
                "selected_trades": int(len(selected)),
                "selected_winners": int(selected_return.gt(0.0).sum()),
                "selected_losers": int(selected_return.le(0.0).sum()),
                "selected_win_rate_pct": round(
                    float(selected_return.gt(0.0).mean() * 100.0)
                    if len(selected_return)
                    else 0.0,
                    2,
                ),
                "selected_net_pnl": selected_net_pnl,
                "selected_avg_daily_pnl": round(
                    selected_net_pnl / sessions if sessions else 0.0, 2
                ),
                "missed_positive_observations": int(len(missed)),
                "missed_positive_symbols": int(
                    missed[["date", "symbol"]].drop_duplicates().shape[0]
                ),
                "missed_behind_selected_loser": int(
                    missed["selected_loss_ahead"].astype(bool).sum()
                ),
                "missed_blocked_active_position": int(
                    reason_counts.get("blocked_active_position", 0)
                ),
                "missed_lower_same_minute_priority": int(
                    reason_counts.get("lower_causal_priority_same_minute", 0)
                ),
                "missed_symbol_repeat": int(
                    reason_counts.get("blocked_symbol_already_traded", 0)
                ),
                "missed_daily_trade_cap": int(
                    reason_counts.get("blocked_daily_trade_cap", 0)
                ),
                "future_pnl_is_non_additive": True,
                "diagnostic_only": True,
                "paper_promotion_allowed": False,
                "real_orders_allowed": False,
            }
        )
    return pd.DataFrame(rows).sort_values(
        [
            "selected_net_pnl",
            "missed_behind_selected_loser",
            "selected_win_rate_pct",
        ],
        ascending=[False, True, False],
    )


def evaluate_selector_regret(
    events: pd.DataFrame,
    dates: list[str],
    *,
    capital: float,
    max_trades: int,
    session_scope: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    decisions = []
    for contract in CONTRACTS:
        contract_events = events[
            events["Contract"].astype(str).eq(contract)
        ].copy()
        for selector in SELECTORS:
            traced = trace_selector_regret(
                contract_events,
                dates,
                selector=selector,
                capital=capital,
                max_trades=max_trades,
                session_scope=session_scope,
            )
            if not traced.empty:
                decisions.append(traced)
    detail = pd.concat(decisions, ignore_index=True) if decisions else pd.DataFrame()
    return detail, summarize_selector_regret(
        detail,
        expected_sessions=len({str(value) for value in dates}),
    )


def regret_status_snapshot(summary: pd.DataFrame, *, excluded: bool) -> dict:
    if summary is None or summary.empty:
        return {}
    row = summary.iloc[0]
    reasons = {
        "active_position": int(row.get("missed_blocked_active_position", 0) or 0),
        "same_minute_priority": int(
            row.get("missed_lower_same_minute_priority", 0) or 0
        ),
        "symbol_repeat": int(row.get("missed_symbol_repeat", 0) or 0),
        "daily_trade_cap": int(row.get("missed_daily_trade_cap", 0) or 0),
    }
    primary_reason = max(reasons, key=reasons.get) if any(reasons.values()) else "none"
    return {
        "contract": str(row.get("contract", "")),
        "selector": str(row.get("selector", "")),
        "sessions": int(row.get("sessions", 0) or 0),
        "selected_trades": int(row.get("selected_trades", 0) or 0),
        "selected_win_rate_pct": round(
            float(row.get("selected_win_rate_pct", 0.0) or 0.0), 2
        ),
        "selected_avg_daily_pnl": round(
            float(row.get("selected_avg_daily_pnl", 0.0) or 0.0), 2
        ),
        "missed_positive_symbols": int(
            row.get("missed_positive_symbols", 0) or 0
        ),
        "missed_behind_selected_loser": int(
            row.get("missed_behind_selected_loser", 0) or 0
        ),
        "primary_missed_reason": primary_reason,
        "primary_missed_observations": reasons[primary_reason]
        if primary_reason != "none"
        else 0,
        "excluded_from_evidence": bool(excluded),
    }


def best_nonoverlapping_schedule(
    events: pd.DataFrame,
    *,
    capital: float = 500_000.0,
    max_trades: int = 10,
) -> tuple[float, pd.DataFrame]:
    """Return the future-known maximum positive P&L with one reusable slot."""
    if events is None or events.empty or max_trades <= 0:
        return 0.0, pd.DataFrame()
    day = events.copy()
    day["entry_time"] = pd.to_datetime(day["entry_time"], errors="coerce")
    day["exit_time"] = pd.to_datetime(day["exit_time"], errors="coerce")
    day["NetReturnPct"] = pd.to_numeric(day["NetReturnPct"], errors="coerce")
    day = day[
        day["entry_time"].notna()
        & day["exit_time"].notna()
        & day["NetReturnPct"].gt(0.0)
        & (day["exit_time"] > day["entry_time"])
    ].sort_values(["exit_time", "entry_time", "Symbol"]).reset_index(drop=True)
    if day.empty:
        return 0.0, pd.DataFrame()

    ends = day["exit_time"].tolist()
    prior = [
        bisect.bisect_right(ends, pd.Timestamp(row.entry_time), 0, index) - 1
        for index, row in enumerate(day.itertuples())
    ]
    n = len(day)
    limit = min(int(max_trades), n)
    dp = [[0.0] * (limit + 1) for _ in range(n + 1)]
    take = [[False] * (limit + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        pnl = float(day.iloc[i - 1]["NetReturnPct"]) * float(capital) / 100.0
        compatible = prior[i - 1] + 1
        for count in range(1, limit + 1):
            include = pnl + dp[compatible][count - 1]
            exclude = dp[i - 1][count]
            if include > exclude:
                dp[i][count] = include
                take[i][count] = True
            else:
                dp[i][count] = exclude

    count = max(range(limit + 1), key=lambda value: dp[n][value])
    selected = []
    i = n
    while i > 0 and count > 0:
        if take[i][count]:
            selected.append(i - 1)
            i = prior[i - 1] + 1
            count -= 1
        else:
            i -= 1
    chosen = day.iloc[list(reversed(selected))].copy() if selected else pd.DataFrame()
    if not chosen.empty:
        chosen["OraclePnl"] = chosen["NetReturnPct"] * float(capital) / 100.0
    return round(float(dp[n][max(range(limit + 1), key=lambda value: dp[n][value])]), 2), chosen


def best_shared_capital_slot_schedule(
    events: pd.DataFrame,
    *,
    slots: int,
    capital: float = 500_000.0,
    max_trades: int = 10,
) -> tuple[float, pd.DataFrame]:
    """Return the exact future-known optimum for fixed equal capital slots."""
    if int(slots) not in SLOT_COUNTS:
        raise ValueError(f"unsupported slot count: {slots}")
    if events is None or events.empty or max_trades <= 0:
        return 0.0, pd.DataFrame()
    day = events.copy()
    day["entry_time"] = pd.to_datetime(day["entry_time"], errors="coerce")
    day["exit_time"] = pd.to_datetime(day["exit_time"], errors="coerce")
    for column in ("EntryAsk", "ExitBid", "StressPct"):
        day[column] = pd.to_numeric(day.get(column), errors="coerce")
    day = day[
        day["entry_time"].notna()
        & day["exit_time"].notna()
        & day["EntryAsk"].gt(0.0)
        & day["ExitBid"].gt(0.0)
        & (day["exit_time"] > day["entry_time"])
    ].copy()
    if day.empty:
        return 0.0, pd.DataFrame()

    slot_capital = float(capital) / int(slots)
    day["OracleQuantity"] = np.floor(
        slot_capital / day["EntryAsk"]
    ).astype(int)
    deployed = day["OracleQuantity"] * day["EntryAsk"]
    day["OraclePnl"] = (
        (day["ExitBid"] - day["EntryAsk"]) * day["OracleQuantity"]
        - deployed * day["StressPct"].fillna(0.0) / 100.0
    )
    day = day[
        day["OracleQuantity"].gt(0) & day["OraclePnl"].gt(0.0)
    ].sort_values(
        ["entry_time", "exit_time", "Symbol"]
    ).reset_index(drop=True)
    if day.empty:
        return 0.0, pd.DataFrame()

    constraints = []
    upper = []
    # Cardinality is global across the session.
    constraints.append(np.ones(len(day), dtype=float))
    upper.append(float(max_trades))
    # A symbol can be selected only once, even if it re-enters the recorder.
    for _, indexes in day.groupby(day["Symbol"].astype(str)).groups.items():
        row = np.zeros(len(day), dtype=float)
        row[list(indexes)] = 1.0
        constraints.append(row)
        upper.append(1.0)
    # For interval schedules, concurrency can increase only at an entry time.
    # Checking each distinct entry instant therefore proves the slot bound.
    for instant in sorted(day["entry_time"].dropna().unique().tolist()):
        active = (
            day["entry_time"].le(pd.Timestamp(instant))
            & day["exit_time"].gt(pd.Timestamp(instant))
        )
        row = active.astype(float).to_numpy()
        if row.any():
            constraints.append(row)
            upper.append(float(slots))

    matrix = np.vstack(constraints)
    result = milp(
        c=-day["OraclePnl"].to_numpy(dtype=float),
        integrality=np.ones(len(day), dtype=int),
        bounds=Bounds(np.zeros(len(day)), np.ones(len(day))),
        constraints=LinearConstraint(
            matrix,
            np.full(len(constraints), -np.inf),
            np.asarray(upper, dtype=float),
        ),
        options={"time_limit": 30.0},
    )
    if not bool(result.success) or result.x is None:
        raise RuntimeError(
            f"shared-capital slot oracle failed: {result.message}"
        )
    selected = day.loc[np.asarray(result.x) > 0.5].copy()
    selected.sort_values(["entry_time", "exit_time", "Symbol"], inplace=True)
    selected["OracleSlots"] = int(slots)
    selected["OracleSlotCapital"] = round(slot_capital, 2)
    return round(float(selected["OraclePnl"].sum()), 2), selected


def _lower95(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    if len(numeric) == 1:
        return float(numeric.iloc[0])
    return float(numeric.mean() - 1.645 * numeric.std(ddof=1) / math.sqrt(len(numeric)))


def evaluate_ceiling(
    events: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    capital: float = 500_000.0,
    daily_goal: float = 10_000.0,
    max_trades: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    daily_rows = []
    selected_rows = []
    dates = coverage["date"].astype(str).tolist() if coverage is not None and not coverage.empty else []
    eligible_map = dict(zip(coverage["date"].astype(str), coverage["eligible"].astype(bool))) if dates else {}
    for date in dates:
        day_all = events[events["date"].astype(str).eq(date)] if events is not None and not events.empty else pd.DataFrame()
        for contract in CONTRACTS:
            day = day_all[day_all["Contract"].astype(str).eq(contract)] if not day_all.empty else day_all
            pnl, chosen = best_nonoverlapping_schedule(day, capital=capital, max_trades=max_trades)
            first_observations = (
                day.sort_values(["entry_time", "Symbol"]).drop_duplicates("Symbol", keep="first")
                if not day.empty else day
            )
            first_pnl, first_chosen = best_nonoverlapping_schedule(
                first_observations, capital=capital, max_trades=max_trades
            )
            positive_unconstrained = (
                pd.to_numeric(day.loc[pd.to_numeric(day.get("NetReturnPct"), errors="coerce").gt(0), "NetReturnPct"], errors="coerce").sum()
                * float(capital) / 100.0
                if not day.empty else 0.0
            )
            daily_rows.append({
                "date": date,
                "contract": contract,
                "eligible_full_session": bool(eligible_map.get(date, False)),
                "perfect_one_slot_pnl": pnl,
                "perfect_one_slot_return_pct": round(pnl / float(capital) * 100.0, 6) if capital else 0.0,
                "goal_hit": pnl >= float(daily_goal),
                "selected_trades": int(len(chosen)),
                "first_observation_one_slot_pnl": first_pnl,
                "first_observation_return_pct": round(first_pnl / float(capital) * 100.0, 6) if capital else 0.0,
                "first_observation_goal_hit": first_pnl >= float(daily_goal),
                "first_observation_selected_trades": int(len(first_chosen)),
                "positive_candidates": int(pd.to_numeric(day.get("NetReturnPct"), errors="coerce").gt(0).sum()) if not day.empty else 0,
                "all_positive_no_capacity_pnl": round(float(positive_unconstrained), 2),
                "uses_future_outcomes": True,
                "can_promote_strategy": False,
            })
            if not chosen.empty:
                chosen = chosen.copy()
                chosen["schedule_type"] = "repeat_allowed_upper_bound"
                chosen["date"] = date
                chosen["contract"] = contract
                chosen["eligible_full_session"] = bool(eligible_map.get(date, False))
                selected_rows.extend(chosen.to_dict(orient="records"))
            if not first_chosen.empty:
                first_chosen = first_chosen.copy()
                first_chosen["schedule_type"] = "first_observation_per_symbol"
                first_chosen["date"] = date
                first_chosen["contract"] = contract
                first_chosen["eligible_full_session"] = bool(eligible_map.get(date, False))
                selected_rows.extend(first_chosen.to_dict(orient="records"))
    daily = pd.DataFrame(daily_rows)
    selected = pd.DataFrame(selected_rows)
    summary_rows = []
    eligible = daily[daily["eligible_full_session"]].copy() if not daily.empty else pd.DataFrame()
    for contract in CONTRACTS:
        group = eligible[eligible["contract"].eq(contract)] if not eligible.empty else pd.DataFrame()
        if group.empty:
            continue
        pnl = pd.to_numeric(group["perfect_one_slot_pnl"], errors="coerce").fillna(0.0)
        first_pnl = pd.to_numeric(group["first_observation_one_slot_pnl"], errors="coerce").fillna(0.0)
        summary_rows.append({
            "contract": contract,
            "sessions": int(len(group)),
            "avg_perfect_one_slot_pnl": round(float(pnl.mean()), 2),
            "lower95_perfect_one_slot_pnl": round(float(_lower95(pnl) or 0.0), 2),
            "goal_hit_pct": round(float((pnl >= float(daily_goal)).mean() * 100.0), 2),
            "avg_first_observation_one_slot_pnl": round(float(first_pnl.mean()), 2),
            "lower95_first_observation_one_slot_pnl": round(float(_lower95(first_pnl) or 0.0), 2),
            "first_observation_goal_hit_pct": round(float((first_pnl >= float(daily_goal)).mean() * 100.0), 2),
            "worst_session_pnl": round(float(pnl.min()), 2),
            "best_session_pnl": round(float(pnl.max()), 2),
            "uses_future_outcomes": True,
            "diagnostic_only": True,
            "evidence_accepted": False,
            "paper_promotion_allowed": False,
            "real_orders_allowed": False,
        })
    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        summary.sort_values(["avg_first_observation_one_slot_pnl", "lower95_first_observation_one_slot_pnl"], ascending=False, inplace=True)
    return daily, summary, selected


def evaluate_selector_gap(
    events: pd.DataFrame,
    ceiling_daily: pd.DataFrame,
    eligible_dates: list[str],
    *,
    capital: float = 500_000.0,
    daily_goal: float = 10_000.0,
    max_trades: int = 10,
    eligible_only: bool = True,
) -> pd.DataFrame:
    """Compare causal selectors with the future-known first-observation ceiling."""
    if not eligible_dates or events is None or events.empty or ceiling_daily.empty:
        return pd.DataFrame()
    rows = []
    for contract in CONTRACTS:
        contract_events = events[events["Contract"].astype(str).eq(contract)].copy()
        oracle_mask = (
            ceiling_daily["contract"].astype(str).eq(contract)
            & ceiling_daily["date"].astype(str).isin(eligible_dates)
        )
        if eligible_only:
            oracle_mask &= ceiling_daily["eligible_full_session"]
        oracle = ceiling_daily.loc[
            oracle_mask, ["date", "first_observation_one_slot_pnl"]
        ].copy()
        if oracle.empty:
            continue
        for selector in SELECTORS:
            causal, _ = replay_one_slot(
                contract_events,
                eligible_dates,
                selector=selector,
                capital=capital,
                max_trades_per_day=max_trades,
            )
            comparison = oracle.merge(causal[["date", "pnl", "trades"]], on="date", how="left")
            comparison[["pnl", "trades"]] = comparison[["pnl", "trades"]].fillna(0.0)
            oracle_pnl = pd.to_numeric(comparison["first_observation_one_slot_pnl"], errors="coerce").fillna(0.0)
            causal_pnl = pd.to_numeric(comparison["pnl"], errors="coerce").fillna(0.0)
            oracle_total = float(oracle_pnl.sum())
            rows.append({
                "contract": contract,
                "selector": selector,
                "session_scope": "eligible_full_session" if eligible_only else "partial_incomplete_diagnostic",
                "sessions": int(len(comparison)),
                "trades": int(pd.to_numeric(comparison["trades"], errors="coerce").fillna(0).sum()),
                "avg_causal_pnl": round(float(causal_pnl.mean()), 2),
                "avg_first_observation_ceiling_pnl": round(float(oracle_pnl.mean()), 2),
                "avg_opportunity_gap_pnl": round(float((oracle_pnl - causal_pnl).mean()), 2),
                "opportunity_capture_pct": round(float(causal_pnl.sum()) / oracle_total * 100.0, 2) if oracle_total > 0 else 0.0,
                "causal_goal_hit_pct": round(float((causal_pnl >= float(daily_goal)).mean() * 100.0), 2),
                "ceiling_goal_hit_pct": round(float((oracle_pnl >= float(daily_goal)).mean() * 100.0), 2),
                "uses_future_outcomes_for_ceiling_only": True,
                "selector_is_causal": True,
                "evidence_accepted": False,
                "paper_promotion_allowed": False,
                "real_orders_allowed": False,
            })
    result = pd.DataFrame(rows)
    if not result.empty:
        result.sort_values(
            ["avg_causal_pnl", "opportunity_capture_pct", "contract", "selector"],
            ascending=[False, False, True, True],
            inplace=True,
        )
    return result


def evaluate_slot_ceiling(
    events: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    capital: float = 500_000.0,
    daily_goal: float = 10_000.0,
    max_trades: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Evaluate exact 1/2/3/5-slot future-known shared-capital capacity."""
    rows = []
    selected_rows = []
    if coverage is None or coverage.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    eligible_map = dict(
        zip(coverage["date"].astype(str), coverage["eligible"].astype(bool))
    )
    for date in coverage["date"].astype(str).tolist():
        day_all = (
            events[events["date"].astype(str).eq(date)]
            if events is not None and not events.empty
            else pd.DataFrame()
        )
        for contract in CONTRACTS:
            contract_day = (
                day_all[day_all["Contract"].astype(str).eq(contract)]
                if not day_all.empty
                else day_all
            )
            first = (
                contract_day.sort_values(["entry_time", "Symbol"])
                .drop_duplicates("Symbol", keep="first")
                if not contract_day.empty
                else contract_day
            )
            for slots in SLOT_COUNTS:
                pnl, chosen = best_shared_capital_slot_schedule(
                    first,
                    slots=slots,
                    capital=capital,
                    max_trades=max_trades,
                )
                rows.append(
                    {
                        "date": date,
                        "contract": contract,
                        "slots": int(slots),
                        "slot_capital": round(float(capital) / slots, 2),
                        "eligible_full_session": bool(
                            eligible_map.get(date, False)
                        ),
                        "perfect_shared_capital_pnl": pnl,
                        "perfect_shared_capital_return_pct": round(
                            pnl / float(capital) * 100.0, 6
                        ) if capital else 0.0,
                        "goal_hit": pnl >= float(daily_goal),
                        "selected_trades": int(len(chosen)),
                        "uses_future_outcomes": True,
                        "can_promote_strategy": False,
                    }
                )
                if not chosen.empty:
                    chosen = chosen.copy()
                    chosen["date"] = date
                    chosen["contract"] = contract
                    chosen["eligible_full_session"] = bool(
                        eligible_map.get(date, False)
                    )
                    selected_rows.extend(chosen.to_dict(orient="records"))
    daily = pd.DataFrame(rows)
    selected = pd.DataFrame(selected_rows)
    summary_rows = []
    eligible = daily[daily["eligible_full_session"]] if not daily.empty else daily
    for (contract, slots), group in eligible.groupby(
        ["contract", "slots"], sort=True
    ):
        pnl = pd.to_numeric(
            group["perfect_shared_capital_pnl"], errors="coerce"
        ).fillna(0.0)
        summary_rows.append(
            {
                "contract": contract,
                "slots": int(slots),
                "sessions": int(len(group)),
                "avg_perfect_shared_capital_pnl": round(float(pnl.mean()), 2),
                "lower95_perfect_shared_capital_pnl": round(
                    float(_lower95(pnl) or 0.0), 2
                ),
                "goal_hit_pct": round(
                    float((pnl >= float(daily_goal)).mean() * 100.0), 2
                ),
                "worst_session_pnl": round(float(pnl.min()), 2),
                "best_session_pnl": round(float(pnl.max()), 2),
                "uses_future_outcomes": True,
                "diagnostic_only": True,
                "evidence_accepted": False,
                "paper_promotion_allowed": False,
                "real_orders_allowed": False,
            }
        )
    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        summary.sort_values(
            [
                "avg_perfect_shared_capital_pnl",
                "lower95_perfect_shared_capital_pnl",
            ],
            ascending=False,
            inplace=True,
        )
    return daily, summary, selected


def run_audit(
    *,
    entry_patterns: Iterable[str],
    outcome_patterns: Iterable[str],
    raw_patterns: Iterable[str],
    output_dir: Path,
    capital: float = 500_000.0,
    daily_goal: float = 10_000.0,
    max_trades: int = 10,
    min_snapshots: int = 1000,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    entries, events = load_matrix_events(entry_patterns, outcome_patterns)
    raw = raw_session_coverage(raw_patterns, min_snapshots=min_snapshots)
    coverage = matrix_session_coverage(entries, events, raw)
    daily, summary, selected = evaluate_ceiling(
        events, coverage, capital=capital, daily_goal=daily_goal, max_trades=max_trades
    )
    slot_daily, slot_summary, slot_selected = evaluate_slot_ceiling(
        events,
        coverage,
        capital=capital,
        daily_goal=daily_goal,
        max_trades=max_trades,
    )
    eligible_dates = coverage.loc[coverage["eligible"], "date"].astype(str).tolist() if not coverage.empty else []
    selector_gap = evaluate_selector_gap(
        events,
        daily,
        eligible_dates,
        capital=capital,
        daily_goal=daily_goal,
        max_trades=max_trades,
    )
    partial_dates = coverage.loc[~coverage["eligible"], "date"].astype(str).tolist() if not coverage.empty else []
    partial_selector_gap = evaluate_selector_gap(
        events,
        daily,
        partial_dates,
        capital=capital,
        daily_goal=daily_goal,
        max_trades=max_trades,
        eligible_only=False,
    )
    regret_decisions, regret_summary = evaluate_selector_regret(
        events,
        eligible_dates,
        capital=capital,
        max_trades=max_trades,
        session_scope="eligible_full_session",
    )
    partial_regret_decisions, partial_regret_summary = evaluate_selector_regret(
        events,
        partial_dates,
        capital=capital,
        max_trades=max_trades,
        session_scope="partial_incomplete_diagnostic",
    )
    daily.to_csv(output_dir / "daily.csv", index=False)
    summary.to_csv(output_dir / "summary.csv", index=False)
    selected.to_csv(output_dir / "selected_intervals.csv", index=False)
    slot_daily.to_csv(output_dir / "slot_daily.csv", index=False)
    slot_summary.to_csv(output_dir / "slot_summary.csv", index=False)
    slot_selected.to_csv(
        output_dir / "slot_selected_intervals.csv", index=False
    )
    selector_gap.to_csv(output_dir / "selector_gap.csv", index=False)
    partial_selector_gap.to_csv(output_dir / "partial_selector_gap.csv", index=False)
    regret_decisions.to_csv(output_dir / "selector_regret_decisions.csv", index=False)
    regret_summary.to_csv(output_dir / "selector_regret_summary.csv", index=False)
    partial_regret_decisions.to_csv(
        output_dir / "partial_selector_regret_decisions.csv", index=False
    )
    partial_regret_summary.to_csv(
        output_dir / "partial_selector_regret_summary.csv", index=False
    )
    best = summary.iloc[0].to_dict() if not summary.empty else {}
    best_slot = (
        slot_summary.iloc[0].to_dict() if not slot_summary.empty else {}
    )
    best_causal = selector_gap.iloc[0].to_dict() if not selector_gap.empty else {}
    best_partial_causal = partial_selector_gap.iloc[0].to_dict() if not partial_selector_gap.empty else {}
    best_regret = regret_status_snapshot(regret_summary, excluded=False)
    best_partial_regret = regret_status_snapshot(
        partial_regret_summary, excluded=True
    )
    partial = daily[~daily["eligible_full_session"]].copy() if not daily.empty else pd.DataFrame()
    partial_slots = (
        slot_daily[~slot_daily["eligible_full_session"]].copy()
        if not slot_daily.empty
        else pd.DataFrame()
    )
    partial_best = {}
    if not partial.empty:
        row = partial.sort_values("first_observation_one_slot_pnl", ascending=False).iloc[0]
        partial_best = {
            "date": str(row["date"]),
            "contract": str(row["contract"]),
            "first_observation_one_slot_pnl": round(float(row["first_observation_one_slot_pnl"]), 2),
            "repeat_allowed_one_slot_pnl": round(float(row["perfect_one_slot_pnl"]), 2),
            "excluded_from_evidence": True,
        }
    partial_slot_best = {}
    if not partial_slots.empty:
        row = partial_slots.sort_values(
            "perfect_shared_capital_pnl", ascending=False
        ).iloc[0]
        partial_slot_best = {
            "date": str(row["date"]),
            "contract": str(row["contract"]),
            "slots": int(row["slots"]),
            "perfect_shared_capital_pnl": round(
                float(row["perfect_shared_capital_pnl"]), 2
            ),
            "selected_trades": int(row["selected_trades"]),
            "excluded_from_evidence": True,
        }
    if not best:
        bottleneck = "awaiting_complete_full_session"
    elif float(
        best_slot.get("avg_perfect_shared_capital_pnl", 0.0) or 0.0
    ) < float(daily_goal):
        bottleneck = "candidate_universe_capacity_below_goal"
    elif float(best_causal.get("avg_causal_pnl", 0.0) or 0.0) < float(daily_goal):
        bottleneck = "causal_selector_bottleneck"
    else:
        bottleneck = "point_target_reached_pending_forward_validation"
    status = {
        "status": "diagnostic_available" if best else "awaiting_complete_full_session",
        "capital": float(capital),
        "daily_goal": float(daily_goal),
        "max_trades_per_day": int(max_trades),
        "contracts": len(CONTRACTS),
        "raw_sessions": int(len(coverage)),
        "eligible_full_sessions": int(coverage["eligible"].sum()) if not coverage.empty else 0,
        "partial_or_incomplete_sessions": int((~coverage["eligible"]).sum()) if not coverage.empty else 0,
        "best_eligible_contract": best,
        "best_shared_capital_slot_ceiling": best_slot,
        "best_causal_combination": best_causal,
        "best_eligible_selector_regret": best_regret,
        "best_partial_selector_regret": best_partial_regret,
        "best_partial_causal_diagnostic": {
            "contract": str(best_partial_causal.get("contract", "")),
            "selector": str(best_partial_causal.get("selector", "")),
            "avg_causal_pnl": round(float(best_partial_causal.get("avg_causal_pnl", 0.0) or 0.0), 2),
            "avg_first_observation_ceiling_pnl": round(float(best_partial_causal.get("avg_first_observation_ceiling_pnl", 0.0) or 0.0), 2),
            "opportunity_capture_pct": round(float(best_partial_causal.get("opportunity_capture_pct", 0.0) or 0.0), 2),
            "excluded_from_evidence": True,
        } if best_partial_causal else {},
        "best_partial_diagnostic": partial_best,
        "best_partial_shared_capital_diagnostic": partial_slot_best,
        "current_bottleneck": bottleneck,
        "uses_future_outcomes": True,
        "diagnostic_only": True,
        "strategy_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }
    (output_dir / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-glob", action="append", default=[])
    parser.add_argument("--outcome-glob", action="append", default=[])
    parser.add_argument("--raw-glob", action="append", default=[])
    parser.add_argument("--output-dir", default="results/Data/full_universe_rank_opportunity_ceiling")
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--min-snapshots", type=int, default=1000)
    args = parser.parse_args()
    status = run_audit(
        entry_patterns=args.entry_glob or ["results/Data/full_universe_rank_paper/entries_20*.csv"],
        outcome_patterns=args.outcome_glob or ["results/Data/full_universe_rank_contract_matrix/outcomes_20*.csv"],
        raw_patterns=args.raw_glob or ["results/Data/live_feature_recorder/live_features_20*.csv"],
        output_dir=Path(args.output_dir),
        capital=args.capital,
        daily_goal=args.daily_goal,
        max_trades=args.max_trades,
        min_snapshots=args.min_snapshots,
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
