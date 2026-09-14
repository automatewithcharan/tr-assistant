import json

import pandas as pd

import pkscreener.classes.SignalMonitor as signal_monitor_module
from pkscreener.classes.ManualDiscordDeliveryGuard import NSEEQManualDiscordGuard
from pkscreener.classes.SignalMonitor import SignalMonitorRunner


def _write_instruments(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)


def test_dual_listed_symbol_is_eligible_when_nse_eq_identity_exists(tmp_path):
    path = tmp_path / "kite_instruments_nse_latest.csv"
    _write_instruments(
        path,
        [{
            "tradingsymbol": "DUAL",
            "exchange": "NSE",
            "segment": "NSE",
            "instrument_type": "EQ",
            "instrument_token": 123,
        }],
    )
    guard = NSEEQManualDiscordGuard(str(tmp_path), str(path))

    decision = guard.check("dual", {"exchange": "BSE"})

    assert decision.eligible is True
    assert decision.exchange == "NSE"
    assert decision.series == "EQ"


def test_bse_only_or_non_eq_symbol_is_suppressed(tmp_path):
    path = tmp_path / "kite_instruments_nse_latest.csv"
    _write_instruments(
        path,
        [{
            "tradingsymbol": "NSEETF",
            "exchange": "NSE",
            "segment": "NSE",
            "instrument_type": "ETF",
            "instrument_token": 456,
        }],
    )
    guard = NSEEQManualDiscordGuard(str(tmp_path), str(path))

    assert guard.check("BSEONLY", {"exchange": "BSE", "segment": "BSE", "instrument_type": "EQ"}).eligible is False
    assert guard.check("NSEETF").eligible is False


def test_missing_instrument_metadata_fails_closed(tmp_path):
    guard = NSEEQManualDiscordGuard(str(tmp_path))

    decision = guard.check("UNKNOWN")

    assert decision.eligible is False
    assert decision.reason == "nse_instrument_metadata_unavailable"


def test_runtime_initialization_with_missing_metadata_fails_closed_for_delivery(
    tmp_path,
):
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    runner.run_at = None

    runner._initialize_runtime_helpers()

    assert runner._manual_discord_guard_required is True
    assert runner._manual_discord_guard is not None
    assert runner._manual_discord_row_is_eligible(
        {"Symbol": "UNKNOWN"}, "candidate"
    ) is False


def test_runtime_guard_initialization_failure_fails_closed(tmp_path, monkeypatch):
    class BrokenGuard:
        def __init__(self, _output_dir):
            raise OSError("instrument metadata unavailable")

    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner.output_dir = str(tmp_path)
    runner.run_at = None
    monkeypatch.setattr(signal_monitor_module, "NSEEQManualDiscordGuard", BrokenGuard)

    runner._initialize_runtime_helpers()

    assert runner._manual_discord_guard is None
    assert runner._manual_discord_row_is_eligible(
        {"Symbol": "UNKNOWN"}, "candidate"
    ) is False


def test_suppression_observability_is_deduplicated(tmp_path):
    guard = NSEEQManualDiscordGuard(str(tmp_path))
    decision = guard.check("BSEONLY")

    guard.record_suppression(decision, "anomaly")
    guard.record_suppression(decision, "anomaly")

    files = list((tmp_path / "manual_discord_delivery_guard").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BSEONLY"
    assert rows[0]["source"] == "anomaly"


def test_signal_monitor_filters_only_delivery_copy(tmp_path):
    path = tmp_path / "kite_instruments_nse_latest.csv"
    _write_instruments(
        path,
        [{
            "tradingsymbol": "NSEOK",
            "exchange": "NSE",
            "segment": "NSE",
            "instrument_type": "EQ",
            "instrument_token": 789,
        }],
    )
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._manual_discord_guard = NSEEQManualDiscordGuard(str(tmp_path), str(path))
    runner.run_at = None
    candidate_frame = pd.DataFrame(
        [
            {"Symbol": "NSEOK", "NowAction": "Buy Now"},
            {"Symbol": "BSEONLY", "NowAction": "Grade-B Anomaly"},
        ]
    )

    delivery_frame = runner._filter_manual_discord_frame(candidate_frame, "candidate")

    assert delivery_frame["Symbol"].tolist() == ["NSEOK"]
    assert candidate_frame["Symbol"].tolist() == ["NSEOK", "BSEONLY"]


def test_monitor_discord_boundary_never_formats_or_sends_bse_only_row(tmp_path):
    path = tmp_path / "kite_instruments_nse_latest.csv"
    _write_instruments(
        path,
        [{
            "tradingsymbol": "NSEOK",
            "exchange": "NSE",
            "segment": "NSE",
            "instrument_type": "EQ",
            "instrument_token": 789,
        }],
    )
    runner = SignalMonitorRunner.__new__(SignalMonitorRunner)
    runner._manual_discord_guard = NSEEQManualDiscordGuard(str(tmp_path), str(path))
    runner.run_at = None
    runner._alert_timing_observer = None
    sent = []
    journaled = []
    runner._send_discord_alert = sent.append
    runner._journal_discord_alerts = lambda frame, *_args: journaled.extend(frame["Symbol"])
    runner._build_discord_monitor_message = (
        lambda frame, *_args, **_kwargs: " ".join(
            f"`{symbol}`" for symbol in frame["Symbol"]
        )
    )
    candidates = pd.DataFrame(
        [
            {"Symbol": "NSEOK", "NowAction": "Buy Now"},
            {"Symbol": "BSEONLY", "NowAction": "Grade-B Anomaly"},
        ]
    )

    runner._send_discord_monitor_alerts(candidates, {})

    assert sent == ["`NSEOK`"]
    assert journaled == ["NSEOK"]
    assert candidates["Symbol"].tolist() == ["NSEOK", "BSEONLY"]
