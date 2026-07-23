import csv
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder


def _tick(
    price: float,
    close: float = 100.0,
    day_open: float = 100.0,
    high: float = 0.0,
    volume: int = 100000,
    buy_qty: int = 300000,
    sell_qty: int = 100000,
    bid: float = 0.0,
    ask: float = 0.0,
):
    high = high or price
    bid = bid or round(price - 0.05, 2)
    ask = ask or round(price + 0.05, 2)
    return {
        "last_price": price,
        "close": close,
        "open": day_open,
        "high": high,
        "volume_traded": volume,
        "total_buy_quantity": buy_qty,
        "total_sell_quantity": sell_qty,
        "depth": {
            "buy": [{"price": bid, "quantity": 1000}],
            "sell": [{"price": ask, "quantity": 1000}],
        },
    }


def test_best_bid_ask_and_spread_from_depth(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=0.5)
    now = datetime(2026, 7, 3, 9, 20)

    rows = recorder.build_rows(
        {"AAA": _tick(101.0, close=100.0, bid=100.90, ask=101.10)},
        alerted_symbols={"AAA"},
        now=now,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["BestBid"] == 100.90
    assert row["BestAsk"] == 101.10
    assert row["SpreadBps"] == pytest.approx(19.802, abs=0.002)
    assert row["SpreadOk"] is True


def test_zero_quote_is_not_an_executable_spread(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=0.5)
    tick = _tick(101.0, close=100.0)
    tick["depth"] = {"buy": [], "sell": []}
    rows = recorder.build_rows(
        {"AAA": tick},
        alerted_symbols={"AAA"},
        now=datetime(2026, 7, 3, 9, 20),
    )
    assert rows[0]["BestBid"] == 0.0
    assert rows[0]["BestAsk"] == 0.0
    assert rows[0]["SpreadOk"] is False


def test_order_book_stability_requires_multiple_samples(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path),
        interval_seconds=0,
        top_gainer_pct=20.0,
        stability_window=3,
        min_stable_bs_ratio=2.0,
    )
    start = datetime(2026, 7, 3, 9, 20)

    first = recorder.build_rows(
        {"AAA": _tick(101.0, buy_qty=300000, sell_qty=100000)},
        alerted_symbols={"AAA"},
        now=start,
    )[0]
    second = recorder.build_rows(
        {"AAA": _tick(101.2, buy_qty=280000, sell_qty=100000)},
        alerted_symbols={"AAA"},
        now=start + timedelta(seconds=20),
    )[0]
    third = recorder.build_rows(
        {"AAA": _tick(101.3, buy_qty=350000, sell_qty=100000)},
        alerted_symbols={"AAA"},
        now=start + timedelta(seconds=40),
    )[0]

    assert first["OrderBookStable"] is False
    assert second["OrderBookStable"] is False
    assert third["OrderBookStable"] is True
    assert third["BuyerSellerMinWindow"] == pytest.approx(2.8)
    assert third["BuyerSellerMedianWindow"] == pytest.approx(3.0)


def test_sector_context_and_missed_runner_label(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=5.0)
    now = datetime(2026, 7, 3, 9, 40)
    ticks = {
        "AAA": _tick(106.0, close=100.0),
        "BBB": _tick(103.0, close=100.0),
        "CCC": _tick(99.0, close=100.0),
        "DDD": _tick(101.0, close=100.0),
    }

    def sector_for(symbol):
        return "AUTO" if symbol in {"AAA", "BBB", "CCC"} else "PHARMA"

    with patch("pkscreener.classes.LiveFeatureRecorder.get_sector", side_effect=sector_for):
        rows = recorder.build_rows(ticks, alerted_symbols=set(), now=now)

    aaa = next(row for row in rows if row["Symbol"] == "AAA")
    assert aaa["MissedRunnerCandidate"] is True
    assert aaa["IsTopGainerNow"] is True
    assert aaa["Sector"] == "AUTO"
    assert aaa["SectorCount"] == 3
    assert aaa["SectorTop2Count"] == 2
    assert aaa["SectorBreadthPct"] == pytest.approx(66.667, abs=0.01)
    assert "missed_runner" in aaa["RecordReason"]


def test_unknown_symbols_do_not_create_a_fake_sector_cluster(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=5.0)
    ticks = {f"S{index}": _tick(106.0, close=100.0) for index in range(5)}
    with patch("pkscreener.classes.LiveFeatureRecorder.get_sector", return_value="UNKNOWN"):
        rows = recorder.build_rows(
            ticks,
            alerted_symbols=set(),
            now=datetime(2026, 7, 3, 9, 40),
        )
    assert rows
    assert all(row["SectorTop2Count"] == 0 for row in rows)
    assert all("sector_leader_cluster" not in row["RecordReason"] for row in rows)


def test_record_persists_and_throttles(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=60, top_gainer_pct=5.0)
    now = datetime(2026, 7, 3, 9, 20)
    ticks = {"AAA": _tick(106.0, close=100.0)}

    first = recorder.record(ticks, alerted_symbols=set(), now=now)
    second = recorder.record(ticks, alerted_symbols=set(), now=now + timedelta(seconds=10))

    assert len(first) == 1
    assert second.empty
    path = tmp_path / "live_feature_recorder" / "live_features_2026-07-03.csv"
    assert path.exists()
    status_path = tmp_path / "live_feature_recorder" / "full_universe_rank_status.json"
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["first_recorded_at"] == "2026-07-03 09:20:00"
    assert status["full_session_eligible"] is True


def test_rank_status_marks_midday_start_as_partial_and_preserves_first_time(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=0.5
    )
    ticks = {"AAA": _tick(101.0, close=100.0)}

    recorder.record(
        ticks, alerted_symbols=set(), now=datetime(2026, 7, 3, 12, 15)
    )
    recorder.record(
        ticks, alerted_symbols=set(), now=datetime(2026, 7, 3, 12, 16)
    )

    status_path = tmp_path / "live_feature_recorder" / "full_universe_rank_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["first_recorded_at"] == "2026-07-03 12:15:00"
    assert status["recorded_at"] == "2026-07-03 12:16:00"
    assert status["full_session_eligible"] is False


def test_rank_status_resets_start_when_recorder_generation_changes(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=0.5
    )
    status_path = (
        tmp_path / "live_feature_recorder" / "full_universe_rank_status.json"
    )
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "date": "2026-07-03",
                "first_recorded_at": "2026-07-03 09:15:00",
                "recorder_generation": 1,
            }
        ),
        encoding="utf-8",
    )

    recorder.record(
        {"AAA": _tick(101.0, close=100.0)},
        alerted_symbols=set(),
        now=datetime(2026, 7, 3, 12, 15),
    )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["recorder_generation"] == LiveFeatureRecorder.GENERATION
    assert status["generation_started_at"] == "2026-07-03 12:15:00"
    assert status["first_recorded_at"] == "2026-07-03 12:15:00"
    assert status["full_session_eligible"] is False


def test_record_atomically_widens_old_same_day_schema(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path), interval_seconds=0, top_gainer_pct=0.5
    )
    now = datetime(2026, 7, 3, 9, 20)
    path = tmp_path / "live_feature_recorder" / "live_features_2026-07-03.csv"
    path.parent.mkdir(parents=True)
    old_columns = [
        column
        for column in recorder.COLUMNS
        if column
        not in {
            "UniverseSize", "DayChangeRank", "DayChangeRankPct",
            "PreviousDayChangeRank", "RankImprovement",
            "RankVelocityPerMinute", "DayChangeDeltaPct", "EnteredTop100",
            "EnteredTop50", "RapidRankClimber",
        }
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(old_columns)
        writer.writerow(["old" if column == "Symbol" else "" for column in old_columns])

    recorder.record(
        {"AAA": _tick(101.0, close=100.0)}, alerted_symbols=set(), now=now
    )

    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == recorder.COLUMNS
    assert {len(row) for row in rows} == {len(recorder.COLUMNS)}
    saved = pd.read_csv(path)
    assert saved["Symbol"].tolist() == ["old", "AAA"]


def test_full_universe_rank_pool_records_early_climber_before_five_percent(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path),
        interval_seconds=0,
        top_gainer_pct=50.0,
        leader_pool_size=10,
        rapid_rank_improvement=25,
        rapid_change_delta_pct=0.15,
        max_rows_per_cycle=20,
    )
    start = datetime(2026, 7, 3, 9, 20)
    first_ticks = {
        f"S{index:03d}": _tick(100.0 + index / 100.0, close=100.0)
        for index in range(1, 121)
    }
    recorder.build_rows(first_ticks, alerted_symbols=set(), now=start)
    second_ticks = dict(first_ticks)
    second_ticks["S001"] = _tick(104.0, close=100.0)

    rows = recorder.build_rows(
        second_ticks,
        alerted_symbols=set(),
        now=start + timedelta(minutes=1),
    )
    climbed = next(row for row in rows if row["Symbol"] == "S001")

    assert climbed["DayChangePct"] == 4.0
    assert climbed["UniverseSize"] == 120
    assert climbed["PreviousDayChangeRank"] == 120
    assert climbed["DayChangeRank"] == 1
    assert climbed["RankImprovement"] == 119
    assert climbed["RankVelocityPerMinute"] == 119.0
    assert climbed["EnteredTop100"] is True
    assert climbed["EnteredTop50"] is True
    assert climbed["RapidRankClimber"] is True
    assert "rapid_rank_climber" in climbed["RecordReason"]
    assert "full_universe_leader_pool" in climbed["RecordReason"]


def test_leader_pool_cannot_be_crowded_out_by_supplemental_rows(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path),
        interval_seconds=0,
        top_gainer_pct=50.0,
        stability_window=2,
        min_stable_bs_ratio=2.0,
        leader_pool_size=10,
        max_rows_per_cycle=10,
    )
    start = datetime(2026, 7, 3, 9, 20)
    ticks = {
        f"S{index:03d}": _tick(
            105.0 - index / 100.0,
            close=100.0,
            buy_qty=300000,
            sell_qty=100000,
        )
        for index in range(1, 41)
    }
    recorder.build_rows(ticks, alerted_symbols=set(), now=start)

    rows = recorder.build_rows(
        ticks, alerted_symbols=set(), now=start + timedelta(minutes=1)
    )

    assert len(rows) == 10
    assert {row["DayChangeRank"] for row in rows} == set(range(1, 11))
    assert {row["RecorderGeneration"] for row in rows} == {
        LiveFeatureRecorder.GENERATION
    }

    recorder.record(
        ticks, alerted_symbols=set(), now=start + timedelta(minutes=2)
    )
    status = json.loads(
        (
            tmp_path
            / "live_feature_recorder"
            / "full_universe_rank_status.json"
        ).read_text(encoding="utf-8")
    )
    assert status["leader_pool_candidates_this_snapshot"] == 10
    assert status["leader_pool_recorded_this_snapshot"] == 10
    assert status["leader_pool_recall_pct"] == 100.0


def test_default_discovery_pool_guarantees_top_200(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path),
        top_gainer_pct=50.0,
        stability_window=10,
    )
    ticks = {
        f"S{index:03d}": _tick(
            103.5 - index / 1000.0,
            close=100.0,
            buy_qty=100000,
            sell_qty=100000,
        )
        for index in range(1, 251)
    }

    rows = recorder.build_rows(
        ticks,
        alerted_symbols=set(),
        now=datetime(2026, 7, 3, 9, 20),
    )

    assert recorder.leader_pool_size == 200
    assert recorder.interval_seconds == 15.0
    assert len(rows) == 200
    assert {row["DayChangeRank"] for row in rows} == set(range(1, 201))
    assert {row["RecorderGeneration"] for row in rows} == {5}


def test_rapid_climbers_make_row_limit_a_soft_cap(tmp_path):
    recorder = LiveFeatureRecorder(
        output_dir=str(tmp_path),
        interval_seconds=0,
        top_gainer_pct=50.0,
        stability_window=10,
        leader_pool_size=5,
        max_rows_per_cycle=10,
        rapid_rank_improvement=5,
        rapid_change_delta_pct=0.15,
    )
    start = datetime(2026, 7, 3, 9, 20)
    initial_ticks = {
        f"S{index:03d}": _tick(
            100.0 + index / 100.0,
            close=100.0,
            buy_qty=100000,
            sell_qty=100000,
        )
        for index in range(1, 41)
    }
    recorder.build_rows(
        initial_ticks, alerted_symbols=set(), now=start
    )

    moved_ticks = dict(initial_ticks)
    for index in range(1, 21):
        moved_ticks[f"S{index:03d}"] = _tick(
            104.0 + index / 100.0,
            close=100.0,
            buy_qty=100000,
            sell_qty=100000,
        )
    frame = recorder.record(
        moved_ticks,
        alerted_symbols=set(),
        now=start + timedelta(minutes=1),
    )
    rows = frame.to_dict(orient="records")

    rapid_rows = [row for row in rows if row["RapidRankClimber"]]
    assert len(rapid_rows) == 20
    assert len(rows) == 20
    assert len(rows) > recorder.max_rows_per_cycle
    assert {row["DayChangeRank"] for row in rows[:5]} == set(range(1, 6))

    status = json.loads(
        (
            tmp_path
            / "live_feature_recorder"
            / "full_universe_rank_status.json"
        ).read_text(encoding="utf-8")
    )
    assert status["rapid_candidates_this_snapshot"] == 20
    assert status["rapid_recorded_this_snapshot"] == 20
    assert status["rapid_recall_pct"] == 100.0


def test_record_decisions_persists_enriched_lane_journal(tmp_path):
    recorder = LiveFeatureRecorder(output_dir=str(tmp_path), interval_seconds=60, top_gainer_pct=5.0)
    now = datetime(2026, 7, 3, 9, 20, 7)
    ticks = {"AAA": _tick(106.0, close=100.0, high=107.0, bid=105.95, ask=106.05)}
    decisions = [
        {
            "When": "2026-07-03 09:20:07",
            "Symbol": "AAA",
            "Decision": "reject",
            "Reason": "strength below radar floor",
            "Action": "Watch for Buy",
            "PriceNow": 106.0,
            "ChangePct": 6.0,
            "SignalStrengthPct": 35.0,
            "VolumeVsAvg": 1.1,
            "BuyerSellerRatio": 3.0,
            "RSI": 62.0,
            "PullbackPct": 0.9,
            "Checks": "confidence ok",
            "Why": "Price above ATR trailing stop",
        }
    ]

    first = recorder.record_decisions("top_gainer_radar", decisions, ticks=ticks, now=now)
    duplicate = recorder.record_decisions("top_gainer_radar", decisions, ticks=ticks, now=now + timedelta(seconds=10))

    assert len(first) == 1
    assert duplicate.empty
    row = first.iloc[0]
    assert row["Lane"] == "top_gainer_radar"
    assert row["Symbol"] == "AAA"
    assert row["Decision"] == "reject"
    assert row["Reason"] == "strength below radar floor"
    assert row["BestBid"] == 105.95
    assert row["BestAsk"] == 106.05
    assert row["SpreadBps"] == pytest.approx(9.434, abs=0.002)
    assert bool(row["SpreadOk"]) is True

    path = tmp_path / "live_feature_recorder" / "lane_decisions_2026-07-03.csv"
    assert path.exists()
    saved = pd.read_csv(path)
    assert list(saved["Symbol"]) == ["AAA"]
    assert list(saved["Lane"]) == ["top_gainer_radar"]
