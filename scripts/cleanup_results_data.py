from __future__ import annotations

import argparse
import re
from pathlib import Path


TIMESTAMPED_SNAPSHOT = re.compile(r"_\d{8}_\d{6}(?=\.)")
TEMP_RUNTIME_FILES = {"ticks.json", "candle_store.pkl"}
PRESERVE_EXACT = {
    "kite_connect_session.json",
    "kite_instruments_nse_latest.csv",
}
PRESERVE_DIRECTORIES = {
    "kite_history_cache",
    "scalable_cache",
}
PRESERVE_PREFIXES = (
    "livetrade_",
    "papertrade_",
)


def should_preserve(path: Path, remove_latest: bool, keep_portfolio_history: bool) -> bool:
    if path.name in PRESERVE_EXACT:
        return True
    if path.is_dir() and path.name in PRESERVE_DIRECTORIES:
        return True
    if keep_portfolio_history and path.name.startswith(PRESERVE_PREFIXES):
        return True
    if not remove_latest and "_latest." in path.name:
        return True
    return False


def should_delete(path: Path, remove_latest: bool, keep_portfolio_history: bool) -> bool:
    if should_preserve(path, remove_latest=remove_latest, keep_portfolio_history=keep_portfolio_history):
        return False
    if path.is_dir():
        return False
    if path.name in TEMP_RUNTIME_FILES:
        return True
    if TIMESTAMPED_SNAPSHOT.search(path.name):
        return True
    if remove_latest and "_latest." in path.name:
        return True
    return False


def cleanup(data_dir: Path, apply_changes: bool, remove_latest: bool, keep_portfolio_history: bool) -> tuple[list[Path], list[Path]]:
    deleted: list[Path] = []
    preserved: list[Path] = []

    for path in sorted(data_dir.iterdir(), key=lambda item: item.name.lower()):
        if should_delete(
            path,
            remove_latest=remove_latest,
            keep_portfolio_history=keep_portfolio_history,
        ):
            deleted.append(path)
            if apply_changes:
                path.unlink(missing_ok=True)
        else:
            preserved.append(path)

    return deleted, preserved


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely clean generated files under results/Data while keeping "
            "the current Kite session, caches, and portfolio files."
        )
    )
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parents[1] / "results" / "Data"),
        help="Directory to clean. Defaults to repo-root/results/Data.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete files. Without this flag, the script only prints a dry-run plan.",
    )
    parser.add_argument(
        "--remove-latest",
        action="store_true",
        help="Also delete *_latest.* snapshot files. By default they are kept.",
    )
    parser.add_argument(
        "--remove-portfolio-history",
        action="store_true",
        help="Also delete timestamped livetrade_*/papertrade_* files. By default they are kept.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        raise SystemExit(f"Data directory does not exist: {data_dir}")

    deleted, preserved = cleanup(
        data_dir=data_dir,
        apply_changes=args.apply,
        remove_latest=args.remove_latest,
        keep_portfolio_history=not args.remove_portfolio_history,
    )

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] results/Data cleanup")
    print(f"Directory: {data_dir}")
    print(f"Delete count: {len(deleted)}")
    print(f"Preserve count: {len(preserved)}")

    if deleted:
        print("\nFiles marked for deletion:")
        for path in deleted:
            print(f" - {path.name}")

    if not args.apply:
        print("\nNo files were deleted. Re-run with --apply to perform the cleanup.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
