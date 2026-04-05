"""
Backtest the BlindBuyScorer against the top-gainer audit data.

Answers the question: "If we had used the blind-buy strategy,
how many of the actual top gainers would we have caught?"

Usage:
    python scripts/backtest_blind_buy.py
    python scripts/backtest_blind_buy.py --min-score 30
    python scripts/backtest_blind_buy.py --audit results/Data/top_gainer_audit_2026-03-25_2026-04-02.csv
"""

import argparse
import os
import re
import sys

import pandas as pd

# Allow running from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _extract_rsi_from_reason(reason_text: str) -> float:
    """Parse RSI value from reason text like 'RSI oversold (33.8)'."""
    match = re.search(r"RSI\s+\w+\s*\((\d+\.?\d*)\)", reason_text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    if "rsi oversold" in reason_text.lower():
        return 25.0  # assume deeply oversold
    if "rsi approaching oversold" in reason_text.lower():
        return 38.0  # approximate
    return 50.0  # neutral default


def _extract_cci_from_reason(reason_text: str) -> float:
    """Parse CCI hint from reason text."""
    if "cci oversold" in reason_text.lower():
        return -120.0
    if "cci overbought" in reason_text.lower():
        return 120.0
    return 0.0


def _extract_mfi_from_reason(reason_text: str) -> float:
    """Parse MFI hint from reason text."""
    if "mfi oversold" in reason_text.lower():
        return 15.0
    if "mfi overbought" in reason_text.lower():
        return 85.0
    return 50.0


def enrich_audit_row(row: pd.Series) -> pd.Series:
    """Add estimated RSI/CCI/MFI columns from the Reason text for legacy audit data."""
    reason = str(row.get("Reason", ""))
    row = row.copy()
    if "RSI" not in row or pd.isna(row.get("RSI")):
        row["RSI"] = _extract_rsi_from_reason(reason)
    if "CCI" not in row or pd.isna(row.get("CCI")):
        row["CCI"] = _extract_cci_from_reason(reason)
    if "MFI" not in row or pd.isna(row.get("MFI")):
        row["MFI"] = _extract_mfi_from_reason(reason)
    # Map audit columns to what BlindBuyScorer expects
    if "ChangePct" not in row:
        row["ChangePct"] = row.get("CloseToClosePct", 0)
    if "EntryGapPct" not in row:
        # Estimate: if entry > prev close, gap = (entry - prevclose) / prevclose * 100
        entry = float(row.get("Entry", 0) or 0)
        prev_close = float(row.get("PrevClose", 0) or 0)
        if entry > 0 and prev_close > 0:
            row["EntryGapPct"] = round(abs(entry - prev_close) / prev_close * 100, 2)
        else:
            row["EntryGapPct"] = 0.0
    if "Why" not in row:
        row["Why"] = row.get("Reason", "")
    return row


def run_backtest(audit_path: str, min_score: float = 40.0, max_entry_gap: float = 1.5):
    """Run the blind-buy backtest against audit data."""
    from pkscreener.classes.AutoPilotInvestor import BlindBuyScorer

    df = pd.read_csv(audit_path)
    print(f"\nLoaded {len(df)} audit rows from {audit_path}")
    print(f"Blind buy min score: {min_score}, max entry gap: {max_entry_gap}%\n")

    results = []
    for _, row in df.iterrows():
        enriched = enrich_audit_row(row)
        blind_score, reasons = BlindBuyScorer.score(enriched)
        entry = float(row.get("Entry", 0) or 0)
        close_on_day = float(row.get("Close", 0) or 0)
        prev_close = float(row.get("PrevClose", 0) or 0)
        actual_return = round((close_on_day - entry) / entry * 100, 2) if entry > 0 else 0
        would_trigger = str(row.get("WouldTriggerEntry", "")).strip().lower() == "true"
        would_profit = str(row.get("WouldCloseAboveEntry", "")).strip().lower() == "true"
        original_action = str(row.get("NowAction", ""))

        # Would blind-buy have flagged this?
        entry_gap = float(enriched.get("EntryGapPct", 0))
        blind_buy_pass = blind_score >= min_score and entry_gap <= max_entry_gap
        original_buy = original_action.lower() in ["buy now", "strong buy now"]

        results.append({
            "Date": row.get("Date", ""),
            "Symbol": row.get("Symbol", ""),
            "DayRank": int(row.get("DayRank", 0)),
            "ActualReturn%": actual_return,
            "CloseToClose%": float(row.get("CloseToClosePct", 0)),
            "OriginalAction": original_action,
            "OriginalBuy": original_buy,
            "BlindScore": blind_score,
            "BlindBuyPass": blind_buy_pass,
            "WouldTrigger": would_trigger,
            "WouldProfit": would_profit,
            "Reasons": reasons,
            "RSI": float(enriched.get("RSI", 50)),
            "VolumeRatio": float(row.get("VolumeRatio", 0) or 0),
            "Confidence": float(row.get("ConfidencePct", 0) or 0),
        })

    results_df = pd.DataFrame(results)

    # ---- Summary ----
    total = len(results_df)
    original_caught = results_df["OriginalBuy"].sum()
    blind_caught = results_df["BlindBuyPass"].sum()
    both_caught = ((results_df["OriginalBuy"]) & (results_df["BlindBuyPass"])).sum()
    blind_only = ((~results_df["OriginalBuy"]) & (results_df["BlindBuyPass"])).sum()
    missed_by_both = ((~results_df["OriginalBuy"]) & (~results_df["BlindBuyPass"])).sum()

    # Win rate for blind-buy picks
    blind_picks = results_df[results_df["BlindBuyPass"]]
    if len(blind_picks) > 0:
        blind_win_rate = (blind_picks["WouldProfit"].sum() / len(blind_picks)) * 100
        blind_avg_return = blind_picks["ActualReturn%"].mean()
        blind_median_return = blind_picks["ActualReturn%"].median()
    else:
        blind_win_rate = 0
        blind_avg_return = 0
        blind_median_return = 0

    # By date breakdown
    print("=" * 80)
    print("BLIND-BUY BACKTEST RESULTS")
    print("=" * 80)
    print(f"\nTotal top gainers analyzed: {total}")
    print(f"Original scanner caught:    {original_caught}/{total} ({original_caught/total*100:.0f}%)")
    print(f"Blind-buy would catch:      {blind_caught}/{total} ({blind_caught/total*100:.0f}%)")
    print(f"  - Also caught by scanner: {both_caught}")
    print(f"  - NEW catches (blind-only): {blind_only}")
    print(f"Missed by both:             {missed_by_both}/{total} ({missed_by_both/total*100:.0f}%)")

    print(f"\n--- Blind-Buy Performance ---")
    print(f"Win rate (would profit):    {blind_win_rate:.0f}%")
    print(f"Average return:             {blind_avg_return:.1f}%")
    print(f"Median return:              {blind_median_return:.1f}%")

    # Per-date breakdown
    print(f"\n--- Per-Date Breakdown ---")
    for date in sorted(results_df["Date"].unique()):
        day = results_df[results_df["Date"] == date]
        day_total = len(day)
        day_orig = day["OriginalBuy"].sum()
        day_blind = day["BlindBuyPass"].sum()
        day_new = ((~day["OriginalBuy"]) & (day["BlindBuyPass"])).sum()
        print(f"  {date}: {day_total} gainers, scanner={day_orig}, blind={day_blind} (new={day_new})")

    # Top scored
    print(f"\n--- Top 15 Blind-Buy Scores ---")
    top = results_df.nlargest(15, "BlindScore")
    for _, r in top.iterrows():
        flag = "CAUGHT" if r["BlindBuyPass"] else "missed"
        orig = "ALSO scanner" if r["OriginalBuy"] else "scanner missed"
        print(f"  {r['Symbol']:12s} score={r['BlindScore']:5.1f} return={r['ActualReturn%']:+6.1f}% "
              f"RSI={r['RSI']:.0f} vol={r['VolumeRatio']:.1f}x [{flag}] [{orig}]")

    # Missed by both
    missed = results_df[(~results_df["OriginalBuy"]) & (~results_df["BlindBuyPass"])]
    if not missed.empty:
        print(f"\n--- Missed By Both ({len(missed)} stocks) ---")
        for _, r in missed.iterrows():
            print(f"  {r['Symbol']:12s} score={r['BlindScore']:5.1f} return={r['ActualReturn%']:+6.1f}% "
                  f"RSI={r['RSI']:.0f} vol={r['VolumeRatio']:.1f}x conf={r['Confidence']:.0f}%")
            print(f"    Action: {r['OriginalAction']} | {r['Reasons']}")

    # Save detailed results
    output_dir = os.path.dirname(audit_path)
    output_path = os.path.join(output_dir, "blind_buy_backtest_results.csv")
    results_df.to_csv(output_path, index=False)
    print(f"\nDetailed results saved to: {output_path}")

    # Score threshold analysis
    print(f"\n--- Score Threshold Analysis ---")
    for threshold in [20, 25, 30, 35, 40, 45, 50, 55, 60]:
        picks = results_df[results_df["BlindScore"] >= threshold]
        if len(picks) > 0:
            catch_rate = len(picks) / total * 100
            win = (picks["WouldProfit"].sum() / len(picks)) * 100
            avg_ret = picks["ActualReturn%"].mean()
            print(f"  Score >= {threshold:2d}: catches {len(picks):2d}/{total} ({catch_rate:4.0f}%), "
                  f"win rate {win:5.1f}%, avg return {avg_ret:+5.1f}%")
        else:
            print(f"  Score >= {threshold:2d}: 0 catches")

    return results_df


def main():
    parser = argparse.ArgumentParser(description="Backtest blind-buy strategy against top-gainer audit data")
    parser.add_argument("--audit", default=None, help="Path to audit CSV (auto-detects if not given)")
    parser.add_argument("--min-score", type=float, default=35.0, help="Minimum blind-buy score threshold (default matches AutoPilotConfig)")
    parser.add_argument("--max-gap", type=float, default=5.0, help="Maximum entry gap percent (default matches AutoPilotConfig)")
    args = parser.parse_args()

    audit_path = args.audit
    if audit_path is None:
        # Auto-detect latest audit file
        data_dir = os.path.join(os.path.dirname(__file__), "..", "results", "Data")
        if os.path.isdir(data_dir):
            candidates = [f for f in os.listdir(data_dir) if f.startswith("top_gainer_audit_") and not "summary" in f]
            if candidates:
                audit_path = os.path.join(data_dir, sorted(candidates)[-1])

    if not audit_path or not os.path.isfile(audit_path):
        print("No audit file found. Run the top-gainer audit first or specify --audit path.")
        sys.exit(1)

    run_backtest(audit_path, min_score=args.min_score, max_entry_gap=args.max_gap)


if __name__ == "__main__":
    main()
