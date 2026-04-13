# Utils.py  — SQLite-backed helpers (drop-in for prior TinyDB-based Utils)
from pathlib import Path
from typing import List, Optional, Tuple
import os
import logging
from logging.handlers import RotatingFileHandler
from time import perf_counter, sleep
import threading
import sqlite3
import atexit
import bisect
from project_paths import DATA_BASE_DIR, LOG_DIR

# ============== Logging (file-only, rotating) ==============
logger = logging.getLogger("file_reading_tools")
logger.setLevel(logging.INFO)
logger.propagate = False
if logger.handlers:
    logger.handlers.clear()

_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "file_reading_tools.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(threadName)s | %(name)s | %(message)s",
    "%Y-%m-%d %H:%M:%S",
)
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)

def _timeit(label: str, fn, *args, **kwargs):
    t0 = perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        logger.info(f"[TIMER] {label} took {(perf_counter() - t0) * 1000:.2f} ms")

# ============== Config ==============
# Put SQLite DB alongside your previous JSON for easy swap
SQLITE_PATH = os.path.join(DATA_BASE_DIR, "file_tracker.db")

# ============== SQLite Setup ==============
_conn = None
_thread_lock = threading.RLock()

def _open_db():
    """Open/create SQLite DB, enable WAL, and ensure schema."""
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    conn = sqlite3.connect(
        SQLITE_PATH,
        timeout=5.0,
        isolation_level=None,        # autocommit mode
        check_same_thread=False
    )
    # Pragmas tuned for real-time-ish workloads
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=2000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS files (
        folder    TEXT PRIMARY KEY,
        last_file TEXT
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);")
    return conn

def _get_conn():
    global _conn
    if _conn is None:
        _conn = _open_db()
    return _conn

def _exec(query, params=(), retries=3, backoff=0.02):
    """Execute write query with small retry on SQLITE_BUSY."""
    with _thread_lock:
        conn = _get_conn()
        for i in range(retries + 1):
            try:
                conn.execute(query, params)
                return
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "database is locked" in msg or "busy" in msg:
                    sleep(backoff * (i + 1))
                    if i == retries:
                        logger.error(f"[DB] Write failed after retries: {e}")
                        raise
                else:
                    raise

def _fetchone(query, params=(), retries=3, backoff=0.02):
    """Execute read query with small retry on SQLITE_BUSY."""
    with _thread_lock:
        conn = _get_conn()
        for i in range(retries + 1):
            try:
                cur = conn.execute(query, params)
                row = cur.fetchone()
                cur.close()
                return row
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "database is locked" in msg or "busy" in msg:
                    sleep(backoff * (i + 1))
                    if i == retries:
                        logger.error(f"[DB] Read failed after retries: {e}")
                        raise
                else:
                    raise

def close_db():
    global _conn
    with _thread_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                logger.exception("[DB] Error on close")
            _conn = None

atexit.register(close_db)

# ================= Core Functions =================
def sort_files(folder_path: str) -> List[Path]:
    """Return a sorted list of Path objects by modification time."""
    folder = Path(folder_path)
    files = [
        f for f in folder.glob("*")
        if f.is_file() and f.suffix.lower() in (".bmp", ".jpg", ".jpeg")
    ]
    files_sorted = _timeit(
        f"sort_files({folder_path})",
        sorted,
        files,
        key=lambda x: x.stat().st_mtime
    )
    return files_sorted

def _paths_and_mtimes(paths: List[Path]) -> Tuple[List[Path], List[float]]:
    paths = list(paths)
    mtimes = [p.stat().st_mtime for p in paths]
    return paths, mtimes

def get_last_processed(folder_key: str) -> Optional[str]:
    """Return the last_file string for a folder, or None if not set."""
    row = _timeit(
        "SQLite.get(files)",
        _fetchone,
        "SELECT last_file FROM files WHERE folder = ?",
        (folder_key,),
    )
    return row[0] if row else None

def check_if_updated(folder_path: str, sorted_files: List[Path]) -> bool:
    """Return True if there are files newer than the last processed one."""
    try:
        if not sorted_files:
            return False

        folder_key = os.path.basename(folder_path)
        last_processed = get_last_processed(folder_key)

        files, mtimes = _paths_and_mtimes(sorted_files)

        if last_processed is None:
            updated = True
        else:
            # If last file still exists, compare by position in the current list
            files_str = [str(p) for p in files]
            try:
                idx_last = files_str.index(last_processed)
                updated = (idx_last < len(files) - 1)
            except ValueError:
                # Not found: fall back to mtime comparison
                try:
                    last_mtime = Path(last_processed).stat().st_mtime
                except Exception:
                    last_mtime = -1.0
                updated = (mtimes[-1] > last_mtime)

        logger.info(f"[check_if_updated] folder={folder_key} last={last_processed} latest={files[-1]} -> {updated}")
        return updated

    except Exception:
        logger.exception("[check_if_updated] Error checking if updated")
        return False

def load_images(file_paths: List[str]) -> List[bytes]:
    """Read image data as bytes from file paths."""
    images = []
    for path in file_paths:
        try:
            with open(path, 'rb') as f:
                images.append(f.read())
        except Exception:
            logger.exception(f"[load_images] Failed to load image {path}")
    logger.info(f"[load_images] Loaded {len(images)}/{len(file_paths)} images")
    return images

def update_db_and_get_new_files(folder_path: str, sorted_files: List[Path]) -> List[str]:
    """
    Updates SQLite with the latest processed file for this folder (camera),
    and returns the list of new files (as strings) since the last processed one.
    """
    folder_key = os.path.basename(folder_path)
    files, mtimes = _paths_and_mtimes(sorted_files)
    files_str = [str(f) for f in files]

    last_processed = get_last_processed(folder_key)

    if last_processed is None:
        idx = 0
    else:
        # Prefer index if present (exactly “after last”), else by mtime
        try:
            idx = files_str.index(last_processed) + 1
        except ValueError:
            try:
                last_mtime = Path(last_processed).stat().st_mtime
            except Exception:
                last_mtime = -1.0
            idx = bisect.bisect(mtimes, last_mtime)

    new_files = files_str[idx:]

    if new_files:
        last = new_files[-1]
        _timeit(
            "SQLite.upsert(files)",
            _exec,
            """
            INSERT INTO files (folder, last_file)
            VALUES (?, ?)
            ON CONFLICT(folder) DO UPDATE SET last_file=excluded.last_file
            """,
            (folder_key, last),
        )
        logger.info(f"[update_db] [{folder_key}] DB updated with last_file={last}")

    logger.info(f"[update_db] [{folder_key}] new_files count={len(new_files)}")
    return new_files
