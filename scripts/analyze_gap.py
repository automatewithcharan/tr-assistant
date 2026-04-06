"""Show the gap between backward and forward backtest."""
import pandas as pd

df = pd.read_csv("results/Data/forward_backtest_trades_2025-05-26_2026-02-06.csv")

print("=== THE CORE PROBLEM ===")
print("Total Buy Now signals across 58 days:", len(df))
print("Won (gained next day):", int(df["Won"].sum()), "(%.1f%%)" % (100*df["Won"].mean()))
print("Lost (dropped next day):", int((~df["Won"]).sum()), "(%.1f%%)" % (100*(~df["Won"]).mean()))

print("\n=== WHAT HAPPENS ON BAD DAYS ===")
for date in ["2025-08-05", "2025-11-03", "2025-07-31"]:
    day = df[df["SignalDate"] == date]
    if len(day) == 0:
        continue
    wins = int(day["Won"].sum())
    losses = len(day) - wins
    print("\n%s: %d Buy Now signals" % (date, len(day)))
    print("  Won: %d, Lost: %d" % (wins, losses))
    winners = day[day["Won"]]
    losers = day[~day["Won"]]
    if len(winners) > 0:
        syms = ", ".join(winners["Symbol"].head(3).tolist())
        print("  Winners: %s (avg +%.1f%%)" % (syms, winners["TradeReturnPct"].mean()))
    if len(losers) > 0:
        syms = ", ".join(losers["Symbol"].head(5).tolist())
        print("  Losers: %s... (avg %.1f%%)" % (syms, losers["TradeReturnPct"].mean()))

print("\n\n=== RETURN DISTRIBUTION OF OUR 778 PICKS ===")
thresholds = [5, 3, 1, 0]
for t in thresholds:
    count = (df["TradeReturnPct"] > t).sum()
    print("  Gained >%d%%:  %d picks (%.1f%%)" % (t, count, 100*count/len(df)))
for t in [0, -1, -3, -5]:
    count = (df["TradeReturnPct"] < t).sum()
    print("  Lost   <%d%%:  %d picks (%.1f%%)" % (t, count, 100*count/len(df)))

print("\n=== THE GAP ===")
print("Backward test: 'Of stocks that gained 5%+, did we flag them?' -> YES (99.9%)")
big_winners = (df["TradeReturnPct"] > 5).sum()
print("Forward test:  'Of stocks we flagged, did they gain 5%+?'     -> %d/%d (%.1f%%)" % (big_winners, len(df), 100*big_winners/len(df)))

print("\n=== PER-DAY SIGNAL ACCURACY ===")
daily = df.groupby("SignalDate").agg(
    Signals=("Won", "count"),
    Winners=("Won", "sum"),
    AvgRet=("TradeReturnPct", "mean"),
).reset_index()
daily["WinRate"] = (100 * daily["Winners"] / daily["Signals"]).round(1)
daily["TopGainerCandidates"] = (df.groupby("SignalDate")["TradeReturnPct"].apply(lambda x: (x > 3).sum())).values
print("Across 58 trading days:")
print("  Avg signals/day: %.1f" % daily["Signals"].mean())
print("  Avg winners/day: %.1f" % daily["Winners"].mean())
print("  Avg that gained >3%%/day: %.1f" % daily["TopGainerCandidates"].mean())
print("  Days with ZERO >3%% winner: %d" % (daily["TopGainerCandidates"] == 0).sum())
