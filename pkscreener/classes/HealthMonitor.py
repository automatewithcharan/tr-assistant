"""
Health & status monitor for the trade assistant — checks worker health,
data freshness, and alerts to Discord if anything is broken.
"""
import os
import time
import json
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path


class HealthMonitor:
    """Monitor worker processes, log freshness, data pipeline health."""

    def __init__(self, session_id: int, output_dir: str, discord_webhook: Optional[str] = None):
        self.session_id = session_id
        self.output_dir = output_dir
        self.discord_webhook = discord_webhook
        self.workers = {
            "supervisor": {"name": "Supervisor", "port": None, "required": True},
            "ticker": {"name": "KiteTickerStream", "required": True},
            "monitor": {"name": "SignalMonitor", "required": True},
            "discord": {"name": "DiscordTradeBot", "required": True},
            "autopilot": {"name": "AutoPilot", "required": True},
        }
        base_dir = output_dir if os.path.basename(os.path.normpath(output_dir)) == "Data" else os.path.join(output_dir, "Data")
        self.logs_dir = os.path.join(base_dir, "assistant_logs")
        self.data_dir = base_dir

    def check_all(self) -> Tuple[bool, str]:
        """Run all health checks. Return (healthy, status_message)."""
        issues = []
        details = []

        # Check 1: Process health
        proc_issues, proc_detail = self._check_processes()
        if proc_issues:
            issues.extend(proc_issues)
        details.append(proc_detail)

        # Check 2: Log freshness
        log_issues, log_detail = self._check_log_freshness()
        if log_issues:
            issues.extend(log_issues)
        details.append(log_detail)

        # Check 3: Data pipeline
        data_issues, data_detail = self._check_data_freshness()
        if data_issues:
            issues.extend(data_issues)
        details.append(data_detail)

        # Check 4: Discord responsiveness
        discord_ok = self._check_discord_webhook()
        if not discord_ok:
            issues.append("Discord webhook unreachable")
            details.append("❌ Discord webhook down")
        else:
            details.append("✅ Discord webhook OK")

        healthy = len(issues) == 0
        status_msg = self._format_status(healthy, issues, details)
        return healthy, status_msg

    def _check_processes(self) -> Tuple[List[str], str]:
        """Check if all required workers are alive."""
        issues = []
        details_lines = []

        try:
            result = subprocess.run(
                ["ps", "-eo", "pid,cmd"],
                capture_output=True,
                text=True,
                timeout=5
            )
            ps_output = result.stdout
        except Exception as e:
            return [f"Could not read process list: {e}"], "❌ Process check failed"

        expected_patterns = {
            "supervisor": "--assistantinternalsupervisor",
            "ticker": "--kitetickerstream",
            "monitor": "--signalmonitor",
            "discord": "--discordtradebot",
            "autopilot": "--autopilot",
        }

        found_workers = set()
        for worker_key, pattern in expected_patterns.items():
            if pattern in ps_output:
                found_workers.add(worker_key)
                details_lines.append(f"✅ {self.workers[worker_key]['name']}")
            else:
                if self.workers[worker_key]["required"]:
                    issues.append(f"{self.workers[worker_key]['name']} not running")
                details_lines.append(f"❌ {self.workers[worker_key]['name']}")

        status = " | ".join(details_lines)
        return issues, status

    def _check_log_freshness(self) -> Tuple[List[str], str]:
        """Check if worker logs are being updated (not hung)."""
        issues = []
        details_lines = []
        now = time.time()
        max_age_sec = 120  # 2 minutes max without log update

        expected_logs = {
            "supervisor": f"session_{self.session_id}_supervisor.log",
            "ticker": f"session_{self.session_id}_ticker.log",
            "monitor": f"session_{self.session_id}_monitor.log",
            "discord": f"session_{self.session_id}_discord.log",
            "autopilot": f"session_{self.session_id}_autopilot.log",
        }

        for worker_key, log_name in expected_logs.items():
            log_path = os.path.join(self.logs_dir, log_name)
            if os.path.exists(log_path):
                mod_time = os.path.getmtime(log_path)
                age = now - mod_time
                if age < max_age_sec:
                    details_lines.append(f"✅ {log_name} ({int(age)}s ago)")
                else:
                    issues.append(f"{self.workers[worker_key]['name']} log stale ({int(age)}s)")
                    details_lines.append(f"⚠️ {log_name} ({int(age)}s ago)")
            else:
                if self.workers[worker_key]["required"]:
                    issues.append(f"{self.workers[worker_key]['name']} log missing")
                details_lines.append(f"❌ {log_name} missing")

        status = " | ".join(details_lines[:3])  # Keep compact
        return issues, status

    def _check_data_freshness(self) -> Tuple[List[str], str]:
        """Check if data files are being updated (ticks, signal monitor)."""
        issues = []
        checks = []

        now = time.time()
        max_age_sec = 60  # 1 minute max data age

        # Check tick data
        ticks_path = os.path.join(
            self.data_dir, "kiteticker_all_ticks_latest.json"
        )
        if os.path.exists(ticks_path):
            age = now - os.path.getmtime(ticks_path)
            if age < max_age_sec:
                checks.append(f"✅ Ticks ({int(age)}s)")
            else:
                issues.append(f"Ticks stale ({int(age)}s)")
                checks.append(f"⚠️ Ticks ({int(age)}s)")
        else:
            issues.append("Ticks file missing")
            checks.append("❌ Ticks missing")

        # Check signal monitor state
        signal_path = os.path.join(
            self.data_dir, "signalmonitor_allstocks_signals_simple_latest.csv"
        )
        if os.path.exists(signal_path):
            age = now - os.path.getmtime(signal_path)
            if age < 90:  # Monitor runs every 60s, allow 90s
                checks.append(f"✅ Signals ({int(age)}s)")
            else:
                issues.append(f"Signals stale ({int(age)}s)")
                checks.append(f"⚠️ Signals ({int(age)}s)")
        else:
            issues.append("Signal monitor state missing")
            checks.append("❌ Signals missing")

        # Check portfolio ledger (if trades are happening)
        portfolio_path = os.path.join(
            self.data_dir, "livetrade_main_trades_latest.csv"
        )
        if os.path.exists(portfolio_path):
            age = now - os.path.getmtime(portfolio_path)
            checks.append(f"✅ Portfolio ({int(age)}s)")

        status = " | ".join(checks)
        return issues, status

    def _check_discord_webhook(self) -> bool:
        """Test if Discord webhook is reachable."""
        if not self.discord_webhook:
            return True  # Not configured, not an issue
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", self.discord_webhook],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.stdout.startswith("204") or result.stdout.startswith("200")
        except Exception:
            return False

    def _format_status(self, healthy: bool, issues: List[str], details: List[str]) -> str:
        """Format status message for display/Discord."""
        emoji = "✅ HEALTHY" if healthy else "⚠️ ISSUES DETECTED"
        msg = f"**{emoji}** — {datetime.now().strftime('%H:%M:%S IST')}\n"
        msg += "\n".join(details)

        if issues:
            msg += "\n\n**Issues:**\n"
            for issue in issues[:5]:  # Max 5 issues shown
                msg += f"• {issue}\n"

        return msg

    def send_alert(self, message: str) -> bool:
        """Send alert to Discord webhook."""
        if not self.discord_webhook:
            return False
        try:
            subprocess.run(
                ["curl", "-s", "-X", "POST", self.discord_webhook,
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps({"content": message})],
                timeout=5
            )
            return True
        except Exception:
            return False
