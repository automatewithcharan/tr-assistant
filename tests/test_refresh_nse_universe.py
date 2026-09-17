import pandas as pd
import pytest

from scripts.refresh_nse_universe import refresh


def _data(new_symbol="NEWLISTING"):
    old = [f"OLD{i:04d}" for i in range(1600)]
    official = pd.DataFrame({
        "SYMBOL": [*old, new_symbol],
        "SERIES": ["EQ"] * 1601,
    })
    content = official.to_csv(index=False).encode()
    instruments = pd.DataFrame({
        "tradingsymbol": [*old, new_symbol, *[f"OTHER{i:04d}" for i in range(3400)]],
        "instrument_token": list(range(5001)),
        "segment": ["NSE"] * 5001,
        "exchange": ["NSE"] * 5001,
        "instrument_type": ["EQ"] * 5001,
    })
    return old, content, instruments


def test_refresh_adds_new_nse_listing_before_bse_and_updates_guard_metadata(tmp_path):
    old, content, instruments = _data()
    indices = tmp_path / "results" / "Indices"
    indices.mkdir(parents=True)
    pd.DataFrame({"SYMBOL": old, "SERIES": ["EQ"] * len(old)}).to_csv(
        indices / "EQUITY_L.csv", index=False
    )
    pd.DataFrame({"Symbol": [*old, "BSEONLY"]}).to_csv(
        indices / "NSE_BSE_COMBINED.csv", index=False
    )
    pd.DataFrame({"Symbol": ["BSEONLY"]}).to_csv(
        indices / "BSE_TRADEABLE.csv", index=False
    )

    summary = refresh(tmp_path, content=content, instruments=instruments)

    combined = pd.read_csv(indices / "NSE_BSE_COMBINED.csv")["Symbol"].tolist()
    master = pd.read_csv(
        tmp_path / "results" / "Data" / "kite_instruments_nse_latest.csv"
    )
    assert summary["new_nse_symbols"] == 1
    assert combined[-2:] == ["NEWLISTING", "BSEONLY"]
    assert "NEWLISTING" in set(master["tradingsymbol"])


def test_invalid_official_download_leaves_existing_files_untouched(tmp_path):
    indices = tmp_path / "results" / "Indices"
    indices.mkdir(parents=True)
    old = indices / "EQUITY_L.csv"
    old.write_text("SYMBOL,SERIES\nEXISTING,EQ\n")
    _, _, instruments = _data()

    with pytest.raises(ValueError, match="size validation"):
        refresh(tmp_path, content=b"SYMBOL,SERIES\nONE,EQ\n", instruments=instruments)

    assert old.read_text() == "SYMBOL,SERIES\nEXISTING,EQ\n"
