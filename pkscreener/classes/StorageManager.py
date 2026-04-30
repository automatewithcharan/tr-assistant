from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import fcntl
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_KEEP_EXACT = {
    "kite_connect_session.json",
    "assistant_runtime.db",
    "PKDevTools_cache.sqlite",
    "kite_orders_log.csv",
    "autopilot_main_log.csv",
    "kite_tracked_positions.json",
    "disk_usage_report_latest.json",
    "disk_usage_report_latest.txt",
}
_KEEP_PREFIXES = (
    "kiteticker_",
)
_KEEP_SUFFIXES = (
    "_latest.csv",
    "_latest.json",
    "_latest.html",
    "_latest.pkl",
    "_latest.pickle",
    "_latest.gz",
)
_TEMP_SUFFIXES = {".tmp", ".temp", ".swp", ".pid"}
_CACHE_SUFFIXES = {".pyc", ".pyo"}


@dataclass
class StorageItem:
    path: str
    size_bytes: int
    category: str
    action: str
    reason: str
    bucket_key: str = ""
    compress_before_upload: bool = False


@dataclass
class StorageSummary:
    disk_used_bytes: int
    disk_free_bytes: int
    largest_folders: List[Tuple[str, int]]
    write_roots: List[str]
    candidate_items: List[StorageItem]
    uploaded: List[str]
    cleaned: List[str]
    skipped: List[str]
    upload_failures: List[str]
    cleanup_failures: List[str]
    report_path: str = ""


class StorageManager:
    def __init__(
        self,
        repo_root: str | Path,
        *,
        bucket: str = "",
        webhook_url: str = "",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = (self.repo_root / "results" / "Data").resolve()
        self.results_dir = (self.repo_root / "results").resolve()
        self.bucket = self._normalize_bucket_uri(
            str(bucket or os.environ.get("TRADE_ASSISTANT_ARCHIVE_BUCKET", "")).strip()
        )
        self.webhook_url = str(webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()
        self.report_dir = self.data_dir / "storage_reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.data_dir / "storage_guard.lock"

    def scan(self) -> StorageSummary:
        disk = shutil.disk_usage(self.repo_root)
        items = self._collect_candidates()
        return StorageSummary(
            disk_used_bytes=int(disk.used),
            disk_free_bytes=int(disk.free),
            largest_folders=self._largest_folders(),
            write_roots=self._write_roots(),
            candidate_items=items,
            uploaded=[],
            cleaned=[],
            skipped=[],
            upload_failures=[],
            cleanup_failures=[],
        )

    def run(self, *, apply_changes: bool = False, notify: bool = False) -> StorageSummary:
        summary = self.scan()
        if apply_changes:
            lock_handle = self._acquire_apply_lock()
            if lock_handle is None:
                summary.skipped.append(
                    "storage apply skipped because another storage cleanup is already running"
                )
            else:
                try:
                    self._apply(summary)
                finally:
                    self._release_apply_lock(lock_handle)
            disk = shutil.disk_usage(self.repo_root)
            summary.disk_used_bytes = int(disk.used)
            summary.disk_free_bytes = int(disk.free)
            summary.largest_folders = self._largest_folders()
        summary.report_path = self._write_reports(summary, apply_changes=apply_changes)
        if notify:
            self._notify(summary, apply_changes=apply_changes)
        return summary

    def format_text_report(self, summary: StorageSummary, *, apply_changes: bool = False) -> str:
        header = "Disk storage maintenance" if apply_changes else "Disk storage preview"
        candidate_total = sum(item.size_bytes for item in summary.candidate_items)
        uploaded_total = sum(self._safe_size(path) for path in summary.uploaded)
        cleaned_total = sum(self._safe_size(path) for path in summary.cleaned)
        lines = [
            header,
            f"Disk used: {self._fmt_bytes(summary.disk_used_bytes)}",
            f"Remaining free space: {self._fmt_bytes(summary.disk_free_bytes)}",
            "Largest folders:",
        ]
        for folder, size in summary.largest_folders[:8]:
            lines.append(f"- {folder}: {self._fmt_bytes(size)}")
        lines.append("Known write paths:")
        for root in summary.write_roots:
            lines.append(f"- {root}")
        if apply_changes:
            lines.append(
                f"Files uploaded to bucket: {len(summary.uploaded)} ({self._fmt_bytes(uploaded_total)})"
            )
            for path in summary.uploaded[:12]:
                lines.append(f"- uploaded {path}")
            if len(summary.uploaded) > 12:
                lines.append(f"- ... and {len(summary.uploaded) - 12} more")
            lines.append(
                f"Files cleaned locally: {len(summary.cleaned)} ({self._fmt_bytes(cleaned_total)})"
            )
            for path in summary.cleaned[:12]:
                lines.append(f"- cleaned {path}")
            if len(summary.cleaned) > 12:
                lines.append(f"- ... and {len(summary.cleaned) - 12} more")
            lines.append(f"Files skipped: {len(summary.skipped)}")
            for path in summary.skipped[:12]:
                lines.append(f"- skipped {path}")
            if len(summary.skipped) > 12:
                lines.append(f"- ... and {len(summary.skipped) - 12} more")
        else:
            lines.append(
                f"Preview cleanup candidates: {len(summary.candidate_items)} ({self._fmt_bytes(candidate_total)})"
            )
            lines.append(
                "Selected upload/delete categories: "
                + ", ".join(sorted({item.category for item in summary.candidate_items}))
            )
            lines.append(
                "Protected local categories: current/latest outputs, open-position state, order/session/config DB files, current ticker snapshots"
            )
            for item in summary.candidate_items[:20]:
                lines.append(
                    f"- {item.action}: {item.path} [{item.category}] {self._fmt_bytes(item.size_bytes)}"
                )
            if len(summary.candidate_items) > 20:
                lines.append(f"- ... and {len(summary.candidate_items) - 20} more")
            lines.append("No files were deleted. Re-run with apply mode to execute cleanup.")
        if summary.upload_failures:
            lines.append("Upload failures:")
            for path in summary.upload_failures[:10]:
                lines.append(f"- {path}")
        if summary.cleanup_failures:
            lines.append("Cleanup failures:")
            for path in summary.cleanup_failures[:10]:
                lines.append(f"- {path}")
        return "\n".join(lines)

    def _collect_candidates(self) -> List[StorageItem]:
        items: List[StorageItem] = []
        seen: set[str] = set()
        for root in self._scan_roots():
            if not root.exists():
                continue
            if root.is_file():
                candidate = self._classify(root)
                if candidate is not None:
                    items.append(candidate)
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                candidate = self._classify(path)
                if candidate is not None:
                    items.append(candidate)
        items.sort(key=lambda item: item.size_bytes, reverse=True)
        return items

    def _scan_roots(self) -> List[Path]:
        roots = [
            self.data_dir,
            self.results_dir,
            self.repo_root / "Screener Alerts - Text Channels - general [1488777695750914060].txt",
            self.repo_root / ".pytest_cache",
            self.repo_root / "__pycache__",
        ]
        docker_root = Path("/var/lib/docker/containers")
        if docker_root.exists():
            roots.append(docker_root)
        return roots

    def _write_roots(self) -> List[str]:
        roots = [
            str(self.data_dir),
            str(self.data_dir / "kite_history_cache"),
            str(self.data_dir / "fresh_early_engine_cache"),
            str(self.data_dir / "assistant_logs"),
            str(self.data_dir / "archive"),
            str(self.data_dir / "paper_trades"),
            str(self.results_dir),
            str(self.repo_root / "Screener Alerts - Text Channels - general [1488777695750914060].txt"),
            "/tmp/trade_watchdog.log",
            str(Path("/var/lib/docker/containers")),
        ]
        deduped: List[str] = []
        for root in roots:
            if root not in deduped:
                deduped.append(root)
        return deduped

    def _largest_folders(self) -> List[Tuple[str, int]]:
        candidates = [
            self.results_dir,
            self.data_dir,
            self.data_dir / "fresh_early_engine_cache",
            self.data_dir / "kite_history_cache",
            self.data_dir / "archive",
            self.data_dir / "assistant_logs",
            self.data_dir / "paper_trades",
            self.repo_root / ".venv",
            self.repo_root / ".git",
            self.repo_root / "screenshots",
        ]
        sizes: List[Tuple[str, int]] = []
        for folder in candidates:
            if not folder.exists():
                continue
            sizes.append((str(folder), self._dir_size(folder)))
        sizes.sort(key=lambda item: item[1], reverse=True)
        return sizes

    def _classify(self, path: Path) -> Optional[StorageItem]:
        if not path.exists():
            return None
        name = path.name
        rel = self._rel(path)
        age_days = self._age_days(path)
        size = path.stat().st_size

        if name in _KEEP_EXACT or any(name.startswith(prefix) for prefix in _KEEP_PREFIXES):
            return None
        if any(name.endswith(suffix) for suffix in _KEEP_SUFFIXES):
            return None
        lower_name = name.lower()
        if path.parent == self.data_dir and any(
            token in lower_name
            for token in ("session", "position", "positions", "credential", "secret", "config")
        ):
            return None

        if "__pycache__" in path.parts or path.suffix in _CACHE_SUFFIXES:
            return StorageItem(rel, size, "python cache files", "DELETE_LOCAL", "safe cache cleanup")
        if path.suffix.lower() in _TEMP_SUFFIXES or name.startswith("monitor_outputs"):
            return StorageItem(rel, size, "temp files", "DELETE_LOCAL", "stale temp/runtime file")
        if "assistant_logs" in path.parts:
            if path.suffix == ".log" and age_days >= 1:
                return StorageItem(
                    rel,
                    size,
                    "logs",
                    "COMPRESS_UPLOAD_DELETE",
                    "old assistant log",
                    bucket_key=f"assistant_logs/{name}.gz",
                    compress_before_upload=True,
                )
            if path.suffix == ".gz" and age_days >= 0:
                return StorageItem(
                    rel,
                    size,
                    "logs",
                    "UPLOAD_DELETE",
                    "compressed assistant log",
                    bucket_key=f"assistant_logs/{name}",
                )
            return None
        if name == "disk_guard.log":
            return None
        if "archive" in path.parts and age_days >= 0:
            return StorageItem(
                rel,
                size,
                "old alert archives",
                "UPLOAD_DELETE",
                "archive already compressed and ready for bucket",
                bucket_key=f"archive/{name}",
            )
        if "kite_history_cache" in path.parts and age_days >= 3:
            return StorageItem(
                rel,
                size,
                "historical candles / OHLCV cache",
                "UPLOAD_DELETE",
                "historical cache older than local retention",
                bucket_key=f"kite_history_cache/{self._bucket_suffix(path)}",
            )
        if "fresh_early_engine_cache" in path.parts and age_days >= 2:
            return StorageItem(
                rel,
                size,
                "downloaded market data cache",
                "UPLOAD_DELETE",
                "early-engine cache older than local retention",
                bucket_key=f"fresh_early_engine_cache/{self._bucket_suffix(path)}",
            )
        if "backtest_2year_cache" in path.parts and age_days >= 3:
            return StorageItem(
                rel,
                size,
                "backtest cache",
                "UPLOAD_DELETE",
                "backtest cache older than local retention",
                bucket_key=f"backtest_cache/{self._bucket_suffix(path)}",
            )
        if "paper_trades" in path.parts and age_days >= 7 and "_latest" not in name:
            return StorageItem(
                rel,
                size,
                "paper trade output",
                "UPLOAD_DELETE",
                "older paper trade history",
                bucket_key=f"paper_trades/{self._bucket_suffix(path)}",
            )
        if path.suffix in {".db", ".sqlite", ".sqlite3"}:
            return None
        if path.suffix in {".pkl", ".pickle", ".joblib"} and age_days >= 3:
            return StorageItem(
                rel,
                size,
                "pickle/cache files",
                "UPLOAD_DELETE",
                "older serialized cache",
                bucket_key=f"pickle_cache/{self._bucket_suffix(path)}",
            )
        if name.startswith(("signalperf_", "signalmonitor_")) and age_days >= 7 and "_latest" not in name:
            return StorageItem(
                rel,
                size,
                "old alert archives",
                "UPLOAD_DELETE",
                "repeated signal performance snapshots",
                bucket_key=f"signal_snapshots/{name}",
            )
        if name.startswith(("livetrade_", "papertrade_", "daily_truth_")) and age_days >= 7 and "_latest" not in name:
            return StorageItem(
                rel,
                size,
                "backtest output / trade snapshots",
                "UPLOAD_DELETE",
                "dated snapshot older than local retention",
                bucket_key=f"snapshots/{name}",
            )
        if path.parent == self.results_dir and path.suffix in {".csv", ".html"} and age_days >= 3:
            return StorageItem(
                rel,
                size,
                "backtest output",
                "UPLOAD_DELETE",
                "old backtest/report output",
                bucket_key=f"results/{name}",
            )
        if "discord" in name.lower() or name.startswith("Screener Alerts -"):
            if age_days >= 7:
                return StorageItem(
                    rel,
                    size,
                    "Discord exports",
                    "COMPRESS_UPLOAD_DELETE",
                    "old Discord export",
                    bucket_key=f"discord_exports/{name}.gz",
                    compress_before_upload=True,
                )
            return None
        if "docker" in str(path).lower() and path.name.endswith("-json.log") and age_days >= 1:
            return StorageItem(
                rel,
                size,
                "Docker/container logs",
                "DELETE_LOCAL",
                "container log cleanup candidate",
            )
        return None

    def _apply(self, summary: StorageSummary) -> None:
        grouped = self._group_for_bulk_upload(summary.candidate_items)
        remote_cache: Dict[str, set[str]] = {}
        for group_key, group_items in grouped.items():
            local_root, bucket_prefix = group_key
            if not self.bucket:
                for item in group_items:
                    summary.skipped.append(f"{item.path} (bucket not configured)")
                continue
            uploaded = self._upload_directory(local_root, bucket_prefix)
            if not uploaded:
                for item in group_items:
                    summary.upload_failures.append(item.path)
                continue
            remote_cache[group_key] = self._list_remote_objects(bucket_prefix)

        for item in summary.candidate_items:
            path = self.repo_root / item.path if not os.path.isabs(item.path) else Path(item.path)
            if not path.exists():
                continue
            try:
                if item.action == "DELETE_LOCAL":
                    path.unlink(missing_ok=True)
                    summary.cleaned.append(item.path)
                    continue
                if item.action in {"UPLOAD_DELETE", "COMPRESS_UPLOAD_DELETE"}:
                    if not self.bucket:
                        summary.skipped.append(f"{item.path} (bucket not configured)")
                        continue
                    bulk_group = self._bulk_group_key(item)
                    if bulk_group is not None:
                        remote_names = remote_cache.get(bulk_group, set())
                        if self._remote_name_for_item(item) not in remote_names:
                            summary.upload_failures.append(f"{item.path} (verification failed)")
                            continue
                        summary.uploaded.append(f"{item.path} -> {self.bucket.rstrip('/')}/{item.bucket_key}")
                        path.unlink(missing_ok=True)
                        summary.cleaned.append(item.path)
                        continue
                    upload_source = path
                    temp_gz: Optional[Path] = None
                    if item.compress_before_upload:
                        temp_gz = self._compress_to_gzip(path)
                        upload_source = temp_gz
                    uploaded = self._upload_file(upload_source, item.bucket_key)
                    if not uploaded:
                        summary.upload_failures.append(item.path)
                        if temp_gz is not None and temp_gz.exists():
                            temp_gz.unlink(missing_ok=True)
                        continue
                    summary.uploaded.append(f"{item.path} -> {self.bucket.rstrip('/')}/{item.bucket_key}")
                    path.unlink(missing_ok=True)
                    summary.cleaned.append(item.path)
                    if temp_gz is not None and temp_gz.exists():
                        temp_gz.unlink(missing_ok=True)
                    continue
            except Exception as exc:
                if item.action in {"UPLOAD_DELETE", "COMPRESS_UPLOAD_DELETE"}:
                    summary.upload_failures.append(f"{item.path} ({exc})")
                else:
                    summary.cleanup_failures.append(f"{item.path} ({exc})")

    def _write_reports(self, summary: StorageSummary, *, apply_changes: bool) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = self.report_dir / f"disk_usage_report_{stamp}.json"
        latest_json_path = self.report_dir / "disk_usage_report_latest.json"
        txt_path = self.report_dir / f"disk_usage_report_{stamp}.txt"
        latest_txt_path = self.report_dir / "disk_usage_report_latest.txt"
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "apply_changes": apply_changes,
            "summary": {
                "disk_used_bytes": summary.disk_used_bytes,
                "disk_free_bytes": summary.disk_free_bytes,
                "largest_folders": summary.largest_folders,
                "write_roots": summary.write_roots,
                "uploaded": summary.uploaded,
                "cleaned": summary.cleaned,
                "skipped": summary.skipped,
                "upload_failures": summary.upload_failures,
                "cleanup_failures": summary.cleanup_failures,
            },
            "candidate_items": [asdict(item) for item in summary.candidate_items],
        }
        text_report = self.format_text_report(summary, apply_changes=apply_changes)
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        latest_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        txt_path.write_text(text_report, encoding="utf-8")
        latest_txt_path.write_text(text_report, encoding="utf-8")
        return str(latest_txt_path)

    def _notify(self, summary: StorageSummary, *, apply_changes: bool) -> None:
        if not self.webhook_url:
            return
        message = self._discord_message(summary, apply_changes=apply_changes)
        try:
            import requests

            requests.post(
                self.webhook_url,
                json={"content": message[:4000]},
                timeout=10,
            )
        except Exception:
            pass

    def _discord_message(self, summary: StorageSummary, *, apply_changes: bool) -> str:
        header = "🧹 Daily storage report" if apply_changes else "🔎 Daily storage preview"
        lines = [
            header,
            f"Disk used: {self._fmt_bytes(summary.disk_used_bytes)}",
            "Largest folders:",
        ]
        for folder, size in summary.largest_folders[:5]:
            lines.append(f"- {Path(folder).name or folder}: {self._fmt_bytes(size)}")
        if apply_changes:
            lines.append(f"Files uploaded to bucket: {len(summary.uploaded)}")
            lines.append(f"Files cleaned locally: {len(summary.cleaned)}")
            lines.append(f"Files skipped: {len(summary.skipped)}")
        else:
            lines.append(f"Files planned for upload/cleanup: {len(summary.candidate_items)}")
        lines.append(f"Remaining free space: {self._fmt_bytes(summary.disk_free_bytes)}")
        return "\n".join(lines)

    def _bucket_suffix(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.data_dir))
        except ValueError:
            try:
                return str(path.relative_to(self.results_dir))
            except ValueError:
                return path.name

    def _upload_file(self, source: Path, bucket_key: str) -> bool:
        destination = f"{self.bucket.rstrip('/')}/{bucket_key.lstrip('/')}"
        commands = []
        if shutil.which("gcloud"):
            commands.append(["gcloud", "storage", "cp", str(source), destination])
        if shutil.which("gsutil"):
            commands.append(["gsutil", "cp", str(source), destination])
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and self._object_exists(destination):
                return True
        return False

    def _upload_directory(self, local_root: str, bucket_prefix: str) -> bool:
        local_path = Path(local_root)
        if not local_path.exists():
            return False
        destination = f"{self.bucket.rstrip('/')}/{bucket_prefix.strip('/')}"
        commands = []
        if shutil.which("gcloud"):
            commands.append(["gcloud", "storage", "rsync", "--recursive", str(local_path), destination])
        if shutil.which("gsutil"):
            commands.append(["gsutil", "-m", "rsync", "-r", str(local_path), destination])
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return True
        return False

    def _list_remote_objects(self, bucket_prefix: str) -> set[str]:
        prefix_uri = f"{self.bucket.rstrip('/')}/{bucket_prefix.strip('/')}"
        commands = []
        if shutil.which("gcloud"):
            commands.append(["gcloud", "storage", "ls", "--recursive", prefix_uri])
        if shutil.which("gsutil"):
            commands.append(["gsutil", "ls", f"{prefix_uri}/**"])
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                objects = set()
                for line in (result.stdout or "").splitlines():
                    line = line.strip()
                    if not line.startswith("gs://"):
                        continue
                    if "/" in line:
                        objects.add(line.rsplit("/", 1)[-1])
                if objects:
                    return objects
        return set()

    def _group_for_bulk_upload(self, items: List[StorageItem]) -> Dict[Tuple[str, str], List[StorageItem]]:
        groups: Dict[Tuple[str, str], List[StorageItem]] = defaultdict(list)
        for item in items:
            group_key = self._bulk_group_key(item)
            if group_key is not None:
                groups[group_key].append(item)
        return groups

    def _bulk_group_key(self, item: StorageItem) -> Optional[Tuple[str, str]]:
        path = Path(item.path)
        path_text = str(path)
        if item.compress_before_upload:
            return None
        if "results/Data/archive/" in path_text:
            return (str(self.data_dir / "archive"), "archive")
        if "results/Data/fresh_early_engine_cache/" in path_text:
            return (str(self.data_dir / "fresh_early_engine_cache"), "fresh_early_engine_cache")
        if "results/Data/kite_history_cache/" in path_text:
            return (str(self.data_dir / "kite_history_cache"), "kite_history_cache")
        if "results/Data/backtest_2year_cache/" in path_text:
            return (str(self.data_dir / "backtest_2year_cache"), "backtest_cache")
        if "results/Data/paper_trades/" in path_text:
            return (str(self.data_dir / "paper_trades"), "paper_trades")
        if "results/Data/assistant_logs/" in path_text and path.suffix == ".gz":
            return (str(self.data_dir / "assistant_logs"), "assistant_logs")
        return None

    @staticmethod
    def _remote_name_for_item(item: StorageItem) -> str:
        return str(item.bucket_key or "").rsplit("/", 1)[-1]

    def _object_exists(self, uri: str) -> bool:
        commands = []
        if shutil.which("gcloud"):
            commands.append(["gcloud", "storage", "ls", uri])
        if shutil.which("gsutil"):
            commands.append(["gsutil", "ls", uri])
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and str(result.stdout or "").strip():
                return True
        return False

    def _compress_to_gzip(self, path: Path) -> Path:
        gz_path = path.with_name(f"{path.name}.upload.gz")
        with open(path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        return gz_path

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root))
        except ValueError:
            return str(path.resolve())

    def _age_days(self, path: Path) -> int:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        delta = datetime.now(timezone.utc) - mtime
        return max(0, delta.days)

    def _dir_size(self, folder: Path) -> int:
        if folder.is_file():
            return folder.stat().st_size
        total = 0
        for path in folder.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    @staticmethod
    def _fmt_bytes(value: int) -> str:
        size = float(value)
        units = ["B", "KB", "MB", "GB", "TB"]
        unit = 0
        while size >= 1024.0 and unit < len(units) - 1:
            size /= 1024.0
            unit += 1
        return f"{size:.1f} {units[unit]}"

    @staticmethod
    def _safe_size(path_text: str) -> int:
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[0]
        try:
            return Path(path_text).stat().st_size
        except Exception:
            return 0

    @staticmethod
    def _normalize_bucket_uri(bucket: str) -> str:
        bucket = str(bucket or "").strip()
        if not bucket:
            return ""
        if bucket.startswith("gs://"):
            return bucket.rstrip("/")
        return f"gs://{bucket.rstrip('/')}"

    def _acquire_apply_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        return handle

    def _release_apply_lock(self, handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
