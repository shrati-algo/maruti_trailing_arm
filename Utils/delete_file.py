from pathlib import Path
from datetime import datetime, timedelta
import os
import logging
from typing import Tuple, Optional

# Reuse your app logger; or create one if this runs standalone
logger = logging.getLogger("file_reading_tools")

# Optional: sync SQLite 'last_file' after deletions
try:
    from Utils import _exec as _db_exec  # internal write helper
except Exception:
    _db_exec = None  # still works without DB sync

VALID_EXTS = {".bmp", ".jpeg", ".jpg", ".png"}

def _parse_timestamp_from_name(name: str) -> Optional[datetime]:
    """
    Expected filename: <cam_name>__<chassis_no>__<YYYYMMDD_%H%M%S_%f>.<ext>
    Returns a datetime or None on parse error.
    """
    try:
        stem = Path(name).stem
        parts = stem.split("__")
        if len(parts) != 3:
            return None
        return datetime.strptime(parts[2], "%Y%m%d_%H%M%S_%f")
    except Exception:
        return None

def _latest_file_by_mtime(folder: Path) -> Optional[str]:
    """Return the path (str) of the newest image file in folder, or None."""
    try:
        files = [p for p in folder.glob("*") if p.is_file() and p.suffix.lower() in VALID_EXTS]
        if not files:
            return None
        files.sort(key=lambda p: p.stat().st_mtime)
        return str(files[-1])
    except Exception:
        logger.exception("[delete_old_files] Failed computing latest file by mtime")
        return None

def delete_old_files(folder_path: str, minutes: int, sync_db: bool = False) -> Tuple[int, int]:
    """
    Delete files older than `minutes` minutes based on the datetime embedded in the filename.
    Assumes names like '<cam_name>__<chassis_no>__<YYYYMMDD_HHMMSS_microsec>.<ext>'.
    Returns: (deleted_count, skipped_count)

    If sync_db=True and Utils._exec is available, will upsert SQLite 'last_file'
    for this folder to the newest remaining file (if any).
    """
    folder = Path(folder_path)
    cutoff_time = datetime.now() - timedelta(minutes=minutes)

    deleted = 0
    skipped = 0

    # Walk only one level; use rglob("*") if you have subfolders
    for file in folder.glob("*"):
        if not file.is_file() or file.suffix.lower() not in VALID_EXTS:
            continue

        ts = _parse_timestamp_from_name(file.name)
        if ts is None:
            skipped += 1
            logger.debug(f"[delete_old_files] Skip unparsable name: {file.name}")
            continue

        if ts < cutoff_time:
            try:
                file.unlink()
                deleted += 1
                logger.info(f"[delete_old_files] Deleted: {file.name}")
            except Exception as e:
                skipped += 1
                logger.exception(f"[delete_old_files] Failed to delete {file.name}: {e}")

    # Optionally sync DB last_file to the newest remaining file
    if sync_db and _db_exec is not None:
        try:
            latest = _latest_file_by_mtime(folder)
            folder_key = os.path.basename(folder_path)
            if latest is None:
                # Clear the row if no files remain
                _db_exec("INSERT INTO files (folder, last_file) VALUES (?, NULL) "
                         "ON CONFLICT(folder) DO UPDATE SET last_file=NULL", (folder_key,))
            else:
                _db_exec("INSERT INTO files (folder, last_file) VALUES (?, ?) "
                         "ON CONFLICT(folder) DO UPDATE SET last_file=excluded.last_file",
                         (folder_key, latest))
            logger.info(f"[delete_old_files] DB sync complete for folder={folder_key}, last_file={latest}")
        except Exception:
            logger.exception("[delete_old_files] DB sync failed (safe to ignore if not required)")

    logger.info(f"[delete_old_files] folder={folder_path} minutes={minutes} -> deleted={deleted}, skipped={skipped}")
    return deleted, skipped
