from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.StorageManager import StorageManager


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or apply storage cleanup/upload rules for the trade assistant."
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root. Defaults to the current checkout.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload/archive/delete according to the storage rules. Without this flag the command is preview-only.",
    )
    parser.add_argument(
        "--notify-discord",
        action="store_true",
        help="Send the summary to DISCORD_WEBHOOK_URL after the run.",
    )
    parser.add_argument(
        "--bucket",
        default="",
        help="Override the bucket path. Defaults to TRADE_ASSISTANT_ARCHIVE_BUCKET.",
    )
    args = parser.parse_args()

    manager = StorageManager(
        args.repo_root,
        bucket=str(args.bucket or "").strip(),
    )
    summary = manager.run(apply_changes=bool(args.apply), notify=bool(args.notify_discord))
    print(manager.format_text_report(summary, apply_changes=bool(args.apply)))
    print(f"Report written to: {summary.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
