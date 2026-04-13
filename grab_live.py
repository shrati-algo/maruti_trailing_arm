import cv2
import threading
import sys
import time
from flask import Flask, Response

sys.path.append(r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport")

from frame_grabbing import MvFrameGrabber

latest_frame = None
frame_lock = threading.Lock()
running = True

def grab_loop(cam_index=0):
    global latest_frame, running
    grabber = MvFrameGrabber(cam_index, fps=20)
    grabber.open()
    while running:
        frame = grabber.read()
        if frame is not None:
            frame = cv2.resize(frame, (640, 480))
            with frame_lock:
                latest_frame = frame.copy()
        else:
            time.sleep(0.005)
    grabber.close()

app = Flask(__name__)

def generate_stream():
    global latest_frame
    while running:
        with frame_lock:
            frame = latest_frame.copy() if latest_frame is not None else None
        if frame is None:
            time.sleep(0.01)
            continue
        ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

@app.route('/video_feed')
def video_feed():
    return Response(generate_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == "__main__":
    t = threading.Thread(target=grab_loop, args=(0,), daemon=True)
    t.start()
    print("Stream URL: http://127.0.0.1:5000/video_feed")
    try:
        app.run(host="0.0.0.0", port=5000, threaded=True)
    except KeyboardInterrupt:
        running = False
        sys.exit(0)
