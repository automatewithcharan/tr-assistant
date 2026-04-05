import pandas as pd
import numpy as np

df = pd.read_csv("/home/charan_sai/tr-assistant/results/Data/forward_backtest_trades_2025-05-26_2026-02-06.csv")
print("Total trades:", len(df))

conf = "ConfidencePct"
ret = "TradeReturnPct"

winners = df[df[ret] > 0]
losers = df[df[ret] <= 0]

print("\n=== CONFIDENCE DISTRIBUTION ===")
for label, subset in [("Winners", winners), ("Losers", losers), ("All", df)]:
    print(f"  {label} ({len(subset)}): mean={subset[conf].mean():.1f}, "
          f"median={subset[conf].median():.1f}, "
          f"p25={subset[conf].quantile(0.25):.1f}, "
          f"p75={subset[conf].quantile(0.75):.1f}")

print("\n=== WIN RATE BY CONFIDENCE ===")
bins = [0, 15, 20, 25, 30, 35, 40, 50, 60, 100]
df["ConfBucket"] = pd.cut(df[conf], bins=bins)
for bucket in df["ConfBucket"].cat.categories:
    subset = df[df["ConfBucket"] == bucket]
    if len(subset) > 0:
        wr = 100 * subset["Won"].sum() / len(subset)
        avg_ret = subset[ret].mean()
        avg_day = len(subset) / df["SignalDate"].nunique()
        print(f"  Conf {bucket}: {len(subset):6d} trades ({avg_day:.0f}/day), WR={wr:5.1f}%, AvgRet={avg_ret:+.2f}%")

print("\n=== BY NOWACTION ===")
for action in df["NowAction"].unique():
    subset = df[df["NowAction"] == action]
    if len(subset) > 0:
        wr = 100 * subset["Won"].sum() / len(subset)
        avg_ret = subset[ret].mean()
        print(f"  {action}: {len(subset):6d} trades, WR={wr:5.1f}%, AvgRet={avg_ret:+.2f}%")

print("\n=== REASON KEYWORDS: WINNERS vs LOSERS ===")
keywords = ["bullish", "bearish", "oversold", "overbought", "crossover",
            "volume surge", "above average", "higher highs", "lower highs",
            "trailing stop", "golden cross", "death cross",
            "bullish alignment", "bearish alignment", "bullish crossover",
            "bearish crossover", "histogram increasing", "histogram decreasing",
            "higher lows", "lower lows"]
for kw in keywords:
    w_has = winners["Reason"].str.lower().str.contains(kw, na=False).sum()
    l_has = losers["Reason"].str.lower().str.contains(kw, na=False).sum()
    total = w_has + l_has
    if total > 50:
        w_wr = 100 * w_has / total if total > 0 else 0
        print(f"  '{kw}': {total:5d} occur, winrate={w_wr:.1f}%, in_winners={w_has}, in_losers={l_has}")

print("\n=== SIMULATED CONFIDENCE FILTERS ===")
for min_conf in [15, 20, 25, 30, 35, 40, 45, 50]:
    subset = df[df[conf] >= min_conf]
    if len(subset) > 0:
        wr = 100 * subset["Won"].sum() / len(subset)
        avg_ret = subset[ret].mean()
        avg_day = len(subset) / df["SignalDate"].nunique()
        total_ret = (1 + subset.groupby("SignalDate")[ret].mean() / 100).prod()
        total_pct = round((total_ret - 1) * 100, 1)
        print(f"  Conf>={min_conf:2d}: {len(subset):6d} trades ({avg_day:6.0f}/day), WR={wr:5.1f}%, AvgRet={avg_ret:+.2f}%, CumRet={total_pct:+.1f}%")

# Multi-factor filter: confidence + volume + bullish reasons
print("\n=== MULTI-FACTOR FILTERS ===")
for min_conf in [25, 30, 35]:
    for require_bullish_reason in [False, True]:
        subset = df[df[conf] >= min_conf].copy()
        label = f"Conf>={min_conf}"
        if require_bullish_reason:
            reason_lower = subset["Reason"].str.lower()
            bullish_mask = (
                reason_lower.str.contains("bullish crossover", na=False) |
                reason_lower.str.contains("golden cross", na=False) |
                reason_lower.str.contains("volume surge", na=False) |
                reason_lower.str.contains("higher highs", na=False) |
                reason_lower.str.contains("oversold", na=False) |
                reason_lower.str.contains("bullish alignment", na=False)
            )
            subset = subset[bullish_mask]
            label += "+bullish"
        if len(subset) > 0:
            wr = 100 * subset["Won"].sum() / len(subset)
            avg_ret = subset[ret].mean()
            avg_day = len(subset) / df["SignalDate"].nunique()
            total_ret = (1 + subset.groupby("SignalDate")[ret].mean() / 100).prod()
            total_pct = round((total_ret - 1) * 100, 1)
            print(f"  {label:25s}: {len(subset):6d} ({avg_day:5.0f}/day), WR={wr:5.1f}%, AvgRet={avg_ret:+.2f}%, Cum={total_pct:+.1f}%")

print("\n=== TOP 20 BEST TRADES ===")
top = df.nlargest(20, ret)
print(top[["SignalDate","Symbol",conf,"NowAction",ret,"Reason"]].to_string(index=False))
