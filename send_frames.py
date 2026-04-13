import base64
import cv2
import time
import requests
import threading
import numpy as np# send_frames.py

import threading
import time
import base64
import requests
from GrabImage import CameraWorker  # Make sure CameraWorker is properly defined and importable

# Replace this with actual frame grabbing logic
def encode_image_to_base64(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

class FrameCollector:
    def __init__(self):
        self.frames = {"cam1": [], "cam2": []}
        self.lock = threading.Lock()

    def add_frame(self, cam_id, image_path):
        b64_frame = encode_image_to_base64(image_path)
        with self.lock:
            self.frames[cam_id].append(b64_frame)

    def pop_frames(self):
        with self.lock:
            to_send = self.frames.copy()
            self.frames = {"cam1": [], "cam2": []}
        return to_send

frame_collector = FrameCollector()

def run_camera(cam_id, serial_number):
    worker = CameraWorker(serial_number, cam_id)
    while True:
        img_path = worker.capture_frame()  # Assumed to return path to saved image
        frame_collector.add_frame(cam_id, img_path)
        time.sleep(10)  # 0.1 fps = one frame every 10 seconds

def sender_loop():
    while True:
        time.sleep(1)  # send every second
        data = frame_collector.pop_frames()
        if any(data.values()):  # only send if there are frames
            try:
                res = requests.post("http://localhost:5002/getframes", json=data)
                print(f"Sent frames: {res.status_code}")
            except Exception as e:
                print(f"Error sending frames: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_camera, args=("cam1", "GOLA_SERIAL_1"), daemon=True).start()
    threading.Thread(target=run_camera, args=("cam2", "GOLA_SERIAL_2"), daemon=True).start()
    sender_loop()


from GrabImage import CameraWorker  # from your modified GrabImage.py

from GrabImage import capture_frames

def encode_image(img):
	_, buffer = cv2.imencode('.jpg', img)
	return base64.b64encode(buffer).decode('utf-8')

def grab_and_send():
	cam1 = CameraWorker(0)
	cam2 = CameraWorker(1)

	try:
		while True:
			frames = {
				'cam1': [],
				'cam2': []
			}

			img1 = cam1.get_frame()
			img2 = cam2.get_frame()

			if img1 is not None:
				frames['cam1'].append(encode_image(img1))
			if img2 is not None:
				frames['cam2'].append(encode_image(img2))

			try:
				resp = requests.post('http://localhost:5002/getframes', json=frames)
				print(f"[INFO] Sent frames. Response: {resp.status_code}")
			except Exception as e:
				print(f"[ERROR] Failed to send frames: {e}")

			time.sleep(1)  # send every second

	finally:
		cam1.close()
		cam2.close()

if __name__ == "__main__":
	grab_and_send()
