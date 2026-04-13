# combined_camera_yolo_pipeline.py
# combined_camera_yolo_pipeline.py  — uses SQLite-backed Utils
import os
import cv2
import threading
import numpy as np
from datetime import datetime
from pathlib import Path
import shutil
import bisect
import logging
from logging.handlers import RotatingFileHandler
from time import perf_counter
from project_paths import LOG_DIR

# --- Project Imports (SQLite-backed) ---
from Utils.file_reading_tools import (
    sort_files,
    check_if_updated,
    update_db_and_get_new_files,
    get_last_processed,          # NEW: to preserve your old “peek” logic
)
#from circle_detection import detect, detect_circle
from Utils.push_to_db import insert_db  # (import kept only if used elsewhere; safe to remove if unused)

# ================== Logging ==================
logger = logging.getLogger("pipeline_app")
logger.setLevel(logging.INFO)
logger.propagate = False
if logger.handlers:
    logger.handlers.clear()

_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "pipeline_app.log"),
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

def log_time(label, func, *args, **kwargs):
    """Run func(*args, **kwargs), log duration, return result."""
    t0 = perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        logger.info(f"[TIMER] {label} took {(perf_counter() - t0) * 1000:.2f} ms")

# # ------------------ Global Setup ------------------
# g_bExit = False
# FPS = 4
# BUFFER_SIZE = 10
CONFIDENCE_THRESHOLD = 0.75

CAM1_FOLDER = "image_data/cam1"
CAM2_FOLDER = "image_data/cam2"
DESTINATION_DIR = "images"
BEST_IMAGE_FOLDER = "best_images_cam1"

os.makedirs(CAM1_FOLDER, exist_ok=True)
os.makedirs(CAM2_FOLDER, exist_ok=True)
os.makedirs(DESTINATION_DIR, exist_ok=True)
os.makedirs(BEST_IMAGE_FOLDER, exist_ok=True)

lock = threading.Lock()  # Thread lock for shared resources

def get_best_image_path(image_paths):
    """
    Given a list of image file paths,
    returns the sharpest image path only if sharpness score > 20.
    Otherwise returns None.
    """
    best_score = -1
    best_path = None

    for path in image_paths:
        try:
            img = cv2.imread(path)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()  # sharpness score

            if lap_var > best_score:
                best_score = lap_var
                best_path = path
        except Exception:
            logger.exception(f"[get_best_image_path] Error processing: {path}")

    if best_score > 20 and best_path:
        try:
            dest_path = os.path.join(BEST_IMAGE_FOLDER, os.path.basename(best_path))
            t0_cp = perf_counter()
            shutil.copy2(best_path, dest_path)
            logger.info(f"[TIMER] shutil.copy2({best_path} -> {dest_path}) took {(perf_counter()-t0_cp)*1000:.2f} ms")
        except Exception:
            logger.exception(f"[get_best_image_path] Failed to copy best image: {best_path}")
        return best_path
    else:
        logger.info("[get_best_image_path] No sufficiently sharp image found in this batch. Discarding...")
        return None

def pipeline(folder_path):
    delete_old(folder_path=folder_path,mins=1)
    try:
        sorted_files = log_time(f"sort_files({folder_path})", sort_files, folder_path)

        # Early return if total files <= 5
        # if len(sorted_files) <= 5:
        #     return None

        updated = log_time(f"check_if_updated({folder_path})", check_if_updated, folder_path, sorted_files)
        if updated:
            # ===== Preserve your earlier “peek new names” logic (non-functional but kept) =====
            folder_key = os.path.basename(folder_path)
            last_processed = get_last_processed(folder_key) or ""

            # Ensure comparisons are on strings
            sorted_files_str = [str(f) for f in sorted_files]
            idx = bisect.bisect(sorted_files_str, str(last_processed))
            _peek = sorted_files_str[idx:]  # peek new names (not used directly)
            logger.info(f"[pipeline] Peek new files (not used): {_peek[:3]}{'...' if len(_peek)>3 else ''}")

            # ===== Actual fetch + DB update =====
            files = log_time(f"update_db_and_get_new_files({folder_path})",
                             update_db_and_get_new_files, folder_path, sorted_files)
            logger.info(f"[pipeline] Received files: {files}")

            # best_file = log_time("get_best_image_path(files)", get_best_image_path, files)
            # logger.info(f"[pipeline] Best file: {best_file}")
            return files[0]

        return None
    except Exception:
        logger.exception(f"[pipeline] Unhandled exception for folder: {folder_path}")
        return None

def delete_old(folder_path: str, mins: int):
    # You had delete_old_files in original import; keep your own implementation/module for it.
    try:
        from Utils import delete_old_files  # if you have this implemented elsewhere
        log_time(f"delete_old_files({folder_path}, {mins})", delete_old_files, folder_path, mins)
    except Exception:
        logger.exception(f"[delete_old] Failed for {folder_path} mins={mins}")

# def timestamp_from_img(path_or_img):
#     """
#     Extract timestamp from filename.
#     Format: <cam_name>__<chassis_no>__<YYYYMMDD_HHMMSS_microsec>.jpg
#     Returns: timestamp string in '%Y-%m-%d %H:%M:%S' format
#     """
#     if isinstance(path_or_img, str):  # file path
#         try:
#             filename = os.path.basename(path_or_img)
#             parts = filename.rsplit(".", 1)[0].split("__")

#             if len(parts) != 3:
#                 raise ValueError(f"Unexpected filename format: {filename}")

#             timestamp_str = parts[2]
#             t_dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S_%f")
#             ts = t_dt.strftime("%Y-%m-%d %H:%M:%S")
#             logger.info(f"[timestamp_from_img] Parsed {filename} -> {ts}")
#             return ts

#         except Exception as e:
#             logger.exception(f"[timestamp_from_img] Failed to parse timestamp from {path_or_img}: {e}")
#             # Fallback to now
#             ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#             return ts_now

#     # fallback if not a path string
#     ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#     logger.info("[timestamp_from_img] Non-path input, returning current time")
#     return ts_now
def timestamp_from_img(path_or_img):
    """
    Extract timestamp from filename.
    Format: <cam_name>__<model_name>__<chassis_no>__<YYYYMMDD_HHMMSS_microsec>.jpg
    Returns: timestamp string in '%Y-%m-%d %H:%M:%S' format
    """
    if isinstance(path_or_img, str):  # file path
        try:
            filename = os.path.basename(path_or_img)
            # Split by "__"
            parts = filename.rsplit(".", 1)[0].split("__")

            if len(parts) != 4:
                raise ValueError(f"Unexpected filename format: {filename}")

            # Timestamp is always the last part
            timestamp_str = parts[3]
            t_dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S_%f")
            ts = t_dt.strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"[timestamp_from_img] Parsed {filename} -> {ts}")
            return ts

        except Exception as e:
            logger.exception(f"[timestamp_from_img] Failed to parse timestamp from {path_or_img}: {e}")
            # Fallback to current time
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return ts_now

    # fallback if not a path string
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("[timestamp_from_img] Non-path input, returning current time")
    return ts_now
