import sys
import os
import uuid
# # # Add the root project directory to PYTHONPATH
# sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime
from data_base.database import append_row_to_table
from logger_sqlite import insert_sqlite_db

# Add root to path
#sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from datetime import datetime, time

def get_shift(timestamp: str) -> str:
    # Convert string timestamp to datetime object
    dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")  # format: 2025-09-01 15:10:00
    t = dt.time()

    # Define shift ranges
    shift_A_start = time(6, 30,00)   # 06:30
    shift_A_end   = time(14, 30,59)  # 15:15
    
    shift_B_start = time(14, 31,00)  # 15:15
    shift_B_end   = time(22, 30,59)  # till midnight
    
    shift_C_start = time(22, 31,00)    # midnight
    shift_C_end   = time(6, 29,59)   # 06:30
   
    # Check shift
    if shift_A_start <= t < shift_A_end:
        return "A"
    elif shift_B_start <= t < time(23,59,59) or t < shift_C_end:  
        return "B"
    else:
        return "C"

import os
def chassis_from_img(path_or_img):
    """
    Extract chassis number from filename.
    Format: <cam_name>__<model_name>__<chassis_no>__<YYYYMMDD_HHMMSS_microsec>.jpg
    Example: cam1__702__26872__20250925_150619_123456.jpg
    Returns:
        chassis_no (string) or None if parsing fails
    """
    if isinstance(path_or_img, str):  # file path
        try:
            filename = os.path.basename(path_or_img)
            parts = filename.rsplit(".", 1)[0].split("__")

            if len(parts) != 4:
                raise ValueError(f"Unexpected filename format: {filename}")

            return parts[2]  # chassis_no

        except Exception as e:
            print(f"[ERROR] Failed to parse chassis number from {path_or_img}: {e}")
            return None

    return None



def insert_db(filename1, filename2, cam1_status, cam2_status, timestamp):
    # Common metadata
    timestamp = timestamp
    shift = get_shift(timestamp=timestamp)
    area = "Welding"

    cam1_status = cam1_status if cam1_status is not None else 0
    cam2_status = cam2_status if cam2_status is not None else 0

    overallStatus = min(cam1_status, cam2_status)
    overallCondition = ["Not Okay", "Somewhat Okay", "Okay"][overallStatus]


    print(overallCondition)

    isFlagged = 0
    flaggedBy = None
    flaggedAt = None

    # Generate unique chassisNo using timestamp
    chassisNo1 = chassis_from_img(filename1) # Millisecond 
    
    productionId= chassisNo1
    productionId = append_row_to_table("Productions", (chassisNo1, timestamp, shift, area, overallCondition,isFlagged, flaggedBy, flaggedAt))
    if productionId is None:
        print(f"[ERROR] Failed to insert production row for chassis {chassisNo1}")
        return False
   

    isViolationCorrect = None
    incorrectViolationReason = None

# Cam1 row
    cameraLabel_cam1 = "Left Camera"
    imageURL_cam1 = os.path.basename(filename1) if filename1 else "Image not found"
    imageCondition = ["Not Okay", "Somewhat Okay", "Okay"][cam1_status]

    print(imageURL_cam1)

    cam1_row = (
        productionId,
        timestamp,
        cameraLabel_cam1,
        imageURL_cam1,
        imageCondition,
        isViolationCorrect,
        incorrectViolationReason
    )
    print(f"[INFO] Inserting Cam1 row into Violations:\n  {cam1_row}")

    # Cam2 row
    cameraLabel_cam2 = "Right Camera"
    imageURL_cam2 = os.path.basename(filename2) if filename2 else "Image not found"
    imageCondition = ["Not Okay", "Somewhat Okay", "Okay"][cam2_status]
    
    cam2_row = (
        productionId,
        timestamp,
        cameraLabel_cam2,
        imageURL_cam2,
        imageCondition,
        isViolationCorrect,
        incorrectViolationReason,
    )
    
    print(f"[INFO] Inserting Cam2 row into Violations:\n  {cam2_row}")

        # === ALERTS TABLE INSERTION ===
    status_messages = {
    0: "not okay",
    1: "somewhat okay",
    2: "okay"
    }
    
    status_text= status_messages.get(overallStatus, "Unknown status")
    alertMessage = f"Chassis No. {chassisNo1}  {status_text}"

    alert_row = (
        productionId,
        timestamp,        # alertTimestamp
        overallCondition, # alertCondition
        alertMessage     #alert message
    )
    
    if overallStatus!=2:
        print(f"[INFO] Inserting overall alert row:\n  {alert_row}")
        append_row_to_table("Alerts", alert_row)
    
    insert_sqlite_db(productionId,chassisNo1,cam1_status,cam2_status,timestamp)

    print(cam1_row)
    print(cam2_row)
    cam1_violation_id = append_row_to_table("Violations", cam1_row)
    cam2_violation_id = append_row_to_table("Violations", cam2_row)

    if cam1_violation_id is None or cam2_violation_id is None:
        print(
            f"[ERROR] Violations insert failed | cam1_ok={cam1_violation_id is not None} | "
            f"cam2_ok={cam2_violation_id is not None}"
        )
        return False

    print("Insertions complete.")
    return True


# === Sample inserts for test ===
# from datetime import datetime

# insert_db(
#     "cam1__yca__CH001__20250722_053937_000000.jpg",
#     "cam1__yca__CH001__20250722_054152_000000.jpg",
#     0,
#     0,
#     datetime.now().strftime("%Y-%m-%d %H:%M:%S")
# )

# insert_db("cam1_20250722_053937.jpg", "cam1_20250722_054152.jpg", 1, 0)
# insert_db("cam1_20250722_053937.jpg", "cam1_20250722_054152.jpg", 0, 1)
#insert_db("cam1_20250722_053937.jpg", "cam1_20250722_054152.jpg", 1, 1)
