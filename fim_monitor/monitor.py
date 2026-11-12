"""Core functionality for the File Integrity Monitor (standard-library only)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CHUNK_SIZE = 64 * 1024


class MonitorError(Exception):
    """An expected error that should be shown as a friendly CLI message."""


def resolved_root(directory: str | Path) -> Path:
    """Validate the folder to monitor and return its absolute path."""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise MonitorError(f"Not a directory: {root}")
    return root


def resolved_baseline(baseline: str | Path, root: Path) -> Path:
    """Ensure the baseline is outside the folder being monitored."""
    path = Path(baseline).expanduser().resolve()
    if path == root or root in path.parents:
        raise MonitorError(
            "The baseline must be OUTSIDE the monitored folder. "
            "Try --baseline ../my-baseline.json or monitor a subfolder."
        )
    return path


def sha256_file(path: Path) -> str:
    """Hash a file in chunks so large files do not fill memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan(root: Path) -> dict[str, dict[str, Any]]:
    """Recursively scan regular files, skipping symbolic links.

    Fails rather than claiming a clean scan if any file cannot be read.
    """
    found: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def on_walk_error(error: OSError) -> None:
        errors.append(str(error))

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=on_walk_error):
        current_path = Path(current)
        # Never traverse linked directories, including links outside the root.
        safe_directories = []
        for name in directories:
            try:
                if not (current_path / name).is_symlink():
                    safe_directories.append(name)
            except OSError as error:
                errors.append(str(error))
        directories[:] = sorted(safe_directories)

        for name in sorted(filenames):
            file_path = current_path / name
            try:
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                relative = file_path.relative_to(root).as_posix()
                # Capture both bytes-based fingerprint and a useful size.
                fingerprint = sha256_file(file_path)
                size = file_path.stat().st_size
                found[relative] = {"sha256": fingerprint, "size": size}
            except OSError as error:
                errors.append(f"{file_path}: {error}")

    if errors:
        raise MonitorError("Scan failed; no result can be trusted:\n  " + "\n  ".join(errors))
    return dict(sorted(found.items()))


def make_baseline(root: Path) -> dict[str, Any]:
    """Create a JSON-serializable snapshot of a directory."""
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": scan(root),
    }


def write_baseline(snapshot: dict[str, Any], path: Path, *, force: bool = False) -> None:
    """Save a snapshot via atomic replacement to avoid partial JSON files."""
    if path.exists() and not force:
        raise MonitorError(
            f"Baseline already exists: {path}. Use 'init --force' to replace it "
            "only after verifying that the changes were intentional."
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=".fim-",
                suffix=".tmp", delete=False
            ) as handle:
                temp_name = handle.name
                json.dump(snapshot, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_name, path)
        finally:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError as error:
        raise MonitorError(f"Could not save baseline at {path}: {error}") from error


def load_baseline(path: Path, root: Path) -> dict[str, Any]:
    """Read and check the structure of a saved snapshot."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as error:
        raise MonitorError(f"No baseline found at {path}. Run 'init' first.") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MonitorError(f"Cannot read baseline {path}: {error}") from error

    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise MonitorError("Invalid or unsupported baseline schema. Re-create it with 'init'.")
    if data.get("root") != str(root):
        raise MonitorError(
            "This baseline was created for a different directory. "
            "Use the original directory or create a new baseline."
        )
    files = data.get("files")
    if not isinstance(files, dict):
        raise MonitorError("Invalid baseline: 'files' must be an object.")
    for name, details in files.items():
        if (not isinstance(name, str) or not isinstance(details, dict)
                or not isinstance(details.get("sha256"), str)
                or len(details["sha256"]) != 64
                or not isinstance(details.get("size"), int)):
            raise MonitorError("Invalid baseline: malformed file entry.")
    return data


def compare(previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compare file contents against a previously trusted snapshot."""
    old_names, new_names = set(previous), set(current)
    return {
        "added": sorted(new_names - old_names),
        "deleted": sorted(old_names - new_names),
        "modified": sorted(
            name for name in (old_names & new_names)
            if previous[name]["sha256"] != current[name]["sha256"]
        ),
        "unchanged_count": sum(
            previous[name]["sha256"] == current[name]["sha256"]
            for name in (old_names & new_names)
        ),
        "scanned_count": len(current),
    }


def check(root: Path, baseline_path: Path) -> dict[str, Any]:
    """Return changes since initialization; does not silently update baseline."""
    old_files = load_baseline(baseline_path, root)["files"]
    current_files = scan(root)
    return compare(old_files, current_files)


def has_changes(report: dict[str, Any]) -> bool:
    return bool(report["added"] or report["deleted"] or report["modified"])
