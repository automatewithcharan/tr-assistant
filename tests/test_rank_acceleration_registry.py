import pandas as pd

from scripts.research_rank_acceleration_registry import (
    RULES,
    add_rank_acceleration,
    daily_returns,
    replay_short_returns,
    rule_pool,
)


def _row(date, symbol, minute, move, move_rank, outcome=0.5):
    return {
        "date": date,
        "symbol": symbol,
        "signal_minute": minute,
        "move_from_open_pct": move,
        "day_change_pct": move,
        "move_rank": move_rank,
        "volume_rank": 0.8,
        "acceleration_rank": 0.8,
        "relative_strength_rank": 0.8,
        "range_rank": 0.8,
        "turnover_rank": 0.8,
        "pullback_quality_rank": 0.8,
        "return__test": outcome,
    }


def test_rank_delta_uses_only_prior_scan_for_same_symbol():
    frame = pd.DataFrame(
        [
            _row("2026-01-02", "AAA", 570, 0.4, 0.4),
            _row("2026-01-02", "AAA", 585, 0.9, 0.8),
            _row("2026-01-02", "BBB", 585, 1.0, 0.9),
        ]
    )
    enriched = add_rank_acceleration(frame)
    aaa = enriched[(enriched["symbol"] == "AAA") & (enriched["signal_minute"] == 585)].iloc[0]
    assert round(aaa["rank_delta"], 6) == 0.4
    bbb = enriched[enriched["symbol"] == "BBB"].iloc[0]
    assert pd.isna(bbb["rank_delta"])


def test_rule_uses_earliest_batch_and_causal_score():
    frame = pd.DataFrame(
        [
            _row("2026-01-02", "AAA", 570, 0.4, 0.4),
            _row("2026-01-02", "AAA", 585, 0.9, 0.8),
            _row("2026-01-02", "BBB", 570, 0.4, 0.3),
            _row("2026-01-02", "BBB", 585, 1.0, 0.75),
            _row("2026-01-02", "CCC", 585, 0.6, 0.3),
            _row("2026-01-02", "CCC", 600, 1.1, 0.9),
        ]
    )
    pool = rule_pool(add_rank_acceleration(frame), RULES[0])
    assert set(pool["signal_minute"]) == {585}
    assert list(pool.sort_values("selection_rank")["symbol"]) == ["BBB", "AAA"]


def test_daily_returns_include_cash_days_and_stress_only_trade_days():
    pool = pd.DataFrame(
        [
            {"date": "2026-01-02", "selection_rank": 1, "return__test": 0.5},
            {"date": "2026-01-02", "selection_rank": 2, "return__test": -0.5},
        ]
    )
    daily = daily_returns(
        pool,
        ["2026-01-02", "2026-01-03"],
        "return__test",
        top_k=1,
        extra_cost_pct=0.1,
    )
    assert daily.loc[0, "daily_return_pct"] == 0.4
    assert daily.loc[1, "daily_return_pct"] == 0.0


def test_short_replay_uses_adverse_entry_and_sixty_minute_path(tmp_path):
    rows = [
        _row("2026-01-02", "AAA", 570, 0.4, 0.4),
        _row("2026-01-02", "AAA", 585, 0.9, 0.8),
    ]
    frame = pd.DataFrame(rows)
    frame["entry_time"] = pd.to_datetime(["2026-01-02 09:35", "2026-01-02 09:50"])
    frame["raw_entry_price"] = 100.0
    pd.DataFrame(
        {
            "datetime": pd.date_range("2026-01-02 09:50", periods=12, freq="5min"),
            "high": [100.0] * 12,
            "low": [99.0] * 12,
            "close": [99.2] * 12,
        }
    ).to_csv(tmp_path / "AAA.csv", index=False)
    replayed, columns, stats = replay_short_returns(
        frame,
        str(tmp_path),
        horizon_minutes=60,
        targets=[0.5],
        stops=[0.5],
        slippage_bps=5.0,
        cost_pct=0.15,
    )
    assert stats["matched_rows"] == 1
    assert len(columns) == 1
    assert replayed.loc[1, columns[0]] == 0.35
