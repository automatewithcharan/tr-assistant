from pkscreener.classes.ActionableReport import ActionableReportRunner


def _make_runner() -> ActionableReportRunner:
    runner = ActionableReportRunner.__new__(ActionableReportRunner)
    runner.min_confidence = 15.0
    runner.buy_now_min_volume_ratio = 1.3
    runner.early_momentum_buy_min_score = 90.0
    runner.early_momentum_buy_max_gap_pct = 1.10
    runner.early_momentum_buy_min_volume_ratio = 1.05
    runner.early_momentum_buy_max_change_pct = 7.0
    runner.early_momentum_buy_max_rsi = 75.0
    runner.stretched_momentum_buy_min_change_pct = 8.0
    runner.stretched_momentum_buy_min_rsi = 78.0
    return runner


def test_promotes_early_high_quality_momentum_watch():
    runner = _make_runner()

    action, checks = runner._promote_momentum_watch(
        now_action="Watch for Buy",
        quality_checks="confidence ok, near trigger, liquidity ok, low volume, kalman quality ok",
        confidence=42.3,
        volume_ratio=1.08,
        momentum_score=95.0,
        entry_gap_pct=0.95,
        change_pct=4.8,
        liquidity_ok=True,
        kalman_quality=72.0,
        kalman_trend="up",
        rsi=68.0,
    )

    assert action == "Buy Now"
    assert "early momentum promotion" in checks


def test_does_not_promote_early_stretched_momentum_watch():
    runner = _make_runner()

    action, checks = runner._promote_momentum_watch(
        now_action="Watch for Buy",
        quality_checks="confidence ok, near trigger, liquidity ok, low volume, kalman quality ok",
        confidence=42.3,
        volume_ratio=1.08,
        momentum_score=95.0,
        entry_gap_pct=0.95,
        change_pct=8.4,
        liquidity_ok=True,
        kalman_quality=72.0,
        kalman_trend="up",
        rsi=68.0,
    )

    assert action == "Watch for Buy"
    assert "promotion" not in checks


def test_regular_momentum_promotion_still_works():
    runner = _make_runner()

    action, checks = runner._promote_momentum_watch(
        now_action="Watch for Buy",
        quality_checks="confidence ok, near trigger, liquidity ok, low volume, kalman quality ok",
        confidence=42.3,
        volume_ratio=1.92,
        momentum_score=95.0,
        entry_gap_pct=0.54,
        change_pct=10.0,
        liquidity_ok=True,
        kalman_quality=75.0,
        kalman_trend="up",
        rsi=70.0,
    )

    assert action == "Buy Now"
    assert "momentum promotion" in checks


def test_stretched_regular_momentum_promotion_is_blocked():
    runner = _make_runner()

    action, checks = runner._promote_momentum_watch(
        now_action="Watch for Buy",
        quality_checks="confidence ok, near trigger, liquidity ok, volume ok, strong confidence, strong volume, kalman quality ok",
        confidence=42.1,
        volume_ratio=8.42,
        momentum_score=50.0,
        entry_gap_pct=0.31,
        change_pct=8.71,
        liquidity_ok=True,
        kalman_quality=75.0,
        kalman_trend="up",
        rsi=79.8,
    )

    assert action == "Watch for Buy"
    assert "promotion" not in checks
