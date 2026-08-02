import csv
import sys

from scripts import anomaly_outcomes


def test_cli_writes_separate_output_and_uses_cache_fallback(tmp_path, monkeypatch):
    source = tmp_path / "anomaly_history.csv"
    output = tmp_path / "research" / "audit.csv"
    first_cache = tmp_path / "minute"
    second_cache = tmp_path / "5minute"
    source.write_text(
        "Date,When,Symbol,PriceNow,Tier,Action,QualityScore\n"
        "2026-07-31,2026-07-31 09:30:00,AAA,100,B,Grade-B Anomaly,60\n",
        encoding="utf-8",
    )
    calls = []

    def fake_load(symbol, when, cache_root):
        calls.append(str(cache_root))
        if str(cache_root) == str(second_cache):
            return {
                "open": 100.0,
                "high": 103.0,
                "low": 99.0,
                "close": 102.0,
                "source": "5minute/AAA.csv",
                "start": "2026-07-31 09:30:00",
            }
        return None

    monkeypatch.setattr(anomaly_outcomes, "load_forward_intraday_bar", fake_load)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "anomaly_outcomes.py",
            "--input", str(source),
            "--output", str(output),
            "--intraday-cache", str(first_cache),
            "--intraday-cache", str(second_cache),
            "--force",
            "--no-daily-fallback",
        ],
    )

    assert anomaly_outcomes.main() == 0
    assert calls == [str(first_cache), str(second_cache)]
    assert source.read_text(encoding="utf-8").splitlines()[0].endswith("QualityScore")
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["MaxFavExcursionPct"] == "3.0"
    assert row["MaxAdvExcursionPct"] == "-1.0"
    assert row["OutcomeSource"] == "5minute/AAA.csv"
