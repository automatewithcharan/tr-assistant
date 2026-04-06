"""Find the highest win-rate subsets in forward backtest data."""
import pandas as pd
import numpy as np
import itertools

df = pd.read_csv("results/Data/forward_backtest_trades_2025-05-26_2026-02-06.csv")
print("Total trades:", len(df), "| Baseline WR:", "%.1f%%" % (100*df["Won"].mean()))
print()

# What is the BEST possible win rate we can extract from this data?
print("=" * 70)
print("FILTER SEARCH: What combination gives highest win rate?")
print("=" * 70)

results = []

# Single filters
for conf_min in [40, 42, 44, 45, 46, 48, 50]:
    s = df[df["ConfidencePct"] >= conf_min]
    if len(s) >= 20:
        results.append(("Conf>=%d" % conf_min, len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))

for vol_min in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
    s = df[df["VolumeRatio"] >= vol_min]
    if len(s) >= 20:
        results.append(("Vol>=%.1f" % vol_min, len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))

for mom_min in [70, 75, 80, 85, 90]:
    s = df[df["MomentumScore"] >= mom_min]
    if len(s) >= 20:
        results.append(("Mom>=%d" % mom_min, len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))

# Combo filters
for conf_min in [40, 42, 44, 46, 48]:
    for vol_min in [1.0, 1.5, 2.0, 2.5, 3.0]:
        s = df[(df["ConfidencePct"] >= conf_min) & (df["VolumeRatio"] >= vol_min)]
        if len(s) >= 10:
            results.append(("Conf>=%d + Vol>=%.1f" % (conf_min, vol_min), len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))

for conf_min in [40, 42, 44, 46, 48]:
    for mom_min in [75, 80, 85, 90]:
        s = df[(df["ConfidencePct"] >= conf_min) & (df["MomentumScore"] >= mom_min)]
        if len(s) >= 10:
            results.append(("Conf>=%d + Mom>=%d" % (conf_min, mom_min), len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))

for conf_min in [40, 42, 44, 46]:
    for vol_min in [1.0, 1.5, 2.0]:
        for mom_min in [75, 80, 85]:
            s = df[(df["ConfidencePct"] >= conf_min) & (df["VolumeRatio"] >= vol_min) & (df["MomentumScore"] >= mom_min)]
            if len(s) >= 10:
                results.append(("Conf>=%d + Vol>=%.1f + Mom>=%d" % (conf_min, vol_min, mom_min), len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))

# Check keyword-based filters
if "Reason" in df.columns:
    for kw in ["bullish alignment", "volume surge", "breakout", "above average volume", "golden cross", "rsi"]:
        mask = df["Reason"].str.lower().str.contains(kw, na=False)
        s = df[mask]
        if len(s) >= 10:
            results.append(("Keyword: %s" % kw, len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))
        # combo with high conf
        s2 = df[mask & (df["ConfidencePct"] >= 44)]
        if len(s2) >= 5:
            results.append(("Conf>=44 + kw:%s" % kw, len(s2), len(s2)/58.0, 100*s2["Won"].mean(), s2["TradeReturnPct"].mean()))

# NowAction breakdown 
for action in df["NowAction"].unique():
    s = df[df["NowAction"] == action]
    if len(s) >= 5:
        results.append(("Action=%s" % action, len(s), len(s)/58.0, 100*s["Won"].mean(), s["TradeReturnPct"].mean()))

# Sort by win rate
results.sort(key=lambda x: -x[3])
print("\nTop 30 filters by win rate (min 10 trades):")
print("%-45s %6s %8s %8s %10s" % ("Filter", "Trades", "Per Day", "WinRate", "AvgReturn"))
print("-" * 80)
for name, trades, per_day, wr, avg_ret in results[:30]:
    print("%-45s %6d %8.1f %7.1f%% %+9.3f%%" % (name, trades, per_day, wr, avg_ret))

# What's the theoretical maximum?
print("\n\n" + "=" * 70)
print("THEORETICAL LIMITS")
print("=" * 70)
print("Even the BEST filter combos in our data:")
best_wr = max(r[3] for r in results if r[1] >= 10)
best_filter = [r for r in results if r[3] == best_wr and r[1] >= 10][0]
print("  Highest WR (>=10 trades): %.1f%% with '%s' (%d trades)" % (best_filter[3], best_filter[0], best_filter[1]))

best_wr5 = max(r[3] for r in results if r[1] >= 50)
best_filter5 = [r for r in results if r[3] == best_wr5 and r[1] >= 50][0]
print("  Highest WR (>=50 trades): %.1f%% with '%s' (%d trades)" % (best_filter5[3], best_filter5[0], best_filter5[1]))

best_wr100 = max(r[3] for r in results if r[1] >= 100)
best_filter100 = [r for r in results if r[3] == best_wr100 and r[1] >= 100][0]
print("  Highest WR (>=100 trades): %.1f%% with '%s' (%d trades)" % (best_filter100[3], best_filter100[0], best_filter100[1]))

# What about just picking the TOP 1 signal per day?
print("\n=== WHAT IF WE ONLY PICK THE TOP 1 PER DAY? ===")
for rank_by in ["ConfidencePct", "VolumeRatio", "MomentumScore"]:
    top1 = df.sort_values(rank_by, ascending=False).groupby("SignalDate").first().reset_index()
    top3 = df.sort_values(rank_by, ascending=False).groupby("SignalDate").head(3).reset_index()
    top5 = df.sort_values(rank_by, ascending=False).groupby("SignalDate").head(5).reset_index()
    print("Rank by %s:" % rank_by)
    print("  Top 1/day: %d trades, WR %.1f%%, avg %+.3f%%" % (len(top1), 100*top1["Won"].mean(), top1["TradeReturnPct"].mean()))
    print("  Top 3/day: %d trades, WR %.1f%%, avg %+.3f%%" % (len(top3), 100*top3["Won"].mean(), top3["TradeReturnPct"].mean()))
    print("  Top 5/day: %d trades, WR %.1f%%, avg %+.3f%%" % (len(top5), 100*top5["Won"].mean(), top5["TradeReturnPct"].mean()))

# Composite score ranking
df["CompositeScore"] = df["ConfidencePct"] * 0.5 + df["VolumeRatio"] * 10 + df["MomentumScore"] * 0.3
for n in [1, 2, 3, 5]:
    topn = df.sort_values("CompositeScore", ascending=False).groupby("SignalDate").head(n).reset_index()
    print("Top %d/day by composite: %d trades, WR %.1f%%, avg %+.3f%%" % (n, len(topn), 100*topn["Won"].mean(), topn["TradeReturnPct"].mean()))
