import os
import cv2
import numpy as np
import threading
import sys
import time
from datetime import datetime
from flask import Flask, Response
from MvCameraControl_class import *  # MVS SDK
import shutil
from plc_process import process_frame

# --- Camera configuration ---
CAMERA_CONFIGS = [
    {"name": "cam1", "folder": "image_data/cam1"},
    {"name": "cam2", "folder": "image_data/cam2"},
    # Add more camera configs if needed
]

latest_frames = {}
frame_locks = {}
running = True
g_bExit = False

def grab_camera(cam_index, cam_name, folder):
    """
    Grabs frames from Hikrobot camera and passes to process_frame.
    """
    os.makedirs(folder, exist_ok=True)
    print(f"[CAM INIT] {cam_name} starting...")
    deviceList = MV_CC_DEVICE_INFO_LIST()
    MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, deviceList)
    if deviceList.nDeviceNum == 0:
        print("[ERROR] No camera found")
        return
    if cam_index >= deviceList.nDeviceNum:
        print(f"[ERROR] Invalid cam_index {cam_index}")
        return
    stDeviceList = cast(deviceList.pDeviceInfo[cam_index], POINTER(MV_CC_DEVICE_INFO)).contents
    cam = MvCamera()
    cam.MV_CC_CreateHandle(stDeviceList)
    cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    cam.MV_CC_StartGrabbing()
    data_size = 1920 * 1080 * 3
    data_buf = (c_ubyte * data_size)()
    print(f"[CAM] {cam_name} started...")
    while not g_bExit:
        stFrameInfo = MV_FRAME_OUT_INFO_EX()
        ret = cam.MV_CC_GetOneFrameTimeout(data_buf, data_size, stFrameInfo, 1000)
        if ret == 0:
            img = np.frombuffer(
                data_buf, dtype=np.uint8,
                count=stFrameInfo.nWidth * stFrameInfo.nHeight * 3
            )
            img = img.reshape((stFrameInfo.nHeight, stFrameInfo.nWidth, 3))
            process_frame(cam_name, img, folder)
        else:
            print(f"[WARN] {cam_name}: Frame grab timeout")
    cam.MV_CC_StopGrabbing()
    cam.MV_CC_CloseDevice()
    cam.MV_CC_DestroyHandle()
    print(f"[CAM] {cam_name} stopped.")

def main():
    global g_bExit
    threads = []
    try:
        for i, cam_cfg in enumerate(CAMERA_CONFIGS):
            t = threading.Thread(target=grab_camera, args=(i, cam_cfg["name"], cam_cfg["folder"]), daemon=True)
            t.start()
            threads.append(t)
        print("[MAIN] Camera threads started. Press Ctrl+C to stop.")
        while not g_bExit:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[MAIN] Caught KeyboardInterrupt, shutting down...")
        g_bExit = True
        # Optionally: join threads if non-daemon
        for t in threads:
            t.join()
        print("[MAIN] All threads exited. Exit complete.")

if __name__ == "__main__":
    main()
