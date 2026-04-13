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
from plc_process import process_frame
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
JPEG_QUALITY = 70
STREAM_SIZE = (640, 480)
EXPOSURE_MAP = {0: 40000, 1: 40000}

CAMERA_CONFIGS = [
    {"name": "cam1", "folder": "image_data/cam1"},   # internal id 0 -> GigE
    {"name": "cam2", "folder": "image_data/cam2"},   # internal id 1 -> USB
]

latest_frames = {}
frame_locks = {}
running = True
threads = []

# -------------------------------
# Fixed camera serial mapping
# -------------------------------
GIGE_SERIAL = "DA5843327"   # internal camera id 0
USB_SERIAL  = "DA5606439"   # internal camera id 1

SERIAL_TO_INTERNAL_ID = {
    GIGE_SERIAL: 0,
    USB_SERIAL: 1,
}

# -------------------------------
# Mock PLC Save Config
# -------------------------------
MOCK_PLC_MESSAGE = "PLC_TEST_OK"
MOCK_SAVE_INTERVAL_SECONDS = 2.0
last_mock_save_time = {}

# -------------------------------
# Optional helper
# -------------------------------
from time import perf_counter

def log_time(label, func, *args, **kwargs):
    t0 = perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        dt_ms = (perf_counter() - t0) * 1000.0
        logger.info(f"[TIMER] {label} took {dt_ms:.2f} ms")

# -------------------------------
# Utility helpers
# -------------------------------
def decode_char_array(arr):
    try:
        return bytes(arr).split(b'\0', 1)[0].decode('utf-8', errors='ignore').strip()
    except Exception:
        return ""

def ip_to_str(ip):
    return "{}.{}.{}.{}".format(
        (ip >> 24) & 0xFF,
        (ip >> 16) & 0xFF,
        (ip >> 8) & 0xFF,
        ip & 0xFF
    )

def get_device_serial(stDeviceList):
    try:
        if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
            return decode_char_array(stDeviceList.SpecialInfo.stGigEInfo.chSerialNumber)
        elif stDeviceList.nTLayerType == MV_USB_DEVICE:
            return decode_char_array(stDeviceList.SpecialInfo.stUsb3VInfo.chSerialNumber)
    except Exception:
        logger.exception("Failed to read device serial number")
    return ""

def get_device_model(stDeviceList):
    try:
        if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
            return decode_char_array(stDeviceList.SpecialInfo.stGigEInfo.chModelName)
        elif stDeviceList.nTLayerType == MV_USB_DEVICE:
            return decode_char_array(stDeviceList.SpecialInfo.stUsb3VInfo.chModelName)
    except Exception:
        logger.exception("Failed to read device model")
    return ""

def get_device_type_name(stDeviceList):
    if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
        return "GigE"
    elif stDeviceList.nTLayerType == MV_USB_DEVICE:
        return "USB"
    return f"Unknown({stDeviceList.nTLayerType})"

def log_detected_devices(deviceList):
    logger.info("========== DETECTED DEVICES ==========")
    for sdk_index in range(deviceList.nDeviceNum):
        try:
            stDeviceList = cast(
                deviceList.pDeviceInfo[sdk_index],
                POINTER(MV_CC_DEVICE_INFO)
            ).contents

            dtype = get_device_type_name(stDeviceList)
            serial = get_device_serial(stDeviceList)
            model = get_device_model(stDeviceList)

            if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
                ip = ip_to_str(stDeviceList.SpecialInfo.stGigEInfo.nCurrentIp)
                logger.info(
                    f"[SDK Index {sdk_index}] Type={dtype}, Model={model}, Serial={serial}, IP={ip}"
                )
            else:
                logger.info(
                    f"[SDK Index {sdk_index}] Type={dtype}, Model={model}, Serial={serial}"
                )

        except Exception:
            logger.exception(f"Failed logging device at SDK index {sdk_index}")
    logger.info("======================================")

def build_camera_mapping(deviceList):
    """
    Build mapping from fixed serial number to internal camera ID.
    Deduplicates duplicate GigE discovery entries using serial number.
    """
    matched_devices = {}
    seen_serials = set()

    for sdk_index in range(deviceList.nDeviceNum):
        try:
            stDeviceList = cast(
                deviceList.pDeviceInfo[sdk_index],
                POINTER(MV_CC_DEVICE_INFO)
            ).contents

            serial = get_device_serial(stDeviceList)
            model = get_device_model(stDeviceList)
            dtype = get_device_type_name(stDeviceList)

            if not serial:
                logger.warning(f"[SDK Index {sdk_index}] Empty serial, skipping")
                continue

            # Ignore duplicate discovery entries by serial
            if serial in seen_serials:
                logger.warning(
                    f"[SDK Index {sdk_index}] Duplicate device entry ignored | "
                    f"Type={dtype}, Model={model}, Serial={serial}"
                )
                continue

            seen_serials.add(serial)

            if serial in SERIAL_TO_INTERNAL_ID:
                internal_id = SERIAL_TO_INTERNAL_ID[serial]
                matched_devices[internal_id] = {
                    "sdk_index": sdk_index,
                    "serial": serial,
                    "model": model,
                    "type": dtype,
                    "stDeviceList": stDeviceList,
                }
                logger.info(
                    f"[Mapping] Internal camera {internal_id} mapped to SDK index {sdk_index} | "
                    f"Type={dtype}, Model={model}, Serial={serial}"
                )
            else:
                logger.warning(
                    f"[SDK Index {sdk_index}] Unused device found | "
                    f"Type={dtype}, Model={model}, Serial={serial}"
                )

        except Exception:
            logger.exception(f"Error while building mapping for SDK index {sdk_index}")

    return matched_devices

# -------------------------------
# Safe delete_old replacement
# -------------------------------
def safe_delete_old(folder, older_than_minutes=60):
    """
    Deletes files older than older_than_minutes from folder.
    Safe standalone replacement for delete_old().
    """
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
                        logger.info(f"[Cleanup] Deleted old file: {fpath}")
                except Exception:
                    logger.exception(f"[Cleanup] Failed deleting file: {fpath}")
    except Exception:
        logger.exception(f"[Cleanup] Failed for folder: {folder}")

# -------------------------------

# -------------------------------
# Camera Grabber
# -------------------------------
def grab_camera(cam_index, stDeviceList, exposure_time, serial_number):
    global latest_frames, running

    logger.info(
        f"[Cam{cam_index}] Starting grab thread... "
        f"(internal_id={cam_index}, serial={serial_number})"
    )

    cam = MvCamera()

    ret = cam.MV_CC_CreateHandle(stDeviceList)
    if ret != 0:
        logger.error(f"[Cam{cam_index}] CreateHandle failed: 0x{ret:x}")
        return

    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    if ret != 0:
        logger.error(f"[Cam{cam_index}] OpenDevice failed: 0x{ret:x}")
        try:
            cam.MV_CC_DestroyHandle()
        except Exception:
            logger.exception(f"[Cam{cam_index}] DestroyHandle cleanup failed")
        return

    try:
        if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
            try:
                nPacketSize = cam.MV_CC_GetOptimalPacketSize()
                if int(nPacketSize) > 0:
                    ret_packet = cam.MV_CC_SetIntValue("GevSCPSPacketSize", nPacketSize)
                    logger.info(f"[Cam{cam_index}] Packet size set to {nPacketSize}, ret={ret_packet}")
            except Exception:
                logger.exception(f"[Cam{cam_index}] Failed setting packet size")

        ret = cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
        logger.info(f"[Cam{cam_index}] TriggerMode OFF ret={ret}")

        ret = cam.MV_CC_SetEnumValue("ExposureAuto", 0)
        logger.info(f"[Cam{cam_index}] ExposureAuto OFF ret={ret}")

        ret = cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_time))
        logger.info(f"[Cam{cam_index}] ExposureTime={exposure_time} ret={ret}")

        ret = cam.MV_CC_StartGrabbing()
        if ret != 0:
            logger.error(f"[Cam{cam_index}] StartGrabbing failed: 0x{ret:x}")
            return

        logger.info(f"[Cam{cam_index}] Grabbing started successfully")

        while running:
            stOutFrame = MV_FRAME_OUT()
            memset(byref(stOutFrame), 0, sizeof(MV_FRAME_OUT))

            ret = cam.MV_CC_GetImageBuffer(stOutFrame, 1000)

            if ret != 0:
                time.sleep(0.01)
                continue

            try:
                width = stOutFrame.stFrameInfo.nWidth
                height = stOutFrame.stFrameInfo.nHeight
                frame_len = stOutFrame.stFrameInfo.nFrameLen
                pixel_type = stOutFrame.stFrameInfo.enPixelType
                frame_num = stOutFrame.stFrameInfo.nFrameNum

                logger.info(
                    f"[Cam{cam_index}] Got frame | Width={width} Height={height} "
                    f"PixelType={pixel_type} FrameLen={frame_len} FrameNum={frame_num}"
                )

                if width <= 0 or height <= 0:
                    logger.error(f"[Cam{cam_index}] Invalid frame size")
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    continue

                # If frame_len is 0 or invalid, fall back to width*height for 8-bit raw formats
                buf_len = frame_len if frame_len and frame_len > 0 else width * height

                raw = np.frombuffer(
                    string_at(stOutFrame.pBufAddr, buf_len),
                    dtype=np.uint8
                )

                if raw.size < width * height:
                    logger.error(
                        f"[Cam{cam_index}] Raw buffer too small: got={raw.size}, need at least={width * height}"
                    )
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                    continue

                raw2d = raw[:width * height].reshape((height, width))

                # Current decode logic kept same as your code
                frame_bgr = cv2.cvtColor(raw2d, cv2.COLOR_BAYER_BG2BGR)

                with frame_locks[cam_index]:
                    latest_frames[cam_index] = frame_bgr.copy()

                logger.info(f"[Cam{cam_index}] Stored frame successfully")

            except Exception:
                logger.exception(f"[Cam{cam_index}] Error while decoding frame")

            finally:
                try:
                    cam.MV_CC_FreeImageBuffer(stOutFrame)
                except Exception:
                    logger.exception(f"[Cam{cam_index}] FreeImageBuffer failed")

    except Exception:
        logger.exception(f"[Cam{cam_index}] Exception in grab loop")

    finally:
        try:
            cam.MV_CC_StopGrabbing()
        except Exception:
            logger.exception(f"[Cam{cam_index}] StopGrabbing error")

        try:
            cam.MV_CC_CloseDevice()
        except Exception:
            logger.exception(f"[Cam{cam_index}] CloseDevice error")

        try:
            cam.MV_CC_DestroyHandle()
        except Exception:
            logger.exception(f"[Cam{cam_index}] DestroyHandle error")

        logger.info(f"[Cam{cam_index}] Stopped grab thread.")

# -------------------------------
# Processing Thread
# -------------------------------
def processing_pipeline_thread(cam_index, cam_name, folder):
    global latest_frames, running

    os.makedirs(folder, exist_ok=True)
    safe_delete_old(folder, 60)

    logger.info(f"[Pipeline-{cam_name}] Starting processing thread for folder={folder}")

    saved_count = 0
    loop_count = 0

    try:
        while running:
            loop_count += 1
            frame_to_process = None

            with frame_locks[cam_index]:
                if latest_frames.get(cam_index) is not None:
                    frame_to_process = latest_frames[cam_index].copy()

            if frame_to_process is None:
                if loop_count % 100 == 0:
                    logger.info(f"[Pipeline-{cam_name}] Waiting for first frame...")
                time.sleep(0.02)
                continue

            logger.info(f"[Pipeline-{cam_name}] Frame received for processing")

            try:
                saved_path = log_time(
                    f"process_frame({cam_name})",
                    process_frame,
                    cam_name,
                    frame_to_process,
                    folder
                )
                if saved_path:
                    saved_count += 1
                    logger.info(f"[Pipeline-{cam_name}] Saved image #{saved_count}: {saved_path}")
            except Exception:
                logger.exception(f"[Pipeline-{cam_name}] Error in process_frame")

            time.sleep(0.01)

    except Exception:
        logger.exception(f"[Pipeline-{cam_name}] Unhandled exception in processing loop")

    finally:
        logger.info(f"[Pipeline-{cam_name}] Stopped processing thread.")

# -------------------------------
# Flask Web Streaming
# -------------------------------
app = Flask(__name__)

def generate_stream(cam_index):
    global latest_frames, running

    while running:
        frame_to_stream = None

        with frame_locks[cam_index]:
            frame = latest_frames.get(cam_index)
            if frame is not None:
                frame_to_stream = frame.copy()

        if frame_to_stream is None:
            time.sleep(0.05)
            continue

        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            cv2.putText(
                frame_to_stream,
                timestamp,
                (10, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (0, 255, 0),
                2,
                cv2.LINE_AA
            )

            frame_resized = cv2.resize(frame_to_stream, STREAM_SIZE)

            t0_enc = perf_counter()
            ret, buffer = cv2.imencode(
                ".jpg",
                frame_resized,
                [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )
            logger.info(f"[TIMER] Cam{cam_index} cv2.imencode took {(perf_counter() - t0_enc) * 1000:.2f} ms")

            if not ret:
                logger.error(f"[Stream Cam {cam_index}] JPEG encode failed")
                time.sleep(0.05)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )

            time.sleep(0.05)

        except Exception:
            logger.exception(f"[Stream Cam {cam_index}] Exception while streaming frame")
            time.sleep(0.05)

@app.route("/video_feed/<int:cam_index>")
def video_feed(cam_index):
    if cam_index not in latest_frames:
        logger.error(f"[Flask] Camera {cam_index} is not available")
        return f"Camera {cam_index} is not available.", 404

    if latest_frames[cam_index] is None:
        return "Camera is initializing, no frames yet.", 503

    return Response(
        generate_stream(cam_index),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

# -------------------------------
# Shutdown handler
# -------------------------------
def _stop_all(*_):
    global running
    if running:
        logger.info("Signal received, stopping all threads...")
        running = False

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop_all)
    signal.signal(signal.SIGTERM, _stop_all)

    # Ensure folders exist before anything starts
    for cfg in CAMERA_CONFIGS:
        os.makedirs(cfg["folder"], exist_ok=True)
        logger.info(f"[Startup] Ensured folder exists: {cfg['folder']}")

    logger.info("[Startup] Initializing MVS SDK...")
    MvCamera.MV_CC_Initialize()

    try:
        deviceList = MV_CC_DEVICE_INFO_LIST()
        memset(byref(deviceList), 0, sizeof(deviceList))

        t0_enum = perf_counter()
        ret = MvCamera.MV_CC_EnumDevices(
            MV_GIGE_DEVICE | MV_USB_DEVICE,
            deviceList
        )
        logger.info(f"[TIMER] MV_CC_EnumDevices took {(perf_counter() - t0_enum) * 1000:.2f} ms")

        if ret != 0:
            logger.error(f"[Startup] Error enumerating devices! ret=0x{ret:x}")
            sys.exit(1)

        if deviceList.nDeviceNum == 0:
            logger.error("[Startup] No cameras found!")
            sys.exit(1)

        logger.info(f"[Startup] Found {deviceList.nDeviceNum} camera enumeration entry(s)")
        log_detected_devices(deviceList)

        matched_devices = build_camera_mapping(deviceList)

        required_internal_ids = [0, 1]
        missing_ids = [cid for cid in required_internal_ids if cid not in matched_devices]
        if missing_ids:
            logger.error(
                f"[Startup] Required camera(s) missing for internal ids: {missing_ids}. "
                f"Expected serial mapping: 0->{GIGE_SERIAL}, 1->{USB_SERIAL}"
            )
            sys.exit(1)

        logger.info("[Startup] Final camera mapping by serial number:")
        for internal_id in sorted(matched_devices.keys()):
            info = matched_devices[internal_id]
            logger.info(
                f"  Internal {internal_id} -> SDK index {info['sdk_index']} | "
                f"Type={info['type']} | Model={info['model']} | Serial={info['serial']}"
            )

        num_cameras_to_run = len(required_internal_ids)
        logger.info(f"[Startup] Will run {num_cameras_to_run} camera(s)")

        for internal_id in required_internal_ids:
            config = CAMERA_CONFIGS[internal_id]
            info = matched_devices[internal_id]
            stDeviceList = info["stDeviceList"]
            serial_number = info["serial"]

            latest_frames[internal_id] = None
            frame_locks[internal_id] = threading.Lock()

            exposure_time = EXPOSURE_MAP.get(internal_id, 30000)

            grabber_thread = threading.Thread(
                target=grab_camera,
                args=(internal_id, stDeviceList, exposure_time, serial_number),
                daemon=True,
                name=f"Grabber-{config['name']}"
            )

            pipeline_thread = threading.Thread(
                target=processing_pipeline_thread,
                args=(internal_id, config["name"], config["folder"]),
                daemon=True,
                name=f"Pipeline-{config['name']}"
            )

            threads.append(grabber_thread)
            threads.append(pipeline_thread)

            grabber_thread.start()
            pipeline_thread.start()

            logger.info(
                f"[Startup] Started internal camera={internal_id}, "
                f"name={config['name']}, folder={config['folder']}, "
                f"exposure={exposure_time}, serial={serial_number}, sdk_index={info['sdk_index']}"
            )

        logger.info("------ Web Streams ------")
        logger.info(f"Camera 0 (GigE / cam1): http://127.0.0.1:5000/video_feed/0")
        logger.info(f"Camera 1 (USB  / cam2): http://127.0.0.1:5000/video_feed/1")
        logger.info("-------------------------")

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
        logger.exception("[Main] Unhandled exception in main execution block")
        sys.exit(1)

    finally:
        running = False

        try:
            for t in threads:
                t.join(timeout=2.0)
        except Exception:
            logger.exception("[Main] Error while joining threads")

        try:
            MvCamera.MV_CC_Finalize()
            logger.info("[Shutdown] MVS SDK finalized. Exiting.")
        except Exception:
            logger.exception("[Shutdown] Error during MVS SDK finalization")

        sys.exit(0)
