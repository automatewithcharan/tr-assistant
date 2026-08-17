from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pkscreener.classes.TopGainerLifecycleRecorder import TopGainerLifecycleRecorder
from scripts.enrich_top_gainer_lifecycle import enrich, load_events, main, write_events
from scripts.analyze_top_gainer_lifecycle import summarize


SESSION = "2026-08-04"


def _observation(tmp_path: Path, symbol: str = "AAA", when: str = "2026-08-04T09:30:00+05:30"):
    recorder = TopGainerLifecycleRecorder(str(tmp_path / "evidence"))
    lifecycle_id = recorder.record_observation(
        {"symbol": symbol, "current_price": 100.0, "feature_available_at": when,
         "missing_reasons": []},
        "early_breakout", when, "broad_nse",
    )
    return recorder, lifecycle_id


def _bars(bars_root: Path, symbol: str = "AAA", rows: list[dict] | None = None) -> Path:
    session = bars_root / f"{SESSION}_{SESSION}"
    session.mkdir(parents=True)
    path = session / f"{symbol}.csv"
    fields = ["datetime", "open", "high", "low", "close", "volume", "symbol"]
    if rows is None:
        start = datetime.fromisoformat(f"{SESSION}T09:31:00")
        end = datetime.fromisoformat(f"{SESSION}T15:29:00")
        rows = [{
            "datetime": current.isoformat(sep=" "), "open": 100,
            "high": 106 if current == start else 104,
            "low": 98 if current == start else 99,
            "close": 105 if current == start else 103,
            "volume": 20, "symbol": symbol,
        } for current in (
            start + timedelta(minutes=offset)
            for offset in range(int((end - start).total_seconds() // 60) + 1)
        )]
        rows.insert(0, {
            "datetime": f"{SESSION} 09:29:00", "open": 99, "high": 500,
            "low": 1, "close": 99, "volume": 10, "symbol": symbol,
        })
        rows.insert(1, {
            "datetime": f"{SESSION} 09:30:00", "open": 100, "high": 999,
            "low": 1, "close": 999, "volume": 10, "symbol": symbol,
        })
        rows.append({
            "datetime": f"{SESSION} 15:30:00", "open": 100, "high": 999,
            "low": 1, "close": 999, "volume": 10, "symbol": symbol,
        })
        rows.append({
            "datetime": "2026-08-05 09:15:00", "open": 100, "high": 999,
            "low": 1, "close": 999, "volume": 10, "symbol": symbol,
        })
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_offline_enrichment_uses_only_post_observation_local_bars(tmp_path):
    recorder, lifecycle_id = _observation(tmp_path)
    bars_root = tmp_path / "bars"
    _bars(bars_root)
    original = load_events(recorder.events_path)

    additions, result = enrich(original, bars_root, SESSION, SESSION)

    outcomes = [row for row in additions if row["event_type"] == "session_outcome_finalized"]
    assert result["complete"] == 1
    assert len(outcomes) == 1
    assert outcomes[0]["lifecycle_id"] == lifecycle_id
    assert outcomes[0]["future_labels"]["later_intraday_high"] == 106.0
    assert outcomes[0]["future_labels"]["mfe_pct"] == pytest.approx(6.0)
    assert outcomes[0]["future_labels"]["reached_5pct"] is True


def test_offline_enrichment_preserves_missing_and_no_post_bar_cases(tmp_path):
    recorder, _ = _observation(tmp_path, "MISSING")
    late_recorder, _ = _observation(tmp_path, "LATE", "2026-08-04T15:31:00+05:30")
    bars_root = tmp_path / "bars"
    _bars(bars_root, "LATE", [{
        "datetime": "2026-08-04 15:29:00", "open": 100, "high": 101,
        "low": 99, "close": 100, "volume": 10, "symbol": "LATE",
    }])
    original = load_events(recorder.events_path)

    additions, result = enrich(original, bars_root, SESSION, SESSION)

    outcomes = [row for row in additions if row["event_type"] == "session_outcome_finalized"]
    assert len(outcomes) == 2
    assert all(row["future_labels"]["completeness_status"] == "incomplete" for row in outcomes)
    assert result["incomplete:bar_file_missing"] == 1
    assert result["incomplete:no_valid_post_observation_bars"] == 1


def test_offline_enrichment_ignores_malformed_bar_rows(tmp_path):
    recorder, _ = _observation(tmp_path)
    bars_root = tmp_path / "bars"
    start = datetime.fromisoformat(f"{SESSION}T09:31:00")
    end = datetime.fromisoformat(f"{SESSION}T15:29:00")
    valid_rows = [{
        "datetime": current.isoformat(sep=" "), "open": 100,
        "high": 102 if current == start + timedelta(minutes=1) else 101,
        "low": 98 if current == start + timedelta(minutes=1) else 99,
        "close": 101 if current == start + timedelta(minutes=1) else 100,
        "volume": 10, "symbol": "AAA",
    } for current in (
        start + timedelta(minutes=offset)
        for offset in range(int((end - start).total_seconds() // 60) + 1)
    )]
    _bars(bars_root, rows=[
        {"datetime": "not-a-timestamp", "open": 100, "high": 999, "low": 1,
         "close": 500, "volume": 10, "symbol": "AAA"},
        {"datetime": "2026-08-04 09:31:00", "open": 100, "high": "bad", "low": 99,
         "close": 100, "volume": 10, "symbol": "AAA"},
        *valid_rows,
    ])

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert result["complete"] == 1
    assert outcome["future_labels"]["later_intraday_high"] == 102.0
    assert outcome["future_labels"]["later_intraday_low"] == 98.0


def test_offline_enrichment_skips_existing_finalized_observation(tmp_path):
    recorder, lifecycle_id = _observation(tmp_path)
    recorder.enrich_session(lifecycle_id, "2026-08-04T15:30:00+05:30", "eod_enrichment", {
        "completeness_status": "complete", "mfe_pct": 1.0,
    })
    bars_root = tmp_path / "bars"
    _bars(bars_root)

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    assert additions == []
    assert result["already_finalized"] == 1


def test_cli_writes_separate_enriched_copy_and_source_remains_unchanged(tmp_path, capsys):
    recorder, _ = _observation(tmp_path)
    bars_root = tmp_path / "bars"
    _bars(bars_root)
    before = recorder.events_path.read_bytes()
    output = tmp_path / "output" / "events.jsonl"

    assert main([
        "--events", str(recorder.events_path), "--bars-root", str(bars_root),
        "--start-date", SESSION, "--end-date", SESSION, "--output-events", str(output),
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["complete"] == 1
    assert recorder.events_path.read_bytes() == before
    assert summarize(output)["lane_comparison"]["early_breakout"]["complete_outcomes"] == 1


def test_cli_refuses_in_place_or_existing_output(tmp_path):
    recorder, _ = _observation(tmp_path)
    bars_root = tmp_path / "bars"
    _bars(bars_root)
    base = [
        "--events", str(recorder.events_path), "--bars-root", str(bars_root),
        "--start-date", SESSION, "--end-date", SESSION,
    ]
    with pytest.raises(SystemExit):
        main([*base, "--output-events", str(recorder.events_path)])
    existing = tmp_path / "already-there.jsonl"
    existing.write_text("do not replace", encoding="utf-8")
    with pytest.raises(SystemExit):
        main([*base, "--output-events", str(existing)])


def test_symbol_cannot_escape_local_session_directory(tmp_path):
    recorder, _ = _observation(tmp_path, "../SECRET")
    bars_root = tmp_path / "bars"
    bars_root.mkdir()

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    assert result["incomplete:unsafe_symbol_bar_path"] == 1
    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert outcome["future_labels"]["completeness_status"] == "incomplete"


def test_equal_timestamp_and_partial_session_never_become_outcomes(tmp_path):
    recorder, _ = _observation(tmp_path)
    bars_root = tmp_path / "bars"
    _bars(bars_root, rows=[
        {"datetime": "2026-08-04 09:30:00", "open": 100, "high": 999, "low": 1,
         "close": 999, "volume": 10, "symbol": "AAA"},
        {"datetime": "2026-08-04 09:31:00", "open": 100, "high": 106, "low": 98,
         "close": 105, "volume": 10, "symbol": "AAA"},
    ])

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert result["incomplete:post_observation_session_incomplete"] == 1
    assert outcome["future_labels"]["later_intraday_high"] is None
    assert outcome["future_labels"]["return_15m_pct"] is None
    assert outcome["future_labels"]["reached_3pct"] is None


def _continuous_rows(start: str, end: str, symbol: str = "AAA") -> list[dict]:
    first = datetime.fromisoformat(f"{SESSION}T{start}:00")
    last = datetime.fromisoformat(f"{SESSION}T{end}:00")
    return [{
        "datetime": current.isoformat(sep=" "), "open": 100,
        "high": 106 if current == first else 104, "low": 98,
        "close": 103, "volume": 10, "symbol": symbol,
    } for current in (
        first + timedelta(minutes=offset)
        for offset in range(int((last - first).total_seconds() // 60) + 1)
    )]


def test_pre_market_observation_starts_expected_coverage_at_0915(tmp_path):
    recorder, _ = _observation(tmp_path, when="2026-08-04T08:55:00+05:30")
    bars_root = tmp_path / "bars"
    rows = _continuous_rows("09:15", "15:29")
    rows.insert(0, {
        "datetime": f"{SESSION} 09:00:00", "open": 100, "high": 999,
        "low": 1, "close": 999, "volume": 10, "symbol": "AAA",
    })
    _bars(bars_root, rows=rows)

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert result["complete"] == 1
    assert outcome["future_labels"]["later_intraday_high"] == 106.0


def test_pre_market_observation_with_internal_session_gap_is_incomplete(tmp_path):
    recorder, _ = _observation(tmp_path, when="2026-08-04T08:55:00+05:30")
    bars_root = tmp_path / "bars"
    rows = [row for row in _continuous_rows("09:15", "15:29")
            if row["datetime"] != f"{SESSION} 10:00:00"]
    _bars(bars_root, rows=rows)

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert result["coverage:internal_minute_gap"] == 1
    assert outcome["future_labels"]["completeness_status"] == "incomplete"
    assert outcome["future_labels"]["reached_5pct"] is None


def test_first_expected_post_observation_minute_missing_is_incomplete(tmp_path):
    recorder, _ = _observation(tmp_path, when="2026-08-04T09:30:20+05:30")
    bars_root = tmp_path / "bars"
    _bars(bars_root, rows=_continuous_rows("09:32", "15:29"))

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert result["coverage:first_expected_minute_missing"] == 1
    assert outcome["future_labels"]["mfe_pct"] is None


def test_archive_ending_before_1529_is_incomplete(tmp_path):
    recorder, _ = _observation(tmp_path, when="2026-08-04T09:30:20+05:30")
    bars_root = tmp_path / "bars"
    _bars(bars_root, rows=_continuous_rows("09:31", "15:28"))

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert result["coverage:session_ended_before_15_29"] == 1
    assert outcome["future_labels"]["return_15m_pct"] is None


def test_utc_bar_timestamps_are_compared_as_ist_instants(tmp_path):
    recorder, _ = _observation(tmp_path, when="2026-08-04T15:27:00+05:30")
    bars_root = tmp_path / "bars"
    _bars(bars_root, rows=[
        {"datetime": "2026-08-04T09:57:00Z", "open": 100, "high": 999, "low": 1,
         "close": 999, "volume": 10, "symbol": "AAA"},
        {"datetime": "2026-08-04T09:58:00Z", "open": 100, "high": 106, "low": 98,
         "close": 105, "volume": 10, "symbol": "AAA"},
        {"datetime": "2026-08-04T09:59:00Z", "open": 100, "high": 104, "low": 99,
         "close": 103, "volume": 10, "symbol": "AAA"},
    ])

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert result["complete"] == 1
    assert outcome["future_labels"]["later_intraday_high"] == 106.0


@pytest.mark.parametrize("contents,reason", [
    ("", "bar_file_empty"),
    ("datetime,close\n2026-08-04 09:31:00,101\n", "bar_file_invalid"),
])
def test_empty_or_invalid_csv_is_explicitly_incomplete(tmp_path, contents, reason):
    recorder, _ = _observation(tmp_path)
    path = tmp_path / "bars" / f"{SESSION}_{SESSION}" / "AAA.csv"
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")

    additions, result = enrich(load_events(recorder.events_path), tmp_path / "bars", SESSION, SESSION)

    assert result[f"incomplete:{reason}"] == 1
    outcome = next(row for row in additions if row["event_type"] == "session_outcome_finalized")
    assert outcome["future_labels"]["reached_3pct"] is None


def test_corrupt_identity_and_malformed_observation_timestamp_fail_closed(tmp_path):
    recorder, _ = _observation(tmp_path)
    rows = load_events(recorder.events_path)
    rows[0]["source_lane"] = "different_lane"
    with pytest.raises(ValueError, match="identity mismatch"):
        enrich(rows, tmp_path, SESSION, SESSION)
    with pytest.raises(ValueError, match="invalid observation timestamp"):
        recorder.record_observation(
            {"symbol": "AAA", "feature_available_at": "not-a-time"},
            "lane", "not-a-time",
        )

    corrupt = tmp_path / "nonfinite.jsonl"
    corrupt.write_text('{"schema_version":1,"event_id":"x","value":NaN}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt lifecycle record"):
        load_events(corrupt)


def test_same_symbol_multiple_lanes_preserve_distinct_identity(tmp_path):
    recorder, first_id = _observation(tmp_path)
    second_id = recorder.record_observation(
        {"symbol": "AAA", "current_price": 100.0,
         "feature_available_at": "2026-08-04T09:30:00+05:30", "missing_reasons": []},
        "top_gainer_radar", "2026-08-04T09:30:00+05:30", "broad_nse",
        experiment_version="experiment_v2",
    )
    bars_root = tmp_path / "bars"
    _bars(bars_root)

    additions, result = enrich(load_events(recorder.events_path), bars_root, SESSION, SESSION)

    outcomes = [row for row in additions if row["event_type"] == "session_outcome_finalized"]
    assert result["complete"] == 2
    assert {row["lifecycle_id"] for row in outcomes} == {first_id, second_id}
    assert {(row["origin_source_lane"], row["experiment_version"]) for row in outcomes} == {
        ("early_breakout", "canonical_v1"), ("top_gainer_radar", "experiment_v2"),
    }


def test_enriched_copy_is_idempotent_input_for_another_run(tmp_path):
    recorder, _ = _observation(tmp_path)
    bars_root = tmp_path / "bars"
    _bars(bars_root)
    original = load_events(recorder.events_path)
    additions, _ = enrich(original, bars_root, SESSION, SESSION)

    repeated, result = enrich([*original, *additions], bars_root, SESSION, SESSION)

    assert repeated == []
    assert result["already_finalized"] == 1


def test_atomic_writer_never_overwrites_or_publishes_partial_output(tmp_path):
    existing = tmp_path / "existing.jsonl"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_events(existing, [{"event_id": "new"}])
    assert existing.read_text(encoding="utf-8") == "keep"

    partial = tmp_path / "partial.jsonl"

    def interrupted_rows():
        yield {"event_id": "first"}
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        write_events(partial, interrupted_rows())
    assert not partial.exists()
