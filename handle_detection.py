import os
import cv2
import time
import torch
import pandas as pd
from datetime import datetime
from ultralytics import YOLO
from project_paths import log_path

# ------------------ Configuration ------------------
g_bExit = False
CONFIDENCE_THRESHOLD = 0.75
LOG_FILE = log_path("handle_logs.csv")

# ------------------ Device Selection ------------------
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[INFO] Using device: {device}")

model = YOLO(r"model.pt").to(device)

# ------------------ Helper Functions ------------------

def detect_handle(frame):
    results = model(frame, verbose=False, device=device)[0]

    for box in results.boxes:
        if box.conf[0] > CONFIDENCE_THRESHOLD:
            return True
    return False


def save_frame(frame, folder, cam_name):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{cam_name.lower()}_{timestamp}.jpg"
    path = os.path.join(folder, filename)
    cv2.imwrite(path, frame)
    print(f"[INFO] Saved {cam_name} frame → {path}")
    return path


def log_detection(cam_name, detected):
    """Log detection results into CSV"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = {
        "timestamp": timestamp,
        "camera": cam_name,
        "handle_present": 1 if detected else 0
    }

    df = pd.DataFrame([log_entry])
    if not os.path.exists(LOG_FILE):
        df.to_csv(LOG_FILE, index=False, mode="w")
    else:
        df.to_csv(LOG_FILE, index=False, mode="a", header=False)
    print(f"[INFO] Logged: {log_entry}")
