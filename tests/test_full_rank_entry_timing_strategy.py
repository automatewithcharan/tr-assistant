import pandas as pd

from pkscreener.classes.FullUniverseRankEntryTimingObserver import ENTRY_VARIANTS
from scripts.assess_full_rank_entry_timing import (
    load_timing_events,
    run_audit,
    timing_session_coverage,
)


def _entry(date, symbol, minute):
    when = pd.Timestamp(f"{date} 09:20:00") + pd.Timedelta(minutes=minute)
    return {
        "EventId": f"{date}:{symbol}:cohort",
        "Date": date,
        "EntryAt": when,
        "RecorderGeneration": 5,
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
        "TradedValue": 50000000,
        "BuyerSellerRatio": 2.0,
    }


def _timing_outcomes(entry, variants=None, net_return=1.4):
    rows = []
    for variant in variants or ENTRY_VARIANTS:
        rows.append(
            {
                "TimingId": f"{entry['EventId']}|{variant}",
                "EventId": entry["EventId"],
                "Variant": variant,
                "SignalAt": entry["EntryAt"],
                "EntryAt": pd.Timestamp(entry["EntryAt"]) + pd.Timedelta(minutes=2),
                "ExitAt": pd.Timestamp(entry["EntryAt"]) + pd.Timedelta(minutes=5),
                "ExitReason": "target" if net_return > 0 else "stop",
                "EntryAsk": 99.5,
                "ExitBid": 101.0 if net_return > 0 else 98.5,
                "EntryDelayMinutes": 2,
                "HoldMinutes": 3,
                "GrossReturnPct": net_return + 0.1,
                "NetReturnPct": net_return,
                "GrossPnl": 7500,
                "StressCost": 500,
                "NetPnl": net_return * 5000,
            }
        )
    return rows


def _raw(date):
    start = pd.Timestamp(f"{date} 09:16:00")
    return pd.DataFrame(
        {
            "When": [start + pd.Timedelta(seconds=15 * i) for i in range(1440)],
            "DayChangeRank": [1] * 1440,
            "RecorderGeneration": [5] * 1440,
        }
    )


def test_timing_coverage_counts_fills_and_nonfills_as_resolved(tmp_path):
    date = "2026-07-22"
    entry = _entry(date, "AAA", 0)
    variants = list(ENTRY_VARIANTS)
    filled_variants = variants[:-1]
    pd.DataFrame([entry]).to_csv(tmp_path / "entries.csv", index=False)
    pd.DataFrame(_timing_outcomes(entry, filled_variants)).to_csv(
        tmp_path / "outcomes.csv", index=False
    )
    nonfill = {
        "TimingId": f"{entry['EventId']}|{variants[-1]}",
        "EventId": entry["EventId"],
        "Variant": variants[-1],
        "ResolvedAt": f"{date} 09:50:00",
        "Reason": "entry_window_expired",
    }
    pd.DataFrame([nonfill]).to_csv(tmp_path / "nonfills.csv", index=False)
    entries, events, nonfills = load_timing_events(
        [str(tmp_path / "entries.csv")],
        [str(tmp_path / "outcomes.csv")],
        [str(tmp_path / "nonfills.csv")],
    )
    raw = pd.DataFrame([{"date": date, "full_session_eligible": True}])

    coverage = timing_session_coverage(entries, events, nonfills, raw).iloc[0]

    assert coverage["complete_variants"] == len(ENTRY_VARIANTS)
    assert bool(coverage["eligible"])


def test_entry_timing_selection_freezes_and_forward_cannot_rewrite_it(tmp_path):
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
            outcomes.extend(_timing_outcomes(entry))
    pd.DataFrame(entries).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes).to_csv(paper / "outcomes.csv", index=False)

    selected = run_audit(
        entry_patterns=[str(paper / "entries.csv")],
        outcome_patterns=[str(paper / "outcomes.csv")],
        nonfill_patterns=[str(paper / "nonfills*.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert selected["status"] == "collecting_entry_timing_forward_sessions"
    assert selected["frozen_selection"]["variant"] in ENTRY_VARIANTS
    assert selected["frozen_selection"]["selector"]
    frozen_before = (output / "frozen_selection.json").read_text(encoding="utf-8")

    forward_date = "2026-06-17"
    _raw(forward_date).to_csv(raw / f"live_features_{forward_date}.csv", index=False)
    loss = _entry(forward_date, "LOSS", 0)
    pd.DataFrame(entries + [loss]).to_csv(paper / "entries.csv", index=False)
    pd.DataFrame(outcomes + _timing_outcomes(loss, net_return=-1.1)).to_csv(
        paper / "outcomes.csv", index=False
    )
    forward = run_audit(
        entry_patterns=[str(paper / "entries.csv")],
        outcome_patterns=[str(paper / "outcomes.csv")],
        nonfill_patterns=[str(paper / "nonfills*.csv")],
        raw_patterns=[str(raw / "*.csv")],
        output_dir=output,
    )

    assert forward["forward"]["sessions"] == 1
    assert (output / "frozen_selection.json").read_text(encoding="utf-8") == frozen_before
    assert not forward["real_orders_allowed"]
