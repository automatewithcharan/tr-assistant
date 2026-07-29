import pandas as pd

from scripts.assess_full_rank_feature_gates import (
    FEATURE_GATES,
    apply_feature_gate,
    run_audit,
)


def _entry(date, symbol, minute):
    when = pd.Timestamp(f"{date} 09:20:00") + pd.Timedelta(minutes=minute)
    return {
        "EventId": f"{date}:{symbol}:cohort",
        "Date": date,
        "EntryAt": when,
        "RecorderGeneration": 6,
        "Exchange": "NSE",
        "Symbol": symbol,
        "Cohort": "rapid:r1_10:m1_2",
        "Source": "rapid_rank_climber",
        "EntryAsk": 100.0,
        "EntryBid": 99.95,
        "EntrySpreadBps": 5.0,
        "Quantity": 5000,
        "Capital": 500000,
        "DayChangePct": 1.2,
        "UniverseSize": 3000,
        "DayChangeRank": minute + 1,
        "RankImprovement": 100,
        "RankVelocityPerMinute": 100.0,
        "DayChangeDeltaPct": 0.3,
        "RapidRankClimber": True,
        "OpenChangePct": 1.0,
        "PullbackFromHighPct": 0.2,
        "TradedValue": 150000000,
        "BuyerSellerRatio": 2.0,
        "BuyerSellerMedianWindow": 2.0,
        "OrderBookStable": True,
        "SectorTop2Count": 3,
        "SectorBreadthPct": 60.0,
    }


def _outcome(entry, net_return=1.4):
    return {
        "EventId": entry["EventId"],
        "ExitAt": pd.Timestamp(entry["EntryAt"]) + pd.Timedelta(minutes=3),
        "ExitReason": "target" if net_return > 0 else "stop",
        "ExitBid": 101.5 if net_return > 0 else 99.0,
        "HoldMinutes": 3,
        "GrossReturnPct": net_return + 0.1,
        "NetReturnPct": net_return,
        "GrossPnl": 7500,
        "StressCost": 500,
        "NetPnl": net_return * 5000,
    }


def _raw(date):
    start = pd.Timestamp(f"{date} 09:16:00")
    return pd.DataFrame(
        {
            "When": [start + pd.Timedelta(seconds=15 * i) for i in range(1440)],
            "DayChangeRank": [1] * 1440,
            "RecorderGeneration": [6] * 1440,
        }
    )


def test_predeclared_feature_gates_select_only_causal_rows():
    good = _entry("2026-07-22", "GOOD", 0)
    weak = {
        **_entry("2026-07-22", "WEAK", 100),
        "RapidRankClimber": False,
        "RankImprovement": 5,
        "DayChangeDeltaPct": 0.01,
        "OrderBookStable": False,
        "BuyerSellerRatio": 0.8,
        "BuyerSellerMedianWindow": 0.8,
        "SectorTop2Count": 0,
        "SectorBreadthPct": 20,
        "EntrySpreadBps": 18,
        "TradedValue": 40000000,
        "PullbackFromHighPct": 1.5,
    }
    frame = pd.DataFrame([good, weak])

    assert len(FEATURE_GATES) == 12
    assert apply_feature_gate(frame, "all_buyable")["Symbol"].tolist() == ["GOOD", "WEAK"]
    for gate in FEATURE_GATES:
        if gate not in {"all_buyable", "afternoon_fresh_acceleration"}:
            assert apply_feature_gate(frame, gate)["Symbol"].tolist() == ["GOOD"]
    assert apply_feature_gate(frame, "afternoon_fresh_acceleration").empty

    afternoon = pd.DataFrame([{**good, "EntryAt": "2026-07-22 12:00:00"}])
    assert apply_feature_gate(afternoon, "afternoon_fresh_acceleration")["Symbol"].tolist() == ["GOOD"]

    no_timestamp = pd.DataFrame([{key: value for key, value in good.items() if key != "EntryAt"}])
    assert apply_feature_gate(no_timestamp, "opening_ranked").empty


def test_feature_gate_selection_freezes_before_forward_loss(tmp_path):
    raw = tmp_path / "raw"
    paper = tmp_path / "paper"
    output = tmp_path / "output"
    raw.mkdir()
    paper.mkdir()
    entries = []
    outcomes = []
    dates = pd.bdate_range("2026-06-01", periods=12)
    for stamp in dates:
        date = stamp.strftime("%Y-%m-%d")
        _raw(date).to_csv(raw / f"live_features_{date}.csv", index=False)
        for index in range(3):
            entry = _entry(date, f"S{index}", index * 10)
            entries.append(entry)
            outcomes.append(_outcome(entry))
    pd.DataFrame(entries).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes).to_csv(paper / "outcomes.csv", index=False)

    selected = run_audit(
        entry_patterns=[str(paper / "entries.csv")],
        outcome_patterns=[str(paper / "outcomes.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert selected["status"] == "collecting_feature_gate_forward_sessions"
    assert selected["frozen_selection"]["gate"] in FEATURE_GATES
    assert selected["frozen_selection"]["selector"]
    frozen_before = (output / "frozen_selection.json").read_text(encoding="utf-8")

    forward_date = "2026-06-17"
    _raw(forward_date).to_csv(raw / f"live_features_{forward_date}.csv", index=False)
    loss = _entry(forward_date, "LOSS", 0)
    pd.DataFrame(entries + [loss]).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes + [_outcome(loss, net_return=-1.1)]).to_csv(
        paper / "outcomes.csv", index=False
    )
    forward = run_audit(
        entry_patterns=[str(paper / "entries.csv")],
        outcome_patterns=[str(paper / "outcomes.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert forward["forward"]["sessions"] == 1
    assert (output / "frozen_selection.json").read_text(encoding="utf-8") == frozen_before
    assert not forward["real_orders_allowed"]
