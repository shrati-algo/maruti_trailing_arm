import cv2
import time
from queue import Queue
from flask import Flask, Response
from threading import Thread

from frame_grab import grab_from_camera   # your grabber (modified to push frames)

# -------------------
# FPS Controls
# -------------------
PROCESS_FPS = 4   # processing rate
LIVE_FPS = 25    # live streaming rate

# -------------------
# Camera Configurations
# -------------------
CAMERAS = [
    {"index": 0, "name": "Cam0", "folder": "output_cam0"},
    {"index": 1, "name": "Cam1", "folder": "output_cam1"},
    # add more cameras here
]

# Each camera gets its own queue
frame_queues = {cam["index"]: Queue(maxsize=10) for cam in CAMERAS}

app = Flask(__name__)

# -------------------
# Grab frames from camera (runs in its own thread)
# -------------------
def start_grabber(cam_index, cam_name, folder):
    """
    Call grab_from_camera as-is, but frames are pushed to frame_queues[cam_index]
    inside grab_from_camera (using the global variable).
    """
    grab_from_camera(
        cam_index=cam_index,
        cam_name=cam_name,
        folder=folder
    )

# -------------------
# Live view generator for a given camera
# -------------------
def generate_frames(cam_index):
    q = frame_queues[cam_index]
    while True:
        frame = q.get()

        # Resize frame to 480p (640x480)
        frame_resized = cv2.resize(frame, (640, 480))

        ret, jpeg = cv2.imencode('.jpg', frame_resized)
        if not ret:
            continue

        # Throttle live streaming FPS
        time.sleep(1.0 / LIVE_FPS)

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')

@app.route('/video_feed/<int:cam_index>')
def video_feed(cam_index):
    if cam_index not in frame_queues:
        return "Camera not found", 404
    return Response(generate_frames(cam_index),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

# -------------------
# Run everything
# -------------------
if __name__ == '__main__':
    # Make frame_queues globally visible to frame_grab
    import frame_grab
    frame_grab.frame_queues = frame_queues

    # Start grabbers for each camera
    for cam in CAMERAS:
        t = Thread(
            target=start_grabber,
            args=(cam["index"], cam["name"], cam["folder"]),
            daemon=True
        )
        t.start()
        print(f"[{cam['name']}] Grabber started...")

    # Start Flask server
    print("Starting HTTP server at http://0.0.0.0:5000/video_feed/<cam_index>")
    app.run(host='0.0.0.0', port=5000, debug=False)
