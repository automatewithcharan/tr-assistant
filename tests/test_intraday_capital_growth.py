import json

import pandas as pd

from scripts.project_intraday_capital_growth import (
    load_validated_strategy,
    project_months,
    required_capital_table,
    run_projection,
)


def _status(path, *, accepted, sessions=20, avg_return=0.5, lower=0.1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "accepted" if accepted else "collecting",
                "accepted": accepted,
                "forward": {
                    "sessions": sessions,
                    "trades": 40,
                    "avg_daily_return_pct": avg_return,
                    "avg_daily_pnl": avg_return * 5000,
                    "lower_95_pct": lower,
                },
                "real_orders_allowed": False,
            }
        ),
        encoding="utf-8",
    )


def test_only_positive_accepted_forward_evidence_can_drive_projection(tmp_path):
    collecting = tmp_path / "collecting.json"
    weak = tmp_path / "weak.json"
    accepted = tmp_path / "accepted.json"
    _status(collecting, accepted=False, avg_return=2.0)
    _status(weak, accepted=True, sessions=10, avg_return=1.0)
    _status(accepted, accepted=True, sessions=20, avg_return=0.6, lower=0.2)

    selected = load_validated_strategy(
        {"collecting": str(collecting), "weak": str(weak), "good": str(accepted)}
    )

    assert selected["name"] == "good"
    assert selected["avg_daily_return_pct"] == 0.6


def test_monthly_additions_reduce_required_return_and_compound_profit():
    projection = project_months(
        initial_capital=500000,
        monthly_contribution=200000,
        daily_goal=10000,
        daily_return_pct=1.0,
        months=2,
        trading_days_per_month=20,
        scenario="test",
    )

    assert projection.iloc[0]["required_return_for_goal_pct"] == 2.0
    assert projection.iloc[0]["end_capital"] > 800000
    assert projection.iloc[1]["required_return_for_goal_pct"] < 1.25
    assert projection.iloc[1]["end_capital"] > projection.iloc[0]["end_capital"] + 200000


def test_required_capital_math_for_daily_goal():
    table = required_capital_table(10000, [0.5, 1.0, 2.0]).set_index(
        "daily_return_pct"
    )

    assert table.loc[0.5, "capital_required_for_daily_goal"] == 2000000
    assert table.loc[1.0, "capital_required_for_daily_goal"] == 1000000
    assert table.loc[2.0, "capital_required_for_daily_goal"] == 500000


def test_projection_keeps_math_scenarios_separate_when_nothing_is_validated(tmp_path):
    unaccepted = tmp_path / "unaccepted.json"
    _status(unaccepted, accepted=False, avg_return=2.0)
    output = tmp_path / "output"

    status = run_projection(
        strategy_paths={"paper": str(unaccepted)},
        output_dir=output,
        initial_capital=500000,
        monthly_contribution=200000,
        daily_goal=10000,
        months=12,
    )

    scenarios = pd.read_csv(output / "monthly_scenarios.csv")
    assert status["status"] == "awaiting_validated_forward_strategy"
    assert status["initial_required_daily_return_pct"] == 2.0
    assert status["first_month_required_return_at_or_below_1pct_without_trading_profit"] == 4
    assert not status["capital_projection_allowed"]
    assert not status["real_orders_allowed"]
    assert not scenarios["scenario"].str.startswith("validated_").any()
