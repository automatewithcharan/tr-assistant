import pandas as pd

from pkscreener.classes.RiskGuardrails import LiveTradingGuardrails


def test_guardrail_pauses_after_daily_profit_target(tmp_path):
    trades_path = tmp_path / "livetrade_main_trades_latest.csv"
    pd.DataFrame(
        [
            {
                "Status": "CLOSED",
                "ExitDate": "2026-05-31",
                "TradeId": 1,
                "RealizedPnL": 1200.0,
            }
        ]
    ).to_csv(trades_path, index=False)

    guardrails = LiveTradingGuardrails(
        account_name="main",
        max_daily_profit_amount=1000.0,
    )
    guardrails.output_dir = str(tmp_path)
    guardrails.today_text = "2026-05-31"

    status = guardrails.evaluate()

    assert status.enabled
    assert status.buy_paused
    assert status.daily_realized_pnl == 1200.0
    assert "profit target" in status.reasons[0]


def test_guardrail_does_not_pause_before_daily_profit_target(tmp_path):
    trades_path = tmp_path / "livetrade_main_trades_latest.csv"
    pd.DataFrame(
        [
            {
                "Status": "CLOSED",
                "ExitDate": "2026-05-31",
                "TradeId": 1,
                "RealizedPnL": 800.0,
            }
        ]
    ).to_csv(trades_path, index=False)

    guardrails = LiveTradingGuardrails(
        account_name="main",
        max_daily_profit_amount=1000.0,
    )
    guardrails.output_dir = str(tmp_path)
    guardrails.today_text = "2026-05-31"

    status = guardrails.evaluate()

    assert status.enabled
    assert not status.buy_paused
    assert status.daily_realized_pnl == 800.0


def test_guardrail_falls_back_to_autotrade_execution_journal(tmp_path):
    journal_path = tmp_path / "autotrade_event_journal.csv"
    pd.DataFrame(
        [
            {
                "timestamp": "2026-07-10T09:59:47+05:30",
                "event": "order_result",
                "symbol": "DJML",
                "side": "BUY",
                "qty": 347,
                "price": 117.977695,
                "status": "COMPLETE",
            },
            {
                "timestamp": "2026-07-10T09:59:47+05:30",
                "event": "entry_result",
                "symbol": "DJML",
                "side": "BUY",
                "qty": 347,
                "price": 117.977695,
                "status": "COMPLETE",
            },
            {
                "timestamp": "2026-07-10T14:40:56+05:30",
                "event": "order_result",
                "symbol": "DJML",
                "side": "SELL",
                "qty": 347,
                "price": 114.79,
                "status": "COMPLETE",
            },
            {
                "timestamp": "2026-07-10T14:40:56+05:30",
                "event": "protective_stop_filled",
                "symbol": "DJML",
                "side": "SELL",
                "qty": 347,
                "price": 114.79,
                "status": "COMPLETE",
            },
        ]
    ).to_csv(journal_path, index=False)

    guardrails = LiveTradingGuardrails(
        account_name="main",
        max_daily_loss_amount=1000.0,
        max_consecutive_losses=1,
    )
    guardrails.output_dir = str(tmp_path)
    guardrails.today_text = "2026-07-10"

    status = guardrails.evaluate()

    assert status.buy_paused
    assert status.daily_realized_pnl == -1106.13
    assert status.consecutive_losses == 1
    assert any("loss limit" in reason for reason in status.reasons)
    assert any("consecutive losing" in reason for reason in status.reasons)
