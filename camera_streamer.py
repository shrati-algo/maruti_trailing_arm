from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
import threading
import cv2
import time
import numpy as np
from MvCameraControl_class import *
from ctypes import create_string_buffer
from MvCameraControl_class import MV_FRAME_OUT_INFO_EX


app = FastAPI()

# Global dictionary to hold latest frames
latest_frames = {
    "cam1": None,
    "cam2": None
}

# Camera grabbing function
def grab_frames(device_index, cam_name):
    device_list = MV_CC_DEVICE_INFO_LIST()
    tlayer_type = MV_GIGE_DEVICE | MV_USB_DEVICE

    ret = MvCamera.MV_CC_EnumDevices(tlayer_type, device_list)
    if ret != 0:
        print(f"[ERROR] EnumDevices fail for {cam_name}: {ret}")
        return

    if device_list.nDeviceNum <= device_index:
        print(f"[ERROR] Camera index {device_index} not found")
        return

    cam = MvCamera()
    device_info = cast(device_list.pDeviceInfo[device_index], POINTER(MV_CC_DEVICE_INFO)).contents
    ret = cam.MV_CC_CreateHandle(device_info)
    if ret != 0:
        print(f"[ERROR] CreateHandle fail for {cam_name}: {ret}")
        return

    ret = cam.MV_CC_OpenDevice()
    if ret != 0:
        print(f"[ERROR] OpenDevice fail for {cam_name}: {ret}")
        return

    # Optional settings for exposure, gain, etc.
    cam.MV_CC_SetEnumValue("TriggerMode", 0)
    cam.MV_CC_SetEnumValue("TriggerSource", 7)

    ret = cam.MV_CC_StartGrabbing()
    if ret != 0:
        print(f"[ERROR] StartGrabbing fail for {cam_name}: {ret}")
        return

    data_buf = None
    # while True:
    #     data_buf = cam.MV_CC_GetOneFrameTimeout(1024*1024, 1000)
    #     if data_buf[0] == 0:
    #         n_ret, frame = data_buf
    #         # Convert raw buffer to BGR for OpenCV
    #         image = np.frombuffer(frame, dtype=np.uint8)
    #         image = image.reshape((1080, 1920, 3))  # Adjust resolution as needed
    #         latest_frames[cam_name] = image
    #     else:
    #         print(f"[WARN] Timeout getting frame from {cam_name}")
    from ctypes import create_string_buffer

    data_size = 1920 * 1080 * 3  # Or as per your resolution and pixel format
    pData = create_string_buffer(data_size)
    stFrameInfo = MV_FRAME_OUT_INFO_EX()

    while True:
        ret = cam.MV_CC_GetOneFrameTimeout(pData, data_size, stFrameInfo, 1000)
        if ret == 0:
            image = np.frombuffer(pData, dtype=np.uint8)
            try:
                image = image.reshape((stFrameInfo.nHeight, stFrameInfo.nWidth, 3))
                latest_frames[cam_name] = image
            except Exception as e:
                print(f"[ERROR] Reshape failed: {e}")
        else:
            print(f"[WARN] Failed to grab frame from {cam_name}, code: {ret}")


    cam.MV_CC_StopGrabbing()
    cam.MV_CC_CloseDevice()
    cam.MV_CC_DestroyHandle()

# Start threads
def start_camera_threads():
    threading.Thread(target=grab_frames, args=(0, "cam1"), daemon=True).start()
    threading.Thread(target=grab_frames, args=(1, "cam2"), daemon=True).start()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[INFO] Starting camera threads...")
    start_camera_threads()
    yield
    print("[INFO] Shutdown logic (if needed)")

app = FastAPI(lifespan=lifespan)

# @app.on_event("startup")
# def startup_event():
#     start_camera_threads()

@app.get("/frame/{cam_name}")
def get_frame(cam_name: str):
    frame = latest_frames.get(cam_name)
    if frame is None:
        raise HTTPException(status_code=404, detail=f"No frame from {cam_name}")
    ret, jpeg = cv2.imencode('.jpg', frame)
    return StreamingResponse(
        iter([jpeg.tobytes()]),
        media_type="image/jpeg"
    )
