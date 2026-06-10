from pkscreener.classes.KiteOrderManager import KiteOrderManager, TrackedPosition


def test_sell_order_respects_auto_trade_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("KITE_AUTO_TRADE", "0")
    manager = KiteOrderManager(output_dir=str(tmp_path))
    manager._positions["MSTCLTD"] = TrackedPosition(
        symbol="MSTCLTD",
        qty=1,
        buy_price=558.0,
        product_type="CNC",
        holding_horizon="swing",
    )

    result = manager.place_sell_order("MSTCLTD", qty=1, price=549.0, reason="test")

    assert result.success is False
    assert result.status == "DISABLED"
    assert result.side == "SELL"
