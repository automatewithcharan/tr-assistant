import csv
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from pkscreener.classes.MarketObservationLedger import MarketObservationLedger
from pathlib import Path

IST = ZoneInfo("Asia/Kolkata")

def write_csv(path, headers, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers); writer.writeheader(); writer.writerows(rows)

def test_observation_is_raw_append_only_and_nifty_is_not_qualified(tmp_path):
    eq, nifty, tokens, ticks = (tmp_path / x for x in ("eq.csv", "nifty.csv", "tokens.csv", "ticks.csv"))
    write_csv(eq, ["SYMBOL", "SERIES"], [{"SYMBOL":"NIFTYONE","SERIES":"EQ"}, {"SYMBOL":"OTHER","SERIES":"EQ"}])
    write_csv(nifty, ["Symbol"], [{"Symbol":"NIFTYONE"}]); write_csv(tokens, ["Symbol"], [{"Symbol":"NIFTYONE"}, {"Symbol":"OTHER"}])
    now = datetime(2026, 10, 1, 10, tzinfo=IST)
    write_csv(ticks, ["symbol","exchange","received_at","last_trade_time","last_price","close"], [
        {"symbol":"NIFTYONE","exchange":"NSE","received_at":now.isoformat(),"last_trade_time":now.isoformat(),"last_price":"110","close":"100"},
        {"symbol":"OTHER","exchange":"NSE","received_at":now.isoformat(),"last_trade_time":now.isoformat(),"last_price":"105","close":"100"}])
    ledger = MarketObservationLedger(tmp_path / "ledger", eq, nifty, tokens, lambda: now)
    assert ledger.process(ticks)["fresh_nifty50"] == 1
    state = json.loads((tmp_path / "ledger/2026-10-01/state.json").read_text())
    assert state["symbols"]["NIFTYONE"]["first_top5_at"] == now.isoformat()
    ledger.process(ticks)
    events = [json.loads(line) for line in (tmp_path / "ledger/2026-10-01/events.jsonl").read_text().splitlines()]
    assert sum(event["event"] == "first_top5_at" and event["symbol"] == "NIFTYONE" for event in events) == 1
    # No order, qualification, anomaly, or Discord object is exposed by the ledger.
    assert not any("order" in name or "alert" in name for name in dir(ledger))

def test_stale_tick_has_no_rank_event(tmp_path):
    eq, nifty, tokens, ticks = (tmp_path / x for x in ("eq.csv", "nifty.csv", "tokens.csv", "ticks.csv"))
    write_csv(eq, ["SYMBOL", "SERIES"], [{"SYMBOL":"A","SERIES":"EQ"}]); write_csv(nifty,["Symbol"],[]); write_csv(tokens,["Symbol"],[{"Symbol":"A"}])
    now = datetime(2026, 10, 1, 10, tzinfo=IST)
    write_csv(ticks,["symbol","exchange","received_at","last_trade_time","last_price","close"],[{"symbol":"A","exchange":"NSE","received_at":"2026-10-01T09:00:00+05:30","last_trade_time":"2026-10-01T09:00:00+05:30","last_price":"200","close":"100"}])
    result = MarketObservationLedger(tmp_path / "ledger",eq,nifty,tokens,lambda:now).process(ticks)
    assert result["events"] == 1  # stale state transition only
    assert 'first_top' not in (tmp_path / "ledger/2026-10-01/events.jsonl").read_text()

def test_checked_in_service_uses_canonical_checkout_not_digest():
    unit = (Path(__file__).resolve().parents[1] / "systemd/ng-nse-top-gainers-watch.service").read_text()
    assert "/home/charan_sai/tr-assistant-digest" not in unit
    assert "/home/charan_sai/tr-assistant/scripts/ng_nse_top_gainers_watch.py" in unit
    assert "--poll-seconds 15" in unit
