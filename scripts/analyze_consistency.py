"""Analyze forward backtest consistency and reliability."""
import pandas as pd
import numpy as np
import sys

trades_file = sys.argv[1] if len(sys.argv) > 1 else "results/Data/forward_backtest_trades_2025-05-26_2026-02-06.csv"
summary_file = sys.argv[2] if len(sys.argv) > 2 else "results/Data/forward_backtest_summary_2025-05-26_2026-02-06.csv"

df = pd.read_csv(trades_file)
sm = pd.read_csv(summary_file)

print("=== DAY-BY-DAY CONSISTENCY ===")
sm_active = sm[sm["TradesExecuted"] > 0].reset_index(drop=True)
active_days = len(sm_active)
profitable_days = (sm_active["AvgReturnPct"] > 0).sum()
losing_days = active_days - profitable_days
print(f"Total active days: {active_days}")
print(f"Profitable days: {profitable_days}/{active_days} ({100*profitable_days/active_days:.1f}%)")
print(f"Losing days: {losing_days}/{active_days} ({100*losing_days/active_days:.1f}%)")
print(f"Best day:  {sm_active.loc[sm_active['AvgReturnPct'].idxmax(), 'SignalDate']} avg {sm_active['AvgReturnPct'].max():+.2f}%")
print(f"Worst day: {sm_active.loc[sm_active['AvgReturnPct'].idxmin(), 'SignalDate']} avg {sm_active['AvgReturnPct'].min():+.2f}%")

# Consecutive losing streaks
streaks = []
current = 0
for _, row in sm_active.iterrows():
    if row["AvgReturnPct"] <= 0:
        current += 1
    else:
        if current > 0:
            streaks.append(current)
        current = 0
if current > 0:
    streaks.append(current)

wstreaks = []
current = 0
for _, row in sm_active.iterrows():
    if row["AvgReturnPct"] > 0:
        current += 1
    else:
        if current > 0:
            wstreaks.append(current)
        current = 0
if current > 0:
    wstreaks.append(current)

print(f"Max consecutive losing days: {max(streaks) if streaks else 0}")
print(f"Max consecutive winning days: {max(wstreaks) if wstreaks else 0}")
print(f"Losing streaks (sorted): {sorted(streaks, reverse=True)[:8]}")

print(f"\n=== MONTHLY CONSISTENCY ===")
df["Month"] = pd.to_datetime(df["SignalDate"]).dt.strftime("%Y-%m")
monthly = df.groupby("Month").agg(
    Trades=("TradeReturnPct", "count"),
    WinRate=("Won", "mean"),
    AvgReturn=("TradeReturnPct", "mean"),
    TotalReturn=("TradeReturnPct", "sum"),
    StdDev=("TradeReturnPct", "std"),
).reset_index()
monthly["WinRate"] = (monthly["WinRate"] * 100).round(1)
monthly["AvgReturn"] = monthly["AvgReturn"].round(3)
monthly["TotalReturn"] = monthly["TotalReturn"].round(1)
monthly["StdDev"] = monthly["StdDev"].round(2)
print(monthly.to_string(index=False))

positive_months = (monthly["AvgReturn"] > 0).sum()
negative_months = len(monthly) - positive_months
print(f"\nPositive months: {positive_months}/{len(monthly)}")
print(f"Negative months: {negative_months}/{len(monthly)}")

# Portfolio equity curve and drawdowns
portfolio = 100000.0
equity = [portfolio]
dates = []
for _, row in sm_active.iterrows():
    pct = row["AvgReturnPct"] / 100.0
    portfolio *= (1 + pct)
    equity.append(portfolio)
    dates.append(row["SignalDate"])

peak = equity[0]
max_dd = 0
dd_start = ""
dd_end = ""
current_peak_idx = 0
for i, val in enumerate(equity):
    if val > peak:
        peak = val
        current_peak_idx = i
    dd = (peak - val) / peak * 100
    if dd > max_dd:
        max_dd = dd
        dd_end = dates[i-1] if i > 0 and i-1 < len(dates) else "start"

print(f"\n=== RISK METRICS ===")
print(f"Max drawdown: {max_dd:.1f}%")
print(f"Final portfolio: Rs {portfolio:,.0f}")

daily_returns = sm_active["AvgReturnPct"].values
if len(daily_returns) > 1:
    sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
    sortino_denom = np.sqrt(np.mean(np.minimum(daily_returns, 0)**2)) * np.sqrt(252)
    sortino = (daily_returns.mean() * np.sqrt(252)) / sortino_denom if sortino_denom > 0 else 0
    print(f"Annualized Sharpe ratio: {sharpe:.2f}")
    print(f"Annualized Sortino ratio: {sortino:.2f}")
    print(f"Daily return std dev: {daily_returns.std():.3f}%")
    print(f"Daily return mean: {daily_returns.mean():.3f}%")

# Rolling 2-week performance
print(f"\n=== ROLLING 2-WEEK WINDOWS (every 2-week block) ===")
window = 10  # ~2 weeks
for i in range(0, len(sm_active), window):
    block = sm_active.iloc[i:i+window]
    if len(block) < 3:
        continue
    block_trades = df[df["SignalDate"].isin(block["SignalDate"].values)]
    wr = 100 * block_trades["Won"].mean() if len(block_trades) > 0 else 0
    avg_r = block_trades["TradeReturnPct"].mean() if len(block_trades) > 0 else 0
    d1 = block.iloc[0]["SignalDate"]
    d2 = block.iloc[-1]["SignalDate"]
    t = len(block_trades)
    print(f"  {d1} to {d2}: {t:4d} trades, WR {wr:.1f}%, avg {avg_r:+.3f}%")

print(f"\n=== MARKET REGIME ANALYSIS ===")
# Look at how performance varies by market conditions
df["DayReturn"] = df["TradeReturnPct"]
day_avg = df.groupby("SignalDate").agg(
    AvgRet=("TradeReturnPct", "mean"),
    Count=("TradeReturnPct", "count"),
    WinRate=("Won", "mean"),
).reset_index()
# "Bad market days" = days where avg return of all signals is very negative
bad_days = day_avg[day_avg["AvgRet"] < -1.0]
good_days = day_avg[day_avg["AvgRet"] > 1.0]
neutral_days = day_avg[(day_avg["AvgRet"] >= -1.0) & (day_avg["AvgRet"] <= 1.0)]
print(f"Bad market days (avg < -1%):    {len(bad_days)} days, avg WR {100*bad_days['WinRate'].mean():.1f}%, avg ret {bad_days['AvgRet'].mean():+.2f}%")
print(f"Neutral days (-1% to +1%):      {len(neutral_days)} days, avg WR {100*neutral_days['WinRate'].mean():.1f}%, avg ret {neutral_days['AvgRet'].mean():+.2f}%")
print(f"Good market days (avg > +1%):   {len(good_days)} days, avg WR {100*good_days['WinRate'].mean():.1f}%, avg ret {good_days['AvgRet'].mean():+.2f}%")

# Confidence breakdown
print(f"\n=== CONFIDENCE BREAKDOWN ===")
for lo, hi in [(40, 45), (45, 50), (50, 100)]:
    subset = df[(df["ConfidencePct"] >= lo) & (df["ConfidencePct"] < hi)]
    if len(subset) > 0:
        print(f"  Conf {lo}-{hi}: {len(subset)} trades, WR {100*subset['Won'].mean():.1f}%, avg {subset['TradeReturnPct'].mean():+.3f}%")
