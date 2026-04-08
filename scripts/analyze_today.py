import pandas as pd, json
from collections import Counter

df = pd.read_csv("results/Data/signalmonitor_allstocks_state_20260407_105638.csv")

with open("results/Data/kiteticker_allstocks_ticks_latest.json") as f:
    ticks = json.load(f)

df["CurrentPrice"] = df["Symbol"].map(lambda s: float((ticks.get(str(s).upper(), {}) or {}).get("last_price", 0) or 0))
df["CurrentChange"] = df["Symbol"].map(lambda s: float((ticks.get(str(s).upper(), {}) or {}).get("change", 0) or 0))

big = df[df["CurrentChange"] >= 5].copy().sort_values("CurrentChange", ascending=False)
print(f"Total scanned: {len(df)}, Up>=5% today: {len(big)}")

def why_missed(row):
    r = []
    if row["NowAction"] in ("Hold / Do Nothing", "Sell / Exit"):
        r.append("HOLD/SELL_ACTION")
    if row["SellIfBelow"] == 0:
        r.append("NO_STOP")
    if row["VolumeRatio"] < 1.0:
        r.append("LOW_VOL_{}x".format(round(row["VolumeRatio"],1)))
    if row["SignalStrengthPct"] < 20:
        r.append("LOW_CONF_{}pct".format(int(row["SignalStrengthPct"])))
    if row["SellIfBelow"] > 0 and row["TakeProfit2"] > 0 and row["CurrentPrice"] > row["SellIfBelow"]:
        risk = row["CurrentPrice"] - row["SellIfBelow"]
        rr = (row["TakeProfit2"] - row["CurrentPrice"]) / risk if risk > 0 else 0
        if rr < 1.5:
            r.append("POOR_RR_{}".format(round(rr,1)))
    return " | ".join(r) if r else "SHOULD_HAVE_ALERTED"

big["WhyMissed"] = big.apply(why_missed, axis=1)

catchable = big[big["WhyMissed"] == "SHOULD_HAVE_ALERTED"]
print("\nSHOULD HAVE ALERTED (missed by system): {}".format(len(catchable)))
if not catchable.empty:
    print(catchable[["Symbol","CurrentChange","NowAction","VolumeRatio","SignalStrengthPct"]].to_string(index=False))

print("\n=== Top 30 movers + why missed ===")
for _, row in big.head(30).iterrows():
    print("  {:15s} {:+6.1f}%  {}".format(row["Symbol"], row["CurrentChange"], row["WhyMissed"]))

print("\n=== Root cause breakdown (out of {} stocks) ===".format(len(big)))
flat = []
for r in big["WhyMissed"]:
    for part in r.split(" | "):
        flat.append(part.split("_")[0])
for reason, count in Counter(flat).most_common():
    pct = 100*count//len(big)
    print("  {}: {} stocks ({}%)".format(reason, count, pct))
