# Frame Grab Filtered Pipeline

## Overview

This repository contains an end-to-end Python-based camera capture, PLC-controlled image saving, and database logging pipeline.

The system uses two cameras:
- `cam1` — GigE camera
- `cam2` — USB camera

Primary goals:
- capture images from both cameras
- save images during PLC-enabled production windows
- select final images when the conveyor stops
- insert production metadata into MySQL and local SQLite for diagnostics
- provide fallback behavior when one camera image is missing

## Repository Layout

- `combined3.py` — main pipeline entrypoint
- `plc_process.py` — PLC/TCP state management and save-window logic
- `Utils/push_to_db.py` — unified DB insertion helper
- `logger_sqlite.py` — local SQLite logging and failure tracking
- `data_base/database.py` — MySQL database insert helper
- `project_paths.py` — path helpers for config and logs
- `config/config.ini` — runtime configuration for database and sockets
- `MvCameraControl_class.py` — MVS SDK Python wrapper used by camera code
- `logs/` — application log output
- `image_data/`, `total_images/` — image save directories created at runtime

Additional helper files in the repository may support alternate capture modes or tests.

## Architecture

### 1. Capture and Pipeline

`combined3.py` is the main coordinator.
- Imports camera control from `MvCameraControl_class.py`
- Uses the MVS SDK Python path found in `combined3.py`
- Starts camera threads for `cam1` and `cam2`
- Stores per-camera frames in `latest_frames`
- Runs a processing loop that pairs frames by timestamp
- When both cameras are available, it attempts a joint database insert
- If one camera is missing, it can perform fallback logic and still log the available image

### 2. PLC and Save Window

`plc_process.py` is responsible for PLC-driven image saving.
- Starts a TCP listener on configured `tcp.ip` / `tcp.port`
- Receives JSON messages containing:
  - `conveyorBit`
  - `chassisNo`
  - `ModelA`
- When `conveyorBit == 1`, saving is enabled for a fixed time window
- During the window, the module saves frames from each camera at a fixed rate
- When `conveyorBit == 0`, the conveyor is stopped and the module selects the middle frame from captured images
- Final selected frames are copied into `image_data/cam1` and `image_data/cam2`

### 3. Database Logging

`Utils/push_to_db.py` contains the business insert logic.
- Uses `append_row_to_table()` from `data_base/database.py` to insert into MySQL
- Uses `insert_sqlite_db()` from `logger_sqlite.py` for local SQLite metadata
- Builds a production record from:
  - image paths
  - camera condition status codes
  - timestamp
  - shift and area metadata
- Writes separate rows for:
  - `Productions`
  - `Violations` for each camera
  - `Alerts` when overall status is not okay

### 4. Fallback & Duplicate Protection

`combined3.py` includes fallback handling:
- If joint DB insert fails because one camera image is unavailable, it duplicates the available image for the missing camera
- Fallback duplicates are stored in `fallback_images/`
- Duplicate inserts are prevented by tracking `(chassis, timestamp)` pairs in memory

## Data Flow

1. PLC data arrives via TCP in `plc_process.py`
2. `combined3.py` captures frames from both cameras
3. Each frame is passed to `process_frame(cam, frame, folder)` in `plc_process.py`
4. If PLC saving is active, frames are stored to `total_images/cam1` or `total_images/cam2`
5. When the conveyor stops, `plc_process.py` selects the middle saved image and copies it to final folders
6. `combined3.py` reads the final candidate images and attempts DB insertion through `Utils/push_to_db.insert_db`
7. Database logs are recorded into MySQL and SQLite

## Configuration

The main configuration file is `config/config.ini`.

Required sections:

```ini
[database]
DB_NAME=maruti_ta
DB_HOST=localhost
DB_USER=root
DB_PASSWORD=Dev112233

[zmq]
ip = 127.0.0.1
port = 5555

[tcp]
ip = 127.0.0.1
port = 4555
```

Notes:
- `database` config is used by `data_base/database.py`
- `tcp` config is used by `plc_process.py`
- `zmq` config may be referenced by other pipeline components or future extensions

## Dependencies

Install dependencies from `requirements.txt`:

```powershell
python -m pip install -r requirements.txt
```

Core Python packages:
- `opencv-python`
- `numpy`
- `pyzmq`
- `Flask`
- `pandas`
- `mysql-connector-python`

In addition, the MVS camera SDK must be installed and accessible on the system.

## Setup Steps

1. Clone or open the repository root.
2. Create a Python environment and install requirements.
3. Ensure `config/config.ini` contains valid MySQL and TCP settings.
4. Install/configure the MVS SDK so `MvCameraControl_class.py` imports successfully.
5. Confirm the `logs/` directory exists (it is created automatically by `project_paths.py`).
6. Confirm that camera serial numbers in `combined3.py` match the expected devices:
   - `DA5843327` => cam1
   - `DA5606439` => cam2

## Running the Pipeline

Run the main application with:

```powershell
python combined3.py
```

Expected behavior:
- two camera streams start
- TCP listener begins receiving PLC messages
- frames are saved only while the PLC save window is active
- final images are selected after conveyor stop
- database insertions are attempted for each pair

## Logging

Important log files:
- `logs/combined3.log` — main pipeline and DB insertion logs
- `logs/plc.log` — PLC state, TCP messages, save-window decisions
- `logs/logger_sqlite.log` — local SQLite operations and failures
- `logs/database.log` — MySQL connection and insert operations

The pipeline also writes failure records to:
- `logs/sqlite_failed_transactions.jsonl`
- `logs/mysql_failed_transactions.jsonl`

## Troubleshooting

### Camera 1 works in live view but not in saved images

Possible reasons:
- PLC flag `conveyorBit` not active during cam1 frame capture
- `plc_process.py` save-window timing excludes the frame
- capture rate or timing differs between `cam1` and `cam2`
- fallback logic may duplicate the available camera image if the partner camera is missing

### Database inserts are skipped

Check:
- `combined3.py` joint insert logic
- `Utils/push_to_db.insert_db()` return value
- `logs/combined3.log` for fallback insert attempts
- `logs/database.log` for MySQL insert failures
- `logs/logger_sqlite.log` for SQLite failures

### PLC data not received

Verify:
- TCP `ip` and `port` in `config/config.ini`
- PLC sender is sending valid JSON objects
- `conveyorBit`, `chassisNo`, and `ModelA` are present

## Key File Responsibilities

- `combined3.py`
  - main pipeline, camera capture, frame pairing, joint insertion logic
- `plc_process.py`
  - PLC/TCP listener, save-window timing, image selection, final image staging
- `Utils/push_to_db.py`
  - build and execute database inserts, overall status mapping
- `data_base/database.py`
  - MySQL connection and insert abstraction
- `logger_sqlite.py`
  - local SQLite schema and failure logging
- `project_paths.py`
  - centralized path management for logs and config
- `MvCameraControl_class.py`
  - low-level camera SDK wrapper for MVS cameras

## Notes

- This repository is designed for assembly-line image capture and logging.
- `cam1` and `cam2` are expected to be paired for each chassis cycle.

## Architecture Diagram

```
          +------------------+             +------------------+
          |   PLC / TCP      |             |    Cameras       |
          |   Input JSON     |             |                  |
          +--------+---------+             +--------+---------+
                   |                               |          
                   |                               |          
                   |          +--------------------v---------+
                   +--------> |    plc_process.py              |
                              |  - TCP listener                |
                              |  - conveyorBit state           |
                              |  - save window management      |
                              +--------+-----------------------+
                                       |        |              
                                       |        |              
                       +---------------+        +---------------+
                       |                                    |
           +-----------v-----------+            +-----------v-----------+
           | save frames to        |            | select final frame    |
           | total_images/cam1     |            | after conveyor stops  |
           | and total_images/cam2 |            |                      |
           +-----------+-----------+            +-----------+-----------+
                       |                                    |
                       |                                    |
                       v                                    v
          +------------------------------+      +---------------------------+
          | combined3.py                 |      | image_data/cam1 & cam2    |
          | - frame capture              |      +---------------------------+
          | - pipeline pairing           |                   ^
          | - DB insert coordination     |                   |
          +-----------+------------------+                   |
                      |                                      |
                      |                                      |
            +---------v---------+              +-------------+-------------+
            | Utils/push_to_db.py|<------------| fallback_images/ on      |
            | - MySQL insert     |              | missing camera failure   |
            | - SQLite insert    |              +--------------------------+
            +---------+---------+
                      |
         +------------+------------+
         |                         |
+--------v--------+      +---------v--------+
| data_base/      |      | logger_sqlite.py |
| database.py     |      | - local SQLite   |
| - MySQL helper  |      |   diagnostics    |
+-----------------+      +------------------+
```

- The pipeline uses production MySQL logging and local SQLite diagnostics.
- Fallback image duplication is used when one camera image is missing.

---

If you want, I can also add a visual architecture diagram or a short quickstart section for new operators.