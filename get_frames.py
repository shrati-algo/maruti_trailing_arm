# -- coding: utf-8 --

import cv2
import numpy as np
import threading
import sys
import time
import os
import signal
from datetime import datetime
from ctypes import *
from flask import Flask, Response

# -------------------------------
# MVS SDK Path
# -------------------------------
sys.path.append(r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport")
from MvCameraControl_class import *

# -------------------------------
# Logging
# -------------------------------
import logging
from logging.handlers import RotatingFileHandler
from project_paths import LOG_DIR

logger = logging.getLogger("camera_app")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False

formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(process)d | %(threadName)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "app.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

werk_logger = logging.getLogger("werkzeug")
werk_logger.setLevel(logging.INFO)
werk_logger.handlers.clear()
werk_logger.addHandler(file_handler)
werk_logger.addHandler(console_handler)
werk_logger.propagate = False

# -------------------------------
# Globals
# -------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

JPEG_QUALITY = 70
STREAM_SIZE = (640, 480)

# cam1 -> id 2
# cam2 -> id 0
CAMERA_CONFIGS = [
    {"name": "cam1", "folder": os.path.join(BASE_DIR, "image_data", "cam1"), "cam_id": 2},
    {"name": "cam2", "folder": os.path.join(BASE_DIR, "image_data", "cam2"), "cam_id": 0},
]

EXPOSURE_MAP = {
    2: 40000,
    0: 40000,
}

# /video_feed/0 -> cam1 (id=2)
# /video_feed/1 -> cam2 (id=0)
STREAM_TO_CAMERA_ID = {
    0: 2,
    1: 0,
}

latest_frames = {}
frame_locks = {}
running = True
threads = []

frame_counts = {}
last_frame_time = {}
camera_meta = {}

# -------------------------------
# Mock PLC Save Config
# -------------------------------
MOCK_PLC_MESSAGE = "PLC_TEST_OK"
MOCK_SAVE_INTERVAL_SECONDS = 2.0
last_mock_save_time = {}

# -------------------------------
# Utility
# -------------------------------
from time import perf_counter

def log_time(label, func, *args, **kwargs):
    t0 = perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        logger.info(f"[TIMER] {label} took {(perf_counter() - t0) * 1000:.2f} ms")

# -------------------------------
# Cleanup
# -------------------------------
def safe_delete_old(folder, older_than_minutes=60):
    try:
        os.makedirs(folder, exist_ok=True)
        now = time.time()
        cutoff = now - (older_than_minutes * 60)

        for fname in os.listdir(folder):
            fpath = os.path.join(folder, fname)
            if os.path.isfile(fpath):
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        logger.info(f"[Cleanup] Deleted {fpath}")
                except Exception:
                    logger.exception(f"[Cleanup] Failed deleting {fpath}")
    except Exception:
        logger.exception("[Cleanup] Error")

# -------------------------------
# Mock PLC save
# -------------------------------
def process_frame(cam_name, frame, folder):
    os.makedirs(folder, exist_ok=True)

    now_ts = time.time()
    last_ts = last_mock_save_time.get(cam_name, 0)

    logger.info(f"[MOCK PLC] Camera={cam_name} | Message={MOCK_PLC_MESSAGE}")

    if now_ts - last_ts < MOCK_SAVE_INTERVAL_SECONDS:
        logger.info(
            f"[MOCK PLC] Camera={cam_name} | Skipping save due to interval gate "
            f"({MOCK_SAVE_INTERVAL_SECONDS}s)"
        )
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = os.path.join(folder, f"{cam_name}_{MOCK_PLC_MESSAGE}_{ts}.jpg")

    ok = cv2.imwrite(filename, frame)
    if ok:
        last_mock_save_time[cam_name] = now_ts
        logger.info(f"[SAVE] {filename}")
        return filename

    logger.error(f"[SAVE] Failed to save {filename}")
    return None

# -------------------------------
# Decode
# -------------------------------
def decode_raw_frame(raw2d, pixel_type, cam_id):
    try:
        # Common Bayer decode path
        return cv2.cvtColor(raw2d, cv2.COLOR_BAYER_BG2BGR)
    except Exception:
        logger.exception(f"[Cam{cam_id}] Bayer decode failed, fallback to GRAY2BGR")
        return cv2.cvtColor(raw2d, cv2.COLOR_GRAY2BGR)

# -------------------------------
# Camera thread
# -------------------------------
def grab_camera(cam_id, stDeviceInfo, exposure_time):
    global running

    logical_name = camera_meta[cam_id]["name"]
    folder = camera_meta[cam_id]["folder"]
    enum_index = camera_meta[cam_id]["enum_index"]

    if stDeviceInfo.nTLayerType == MV_USB_DEVICE:
        transport = "USB"
    elif stDeviceInfo.nTLayerType == MV_GIGE_DEVICE:
        transport = "GigE"
    else:
        transport = f"OTHER({stDeviceInfo.nTLayerType})"

    cam = MvCamera()

    logger.info(
        f"[Cam{cam_id}] Starting thread | logical={logical_name} | "
        f"enum_index={enum_index} | transport={transport} | folder={folder}"
    )

    ret = cam.MV_CC_CreateHandle(stDeviceInfo)
    logger.info(f"[Cam{cam_id}] CreateHandle ret={ret}")
    if ret != 0:
        logger.error(f"[Cam{cam_id}] CreateHandle failed: 0x{ret:x}")
        return

    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    logger.info(f"[Cam{cam_id}] OpenDevice ret={ret}")
    if ret != 0:
        logger.error(f"[Cam{cam_id}] OpenDevice failed: 0x{ret:x}")
        try:
            cam.MV_CC_DestroyHandle()
        except Exception:
            logger.exception(f"[Cam{cam_id}] DestroyHandle cleanup failed")
        return

    try:
        if stDeviceInfo.nTLayerType == MV_GIGE_DEVICE:
            try:
                nPacketSize = cam.MV_CC_GetOptimalPacketSize()
                logger.info(f"[Cam{cam_id}] OptimalPacketSize={nPacketSize}")
                if int(nPacketSize) > 0:
                    ret_packet = cam.MV_CC_SetIntValue("GevSCPSPacketSize", nPacketSize)
                    logger.info(f"[Cam{cam_id}] Set GevSCPSPacketSize ret={ret_packet}")
            except Exception:
                logger.exception(f"[Cam{cam_id}] Failed setting packet size")

        ret = cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
        logger.info(f"[Cam{cam_id}] TriggerMode OFF ret={ret}")

        ret = cam.MV_CC_SetEnumValue("ExposureAuto", 0)
        logger.info(f"[Cam{cam_id}] ExposureAuto OFF ret={ret}")

        ret = cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_time))
        logger.info(f"[Cam{cam_id}] ExposureTime={exposure_time} ret={ret}")

        ret = cam.MV_CC_StartGrabbing()
        logger.info(f"[Cam{cam_id}] StartGrabbing ret={ret}")
        if ret != 0:
            logger.error(f"[Cam{cam_id}] StartGrabbing failed: 0x{ret:x}")
            return

        logger.info(f"[Cam{cam_id}] Grabbing started successfully")

        alive_log_ts = time.time()

        while running:
            stOutFrame = MV_FRAME_OUT()
            memset(byref(stOutFrame), 0, sizeof(MV_FRAME_OUT))

            ret = cam.MV_CC_GetImageBuffer(stOutFrame, 1000)

            if ret != 0:
                if time.time() - alive_log_ts >= 2:
                    logger.info(
                        f"[Cam{cam_id}] Alive but no frame | logical={logical_name} | "
                        f"enum_index={enum_index} | ret=0x{ret:x}"
                    )
                    alive_log_ts = time.time()
                time.sleep(0.01)
                continue

            buffer_freed = False
            try:
                width = stOutFrame.stFrameInfo.nWidth
                height = stOutFrame.stFrameInfo.nHeight
                frame_len = stOutFrame.stFrameInfo.nFrameLen
                pixel_type = stOutFrame.stFrameInfo.enPixelType
                frame_num = stOutFrame.stFrameInfo.nFrameNum

                logger.info(
                    f"[Cam{cam_id}] Got frame | logical={logical_name} | "
                    f"width={width} height={height} frame_len={frame_len} "
                    f"pixel_type={pixel_type} frame_num={frame_num}"
                )

                if width <= 0 or height <= 0:
                    logger.error(
                        f"[Cam{cam_id}] Invalid frame dimensions | width={width}, height={height}"
                    )
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    buffer_freed = True
                    continue

                if frame_len <= 0:
                    logger.error(
                        f"[Cam{cam_id}] Empty frame buffer | frame_len={frame_len} "
                        f"| width={width} height={height} pixel_type={pixel_type}"
                    )
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    buffer_freed = True
                    continue

                if not stOutFrame.pBufAddr:
                    logger.error(
                        f"[Cam{cam_id}] Null pBufAddr | frame_len={frame_len} "
                        f"| width={width} height={height} pixel_type={pixel_type}"
                    )
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    buffer_freed = True
                    continue

                try:
                    raw = np.frombuffer(
                        string_at(stOutFrame.pBufAddr, frame_len),
                        dtype=np.uint8
                    )
                except Exception:
                    logger.exception(f"[Cam{cam_id}] Failed to convert SDK buffer to numpy")
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    buffer_freed = True
                    continue

                if raw.size == 0:
                    logger.error(
                        f"[Cam{cam_id}] Raw buffer became empty array | "
                        f"frame_len={frame_len} width={width} height={height} "
                        f"pixel_type={pixel_type}"
                    )
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    buffer_freed = True
                    continue

                expected_min = width * height
                if raw.size < expected_min:
                    logger.error(
                        f"[Cam{cam_id}] Raw buffer too small | got={raw.size}, "
                        f"expected_at_least={expected_min}, frame_len={frame_len}, "
                        f"width={width}, height={height}, pixel_type={pixel_type}"
                    )
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    buffer_freed = True
                    continue

                try:
                    raw2d = raw[:expected_min].reshape((height, width))
                except Exception:
                    logger.exception(
                        f"[Cam{cam_id}] Reshape failed | raw_size={raw.size}, "
                        f"expected_min={expected_min}, width={width}, height={height}, "
                        f"frame_len={frame_len}, pixel_type={pixel_type}"
                    )
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    buffer_freed = True
                    continue

                frame_bgr = decode_raw_frame(raw2d, pixel_type, cam_id)

                cv2.putText(
                    frame_bgr,
                    f"{logical_name} | cam_id={cam_id} | enum={enum_index} | {transport}",
                    (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.0,
                    (0, 0, 255),
                    3,
                    cv2.LINE_AA
                )

                with frame_locks[cam_id]:
                    latest_frames[cam_id] = frame_bgr.copy()

                frame_counts[cam_id] += 1
                last_frame_time[cam_id] = time.time()

                if time.time() - alive_log_ts >= 2:
                    logger.info(
                        f"[Cam{cam_id}] Alive | logical={logical_name} | "
                        f"frames={frame_counts[cam_id]}"
                    )
                    alive_log_ts = time.time()

            except Exception:
                logger.exception(f"[Cam{cam_id}] Unexpected error in grab loop")

            finally:
                if not buffer_freed:
                    try:
                        cam.MV_CC_FreeImageBuffer(stOutFrame)
                    except Exception:
                        logger.exception(f"[Cam{cam_id}] FreeImageBuffer failed")

    except Exception:
        logger.exception(f"[Cam{cam_id}] Exception in camera thread")

    finally:
        try:
            cam.MV_CC_StopGrabbing()
        except Exception:
            logger.exception(f"[Cam{cam_id}] StopGrabbing error")

        try:
            cam.MV_CC_CloseDevice()
        except Exception:
            logger.exception(f"[Cam{cam_id}] CloseDevice error")

        try:
            cam.MV_CC_DestroyHandle()
        except Exception:
            logger.exception(f"[Cam{cam_id}] DestroyHandle error")

        logger.info(f"[Cam{cam_id}] Thread stopped")

# -------------------------------
# Processing thread
# -------------------------------
def processing_pipeline_thread(cam_id, cam_name, folder):
    global running

    os.makedirs(folder, exist_ok=True)
    safe_delete_old(folder, 60)

    logger.info(f"[Pipeline-{cam_name}] Started | cam_id={cam_id} | folder={folder}")

    while running:
        frame = None

        try:
            with frame_locks[cam_id]:
                if latest_frames.get(cam_id) is not None:
                    frame = latest_frames[cam_id].copy()
        except Exception:
            logger.exception(f"[Pipeline-{cam_name}] Failed reading latest frame")
            time.sleep(0.05)
            continue

        if frame is None:
            time.sleep(0.02)
            continue

        try:
            log_time(f"process_frame({cam_name})", process_frame, cam_name, frame, folder)
        except Exception:
            logger.exception(f"[Pipeline-{cam_name}] process_frame failed")

        time.sleep(0.01)

    logger.info(f"[Pipeline-{cam_name}] Stopped")

# -------------------------------
# Flask
# -------------------------------
app = Flask(__name__)

@app.route("/")
def index():
    return """
    <html>
    <head>
        <title>Dual Camera Live</title>
        <style>
            body { font-family: Arial; background: #111; color: #fff; }
            .cam { margin-bottom: 30px; }
            img { border: 2px solid #444; }
        </style>
    </head>
    <body>
        <h1>Dual Camera Live</h1>
        <div class="cam">
            <h2>Camera 1</h2>
            <img src="/video_feed/0" width="640">
        </div>
        <div class="cam">
            <h2>Camera 2</h2>
            <img src="/video_feed/1" width="640">
        </div>
    </body>
    </html>
    """

def generate_stream(cam_id):
    global running

    while running:
        frame = None

        try:
            with frame_locks[cam_id]:
                if latest_frames.get(cam_id) is not None:
                    frame = latest_frames[cam_id].copy()
        except Exception:
            logger.exception(f"[Stream Cam {cam_id}] Failed reading frame")
            time.sleep(0.05)
            continue

        if frame is None:
            time.sleep(0.05)
            continue

        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            grabbed = frame_counts.get(cam_id, 0)
            age = -1
            if cam_id in last_frame_time and last_frame_time[cam_id] > 0:
                age = round(time.time() - last_frame_time[cam_id], 2)

            cv2.putText(
                frame,
                f"{timestamp} | cam_id={cam_id} | grabbed={grabbed} | age={age}s",
                (10, frame.shape[0] - 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
                cv2.LINE_AA
            )

            frame = cv2.resize(frame, STREAM_SIZE)

            ret, buffer = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )

            if not ret:
                logger.error(f"[Stream Cam {cam_id}] JPEG encode failed")
                time.sleep(0.05)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )

        except Exception:
            logger.exception(f"[Stream Cam {cam_id}] Stream generation failed")
            time.sleep(0.05)

@app.route("/video_feed/<int:cam_index>")
def video_feed(cam_index):
    if cam_index not in STREAM_TO_CAMERA_ID:
        logger.error(f"[Flask] Invalid stream index: {cam_index}")
        return "Invalid stream", 404

    cam_id = STREAM_TO_CAMERA_ID[cam_index]

    if cam_id not in latest_frames:
        logger.error(f"[Flask] Camera ID {cam_id} not available for stream index {cam_index}")
        return f"Camera for stream {cam_index} is not available.", 404

    return Response(
        generate_stream(cam_id),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

# -------------------------------
# Shutdown
# -------------------------------
def _stop_all(*_):
    global running
    if running:
        logger.info("[Shutdown] Stop signal received")
        running = False

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop_all)
    signal.signal(signal.SIGTERM, _stop_all)

    for cfg in CAMERA_CONFIGS:
        os.makedirs(cfg["folder"], exist_ok=True)
        logger.info(f"[Startup] Ensured folder exists: {cfg['folder']}")

    logger.info(f"[Startup] Current working directory: {os.getcwd()}")
    logger.info("[Startup] Initializing MVS SDK...")
    MvCamera.MV_CC_Initialize()

    try:
        deviceList = MV_CC_DEVICE_INFO_LIST()
        memset(byref(deviceList), 0, sizeof(deviceList))

        t0_enum = perf_counter()
        ret = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, deviceList)
        logger.info(f"[TIMER] MV_CC_EnumDevices took {(perf_counter() - t0_enum) * 1000:.2f} ms")

        if ret != 0:
            logger.error(f"[Startup] Error enumerating devices! ret=0x{ret:x}")
            sys.exit(1)

        if deviceList.nDeviceNum == 0:
            logger.error("[Startup] No cameras found!")
            sys.exit(1)

        logger.info(f"[Startup] Found {deviceList.nDeviceNum} camera(s)")

        num = min(deviceList.nDeviceNum, len(CAMERA_CONFIGS))
        logger.info(f"[Startup] Will run {num} camera(s)")

        for i in range(num):
            config = CAMERA_CONFIGS[i]
            cam_id = config["cam_id"]

            stDeviceInfo = cast(
                deviceList.pDeviceInfo[i],
                POINTER(MV_CC_DEVICE_INFO)
            ).contents

            if stDeviceInfo.nTLayerType == MV_USB_DEVICE:
                transport = "USB"
            elif stDeviceInfo.nTLayerType == MV_GIGE_DEVICE:
                transport = "GigE"
            else:
                transport = f"OTHER({stDeviceInfo.nTLayerType})"

            camera_meta[cam_id] = {
                "name": config["name"],
                "folder": config["folder"],
                "enum_index": i,
                "transport": transport,
            }

            latest_frames[cam_id] = None
            frame_locks[cam_id] = threading.Lock()
            frame_counts[cam_id] = 0
            last_frame_time[cam_id] = 0

            exposure = EXPOSURE_MAP.get(cam_id, 30000)

            logger.info(
                f"[Startup] enum_index={i} -> logical_name={config['name']} -> "
                f"camera_id={cam_id} -> transport={transport} -> exposure={exposure}"
            )

            t1 = threading.Thread(
                target=grab_camera,
                args=(cam_id, stDeviceInfo, exposure),
                daemon=True,
                name=f"Grabber-{config['name']}"
            )

            t2 = threading.Thread(
                target=processing_pipeline_thread,
                args=(cam_id, config["name"], config["folder"]),
                daemon=True,
                name=f"Pipeline-{config['name']}"
            )

            threads.append(t1)
            threads.append(t2)

            t1.start()
            t2.start()

        logger.info("------ Streams ------")
        logger.info("http://127.0.0.1:5000/video_feed/0 -> cam1 (id=2)")
        logger.info("http://127.0.0.1:5000/video_feed/1 -> cam2 (id=0)")
        logger.info("---------------------")

        app.run(
            host="0.0.0.0",
            port=5000,
            threaded=True,
            debug=False,
            use_reloader=False
        )

    except KeyboardInterrupt:
        _stop_all()

    except Exception:
        logger.exception("[Main] Unhandled exception")
        sys.exit(1)

    finally:
        running = False

        try:
            for t in threads:
                t.join(timeout=2.0)
        except Exception:
            logger.exception("[Main] Thread join error")

        try:
            MvCamera.MV_CC_Finalize()
            logger.info("[Shutdown] MVS SDK finalized")
        except Exception:
            logger.exception("[Shutdown] Finalize error")

        sys.exit(0)
