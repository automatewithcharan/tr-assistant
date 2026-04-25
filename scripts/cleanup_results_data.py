from __future__ import annotations

import argparse
import csv
import gzip
import re
import shutil
from datetime import datetime
from pathlib import Path


TIMESTAMPED_SNAPSHOT = re.compile(r"_(\d{8})_(\d{6})(?=\.)")
TEMP_RUNTIME_FILES = {"ticks.json", "candle_store.pkl"}
PRESERVE_EXACT = {
    "kite_connect_session.json",
    "kite_instruments_nse_latest.csv",
}
PRESERVE_DIRECTORIES = {
    "archive",
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


def extract_snapshot_datetime(path: Path) -> datetime | None:
    match = TIMESTAMPED_SNAPSHOT.search(path.name)
    if not match:
        return None
    date_part, time_part = match.groups()
    try:
        return datetime.strptime(f"{date_part}{time_part}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def should_delete(
    path: Path,
    remove_latest: bool,
    keep_portfolio_history: bool,
    keep_days: int,
    now: datetime,
) -> bool:
    if should_preserve(path, remove_latest=remove_latest, keep_portfolio_history=keep_portfolio_history):
        return False
    if path.is_dir():
        return False

    snapshot_dt = extract_snapshot_datetime(path)
    if snapshot_dt is not None:
        age_days = max(0, (now - snapshot_dt).days)
        # Keep recent snapshots directly accessible for quick analysis/debugging.
        if age_days <= keep_days:
            return False
        return True

    if path.name in TEMP_RUNTIME_FILES:
        return True
    if remove_latest and "_latest." in path.name:
        return True
    return False


def archive_file(path: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_path = archive_dir / f"{path.name}.gz"
    with open(path, "rb") as src, gzip.open(archived_path, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink(missing_ok=True)
    return archived_path


def ensure_archive_manifest(archive_dir: Path) -> Path:
    """Ensure manifest exists even before first archive, improving ML discoverability."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_dir / "archive_manifest.csv"
    if not manifest_path.exists():
        with open(manifest_path, "w", newline="", encoding="utf-8") as csvfile:
            fieldnames = [
                "archived_at",
                "original_name",
                "snapshot_time",
                "category",
                "original_size_bytes",
                "archive_name",
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
    return manifest_path


def append_archive_manifest(
    archive_dir: Path,
    original_name: str,
    archived_name: str,
    snapshot_dt: datetime | None,
    archived_at: datetime,
    original_size_bytes: int,
) -> None:
    """Maintain a CSV index so archived snapshots are easy to locate for ML later."""
    manifest_path = ensure_archive_manifest(archive_dir)
    stem = Path(original_name).stem
    category = (stem.split("_")[0] if "_" in stem else "misc").lower()
    row = {
        "archived_at": archived_at.strftime("%Y-%m-%d %H:%M:%S"),
        "original_name": original_name,
        "snapshot_time": snapshot_dt.strftime("%Y-%m-%d %H:%M:%S") if snapshot_dt else "",
        "category": category,
        "original_size_bytes": str(int(original_size_bytes or 0)),
        "archive_name": archived_name,
    }
    with open(manifest_path, "a", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "archived_at",
            "original_name",
            "snapshot_time",
            "category",
            "original_size_bytes",
            "archive_name",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writerow(row)


def cleanup(
    data_dir: Path,
    apply_changes: bool,
    remove_latest: bool,
    keep_portfolio_history: bool,
    keep_days: int,
    archive_instead_of_delete: bool,
    archive_dir: Path,
) -> tuple[list[Path], list[Path], list[Path]]:
    deleted: list[Path] = []
    preserved: list[Path] = []
    archived: list[Path] = []
    now = datetime.now()

    for path in sorted(data_dir.iterdir(), key=lambda item: item.name.lower()):
        if should_delete(
            path,
            remove_latest=remove_latest,
            keep_portfolio_history=keep_portfolio_history,
            keep_days=keep_days,
            now=now,
        ):
            deleted.append(path)
            if apply_changes:
                if archive_instead_of_delete and extract_snapshot_datetime(path) is not None:
                    snapshot_dt = extract_snapshot_datetime(path)
                    original_name = path.name
                    original_size = path.stat().st_size if path.exists() else 0
                    archived_path = archive_file(path, archive_dir=archive_dir)
                    archived.append(archived_path)
                    append_archive_manifest(
                        archive_dir=archive_dir,
                        original_name=original_name,
                        archived_name=archived_path.name,
                        snapshot_dt=snapshot_dt,
                        archived_at=now,
                        original_size_bytes=original_size,
                    )
                else:
                    path.unlink(missing_ok=True)
        else:
            preserved.append(path)

    return deleted, preserved, archived


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
        "--keep-days",
        type=int,
        default=14,
        help="Keep timestamped snapshots from the last N days in place. Older snapshots are archived/deleted.",
    )
    parser.add_argument(
        "--archive-instead-of-delete",
        action="store_true",
        help="Archive old timestamped snapshots as .gz under archive dir instead of deleting.",
    )
    parser.add_argument(
        "--archive-dir",
        default="",
        help="Archive directory path. Defaults to <data-dir>/archive.",
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

    archive_dir = Path(args.archive_dir).resolve() if str(args.archive_dir or "").strip() else (data_dir / "archive")
    if args.apply and args.archive_instead_of_delete:
        ensure_archive_manifest(archive_dir)
    deleted, preserved, archived = cleanup(
        data_dir=data_dir,
        apply_changes=args.apply,
        remove_latest=args.remove_latest,
        keep_portfolio_history=not args.remove_portfolio_history,
        keep_days=max(0, int(args.keep_days or 0)),
        archive_instead_of_delete=bool(args.archive_instead_of_delete),
        archive_dir=archive_dir,
    )

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] results/Data cleanup")
    print(f"Directory: {data_dir}")
    print(f"Delete count: {len(deleted)}")
    print(f"Preserve count: {len(preserved)}")
    print(f"Archived count: {len(archived)}")
    print(f"Archive mode: {'on' if args.archive_instead_of_delete else 'off'}")
    print(f"Keep days: {max(0, int(args.keep_days or 0))}")
    print(f"Archive dir: {archive_dir}")

    if deleted:
        print("\nFiles marked for deletion:")
        for path in deleted:
            print(f" - {path.name}")

    if archived:
        print("\nArchived files:")
        for path in archived[:20]:
            print(f" - {path.name}")
        if len(archived) > 20:
            print(f" - ... and {len(archived) - 20} more")

    if not args.apply:
        print("\nNo files were deleted. Re-run with --apply to perform the cleanup.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
