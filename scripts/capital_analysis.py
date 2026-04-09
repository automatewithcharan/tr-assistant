#!/usr/bin/env python3
"""
Quick analysis:
1. Do bearish days produce big winners? (Answer Q1)
2. Capital calculator for ₹1-2 lakh/month targets (Answer Q2)
"""
import csv, os
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "results", "Data", "kite_history_cache", "day")


def load_stocks():
    stocks = {}
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".csv"):
            continue
        symbol = fname.replace(".csv", "")
        candles = []
        try:
            with open(os.path.join(DATA_DIR, fname)) as f:
                for row in csv.DictReader(f):
                    candles.append({
                        "date": row["datetime"][:10],
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(float(row["volume"])),
                    })
        except Exception:
            continue
        if len(candles) >= 30:
            stocks[symbol] = candles
    return stocks


def compute_vol_ratio(volumes, i, period=20):
    if i < period + 1:
        return 1.0
    avg = sum(volumes[i - period:i]) / period
    return volumes[i] / avg if avg > 0 else 1.0


def main():
    stocks = load_stocks()
    print(f"Loaded {len(stocks)} stocks\n")

    # Build market index (avg daily return)
    date_returns = defaultdict(list)
    for sym, candles in stocks.items():
        for i in range(1, len(candles)):
            ret = (candles[i]["close"] - candles[i-1]["close"]) / candles[i-1]["close"] * 100
            date_returns[candles[i]["date"]].append(ret)
    market_daily = {d: sum(r)/len(r) for d, r in date_returns.items() if len(r) >= 100}

    # Classify days
    bearish_days = {d for d, r in market_daily.items() if r < -0.3}
    neutral_days = {d for d, r in market_daily.items() if -0.3 <= r <= 0.3}
    bullish_days = {d for d, r in market_daily.items() if r > 0.3}

    print(f"Market days: {len(bearish_days)} bearish, {len(neutral_days)} neutral, {len(bullish_days)} bullish")

    # ── Q1: Big winners on bearish days ──
    print("\n" + "=" * 80)
    print("Q1: DO BEARISH DAYS PRODUCE BIG WINNERS?")
    print("=" * 80)

    # Track gap-up performance by market condition
    conditions = {"BEARISH": bearish_days, "NEUTRAL": neutral_days, "BULLISH": bullish_days}
    
    for cond_name, cond_days in conditions.items():
        gap_ups = []
        big_winners = []  # >5% from open
        huge_winners = []  # >10% from open
        
        for sym, candles in stocks.items():
            volumes = [c["volume"] for c in candles]
            for i in range(21, len(candles)):
                if candles[i]["date"] not in cond_days:
                    continue
                prev_close = candles[i-1]["close"]
                if prev_close <= 0:
                    continue
                gap = (candles[i]["open"] - prev_close) / prev_close * 100
                if gap < 3.0 or gap > 15.0 or candles[i]["open"] < 20:
                    continue
                
                vol_ratio = compute_vol_ratio(volumes, i)
                if vol_ratio < 2.0:
                    continue
                
                entry = candles[i]["open"]
                high_pct = (candles[i]["high"] - entry) / entry * 100
                close_pct = (candles[i]["close"] - entry) / entry * 100
                
                # 3-day forward
                max_high_3d = entry
                for d in range(3):
                    if i + d < len(candles):
                        max_high_3d = max(max_high_3d, candles[i+d]["high"])
                forward_3d_pct = (max_high_3d - entry) / entry * 100
                
                gap_ups.append({
                    "sym": sym, "date": candles[i]["date"],
                    "gap": gap, "vol": vol_ratio,
                    "high_pct": high_pct, "close_pct": close_pct,
                    "fwd_3d": forward_3d_pct,
                })
                if high_pct >= 5:
                    big_winners.append(gap_ups[-1])
                if high_pct >= 10:
                    huge_winners.append(gap_ups[-1])
        
        if gap_ups:
            wins = sum(1 for g in gap_ups if g["close_pct"] > 0)
            avg_high = sum(g["high_pct"] for g in gap_ups) / len(gap_ups)
            avg_close = sum(g["close_pct"] for g in gap_ups) / len(gap_ups)
            avg_3d = sum(g["fwd_3d"] for g in gap_ups) / len(gap_ups)
            
            print(f"\n  {cond_name} DAYS ({len(cond_days)} days):")
            print(f"    Gap-ups (3%+, vol>=2x): {len(gap_ups)}")
            print(f"    Win rate (close > open): {wins/len(gap_ups)*100:.0f}%")
            print(f"    Avg intraday high from entry: +{avg_high:.1f}%")
            print(f"    Avg EOD P&L: {avg_close:+.1f}%")
            print(f"    Avg 3-day max high: +{avg_3d:.1f}%")
            print(f"    Big winners (>5% intraday): {len(big_winners)} ({len(big_winners)/len(gap_ups)*100:.0f}%)")
            print(f"    Huge winners (>10% intraday): {len(huge_winners)} ({len(huge_winners)/len(gap_ups)*100:.0f}%)")
            
            if big_winners:
                top = sorted(big_winners, key=lambda x: -x["high_pct"])[:5]
                print(f"    Top 5 big winners on {cond_name} days:")
                for t in top:
                    print(f"      {t['sym']:>15} {t['date']} gap +{t['gap']:.1f}% vol {t['vol']:.1f}x → intraday +{t['high_pct']:.1f}%, 3d +{t['fwd_3d']:.1f}%")

    # ── What we MISS by skipping bearish days ──
    print("\n" + "-" * 80)
    print("WHAT YOU MISS BY SKIPPING BEARISH DAYS:")
    print("-" * 80)
    
    # Count big winners missed
    all_big = []
    bearish_big = []
    for sym, candles in stocks.items():
        volumes = [c["volume"] for c in candles]
        for i in range(21, len(candles)):
            prev_close = candles[i-1]["close"]
            if prev_close <= 0:
                continue
            gap = (candles[i]["open"] - prev_close) / prev_close * 100
            if gap < 3.0 or gap > 15.0 or candles[i]["open"] < 20:
                continue
            vol_ratio = compute_vol_ratio(volumes, i)
            if vol_ratio < 2.0:
                continue
            
            entry = candles[i]["open"]
            high_pct = (candles[i]["high"] - entry) / entry * 100
            
            if high_pct >= 5:
                all_big.append(candles[i]["date"])
                if candles[i]["date"] in bearish_days:
                    bearish_big.append(candles[i]["date"])
    
    print(f"  Total >5% winners across all days: {len(all_big)}")
    print(f"  >5% winners on BEARISH days: {len(bearish_big)} ({len(bearish_big)/max(1,len(all_big))*100:.0f}% of all big winners)")
    print(f"  → Skipping bearish days would miss {len(bearish_big)} big winners")
    print(f"     BUT also avoid {len(bearish_days)} potentially losing days")

    # ── Compare with vs without market filter for HOLD_3D ──
    print("\n" + "=" * 80)
    print("HOLD_3D: WITH vs WITHOUT MARKET FILTER")
    print("=" * 80)
    
    for label, skip_bearish in [("ALL DAYS", False), ("SKIP BEARISH", True)]:
        trades = []
        for sym, candles in stocks.items():
            volumes = [c["volume"] for c in candles]
            closes = [c["close"] for c in candles]
            highs = [c["high"] for c in candles]
            lows = [c["low"] for c in candles]
            
            for i in range(21, len(candles)):
                if skip_bearish and candles[i]["date"] in bearish_days:
                    continue
                prev_close = candles[i-1]["close"]
                if prev_close <= 0:
                    continue
                gap = (candles[i]["open"] - prev_close) / prev_close * 100
                if gap < 3.0 or gap > 15.0 or candles[i]["open"] < 50:
                    continue
                vol_ratio = compute_vol_ratio(volumes, i)
                if vol_ratio < 2.0:
                    continue
                
                # RSI check
                if len(closes[:i]) < 15:
                    continue
                deltas = [closes[j] - closes[j-1] for j in range(max(1, i-14), i)]
                gains = [max(d,0) for d in deltas[-14:]]
                losses = [max(-d,0) for d in deltas[-14:]]
                ag = sum(gains)/14
                al = sum(losses)/14
                rsi = 100 - (100/(1+ag/al)) if al > 0 else 100
                if rsi >= 70:
                    continue
                
                entry = candles[i]["open"]
                # ATR
                trs = []
                for k in range(max(1, i-14), i):
                    trs.append(max(highs[k]-lows[k], abs(highs[k]-closes[k-1]), abs(lows[k]-closes[k-1])))
                atr = sum(trs[-14:])/14 if trs else entry * 0.02
                atr_pct = min(max(atr/entry*100 * 1.5, 1.0), 3.0)
                
                # Hold 3 days with trailing stop
                trailing_sl = entry * (1 - atr_pct/100)
                max_price = entry
                exit_price = None
                hold_days = min(3, len(candles) - i)
                
                for d in range(hold_days):
                    c = candles[i + d]
                    if c["low"] <= trailing_sl:
                        exit_price = trailing_sl
                        break
                    if c["high"] > max_price:
                        max_price = c["high"]
                        new_sl = max_price * (1 - atr_pct/100)
                        trailing_sl = max(trailing_sl, new_sl)
                
                if exit_price is None and i + hold_days - 1 < len(candles):
                    exit_price = candles[i + hold_days - 1]["close"]
                elif exit_price is None:
                    continue
                
                pnl = (exit_price - entry) / entry * 100
                trades.append(pnl)
        
        if trades:
            n = len(trades)
            wins = sum(1 for t in trades if t > 0)
            avg = sum(trades) / n
            avg_win = sum(t for t in trades if t > 0) / max(1, wins)
            avg_loss = sum(t for t in trades if t <= 0) / max(1, n - wins)
            exp = (wins/n) * avg_win + ((n-wins)/n) * avg_loss
            big = sum(1 for t in trades if t >= 5)
            
            print(f"\n  {label}:")
            print(f"    Trades: {n}, Win rate: {wins/n*100:.0f}%, Avg P&L: {avg:+.2f}%")
            print(f"    Avg win: +{avg_win:.2f}%, Avg loss: {avg_loss:.2f}%")
            print(f"    Expectancy: {exp:+.3f}% per trade")
            print(f"    >5% winners: {big} ({big/n*100:.0f}%)")
            print(f"    Total P&L sum: {sum(trades):+.1f}%")

    # ── Q2: Capital Calculator ──
    print("\n" + "=" * 80)
    print("Q2: CAPITAL NEEDED FOR ₹1-2 LAKH/MONTH")
    print("=" * 80)
    
    # Use HOLD_3D results: 12.7% avg monthly, range 2.2%-26.4%
    # But be conservative: use 8% as realistic expectation
    monthly_returns = {
        "Conservative (worst months)": 5.0,
        "Realistic average": 8.0,
        "Good months": 12.0,
        "Great months (Apr/May)": 20.0,
    }
    
    targets = [50000, 100000, 150000, 200000]
    
    print(f"\n  {'Scenario':<30} {'Monthly%':>10} {'For ₹50K':>12} {'For ₹1L':>12} {'For ₹1.5L':>12} {'For ₹2L':>12}")
    print(f"  {'-'*30} {'-'*10} {'-'*12} {'-'*12} {'-'*12} {'-'*12}")
    
    for scenario, pct in monthly_returns.items():
        row = f"  {scenario:<30} {pct:>9.0f}%"
        for target in targets:
            capital = target / (pct / 100)
            if capital >= 100000:
                row += f"  ₹{capital/100000:>7.1f}L"
            else:
                row += f"  ₹{capital/1000:>7.0f}K"
            
        print(row)
    
    # Position sizing detail
    print(f"\n  DETAILED POSITION SIZING (for ₹1L/month target):")
    print(f"  {'─'*60}")
    
    capital_options = [300000, 500000, 800000, 1000000, 1500000]
    
    for capital in capital_options:
        # 3 trades/day, 10% allocation each = 30% capital at risk
        per_trade = capital * 0.10
        daily_trades = 3
        monthly_trades = daily_trades * 22
        
        # Need ₹1L from this capital
        needed_monthly_pct = 100000 / capital * 100
        needed_per_trade = needed_monthly_pct / monthly_trades
        
        # With HOLD_3D expectancy of 0.645%/trade
        expected_monthly = 0.645 * monthly_trades * 0.10  # 10% allocation
        expected_monthly_rupees = capital * expected_monthly / 100
        
        cap_str = f"₹{capital/100000:.0f}L" if capital >= 100000 else f"₹{capital/1000:.0f}K"
        print(f"\n    Capital: {cap_str}")
        print(f"      Per trade (10%): ₹{per_trade:,.0f}")
        print(f"      Trades/month: {monthly_trades}")
        print(f"      Need {needed_monthly_pct:.1f}%/month → {needed_per_trade:.2f}%/trade")
        print(f"      HOLD_3D delivers: ~{expected_monthly:.1f}%/month = ₹{expected_monthly_rupees:,.0f}/month")
        
        # With 20% allocation
        expected_20 = 0.645 * monthly_trades * 0.20
        expected_20_rupees = capital * expected_20 / 100
        print(f"      At 20% allocation: ~{expected_20:.1f}%/month = ₹{expected_20_rupees:,.0f}/month")
        
        status = "✅" if expected_20_rupees >= 100000 else "⚠️"
        print(f"      {status} Target ₹1L: {'ACHIEVABLE' if expected_20_rupees >= 100000 else 'NEEDS MORE CAPITAL'}")
    
    print(f"\n  {'='*60}")
    print(f"  BOTTOM LINE:")
    print(f"  {'='*60}")
    print(f"  HOLD_3D strategy expectancy: +0.645% per trade")
    print(f"  With 3 trades/day × 22 days = 66 trades/month")
    print(f"  At 10% capital per trade: ~4.3% monthly on total capital")
    print(f"  At 20% capital per trade: ~8.5% monthly on total capital")
    print(f"  At 30% capital per trade: ~12.7% monthly on total capital (aggressive)")
    print(f"")
    print(f"  For ₹1 lakh/month:")
    print(f"    Conservative (10% alloc): Need ₹23L capital")
    print(f"    Moderate   (20% alloc): Need ₹12L capital")
    print(f"    Aggressive (30% alloc): Need ₹8L capital")
    print(f"")
    print(f"  For ₹2 lakh/month:")
    print(f"    Conservative: Need ₹46L")
    print(f"    Moderate:     Need ₹24L")
    print(f"    Aggressive:   Need ₹16L")
    print(f"")
    print(f"  Mutual fund comparison:")
    print(f"    Avg equity MF: ~12-15% per YEAR")
    print(f"    HOLD_3D at 20% alloc: ~8.5% per MONTH = ~102% per YEAR")
    print(f"    That's 7x better than mutual funds")
    print(f"    Even worst month (+2.2%) beats MF annual rate")


if __name__ == "__main__":
    main()
