import cv2
import numpy as np
import time
import threading
from ctypes import *
from MvCameraControl_class import *

# ----------------- Global Variables -----------------
latest_frames = {"cam1": None, "cam2": None}
frame_locks = {"cam1": threading.Lock(), "cam2": threading.Lock()}
running = True

# 🔹 Fixed mapping: assign your serial numbers
# Left camera → cam1, Right camera → cam2
CAMERA_MAP = {
    "00D24567089A": "cam1",     # Example USB3V left camera serial
    "DA5606439" : "cam2"   # Example GigE right camera serial
}


# ----------------- Helper Functions -----------------
def get_camera_identifier(stDevInfo):
    """Extract unique identifier (serial number) from device info."""
    if stDevInfo.nTLayerType == MV_GIGE_DEVICE:
        return stDevInfo.SpecialInfo.stGigEInfo.chSerialNumber.decode("utf-8")
    elif stDevInfo.nTLayerType == MV_USB_DEVICE:
        return stDevInfo.SpecialInfo.stUsb3VInfo.chSerialNumber.decode("utf-8")
    else:
        return None


def grab_camera(role, stDevInfo):
    """
    Connects to a camera by role (cam1/cam2), grabs frames,
    and places them in shared latest_frames dictionary.
    """
    global latest_frames, running

    print(f"[{role}] Starting grab thread...")

    cam = MvCamera()
    ret = cam.MV_CC_CreateHandle(stDevInfo)
    if ret != 0:
        print(f"[{role}] Error creating handle: {ret}")
        return

    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    if ret != 0:
        print(f"[{role}] Error opening device: {ret}")
        return

    ret = cam.MV_CC_StartGrabbing()
    if ret != 0:
        print(f"[{role}] Error starting grabbing: {ret}")
        return

    stOutFrame = MV_FRAME_OUT()
    img_buff = None

    while running:
        ret = cam.MV_CC_GetImageBuffer(stOutFrame, 1000)
        if ret == 0:
            width = stOutFrame.stFrameInfo.nWidth
            height = stOutFrame.stFrameInfo.nHeight

            if img_buff is None:
                img_buff = (c_ubyte * (width * height * 3))()

            stConvertParam = MV_CC_PIXEL_CONVERT_PARAM()
            stConvertParam.nWidth = width
            stConvertParam.nHeight = height
            stConvertParam.pSrcData = cast(stOutFrame.pBufAddr, POINTER(c_ubyte))
            stConvertParam.nSrcDataLen = stOutFrame.stFrameInfo.nFrameLen
            stConvertParam.enSrcPixelType = stOutFrame.stFrameInfo.enPixelType
            stConvertParam.enDstPixelType = PixelType_Gvsp_RGB8_Packed
            stConvertParam.pDstBuffer = img_buff
            stConvertParam.nDstBufferSize = width * height * 3
            ret = cam.MV_CC_ConvertPixelType(stConvertParam)

            if ret == 0:
                frame = np.asarray(img_buff).reshape((height, width, 3))
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

                with frame_locks[role]:
                    latest_frames[role] = frame.copy()

            cam.MV_CC_FreeImageBuffer(stOutFrame)
        else:
            time.sleep(0.005)

    cam.MV_CC_StopGrabbing()
    cam.MV_CC_CloseDevice()
    cam.MV_CC_DestroyHandle()
    print(f"[{role}] Stopped grab thread.")


def init_cameras():
    """Initialize cameras and assign cam1/cam2 based on serial mapping."""
    deviceList = MV_CC_DEVICE_INFO_LIST()
    ret = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, deviceList)
    if ret != 0 or deviceList.nDeviceNum == 0:
        print("No cameras found.")
        return {}

    camera_handles = {}

    for i in range(deviceList.nDeviceNum):
        stDevInfo = cast(deviceList.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
        serial = get_camera_identifier(stDevInfo)

        if serial in CAMERA_MAP:
            role = CAMERA_MAP[serial]   # cam1 or cam2
            camera_handles[role] = stDevInfo
            print(f"Assigned {serial} -> {role}")

    return camera_handles
