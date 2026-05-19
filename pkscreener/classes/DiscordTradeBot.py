"""
Discord bot for mobile-first live portfolio tracking.

This bot is intentionally focused on manual trade logging and portfolio review:
- receive slash commands from Discord mobile/desktop
- save trades into separate livetrade_* files
- refresh open positions with current market data
- reply with cash, equity, P/L, and position guidance
"""

import asyncio
import os
from typing import Optional, Tuple

import pandas as pd

import pkscreener.classes.ConfigManager as ConfigManager
from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.log import default_logger

from pkscreener.classes.LivePortfolio import LivePortfolioRunner
from pkscreener.classes.RiskGuardrails import LiveTradingGuardrails
from pkscreener.classes.HealthMonitor import HealthMonitor


def _get_secret(name: str, default: str = "") -> str:
    """Load a secret from env first, then PKEnvironment if available."""
    value = str(os.environ.get(name, default) or default).strip()
    if value:
        return value
    try:
        from PKDevTools.classes.Environment import PKEnvironment

        env = PKEnvironment()
        return str(env.allSecrets.get(name, default) or default).strip()
    except Exception:
        return str(default).strip()


def _get_int_secret(name: str) -> Optional[int]:
    """Load an integer secret/setting if present."""
    value = _get_secret(name, "")
    if not value:
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _get_float_secret(name: str, default: float) -> float:
    """Load a float secret/setting if present, otherwise return the default."""
    value = _get_secret(name, str(default))
    try:
        return max(0.0, float(value))
    except Exception:
        return float(default)


def _format_money(value: float) -> str:
    return f"{float(value or 0.0):,.2f}"


def _estimate_entry_costs(turnover: float) -> float:
    """Estimate buy-side costs using the same env-tunable defaults as the portfolio engine."""
    pct = _get_float_secret("CHARAN_EST_BUY_COST_PCT", 0.00118)
    return round(max(0.0, float(turnover or 0.0)) * pct, 2)


def _estimate_exit_costs(turnover: float) -> float:
    """Estimate sell-side costs using the same env-tunable defaults as the portfolio engine."""
    pct = _get_float_secret("CHARAN_EST_SELL_COST_PCT", 0.00104)
    return round(max(0.0, float(turnover or 0.0)) * pct, 2)


def _format_summary_text(summary_df: pd.DataFrame) -> str:
    """Turn the summary frame into a short Discord-friendly text block."""
    if summary_df is None or summary_df.empty:
        return "Portfolio summary is not available yet."

    row = summary_df.iloc[0]
    return "\n".join(
        [
            f"Money In: {_format_money(row.get('TotalMoneyAdded', 0.0))}",
            f"Cash Left: {_format_money(row.get('Cash', 0.0))}",
            f"Open Trades Value: {_format_money(row.get('MarketValue', 0.0))}",
            f"Charges Paid: {_format_money(row.get('ChargesPaid', 0.0))}",
            f"If Sold Now Extra Charges: {_format_money(row.get('OpenExitChargesEstimate', 0.0))}",
            f"Total Account Value: {_format_money(row.get('Equity', 0.0))}",
            f"Booked Net P/L: {_format_money(row.get('RealizedPnL', 0.0))}",
            f"Running Net P/L: {_format_money(row.get('UnrealizedPnL', 0.0))}",
            f"Total Net P/L: {_format_money(row.get('TotalPnL', 0.0))} ({float(row.get('TotalReturnPct', 0.0)):.2f}%)",
            f"Open Trades: {int(row.get('OpenPositions', 0) or 0)} | Closed Trades: {int(row.get('ClosedTrades', 0) or 0)}",
            f"Last Checked: {row.get('LastRunAt', '')}",
        ]
    )


def _format_positions_text(runner: LivePortfolioRunner, positions_df: pd.DataFrame, limit: int = 8) -> str:
    """Turn open positions into a short Discord-friendly list."""
    if positions_df is None or positions_df.empty:
        return "No open positions right now."

    simple_df = runner._build_positions_simple_view(positions_df).head(limit).copy()
    lines = []
    for _, row in simple_df.iterrows():
        lines.append(
            f"{row['Stock']} x{int(float(row['Shares Held']))} | buy {_format_money(row['Bought At'])} | "
            f"now {_format_money(row['Price Now'])} | net P/L {_format_money(row['Profit/Loss Now'])} | "
            f"exit costs {_format_money(row.get('Est. Exit Charges', 0.0))} | {row['What To Do Now']}"
        )
    return "\n".join(lines)


def _format_pnl_text(summary_df: pd.DataFrame) -> str:
    """Create a cleaner P&L snapshot for quick phone reading."""
    if summary_df is None or summary_df.empty:
        return "P&L is not available yet."

    row = summary_df.iloc[0]
    return "\n".join(
        [
            "P&L Snapshot",
            f"Money In: {_format_money(row.get('TotalMoneyAdded', 0.0))}",
            f"Cash Left: {_format_money(row.get('Cash', 0.0))}",
            f"Open Trades Value: {_format_money(row.get('MarketValue', 0.0))}",
            f"Charges Paid: {_format_money(row.get('ChargesPaid', 0.0))}",
            f"If Sold Now Extra Charges: {_format_money(row.get('OpenExitChargesEstimate', 0.0))}",
            f"Booked Net P&L: {_format_money(row.get('RealizedPnL', 0.0))}",
            f"Running Net P&L: {_format_money(row.get('UnrealizedPnL', 0.0))}",
            f"Total Net P&L: {_format_money(row.get('TotalPnL', 0.0))} ({float(row.get('TotalReturnPct', 0.0)):.2f}%)",
            f"Wins: {int(row.get('Wins', 0) or 0)} | Losses: {int(row.get('Losses', 0) or 0)} | Win Rate: {float(row.get('WinRatePct', 0.0) or 0.0):.2f}%",
            f"Open Trades: {int(row.get('OpenPositions', 0) or 0)} | Closed Trades: {int(row.get('ClosedTrades', 0) or 0)}",
            f"Last Checked: {row.get('LastRunAt', '')}",
        ]
    )


def _format_trade_history_text(trades_df: pd.DataFrame, limit: int = 8) -> str:
    """Format recent open/closed trade records for Discord."""
    if trades_df is None or trades_df.empty:
        return "No trade history is available yet."

    history_df = trades_df.copy()
    for column in ["EntryPrice", "ExitPrice", "RealizedPnL", "UnrealizedPnL", "Quantity"]:
        if column in history_df.columns:
            history_df[column] = pd.to_numeric(history_df[column], errors="coerce").fillna(0.0)

    history_df["SortKey"] = history_df["ExitDate"].astype(str).where(
        history_df["ExitDate"].astype(str).str.strip() != "",
        history_df["LastUpdatedAt"].astype(str).where(
            history_df["LastUpdatedAt"].astype(str).str.strip() != "",
            history_df["EntryDate"].astype(str),
        ),
    )
    history_df.sort_values(["SortKey", "TradeId"], ascending=[False, False], inplace=True)

    lines = []
    for _, row in history_df.head(max(1, int(limit or 8))).iterrows():
        status = str(row.get("Status", "")).strip().upper()
        symbol = str(row.get("Symbol", "")).strip().upper()
        qty = int(float(row.get("Quantity", 0) or 0))
        entry_price = _format_money(row.get("EntryPrice", 0.0))
        invested_amount = float(row.get("InvestedAmount", 0.0) or 0.0)
        entry_charges = _estimate_entry_costs(invested_amount)
        if status == "OPEN":
            current_value = float(row.get("CurrentValue", 0.0) or 0.0)
            exit_costs = _estimate_exit_costs(current_value)
            lines.append(
                f"OPEN {symbol} x{qty} | buy {entry_price} | now {_format_money(row.get('CurrentPrice', 0.0))} | "
                f"net P&L {_format_money(row.get('UnrealizedPnL', 0.0))} | costs {_format_money(entry_charges + exit_costs)}"
            )
        else:
            exit_value = float(row.get("ExitValue", 0.0) or 0.0)
            exit_charges = _estimate_exit_costs(exit_value)
            lines.append(
                f"CLOSED {symbol} x{qty} | buy {entry_price} | sell {_format_money(row.get('ExitPrice', 0.0))} | "
                f"net P&L {_format_money(row.get('RealizedPnL', 0.0))} | costs {_format_money(entry_charges + exit_charges)}"
            )
    return "\n".join(lines) if lines else "No trade history is available yet."


def _format_event_lines(prefix: str, events) -> str:
    """Format event messages for Discord replies."""
    lines = [str(event).strip() for event in (events or []) if str(event).strip()]
    if not lines:
        return ""
    return "\n".join([f"{prefix} {line}" for line in lines[:8]])


def _latest_signalmonitor_path(universe: str) -> str:
    """Return the latest signal monitor simple board path for a universe."""
    return os.path.join(
        Archiver.get_user_data_dir(),
        f"signalmonitor_{str(universe or '').strip().lower()}_signals_simple_latest.csv",
    )


def _load_signal_rows(universe: str, action, limit: int = 5) -> pd.DataFrame:
    """Load the latest monitor board and filter it by one or more plain-English actions."""
    path = _latest_signalmonitor_path(universe)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()

    if "What To Do" not in df.columns:
        return pd.DataFrame()

    actions = action if isinstance(action, (list, tuple, set)) else [action]
    normalized = {str(item).strip() for item in actions if str(item).strip()}
    filtered = df[df["What To Do"].astype(str).str.strip().isin(normalized)].copy()
    if filtered.empty:
        return filtered
    action_rank = {
        "Strong Buy Now": 0,
        "Buy Now": 1,
        "Sell / Exit": 2,
    }
    filtered["__rank"] = filtered["What To Do"].astype(str).map(action_rank).fillna(99)
    filtered.sort_values(["__rank", "Signal Strength %", "Stock"], ascending=[True, False, True], inplace=True)
    filtered.drop(columns=["__rank"], inplace=True, errors="ignore")
    return filtered.head(max(1, int(limit or 5)))


def _load_buynow_rows(universe: str, limit: int = 5) -> pd.DataFrame:
    """Load /buynow picks with the same quality gates as AutoPilot Discord alerts.

    Filters applied (same as _find_blind_buys + tick path):
    - BlindBuyScore <= max_score (dead-cat bounce cap)
    - Confidence >= min_confidence
    - Volume ratio >= min_volume_ratio (time-adjusted in live scan)
    - Entry gap <= max_entry_gap_pct
    - R:R >= 2.0 on Target 2
    - No bearish / DO NOT BUY flags
    """
    path = _latest_signalmonitor_path(universe)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()

    if "What To Do" not in df.columns:
        return pd.DataFrame()

    buy_actions = {"Strong Buy Now", "Buy Now"}
    candidates = df[df["What To Do"].astype(str).str.strip().isin(buy_actions)].copy()
    if candidates.empty:
        return candidates

    try:
        from pkscreener.classes.AutoPilotInvestor import BlindBuyScorer, AutoPilotConfig
        _cfg = AutoPilotConfig()
    except Exception:
        # If scorer unavailable, fall back to unfiltered
        return _load_signal_rows(universe, list(buy_actions), limit)

    min_confidence = float(getattr(_cfg, "min_confidence", 15.0) or 15.0)
    min_volume_ratio = float(getattr(_cfg, "min_volume_ratio", 5.0) or 5.0)
    max_entry_gap_pct = float(getattr(_cfg, "max_entry_gap_pct", 5.0) or 5.0)
    max_blind_score = float(getattr(_cfg, "blind_buy_max_score", 40.0) or 40.0)
    # Strong Buy Now thresholds — calibrated 2026-05-19 against actual alert
    # distributions. Previous (conf>=25, gap<=0.25) qualified zero of 5 alerts
    # firing in production, because real entry gaps are typically 0.3-0.7%
    # and even top-confidence stocks rarely hit 25%+ with gap under 0.25%.
    # New thresholds: conf >= 30 AND gap <= 0.5% — selective but actually firing.
    strong_buy_min_conf = max(min_confidence + 15.0, 30.0)
    strong_buy_max_gap = 0.5

    results = []
    for _, row in candidates.iterrows():
        blind_score, blind_reasons = BlindBuyScorer.score(row)
        if max_blind_score > 0 and blind_score >= max_blind_score:
            continue

        reasons_lower = blind_reasons.lower()
        if "do not buy" in reasons_lower or "bearish" in reasons_lower:
            continue

        confidence = float(row.get("Signal Strength %", row.get("ConfidencePct", 0)) or 0)
        if confidence < min_confidence:
            continue

        entry_gap = float(row.get("Entry Gap %", row.get("EntryGapPct", 0)) or 0)
        if entry_gap > max_entry_gap_pct:
            continue

        vol_ratio = float(row.get("Volume Ratio", row.get("VolumeRatio", 0)) or 0)
        if vol_ratio < min_volume_ratio:
            continue

        price     = float(row.get("Price Now", row.get("PriceNow", 0)) or 0)
        stop_loss = float(row.get("Sell If Below", row.get("SellIfBelow", 0)) or 0)
        target2   = float(row.get("Take Profit 2", row.get("TakeProfit2", 0)) or 0)

        if stop_loss > 0 and target2 > 0 and price > stop_loss:
            risk = price - stop_loss
            rr2 = (target2 - price) / risk if risk > 0 else 0
            if rr2 < 2.0:
                continue

        row = row.copy()
        row["__blind_score"] = blind_score
        row["What To Do"] = "Strong Buy Now" if confidence >= strong_buy_min_conf and entry_gap <= strong_buy_max_gap else "Buy Now"
        results.append(row)

    if not results:
        return pd.DataFrame()

    out = pd.DataFrame(results)
    out.sort_values("__blind_score", ascending=False, inplace=True)
    # Keep __blind_score for formatting — dropped in _format_signal_rows_text after use
    return out.head(max(1, int(limit or 5)))


def _guardrail_text(account_name: str) -> str:
    """Return a short buy-side guardrail status line for Discord replies."""
    max_daily_loss = _get_float_secret("ASSISTANT_MAX_DAILY_LOSS", 0.0)
    max_consecutive_losses = _get_int_secret("ASSISTANT_MAX_CONSECUTIVE_LOSSES") or 0
    status = LiveTradingGuardrails(
        account_name=account_name,
        max_daily_loss_amount=max_daily_loss,
        max_consecutive_losses=max_consecutive_losses,
    ).evaluate()
    if not status.enabled:
        return ""
    if status.buy_paused:
        return "Buy guardrail active: " + "; ".join(status.reasons)
    return (
        f"Buy guardrail clear: daily realized P&L {_format_money(status.daily_realized_pnl)} | "
        f"consecutive losses {int(status.consecutive_losses)}"
    )


def _format_signal_rows_text(rows_df: pd.DataFrame, empty_text: str) -> str:
    """Format /buynow signal rows in the same style as auto-BUY alerts."""
    if rows_df is None or rows_df.empty:
        return empty_text

    try:
        from pkscreener.classes.AutoPilotInvestor import _compute_suggested_qty
    except Exception:
        _compute_suggested_qty = None

    lines = []
    for _, row in rows_df.iterrows():
        sym = str(row.get("Stock", row.get("Symbol", ""))).strip().upper()
        price = float(row.get("Price Now", row.get("PriceNow", 0)) or 0)
        buy_above = float(row.get("Buy Above", row.get("BuyAbove", 0)) or 0)
        stop_loss = float(row.get("Sell If Below", row.get("SellIfBelow", 0)) or 0)
        target1 = float(row.get("Take Profit 1", row.get("TakeProfit1", 0)) or 0)
        target2 = float(row.get("Take Profit 2", row.get("TakeProfit2", 0)) or 0)
        change_pct = float(row.get("Change %", row.get("ChangePct", row.get("% Change", 0))) or 0)
        blind_score = float(row.get("__blind_score", 0) or 0)

        # Score label matching auto-alert style
        if blind_score >= 70:
            score_label = f"\U0001f525 **{blind_score:.0f}/100**"
        elif blind_score >= 55:
            score_label = f"\u2705 **{blind_score:.0f}/100**"
        else:
            score_label = f"**{blind_score:.0f}/100**"

        # Quantity and investment amount
        _qty_raw = row.get("Suggested Qty", 0)
        try:
            qty = int(float(_qty_raw)) if _qty_raw == _qty_raw else 0
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0 and _compute_suggested_qty is not None:
            qty, invest_amt = _compute_suggested_qty(
                price,
                stop_loss=stop_loss,
                score=blind_score,
            )
        else:
            if price > 0:
                qty = min(qty, int(5000 // price))
            invest_amt = round(qty * price, 0) if price > 0 and qty > 0 else 0

        # R:R
        rr_text = ""
        if stop_loss > 0 and target2 > 0 and price > stop_loss:
            risk = price - stop_loss
            rr2 = round((target2 - price) / risk, 1) if risk > 0 else 0
            rr_text = f" | R:R `1:{rr2}`"

        direction = "+" if change_pct >= 0 else ""
        why = str(row.get("Why", "") or "").strip()

        lines.extend([
            f"**\U0001f7e2 BUY `{sym}`** (OHLCV signal, not yet tick-confirmed) \u2014 Score {score_label}",
            f"Price `{price:.2f}` ({direction}{change_pct:.2f}% today) | Buy Above `{buy_above:.2f}`",
            f"Stop Loss `{stop_loss:.2f}` | Target 1 `{target1:.2f}` | Target 2 `{target2:.2f}`{rr_text}",
            (
                f"Suggested: Buy **`{qty}` shares** (\u20b9{invest_amt:,.0f})"
                if qty > 0
                else "Suggested: Skip starter-size entry under the \u20b95,000 cap"
            ),
            f"Why: {why}",
            "",
        ])
    return "\n".join(lines).strip()


def _tag_buy_with_blind_score(symbol: str, qty: int, price: float, account_name: str, universe: str) -> str:
    """Score a manually-bought stock with BlindBuyScorer and log to trade journal CSV."""
    import os
    from datetime import datetime
    import pandas as pd
    from PKDevTools.classes import Archiver

    output_dir = Archiver.get_user_data_dir()

    # Try to find this symbol in the latest signal state
    blind_score = 0.0
    reasons = ""
    for name in ["state", "signals", "actionable"]:
        path = os.path.join(output_dir, f"signalmonitor_{universe}_{name}_latest.csv")
        if os.path.isfile(path):
            try:
                df = pd.read_csv(path)
                sym_col = "Symbol" if "Symbol" in df.columns else "Stock"
                match = df[df[sym_col].astype(str).str.strip().str.upper() == symbol.strip().upper()]
                if not match.empty:
                    from pkscreener.classes.AutoPilotInvestor import BlindBuyScorer
                    blind_score, reasons = BlindBuyScorer.score(match.iloc[0])
                    break
            except Exception:
                continue

    # Append to trade journal
    journal_path = os.path.join(output_dir, f"trade_journal_{account_name}.csv")
    row = {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Action": "BUY",
        "Symbol": symbol.strip().upper(),
        "Qty": qty,
        "Price": price,
        "BlindScore": blind_score,
        "Reasons": reasons,
    }
    row_df = pd.DataFrame([row])
    if os.path.isfile(journal_path):
        row_df.to_csv(journal_path, mode="a", header=False, index=False)
    else:
        row_df.to_csv(journal_path, index=False)

    if blind_score > 0:
        return f"_Blind-buy score at entry: **{blind_score:.0f}/100** ({reasons})_"
    return ""


def _tag_sell_in_journal(symbol: str, qty: int, price: float, account_name: str) -> str:
    """Log a sell to the trade journal and compute return vs entry blind-buy score."""
    import os
    from datetime import datetime
    import pandas as pd
    from PKDevTools.classes import Archiver

    output_dir = Archiver.get_user_data_dir()
    journal_path = os.path.join(output_dir, f"trade_journal_{account_name}.csv")

    # Find matching buy entry to compute return
    entry_score = 0.0
    entry_price = 0.0
    if os.path.isfile(journal_path):
        try:
            journal = pd.read_csv(journal_path)
            buys = journal[
                (journal["Action"] == "BUY")
                & (journal["Symbol"].astype(str).str.strip().str.upper() == symbol.strip().upper())
            ]
            if not buys.empty:
                last_buy = buys.iloc[-1]
                entry_score = float(last_buy.get("BlindScore", 0) or 0)
                entry_price = float(last_buy.get("Price", 0) or 0)
        except Exception:
            pass

    pnl_pct = round((price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0.0

    # Append sell to journal
    row = {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Action": "SELL",
        "Symbol": symbol.strip().upper(),
        "Qty": qty,
        "Price": price,
        "BlindScore": entry_score,
        "Reasons": f"entry@{entry_price:.2f} pnl={pnl_pct:+.1f}%",
    }
    row_df = pd.DataFrame([row])
    if os.path.isfile(journal_path):
        row_df.to_csv(journal_path, mode="a", header=False, index=False)
    else:
        row_df.to_csv(journal_path, index=False)

    if entry_price > 0:
        emoji = "+" if pnl_pct >= 0 else ""
        return f"_Trade closed: entry score **{entry_score:.0f}/100**, return **{emoji}{pnl_pct:.1f}%**_"
    return ""


def _execute_live_portfolio_action(
    config_manager,
    account_name: str,
    universe: str,
    add_cash_amount: float = 0.0,
    manual_buys=None,
    manual_sells=None,
):
    """Blocking helper used by Discord commands via asyncio.to_thread."""
    runner = LivePortfolioRunner(
        config_manager=config_manager,
        account_name=account_name,
        universe=universe,
        add_cash_amount=add_cash_amount,
        manual_buys=manual_buys,
        manual_sells=manual_sells,
    )
    trades_df, positions_df, summary_df = runner.run()
    return runner, trades_df, positions_df, summary_df


async def run_discord_trade_bot(account_name: str = "main", universe: str = "allstocks") -> None:
    """Run the Discord slash-command bot for live portfolio tracking."""
    try:
        import discord
        from discord import app_commands
    except Exception as exc:
        OutputControls().printOutput(
            f"  [+] Discord bot support needs 'discord.py'. Install it first. Details: {exc}"
        )
        return

    token = _get_secret("DISCORD_BOT_TOKEN", "")
    if not token:
        OutputControls().printOutput(
            "  [+] DISCORD_BOT_TOKEN is not configured. Add it to .env.dev before starting the Discord bot."
        )
        return

    guild_id = _get_int_secret("DISCORD_GUILD_ID")
    allowed_user_id = _get_int_secret("DISCORD_ALLOWED_USER_ID")
    allowed_channel_id = _get_int_secret("DISCORD_CHANNEL_ID")

    config_manager = ConfigManager.tools()
    if not config_manager.checkConfigFile():
        config_manager.setConfig(ConfigManager.parser, default=True, showFileCreatedText=False)
    config_manager.getConfig(ConfigManager.parser)

    intents = discord.Intents.default()
    intents.message_content = True  # required to read plain-text messages for buy-intent workflow

    class PKScreenerDiscordTradeBot(discord.Client):
        def __init__(self):
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self):
            if guild_id:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                OutputControls().printOutput(
                    f"  [+] Synced {len(synced)} Discord bot commands to guild {guild_id}."
                )
            else:
                synced = await self.tree.sync()
                OutputControls().printOutput(
                    f"  [+] Synced {len(synced)} Discord bot commands globally."
                )

        async def on_ready(self):
            OutputControls().printOutput(
                f"  [+] Discord trade bot is online as {self.user}."
            )
            
            # Start periodic health check once (on first ready)
            if not hasattr(self, '_health_check_started'):
                self._health_check_started = True
                self.loop.create_task(self.periodic_health_check())
            
            # Broadcast a startup message to the configured channel so the user
            # knows the bot is online and remembers where to type commands.
            if allowed_channel_id:
                try:
                    channel = self.get_channel(int(allowed_channel_id))
                    if channel is None:
                        channel = await self.fetch_channel(int(allowed_channel_id))
                    if channel:
                        kite_line = ""
                        try:
                            from pkscreener.classes.KiteOrderManager import KiteOrderManager
                            _km = KiteOrderManager(output_dir=Archiver.get_user_data_dir())
                            if _km.auto_trade:
                                kite_line = f"\n• 🤖 Kite auto-trade **ON** (dry_run={'Yes' if _km.dry_run else 'No'})"
                        except Exception:
                            pass
                        await channel.send(
                            f"**\U0001f7e2 Trade bot online** (`{self.user}`)"
                            f"\n• `buy RELIANCE` — record buy intent (then `bought RELIANCE 10 1450` to confirm + auto-GTT)"
                            f"\n• `/buy symbol:RELIANCE qty:2 price:1450` — log a buy (slash)"
                            f"\n• `/sell symbol:RELIANCE qty:2 price:1480` — log a sell"
                            f"\n• `/portfolio` — see all positions + P&L"
                            f"\n• `/buynow` — latest buy signals"
                            f"{kite_line}"
                            f"\n• `/helptrade` — full command list"
                        )
                except Exception as exc:
                    OutputControls().printOutput(f"  [+] Could not send startup message: {exc}")
            
            async def _run_periodic_health_check():
                """Background task: check system health every 5 minutes and alert on issues."""
                await asyncio.sleep(15)  # Let bot fully connect to Discord first
                last_issues = set()
                
                while True:
                    try:
                        await asyncio.sleep(300)  # Check every 5 minutes
                        
                        healthy, status_msg = self.health_monitor.check_all()
                        
                        # Extract issue descriptions (only ❌ and ⚠️ lines)
                        current_issues = {
                            line.strip() for line in status_msg.split('\n') 
                            if line.strip() and ('❌' in line or '⚠️' in line)
                        }
                        
                        if not healthy and current_issues != last_issues:
                            # Issues detected and changed from last check - send alert
                            alert_msg = f"⚠️ **Health Alert** — Issues detected:\n{status_msg}"
                            if allowed_channel_id:
                                try:
                                    channel = self.get_channel(int(allowed_channel_id))
                                    if channel:
                                        await channel.send(alert_msg[:1900])
                                except Exception as e:
                                    OutputControls().printOutput(f"  [+] Could not send health alert: {e}")
                            self.health_monitor.send_alert(alert_msg)
                            last_issues = current_issues
                            
                        elif healthy and last_issues:
                            # System recovered from previous issues
                            recovery_msg = f"✅ **System Recovered** — All checks passing\n{status_msg}"
                            if allowed_channel_id:
                                try:
                                    channel = self.get_channel(int(allowed_channel_id))
                                    if channel:
                                        await channel.send(recovery_msg[:1900])
                                except Exception:
                                    pass
                            last_issues = set()
                            
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        OutputControls().printOutput(f"  [+] Health check error: {e}")
            
            # Store the health monitor for use in background task
            self.health_monitor = getattr(self, 'health_monitor', None)
            
            # Create and store the periodic health check task
            self.periodic_health_check = _run_periodic_health_check

    bot = PKScreenerDiscordTradeBot()

    # Initialize health monitor
    output_dir = Archiver.get_user_outputs_dir()
    health_monitor = HealthMonitor(
        session_id=_get_int_secret("SUPERVISOR_SESSION_ID") or 0,
        output_dir=output_dir,
        discord_webhook=_get_secret("DISCORD_WEBHOOK_URL", ""),
    )

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error) -> None:
        """Global handler: any unhandled exception in a command shows an error instead of 'did not respond'."""
        import traceback
        err_str = str(error)
        # Unwrap CommandInvokeError to get the original exception message
        cause = getattr(error, "original", error)
        err_str = f"{type(cause).__name__}: {cause}"
        msg = f"⚠️ Command failed: {err_str}"[:1900]
        OutputControls().printOutput(f"  [+] Discord command error: {traceback.format_exc()}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    def _authorize(interaction) -> Tuple[bool, str]:
        if allowed_user_id and int(interaction.user.id) != int(allowed_user_id):
            return False, "⛔ You are not authorised to use this trading bot."
        if allowed_channel_id and int(interaction.channel_id) != int(allowed_channel_id):
            channel_mention = f"<#{allowed_channel_id}>" if allowed_channel_id else "the configured trade channel"
            return False, f"⛔ Trade commands only work in {channel_mention}. Please go there and try again."
        return True, ""

    async def _reply_unauthorized(interaction, reason: str):
        if interaction.response.is_done():
            await interaction.followup.send(reason, ephemeral=True)
        else:
            await interaction.response.send_message(reason, ephemeral=True)

    @bot.tree.command(name="whoami", description="Show your Discord ids for bot setup")
    async def whoami(interaction):  # type: ignore
        text = "\n".join(
            [
                f"User ID: {interaction.user.id}",
                f"Guild ID: {interaction.guild_id or 'DM/None'}",
                f"Channel ID: {interaction.channel_id}",
            ]
        )
        await interaction.response.send_message(text, ephemeral=True)

    @bot.tree.command(name="helptrade", description="Show the available trade commands")
    async def helptrade(interaction):  # type: ignore
        text = "\n".join(
            [
                "**Trade Bot Commands:**",
                "`/buy symbol:RELIANCE qty:2 price:1450` — log a buy",
                "`/sell symbol:RELIANCE qty:2 price:1480` — log a sell",
                "`/addcash amount:5000` — add cash to account",
                "`/portfolio` — all positions + P&L summary",
                "`/positions` — open positions only",
                "`/pnl` — booked + running P&L",
                "`/buynow` — latest Buy Now signals",
                "`/sellnow` — latest Sell signals",
                "`/history` — recent trade log",
                "`/refresh` — refresh prices for open positions",
                "`/autopilot` — run a blind-buy scan now",
                "",
                "**Manual Buy Workflow (plain text):**",
                "`buy RELIANCE` — record intent (shows SL + target from signal)",
                "`buy RELIANCE 10 1450` — intent with qty and price",
                "`bought RELIANCE 10 1450` — confirm you bought; auto-sets GTT stop-loss + target on Kite",
                "`cancel RELIANCE` — drop a pending intent",
                "`intents` — list pending buy intents",
                "",
                "**Kite Auto-Trade:**",
                "`/kitebuy symbol:RELIANCE` — buy via Kite API",
                "`/kitesell symbol:RELIANCE` — sell via Kite API",
                "`/kitepositions` — Kite-tracked positions + P/L",
                "`/kitestatus` — auto-trade config + stats",
                "`/kitereconcile` — compare tracked vs broker positions",
                "",
                "`/whoami` — show your user/channel IDs",
                "_Commands only work in the configured trade channel._",
            ]
        )
        await interaction.response.send_message(text, ephemeral=True)

    @bot.tree.command(name="health", description="Check screener health status")
    async def health(interaction):  # type: ignore
        """Check system health: processes, logs, data freshness, Discord."""
        ok, status_msg = health_monitor.check_all()
        emoji = "✅" if ok else "⚠️"
        await interaction.response.send_message(f"{emoji} **System Status**\n{status_msg}", ephemeral=True)

    @bot.tree.command(name="addcash", description="Add cash to the live portfolio")
    @app_commands.describe(amount="Amount of money added to the live account")
    async def addcash(interaction, amount: float):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            runner, _, positions_df, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                amount,
                None,
                None,
            )
            event_text = _format_event_lines("Cash:", runner.last_cash_events)
            message = "\n\n".join(
                part for part in [event_text, _format_summary_text(summary_df)] if part
            ) or "Cash added."
        except Exception as exc:
            message = f"⚠️ addcash failed: {exc}"
        await interaction.followup.send(message[:1900])

    # Guard against duplicate /buy submissions (symbol -> last submit time)
    _recent_buys: dict = {}

    @bot.tree.command(name="buy", description="Log a real buy trade into the live portfolio")
    @app_commands.describe(symbol="Stock symbol", qty="Quantity bought", price="Buy price per share")
    async def buy(interaction, symbol: str, qty: int, price: float):  # type: ignore
        import time
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        # Duplicate guard: reject same symbol within 60 seconds
        sym_key = symbol.strip().upper()
        now = time.monotonic()
        last_time = _recent_buys.get(sym_key, 0)
        if now - last_time < 60:
            await interaction.response.send_message(
                f"⚠️ Duplicate detected — `{sym_key}` was already submitted {int(now - last_time)}s ago. "
                f"If this is intentional, wait 60 seconds and try again.",
                ephemeral=True,
            )
            return
        _recent_buys[sym_key] = now

        await interaction.response.defer(thinking=True)
        try:
            runner, _, positions_df, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                0.0,
                [{"symbol": symbol, "quantity": qty, "price": price}],
                None,
            )
            # Tag this buy with blind-buy score for trade journal
            journal_line = ""
            try:
                journal_line = await asyncio.to_thread(
                    _tag_buy_with_blind_score, symbol, qty, price, account_name, universe
                )
            except Exception:
                pass
            event_text = _format_event_lines("Buy:", runner.last_buy_events)
            positions_text = _format_positions_text(runner, positions_df)
            parts = [part for part in [event_text, _format_summary_text(summary_df), positions_text] if part]
            if journal_line:
                parts.append(journal_line)
            message = "\n\n".join(parts) or f"Logged BUY {symbol} x{qty} @ {price}"
        except Exception as exc:
            message = f"⚠️ buy failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="sell", description="Log a real sell trade into the live portfolio")
    @app_commands.describe(symbol="Stock symbol", qty="Quantity sold", price="Sell price per share")
    async def sell(interaction, symbol: str, qty: int, price: float):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            runner, _, positions_df, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                0.0,
                None,
                [{"symbol": symbol, "quantity": qty, "price": price}],
            )
            # Log sell to trade journal with P&L
            journal_line = ""
            try:
                journal_line = await asyncio.to_thread(
                    _tag_sell_in_journal, symbol, qty, price, account_name
                )
            except Exception:
                pass
            event_text = _format_event_lines("Sell:", runner.last_sell_events)
            positions_text = _format_positions_text(runner, positions_df)
            parts = [part for part in [event_text, _format_summary_text(summary_df), positions_text] if part]
            if journal_line:
                parts.append(journal_line)
            message = "\n\n".join(parts) or f"Logged SELL {symbol} x{qty} @ {price}"
        except Exception as exc:
            message = f"⚠️ sell failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="portfolio", description="Show the current live portfolio summary")
    async def portfolio(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            runner, _, positions_df, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                0.0,
                None,
                None,
            )
            positions_text = _format_positions_text(runner, positions_df)
            message = "\n\n".join([_format_summary_text(summary_df), positions_text]) or "No portfolio data."
        except Exception as exc:
            message = f"⚠️ portfolio failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="pnl", description="Show booked, running, and total profit/loss")
    async def pnl(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            runner, trades_df, positions_df, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                0.0,
                None,
                None,
            )
            message = "\n\n".join(
                [
                    _format_pnl_text(summary_df),
                    _format_positions_text(runner, positions_df, limit=6),
                ]
            ) or "No P&L data."
        except Exception as exc:
            message = f"⚠️ pnl failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="history", description="Show recent live buys and sells")
    @app_commands.describe(limit="Maximum number of trade records to show")
    async def history(interaction, limit: Optional[int] = 8):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            _, trades_df, _, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                0.0,
                None,
                None,
            )
            header = _format_pnl_text(summary_df)
            history_text = _format_trade_history_text(trades_df, limit=limit or 8)
            message = "\n\n".join(part for part in [header, history_text] if part) or "No trade history."
        except Exception as exc:
            message = f"⚠️ history failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="buynow", description="Show the latest Strong Buy Now and Buy Now names from the signal monitor board")
    @app_commands.describe(limit="Maximum number of names to show")
    async def buynow(interaction, limit: Optional[int] = 5):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        rows_df = await asyncio.to_thread(_load_buynow_rows, universe, limit or 5)
        guardrail_text = _guardrail_text(account_name)
        signal_text = _format_signal_rows_text(
            rows_df,
            f"No qualifying buy signals right now (all filtered: score<50, vol<1x, R:R<1.5, or bearish).",
        )
        message = "\n\n".join(part for part in [guardrail_text, signal_text] if part)
        await interaction.response.send_message(message[:1900], ephemeral=True)

    @bot.tree.command(name="sellnow", description="Show the latest Sell / Exit names from the signal monitor board")
    @app_commands.describe(limit="Maximum number of names to show")
    async def sellnow(interaction, limit: Optional[int] = 5):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        rows_df = await asyncio.to_thread(_load_signal_rows, universe, "Sell / Exit", limit or 5)
        message = _format_signal_rows_text(
            rows_df,
            f"No `Sell / Exit` names are available in the latest `{universe}` monitor board yet.",
        )
        await interaction.response.send_message(message[:1900], ephemeral=True)

    @bot.tree.command(name="positions", description="Show current live positions")
    async def positions(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            runner, _, positions_df, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                0.0,
                None,
                None,
            )
            positions_text = _format_positions_text(runner, positions_df, limit=12)
            header = f"Cash Left: {_format_money(summary_df.iloc[0].get('Cash', 0.0))}" if summary_df is not None and not summary_df.empty else ""
            message = "\n\n".join(part for part in [header, positions_text] if part) or "No open positions."
        except Exception as exc:
            message = f"⚠️ positions failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="refresh", description="Refresh prices and signal guidance for open live positions")
    async def refresh(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            runner, _, positions_df, summary_df = await asyncio.to_thread(
                _execute_live_portfolio_action,
                config_manager,
                account_name,
                universe,
                0.0,
                None,
                None,
            )
            message = "\n\n".join([_format_summary_text(summary_df), _format_positions_text(runner, positions_df)]) or "No positions to refresh."
        except Exception as exc:
            message = f"⚠️ refresh failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="autopilot", description="Scan for blind-buy opportunities and check portfolio for sell alerts")
    async def autopilot_cmd(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)

        def _run_autopilot():
            from pkscreener.classes.AutoPilotInvestor import AutoPilotConfig, AutoPilotInvestor
            cfg = AutoPilotConfig(
                universe=universe,
                account_name=account_name,
                alert_channels="none",  # we'll format the response ourselves
            )
            investor = AutoPilotInvestor(config=cfg)
            return investor.run_once()

        result = await asyncio.to_thread(_run_autopilot)

        try:
            parts = ["**Blind-Buy Scan Results**"]

            if result.buy_candidates:
                parts.append(f"Found **{len(result.buy_candidates)}** opportunity{'s' if len(result.buy_candidates) != 1 else ''}:")
                for c in result.buy_candidates[:5]:
                    pct = f"+{c.change_pct:.1f}%" if c.change_pct > 0 else f"{c.change_pct:.1f}%"
                    parts.append(
                        f"**BUY `{c.symbol}`** (Score: {c.blind_score:.0f}/100)\n"
                        f"Price `{c.price:.2f}` ({pct}) | Buy Above `{c.buy_above:.2f}`\n"
                        f"Stop `{c.stop_loss:.2f}` | T1 `{c.target1:.2f}` | T2 `{c.target2:.2f}`\n"
                        f"Why: {c.reasons}"
                    )
            else:
                parts.append("No blind-buy opportunities right now.")

            if result.sell_alerts:
                parts.append(f"\n**SELL ALERTS ({len(result.sell_alerts)}):**")
                for a in result.sell_alerts[:5]:
                    emoji = "+" if a.pnl_pct >= 0 else ""
                    parts.append(
                        f"**SELL `{a.symbol}`** x{a.quantity} @ `{a.current_price:.2f}` "
                        f"({emoji}{a.pnl_pct:.1f}%) — {a.reason}"
                    )

            if result.buy_paused:
                parts.append(f"\nBuy paused: {'; '.join(result.pause_reasons)}")
            if result.error:
                parts.append(f"\nError: {result.error}")

            parts.append(f"\nPositions monitored: {result.open_positions}")
            message = "\n\n".join(parts)
        except Exception as exc:
            message = f"⚠️ autopilot failed: {exc}"

        await interaction.followup.send(message[:1900])

    # ------------------------------------------------------------------
    # Kite auto-trade commands
    # ------------------------------------------------------------------

    @bot.tree.command(name="kitepositions", description="Show positions tracked by Kite auto-trader")
    async def kitepositions(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        try:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager
            mgr = KiteOrderManager(output_dir=Archiver.get_user_data_dir())
            message = mgr.format_positions_summary()
        except Exception as exc:
            message = f"⚠️ kitepositions failed: {exc}"
        await interaction.response.send_message(message[:1900])

    @bot.tree.command(name="kitesell", description="Sell a Kite-tracked position via market order")
    @app_commands.describe(symbol="Stock symbol to sell")
    async def kitesell(interaction, symbol: str):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager
            mgr = KiteOrderManager(output_dir=Archiver.get_user_data_dir())

            # Snapshot system recommendation BEFORE executing the sell.
            # If the system wasn't flagging an exit, this is a manual override.
            _sym_upper = symbol.strip().upper()
            _sys_rec = ""
            _entry_price = 0.0
            _pos_before = mgr.get_position(_sym_upper)
            if _pos_before and _pos_before.status == "open":
                _entry_price = float(_pos_before.buy_price or 0)
                _stage = str(getattr(_pos_before, "swing_stage", "") or "")
                _trail = float(getattr(_pos_before, "trailing_stop", 0) or 0)
                _pnl_pct = (
                    (_pos_before.highest_price - _pos_before.buy_price) / _pos_before.buy_price * 100
                    if _pos_before.buy_price > 0 else 0
                )
                if _stage in ("CONFIRMED", "BREAKEVEN_PROTECTED", "RUNNER"):
                    _sys_rec = f"HOLD (stage={_stage}, trail={_trail:.2f}, peak_pnl={_pnl_pct:.1f}%)"
                elif _stage == "PARTIAL_BOOKED":
                    _sys_rec = f"HOLD_RUNNER (stage={_stage}, trail={_trail:.2f})"
                elif _stage in ("NEW", "PROTECTED"):
                    _sys_rec = f"HOLD (stage={_stage}, no trail yet)"
                # If stage is WEAKENING or empty, system may already agree — no override note

            result = mgr.place_sell_order(symbol=symbol, reason="manual_sell")
            if result.success:
                pos = mgr.get_position(_sym_upper)
                pnl_text = f" | P/L: ₹{pos.pnl:+,.0f}" if pos and pos.sell_price > 0 else ""
                message = f"✅ Sold `{_sym_upper}` x{result.qty} (ID: {result.order_id}){pnl_text}"

                # Log override if system recommendation was to hold
                if _sys_rec and "HOLD" in _sys_rec:
                    try:
                        import os
                        from pathlib import Path
                        from pkscreener.classes.LivePortfolio import LivePortfolioStore
                        _db = str(Path(Archiver.get_user_outputs_dir()) / "assistant_portfolio.db")
                        LivePortfolioStore(_db).log_override(
                            symbol=_sym_upper,
                            manual_action=f"MANUAL_SELL at {float(result.price or 0):.2f}",
                            system_recommendation=_sys_rec,
                            entry_price=_entry_price,
                            override_price=float(result.price or 0),
                        )
                    except Exception:
                        pass
            else:
                message = f"⚠️ Sell failed: {result.message}"
        except Exception as exc:
            message = f"⚠️ kitesell failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="kitebuy", description="Buy a stock via Kite auto-trader")
    @app_commands.describe(
        symbol="Stock symbol",
        amount="Approximate amount to invest (INR, default: max per trade setting)",
    )
    async def kitebuy(interaction, symbol: str, amount: Optional[float] = None):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager
            mgr = KiteOrderManager(output_dir=Archiver.get_user_data_dir())

            # Get live price from ticks
            ticks = mgr._load_ticks()
            tick = ticks.get(symbol.strip().upper(), {})
            live_price = float(tick.get("last_price", 0))
            if live_price <= 0:
                message = f"⚠️ No live price for `{symbol.upper()}` — is the ticker running?"
            else:
                invest = amount if amount and amount > 0 else mgr.max_order_value
                qty = max(1, int(invest / live_price))
                result = mgr.place_buy_order(
                    symbol=symbol, qty=qty, price=live_price, order_type="MARKET"
                )
                if result.success:
                    message = f"✅ Bought `{symbol.upper()}` x{qty} @ market (ID: {result.order_id})"
                else:
                    message = f"⚠️ Buy failed: {result.message}"
        except Exception as exc:
            message = f"⚠️ kitebuy failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="kitereconcile", description="Compare Kite broker positions with tracked positions")
    async def kitereconcile(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        await interaction.response.defer(thinking=True)
        try:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager
            mgr = KiteOrderManager(output_dir=Archiver.get_user_data_dir())
            report = await asyncio.to_thread(mgr.reconcile_with_broker)
            if report.get("error"):
                message = f"⚠️ Reconciliation error: {report['error']}"
            else:
                lines = [
                    f"**📊 Position Reconciliation**",
                    f"Broker positions: {report['broker_positions']}",
                    f"Tracked positions: {report['tracked_positions']}",
                ]
                if report.get("mismatches"):
                    lines.append("**Mismatches:**")
                    for m in report["mismatches"]:
                        lines.append(f"  ⚠️ {m}")
                if report.get("untracked"):
                    lines.append(f"**Untracked in broker:** {', '.join(report['untracked'])}")
                if not report.get("mismatches") and not report.get("untracked"):
                    lines.append("✅ All positions match!")
                message = "\n".join(lines)
        except Exception as exc:
            message = f"⚠️ reconcile failed: {exc}"
        await interaction.followup.send(message[:1900])

    @bot.tree.command(name="kitestatus", description="Show Kite auto-trade settings and daily stats")
    async def kitestatus(interaction):  # type: ignore
        ok, reason = _authorize(interaction)
        if not ok:
            await _reply_unauthorized(interaction, reason)
            return

        try:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager
            mgr = KiteOrderManager(output_dir=Archiver.get_user_data_dir())
            open_pos = mgr.get_open_positions()
            closed_pos = mgr.get_closed_positions()
            total_pnl = mgr.get_total_pnl()
            lines = [
                f"**⚙️ Kite Auto-Trade Status**",
                f"Auto-trade: {'✅ ON' if mgr.auto_trade else '❌ OFF'}",
                f"Dry run: {'🧪 YES' if mgr.dry_run else 'No'}",
                f"Max per order: ₹{mgr.max_order_value:,.0f}",
                f"Max daily orders: {mgr.max_daily_orders}",
                f"Max open positions: {mgr.max_open_positions}",
                f"Trailing stop: activates at +{mgr.trailing_stop_pct}%, trails at {mgr.trailing_trail_pct}%",
                f"Max hold days: {mgr.max_hold_days}",
                f"Open positions: {len(open_pos)}",
                f"Closed today: {len(closed_pos)}",
                f"Total booked P/L: ₹{total_pnl:+,.0f}",
            ]
            message = "\n".join(lines)
        except Exception as exc:
            message = f"⚠️ kitestatus failed: {exc}"
        await interaction.response.send_message(message[:1900])

    # ------------------------------------------------------------------
    # Natural-text buy-intent workflow
    # i.   "buy RELIANCE"  → records intent, replies with signal summary
    # ii.  "bought RELIANCE 10 1450"  → confirms buy, fetches Kite portfolio,
    #       logs trade, places GTT OCO (SL + target automatically)
    # iii. "cancel RELIANCE" → drops a pending intent
    # ------------------------------------------------------------------

    # symbol -> {"qty": int, "price": float, "stop_loss": float, "target": float}
    _buy_intents: Dict[str, Dict] = {}

    async def _handle_buy_intent(message, symbol: str):
        """Step i: user says 'buy SYMBOL [qty] [price]' — record intent, show signal."""
        symbol = symbol.strip().upper()
        parts = message.content.strip().split()
        # Optional: "buy RELIANCE 10 1450"
        qty = 0
        price = 0.0
        try:
            if len(parts) >= 3:
                qty = int(parts[2])
            if len(parts) >= 4:
                price = float(parts[3])
        except (ValueError, IndexError):
            pass

        # Look up signal data for SL/target
        try:
            from pkscreener.classes.DiscordTradeBot import _load_signal_rows
            rows = _load_signal_rows(universe, ["Strong Buy Now", "Buy Now", "Watch"], limit=50)
            row = rows[rows["Symbol"].astype(str).str.upper() == symbol] if not rows.empty else None
            if row is not None and not row.empty:
                row = row.iloc[0]
                if price <= 0:
                    price = float(row.get("LTP", row.get("Price Now", 0)) or 0)
                sl = float(row.get("SellIfBelow", row.get("StopLoss", row.get("Sell If Below", 0))) or 0)
                t2 = float(row.get("Target2", row.get("TakeProfit2", row.get("Take Profit 2", 0))) or 0)
                t1 = float(row.get("Target1", row.get("TakeProfit1", 0)) or 0)
                score = float(row.get("BlindScore", row.get("Score", 0)) or 0)
                vol = float(row.get("VolumeRatio", row.get("Volume Ratio", 0)) or 0)
            else:
                sl = t1 = t2 = score = vol = 0.0
        except Exception:
            sl = t1 = t2 = score = vol = 0.0

        _buy_intents[symbol] = {"qty": qty, "price": price, "stop_loss": sl, "target1": t1, "target": t2, "score": score}

        lines = [f"📋 **Intent recorded: BUY `{symbol}`**"]
        if price > 0:
            lines.append(f"Price ≈ ₹{price:.2f}")
        if sl > 0:
            lines.append(f"Stop Loss: ₹{sl:.2f}")
        if t2 > 0:
            lines.append(f"Target: ₹{t2:.2f}")
        if score > 0:
            lines.append(f"Score: {score:.0f}/100 | Vol {vol:.1f}x")
        lines.append(f'\nWhen you buy in Kite, type: `bought {symbol} {qty or "<qty>"} {price or "<price>"}`')
        lines.append("To cancel this intent: `cancel " + symbol + "`")
        await message.channel.send("\n".join(lines))

    async def _handle_bought_confirm(message, parts):
        """Step ii/iii: user says 'bought SYMBOL QTY PRICE' — log + GTT + reconcile."""
        # Parse: bought RELIANCE 10 1450.5
        if len(parts) < 4:
            await message.channel.send(
                "⚠️ Format: `bought SYMBOL QTY PRICE` — e.g. `bought RELIANCE 10 1450`"
            )
            return
        symbol = parts[1].strip().upper()
        try:
            qty = int(parts[2])
            price = float(parts[3])
        except (ValueError, IndexError):
            await message.channel.send("⚠️ Could not parse qty/price. Format: `bought SYMBOL QTY PRICE`")
            return

        intent = _buy_intents.pop(symbol, {})
        stop_loss = float(intent.get("stop_loss", 0))
        target = float(intent.get("target", 0))
        target1 = float(intent.get("target1", 0))
        score = float(intent.get("score", 0))

        reply_lines = [f"✅ **Confirmed: BUY `{symbol}` x{qty} @ ₹{price:.2f}**"]

        # Log trade to portfolio journal
        try:
            tag = await asyncio.to_thread(
                _tag_buy_with_blind_score, symbol, qty, price, account_name, universe
            )
            if tag:
                reply_lines.append(tag)
        except Exception as exc:
            reply_lines.append(f"_(Journal log failed: {exc})_")

        # Auto-place GTT OCO on Kite (stop-loss + target)
        gtt_placed = False
        if stop_loss > 0 and target > 0:
            try:
                from pkscreener.classes.KiteOrderManager import KiteOrderManager
                mgr = KiteOrderManager(output_dir=Archiver.get_user_data_dir())
                gtt = await asyncio.to_thread(
                    mgr.place_gtt_oco, symbol, qty, price, stop_loss, target
                )
                if gtt.get("success"):
                    reply_lines.append(
                        f"🔐 **GTT set automatically:**\n"
                        f"  SL ₹{stop_loss:.2f} (auto-sell if drops)\n"
                        f"  Target ₹{target:.2f} (auto-sell at profit)\n"
                        f"  _{gtt.get('message', '')}_"
                    )
                    gtt_placed = True
                else:
                    reply_lines.append(f"⚠️ GTT failed: {gtt.get('message', 'unknown error')}")
            except Exception as exc:
                reply_lines.append(f"⚠️ GTT error: {exc}")
        elif stop_loss <= 0 or target <= 0:
            reply_lines.append(
                "ℹ️ No SL/target in signal data. Set manually in Kite or use `/kitestatus` to confirm."
            )

        # Reconcile with Kite portfolio to confirm holding
        try:
            from pkscreener.classes.KiteOrderManager import KiteOrderManager
            mgr = KiteOrderManager(output_dir=Archiver.get_user_data_dir())
            rec = await asyncio.to_thread(mgr.reconcile_with_broker)
            kite_holdings = rec.get("broker_positions", []) or []
            found = any(
                str(p.get("tradingsymbol", "")).upper() == symbol
                for p in kite_holdings
            )
            if found:
                reply_lines.append(f"📊 Kite confirms you hold `{symbol}` ✅")
            else:
                reply_lines.append(
                    f"⚠️ Kite doesn't show `{symbol}` in holdings yet "
                    f"(may take a minute to settle — check `/kitereconcile`)"
                )
        except Exception as exc:
            reply_lines.append(f"_(Kite reconcile failed: {exc})_")

        await message.channel.send("\n".join(reply_lines)[:1900])

    @bot.event
    async def on_message(message):
        """Handle natural-text buy-intent commands alongside slash commands.

        Supported patterns (case-insensitive, in the configured trade channel):
          buy SYMBOL [qty] [price]   → record buy intent
          bought SYMBOL QTY PRICE    → confirm manual buy, auto-GTT, reconcile
          cancel SYMBOL              → drop pending intent
        """
        # Ignore messages from the bot itself
        if message.author == bot.user:
            return
        # Only act in the configured trade channel (same guard as slash commands)
        if allowed_channel_id and str(message.channel.id) != str(allowed_channel_id):
            return
        # Only act for the configured user (same guard as slash commands)
        if allowed_user_id and str(message.author.id) != str(allowed_user_id):
            return

        content = message.content.strip()
        lower = content.lower()
        parts = content.split()

        if not parts:
            return

        cmd = lower.split()[0]

        if cmd == "buy" and len(parts) >= 2:
            await _handle_buy_intent(message, parts[1])

        elif cmd == "bought" and len(parts) >= 2:
            await _handle_bought_confirm(message, parts)

        elif cmd == "cancel" and len(parts) >= 2:
            sym = parts[1].strip().upper()
            if sym in _buy_intents:
                _buy_intents.pop(sym)
                await message.channel.send(f"❌ Buy intent for `{sym}` cancelled.")
            else:
                await message.channel.send(f"Nothing pending for `{sym}`.")

        elif cmd == "intents":
            if _buy_intents:
                lines = ["**⏳ Pending buy intents:**"]
                for s, d in _buy_intents.items():
                    lines.append(f"  `{s}` — qty={d.get('qty') or '?'} price={d.get('price') or '?'}")
                await message.channel.send("\n".join(lines))
            else:
                await message.channel.send("No pending buy intents.")

    # Assign health_monitor to bot instance before starting
    bot.health_monitor = health_monitor

    try:
        await bot.start(token)
    except KeyboardInterrupt:
        await bot.close()
    except Exception as exc:
        default_logger().debug(f"Discord trade bot failed: {exc}", exc_info=True)
        OutputControls().printOutput(f"  [+] Discord trade bot stopped: {exc}")
