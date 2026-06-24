#!/usr/bin/python3
"""
    The MIT License (MIT)

    Copyright (c) 2023 pkjmesra

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

"""
# =============================================================================
# PKScreener CLI - Command Line Interface
# =============================================================================
# Pyinstaller compile Windows: pyinstaller --onefile --icon=screenshots\icon.ico pkscreener\pkscreenercli.py  --hidden-import cmath --hidden-import talib.stream --hidden-import numpy --hidden-import pandas --hidden-import alive_progress
# Pyinstaller compile Linux  : pyinstaller --onefile --icon=screenshots/icon.ico pkscreener/pkscreenercli.py  --hidden-import cmath --hidden-import talib.stream --hidden-import numpy --hidden-import pandas --hidden-import alive_progress

import warnings
warnings.simplefilter("ignore", UserWarning, append=True)

import asyncio
import argparse
import builtins
import datetime
import json
import logging
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Disable protobuf logging
os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'
os.environ['PROTOBUF_PYTHON_SILENT_WARNINGS'] = '1'
os.environ["PYTHONWARNINGS"] = "ignore::UserWarning"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["AUTOGRAPH_VERBOSITY"] = "0"
os.environ.setdefault("SUBSCRIPTION_ENABLED", "0")

import multiprocessing
sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def _auto_worker_count(env_name: str, default_cap: int = 16) -> int:
    """Pick a safe I/O-heavy scanner worker count without hard-capping at 3/8."""
    try:
        configured = int(os.environ.get(env_name, "0") or 0)
        if configured > 0:
            return max(1, configured)
    except Exception:
        pass
    cpu_count = os.cpu_count() or 2
    return max(4, min(default_cap, cpu_count * 2))

# try:
#     logging.getLogger("tensorflow").setLevel(logging.ERROR)
# except Exception:
#     pass
# =============================================================================
# PROTOBUF PATCH - MUST BE FIRST
# =============================================================================
"""
This patch fixes the 'GetPrototype' AttributeError in protobuf by monkey-patching
the MessageFactory class before any other code imports it.
"""
import sys
import os

# Set environment variables to reduce protobuf logging
os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'
os.environ['PROTOBUF_PYTHON_SILENT_WARNINGS'] = '1'

# Force import of protobuf modules first
try:
    import google.protobuf.message_factory
    
    # Patch MessageFactory class
    if hasattr(google.protobuf.message_factory, 'MessageFactory'):
        # Store the original __init__
        original_init = google.protobuf.message_factory.MessageFactory.__init__
        
        def patched_init(self, *args, **kwargs):
            """Patched __init__ that adds GetPrototype method"""
            original_init(self, *args, **kwargs)
            
            # Add GetPrototype if it doesn't exist
            if not hasattr(self, 'GetPrototype'):
                def get_prototype(self, descriptor):
                    """Add GetPrototype method to MessageFactory instances"""
                    try:
                        # Try the modern method
                        return self._GetPrototype(descriptor)
                    except AttributeError:
                        try:
                            # Try the older method
                            from google.protobuf import message_factory
                            return message_factory.GetMessageClass(descriptor)
                        except (ImportError, AttributeError):
                            # Ultimate fallback - create a dummy class
                            class DummyMessage:
                                DESCRIPTOR = descriptor
                                @classmethod
                                def FromString(cls, s):
                                    return cls()
                            return DummyMessage
                
                # Bind the method to the instance
                self.GetPrototype = get_prototype.__get__(self)
        
        # Apply the patch
        google.protobuf.message_factory.MessageFactory.__init__ = patched_init
        
        # Also patch the module-level function if needed
        if not hasattr(google.protobuf.message_factory, 'GetPrototype'):
            def module_get_prototype(descriptor):
                """Module-level GetPrototype function"""
                try:
                    return google.protobuf.message_factory.GetMessageClass(descriptor)
                except AttributeError:
                    # Create a dynamic message class
                    from google.protobuf import descriptor_pb2
                    from google.protobuf.message import Message
                    
                    class DynamicMessage(Message):
                        DESCRIPTOR = descriptor
                        
                        def __init__(self, **kwargs):
                            super().__init__(**kwargs)
                        
                        @classmethod
                        def FromString(cls, s):
                            return cls()
                    
                    return DynamicMessage
            
            google.protobuf.message_factory.GetPrototype = module_get_prototype
    
    # print("✓ Protobuf patched successfully", file=sys.stderr)
    
except ImportError:
    # protobuf not installed, nothing to patch
    pass
except Exception as e:
    print(f"⚠️ Protobuf patch warning: {e}", file=sys.stderr)

from time import sleep

# ── Load .env.dev (or .env) so Kite/Discord secrets are available ──
try:
    from dotenv import load_dotenv as _load_dotenv
    import pathlib as _pathlib
    _repo_root = _pathlib.Path(__file__).resolve().parent.parent
    _env_dev = _repo_root / ".env.dev"
    _env_file = _repo_root / ".env"
    if _env_dev.exists():
        _load_dotenv(_env_dev, override=True)
    elif _env_file.exists():
        _load_dotenv(_env_file, override=True)
except Exception:
    pass

from PKDevTools.classes import log as log
from PKDevTools.classes.ColorText import colorText
from PKDevTools.classes.log import default_logger
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.FunctionTimeouts import ping
from PKDevTools.classes.DebugConfig import DebugConfigManager

from pkscreener import Imports
from pkscreener.classes.MarketMonitor import MarketMonitor
from pkscreener.classes.PKAnalytics import PKAnalyticsService
import pkscreener.classes.ConfigManager as ConfigManager
from PKDevTools.classes import Archiver

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from unittest.mock import patch
    patch("multiprocessing.resource_tracker.register", lambda *args, **kwargs: None)


# =============================================================================
# ARGUMENT PARSER
# =============================================================================

class ArgumentParser:
    """Handles command line argument parsing for PKScreener."""
    
    @staticmethod
    def create_parser():
        """Create and configure the argument parser."""
        parser = argparse.ArgumentParser(
            description="PKScreener - Stock Screening Tool",
            formatter_class=argparse.RawDescriptionHelpFormatter
        )
        
        # Answer/Default options
        parser.add_argument(
            "-a", "--answerdefault",
            help="Pass default answer to questions/choices (Y/N)",
            required=False,
        )
        
        # Backtest options
        parser.add_argument(
            "--backtestdaysago",
            help="Run scanner for N days ago from today",
            required=False,
        )

        parser.add_argument(
            "--actionable",
            nargs="?",
            const="nifty50",
            help="Generate a local actionable BUY/SELL report (default universe: nifty50)",
            required=False,
        )
        parser.add_argument(
            "--actionablelimit",
            type=int,
            help="Maximum symbols to analyze for actionable report generation",
            required=False,
        )
        parser.add_argument(
            "--actionableminconfidence",
            type=float,
            help="Minimum confidence percentage for actionable rows",
            required=False,
        )
        parser.add_argument(
            "--actionablebudget",
            type=float,
            help="Optional one-stock budget used to estimate quantity in the simple actionable report",
            required=False,
        )
        parser.add_argument(
            "--actionableriskpct",
            type=float,
            help="Risk percent of the entered capital to use for quantity sizing in actionable mode",
            required=False,
        )
        parser.add_argument(
            "--actionableminliquidity",
            type=float,
            help="Minimum average daily traded value required for actionable Buy Now decisions",
            required=False,
        )
        parser.add_argument(
            "--actionableminvolratio",
            type=float,
            help="Minimum current volume vs 20-day average volume ratio for actionable Buy Now decisions",
            required=False,
        )
        parser.add_argument(
            "--actionableworkers",
            type=int,
            help="Number of parallel workers to use in actionable mode",
            required=False,
        )
        parser.add_argument(
            "--actionablelivekite",
            action="store_true",
            help="Merge the latest official Kite live ticks into actionable analysis when available",
            required=False,
        )
        parser.add_argument(
            "--actionablestrongbuyonly",
            action="store_true",
            help="Only promote the strictest buy setups into immediate buy signals in actionable mode",
            required=False,
        )
        parser.add_argument(
            "--signalmonitor",
            nargs="?",
            const="nifty50",
            help="Continuously monitor a universe and alert on fresh Buy Now / Sell Now changes",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorlimit",
            type=int,
            help="Maximum symbols to analyze for monitor mode",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorminconfidence",
            type=float,
            help="Minimum confidence percentage for Buy Now / Sell Now monitor decisions",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorbudget",
            type=float,
            help="Optional budget used to estimate suggested quantity in monitor mode",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorriskpct",
            type=float,
            help="Risk percent of the entered capital to use for monitor quantity sizing",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorminliquidity",
            type=float,
            help="Minimum average daily traded value required for monitor Buy Now decisions",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorminvolratio",
            type=float,
            help="Minimum current volume vs 20-day average volume ratio required for monitor Buy Now decisions",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorworkers",
            type=int,
            help="Number of parallel workers to use in signal monitor mode",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorlivekite",
            action="store_true",
            help="Merge the latest official Kite live ticks into signal monitor analysis when available",
            required=False,
        )
        parser.add_argument(
            "--signalmonitoralerts",
            help="Alert channel for monitor events: desktop, telegram, discord, both, or comma-separated combinations",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorautotune",
            action="store_true",
            help="Use the latest saved tuning profile for monitor thresholds when available",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorreset",
            action="store_true",
            help="Reset monitor state so current Buy Now / Sell Now names alert fresh on the next run",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorstrongbuyonly",
            action="store_true",
            help="Only alert on the strict Strong Buy Now tier for buy-side monitor signals",
            required=False,
        )
        parser.add_argument(
            "--signalmonitorstream",
            action="store_true",
            help="Experimental fast live mode: preload cached history and bootstrap from the previous board before periodic full refreshes",
            required=False,
        )
        parser.add_argument(
            "--signalmonitoraccount",
            help="Linked live account name used for buy-side guardrails in signal monitor mode (default: main)",
            required=False,
        )
        parser.add_argument(
            "--signalmonitormaxdailyloss",
            type=float,
            help="Pause new buy-side monitor ideas after this much realized loss in the linked live account for today",
            required=False,
        )
        parser.add_argument(
            "--signalmonitormaxconsecutivelosses",
            type=int,
            help="Pause new buy-side monitor ideas after this many consecutive losing closed trades in the linked live account",
            required=False,
        )
        parser.add_argument(
            "--signalscorecard",
            nargs="?",
            const="nifty50",
            help="Update and show the saved signal-performance scorecard for a universe",
            required=False,
        )
        parser.add_argument(
            "--signalscorecardlivekite",
            action="store_true",
            help="Use the latest official Kite live ticks while updating the signal scorecard",
            required=False,
        )
        parser.add_argument(
            "--dailytruth",
            nargs="?",
            const="today",
            help="Build the daily truth report for alerts, orders, costs, and benchmark comparison",
            required=False,
        )
        parser.add_argument(
            "--dailytruthuniverse",
            default="allstocks",
            help="Signal-performance universe to use for the daily truth report (default: allstocks)",
            required=False,
        )
        parser.add_argument(
            "--dailytruthaccount",
            default="main",
            help="Live account name used for engine-confirmed buy and Kite order logs (default: main)",
            required=False,
        )
        parser.add_argument(
            "--dailytruthfetchbenchmarks",
            action="store_true",
            help="Fetch missing benchmark history from Kite instead of using only cached benchmark data",
            required=False,
        )
        parser.add_argument(
            "--signalvalidate",
            nargs="?",
            const="nifty50",
            help="Replay the current signal logic over recent history for a universe",
            required=False,
        )
        parser.add_argument(
            "--signalvalidatedays",
            type=int,
            help="How many recent calendar days to validate over (default: 60)",
            required=False,
        )
        parser.add_argument(
            "--signalvalidatelimit",
            type=int,
            help="Maximum symbols to validate from the selected universe",
            required=False,
        )
        parser.add_argument(
            "--signalvalidateworkers",
            type=int,
            help="Parallel worker count for recent-history validation",
            required=False,
        )
        parser.add_argument(
            "--signalvalidateminconfidence",
            type=float,
            help="Minimum confidence threshold reused while replaying recent signals",
            required=False,
        )
        parser.add_argument(
            "--signalvalidateminliquidity",
            type=float,
            help="Minimum average traded value threshold reused while replaying recent signals",
            required=False,
        )
        parser.add_argument(
            "--signalvalidateminvolratio",
            type=float,
            help="Minimum volume-ratio threshold reused while replaying recent signals",
            required=False,
        )
        parser.add_argument(
            "--signaltune",
            nargs="?",
            const="nifty50",
            help="Tune live Buy Now thresholds from the latest recent-history validation ledger",
            required=False,
        )
        parser.add_argument(
            "--signaltuneminsamples",
            type=int,
            help="Minimum recent validation samples required while tuning thresholds",
            required=False,
        )
        parser.add_argument(
            "--papertrade",
            nargs="?",
            const="nifty50",
            help="Run a local paper-trading account (default universe: nifty50)",
            required=False,
        )
        parser.add_argument(
            "--papertradelimit",
            type=int,
            help="Maximum symbols to analyze for paper-trading candidate selection",
            required=False,
        )
        parser.add_argument(
            "--papertradecapital",
            type=float,
            help="Initial paper capital to use on the first paper-trading run",
            required=False,
        )
        parser.add_argument(
            "--papertrademaxpositions",
            type=int,
            help="Maximum simultaneous paper positions to keep open",
            required=False,
        )
        parser.add_argument(
            "--papertrademinconfidence",
            type=float,
            help="Minimum confidence required before opening a new paper position",
            required=False,
        )
        parser.add_argument(
            "--papertraderiskpct",
            type=float,
            help="Maximum percent of current cash to risk on any new paper trade",
            required=False,
        )
        parser.add_argument(
            "--papertrademinliquidity",
            type=float,
            help="Minimum average daily traded value required before a paper trade can auto-enter",
            required=False,
        )
        parser.add_argument(
            "--papertrademinvolratio",
            type=float,
            help="Minimum current volume vs 20-day average volume ratio before a paper trade can auto-enter",
            required=False,
        )
        parser.add_argument(
            "--papertradeworkers",
            type=int,
            help="Number of parallel workers to use in paper-trading scans",
            required=False,
        )
        parser.add_argument(
            "--papertradeaddcash",
            type=float,
            help="Add extra money to the existing paper account before the next run",
            required=False,
        )
        parser.add_argument(
            "--papertradealerts",
            help="Alert channel for paper-trading events: desktop, telegram, discord, both, or comma-separated combinations",
            required=False,
        )
        parser.add_argument(
            "--papertradebuy",
            help="Manual buy entries in SYMBOL:QTY:PRICE format, comma-separated",
            required=False,
        )
        parser.add_argument(
            "--papertradesell",
            help="Manual sell entries in SYMBOL:QTY:PRICE format, comma-separated",
            required=False,
        )
        parser.add_argument(
            "--papertradereset",
            action="store_true",
            help="Start a fresh paper account and ignore earlier paper positions",
            required=False,
        )

        # AutoPilot options
        parser.add_argument(
            "--autopilot",
            nargs="?",
            const="nifty50",
            help="Run hands-off autopilot investing mode (default universe: nifty50)",
            required=False,
        )
        parser.add_argument(
            "--autopilotaccount",
            help="Live account name for autopilot trades (default: main)",
            required=False,
        )
        parser.add_argument(
            "--autopilotminconfidence",
            type=float,
            help="Minimum signal confidence for autopilot buy decisions (default: 10.0)",
            required=False,
        )
        parser.add_argument(
            "--autopilotminliquidity",
            type=float,
            help="Minimum average daily traded value for autopilot buys (default: 10000000)",
            required=False,
        )
        parser.add_argument(
            "--autopilotminvolratio",
            type=float,
            help="Minimum volume ratio for autopilot buys (default: 1.0)",
            required=False,
        )
        parser.add_argument(
            "--autopilotmaxdailyloss",
            type=float,
            help="Pause autopilot buying after this much realized loss today (default: 0 = disabled)",
            required=False,
        )
        parser.add_argument(
            "--autopilotmaxconsecutivelosses",
            type=int,
            help="Pause autopilot buying after this many consecutive losing trades (default: 0 = disabled)",
            required=False,
        )
        parser.add_argument(
            "--autopilotalerts",
            help="Alert channels for autopilot events: discord, telegram, desktop, or comma-separated",
            required=False,
        )
        parser.add_argument(
            "--autopilotworkers",
            type=int,
            help="Number of parallel workers for autopilot analysis (default: 3)",
            required=False,
        )

        # Barometer option
        parser.add_argument(
            "--barometer",
            action="store_true",
            help="Send global market barometer to telegram channel or user",
            required=False,
        )
        
        # Bot options
        parser.add_argument(
            "--bot",
            action="store_true",
            help="Run only in telegram bot mode",
            required=False,
        )
        parser.add_argument(
            "--botavailable",
            action="store_true",
            help="Enforce bot availability status",
            required=False,
        )
        parser.add_argument(
            "--discordtradebot",
            action="store_true",
            help="Run the Discord live-portfolio bot",
            required=False,
        )
        parser.add_argument(
            "--discordtradeaccount",
            help="Account name used for live-trade files (default: main)",
            required=False,
        )
        parser.add_argument(
            "--discordtradeuniverse",
            help="Universe used to refresh live positions (default: allstocks)",
            required=False,
        )
        parser.add_argument(
            "--assistantstart",
            nargs="?",
            const="nifty50",
            help="Start the simplified live assistant stack in the background (default universe: nifty50)",
            required=False,
        )
        parser.add_argument(
            "--assistantstop",
            action="store_true",
            help="Stop the simplified live assistant stack",
            required=False,
        )
        parser.add_argument(
            "--assistantstatus",
            action="store_true",
            help="Show status for the simplified live assistant stack",
            required=False,
        )
        parser.add_argument(
            "--diskusagereport",
            action="store_true",
            help="Preview local disk usage, write paths, and storage cleanup candidates",
            required=False,
        )
        parser.add_argument(
            "--diskusageapply",
            action="store_true",
            help="Apply the storage cleanup/upload plan instead of only previewing it",
            required=False,
        )
        parser.add_argument(
            "--diskusagebucket",
            help="Override the archive bucket path for disk usage cleanup/reporting",
            required=False,
        )
        parser.add_argument(
            "--diskusagenotify",
            action="store_true",
            help="Send the disk usage report summary to DISCORD_WEBHOOK_URL",
            required=False,
        )
        parser.add_argument(
            "--assistantaccount",
            help="Account name used by the simplified live assistant (default: main)",
            required=False,
        )
        parser.add_argument(
            "--assistantbudget",
            type=float,
            help="Budget used for suggested quantity in the simplified live assistant",
            required=False,
        )
        parser.add_argument(
            "--assistantriskpct",
            type=float,
            help="Risk percent used for suggested quantity in the simplified live assistant",
            required=False,
        )
        parser.add_argument(
            "--assistantminconfidence",
            type=float,
            help="Minimum confidence needed before a live Buy Now alert is emitted in simplified mode",
            required=False,
        )
        parser.add_argument(
            "--assistantminliquidity",
            type=float,
            help="Minimum traded value required in simplified live mode",
            required=False,
        )
        parser.add_argument(
            "--assistantminvolratio",
            type=float,
            help="Minimum volume ratio required in simplified live mode",
            required=False,
        )
        parser.add_argument(
            "--assistantworkers",
            type=int,
            help="Worker count used by the simplified live assistant",
            required=False,
        )
        parser.add_argument(
            "--assistantinterval",
            type=int,
            help="Seconds between simplified live monitor refreshes (default: 30)",
            required=False,
        )
        parser.add_argument(
            "--assistantlimit",
            type=int,
            help="Optional symbol limit used by the simplified live monitor",
            required=False,
        )
        parser.add_argument(
            "--assistantalerts",
            help="Alert channels used by the simplified live assistant (default: discord)",
            required=False,
        )
        parser.add_argument(
            "--assistanttickerlimit",
            type=int,
            help="Optional symbol limit used by the Kite live ticker in simplified mode",
            required=False,
        )
        parser.add_argument(
            "--assistantprewarm",
            action="store_true",
            help="Run a Kite historical prewarm before starting the simplified live assistant",
            required=False,
        )
        parser.add_argument(
            "--assistantprewarmworkers",
            type=int,
            help="Parallel worker count for the optional simplified prewarm step",
            required=False,
        )
        parser.add_argument(
            "--assistantautotune",
            action="store_true",
            help="Use the latest saved tuning profile when starting the simplified live assistant",
            required=False,
        )
        parser.add_argument(
            "--assistantstrongbuyonly",
            action="store_true",
            help="Run the simplified live assistant in strict Strong Buy Now mode",
            required=False,
        )
        parser.add_argument(
            "--assistantmaxdailyloss",
            type=float,
            help="Pause new buy-side assistant ideas after this much realized loss in the linked live account for today",
            required=False,
        )
        parser.add_argument(
            "--assistantmaxconsecutivelosses",
            type=int,
            help="Pause new buy-side assistant ideas after this many consecutive losing closed trades in the linked live account",
            required=False,
        )
        parser.add_argument(
            "--assistantautopilot",
            action="store_true",
            help="Enable blind-buy autopilot alerts alongside the live assistant",
            required=False,
        )
        parser.add_argument(
            "--assistantautopilotinterval",
            type=int,
            help="Autopilot scan interval in seconds (default: 60)",
            required=False,
        )
        parser.add_argument(
            "--assistantinternalsupervisor",
            action="store_true",
            help=argparse.SUPPRESS,
            required=False,
        )
        parser.add_argument(
            "--assistantforeground",
            action="store_true",
            help="Run the simplified live assistant in the foreground for process supervisors like systemd",
            required=False,
        )
        parser.add_argument(
            "--assistantsessionid",
            type=int,
            help=argparse.SUPPRESS,
            required=False,
        )
        parser.add_argument(
            "--kiteconnectloginurl",
            action="store_true",
            help="Print the official Kite Connect login URL from KITE_API_KEY",
            required=False,
        )
        parser.add_argument(
            "--kiteconnectsession",
            help="Exchange an official Kite request token or full redirect URL into a saved access token",
            required=False,
        )
        parser.add_argument(
            "--kiteconnectdoctor",
            action="store_true",
            help="Check official Kite Connect configuration and saved session health",
            required=False,
        )
        parser.add_argument(
            "--kiteconnectprofile",
            action="store_true",
            help="Show the authenticated official Kite Connect profile",
            required=False,
        )
        parser.add_argument(
            "--kiteconnectclearsession",
            action="store_true",
            help="Delete the locally saved official Kite Connect session file",
            required=False,
        )
        parser.add_argument(
            "--kiteautologin",
            action="store_true",
            help="Perform automated TOTP login using KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET",
            required=False,
        )
        parser.add_argument(
            "--kitetickerstream",
            nargs="?",
            const="nifty50",
            help="Start an official KiteTicker live stream for a universe and save the latest full ticks locally",
            required=False,
        )
        parser.add_argument(
            "--kitetickerlimit",
            type=int,
            help="Maximum number of symbols to subscribe in official KiteTicker mode",
            required=False,
        )
        parser.add_argument(
            "--kitetickersaveinterval",
            type=float,
            help="Seconds between saving official KiteTicker snapshots to disk (default 0.5)",
            required=False,
        )
        parser.add_argument(
            "--kitetickerrunseconds",
            type=int,
            help="Optional timed run for the official KiteTicker stream, useful for testing",
            required=False,
        )
        parser.add_argument(
            "--kitetickerstatus",
            nargs="?",
            const="nifty50",
            help="Show the latest saved official KiteTicker status for a universe",
            required=False,
        )
        parser.add_argument(
            "--kiteprewarm",
            nargs="?",
            const="nifty50",
            help="Fetch and cache official Kite historical candles for a universe before market hours",
            required=False,
        )
        parser.add_argument(
            "--kiteprewarmlimit",
            type=int,
            help="Maximum symbols to prewarm in official Kite historical cache mode",
            required=False,
        )
        parser.add_argument(
            "--kiteprewarminterval",
            help="Historical candle interval for prewarm (default: current config duration)",
            required=False,
        )
        parser.add_argument(
            "--kiteprewarmworkers",
            type=int,
            help="Number of parallel workers to use while prewarming official Kite history",
            required=False,
        )
        parser.add_argument(
            "--kiteprewarmforce",
            action="store_true",
            help="Ignore existing prewarmed cache files and fetch official Kite history again",
            required=False,
        )

        # Beginner mode options
        parser.add_argument(
            "--beginner",
            nargs="?",
            const="swing",
            help="Run a curated beginner preset (swing, momentum, longterm)",
            required=False,
        )
        parser.add_argument(
            "--beginnerlist",
            action="store_true",
            help="List beginner presets and exit",
            required=False,
        )
        
        # Cron/Scheduling options
        parser.add_argument(
            "-c", "--croninterval",
            help="Interval in seconds between runs",
            required=False,
        )
        
        # Download option
        parser.add_argument(
            "-d", "--download",
            action="store_true",
            help="Only download stock data (no analysis)",
            required=False,
        )
        
        # Exit option
        parser.add_argument(
            "-e", "--exit",
            action="store_true",
            help="Exit after single execution",
            required=False,
        )
        
        # File options
        parser.add_argument(
            "--fname",
            help="File name with results contents",
            required=False,
        )
        
        # Force backtest option
        parser.add_argument(
            "--forceBacktestsForZeroResultDays",
            help="Force backtests even for zero-result days",
            action=argparse.BooleanOptionalAction,
        )
        
        # Intraday option
        parser.add_argument(
            "-i", "--intraday",
            help="Intraday candlestick duration (1m, 5m, 15m, 1h, etc.)",
            required=False,
        )
        
        # Monitor option
        parser.add_argument(
            "-m", "--monitor",
            help="Monitor for intraday scanners",
            nargs='?',
            const='X',
            type=str,
            required=False,
        )
        
        # Display options
        parser.add_argument(
            "--maxdisplayresults",
            help="Maximum results to display",
            required=False,
        )
        parser.add_argument(
            "--maxprice",
            help="Maximum stock price filter",
            required=False,
        )
        parser.add_argument(
            "--minprice",
            help="Minimum stock price filter",
            required=False,
        )
        
        # Options/Menu option
        parser.add_argument(
            "-o", "--options",
            help="Menu options in MainMenu:SubMenu:SubMenu format (e.g., X:12:10)",
            required=False,
        )
        
        # Build mode options
        parser.add_argument(
            "-p", "--prodbuild",
            action="store_true",
            help="Run in production-build mode",
            required=False,
        )
        parser.add_argument(
            "-t", "--testbuild",
            action="store_true",
            help="Run in test-build mode",
            required=False,
        )
        
        # Progress/Status options
        parser.add_argument(
            "--progressstatus",
            help="Progress status to display during scans",
            required=False,
        )
        parser.add_argument(
            "--runintradayanalysis",
            action="store_true",
            help="Run intraday analysis (morning vs EoD)",
            required=False,
        )
        
        # Simulation options
        parser.add_argument(
            "--simulate",
            type=json.loads,
            help='Simulate conditions (JSON format)',
            required=False,
        )
        parser.add_argument(
            "--singlethread",
            action="store_true",
            help="Run in single-threaded mode for debugging",
            required=False,
        )
        parser.add_argument(
            "--slicewindow",
            type=str,
            help="Time slice window (datetime with timezone)",
            required=False,
        )
        
        # Stock list option
        parser.add_argument(
            "--stocklist",
            type=str,
            help="Comma-separated list of stocks",
            required=False,
        )
        
        # System options
        parser.add_argument(
            "--systemlaunched",
            action="store_true",
            help="Indicate system-launched screener",
            required=False,
        )
        parser.add_argument(
            "--telegram",
            action="store_true",
            help="Run as telegram bot instance",
            required=False,
        )
        parser.add_argument(
            "--triggertimestamp",
            help="Trigger timestamp value",
            required=False,
        )
        
        # User options
        parser.add_argument(
            "-u", "--user",
            help="Telegram user ID for results",
            required=False,
        )
        parser.add_argument(
            "-l", "--log",
            action="store_true",
            help="Enable full logging",
            required=False,
        )
        parser.add_argument("-v", action="store_true")  # Pytest dummy arg
        
        # Piped options
        parser.add_argument(
            "--pipedtitle",
            help="Piped scan titles",
            required=False,
        )
        parser.add_argument(
            "--pipedmenus",
            help="Piped menu options",
            required=False,
        )
        parser.add_argument(
            "--usertag",
            help="User-defined tag values",
            required=False,
        )
        parser.add_argument(
            "--testalloptions",
            action="store_true",
            help="Test all menu options",
            required=False,
        )
        
        return parser


BEGINNER_PRESETS = {
    "overview": {
        "options": "X:1:0",
        "label": "Market Overview",
        "description": "Nifty 50 full screening with technical columns and minimal filtering.",
    },
    "swing": {
        "options": "X:1:1",
        "label": "Swing Breakouts",
        "description": "Nifty 50 probable breakouts for a simpler swing-trading watchlist.",
    },
    "momentum": {
        "options": "X:1:31",
        "label": "Momentum Leaders",
        "description": "Nifty 50 high-momentum names for trend-following ideas.",
    },
    "longterm": {
        "options": "X:1:44",
        "label": "Long-Term Strength",
        "description": "Nifty 50 strong buy signals as a starting shortlist for deeper research.",
    },
}


def _normalize_beginner_preset(value):
    """Normalize beginner preset aliases to a supported preset key."""
    if value is None:
        return None

    normalized = str(value).strip().lower()
    aliases = {
        "0": "overview",
        "1": "swing",
        "2": "momentum",
        "3": "longterm",
        "full": "overview",
        "overview_scan": "overview",
        "breakout": "swing",
        "swing_breakout": "swing",
        "strength": "longterm",
        "long_term": "longterm",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in BEGINNER_PRESETS else None


def _print_beginner_presets():
    """Print the curated beginner presets in a compact format."""
    OutputControls().printOutput("  [+] Beginner presets:")
    for index, key in enumerate(["overview", "swing", "momentum", "longterm"], start=0):
        preset = BEGINNER_PRESETS[key]
        OutputControls().printOutput(
            f"    {index}. {preset['label']} ({key}) => {preset['options']}"
        )
        OutputControls().printOutput(f"       {preset['description']}")


def _parse_manual_trade_specs(raw_value):
    """Parse comma-separated SYMBOL:QTY:PRICE manual trade specs."""
    if raw_value is None or str(raw_value).strip() == "":
        return []

    specs = []
    chunks = [chunk.strip() for chunk in str(raw_value).split(",") if chunk.strip()]
    for chunk in chunks:
        parts = [part.strip() for part in chunk.split(":")]
        if len(parts) != 3:
            raise ValueError(
                f"Invalid manual trade format '{chunk}'. Use SYMBOL:QTY:PRICE"
            )
        symbol, quantity_text, price_text = parts
        quantity = int(float(quantity_text))
        price = float(price_text)
        if quantity <= 0 or price <= 0:
            raise ValueError(
                f"Invalid manual trade values '{chunk}'. Quantity and price must be positive."
            )
        specs.append({"symbol": symbol.upper(), "quantity": quantity, "price": price})
    return specs


# =============================================================================
# OUTPUT CONTROL
# =============================================================================

class OutputController:
    """Controls output (stdout/stderr) for production mode."""
    
    _print_enabled = False
    _original_stdout = None
    _original__stdout = None
    _devnull_stdout = None
    _devnull__stdout = None
    
    @staticmethod
    def _decorator(func):
        """Decorator to conditionally execute print."""
        def new_func(*args, **kwargs):
            if OutputController._print_enabled:
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    default_logger().debug(e, exc_info=True)
        return new_func
    
    @classmethod
    def disable_output(cls, disable_input=True, disable=True):
        """Disable or enable system output."""
        cls._print_enabled = not disable
        
        if disable:
            if cls._original_stdout is None:
                builtins.print = cls._decorator(builtins.print)
                if disable_input:
                    builtins.input = cls._decorator(builtins.input)
                cls._original_stdout = sys.stdout
                cls._original__stdout = sys.__stdout__
            cls._devnull_stdout = open(os.devnull, "w")
            cls._devnull__stdout = open(os.devnull, "w")
            sys.stdout = cls._devnull_stdout
            sys.__stdout__ = cls._devnull__stdout
        else:
            try:
                # Close the devnull file handles, not the original stdout
                if hasattr(cls, '_devnull_stdout') and cls._devnull_stdout:
                    cls._devnull_stdout.close()
                if hasattr(cls, '_devnull__stdout') and cls._devnull__stdout:
                    cls._devnull__stdout.close()
            except Exception as e:
                default_logger().debug(e, exc_info=True)
            sys.stdout = cls._original_stdout if cls._original_stdout else sys.stdout
            sys.__stdout__ = cls._original__stdout if cls._original__stdout else sys.__stdout__


# =============================================================================
# LOGGER SETUP
# =============================================================================

class LoggerSetup:
    """Handles logging configuration for the application."""
    
    @staticmethod
    def get_log_file_path():
        """Get the path for the log file."""
        try:
            file_path = os.path.join(Archiver.get_user_data_dir(), "pkscreener-logs.txt")
            with open(file_path, "w") as f:
                f.write("Logger file for pkscreener!")
        except Exception:
            file_path = os.path.join(tempfile.gettempdir(), "pkscreener-logs.txt")
        return file_path
    
    @staticmethod
    def setup(should_log=False, trace=False):
        """Setup logging based on configuration."""
        if not should_log:
            if "PKDevTools_Default_Log_Level" in os.environ.keys():
                del os.environ['PKDevTools_Default_Log_Level']
            return
        
        log_file_path = LoggerSetup.get_log_file_path()
        
        if os.path.exists(log_file_path):
            try:
                os.remove(log_file_path)
            except Exception:
                pass
        
        OutputControls().printOutput(colorText.FAIL + "\n  [+] Logs will be written to:" + colorText.END)
        OutputControls().printOutput(colorText.GREEN + f"  [+] {log_file_path}" + colorText.END)
        OutputControls().printOutput(
            colorText.FAIL + "  [+] If you need to share, open this folder, copy and zip the log file to share.\n" + colorText.END
        )
        
        os.environ["PKDevTools_Default_Log_Level"] = str(log.logging.DEBUG)
        log.setup_custom_logger(
            "pkscreener",
            log.logging.DEBUG,
            trace=trace,
            log_file_path=log_file_path,
            filter=None,
        )


# =============================================================================
# DEPENDENCY CHECKER
# =============================================================================

class DependencyChecker:
    """Checks and warns about missing dependencies."""
    
    @staticmethod
    def warn_about_dependencies():
        """Check for required dependencies and warn if missing."""
        if not Imports["talib"]:
            OutputControls().printOutput(
                colorText.FAIL + "  [+] TA-Lib is not installed. Looking for pandas_ta_classic." + colorText.END
            )
            sleep(1)
            
            issue_link = "https://github.com/pkjmesra/PKScreener"
            issue_link = f"\x1b[97m\x1b]8;;{issue_link}\x1b\\{issue_link}\x1b]8;;\x1b\\\x1b[0m"
            
            if Imports["pandas_ta_classic"]:
                ta_link = "https://github.com/ta-lib/ta-lib-python"
                ta_link = f"\x1b[97m\x1b]8;;{ta_link}\x1b\\{ta_link}\x1b]8;;\x1b\\\x1b[0m"
                OutputControls().printOutput(
                    colorText.GREEN +
                    f"  [+] Found and falling back on pandas_ta_classic.\n"
                    f"  [+] For full coverage (candle patterns), read README: {issue_link}\n"
                    f"  [+] or follow instructions from {ta_link}" +
                    colorText.END
                )
                sleep(1)
            else:
                OutputControls().printOutput(
                    colorText.FAIL +
                    f"  [+] Neither ta-lib nor pandas_ta_classic found.\n"
                    f"  [+] Please follow instructions from README: {issue_link}" +
                    colorText.END
                )
                OutputControls().takeUserInput("Press any key to try anyway...")


# =============================================================================
# APPLICATION RUNNER
# =============================================================================

class ApplicationRunner:
    """Manages the main application execution flow."""
    
    def __init__(self, config_manager, args, arg_parser):
        """
        Initialize the application runner.
        
        Args:
            config_manager: Configuration manager instance
            args: Parsed command line arguments
            arg_parser: Argument parser instance
        """
        self.config_manager = config_manager
        self.args = args
        self.arg_parser = arg_parser
        self.results = None
        self.result_stocks = None
        self.plain_results = None
        self.db_timestamp = None
        self.elapsed_time = 0
        self.start_time = None
    
    def run(self):
        """Run the main application."""
        from pkscreener.globals import (
            main, sendQuickScanResult, sendMessageToTelegramChannel,
            sendGlobalMarketBarometer, updateMenuChoiceHierarchy, isInterrupted,
            refreshStockData, closeWorkersAndExit, resetUserMenuChoiceOptions,
            menuChoiceHierarchy
        )
        from pkscreener.classes.MenuOptions import (
            menus, PREDEFINED_SCAN_MENU_TEXTS,
            PREDEFINED_PIPED_MENU_ANALYSIS_OPTIONS, PREDEFINED_SCAN_MENU_VALUES
        )
        
        # Preserve piped args
        saved_piped_args = getattr(self.args, 'pipedmenus', None)
        
        # Re-parse args if needed
        self.args = self._refresh_args()
        self.args.pipedmenus = saved_piped_args
        
        # Setup user and timestamp
        self._setup_user_and_timestamp()
        
        # Handle options processing
        if self.args.options is not None:
            self.args.options = self.args.options.replace("::", ":").replace('"', "").replace("'", "")
            if str(self.args.options).upper().startswith("C") or "C:" in str(self.args.options).upper():
                self.args.runintradayanalysis = True
            self.args, _ = self._update_progress_status()
        
        # Route to appropriate handler
        if self.args.runintradayanalysis:
            self._run_intraday_analysis()
        elif self.args.testalloptions:
            self._test_all_options(menus, main)
        elif self.args.barometer:
            sendGlobalMarketBarometer(userArgs=self.args)
            sys.exit(0)
        else:
            self._run_standard_scan(main, closeWorkersAndExit, isInterrupted,
                                   updateMenuChoiceHierarchy, refreshStockData)
    
    def _refresh_args(self):
        """Refresh arguments from parser."""
        current_args = self.args
        args = _get_debug_args()
        if not isinstance(args, argparse.Namespace) and not hasattr(args, "side_effect"):
            argsv = self.arg_parser.parse_known_args(args=args)
            args = argsv[0]
        if args is not None and not args.exit and not args.monitor:
            argsv = self.arg_parser.parse_known_args()
            args = argsv[0]
        if current_args is not None and args is not None:
            preserved_attrs = [
                "actionable",
                "actionablebudget",
                "actionablelimit",
                "actionableminliquidity",
                "actionableminconfidence",
                "actionableminvolratio",
                "actionableriskpct",
                "actionableworkers",
                "answerdefault",
                "beginner",
                "discordtradeaccount",
                "discordtradebot",
                "discordtradeuniverse",
                "download",
                "exit",
                "intraday",
                "maxdisplayresults",
                "options",
                "signalmonitor",
                "signalmonitoralerts",
                "signalmonitorautotune",
                "signalmonitorbudget",
                "signalmonitorlimit",
                "signalmonitorminconfidence",
                "signalmonitorminliquidity",
                "signalmonitorminvolratio",
                "signalmonitorreset",
                "signalmonitorriskpct",
                "signalmonitorstrongbuyonly",
                "signalmonitoraccount",
                "signalmonitormaxdailyloss",
                "signalmonitormaxconsecutivelosses",
                "signalmonitorworkers",
                "signaltune",
                "signaltuneminsamples",
                "signalvalidate",
                "signalvalidatedays",
                "signalvalidatelimit",
                "signalvalidateminconfidence",
                "signalvalidateminliquidity",
                "signalvalidateminvolratio",
                "signalvalidateworkers",
                "assistantautotune",
                "assistantmaxdailyloss",
                "assistantmaxconsecutivelosses",
                "assistantstrongbuyonly",
                "papertrade",
                "papertradeaddcash",
                "papertradealerts",
                "papertradebuy",
                "papertradecapital",
                "papertradelimit",
                "papertrademinliquidity",
                "papertrademaxpositions",
                "papertrademinconfidence",
                "papertrademinvolratio",
                "papertraderiskpct",
                "papertradeworkers",
                "papertradesell",
                "papertradereset",
                "autopilot",
                "autopilotaccount",
                "autopilotminconfidence",
                "autopilotminliquidity",
                "autopilotminvolratio",
                "autopilotmaxdailyloss",
                "autopilotmaxconsecutivelosses",
                "autopilotalerts",
                "autopilotworkers",
                "progressstatus",
                "stocklist",
                "systemlaunched",
                "testbuild",
                "user",
            ]
            for attr in preserved_attrs:
                current_value = getattr(current_args, attr, None)
                parsed_value = getattr(args, attr, None)
                if current_value not in [None, "", False]:
                    if parsed_value in [None, "", False]:
                        setattr(args, attr, current_value)
        return args
    
    def _setup_user_and_timestamp(self):
        """Setup user ID and trigger timestamp."""
        if self.args.user is None:
            from PKDevTools.classes.Environment import PKEnvironment
            channel_id, _, _, _ = PKEnvironment().secrets
            if channel_id is not None and len(str(channel_id)) > 0:
                self.args.user = int(f"-{channel_id}")
        
        if self.args.triggertimestamp is None:
            self.args.triggertimestamp = int(PKDateUtilities.currentDateTimestamp())
        else:
            self.args.triggertimestamp = int(self.args.triggertimestamp)
        
        if self.args.systemlaunched and self.args.options is not None:
            self.args.systemlaunched = self.args.options
    
    def _update_progress_status(self, monitor_options=None):
        """Update progress status for display."""
        from pkscreener.classes.MenuOptions import (
            PREDEFINED_SCAN_MENU_TEXTS, PREDEFINED_SCAN_MENU_VALUES, INDICES_MAP
        )
        
        choices = ""
        try:
            if self.args.systemlaunched or monitor_options is not None:
                options_to_use = self.args.options if monitor_options is None else monitor_options
                choices = f"--systemlaunched -a y -e -o '{options_to_use.replace('C:', 'X:').replace('D:', '')}'"
                
                search_choices = choices
                for index_key in INDICES_MAP.keys():
                    if index_key.isnumeric():
                        search_choices = search_choices.replace(f"X:{index_key}:", "X:12:")
                
                index_num = PREDEFINED_SCAN_MENU_VALUES.index(search_choices)
                selected_index_option = choices.split(":")[1]
                choices = f"P_1_{str(index_num + 1)}_{str(selected_index_option)}" if ">|" in choices else choices
                self.args.progressstatus = f"  [+] {choices} => Running {choices}"
                self.args.usertag = PREDEFINED_SCAN_MENU_TEXTS[index_num]
                self.args.maxdisplayresults = 2000
        except:
            choices = ""
        return self.args, choices
    
    def _run_intraday_analysis(self):
        """Run intraday analysis reports."""
        from pkscreener.classes.cli.PKCliRunner import IntradayAnalysisRunner
        runner = IntradayAnalysisRunner(self.config_manager, self.args)
        runner.generate_reports()
    
    def _test_all_options(self, menus, main_func):
        """Test all menu options."""
        all_menus, _ = menus.allMenus(index=0)
        for scan_option in all_menus:
            self.args.options = f"{scan_option}:SBIN,"
            main_func(userArgs=self.args)
        sys.exit(0)
    
    def _run_standard_scan(self, main, close_workers, is_interrupted,
                          update_menu_hierarchy, refresh_data):
        """Run standard scanning mode."""
        from pkscreener.classes.cli.PKCliRunner import PKCliRunner
        
        cli_runner = PKCliRunner(self.config_manager, self.args)
        monitor_option_org = ""
        
        # Handle monitor mode
        if self.args.monitor:
            self._setup_monitor_mode(cli_runner, refresh_data)
            monitor_option_org = MarketMonitor().currentMonitorOption()
        
        # Run the scan
        try:
            self._execute_scan(main, close_workers, is_interrupted,
                              update_menu_hierarchy, cli_runner, monitor_option_org)
        except SystemExit:
            close_workers()
            _exit_gracefully(self.config_manager, self.arg_parser)
            sys.exit(0)
        except KeyboardInterrupt:
            close_workers()
            _exit_gracefully(self.config_manager, self.arg_parser)
            sys.exit(0)
        except Exception as e:
            default_logger().debug(e, exc_info=True)
            if self.args.log:
                traceback.print_exc()
    
    def _setup_monitor_mode(self, cli_runner, refresh_data):
        """Setup monitor mode."""
        self.args.monitor = self.args.monitor.replace("::", ":").replace('"', "").replace("'", "")
        self.config_manager.getConfig(ConfigManager.parser)
        self.args.answerdefault = self.args.answerdefault or 'Y'
        MarketMonitor().hiddenColumns = self.config_manager.alwaysHiddenDisplayColumns
        
        if MarketMonitor().monitorIndex == 0:
            self.db_timestamp = PKDateUtilities.currentDateTime().strftime("%H:%M:%S")
            self.elapsed_time = 0
            if self.start_time is None:
                self.start_time = time.time()
            else:
                self.elapsed_time = round(time.time() - self.start_time, 2)
                self.start_time = time.time()
    
    def _execute_scan(self, main, close_workers, is_interrupted,
                     update_menu_hierarchy, cli_runner, monitor_option_org):
        """Execute the scanning process."""
        self.results = None
        self.plain_results = None
        self.result_stocks = None
        
        if self.args is not None and ((self.args.options is not None and "|" in self.args.options) or self.args.systemlaunched):
            self.args.maxdisplayresults = 2000
        
        cli_runner.update_config_durations()
        cli_runner.update_config()
        
        self.results, self.plain_results = main(userArgs=self.args)
        
        # Handle piped menus
        if self.args.pipedmenus is not None:
            while self.args.pipedmenus is not None:
                self.args, _ = self._update_progress_status()
                self.results, self.plain_results = main(userArgs=self.args)
        
        if is_interrupted():
            close_workers()
            _exit_gracefully(self.config_manager, self.arg_parser)
            sys.exit(0)
        
        # Handle piped scans
        run_piped_scans = True
        while run_piped_scans:
            run_piped_scans = cli_runner.pipe_results(self.plain_results)
            if run_piped_scans:
                self.args, _ = self._update_progress_status()
                self.results, self.plain_results = main(userArgs=self.args)
            elif self.args is not None and self.args.pipedtitle is not None and "|" in self.args.pipedtitle:
                OutputControls().printOutput(
                    colorText.WARN +
                    f"  [+] Pipe Results Found: {self.args.pipedtitle}. "
                    f"{'Reduce number of piped scans if no stocks found.' if '[0]' in self.args.pipedtitle else ''}" +
                    colorText.END
                )
                if self.args.answerdefault is None:
                    OutputControls().takeUserInput("Press <Enter> to continue...")
        
        # Process results
        self._process_results(update_menu_hierarchy, monitor_option_org)
    
    def _process_results(self, update_menu_hierarchy, monitor_option_org):
        """Process scan results."""
        if self.plain_results is not None and not self.plain_results.empty:
            try:
                self.plain_results.set_index("Stock", inplace=True)
            except:
                pass
            try:
                self.results.set_index("Stock", inplace=True)
            except:
                pass
            self.plain_results = self.plain_results[~self.plain_results.index.duplicated(keep='first')]
            self.results = self.results[~self.results.index.duplicated(keep='first')]
            self.result_stocks = self.plain_results.index
            self._save_beginner_results()

        if self.args.monitor is not None:
            MarketMonitor().saveMonitorResultStocks(self.plain_results)
            if self.results is not None and len(monitor_option_org) > 0:
                chosen_menu = self.args.pipedtitle if self.args.pipedtitle is not None else update_menu_hierarchy()
                MarketMonitor().refresh(
                    screen_df=self.results,
                    screenOptions=monitor_option_org,
                    chosenMenu=chosen_menu[:120],
                    dbTimestamp=f"{self.db_timestamp} | CycleTime:{self.elapsed_time}s",
                    telegram=self.args.telegram
                )
                self.args.pipedtitle = ""
            
            # Check market close
            self._check_market_close()

    def _save_beginner_results(self):
        """Save a simple CSV snapshot for beginner-mode runs."""
        beginner_preset = getattr(self.args, "beginner", None)
        beginner_preset = _normalize_beginner_preset(beginner_preset)
        if beginner_preset is None or self.plain_results is None or self.plain_results.empty:
            return

        try:
            output_dir = Archiver.get_user_data_dir()
            timestamp = PKDateUtilities.currentDateTime().strftime("%Y%m%d_%H%M%S")
            latest_path = os.path.join(output_dir, f"beginner_{beginner_preset}_latest.csv")
            dated_path = os.path.join(output_dir, f"beginner_{beginner_preset}_{timestamp}.csv")

            export_df = self.plain_results.copy()
            export_df.to_csv(latest_path)
            export_df.to_csv(dated_path)

            OutputControls().printOutput(
                colorText.GREEN
                + f"  [+] Beginner results saved to {latest_path}"
                + colorText.END
            )
        except Exception as e:
            default_logger().debug(f"Failed saving beginner results: {e}", exc_info=True)
    
    def _check_market_close(self):
        """Check if market has closed and exit if needed."""
        if "RUNNER" in os.environ.keys() and self.args.triggertimestamp is not None:
            from datetime import timezone
            from PKDevTools.classes.MarketHours import MarketHours
            
            market_close_ts = PKDateUtilities.currentDateTime(
                simulate=True,
                hour=MarketHours().closeHour,
                minute=MarketHours().closeMinute
            ).replace(tzinfo=timezone.utc).timestamp()
            
            if (int(self.args.triggertimestamp) < int(market_close_ts) and
                int(PKDateUtilities.currentDateTimestamp()) >= market_close_ts):
                OutputControls().printOutput("Exiting monitor now since market has closed!", enableMultipleLineOutput=True)
                sys.exit(0)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _get_debug_args():
    """Get debug arguments from command line - fixed version."""
    import sys
    import shlex
    
    try:
        if args is not None:
            # If args is already set, use it
            if isinstance(args, str):
                # Split the string properly, respecting quotes
                return shlex.split(args)
            return list(args) if args else []
    except NameError:
        # Get from sys.argv
        args = sys.argv[1:]
        # If there's only one argument and it contains spaces, split it
        if len(args) == 1 and ' ' in args[0]:
            return shlex.split(args[0])
        return args
    except Exception:
        pass
    return []


def _exit_gracefully(config_manager, arg_parser):
    """Perform graceful exit cleanup."""
    try:
        from pkscreener.globals import resetConfigToDefault
        
        file_path = None
        try:
            file_path = os.path.join(Archiver.get_user_data_dir(), "monitor_outputs")
        except:
            pass
        
        if file_path is None:
            return
        
        # Clean up monitor output files
        index = 0
        while index < config_manager.maxDashboardWidgetsPerRow * config_manager.maxNumResultRowsInMonitor:
            try:
                os.remove(f"{file_path}_{index}.txt")
            except:
                pass
            index += 1
        
        # Reset config if needed
        argsv = arg_parser.parse_known_args()
        args = argsv[0]
        if args is not None and args.options is not None and not str(args.options).upper().startswith("T"):
            resetConfigToDefault(force=True)
        
        if "PKDevTools_Default_Log_Level" in os.environ.keys():
            if args is None or (args is not None and args.options is not None and "|" not in str(args.options)):
                del os.environ['PKDevTools_Default_Log_Level']
        
        config_manager.logsEnabled = False
        config_manager.setConfig(ConfigManager.parser, default=True, showFileCreatedText=False)
    except RuntimeError:
        OutputControls().printOutput(
            f"{colorText.WARN}If you're running from within docker, please run like this:{colorText.END}\n"
            f"{colorText.FAIL}docker run -it pkjmesra/pkscreener:latest\n{colorText.END}"
        )


def _remove_old_instances():
    """Remove old CLI instances."""
    import glob
    pattern = "pkscreenercli*"
    this_instance = sys.argv[0]
    for f in glob.glob(pattern, root_dir=os.getcwd(), recursive=True):
        file_to_delete = f if (os.sep in f and f.startswith(this_instance[:10])) else os.path.join(os.getcwd(), f)
        if not file_to_delete.endswith(this_instance):
            try:
                os.remove(file_to_delete)
            except:
                pass


# =============================================================================
# MAIN ENTRY POINTS
# =============================================================================

# Global state

args = None
argParser = ArgumentParser.create_parser()
configManager = ConfigManager.tools()

# Parse initial arguments
args = _get_debug_args()
argsv = argParser.parse_known_args(args=args) if args is not None else argParser.parse_known_args()
args = argsv[0]


def runApplication():
    """Run the main application."""
    global args
    runner = ApplicationRunner(configManager, args, argParser)
    runner.run()


def runApplicationForScreening():
    """Run application in screening mode."""
    from pkscreener.globals import closeWorkersAndExit
    
    try:
        has_cron_interval = args.croninterval is not None and str(args.croninterval).isnumeric()
        should_break = (args.exit and not has_cron_interval) or args.user is not None or args.testbuild
        
        if has_cron_interval:
            _schedule_next_run()
        else:
            runApplication()
        
        while True:
            if should_break:
                break
            if has_cron_interval:
                _schedule_next_run()
            else:
                runApplication()
        
        if args.v:
            OutputController.disable_output(disable=False)
            return
        
        closeWorkersAndExit()
        _exit_gracefully(configManager, argParser)
        sys.exit(0)
        
    except SystemExit:
        closeWorkersAndExit()
        _exit_gracefully(configManager, argParser)
        sys.exit(0)
    except (RuntimeError, Exception) as e:
        default_logger().debug(e, exc_info=True)
        if args.prodbuild:
            OutputController.disable_output(disable=False)
        OutputControls().printOutput(
            f"{e}\n  [+] An error occurred! Please run with '-l' option to collect the logs.\n"
            f"  [+] For example, 'pkscreener -l' and then contact the developer!"
        )
        if "RUNNER" in os.environ.keys() or ('PKDevTools_Default_Log_Level' in os.environ.keys() and
                                             os.environ["PKDevTools_Default_Log_Level"] != str(log.logging.NOTSET)):
            traceback.print_exc()
        
        if args.v:
            OutputController.disable_output(disable=False)
            return
        
        closeWorkersAndExit()
        _exit_gracefully(configManager, argParser)
        sys.exit(0)


_cron_runs = 0

def _schedule_next_run():
    """Schedule next run based on cron interval."""
    global _cron_runs
    
    sleep_until_next = not PKDateUtilities.isTradingTime()
    while sleep_until_next:
        OutputControls().printOutput(
            colorText.FAIL +
            f"SecondsAfterClosingTime[{int(PKDateUtilities.secondsAfterCloseTime())}] "
            f"SecondsBeforeMarketOpen [{int(PKDateUtilities.secondsBeforeOpenTime())}]. "
            f"Next run at [{PKDateUtilities.nextRunAtDateTime(bufferSeconds=3600, cronWaitSeconds=int(args.croninterval))}]" +
            colorText.END
        )
        
        if (PKDateUtilities.secondsAfterCloseTime() >= 3600 and
            PKDateUtilities.secondsAfterCloseTime() <= (3600 + 1.5 * int(args.croninterval))):
            sleep_until_next = False
        
        if (PKDateUtilities.secondsBeforeOpenTime() <= -3600 and
            PKDateUtilities.secondsBeforeOpenTime() >= (-3600 - 1.5 * int(args.croninterval))):
            sleep_until_next = False
        
        sleep(int(args.croninterval))
    
    if _cron_runs > 0:
        next_time = (PKDateUtilities.currentDateTime() + datetime.timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        OutputControls().printOutput(
            colorText.GREEN + f'=> Going to fetch again in {int(args.croninterval)} sec. at {next_time} IST...' + colorText.END,
            end="\r",
            flush=True,
        )
        sleep(int(args.croninterval) if not args.testbuild else 3)
    
    runApplication()
    _cron_runs += 1


@ping(interval=60, instance=PKAnalyticsService())
def pkscreenercli():
    """Main CLI entry point."""
    global args
    
    # Setup multiprocessing for macOS
    if sys.platform.startswith("darwin"):
        try:
            multiprocessing.set_start_method("fork")
        except RuntimeError as e:
            if "RUNNER" not in os.environ.keys() and ('PKDevTools_Default_Log_Level' in os.environ.keys() and
                                                      os.environ["PKDevTools_Default_Log_Level"] != str(log.logging.NOTSET)):
                OutputControls().printOutput("  [+] RuntimeError with 'multiprocessing'.\n  [+] Please contact the Developer!")
                OutputControls().printOutput(e)
                traceback.print_exc()
    
    try:
        debug_config_path = os.path.join(Archiver.get_user_data_dir(), "debug_config.ini")
        if os.path.exists(debug_config_path) and os.path.isfile(debug_config_path):
            manager = DebugConfigManager()
            config = manager.load_from_file(debug_config_path)
        _remove_old_instances()
        OutputControls(
            enableMultipleLineOutput=(args is None or args.monitor is None or args.runintradayanalysis),
            enableUserInput=(args is None or args.answerdefault is None)
        ).printOutput("", end="\r")
        
        configManager.getConfig(ConfigManager.parser)

        if not configManager.checkConfigFile():
            configManager.setConfig(ConfigManager.parser, default=True, showFileCreatedText=False)

        if args.assistantinternalsupervisor:
            from pkscreener.classes.AssistantLiveRuntime import AssistantLiveSupervisor

            if args.assistantsessionid is None:
                OutputControls().printOutput(
                    colorText.FAIL
                    + "  [+] Internal live supervisor needs --assistantsessionid."
                    + colorText.END
                )
                sys.exit(1)
            supervisor = AssistantLiveSupervisor(config_manager=configManager, session_id=args.assistantsessionid)
            supervisor.run()
            sys.exit(0)

        if args.diskusagereport:
            from pkscreener.classes.StorageManager import StorageManager

            manager = StorageManager(
                Path(__file__).resolve().parents[1],
                bucket=str(args.diskusagebucket or "").strip(),
            )
            summary = manager.run(
                apply_changes=bool(args.diskusageapply),
                notify=bool(args.diskusagenotify),
            )
            OutputControls().printOutput(
                manager.format_text_report(summary, apply_changes=bool(args.diskusageapply))
            )
            OutputControls().printOutput(f"  [+] Report written to: {summary.report_path}")
            sys.exit(0)

        if args.assistantstatus or args.assistantstop or args.assistantstart is not None:
            from pkscreener.classes.AssistantLiveRuntime import (
                AssistantLiveLauncher,
                AssistantLiveSettings,
            )

            launcher = AssistantLiveLauncher()

            if args.assistantstatus:
                launcher.print_status()
                sys.exit(0)

            if args.assistantstop:
                stopped_session = launcher.stop()
                if stopped_session is None:
                    OutputControls().printOutput("  [+] No live assistant session is running right now.")
                else:
                    OutputControls().printOutput(
                        colorText.GREEN
                        + f"  [+] Stopped live assistant session {stopped_session}."
                        + colorText.END
                    )
                sys.exit(0)

            if args.assistantstart is not None:
                tuned_profile = None
                if args.assistantautotune:
                    from pkscreener.classes.SignalThresholdTuner import SignalThresholdTuner

                    tuned_profile = SignalThresholdTuner.load_latest_profile(
                        str(args.assistantstart or "nifty50").strip().lower()
                    )
                    if tuned_profile is not None:
                        OutputControls().printOutput(
                            colorText.GREEN
                            + (
                                "  [+] Applying tuned live profile: "
                                f"confidence>={tuned_profile.get('min_confidence')}, "
                                f"volume>={tuned_profile.get('min_volume_ratio')}, "
                                f"liquidity>={tuned_profile.get('min_trade_value'):.0f}"
                            )
                            + colorText.END
                        )
                    else:
                        OutputControls().printOutput(
                            "  [+] No saved tuning profile found. Using the configured live thresholds."
                        )

                settings = AssistantLiveSettings(
                    universe=str(args.assistantstart or "nifty50").strip().lower(),
                    account_name=str(args.assistantaccount or "main").strip().lower(),
                    budget_amount=(args.assistantbudget if args.assistantbudget is not None else 10000.0),
                    risk_percent=(args.assistantriskpct if args.assistantriskpct is not None else 1.0),
                    min_confidence=(
                        args.assistantminconfidence
                        if args.assistantminconfidence is not None
                        else (tuned_profile.get("min_confidence") if tuned_profile is not None else 15.0)
                    ),
                    min_trade_value=(
                        args.assistantminliquidity
                        if args.assistantminliquidity is not None
                        else (tuned_profile.get("min_trade_value") if tuned_profile is not None else 10000000.0)
                    ),
                    min_volume_ratio=(
                        args.assistantminvolratio
                        if args.assistantminvolratio is not None
                        else (tuned_profile.get("min_volume_ratio") if tuned_profile is not None else 1.0)
                    ),
                    max_workers=(
                        args.assistantworkers
                        if args.assistantworkers is not None
                        else _auto_worker_count("ASSISTANT_WORKERS", 16)
                    ),
                    signal_interval=(args.assistantinterval if args.assistantinterval is not None else 10),
                    signal_limit=(args.assistantlimit if args.assistantlimit is not None else 0),
                    alert_channels=str(args.assistantalerts or "discord").strip().lower(),
                    ticker_limit=(args.assistanttickerlimit if args.assistanttickerlimit is not None else 0),
                    ticker_save_interval=2,
                    prewarm=bool(args.assistantprewarm),
                    prewarm_workers=(args.assistantprewarmworkers if args.assistantprewarmworkers is not None else 4),
                    strong_buy_only=bool(args.assistantstrongbuyonly),
                    max_daily_loss_amount=(
                        args.assistantmaxdailyloss
                        if args.assistantmaxdailyloss is not None
                        else float(
                            os.environ.get(
                                "ASSISTANT_MAX_DAILY_LOSS",
                                os.environ.get("AUTOPILOT_MAX_DAILY_LOSS", "0"),
                            )
                            or 0
                        )
                    ),
                    max_consecutive_losses=(
                        args.assistantmaxconsecutivelosses
                        if args.assistantmaxconsecutivelosses is not None
                        else int(
                            os.environ.get(
                                "ASSISTANT_MAX_CONSECUTIVE_LOSSES",
                                os.environ.get("AUTOPILOT_MAX_CONSECUTIVE_LOSSES", "0"),
                            )
                            or 0
                        )
                    ),
                    autopilot_enabled=bool(getattr(args, 'assistantautopilot', False)),
                    autopilot_interval=(
                        args.assistantautopilotinterval
                        if getattr(args, 'assistantautopilotinterval', None) is not None
                        else 60
                    ),
                )
                try:
                    if args.assistantforeground:
                        OutputControls().printOutput(
                            colorText.GREEN
                            + f"  [+] Starting live assistant in foreground mode for {settings.universe}."
                            + colorText.END
                        )
                        launcher.run_foreground(settings, configManager)
                        sys.exit(0)
                    session_id = launcher.start(settings)
                except Exception as exc:
                    OutputControls().printOutput(
                        colorText.FAIL
                        + f"  [+] Could not start the live assistant: {exc}"
                        + colorText.END
                    )
                    sys.exit(1)

                OutputControls().printOutput(
                    colorText.GREEN
                    + f"  [+] Started live assistant session {session_id} for {settings.universe}."
                    + colorText.END
                )
                OutputControls().printOutput(
                    "  [+] Use '--assistantstatus' to check health and '--assistantstop' to stop everything."
                )
                launcher.print_status()
                sys.exit(0)

        if args.beginnerlist:
            _print_beginner_presets()
            sys.exit(0)
        
        # Validate TOS acceptance
        from pkscreener.classes.cli.PKCliRunner import CliConfigManager
        cli_config = CliConfigManager(configManager, args)
        if not cli_config.validate_tos_acceptance():
            sys.exit(0)
        
        # Setup configuration
        try:
            from pkscreener.classes import VERSION
            del os.environ['PKDevTools_Default_Log_Level']
        except:
            pass
        
        configManager.logsEnabled = False
        configManager.tosAccepted = True
        from pkscreener.classes import VERSION
        configManager.appVersion = VERSION
        configManager.setConfig(ConfigManager.parser, default=True, showFileCreatedText=False)
        
        import atexit
        atexit.register(lambda: _exit_gracefully(configManager, argParser))
        
        # Set trigger timestamp
        if args.triggertimestamp is None:
            args.triggertimestamp = int(PKDateUtilities.currentDateTimestamp())
        else:
            args.triggertimestamp = int(args.triggertimestamp)
        
        # Setup monitor if requested
        if args.monitor is not None:
            from pkscreener.classes.MenuOptions import NA_NON_MARKET_HOURS
            configured_options = (configManager.defaultMonitorOptions.split("~")
                                if len(configManager.myMonitorOptions) < 1
                                else configManager.myMonitorOptions.split("~"))
            
            for option in NA_NON_MARKET_HOURS:
                if option in configured_options and not PKDateUtilities.isTradingTime():
                    configured_options.remove(option)
            
            MarketMonitor(
                monitors=args.monitor.split("~") if len(args.monitor) > 5 else configured_options,
                maxNumResultsPerRow=configManager.maxDashboardWidgetsPerRow,
                maxNumColsInEachResult=6,
                maxNumRowsInEachResult=10,
                maxNumResultRowsInMonitor=configManager.maxNumResultRowsInMonitor,
                pinnedIntervalWaitSeconds=configManager.pinnedMonitorSleepIntervalSeconds,
                alertOptions=configManager.soundAlertForMonitorOptions.split("~")
            )
        
        # Setup logging
        if args.log or configManager.logsEnabled:
            LoggerSetup.setup(should_log=True, trace=args.testbuild)
            if not args.prodbuild and args.answerdefault is None:
                try:
                    OutputControls().takeUserInput("Press <Enter> to continue...")
                except EOFError:
                    OutputControls().printOutput(
                        f"{colorText.WARN}If you're running from within docker, please run like this:{colorText.END}\n"
                        f"{colorText.FAIL}docker run -it pkjmesra/pkscreener:latest\n{colorText.END}"
                    )
        else:
            if "PKDevTools_Default_Log_Level" in os.environ.keys():
                del os.environ['PKDevTools_Default_Log_Level']
        
        # Handle simulation
        if args.simulate:
            os.environ["simulation"] = json.dumps(args.simulate)
        elif "simulation" in os.environ.keys():
            del os.environ['simulation']
        
        # Import dependencies
        from pkscreener.classes import Utility, ConsoleUtility
        
        configManager.default_logger = default_logger()
        
        if OutputController._original_stdout is None:
            ConsoleUtility.PKConsoleTools.clearScreen(userArgs=args, clearAlways=True)
        
        DependencyChecker.warn_about_dependencies()

        beginner_preset = _normalize_beginner_preset(args.beginner)
        if args.beginner is not None and beginner_preset is None:
            OutputControls().printOutput(
                colorText.FAIL
                + "  [+] Unknown beginner preset. Use --beginnerlist to see valid choices."
                + colorText.END
            )
            sys.exit(1)
        if beginner_preset is not None:
            preset = BEGINNER_PRESETS[beginner_preset]
            if args.options is None or str(args.options).strip() in ["", "0"]:
                args.options = preset["options"]
            if args.answerdefault is None:
                args.answerdefault = "Y"
            if not args.exit:
                args.exit = True
            if not args.systemlaunched:
                args.systemlaunched = args.options
            OutputControls().printOutput(
                colorText.GREEN
                + f"  [+] Beginner Mode: {preset['label']} => {preset['options']}"
                + colorText.END
            )
            OutputControls().printOutput(f"  [+] {preset['description']}")

        # Handle production mode
        if args.prodbuild:
            if args.options and len(args.options.split(":")) > 0:
                do_not_disable = any(f":{i}:30:" in args.options for i in range(16))
                if not do_not_disable:
                    OutputController.disable_output()
            else:
                OutputController.disable_output()
        
        if (
            args.kiteconnectloginurl
            or args.kiteconnectsession
            or args.kiteconnectdoctor
            or args.kiteconnectprofile
            or args.kiteconnectclearsession
            or args.kiteautologin
        ):
            from pkscreener.classes.KiteConnectManager import (
                KiteConnectSessionManager,
                format_connection_report,
            )

            manager = KiteConnectSessionManager()

            if args.kiteconnectclearsession:
                removed = manager.clear_saved_session()
                if removed:
                    OutputControls().printOutput(
                        colorText.GREEN
                        + f"  [+] Removed saved official Kite session file: {manager.session_file_path}"
                        + colorText.END
                    )
                else:
                    OutputControls().printOutput(
                        colorText.WARN
                        + f"  [+] No saved official Kite session file was found at: {manager.session_file_path}"
                        + colorText.END
                    )

            if args.kiteconnectloginurl:
                try:
                    login_url = manager.get_login_url()
                    OutputControls().printOutput("  [+] Official Kite Connect login URL:")
                    OutputControls().printOutput(login_url)
                    OutputControls().printOutput(
                        "  [+] After login, copy the full redirect URL from the browser and pass it to --kiteconnectsession."
                    )
                except Exception as exc:
                    OutputControls().printOutput(
                        colorText.FAIL
                        + f"  [+] Could not build the official Kite login URL: {exc}"
                        + colorText.END
                    )
                    sys.exit(1)

            if args.kiteconnectsession:
                try:
                    payload = manager.create_session(args.kiteconnectsession)
                    session = payload.get("session", {}) or {}
                    profile = payload.get("profile", {}) or {}
                    OutputControls().printOutput(
                        colorText.GREEN
                        + "  [+] Saved official Kite Connect session locally."
                        + colorText.END
                    )
                    OutputControls().printOutput(f"  [+] Session file: {manager.session_file_path}")
                    OutputControls().printOutput(
                        f"  [+] User: {profile.get('user_name') or session.get('user_name') or session.get('user_id') or 'Unknown'}"
                    )
                except Exception as exc:
                    OutputControls().printOutput(
                        colorText.FAIL
                        + f"  [+] Failed to create the official Kite session: {exc}"
                        + colorText.END
                    )
                    sys.exit(1)

            if args.kiteconnectdoctor:
                report = manager.get_connection_report()
                OutputControls().printOutput("  [+] Official Kite Connect doctor")
                OutputControls().printOutput(format_connection_report(report))
                if report.get("error"):
                    sys.exit(1)

            if args.kiteconnectprofile:
                try:
                    profile = manager.get_profile()
                    pretty_profile = json.dumps(profile, indent=2, default=str)
                    OutputControls().printOutput("  [+] Official Kite Connect profile")
                    OutputControls().printOutput(pretty_profile)
                except Exception as exc:
                    OutputControls().printOutput(
                        colorText.FAIL
                        + f"  [+] Could not load the official Kite profile: {exc}"
                        + colorText.END
                    )
                    sys.exit(1)

            if args.kiteautologin:
                if not manager.can_auto_login():
                    OutputControls().printOutput(
                        colorText.FAIL
                        + "  [!] Auto-login requires KITE_USER_ID, KITE_PASSWORD, and KITE_TOTP_SECRET."
                        + colorText.END
                    )
                    sys.exit(1)
                try:
                    payload = manager.auto_login()
                    profile = payload.get("profile", {}) or {}
                    OutputControls().printOutput(
                        colorText.GREEN
                        + "  [+] TOTP auto-login successful!"
                        + colorText.END
                    )
                    OutputControls().printOutput(
                        f"  [+] User: {profile.get('user_name') or payload.get('user_id') or 'authenticated'}"
                    )
                    OutputControls().printOutput(f"  [+] Session file: {manager.session_file_path}")
                except Exception as exc:
                    OutputControls().printOutput(
                        colorText.FAIL
                        + f"  [!] TOTP auto-login failed: {exc}"
                        + colorText.END
                    )
                    sys.exit(1)

            sys.exit(0)

        if args.kitetickerstatus is not None:
            from pkscreener.classes.KiteTickerStream import read_kiteticker_status

            status_universe = str(args.kitetickerstatus or "nifty50").strip().lower()
            status = read_kiteticker_status(status_universe)
            if not status:
                OutputControls().printOutput(
                    colorText.WARN
                    + f"  [+] No saved official KiteTicker status was found for {status_universe}."
                    + colorText.END
                )
                sys.exit(1)
            OutputControls().printOutput("  [+] Official KiteTicker status")
            OutputControls().printOutput(json.dumps(status, indent=2, default=str))
            sys.exit(0)

        if args.kitetickerstream is not None:
            from pkscreener.classes.KiteTickerStream import KiteTickerStreamRunner

            ticker_universe = str(args.kitetickerstream or "nifty50").strip().lower()
            ticker_limit = args.kitetickerlimit if args.kitetickerlimit is not None else None
            ticker_save_interval = (
                args.kitetickersaveinterval
                if args.kitetickersaveinterval is not None
                else 2
            )
            ticker_run_seconds = (
                args.kitetickerrunseconds
                if args.kitetickerrunseconds is not None
                else None
            )
            runner = KiteTickerStreamRunner(
                config_manager=configManager,
                universe=ticker_universe,
                limit=ticker_limit,
                save_interval_seconds=ticker_save_interval,
                run_seconds=ticker_run_seconds,
            )
            runner.run()
            sys.exit(0)

        if args.kiteprewarm is not None:
            from pkscreener.classes.ActionableReport import ActionableReportRunner
            from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider

            prewarm_universe = str(args.kiteprewarm or "nifty50").strip().lower()
            prewarm_interval = str(
                args.kiteprewarminterval
                or getattr(configManager, "duration", "1d")
                or "1d"
            ).strip()
            prewarm_workers = (
                args.kiteprewarmworkers
                if args.kiteprewarmworkers is not None
                else 4
            )
            prewarm_runner = ActionableReportRunner(
                config_manager=configManager,
                universe=prewarm_universe,
                limit=(args.kiteprewarmlimit or 0),
            )
            symbols = prewarm_runner.get_universe_symbols()
            if args.kiteprewarmlimit and args.kiteprewarmlimit > 0:
                symbols = symbols[: int(args.kiteprewarmlimit)]

            if not symbols:
                OutputControls().printOutput(
                    colorText.FAIL
                    + f"  [+] No symbols were found for prewarm universe {prewarm_universe}."
                    + colorText.END
                )
                sys.exit(1)

            OutputControls().printOutput(
                f"  [+] Prewarming official Kite history for {len(symbols)} symbols from {prewarm_universe}..."
            )
            provider = KiteHistoricalDataProvider()
            try:
                provider.session_manager.ensure_fresh_session()
            except Exception as exc:
                OutputControls().printOutput(
                    colorText.FAIL
                    + f"  [+] Kite session preflight failed before prewarm: {exc}"
                    + colorText.END
                )
                OutputControls().printOutput(
                    colorText.FAIL
                    + "  [+] Prewarm aborted before touching cache files."
                    + colorText.END
                )
                sys.exit(1)
            history_count = prewarm_runner._estimate_history_count()
            summary_df = provider.prewarm_symbols(
                symbols=symbols,
                interval=prewarm_interval,
                count=history_count,
                max_workers=prewarm_workers,
                force_refresh=args.kiteprewarmforce,
            )

            summary_path = os.path.join(
                Archiver.get_user_data_dir(),
                f"kiteprewarm_{prewarm_universe}_{provider._normalize_interval(prewarm_interval)}_latest.csv",
            )
            summary_df.to_csv(summary_path, index=False)

            ok_count = int((summary_df["Status"] == "ok").sum()) if not summary_df.empty else 0
            miss_count = int((summary_df["Status"] != "ok").sum()) if not summary_df.empty else 0
            OutputControls().printOutput(
                colorText.GREEN
                + f"  [+] Prewarm finished. Cached {ok_count} symbols, missed {miss_count}."
                + colorText.END
            )
            OutputControls().printOutput(f"  [+] Summary: {summary_path}")
            OutputControls().printOutput(
                f"  [+] Cache directory: {provider.cache_dir_for_interval(prewarm_interval)}"
            )
            if not summary_df.empty:
                preview_df = summary_df.head(10).copy()
                OutputControls().printOutput(preview_df.to_string(index=False))
            sys.exit(0)

        if args.actionable is not None:
            from pkscreener.classes.ActionableReport import ActionableReportRunner

            actionable_universe = str(args.actionable).strip().lower()
            actionable_limit = args.actionablelimit or 50
            actionable_min_conf = (
                args.actionableminconfidence
                if args.actionableminconfidence is not None
                else 10.0
            )
            actionable_budget = (
                args.actionablebudget
                if args.actionablebudget is not None
                else None
            )
            actionable_risk_pct = (
                args.actionableriskpct
                if args.actionableriskpct is not None
                else 1.0
            )
            actionable_min_liquidity = (
                args.actionableminliquidity
                if args.actionableminliquidity is not None
                else 10000000.0
            )
            actionable_min_vol_ratio = (
                args.actionableminvolratio
                if args.actionableminvolratio is not None
                else 1.0
            )
            actionable_workers = (
                args.actionableworkers
                if args.actionableworkers is not None
                else 3
            )

            runner = ActionableReportRunner(
                config_manager=configManager,
                universe=actionable_universe,
                limit=actionable_limit,
                min_confidence=actionable_min_conf,
                budget_amount=actionable_budget,
                risk_percent=actionable_risk_pct,
                min_trade_value=actionable_min_liquidity,
                min_volume_ratio=actionable_min_vol_ratio,
                max_workers=actionable_workers,
                use_live_kite=args.actionablelivekite,
                strong_buy_only=bool(args.actionablestrongbuyonly),
            )
            runner.run()
            sys.exit(0)

        if args.signalmonitor is not None:
            from pkscreener.classes.SignalMonitor import SignalMonitorRunner

            signalmonitor_universe = str(args.signalmonitor).strip().lower()
            signalmonitor_limit = args.signalmonitorlimit if args.signalmonitorlimit is not None else 50
            signalmonitor_min_conf = (
                args.signalmonitorminconfidence
                if args.signalmonitorminconfidence is not None
                else 10.0
            )
            signalmonitor_budget = (
                args.signalmonitorbudget
                if args.signalmonitorbudget is not None
                else None
            )
            signalmonitor_risk_pct = (
                args.signalmonitorriskpct
                if args.signalmonitorriskpct is not None
                else 1.0
            )
            signalmonitor_min_liquidity = (
                args.signalmonitorminliquidity
                if args.signalmonitorminliquidity is not None
                else 10000000.0
            )
            signalmonitor_min_vol_ratio = (
                args.signalmonitorminvolratio
                if args.signalmonitorminvolratio is not None
                else 1.0
            )
            signalmonitor_workers = (
                args.signalmonitorworkers
                if args.signalmonitorworkers is not None
                else _auto_worker_count("SIGNALMONITOR_WORKERS", 16)
            )
            signalmonitor_alerts = str(args.signalmonitoralerts or "none").strip().lower()
            signalmonitor_interval = (
                int(args.croninterval)
                if args.croninterval is not None and str(args.croninterval).isdigit()
                else None
            )
            if args.signalmonitorautotune:
                from pkscreener.classes.SignalThresholdTuner import SignalThresholdTuner

                tuned_profile = SignalThresholdTuner.load_latest_profile(signalmonitor_universe)
                if tuned_profile is not None:
                    signalmonitor_min_conf = float(tuned_profile.get("min_confidence", signalmonitor_min_conf))
                    signalmonitor_min_liquidity = float(tuned_profile.get("min_trade_value", signalmonitor_min_liquidity))
                    signalmonitor_min_vol_ratio = float(tuned_profile.get("min_volume_ratio", signalmonitor_min_vol_ratio))
                    OutputControls().printOutput(
                        colorText.GREEN
                        + (
                            "  [+] Using tuned monitor profile: "
                            f"confidence>={signalmonitor_min_conf}, "
                            f"volume>={signalmonitor_min_vol_ratio}, "
                            f"liquidity>={signalmonitor_min_liquidity:.0f}"
                        )
                        + colorText.END
                    )
                else:
                    OutputControls().printOutput(
                        "  [+] No saved tuning profile found. Using the configured monitor thresholds."
                    )

            runner = SignalMonitorRunner(
                config_manager=configManager,
                universe=signalmonitor_universe,
                limit=signalmonitor_limit,
                min_confidence=signalmonitor_min_conf,
                budget_amount=signalmonitor_budget,
                risk_percent=signalmonitor_risk_pct,
                min_trade_value=signalmonitor_min_liquidity,
                min_volume_ratio=signalmonitor_min_vol_ratio,
                max_workers=signalmonitor_workers,
                use_live_kite=args.signalmonitorlivekite,
                alert_channels=signalmonitor_alerts,
                reset_state=bool(args.signalmonitorreset),
                strong_buy_only=bool(args.signalmonitorstrongbuyonly),
                streaming_mode=bool(args.signalmonitorstream),
                guardrail_account_name=str(args.signalmonitoraccount or "main").strip().lower(),
                guardrail_max_daily_loss_amount=(
                    args.signalmonitormaxdailyloss
                    if args.signalmonitormaxdailyloss is not None
                    else 0.0
                ),
                guardrail_max_consecutive_losses=(
                    args.signalmonitormaxconsecutivelosses
                    if args.signalmonitormaxconsecutivelosses is not None
                    else 0
                ),
            )
            while True:
                runner.run()
                runner.reset_state = False
                if not signalmonitor_interval:
                    break
                OutputControls().printOutput(
                    colorText.GREEN
                    + f"  [+] Waiting {signalmonitor_interval} sec. before the next monitor update..."
                    + colorText.END
                )
                sleep(signalmonitor_interval if not args.testbuild else min(signalmonitor_interval, 3))
            sys.exit(0)

        if args.signaltune is not None:
            from pkscreener.classes.SignalThresholdTuner import SignalThresholdTuner

            tune_universe = str(args.signaltune or "nifty50").strip().lower()
            tuner = SignalThresholdTuner(
                universe=tune_universe,
                min_samples=(args.signaltuneminsamples if args.signaltuneminsamples is not None else 12),
            )
            profile, candidates_df = tuner.tune()
            profile_path = os.path.join(
                Archiver.get_user_data_dir(),
                f"signalprofile_{tune_universe}_latest.json",
            )
            candidates_path = os.path.join(
                Archiver.get_user_data_dir(),
                f"signaltune_{tune_universe}_latest.csv",
            )
            if profile is None:
                OutputControls().printOutput(
                    "  [+] Not enough recent validation data is available yet to tune a profile."
                )
                OutputControls().printOutput(
                    f"  [+] Run --signalvalidate {tune_universe} first, then try tuning again."
                )
            else:
                OutputControls().printOutput(
                    colorText.GREEN
                    + f"  [+] Saved tuned profile for {tune_universe}."
                    + colorText.END
                )
                OutputControls().printOutput(f"  [+] Profile: {profile_path}")
                OutputControls().printOutput(f"  [+] Top candidates: {candidates_path}")
                OutputControls().printOutput(
                    "  [+] Recommended live thresholds: "
                    f"confidence>={profile['min_confidence']}, "
                    f"volume>={profile['min_volume_ratio']}, "
                    f"liquidity>={profile['min_trade_value']:.0f}"
                )
                if candidates_df is not None and not candidates_df.empty:
                    OutputControls().printOutput(
                        candidates_df.head(10).to_string(index=False),
                        enableMultipleLineOutput=True,
                    )
            sys.exit(0)

        if args.signalscorecard is not None:
            from pkscreener.classes.SignalPerformanceTracker import SignalPerformanceTracker

            scorecard_universe = str(args.signalscorecard or "nifty50").strip().lower()
            tracker = SignalPerformanceTracker(
                config_manager=configManager,
                universe=scorecard_universe,
                use_live_kite=args.signalscorecardlivekite,
            )
            ledger_df, scorecard_df = tracker.register_and_update()
            scorecard_path = os.path.join(
                Archiver.get_user_data_dir(),
                f"signalperf_{scorecard_universe}_scorecard_latest.csv",
            )
            ledger_path = os.path.join(
                Archiver.get_user_data_dir(),
                f"signalperf_{scorecard_universe}_ledger_latest.csv",
            )
            OutputControls().printOutput(
                colorText.GREEN
                + f"  [+] Signal scorecard updated for {scorecard_universe}."
                + colorText.END
            )
            OutputControls().printOutput(f"  [+] Ledger: {ledger_path}")
            OutputControls().printOutput(f"  [+] Scorecard: {scorecard_path}")
            if scorecard_df is not None and not scorecard_df.empty:
                OutputControls().printOutput(
                    scorecard_df.to_string(index=False),
                    enableMultipleLineOutput=True,
                )
            else:
                OutputControls().printOutput(
                    "  [+] No signal alerts are stored yet, so the scorecard is still empty."
                )
            sys.exit(0)

        if args.dailytruth is not None:
            from pkscreener.classes.DailyTruthReport import DailyTruthReport

            truth_date = None if str(args.dailytruth or "").lower() == "today" else str(args.dailytruth)
            report = DailyTruthReport(
                report_date=truth_date,
                universe=str(args.dailytruthuniverse or "allstocks"),
                account_name=str(args.dailytruthaccount or "main"),
                fetch_benchmarks=bool(args.dailytruthfetchbenchmarks),
            )
            detail_df, summary_df, benchmarks_df, paths = report.build()
            OutputControls().printOutput(
                colorText.GREEN
                + "  [+] Daily truth report generated."
                + colorText.END
            )
            OutputControls().printOutput(f"  [+] Detail: {paths.detail}")
            OutputControls().printOutput(f"  [+] Summary: {paths.summary}")
            OutputControls().printOutput(f"  [+] Benchmarks: {paths.benchmarks}")
            OutputControls().printOutput(f"  [+] Dashboard: {paths.dashboard}")
            if summary_df is not None and not summary_df.empty:
                OutputControls().printOutput(
                    summary_df.to_string(index=False),
                    enableMultipleLineOutput=True,
                )
            if benchmarks_df is not None and not benchmarks_df.empty:
                OutputControls().printOutput(
                    benchmarks_df.to_string(index=False),
                    enableMultipleLineOutput=True,
                )
            if detail_df is None or detail_df.empty:
                OutputControls().printOutput(
                    "  [+] No alert rows found for this date yet. Run --signalscorecard for the universe first if needed."
                )
            sys.exit(0)

        if args.signalvalidate is not None:
            from pkscreener.classes.HistoricalSignalValidator import HistoricalSignalValidator

            validation_universe = str(args.signalvalidate or "nifty50").strip().lower()
            validator = HistoricalSignalValidator(
                config_manager=configManager,
                universe=validation_universe,
                lookback_days=(args.signalvalidatedays if args.signalvalidatedays is not None else 60),
                symbol_limit=(args.signalvalidatelimit if args.signalvalidatelimit is not None else 0),
                min_confidence=(
                    args.signalvalidateminconfidence
                    if args.signalvalidateminconfidence is not None
                    else 15.0
                ),
                min_trade_value=(
                    args.signalvalidateminliquidity
                    if args.signalvalidateminliquidity is not None
                    else 10000000.0
                ),
                min_volume_ratio=(
                    args.signalvalidateminvolratio
                    if args.signalvalidateminvolratio is not None
                    else 1.0
                ),
                max_workers=(args.signalvalidateworkers if args.signalvalidateworkers is not None else 2),
            )
            ledger_df, scorecard_df = validator.run()
            ledger_path = os.path.join(
                Archiver.get_user_data_dir(),
                f"signalvalidate_{validation_universe}_ledger_latest.csv",
            )
            scorecard_path = os.path.join(
                Archiver.get_user_data_dir(),
                f"signalvalidate_{validation_universe}_scorecard_latest.csv",
            )
            OutputControls().printOutput(
                colorText.GREEN
                + f"  [+] Recent-history validation finished for {validation_universe}."
                + colorText.END
            )
            OutputControls().printOutput(f"  [+] Validation ledger: {ledger_path}")
            OutputControls().printOutput(f"  [+] Validation scorecard: {scorecard_path}")
            if scorecard_df is not None and not scorecard_df.empty:
                OutputControls().printOutput(
                    scorecard_df.to_string(index=False),
                    enableMultipleLineOutput=True,
                )
            else:
                OutputControls().printOutput(
                    "  [+] No recent actionable setups were found for the selected validation window."
                )
            if ledger_df is not None and not ledger_df.empty:
                OutputControls().printOutput(
                    f"  [+] Logged {len(ledger_df)} historical setups."
                )
            sys.exit(0)

        if args.papertrade is not None:
            from pkscreener.classes.PaperTrading import PaperTradingRunner

            papertrade_universe = str(args.papertrade).strip().lower()
            papertrade_limit = args.papertradelimit or 50
            papertrade_capital = (
                args.papertradecapital
                if args.papertradecapital is not None
                else 100000.0
            )
            papertrade_add_cash = (
                args.papertradeaddcash
                if args.papertradeaddcash is not None
                else 0.0
            )
            papertrade_alerts = str(args.papertradealerts or "none").strip().lower()
            papertrade_max_positions = args.papertrademaxpositions or 5
            papertrade_min_conf = (
                args.papertrademinconfidence
                if args.papertrademinconfidence is not None
                else 15.0
            )
            papertrade_risk_pct = (
                args.papertraderiskpct
                if args.papertraderiskpct is not None
                else 1.0
            )
            papertrade_min_liquidity = (
                args.papertrademinliquidity
                if args.papertrademinliquidity is not None
                else 10000000.0
            )
            papertrade_min_vol_ratio = (
                args.papertrademinvolratio
                if args.papertrademinvolratio is not None
                else 1.0
            )
            papertrade_workers = (
                args.papertradeworkers
                if args.papertradeworkers is not None
                else 3
            )
            try:
                manual_buy_specs = _parse_manual_trade_specs(args.papertradebuy)
                manual_sell_specs = _parse_manual_trade_specs(args.papertradesell)
            except ValueError as exc:
                OutputControls().printOutput(colorText.FAIL + f"  [+] {exc}" + colorText.END)
                sys.exit(1)

            papertrade_interval = (
                int(args.croninterval)
                if args.croninterval is not None and str(args.croninterval).isdigit()
                else None
            )

            first_run = True
            while True:
                runner = PaperTradingRunner(
                    config_manager=configManager,
                    universe=papertrade_universe,
                    limit=papertrade_limit,
                    initial_capital=papertrade_capital,
                    max_positions=papertrade_max_positions,
                    min_confidence=papertrade_min_conf,
                    reset_account=(args.papertradereset if first_run else False),
                    add_cash_amount=(papertrade_add_cash if first_run else 0.0),
                    alert_channels=papertrade_alerts,
                    manual_buys=(manual_buy_specs if first_run else []),
                    manual_sells=(manual_sell_specs if first_run else []),
                    risk_percent=papertrade_risk_pct,
                    min_trade_value=papertrade_min_liquidity,
                    min_volume_ratio=papertrade_min_vol_ratio,
                    max_workers=papertrade_workers,
                )
                runner.run()
                if not papertrade_interval:
                    break
                OutputControls().printOutput(
                    colorText.GREEN
                    + f"  [+] Waiting {papertrade_interval} sec. before the next paper-trading update..."
                    + colorText.END
                )
                sleep(papertrade_interval if not args.testbuild else min(papertrade_interval, 3))
                first_run = False
            sys.exit(0)

        if args.autopilot is not None:
            from pkscreener.classes.AutoPilotInvestor import AutoPilotConfig, AutoPilotInvestor

            autopilot_config = AutoPilotConfig(
                universe=str(args.autopilot).strip().lower(),
                account_name=str(args.autopilotaccount or "main").strip().lower(),
                min_confidence=(
                    args.autopilotminconfidence
                    if args.autopilotminconfidence is not None
                    else 5.0
                ),
                min_trade_value=(
                    args.autopilotminliquidity
                    if args.autopilotminliquidity is not None
                    else 0.0
                ),
                min_volume_ratio=(
                    args.autopilotminvolratio
                    if args.autopilotminvolratio is not None
                    else 0.0
                ),
                max_workers=(
                    args.autopilotworkers
                    if args.autopilotworkers is not None
                    else _auto_worker_count("AUTOPILOT_WORKERS", 16)
                ),
                max_daily_loss_amount=(
                    args.autopilotmaxdailyloss
                    if args.autopilotmaxdailyloss is not None
                    else float(
                        os.environ.get(
                            "AUTOPILOT_MAX_DAILY_LOSS",
                            os.environ.get("ASSISTANT_MAX_DAILY_LOSS", "0"),
                        )
                        or 0
                    )
                ),
                max_consecutive_losses=(
                    args.autopilotmaxconsecutivelosses
                    if args.autopilotmaxconsecutivelosses is not None
                    else int(
                        os.environ.get(
                            "AUTOPILOT_MAX_CONSECUTIVE_LOSSES",
                            os.environ.get("ASSISTANT_MAX_CONSECUTIVE_LOSSES", "0"),
                        )
                        or 0
                    )
                ),
                alert_channels=str(args.autopilotalerts or "discord").strip().lower(),
                cycle_interval_sec=(
                    int(args.croninterval)
                    if args.croninterval is not None and str(args.croninterval).isdigit()
                    else 60
                ),
            )

            investor = AutoPilotInvestor(config=autopilot_config)
            if autopilot_config.cycle_interval_sec > 0 and args.croninterval is not None:
                investor.run_loop()
            else:
                investor.run_once()
            sys.exit(0)

        if args.discordtradebot:
            from pkscreener.classes.DiscordTradeBot import run_discord_trade_bot

            discord_account = str(args.discordtradeaccount or "main").strip().lower()
            discord_universe = str(args.discordtradeuniverse or "allstocks").strip().lower()
            asyncio.run(
                run_discord_trade_bot(
                    account_name=discord_account,
                    universe=discord_universe,
                )
            )
            return
        
        # Validate premium user for system-launched
        from pkscreener.classes.PKUserRegistration import PKUserRegistration, ValidationResult
        PKUserRegistration.populateSavedUserCreds()
        local_auth_bypass_early = str(
            os.environ.get("PKSCREENER_SKIP_LOGIN", "0")
        ).strip().lower() in ["1", "y", "yes", "true"]
        if args.systemlaunched and not local_auth_bypass_early and not PKUserRegistration.validateToken()[0]:
            result = PKUserRegistration.login()
            if result != ValidationResult.Success:
                OutputControls().printOutput(f"\n[+] {colorText.FAIL}You MUST be a premium/paid user to use this feature!{colorText.END}\n")
                input("Press any key to exit...")
                sys.exit(0)
        
        if args.systemlaunched and args.options is not None:
            args.systemlaunched = args.options
        
        # Handle telegram mode
        if args.telegram:
            if (PKDateUtilities.isTradingTime() and not PKDateUtilities.isTodayHoliday()[0]) or ("PKDevTools_Default_Log_Level" in os.environ.keys()):
                file_path = os.path.join(Archiver.get_user_data_dir(), "monitor_outputs_1.txt")
                if os.path.exists(file_path):
                    default_logger().info("monitor_outputs_1.txt exists! Another instance may be running. Exiting...")
                    return
            else:
                default_logger().info("--telegram option must be launched ONLY during NSE trading hours. Exiting...")
                return
        
        # Handle bot mode
        if args.bot:
            from pkscreener import pkscreenerbot
            pkscreenerbot.runpkscreenerbot(availability=args.botavailable)
            return
        
        # Update configuration
        from pkscreener.classes.cli.PKCliRunner import PKCliRunner
        cli_runner = PKCliRunner(configManager, args)
        cli_runner.update_config()
        
        if args.options is not None:
            if str(args.options) == "0":
                args.options = None
            else:
                args.options = args.options.replace("::", ":")
        
        # Apply price filters
        if args.maxprice:
            configManager.maxLTP = args.maxprice
            configManager.setConfig(ConfigManager.parser, default=True, showFileCreatedText=False)
        if args.minprice:
            configManager.minLTP = args.minprice
            configManager.setConfig(ConfigManager.parser, default=True, showFileCreatedText=False)
        
        # Handle login
        global LoggedIn
        try:
            LoggedIn
        except NameError:
            LoggedIn = False
        
        local_auth_bypass = False
        try:
            from PKDevTools.classes.Environment import PKEnvironment
            local_auth_bypass = str(
                PKEnvironment().allSecrets.get(
                    "PKSCREENER_SKIP_LOGIN",
                    os.environ.get("PKSCREENER_SKIP_LOGIN", "0"),
                )
            ).strip().lower() in ["1", "y", "yes", "true"]
        except Exception:
            local_auth_bypass = str(
                os.environ.get("PKSCREENER_SKIP_LOGIN", "0")
            ).strip().lower() in ["1", "y", "yes", "true"]

        auth_not_required = (
            LoggedIn
            or args.telegram
            or args.bot
            or args.systemlaunched
            or args.testbuild
            or local_auth_bypass
        )
        if local_auth_bypass:
            OutputControls().printOutput(
                colorText.WARN
                + "  [+] Local login bypass enabled via PKSCREENER_SKIP_LOGIN."
                + colorText.END
            )
        if not auth_not_required:
            if not PKUserRegistration.login():
                sys.exit(0)
            LoggedIn = True
        
        # Run appropriate mode
        if args.testbuild and not args.prodbuild:
            OutputControls().printOutput(colorText.FAIL + "  [+] Started in TestBuild mode!" + colorText.END)
            runApplication()
            from pkscreener.globals import closeWorkersAndExit
            closeWorkersAndExit()
            _exit_gracefully(configManager, argParser)
            sys.exit(0)
        elif args.download:
            OutputControls().printOutput(colorText.FAIL + "  [+] Download ONLY mode! Stocks will not be screened!" + colorText.END)
            configManager.restartRequestsCache()
            runApplication()
            from pkscreener.globals import closeWorkersAndExit
            closeWorkersAndExit()
            _exit_gracefully(configManager, argParser)
            sys.exit(0)
        else:
            runApplicationForScreening()
            
    except KeyboardInterrupt:
        from pkscreener.globals import closeWorkersAndExit
        closeWorkersAndExit()
        _exit_gracefully(configManager, argParser)
        sys.exit(0)
    except Exception as e:
        if "RUNNER" in os.environ.keys():
            OutputControls().printOutput(e)
            traceback.print_exc()
        if "RUNNER" not in os.environ.keys() and ('PKDevTools_Default_Log_Level' in os.environ.keys() and
                                                  os.environ["PKDevTools_Default_Log_Level"] != str(log.logging.NOTSET)):
            OutputControls().printOutput("  [+] RuntimeError with 'multiprocessing'.\n  [+] Please contact the Developer!")
            OutputControls().printOutput(e)
            traceback.print_exc()


# Backward compatibility aliases
disableSysOut = OutputController.disable_output
setupLogger = LoggerSetup.setup
logFilePath = LoggerSetup.get_log_file_path
warnAboutDependencies = DependencyChecker.warn_about_dependencies
exitGracefully = lambda: _exit_gracefully(configManager, argParser)


if __name__ == "__main__":
    if "RUNNER" in os.environ.keys():
        try:
            owner = os.popen('git ls-remote --get-url origin | cut -d/ -f4').read().replace("\n", "")
            repo = os.popen('git ls-remote --get-url origin | cut -d/ -f5').read().replace(".git", "").replace("\n", "")
            if owner.lower() not in ["pkjmesra", "pkscreener"]:
                sys.exit(0)
        except:
            pass
    
    try:
        pkscreenercli()
    except KeyboardInterrupt:
        from pkscreener.globals import closeWorkersAndExit
        closeWorkersAndExit()
        _exit_gracefully(configManager, argParser)
        sys.exit(0)
    except Exception as e:
        default_logger().debug(e, exc_info=True)
        if args.log:
            traceback.print_exc()
        from pkscreener.globals import closeWorkersAndExit
        closeWorkersAndExit()
        _exit_gracefully(configManager, argParser)
        sys.exit(0)
