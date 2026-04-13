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
# Custom Imports
# -------------------------------
from plc_process import process_frame
from circle_detection3 import detect, detect_circle
from combined import pipeline, timestamp_from_img, delete_old
from Utils.push_to_db import insert_db

# -------------------------------
# Logging
# -------------------------------
import logging
from logging.handlers import RotatingFileHandler
from project_paths import LOG_DIR

logger = logging.getLogger("combined3")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False

formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(process)d | %(threadName)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "combined3.log"),
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

# Internal IDs remain fixed:
# 0 -> cam1 (GigE)
# 1 -> cam2 (USB)
EXPOSURE_MAP = {
    0: 40000,
    1: 40000,
}

CAMERA_CONFIGS = {
    0: {"name": "cam1", "folder": "image_data/cam1"},
    1: {"name": "cam2", "folder": "image_data/cam2"},
}

latest_frames = {}
frame_locks = {}
running = True
threads = []

results_buffer = {}
results_lock = threading.Lock()
inserted_joint_keys = set()
last_joint_dt = None

# -------------------------------
# Fixed serial mapping
# -------------------------------
GIGE_SERIAL = "DA5843327"   # cam1 / internal id 0
USB_SERIAL  = "DA5606439"   # cam2 / internal id 1

SERIAL_TO_INTERNAL_ID = {
    GIGE_SERIAL: 0,
    USB_SERIAL: 1,
}

# -------------------------------
# Utility helpers
# -------------------------------
def decode_char_array(arr):
    try:
        return bytes(arr).split(b'\0', 1)[0].decode("utf-8", errors="ignore").strip()
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
    Builds mapping using fixed camera serial numbers.
    Deduplicates duplicate discovery entries by serial.
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

            if serial in seen_serials:
                logger.warning(
                    f"[SDK Index {sdk_index}] Duplicate entry ignored | Type={dtype}, Model={model}, Serial={serial}"
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
                    f"[SDK Index {sdk_index}] Unused device found | Type={dtype}, Model={model}, Serial={serial}"
                )

        except Exception:
            logger.exception(f"Error while building mapping for SDK index {sdk_index}")

    return matched_devices

# -------------------------------
# Camera Grabber
# -------------------------------
# def grab_camera(cam_index, stDeviceList, exposure_time, serial_number):

#     print("Grabbing process has started")
#     global latest_frames, running

#     cam_type = get_device_type_name(stDeviceList)
#     cam_name = CAMERA_CONFIGS[cam_index]["name"]

#     logger.info(
#         f"[{cam_name}] Grab thread starting | internal_id={cam_index} | type={cam_type} | serial={serial_number}"
#     )

#     cam = MvCamera()

#     BLACK_THRESHOLD = 12
#     MAX_BLACK_FRAMES = 30
#     black_frame_count = 0

#     def open_camera():
#         ret = cam.MV_CC_CreateHandle(stDeviceList)
#         if ret != 0:
#             return False

#         ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
#         if ret != 0:
#             cam.MV_CC_DestroyHandle()
#             return False

#         if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
#             try:
#                 packet_size = cam.MV_CC_GetOptimalPacketSize()
#                 if packet_size > 0:
#                     cam.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)
#             except:
#                 pass

#         cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
#         cam.MV_CC_SetEnumValue("ExposureAuto", 0)
#         cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_time))

#         ret = cam.MV_CC_StartGrabbing()
#         return ret == 0

#     def close_camera():
#         try:
#             cam.MV_CC_StopGrabbing()
#         except:
#             pass
#         try:
#             cam.MV_CC_CloseDevice()
#         except:
#             pass
#         try:
#             cam.MV_CC_DestroyHandle()
#         except:
#             pass

#     # 🔁 initial open
#     if not open_camera():
#         logger.error(f"[{cam_name}] Initial open failed")
#         return

#     logger.info(f"[{cam_name}] Camera started")

#     stOutFrame = MV_FRAME_OUT()
#     last_no_frame_log = time.time()

#     while running:
#         memset(byref(stOutFrame), 0, sizeof(MV_FRAME_OUT))

#         ret = cam.MV_CC_GetImageBuffer(stOutFrame, 1000)

#         if ret != 0:
#             if time.time() - last_no_frame_log >= 5:
#                 logger.warning(f"[{cam_name}] No frame | ret=0x{ret:x}")
#                 last_no_frame_log = time.time()
#             time.sleep(0.005)
#             continue

#         try:
#             info = stOutFrame.stFrameInfo
#             w, h = info.nWidth, info.nHeight

#             if w <= 0 or h <= 0:
#                 continue

#             frame_len = info.nFrameLen or (w * h)

#             raw = np.ctypeslib.as_array(
#                 cast(stOutFrame.pBufAddr, POINTER(c_ubyte)),
#                 shape=(frame_len,)
#             )

#             if raw.size < w * h:
#                 continue

#             raw2d = raw[:w * h].reshape(h, w)
#             frame_bgr = cv2.cvtColor(raw2d, cv2.COLOR_BAYER_BG2BGR)

#             # -------------------------------
#             # 🔥 BLACK FRAME DETECTION
#             # -------------------------------
#             mean_val = frame_bgr.mean()

#             if mean_val < BLACK_THRESHOLD:
#                 black_frame_count += 1
#             else:
#                 black_frame_count = 0

#             if black_frame_count >= MAX_BLACK_FRAMES:
#                 logger.error(f"[{cam_name}] BLACK FRAMES DETECTED → restarting camera")

#                 close_camera()
#                 time.sleep(1)

#                 if open_camera():
#                     logger.info(f"[{cam_name}] Camera restarted successfully")
#                     black_frame_count = 0
#                     continue
#                 else:
#                     logger.error(f"[{cam_name}] Camera restart failed, retrying...")
#                     time.sleep(2)
#                     continue

#             # -------------------------------
#             # NORMAL FLOW
#             # -------------------------------
#             with frame_locks[cam_index]:
#                 latest_frames[cam_index] = frame_bgr.copy()  # safer

#         except Exception:
#             logger.exception(f"[{cam_name}] Frame processing error")

#         finally:
#             cam.MV_CC_FreeImageBuffer(stOutFrame)

#     # cleanup
#     close_camera()
#     logger.info(f"[{cam_name}] Grab thread stopped")



def grab_camera(cam_index, stDeviceList, exposure_time, serial_number):

    global latest_frames, running

    cam_name = CAMERA_CONFIGS[cam_index]["name"]
    cam_type = get_device_type_name(stDeviceList)

    logger.info(f"[{cam_name}] Grab thread starting | type={cam_type} | serial={serial_number}")

    cam = MvCamera()

    # -------------------------------
    # CONFIG
    # -------------------------------
    MAX_NO_FRAME = 50
    MAX_SAME_FRAME = 50

    no_frame_count = 0
    same_frame_count = 0
    last_frame_num = -1

    last_log_time = time.time()

    # -------------------------------
    # CAMERA OPEN
    # -------------------------------
    def open_camera():
        try:
            if cam.MV_CC_CreateHandle(stDeviceList) != 0:
                return False

            if cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0) != 0:
                cam.MV_CC_DestroyHandle()
                return False

            if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
                try:
                    packet_size = cam.MV_CC_GetOptimalPacketSize()
                    if packet_size > 0:
                        cam.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)

                    cam.MV_CC_SetBoolValue("GevPacketResend", True)
                    cam.MV_CC_SetIntValue("GevSCPD", 1000)
                    cam.MV_CC_SetIntValue("GevHeartbeatTimeout", 5000)

                except Exception:
                    logger.exception(f"[{cam_name}] GigE tuning failed")

            cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
            cam.MV_CC_SetEnumValue("ExposureAuto", 0)
            cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_time))

            try:
                cam.MV_CC_SetIntValue("ImageNodeNum", 10)
            except:
                pass

            if cam.MV_CC_StartGrabbing() != 0:
                return False

            return True

        except Exception:
            logger.exception(f"[{cam_name}] open_camera failed")
            return False

    def close_camera():
        try:
            cam.MV_CC_StopGrabbing()
        except:
            pass
        try:
            cam.MV_CC_CloseDevice()
        except:
            pass
        try:
            cam.MV_CC_DestroyHandle()
        except:
            pass

    # -------------------------------
    # INITIAL OPEN
    # -------------------------------
    if not open_camera():
        logger.error(f"[{cam_name}] Initial open failed")
        return

    logger.info(f"[{cam_name}] Camera started")

    stOutFrame = MV_FRAME_OUT()

    # -------------------------------
    # MAIN LOOP
    # -------------------------------
    while running:

        memset(byref(stOutFrame), 0, sizeof(MV_FRAME_OUT))
        ret = cam.MV_CC_GetImageBuffer(stOutFrame, 1000)

        # -------------------------------
        # NO FRAME CASE
        # -------------------------------
        if ret != 0:
            no_frame_count += 1

            logger.warning(
                f"[{cam_name}] NO FRAME | ret=0x{ret:x} | no_frame_count={no_frame_count}"
            )

            if no_frame_count >= MAX_NO_FRAME:
                logger.error(f"[{cam_name}] Too many NO FRAME → restarting camera")

                close_camera()
                time.sleep(1)

                if open_camera():
                    logger.info(f"[{cam_name}] Restart success (NO FRAME recovery)")
                    no_frame_count = 0
                    same_frame_count = 0
                    last_frame_num = -1
                    continue
                else:
                    logger.error(f"[{cam_name}] Restart failed, retrying...")
                    time.sleep(2)
                    continue

            time.sleep(0.005)
            continue

        # Reset no-frame counter
        no_frame_count = 0

        try:
            info = stOutFrame.stFrameInfo
            w, h = info.nWidth, info.nHeight
            frame_num = info.nFrameNum

            if w <= 0 or h <= 0:
                logger.warning(f"[{cam_name}] Invalid frame size | w={w}, h={h}")
                continue

            # -------------------------------
            # FRAME NUMBER LOGGING
            # -------------------------------
            logger.info(f"[{cam_name}] Frame Received | FrameNum={frame_num}")

            # -------------------------------
            # STALE FRAME DETECTION
            # -------------------------------
            if frame_num == last_frame_num:
                same_frame_count += 1

                logger.warning(
                    f"[{cam_name}] STALE FRAME | FrameNum={frame_num} | same_count={same_frame_count}"
                )
            else:
                same_frame_count = 0
                last_frame_num = frame_num

            if same_frame_count >= MAX_SAME_FRAME:
                logger.error(f"[{cam_name}] STALE FRAME LIMIT HIT → restarting camera")

                close_camera()
                time.sleep(1)

                if open_camera():
                    logger.info(f"[{cam_name}] Restart success (STALE FRAME recovery)")
                    same_frame_count = 0
                    last_frame_num = -1
                    continue
                else:
                    logger.error(f"[{cam_name}] Restart failed, retrying...")
                    time.sleep(2)
                    continue

            # -------------------------------
            # BUFFER → NUMPY
            # -------------------------------
            frame_len = info.nFrameLen or (w * h)

            raw = np.ctypeslib.as_array(
                cast(stOutFrame.pBufAddr, POINTER(c_ubyte)),
                shape=(frame_len,)
            )

            if raw.size < w * h:
                logger.error(f"[{cam_name}] Buffer too small | size={raw.size}")
                continue

            raw2d = raw[:w * h].reshape(h, w)

            # -------------------------------
            # CONVERT
            # -------------------------------
            try:
                frame_bgr = cv2.cvtColor(raw2d, cv2.COLOR_BAYER_BG2BGR)
            except Exception:
                logger.warning(f"[{cam_name}] Bayer convert failed, fallback GRAY")
                frame_bgr = cv2.cvtColor(raw2d, cv2.COLOR_GRAY2BGR)

            # -------------------------------
            # STORE FRAME
            # -------------------------------
            with frame_locks[cam_index]:
                latest_frames[cam_index] = frame_bgr.copy()

        except Exception:
            logger.exception(f"[{cam_name}] Frame processing error")

        finally:
            cam.MV_CC_FreeImageBuffer(stOutFrame)

    # -------------------------------
    # CLEANUP
    # -------------------------------
    close_camera()
    logger.info(f"[{cam_name}] Grab thread stopped")

# def grab_camera(cam_index, stDeviceList, exposure_time, serial_number):
#     print("Grabbing process has started")
#     global latest_frames, running

#     cam_type = get_device_type_name(stDeviceList)
#     cam_name = CAMERA_CONFIGS[cam_index]["name"]

#     logger.info(
#         f"[{cam_name}] Grab thread starting | internal_id={cam_index} | type={cam_type} | serial={serial_number}"
#     )

#     cam = MvCamera()
#     first_frame_logged = False
#     last_no_frame_log = time.time()

#     try:
#         # Create handle to the camera
#         ret = cam.MV_CC_CreateHandle(stDeviceList)
#         if ret != 0:
#             logger.error(f"[{cam_name}] CreateHandle failed: 0x{ret:x}")
#             return

#         # Open device for exclusive access
#         ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
#         if ret != 0:
#             logger.error(f"[{cam_name}] OpenDevice failed: 0x{ret:x} | type={cam_type} | serial={serial_number}")
#             try:
#                 cam.MV_CC_DestroyHandle()
#             except Exception:
#                 logger.exception(f"[{cam_name}] DestroyHandle cleanup failed")
#             return
#         else:
#             logger.info(f"[{cam_name}] Device opened successfully")

#         # Set optimal packet size if GigE camera
#         if stDeviceList.nTLayerType == MV_GIGE_DEVICE:
#             try:
#                 nPacketSize = cam.MV_CC_GetOptimalPacketSize()
#                 if int(nPacketSize) > 0:
#                     ret_packet = cam.MV_CC_SetIntValue("GevSCPSPacketSize", nPacketSize)
#                     if ret_packet == 0:
#                         logger.info(f"[{cam_name}] Packet size set to {nPacketSize}")
#                     else:
#                         logger.warning(f"[{cam_name}] Packet size set failed: 0x{ret_packet:x}")
#             except Exception:
#                 logger.exception(f"[{cam_name}] Failed setting packet size")

#         # Configure exposure settings
#         ret = cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
#         if ret != 0:
#             logger.warning(f"[{cam_name}] TriggerMode OFF failed: 0x{ret:x}")

#         ret = cam.MV_CC_SetEnumValue("ExposureAuto", 0)
#         if ret != 0:
#             logger.warning(f"[{cam_name}] ExposureAuto OFF failed: 0x{ret:x}")

#         ret = cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_time))
#         if ret != 0:
#             logger.warning(f"[{cam_name}] ExposureTime set failed: 0x{ret:x}")
#         else:
#             logger.info(f"[{cam_name}] ExposureTime set to {exposure_time}")

#         # Start grabbing frames
#         ret = cam.MV_CC_StartGrabbing()
#         if ret != 0:
#             logger.error(f"[{cam_name}] StartGrabbing failed: 0x{ret:x}")
#             return
#         logger.info(f"[{cam_name}] Grabbing started successfully")

#         while running:
#             stOutFrame = MV_FRAME_OUT()
#             memset(byref(stOutFrame), 0, sizeof(MV_FRAME_OUT))

#             ret = cam.MV_CC_GetImageBuffer(stOutFrame, 1000)

#             if ret != 0:
#                 if time.time() - last_no_frame_log >= 5:
#                     logger.warning(f"[{cam_name}] No frame received for 5 seconds | ret=0x{ret:x}")
#                     last_no_frame_log = time.time()
#                 time.sleep(0.01)
#                 continue

#             try:
#                 width = stOutFrame.stFrameInfo.nWidth
#                 height = stOutFrame.stFrameInfo.nHeight
#                 frame_len = stOutFrame.stFrameInfo.nFrameLen
#                 pixel_type = stOutFrame.stFrameInfo.enPixelType

#                 if width <= 0 or height <= 0:
#                     logger.error(f"[{cam_name}] Invalid frame size | width={width}, height={height}")
#                     continue

#                 buf_len = frame_len if frame_len and frame_len > 0 else width * height

#                 raw = np.frombuffer(
#                     string_at(stOutFrame.pBufAddr, buf_len),
#                     dtype=np.uint8
#                 )

#                 if raw.size < width * height:
#                     logger.error(
#                         f"[{cam_name}] Raw buffer too small | got={raw.size}, need={width * height}"
#                     )
#                     continue

#                 raw2d = raw[:width * height].reshape((height, width))
#                 frame_bgr = cv2.cvtColor(raw2d, cv2.COLOR_BAYER_BG2BGR)

#                 with frame_locks[cam_index]:
#                     latest_frames[cam_index] = frame_bgr.copy()

#                 if not first_frame_logged:
#                     logger.info(
#                         f"[{cam_name}] First frame received | w={width}, h={height}, pixel_type=0x{pixel_type:x}"
#                     )
#                     first_frame_logged = True

#             except Exception:
#                 logger.exception(f"[{cam_name}] Error while decoding frame")

#             finally:
#                 try:
#                     cam.MV_CC_FreeImageBuffer(stOutFrame)
#                 except Exception:
#                     logger.exception(f"[{cam_name}] FreeImageBuffer failed")

#     except Exception:
#         logger.exception(f"[{cam_name}] Exception in grab loop")

#     finally:
#         try:
#             cam.MV_CC_StopGrabbing()
#         except Exception:
#             logger.exception(f"[{cam_name}] StopGrabbing error")

        # try:
        #     cam.MV_CC_CloseDevice()
        # except Exception:
        #     logger.exception(f"[{cam_name}] CloseDevice error")

        # try:
        #     cam.MV_CC_DestroyHandle()
        # except Exception:
        #     logger.exception(f"[{cam_name}] DestroyHandle error")

        # logger.info(f"[{cam_name}] Grab thread stopped")



# -------------------------------
# Processing Pipeline
# -------------------------------
def processing_pipeline_thread(cam_index, cam_name, folder):

    print("processing has started")
    global latest_frames, running, results_lock, inserted_joint_keys, last_joint_dt

    os.makedirs(folder, exist_ok=True)

    try:
        delete_old(folder, 60)
    except Exception:
        logger.exception(f"[Pipeline-{cam_name}] delete_old failed for {folder}")

    logger.info(f"[Pipeline-{cam_name}] Processing thread started | folder={folder}")

    last_timestamp = None
    last_no_frame_log = time.time()

    try:
        while running:
            frame_to_process = None

            with frame_locks[cam_index]:
                if latest_frames.get(cam_index) is not None:
                    frame_to_process = latest_frames[cam_index].copy()

            if frame_to_process is None:
                if time.time() - last_no_frame_log >= 5:
                    logger.warning(f"[Pipeline-{cam_name}] No frame available for processing")
                    last_no_frame_log = time.time()
                time.sleep(0.01)
                continue

            try:
                process_frame(cam_name, frame_to_process, folder)
            except Exception:
                logger.exception(f"[Pipeline-{cam_name}] Error in process_frame")
                time.sleep(0.01)
                continue

            img_from_pipeline = None
            try:
                img_from_pipeline = pipeline(folder)
            except Exception:
                logger.exception(f"[Pipeline-{cam_name}] Error in pipeline()")
                time.sleep(0.01)
                continue

            if img_from_pipeline is None:
                time.sleep(0.01)
                continue

            try:
                detect_ret = detect(img_from_pipeline, None)
                result, _ = detect_ret if isinstance(detect_ret, tuple) else (detect_ret, None)
            except Exception:
                logger.exception(f"[Pipeline-{cam_name}] Error in detect()")
                result = None

            try:
                detect_circle(img_from_pipeline)
            except Exception:
                logger.exception(f"[Pipeline-{cam_name}] Error in detect_circle()")

            times = None
            try:
                times = timestamp_from_img(img_from_pipeline)
            except Exception:
                logger.exception(f"[Pipeline-{cam_name}] Error in timestamp_from_img()")

            if not times:
                time.sleep(0.01)
                continue

            try:
                times_dt = datetime.strptime(times, "%Y-%m-%d %H:%M:%S") if isinstance(times, str) else times
                logger.info(f"[Pipeline-{cam_name}] Valid timestamp extracted: {times_dt}")

                if last_timestamp is None or (times_dt - last_timestamp).total_seconds() >= 30:
                    with results_lock:
                        results_buffer[cam_name] = {
                            "img": img_from_pipeline,
                            "result": result,
                            "time_dt": times_dt,
                            "time_raw": times if isinstance(times, str) else times_dt.strftime("%Y-%m-%d %H:%M:%S")
                        }

                        if "cam1" in results_buffer and "cam2" in results_buffer:
                            c1 = results_buffer["cam1"]
                            c2 = results_buffer["cam2"]

                            joint_time_dt = min(c1["time_dt"], c2["time_dt"])
                            joint_time_str = joint_time_dt.strftime("%Y-%m-%d %H:%M:%S")

                            if (last_joint_dt is None) or ((joint_time_dt - last_joint_dt).total_seconds() >= 30):
                                if joint_time_str not in inserted_joint_keys:
                                    inserted_joint_keys.add(joint_time_str)
                                    try:
                                        insert_db(
                                            c1["img"],
                                            c2["img"],
                                            c1["result"],
                                            c2["result"],
                                            joint_time_str
                                        )
                                        logger.info(f"[DB] Joint insert successful at {joint_time_str}")
                                        last_joint_dt = joint_time_dt
                                        results_buffer.clear()
                                    except Exception:
                                        logger.exception("[DB] Joint insert failed")
                                        inserted_joint_keys.discard(joint_time_str)

                    last_timestamp = times_dt

            except Exception:
                logger.exception(f"[Pipeline-{cam_name}] Timestamp parse/logic failed")

            time.sleep(0.01)

    except Exception:
        logger.exception(f"[Pipeline-{cam_name}] Unhandled exception in processing loop")

    finally:
        logger.info(f"[Pipeline-{cam_name}] Processing thread stopped")

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

            ret, buffer = cv2.imencode(
                ".jpg",
                frame_resized,
                [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )

            if not ret:
                logger.error(f"[Stream-{CAMERA_CONFIGS[cam_index]['name']}] JPEG encode failed")
                time.sleep(0.05)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )

            time.sleep(0.05)

        except Exception:
            logger.exception(f"[Stream-{CAMERA_CONFIGS[cam_index]['name']}] Exception while streaming frame")
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
        logger.info("[Shutdown] Signal received, stopping all threads")
        running = False

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop_all)
    signal.signal(signal.SIGTERM, _stop_all)

    for internal_id, cfg in CAMERA_CONFIGS.items():
        os.makedirs(cfg["folder"], exist_ok=True)
        logger.info(f"[Startup] Ensured folder exists: {cfg['folder']}")

    logger.info("[Startup] Initializing MVS SDK")
    MvCamera.MV_CC_Initialize()

    try:
        deviceList = MV_CC_DEVICE_INFO_LIST()
        memset(byref(deviceList), 0, sizeof(deviceList))

        ret = MvCamera.MV_CC_EnumDevices(
            MV_GIGE_DEVICE | MV_USB_DEVICE,
            deviceList
        )

        if ret != 0:
            logger.error(f"[Startup] Error enumerating devices | ret=0x{ret:x}")
            sys.exit(1)

        if deviceList.nDeviceNum == 0:
            logger.error("[Startup] No cameras found")
            sys.exit(1)

        logger.info(f"[Startup] Found {deviceList.nDeviceNum} camera enumeration entry(s)")
        log_detected_devices(deviceList)

        matched_devices = build_camera_mapping(deviceList)

        required_internal_ids = sorted(CAMERA_CONFIGS.keys())
        missing_ids = [cid for cid in required_internal_ids if cid not in matched_devices]
        if missing_ids:
            logger.error(
                f"[Startup] Required camera(s) missing for internal ids: {missing_ids}. "
                f"Expected serial mapping: 0->{GIGE_SERIAL}, 1->{USB_SERIAL}"
            )
            sys.exit(1)

        logger.info("[Startup] Final serial-based camera mapping:")
        for internal_id in required_internal_ids:
            info = matched_devices[internal_id]
            logger.info(
                f"  Internal {internal_id} -> SDK index {info['sdk_index']} | "
                f"name={CAMERA_CONFIGS[internal_id]['name']} | "
                f"type={info['type']} | model={info['model']} | serial={info['serial']}"
            )

        for internal_id in required_internal_ids:
            config = CAMERA_CONFIGS[internal_id]
            info = matched_devices[internal_id]
            stDeviceList = info["stDeviceList"]
            serial_number = info["serial"]
            exposure_time = EXPOSURE_MAP.get(internal_id, 30000)

            latest_frames[internal_id] = None
            frame_locks[internal_id] = threading.Lock()

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
                f"[Startup] Started {config['name']} | internal_id={internal_id} | "
                f"sdk_index={info['sdk_index']} | serial={serial_number} | "
                f"type={info['type']} | exposure={exposure_time} | folder={config['folder']}"
            )

        logger.info("------ Web Streams ------")
        logger.info("cam1 (internal_id=0): http://127.0.0.1:5000/video_feed/0")
        logger.info("cam2 (internal_id=1): http://127.0.0.1:5000/video_feed/1")
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
