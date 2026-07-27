from pathlib import Path


def test_next_broad_checkpoint_covers_delayed_intraday_starters():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_broad_rank_acceleration_checkpoint.sh"
    ).read_text(encoding="utf-8")
    signal_line = next(
        line for line in script.splitlines()
        if line.startswith('SIGNAL_TIMES="')
    )
    values = signal_line.split('"', 2)[1].split(",")

    assert len(values) == 20
    assert values[0] == "09:30"
    assert values[-1] == "14:15"
    assert "11:30" in values
    assert "13:30" in values
    assert '--signal-times "$SIGNAL_TIMES"' in script


def test_checkpoint_predeclares_dynamic_model_families():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_broad_rank_acceleration_checkpoint.sh"
    ).read_text(encoding="utf-8")

    for family in (
        "eventual_runner_bar_path_regime_classifier",
        "direct_target_bar_path_regime_classifier",
        "expected_return_snapshot_classifier",
        "expected_return_temporal_classifier",
        "expected_return_bar_path_classifier",
        "expected_return_bar_path_regime_classifier",
    ):
        assert family in script
    assert "--label-mode expected_return" in script
    registry_block = script.split(
        '"$PYTHON" scripts/select_broad_strategy_registry.py',
        1,
    )[1].split(
        '"$PYTHON" scripts/build_broad_rank_checkpoint_status.py',
        1,
    )[0]
    assert registry_block.count('--status "') == 15
