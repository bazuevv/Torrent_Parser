#!/usr/bin/env python3
"""Outputs DB settings as KEY=VALUE lines for bash eval.
Usage: get_settings.py [group]
"""
import sys
import db

group = sys.argv[1] if len(sys.argv) > 1 else None
try:
    conn = db.get_db_connection()
    cursor = conn.cursor()
    if group:
        cursor.execute("SELECT key_name, value FROM settings WHERE group_name = %s", (group,))
    else:
        cursor.execute("SELECT key_name, value FROM settings")
    for key, value in cursor.fetchall():
        print(f"{key}={value}")
    cursor.close()
    conn.close()
except Exception as e:
    sys.stderr.write(f"get_settings error: {e}\n")
    sys.exit(1)
