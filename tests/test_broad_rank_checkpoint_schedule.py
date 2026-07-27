from pathlib import Path


def _signal_times(script_name: str, variable: str) -> tuple[list[str], str]:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / script_name
    ).read_text(encoding="utf-8")
    signal_line = next(
        line for line in script.splitlines()
        if line.startswith(variable)
    )
    return signal_line.split(":-", 1)[-1].rstrip('}"').split(","), script


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
    assert (
        "FIXED_PHASE_SIGNAL_TIMES=(09:30 10:30 12:30 14:15)"
        in script
    )
    assert (
        'for SIGNAL_TIME in "${FIXED_PHASE_SIGNAL_TIMES[@]}"; do'
        in script
    )


def test_full_session_launchers_keep_delayed_intraday_starters():
    for script_name in (
        "run_full_session_leader_research.sh",
        "run_full_session_regime_leader_research.sh",
    ):
        values, script = _signal_times(
            script_name,
            'SIGNAL_TIMES="${FULL_SESSION_SIGNAL_TIMES:-',
        )
        assert len(values) == 20
        assert values[0] == "09:30"
        assert values[-1] == "14:15"
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


def test_final_checkpoint_uses_mapped_universe_gate_function():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_broad_rank_acceleration_checkpoint.sh"
    ).read_text(encoding="utf-8")

    assert "required_goal_cached_symbols" in script
    assert 'REQUIRED="$FINAL_REQUIRED"' in script
    assert 'final_required_cached_symbols.txt' in script
