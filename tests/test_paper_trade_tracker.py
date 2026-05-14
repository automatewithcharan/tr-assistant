from datetime import datetime

import pkscreener.classes.PaperTradeTracker as paper_tracker_module
from pkscreener.classes.PaperTradeTracker import PaperTradeTracker


def test_paper_tracker_reloads_open_trades_and_closes_with_top_level_tick_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(
        paper_tracker_module.Archiver,
        "get_user_data_dir",
        staticmethod(lambda: str(tmp_path)),
    )
    monkeypatch.setattr(
        paper_tracker_module.PKDateUtilities,
        "currentDateTime",
        staticmethod(lambda: datetime(2026, 5, 12, 10, 15, 0)),
    )

    tracker = PaperTradeTracker()
    tracker.log_entry(
        symbol="TEST",
        price=100.0,
        score=72.0,
        grade="B",
        volume_ratio=8.0,
        change_at_entry=2.5,
        stop_loss=99.0,
        target1=103.0,
        target2=105.0,
        suggested_qty=10,
        source="signal_monitor_anomaly",
    )

    restarted_tracker = PaperTradeTracker()
    summary = restarted_tracker.close_day(
        {
            "TEST": {
                "last_price": 104.0,
                "high": 106.0,
                "low": 98.5,
            }
        }
    )

    assert "Trades: 1" in summary
    assert "Avg P&L: +4.00%" in summary
    report = restarted_tracker.generate_report(days=1)
    assert "Total Trades:     1" in report
    assert "signal_monitor_anomaly" in report
