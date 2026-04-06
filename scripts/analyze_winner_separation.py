"""Analyze: can we predict which Buy Now signals will win vs lose?"""
import pandas as pd
import numpy as np

# Use v1 data (has ALL signals, not just top 3) for full picture
df = pd.read_csv("results/Data/forward_backtest_trades_2025-05-26_2026-02-06.csv")
print("=== FULL SIGNAL POOL (v1 - all 778 trades) ===")
print("Total:", len(df))
print("Winners:", int(df["Won"].sum()), "| Losers:", int((~df["Won"]).sum()))
print()

# THE KEY QUESTION: Within each day's signals, what separates winners from losers?
print("=" * 70)
print("WITHIN EACH DAY: What distinguishes winners from losers?")
print("=" * 70)

winners = df[df["Won"]]
losers = df[~df["Won"]]

for col in ["ConfidencePct", "VolumeRatio", "MomentumScore", "AvgTradeValue20"]:
    w_mean = winners[col].mean()
    l_mean = losers[col].mean()
    diff = w_mean - l_mean
    print("  %s: Winners=%.2f, Losers=%.2f, Diff=%+.2f %s" % (
        col, w_mean, l_mean, diff,
        "***" if abs(diff) > 0.5 else ""
    ))

# Per-day analysis: if we picked the top 3 by each metric, what WR would we get?
print()
print("=" * 70)
print("ORACLE TEST: If we could perfectly pick the best 3 each day")
print("=" * 70)

days = df.groupby("SignalDate")

# Perfect oracle: pick the 3 that actually gained the most
oracle3 = df.sort_values("TradeReturnPct", ascending=False).groupby("SignalDate").head(3)
oracle_wr = 100 * oracle3["Won"].mean()
oracle_avg = oracle3["TradeReturnPct"].mean()
print("Perfect oracle (top 3 actual winners): WR=%.1f%%, avg=%+.2f%%" % (oracle_wr, oracle_avg))

oracle1 = df.sort_values("TradeReturnPct", ascending=False).groupby("SignalDate").head(1)
print("Perfect oracle (top 1 actual winner):  WR=%.1f%%, avg=%+.2f%%" % (
    100*oracle1["Won"].mean(), oracle1["TradeReturnPct"].mean()))

# Now test each ranking metric
print()
print("Our ranking attempts (pick top 3 by each metric):")
for rank_col, ascending in [
    ("ConfidencePct", False),
    ("VolumeRatio", False),
    ("MomentumScore", False),
    ("AvgTradeValue20", False),
]:
    top3 = df.sort_values(rank_col, ascending=ascending).groupby("SignalDate").head(3)
    wr = 100 * top3["Won"].mean()
    avg_r = top3["TradeReturnPct"].mean()
    print("  Rank by %-20s: WR=%.1f%%, avg=%+.3f%% (%d trades)" % (rank_col, wr, avg_r, len(top3)))

# Composite ranking (what v2 uses)
df["CompositeRank"] = (
    df["ConfidencePct"].clip(upper=60) / 60 * 40 +
    df["VolumeRatio"].clip(upper=5) / 5 * 25 +
    df["MomentumScore"].clip(upper=100) / 100 * 20
)
top3_composite = df.sort_values("CompositeRank", ascending=False).groupby("SignalDate").head(3)
print("  Rank by %-20s: WR=%.1f%%, avg=%+.3f%% (%d trades)" % (
    "Composite(v2)", 100*top3_composite["Won"].mean(),
    top3_composite["TradeReturnPct"].mean(), len(top3_composite)))

# THE REAL TEST: Is there ANY signal that can get us to 90%?
print()
print("=" * 70)
print("CAN ANY FILTER GET US TO 90%? (searching exhaustively)")
print("=" * 70)

best_wr = 0
best_filter = ""
best_count = 0

# Try every combination of metrics at various thresholds
for conf in [40, 42, 44, 46, 48, 50]:
    for vol in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]:
        for mom in [75, 80, 85, 90, 95]:
            mask = (df["ConfidencePct"] >= conf) & (df["VolumeRatio"] >= vol) & (df["MomentumScore"] >= mom)
            subset = df[mask]
            if len(subset) >= 5:  # at least 5 trades
                wr = 100 * subset["Won"].mean()
                if wr > best_wr:
                    best_wr = wr
                    best_filter = "Conf>=%d + Vol>=%.1f + Mom>=%d" % (conf, vol, mom)
                    best_count = len(subset)
                if wr >= 80:
                    print("  %s: %d trades, WR=%.1f%%, avg=%+.2f%%" % (
                        "Conf>=%d + Vol>=%.1f + Mom>=%d" % (conf, vol, mom),
                        len(subset), wr, subset["TradeReturnPct"].mean()))

print("\nBest filter found: %s" % best_filter)
print("  Trades: %d, Win Rate: %.1f%%" % (best_count, best_wr))

# What about looking at REASONS/keywords?
print()
print("=" * 70)
print("WHICH SIGNAL REASONS PREDICT WINNERS?")
print("=" * 70)

keywords = [
    "bullish alignment", "volume surge", "above average volume",
    "breakout", "golden cross", "rsi", "macd", "higher highs",
    "ema", "momentum", "oversold", "expansion",
]
for kw in keywords:
    mask = df["Reason"].str.lower().str.contains(kw, na=False)
    sub = df[mask]
    if len(sub) >= 10:
        wr = 100 * sub["Won"].mean()
        marker = " <-- GOOD" if wr >= 55 else ""
        print("  %-25s: %4d trades, WR=%.1f%%, avg=%+.3f%%%s" % (
            kw, len(sub), wr, sub["TradeReturnPct"].mean(), marker))

# The fundamental question: within a day's signals, do winners look different?
print()
print("=" * 70)
print("PER-DAY WINNER vs LOSER SEPARATION")
print("=" * 70)
print("On each day, among our Buy Now signals, can we tell winners from losers?")

separations = []
for date, group in days:
    if len(group) < 4:
        continue
    w = group[group["Won"]]
    l = group[~group["Won"]]
    if len(w) == 0 or len(l) == 0:
        continue
    for col in ["ConfidencePct", "VolumeRatio", "MomentumScore"]:
        sep = w[col].mean() - l[col].mean()
        separations.append({"Date": date, "Metric": col, "Separation": sep})

sep_df = pd.DataFrame(separations)
for col in ["ConfidencePct", "VolumeRatio", "MomentumScore"]:
    subset = sep_df[sep_df["Metric"] == col]
    mean_sep = subset["Separation"].mean()
    pct_positive = 100 * (subset["Separation"] > 0).mean()
    print("  %s:" % col)
    print("    Avg separation (winner - loser): %+.3f" % mean_sep)
    print("    Days where winners have HIGHER value: %.1f%%" % pct_positive)

print()
print("=" * 70)
print("BOTTOM LINE")
print("=" * 70)
print("If our metrics could separate winners from losers, we'd see:")
print("  - Large positive separation values")
print("  - >70% of days where winners rank higher")
print("If it's ~50%, the metrics are essentially RANDOM at distinguishing")
print("winners from losers within the Buy Now pool.")
