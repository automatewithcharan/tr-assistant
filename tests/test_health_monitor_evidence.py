from pkscreener.classes.HealthMonitor import HealthMonitor


def test_evidence_worker_is_optional_when_standalone_mode_is_off(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("SIGNALMONITOR_STANDALONE_EVIDENCE", raising=False)

    monitor = HealthMonitor(1, str(tmp_path))

    assert monitor.workers["evidence"]["required"] is False


def test_evidence_worker_is_required_when_standalone_mode_is_on(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SIGNALMONITOR_STANDALONE_EVIDENCE", "1")

    monitor = HealthMonitor(1, str(tmp_path))

    assert monitor.workers["evidence"]["required"] is True
