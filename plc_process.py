import os
import cv2
import json
import time
import socket
import shutil
import configparser
import threading
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from collections import defaultdict
from project_paths import CONFIG_DIR, LOG_DIR

# ================= Logging =================
logger = logging.getLogger("tcp_saver")
logger.setLevel(logging.INFO)
logger.propagate = False

if logger.handlers:
    logger.handlers.clear()

fh = RotatingFileHandler(os.path.join(LOG_DIR, "plc.log"), maxBytes=5*1024*1024, backupCount=3)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
fh.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(logging.StreamHandler())

# ================= Config =================
config = configparser.ConfigParser()
config.read(str(CONFIG_DIR / "config.ini"))

TCP_IP = config.get("tcp", "ip", fallback="127.0.0.1")
TCP_PORT = config.getint("tcp", "port", fallback=4555)

# ================= Folders =================
TOTAL_CAM1_DIR = "total_images/cam1"
TOTAL_CAM2_DIR = "total_images/cam2"
FINAL_CAM1_DIR = "image_data/cam1"
FINAL_CAM2_DIR = "image_data/cam2"

os.makedirs(TOTAL_CAM1_DIR, exist_ok=True)
os.makedirs(TOTAL_CAM2_DIR, exist_ok=True)
os.makedirs(FINAL_CAM1_DIR, exist_ok=True)
os.makedirs(FINAL_CAM2_DIR, exist_ok=True)

# ================= State =================
saving_enabled = False
conveyor_stopped = False
selection_in_progress = False

cycle_start_time = None

current_chassis_no = None
current_model_name = None

cycle_saved_files = {"cam1": [], "cam2": []}
last_save_time = defaultdict(lambda: 0.0)

lock = threading.Lock()
message_lock = threading.Lock()

latest_message = {
    "conveyorBit": 0,
    "chassisNo": "UNKNOWN",
    "ModelA": "0"
}

# ================= Utils =================
def make_filename(cam, chassis, model, ts):
    return f"{cam}__{model}__{chassis}__{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"


def save_frame(cam, frame, folder, chassis, model):
    ts = datetime.now()
    filename = make_filename(cam, chassis, model, ts)
    path = os.path.join(folder, filename)

    if cv2.imwrite(path, frame):
        logger.info(f"[SAVE] {path}")
        return path, ts
    return None, None


def copy_to_final(src, cam, chassis, model, ts, folder):
    if not os.path.exists(src):
        return

    filename = make_filename(cam, chassis, model, ts)
    dst = os.path.join(folder, filename)

    shutil.copy2(src, dst)
    logger.info(f"[FINAL] {dst}")


def cleanup():
    for cam in ("cam1", "cam2"):
        for entry in cycle_saved_files[cam]:
            if os.path.exists(entry["filepath"]):
                os.remove(entry["filepath"])

# ================= TCP =================
def tcp_listener():
    global latest_message

    while True:
        try:
            s = socket.socket()
            s.bind((TCP_IP, TCP_PORT))
            s.listen(1)

            conn, _ = s.accept()
            buffer = ""

            while True:
                data = conn.recv(1024)
                if not data:
                    break

                buffer += data.decode()

                try:
                    msg = json.loads(buffer)
                    buffer = ""

                    with message_lock:
                        latest_message["conveyorBit"] = int(msg.get("conveyorBit", 0))
                        latest_message["chassisNo"] = msg.get("chassisNo", "UNKNOWN")
                        latest_message["ModelA"] = msg.get("ModelA", "0")

                except:
                    continue

        except Exception as e:
            logger.error(e)
            time.sleep(1)


threading.Thread(target=tcp_listener, daemon=True).start()

# ================= Selection =================
def select_middle_frames(chassis, model):
    for cam in ("cam1", "cam2"):
        files = cycle_saved_files[cam]

        if not files:
            logger.warning(f"[SELECT] {cam}: No images")
            continue

        mid_idx = len(files) // 2
        selected = files[mid_idx]

        logger.info(f"[SELECT] {cam}: middle frame index={mid_idx}")

        copy_to_final(
            selected["filepath"],
            cam,
            chassis,
            model,
            selected["timestamp"],
            FINAL_CAM1_DIR if cam == "cam1" else FINAL_CAM2_DIR
        )

# ================= Main Processing =================
def process_frame(cam, frame, folder):
    global saving_enabled, conveyor_stopped, selection_in_progress
    global current_chassis_no, current_model_name
    global cycle_saved_files, cycle_start_time

    with message_lock:
        bit = latest_message["conveyorBit"]
        chassis = latest_message["chassisNo"]
        model = latest_message["ModelA"]

    with lock:
        if bit == 1 and not saving_enabled:
            saving_enabled = True
            conveyor_stopped = False
            selection_in_progress = False
            cycle_start_time = time.time()

            current_chassis_no = chassis
            current_model_name = model

            cycle_saved_files = {"cam1": [], "cam2": []}
            last_save_time.clear()

        elif bit == 0 and saving_enabled:
            saving_enabled = False
            conveyor_stopped = True

    if saving_enabled:
        elapsed = time.time() - cycle_start_time

        # Save only between 2s and 6s
        if 2 <= elapsed <= 6:
            now = time.time()

            if now - last_save_time[cam] >= 0.5:
                last_save_time[cam] = now

                path, ts = save_frame(
                    cam,
                    frame.copy(),
                    TOTAL_CAM1_DIR if cam == "cam1" else TOTAL_CAM2_DIR,
                    chassis,
                    model
                )

                if path:
                    cycle_saved_files[cam].append({
                        "filepath": path,
                        "timestamp": ts
                    })
        return

    if conveyor_stopped:
        with lock:
            if selection_in_progress:
                return
            selection_in_progress = True

        try:
            select_middle_frames(current_chassis_no, current_model_name)
        finally:
            cleanup()
            with lock:
                conveyor_stopped = False
                selection_in_progress = False
                cycle_saved_files = {"cam1": [], "cam2": []}
