import configparser
import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

import mysql.connector
import pandas as pd

from logger_sqlite import log_failed_transaction
from project_paths import CONFIG_DIR, log_path

logger = logging.getLogger("mysql_database")
logger.setLevel(logging.INFO)
logger.propagate = False
if logger.handlers:
    logger.handlers.clear()

file_handler = RotatingFileHandler(
    log_path("database.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
logger.addHandler(file_handler)

FAILED_TXN_LOG = log_path("mysql_failed_transactions.jsonl")


def _record_failed_transaction(table_name, query, row_data, error):
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "table": table_name,
        "query": " ".join(query.split()),
        "row_data": list(row_data),
        "error": str(error),
    }
    with open(FAILED_TXN_LOG, "a", encoding="utf-8") as failure_file:
        failure_file.write(json.dumps(record, default=str) + "\n")
    log_failed_transaction("mysql", f"{table_name}_insert_failed", query, row_data, error)

def append_row_to_table(table_name, row_data, exclude_auto_increment=True):
    config = configparser.ConfigParser()
    connection = None
    cursor = None
    #config_path = os.path.join("db_credentials.ini")  # Universal path
    config_path = str(CONFIG_DIR / "config.ini")
    config.read(config_path)

    # Get database credentials
    db_name = config.get('database', 'DB_NAME')
    db_host = config.get('database', 'DB_HOST')
    db_user = config.get('database', 'DB_USER')
    db_password = config.get('database', 'DB_PASSWORD')


    try:
        logger.info("[MySQL] Connecting to %s for table=%s", db_name, table_name)
        # Establish connection
        connection = mysql.connector.connect(
            host=db_host,
            user=db_user,
            password=db_password,
            database=db_name
        )
        cursor = connection.cursor()

        # Fetch column names
        describe_query = f"DESCRIBE {table_name}"
        logger.info("[MySQL] Executing schema query | table=%s | query=%s", table_name, describe_query)
        cursor.execute(describe_query)
        columns = [column[0] for column in cursor.fetchall()]
        
        # Exclude auto-increment primary key
        primary_key = columns[0]  # Assuming the first column is PK and auto-increment
        columns = columns[1:]
        
        # Exclude 'createdAt' and 'modifiedAt' as they should be auto-filled
        if 'createdAt' in columns and 'updatedAt' in columns:
            columns.remove('createdAt')
            columns.remove('updatedAt')
        
        #if len(columns) != len(row_data):
            #raise ValueError("Mismatch between table columns and provided row data.")
        
        # Prepare INSERT query
        placeholders = ', '.join(['%s'] * len(row_data))
        insert_query = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"
        logger.info(
            "[MySQL] Executing insert | table=%s | query=%s | row_data=%s",
            table_name,
            insert_query,
            row_data,
        )
        
        # Execute query
        cursor.execute(insert_query, row_data)
        connection.commit()

        # Retrieve the last inserted primary key
        inserted_pk = cursor.lastrowid
        logger.info("[MySQL] Insert succeeded | table=%s | pk=%s", table_name, inserted_pk)
        print(f"Row inserted successfully with PK: {inserted_pk}")
        
        return inserted_pk

    except mysql.connector.Error as err:
        logger.exception("[MySQL] Insert failed | table=%s", table_name)
        failed_query = locals().get("insert_query") or locals().get("describe_query") or f"TABLE {table_name}"
        _record_failed_transaction(table_name, failed_query, row_data, err)
        print(f"Error: {err}")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None and connection.is_connected():
            connection.close()
            logger.info("[MySQL] Connection closed | table=%s", table_name)
            print("Connection closed.")

# # File: data_base/database.py
# import mysql.connector
# import configparser
# import os
# from datetime import datetime
# from typing import Any, Tuple, Optional

# def load_db_config(config_file: str = "pipeline_image/config/config.ini") -> dict:
#     """Load database configuration from an .ini file."""
#     config = configparser.ConfigParser()
#     config.read(config_file)

#     return {
#         "host": config.get('database', 'DB_HOST'),
#         "user": config.get('database', 'DB_USER'),
#         "password": config.get('database', 'DB_PASSWORD'),
#         "database": config.get('database', 'DB_NAME')
#     }

# def append_row_to_table(table_name: str, row_data: Tuple[Any, ...], exclude_auto_increment: bool = True) -> Optional[int]:
#     """
#     Appends a row to a MySQL table, automatically handling column selection and validation.

#     Args:
#         table_name (str): Name of the table to insert data into.
#         row_data (tuple): Tuple containing values to insert.
#         exclude_auto_increment (bool): If True, excludes auto-increment and timestamp columns.

#     Returns:
#         Optional[int]: The last inserted row's primary key, or None on error.
#     """
#     db_config = load_db_config()
#     connection = None

#     try:
#         connection = mysql.connector.connect(**db_config)
#         cursor = connection.cursor()

#         # Get column names
#         cursor.execute(f"DESCRIBE {table_name}")
#         all_columns = [column[0] for column in cursor.fetchall()]

#         # Remove auto-increment ID and timestamp columns
#         columns = all_columns
#         if exclude_auto_increment:
#             columns = all_columns[1:]  # Skip auto-increment PK
#         columns = [col for col in columns if col not in ('createdAt', 'updatedAt')]

#         # Check for mismatch
#         if len(columns) != len(row_data):
#             print(f"\n❌ [DEBUG] Column Mismatch in '{table_name}'")
#             print(f"Columns Expected ({len(columns)}): {columns}")
#             print(f"Values Provided ({len(row_data)}): {row_data}\n")
#             raise ValueError("Mismatch between table columns and provided row data.")

#         # Prepare and execute insert
#         placeholders = ', '.join(['%s'] * len(row_data))
#         col_names = ', '.join(columns)
#         insert_query = f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders})"

#         print(f"[DEBUG] Executing SQL:\n{insert_query}")
#         print(f"[DEBUG] With Values:\n{row_data}\n")

#         cursor.execute(insert_query, row_data)
#         connection.commit()

#         inserted_pk = cursor.lastrowid
#         print(f"✅ Row inserted successfully into {table_name}, PK: {inserted_pk}")
#         return inserted_pk

#     except mysql.connector.Error as err:
#         print(f"❌ MySQL Error: {err}")
#         return None

#     except ValueError as val_err:
#         print(f"❌ Validation Error: {val_err}")
#         return None

#     finally:
#         if connection and connection.is_connected():
#             cursor.close()
#             connection.close()
#             print("🔌 MySQL connection closed.\n")
