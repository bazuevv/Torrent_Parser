#!/usr/bin/env python3
"""
CLI helper for transcode.sh — logs each transcode operation to DB.

Commands:
  queue  <filename> <src_size_mb> <src_height> <src_duration_sec> <action>   → inserts 'queued', prints id
  running <id>                                                                → marks running, sets started_at
  end    <id> <dest_size_mb>                                                 → marks done
  error  <id>                                                                → marks error
"""
import sys
from datetime import datetime
import db


def cmd_queue(filename: str, src_size_mb: int, src_height: int, src_duration_sec: int, action: str) -> None:
    conn = db.get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO transcode_log
               (queued_at, filename, action, src_height, src_duration_sec, src_size_mb, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'queued')""",
        (datetime.now(), filename, action, src_height or None, src_duration_sec or None, src_size_mb),
    )
    conn.commit()
    print(cursor.lastrowid)
    cursor.close()
    conn.close()


def cmd_running(record_id: int) -> None:
    conn = db.get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE transcode_log SET status = 'running', started_at = %s WHERE id = %s",
        (datetime.now(), record_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def cmd_end(record_id: int, dest_size_mb: int) -> None:
    conn = db.get_db_connection()
    cursor = conn.cursor()
    finished = datetime.now()
    cursor.execute(
        """UPDATE transcode_log
           SET dest_size_mb = %s,
               finished_at  = %s,
               duration_sec = TIMESTAMPDIFF(SECOND, started_at, %s),
               status       = 'done'
           WHERE id = %s""",
        (dest_size_mb, finished, finished, record_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def cmd_error(record_id: int) -> None:
    conn = db.get_db_connection()
    cursor = conn.cursor()
    finished = datetime.now()
    cursor.execute(
        """UPDATE transcode_log
           SET finished_at  = %s,
               duration_sec = TIMESTAMPDIFF(SECOND, started_at, %s),
               status       = 'error'
           WHERE id = %s""",
        (finished, finished, record_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit("Usage: transcode_db.py <queue|running|end|error> ...")

    cmd = args[0]
    try:
        if cmd == "queue" and len(args) == 6:
            cmd_queue(args[1], int(args[2]), int(args[3]), int(args[4]), args[5])
        elif cmd == "running" and len(args) == 2:
            cmd_running(int(args[1]))
        elif cmd == "end" and len(args) == 3:
            cmd_end(int(args[1]), int(args[2]))
        elif cmd == "error" and len(args) == 2:
            cmd_error(int(args[1]))
        else:
            sys.exit(f"Unknown command or wrong args: {args}")
    except Exception as exc:
        sys.stderr.write(f"transcode_db error: {exc}\n")
        sys.exit(1)
