from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from pkscreener.classes.SignalMonitor import SignalMonitorRunner


NOW = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def runner(tmp_path) -> SignalMonitorRunner:
    value = SignalMonitorRunner.__new__(SignalMonitorRunner)
    value.output_dir = str(tmp_path)
    value.universe = "all"
    value.run_at = NOW
    value.run_at_str = "2026-09-27 10:00:00"
    value._causal_clock = lambda: NOW
    value._candidate_market_history = {}
    value._candidate_feature_history = {}
    value._candidate_alert_state = {}
    value._candidate_alert_state_path = str(tmp_path / "candidate-state.json")
    value._early_breakout_alerted_today = set()
    value._top_gainer_radar_today = set()
    value._append_daily_symbol = lambda *_args, **_kwargs: None
    value.candidate_cooldown_seconds = 900
    value.candidate_score_improvement = 8
    value.candidate_state_change_bypasses_cooldown = True
    value.candidate_reject_low_liquidity = True
    value.candidate_max_volume_participation_pct = 10
    value.candidate_min_avg_1m_traded_value = 25_000
    value.candidate_score_weights = {
        "momentum": 1.0,
        "volume": 1.0,
        "liquidity": 1.0,
        "structure": 1.0,
        "penalties": 1.0,
    }
    value.EARLY_BREAKOUT_MAX_PER_CYCLE = 4
    value.TOP_GAINER_RADAR_MAX_PER_CYCLE = 1
    value.TRADINGVIEW_CANDIDATE_MAX_PER_CYCLE = 5
    value.alert_channels = {"discord"}
    value._tradingview_delivery_available = True
    value._manual_discord_row_is_eligible = lambda *_args, **_kwargs: True
    value._direct_candidate_lanes_date = "2026-09-27"
    value._direct_candidate_lanes_by_symbol = {}
    value.actionable_runner = SimpleNamespace(live_ticks={})
    return value


def candidate(symbol, lane="early_breakout", **overrides):
    row = {
        "Symbol": symbol,
        "Lane": lane,
        "When": NOW.isoformat(),
        "PriceNow": 100.0,
        "ChangePct": 2.0,
        "SignalStrengthPct": 45.0,
        "VolumeRatio": 1.5,
        "PriorVolumeRatio": 1.2,
        "BuyerSellerRatio": 1.8,
        "PullbackPct": 0.2,
        "RSI": 66.0,
        "SuggestedQty": 10,
        "Avg1mVolume": 2_000,
        "Avg1mTradedValue": 200_000,
        "PriceChange1m": 0.4,
        "PriceChange3m": 0.8,
        "PriceChange5m": 1.0,
        "MomentumAcceleration": 0.13,
        "Why": "manual-review discovery candidate",
    }
    row.update(overrides)
    return row


def test_detectors_do_not_send_before_central_ranking():
    import inspect

    source = inspect.getsource(SignalMonitorRunner._build_early_breakout_alerts)
    source += inspect.getsource(SignalMonitorRunner._build_top_gainer_radar_alerts)
    assert "_send_tradingview_candidate_alerts" not in source


def test_duplicate_dual_lane_is_sent_once_with_both_labels(tmp_path):
    value = runner(tmp_path)
    selected = value._select_tradingview_candidates(
        pd.DataFrame([candidate("DUAL")]),
        pd.DataFrame([candidate("DUAL", "top_gainer_radar", SignalStrengthPct=48)]),
    )
    sent = []
    value._deliver_tradingview_candidate = lambda common, message, **kwargs: sent.append((common, message))
    value._persist_discord_shadow_records = lambda *_args: None
    value._send_tradingview_candidate_alerts(selected)

    assert len(selected) == 1
    assert selected.iloc[0]["Lane"] == "both"
    assert selected.iloc[0]["Lanes"] == "early_breakout,top_gainer_radar"
    assert len(sent) == 1
    assert "EARLY_BREAKOUT + TOP_GAINER_RADAR" in sent[0][1]


def test_ranking_precedes_caps_and_higher_score_sends_first(tmp_path):
    value = runner(tmp_path)
    value.EARLY_BREAKOUT_MAX_PER_CYCLE = 1
    selected = value._select_tradingview_candidates(
        pd.DataFrame([
            candidate("WEAK", SignalStrengthPct=38, VolumeRatio=1.3, MomentumAcceleration=-0.1),
            candidate("STRONG", SignalStrengthPct=60, VolumeRatio=2.4, MomentumAcceleration=0.3),
        ]),
        pd.DataFrame(),
    )
    assert list(selected["Symbol"]) == ["STRONG"]
    assert selected.iloc[0]["CandidateRank"] == 1


def test_lane_and_global_caps_are_enforced_after_scoring(tmp_path):
    value = runner(tmp_path)
    value.EARLY_BREAKOUT_MAX_PER_CYCLE = 2
    value.TOP_GAINER_RADAR_MAX_PER_CYCLE = 1
    value.TRADINGVIEW_CANDIDATE_MAX_PER_CYCLE = 2
    selected = value._select_tradingview_candidates(
        pd.DataFrame([candidate("E1"), candidate("E2"), candidate("E3")]),
        pd.DataFrame([candidate("R1", "top_gainer_radar")]),
    )
    assert len(selected) == 2
    assert sum(selected["CapBucket"].eq("top_gainer_radar")) <= 1
    assert sum(selected["CapBucket"].eq("early_breakout")) <= 2


def test_low_liquidity_candidate_is_rejected_without_shrinking_quantity(tmp_path):
    value = runner(tmp_path)
    selected = value._select_tradingview_candidates(
        pd.DataFrame([candidate("THIN", SuggestedQty=500, Avg1mVolume=100)]),
        pd.DataFrame(),
    )
    assert selected.empty


def test_cooldown_suppresses_identical_repeat(tmp_path):
    value = runner(tmp_path)
    first = value._select_tradingview_candidates(pd.DataFrame([candidate("SAME")]), pd.DataFrame())
    second = value._select_tradingview_candidates(pd.DataFrame([candidate("SAME")]), pd.DataFrame())
    assert list(first["Symbol"]) == ["SAME"]
    assert second.empty


def test_dual_lane_transition_can_bypass_cooldown(tmp_path):
    value = runner(tmp_path)
    value._select_tradingview_candidates(pd.DataFrame([candidate("CHANGE")]), pd.DataFrame())
    changed = value._select_tradingview_candidates(
        pd.DataFrame([candidate("CHANGE")]),
        pd.DataFrame([candidate("CHANGE", "top_gainer_radar")]),
    )
    assert list(changed["Symbol"]) == ["CHANGE"]
    assert changed.iloc[0]["CooldownDecision"] == "new lane qualification"


def test_score_is_deterministic_and_explainable(tmp_path):
    value = runner(tmp_path)
    row = candidate("SCORE")
    row.update(value._candidate_dynamic_metrics(row))
    row.update(value._candidate_liquidity(row))
    first = value._score_tradingview_candidate(dict(row))
    second = value._score_tradingview_candidate(dict(row))
    assert first == second
    assert first["ScoreTotal"] == sum(
        first[key] for key in (
            "ScoreMomentum", "ScoreVolume", "ScoreLiquidity",
            "ScoreStructure", "ScorePenalties",
        )
    )


def test_missing_volume_rvol_and_orderbook_data_does_not_crash(tmp_path):
    value = runner(tmp_path)
    sparse = candidate(
        "SPARSE",
        VolumeRatio=None,
        PriorVolumeRatio=None,
        Avg1mVolume=None,
        Avg1mTradedValue=None,
    )
    selected = value._select_tradingview_candidates(pd.DataFrame([sparse]), pd.DataFrame())
    assert list(selected["Symbol"]) == ["SPARSE"]
    assert selected.iloc[0]["LiquidityStatus"] == "UNKNOWN"


def test_message_is_explicitly_attention_only_not_a_trade_signal(tmp_path):
    value = runner(tmp_path)
    selected = value._select_tradingview_candidates(
        pd.DataFrame([candidate("REVIEW")]), pd.DataFrame()
    )
    message = value._build_tradingview_candidate_message(selected.iloc[0].to_dict())
    assert "MANUAL TRADINGVIEW REVIEW — NOT A BUY SIGNAL" in message
    assert "attention ranking only" in message
