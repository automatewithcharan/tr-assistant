import pandas as pd
import numpy as np

df = pd.read_csv("/home/charan_sai/tr-assistant/results/Data/forward_backtest_trades_2025-05-26_2026-02-06.csv")
print("Total trades:", len(df))
print("Columns:", list(df.columns))

conf = "ConfidencePct"
ret = "TradeReturnPct"
n_days = df["SignalDate"].nunique()

winners = df[df[ret] > 0]
losers = df[df[ret] <= 0]

def cum_ret(subset):
    if len(subset) == 0:
        return 0.0
    daily = subset.groupby("SignalDate")[ret].mean()
    return round(((1 + daily / 100).prod() - 1) * 100, 1)

def show_filter(label, subset):
    if len(subset) == 0:
        print(f"  {label:45s}: 0 trades")
        return
    wr = 100 * subset["Won"].sum() / len(subset)
    avg = subset[ret].mean()
    avg_day = len(subset) / n_days
    cr = cum_ret(subset)
    print(f"  {label:45s}: {len(subset):6d} ({avg_day:5.0f}/day) WR={wr:5.1f}% Avg={avg:+.2f}% Cum={cr:+.1f}%")

has_vol = "VolumeRatio" in df.columns
has_mom = "MomentumScore" in df.columns
has_atv = "AvgTradeValue20" in df.columns
print(f"Has VolumeRatio: {has_vol}, MomentumScore: {has_mom}, AvgTradeValue20: {has_atv}")

print(f"\n=== SINGLE FACTOR FILTERS ===")
for mc in [10, 20, 25, 30, 35, 40, 45]:
    show_filter(f"Conf>={mc}", df[df[conf] >= mc])

if has_vol:
    print()
    for vr in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        show_filter(f"VolRatio>={vr}", df[df["VolumeRatio"] >= vr])
    print(f"\n  Winners avg VolumeRatio: {winners['VolumeRatio'].mean():.2f}")
    print(f"  Losers  avg VolumeRatio: {losers['VolumeRatio'].mean():.2f}")

if has_mom:
    print()
    for ms in [30, 40, 50, 60, 70, 80]:
        show_filter(f"Momentum>={ms}", df[df["MomentumScore"] >= ms])
    print(f"\n  Winners avg Momentum: {winners['MomentumScore'].mean():.1f}")
    print(f"  Losers  avg Momentum: {losers['MomentumScore'].mean():.1f}")

if has_atv:
    print()
    for tv in [0, 500000, 1000000, 5000000, 10000000]:
        show_filter(f"AvgTradeVal>={tv/1e6:.1f}M", df[df["AvgTradeValue20"] >= tv])

# Multi-factor combos
print(f"\n=== MULTI-FACTOR GRID SEARCH ===")
combos = []
conf_vals = [25, 30, 35, 40]
vol_vals = [0, 0.5, 1.0, 1.5, 2.0] if has_vol else [0]
mom_vals = [0, 40, 50, 60, 70] if has_mom else [0]
atv_vals = [0, 500000, 5000000] if has_atv else [0]

for mc in conf_vals:
    for vr in vol_vals:
        for ms in mom_vals:
            for tv in atv_vals:
                mask = df[conf] >= mc
                if vr > 0:
                    mask = mask & (df["VolumeRatio"] >= vr)
                if ms > 0:
                    mask = mask & (df["MomentumScore"] >= ms)
                if tv > 0:
                    mask = mask & (df["AvgTradeValue20"] >= tv)
                subset = df[mask]
                if len(subset) < 30:
                    continue
                wr = 100 * subset["Won"].sum() / len(subset)
                avg = subset[ret].mean()
                avg_day = len(subset) / n_days
                cr = cum_ret(subset)
                combos.append({
                    "MinConf": mc, "MinVol": vr, "MinMom": ms,
                    "MinATV": int(tv), "N": len(subset), "PerDay": round(avg_day, 1),
                    "WR": round(wr, 1), "Avg": round(avg, 2), "Cum": cr
                })

combo_df = pd.DataFrame(combos)
good = combo_df[(combo_df["Cum"] > 0) & (combo_df["PerDay"] >= 3)].nlargest(30, "Cum")
if len(good) > 0:
    print("  Top profitable combos (>=3 trades/day):")
    print(good.to_string(index=False))
else:
    print("  No profitable combos with >=3/day")
    good2 = combo_df[combo_df["Cum"] > 0].nlargest(20, "Cum")
    if len(good2) > 0:
        print("  Top profitable (any count):")
        print(good2.to_string(index=False))
    else:
        print("  All combos:")
        print(combo_df.nlargest(20, "Cum").to_string(index=False))

# Volume surge keyword
print(f"\n=== VOLUME SURGE KEYWORD ===")
vol_surge = df[df["Reason"].str.lower().str.contains("volume surge", na=False)]
show_filter("Has 'volume surge'", vol_surge)
if has_vol:
    show_filter("VolSurge + Conf>=30", vol_surge[vol_surge[conf] >= 30])
    show_filter("VolSurge + Conf>=35", vol_surge[vol_surge[conf] >= 35])
    show_filter("VolSurge + Vol>=2 + Conf>=30", vol_surge[(vol_surge["VolumeRatio"]>=2)&(vol_surge[conf]>=30)])
