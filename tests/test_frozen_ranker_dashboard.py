import json

from scripts import dashboard


def test_leader_dashboard_reads_frozen_ranker_status(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    root = tmp_path / "live_leader_continuation"
    root.mkdir()
    (root / "frozen_ranker_status.json").write_text(
        json.dumps(
            {
                "status": "collecting_forward_evidence",
                "contract": "t1.5_s1.0",
                "forward_start": "2026-07-22",
                "forward_stress": {
                    "sessions": 3,
                    "trades": 4,
                    "avg_daily_pnl": 1250.0,
                    "lower_95_pct": -0.4,
                    "goal_hit_pct": 0.0,
                },
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )

    stats = dashboard.leader_discovery_stats()

    assert stats["ranker_status"] == "collecting_forward_evidence"
    assert stats["ranker_forward_sessions"] == 3
    assert stats["ranker_forward_trades"] == 4
    assert stats["ranker_stress_avg_pnl"] == 1250.0
    assert not stats["ranker_accepted"]
