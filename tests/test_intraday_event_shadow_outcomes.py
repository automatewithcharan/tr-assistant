import pandas as pd

from pkscreener.classes.IntradayEventShadowOutcomes import (
    evaluate_event_shadow_day,
    evaluate_event_shadow_fade_day,
)


def _write_day(tmp_path, future_highs, future_lows, future_closes):
    root = tmp_path / "intraday_event_shadow"
    root.mkdir()
    times = pd.to_datetime(["2026-01-02 09:30", "2026-01-02 09:35", "2026-01-02 09:40", "2026-01-02 15:25"])
    pd.DataFrame(
        {
            "symbol": ["AAA"] * 4,
            "datetime": times,
            "open": [100.0] * 4,
            "high": [100.2, *future_highs],
            "low": [99.9, *future_lows],
            "close": [100.0, *future_closes],
            "volume": [1000] * 4,
        }
    ).to_csv(root / "bars_2026-01-02.csv", index=False)
    pd.DataFrame(
        [{"date": "2026-01-02", "symbol": "AAA", "event_type": "or15_breakout", "event_stage": 1, "signal_time": "2026-01-02 09:30"}]
    ).to_csv(root / "events_2026-01-02.csv", index=False)


def test_target_before_stop_is_positive_after_cost(tmp_path):
    _write_day(tmp_path, [101.6, 101.0, 101.0], [99.5, 99.5, 99.5], [101.0, 101.0, 101.0])
    outcomes, summary = evaluate_event_shadow_day(tmp_path, "2026-01-02")
    assert outcomes.iloc[0]["exit_reason"] == "target"
    assert outcomes.iloc[0]["net_return_pct"] == 1.35
    assert bool(outcomes.iloc[0]["complete_session"]) is True
    assert summary.iloc[0]["target_before_stop_pct"] == 100.0


def test_same_bar_target_and_stop_is_conservative_loss(tmp_path):
    _write_day(tmp_path, [101.6, 101.0, 101.0], [98.9, 99.5, 99.5], [100.0, 100.0, 100.0])
    outcomes, _ = evaluate_event_shadow_day(tmp_path, "2026-01-02")
    assert outcomes.iloc[0]["exit_reason"] == "ambiguous_stop_target"
    assert outcomes.iloc[0]["net_return_pct"] == -1.15
    assert bool(outcomes.iloc[0]["target_before_stop"]) is False


def test_incomplete_symbol_is_not_used_in_summary(tmp_path):
    _write_day(tmp_path, [101.0, 101.0, 101.0], [99.5, 99.5, 99.5], [100.2, 100.2, 100.2])
    path = tmp_path / "intraday_event_shadow" / "bars_2026-01-02.csv"
    bars = pd.read_csv(path).iloc[:-1]
    bars.to_csv(path, index=False)
    outcomes, summary = evaluate_event_shadow_day(tmp_path, "2026-01-02")
    assert bool(outcomes.iloc[0]["complete_session"]) is False
    assert summary.empty


def test_short_fade_shadow_uses_directional_target_and_cost(tmp_path):
    _write_day(tmp_path, [100.2, 100.1, 100.0], [99.5, 97.9, 98.0], [99.0, 98.0, 98.0])
    outcomes, summary = evaluate_event_shadow_fade_day(tmp_path, "2026-01-02")
    assert outcomes.iloc[0]["side"] == "short"
    assert outcomes.iloc[0]["exit_reason"] == "target"
    assert outcomes.iloc[0]["net_return_pct"] == 1.85
    assert summary.iloc[0]["side"] == "short"


def test_short_fade_same_bar_is_conservative_stop(tmp_path):
    _write_day(tmp_path, [100.6, 100.1, 100.0], [97.9, 99.0, 99.0], [100.0, 99.0, 99.0])
    outcomes, _ = evaluate_event_shadow_fade_day(tmp_path, "2026-01-02")
    assert outcomes.iloc[0]["exit_reason"] == "ambiguous_stop_target"
    assert outcomes.iloc[0]["net_return_pct"] == -0.65


def test_outcomes_freeze_top_candidate_from_first_signal_batch(tmp_path):
    _write_day(tmp_path, [101.0, 101.0, 101.0], [99.5, 99.5, 99.5], [100.2, 100.2, 100.2])
    events_path = tmp_path / "intraday_event_shadow" / "events_2026-01-02.csv"
    events = pd.read_csv(events_path)
    events = pd.concat(
        [
            events.assign(event_score=1.0),
            events.assign(symbol="AAA", event_type="vwap_reclaim", event_score=2.0),
        ],
        ignore_index=True,
    )
    events.to_csv(events_path, index=False)
    outcomes, summary = evaluate_event_shadow_day(tmp_path, "2026-01-02")
    selected = outcomes[outcomes["first_batch_selected"]]
    assert len(selected) == 1
    assert selected.iloc[0]["event_type"] == "vwap_reclaim"
    portfolio = summary[summary["event_type"] == "__first_batch_portfolio__"]
    assert len(portfolio) == 1
