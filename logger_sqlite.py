import json
import logging
import sqlite3
from datetime import datetime
from logging.handlers import RotatingFileHandler

from project_paths import LOG_DIR, log_path, project_path

# DB path (creates in project directory)
DB_NAME = project_path("productions.db")
FAILED_TXN_LOG = log_path("sqlite_failed_transactions.jsonl")
PRODUCTIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS productions (
        productionID TEXT,
        chassisNo TEXT,
        cam1_status INTEGER,
        cam2_status INTEGER,
        timestamp DATETIME
    )
"""
FAILED_TRANSACTIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS failed_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        operation TEXT NOT NULL,
        query_text TEXT NOT NULL,
        params_json TEXT,
        error_text TEXT NOT NULL,
        logged_at DATETIME NOT NULL
    )
"""

logger = logging.getLogger("logger_sqlite")
logger.setLevel(logging.INFO)
logger.propagate = False
if logger.handlers:
    logger.handlers.clear()

file_handler = RotatingFileHandler(
    log_path("logger_sqlite.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
logger.addHandler(file_handler)


def _write_failed_transaction_file(record):
    with open(FAILED_TXN_LOG, "a", encoding="utf-8") as failure_file:
        failure_file.write(json.dumps(record, default=str) + "\n")


def _ensure_schema(conn):
    conn.execute(PRODUCTIONS_SCHEMA)
    conn.execute(FAILED_TRANSACTIONS_SCHEMA)


def log_failed_transaction(source, operation, query, params=(), error=""):
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "operation": operation,
        "database": DB_NAME,
        "query": " ".join(query.split()),
        "params": list(params),
        "error": str(error),
    }
    _write_failed_transaction_file(record)

    insert_failure_query = """
        INSERT INTO failed_transactions
        (source, operation, query_text, params_json, error_text, logged_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    insert_failure_params = (
        source,
        operation,
        record["query"],
        json.dumps(record["params"], default=str),
        record["error"],
        record["timestamp"],
    )

    try:
        with sqlite3.connect(DB_NAME) as conn:
            _ensure_schema(conn)
            conn.execute(insert_failure_query, insert_failure_params)
        logger.info(
            "[SQLite] Failed transaction logged | source=%s | operation=%s",
            source,
            operation,
        )
        return True
    except sqlite3.Error:
        logger.exception(
            "[SQLite] Failed to persist failure record | source=%s | operation=%s",
            source,
            operation,
        )
        return False


def create_db():
    try:
        with sqlite3.connect(DB_NAME) as conn:
            _ensure_schema(conn)
        logger.info("[SQLite] Schema ensured at %s", DB_NAME)
        return True
    except sqlite3.Error as exc:
        logger.exception("[SQLite] Failed ensuring schema")
        log_failed_transaction("sqlite", "create_db", PRODUCTIONS_SCHEMA, (), exc)
        return False


def insert_sqlite_db(productionID, chassisNo, cam1_status, cam2_status, timestamp):
    insert_query = """
        INSERT INTO productions (productionID, chassisNo, cam1_status, cam2_status, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """
    params = (productionID, chassisNo, cam1_status, cam2_status, timestamp)

    if not create_db():
        return False

    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute(insert_query, params)
        logger.info(
            "[SQLite] Inserted production record | productionID=%s | chassisNo=%s",
            productionID,
            chassisNo,
        )
        return True
    except sqlite3.Error as exc:
        logger.exception(
            "[SQLite] Insert failed | productionID=%s | chassisNo=%s | query=%s | params=%s",
            productionID,
            chassisNo,
            " ".join(insert_query.split()),
            params,
        )
        log_failed_transaction("sqlite", "insert_sqlite_db", insert_query, params, exc)
        return False


# from datetime import datetime

# ts = datetime.now()  # no formatting needed

# insert_sqlite_db("P001", "CH12345", 1, 0, ts)



