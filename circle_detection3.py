import cv2
import numpy as np
import os
import csv
from project_paths import LOG_DIR

# ----------------------------------------
# Calibration constants (updated camera-wise)
# ----------------------------------------
camera_ppm = {
    "cam1": 17.0,
    "cam2": 17.0,
    "yed": 19.02
}

os.makedirs(LOG_DIR, exist_ok=True)
csv_file = os.path.join(LOG_DIR, "detected_diameters.csv")

# Initialize CSV with header
if not os.path.exists(csv_file):
    with open(csv_file, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Filename", "Camera", "Model", "Detected_Diameter_mm", "Status"])


# ----------------------------------------
# 1️⃣ Get target diameter + camera name
# ----------------------------------------
def get_target_diameter(filename):
    """
    Extract cam_name and model_name from filename and set target diameter and pixels_per_mm.
    Filename format: {cam_name}__{model_name}__{chassis_no}__{timestamp}.jpg
    """
    base_name = os.path.basename(filename)
    parts = base_name.split("__")

    cam_name = parts[0].lower() if len(parts) > 0 else "unknown"
    model_name = parts[1].upper() if len(parts) > 1 else "YCA"

    # Default target diameters
    if model_name == "YCA":
        target_diameter = 13.0
    elif model_name == "YED":
        target_diameter = 15.0
    else:
        target_diameter = 13.0

    # Pick pixels-per-mm based on camera
    if "cam1" in cam_name:
        ppm = camera_ppm["cam1"]
    elif "cam2" in cam_name:
        ppm = camera_ppm["cam2"]
    elif model_name == "YED":
        ppm = camera_ppm["yed"]
    else:
        ppm = 19.02  # fallback

    return target_diameter, ppm, cam_name, model_name


# ----------------------------------------
# 2️⃣ Preprocessing
# ----------------------------------------
def process_image(img):
    """Preprocess: grayscale, blur, threshold, and morphological close."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        51, 10
    )
    kernel = np.ones((3, 3), np.uint8)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)
    return gray, thresh


# ----------------------------------------
# 3️⃣ Diameter conversion
# ----------------------------------------
def classify_by_size(main_radius, ppm):
    """Convert detected radius (in pixels) to diameter in mm."""
    detected_diameter_mm = (2 * main_radius) / ppm
    return detected_diameter_mm


# ----------------------------------------
# 4️⃣ Detect and classify
# ----------------------------------------
def detect_single(img, target_diameter_mm, ppm, return_circle=False):
    """
    Detect if a valid circle is present and classify by size.
    Returns: status (0=Not OK, 1=Somewhat OK, 2=OK)
    If return_circle=True, also returns (x, y, r, diameter_mm) or None if not found.
    """
    gray, thresh = process_image(img)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for cnt in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(cnt) < 50:
            continue

        mask = np.zeros_like(gray)
        cv2.drawContours(mask, [cnt], -1, 255, thickness=cv2.FILLED)
        masked_gray = cv2.bitwise_and(gray, gray, mask=mask)

        min_radius = int((target_diameter_mm - 0.3) / 2 * ppm)
        max_radius = int((target_diameter_mm + 0.3) / 2 * ppm)

        circles = cv2.HoughCircles(
            masked_gray,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=10,
            param1=50,
            param2=20,
            minRadius=min_radius,
            maxRadius=max_radius
        )

        if circles is not None:
            x, y, r = np.uint16(np.around(circles))[0, 0]
            detected_diameter = classify_by_size(r, ppm)

            # Refined classification
            if (target_diameter_mm - 0.2) <= detected_diameter <= (target_diameter_mm + 0.2):
                status = 2  # OK
            elif (target_diameter_mm - 0.5) <= detected_diameter <= (target_diameter_mm + 0.5):
                status = 1  # Somewhat OK
            else:
                status = 0  # Not OK

            return (status, (x, y, r, detected_diameter)) if return_circle else status

    # 🚨 No circle detected
    return (0, None) if return_circle else 0


# ----------------------------------------
# 5️⃣ Draw reference
# ----------------------------------------
def draw_reference_circles(img, x, y, target_diameter_mm, ppm):
    """Draw reference circle based on target diameter."""
    radius_target = int((target_diameter_mm / 2) * ppm)
    cv2.circle(img, (x, y), radius_target, (0, 255, 0), 2)  # Green


# ----------------------------------------
# 6️⃣ Detect & Save
# ----------------------------------------
def detect_circle(image_path, output_folder=r"images"):
    """
    Detect circle, classify, draw references, save output, return saved path.
    """
    if not os.path.exists(image_path):
        print(f"Error: Cannot load image at {image_path}")
        return None

    target_diameter_mm, ppm, cam_name, model_name = get_target_diameter(image_path)
    img = cv2.imread(image_path)
    output_img = img.copy()

    status, circle_params = detect_single(img, target_diameter_mm, ppm, return_circle=True)

    if circle_params is not None:
        x, y, r, detected_diameter = circle_params

        if status == 2:
            text = "OK"
        elif status == 1:
            text = "Somewhat OK"
        else:
            text = "Not OK"

        cv2.putText(output_img, text, (x - 60, y - r - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 3)
        draw_reference_circles(output_img, x, y, target_diameter_mm, ppm)

    else:
        detected_diameter = None
        status = 0
        cv2.putText(output_img, "Not OK", (50, output_img.shape[0] // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 4)

    # Save CSV log
    with open(csv_file, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            os.path.basename(image_path),
            cam_name,
            model_name,
            f"{detected_diameter:.2f}" if detected_diameter else "N/A",
            status
        ])

    os.makedirs(output_folder, exist_ok=True)
    output_path = os.path.join(output_folder, os.path.basename(image_path))
    # output_path= output_path.rsplit('.',1)[0] + '.webp'
    # cv2.imwrite(output_path, output_img, [cv2.IMWRITE_WEBP_QUALITY,10])
    cv2.imwrite(output_path, output_img, [int(cv2.IMWRITE_JPEG_QUALITY),10])
    return output_path


# ----------------------------------------
# 7️⃣ Dual detection
# ----------------------------------------
def detect(image1_path, image2_path):
    """
    Detect status for two images. Automatically uses target diameter from filename model_name.
    Returns a tuple of results (status1, status2)
    """
    results = []
    for path in [image1_path, image2_path]:
        if path and os.path.exists(path):
            target_diameter_mm, ppm, cam_name, model_name = get_target_diameter(path)
            img = cv2.imread(path)
            if img is None:
                print(f"[ERROR] Failed to read: {path}")
                results.append(None)
            else:
                results.append(detect_single(img, target_diameter_mm, ppm))
        else:
            results.append(None)
    return tuple(results)
