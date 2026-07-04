#!/usr/bin/env python3
"""Sequential queue processor for transcode_log entries with status='queued'.

Control files (created/removed by this process and web.py):
  /tmp/transcode_queue.pid   — PID of this process (written on start)
  /tmp/transcode_ffmpeg.pid  — PID of current ffmpeg (written while encoding)
  /tmp/transcode_paused      — presence pauses processing between files

Signals:
  SIGTERM → graceful stop: reset current 'running' entry → 'queued', cleanup.
"""
import glob
import json
import logging
import os
import signal
import subprocess
import sys
import time

import db

BT_DIR = "/home/ubuntu/Videos/Bittorrent"
DEST_DIR = "/home/ubuntu/Videos"
PID_FILE = "/tmp/transcode_queue.pid"
FFMPEG_PID_FILE = "/tmp/transcode_ffmpeg.pid"
PAUSE_FLAG = "/tmp/transcode_paused"
PROGRESS_FILE = "/tmp/ffmpeg_progress.txt"
CURRENT_FILE = "/tmp/transcode_current.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/torrent_parser/transcode.log", mode="a"),
    ],
)
log = logging.getLogger(__name__)

_current_rid: int | None = None
_current_dest: str | None = None
_stop_requested = False


def _sigterm_handler(signum, frame):
    global _stop_requested
    _stop_requested = True
    log.info("SIGTERM received — stopping after current operation")


signal.signal(signal.SIGTERM, _sigterm_handler)


def _write_current(rid, row, action, dest_dir):
    data = {
        "id": rid,
        "filename": row["filename"],
        "action": action,
        "src_size_mb": row.get("src_size_mb"),
        "src_height": row.get("src_height"),
        "src_duration_sec": row.get("src_duration_sec"),
        "dest_dir": dest_dir,
        "started_at": time.time(),
    }
    with open(CURRENT_FILE, "w") as f:
        json.dump(data, f)


def _clear_current():
    for p in (CURRENT_FILE, PROGRESS_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _cleanup():
    for f in (PID_FILE, FFMPEG_PID_FILE):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    _clear_current()
    if _current_rid is not None:
        try:
            _set_status(_current_rid, "queued")
            log.info("Reset entry %d → queued", _current_rid)
        except Exception:
            pass
    if _current_dest and os.path.isfile(_current_dest):
        try:
            os.remove(_current_dest)
            log.info("Removed partial file: %s", _current_dest)
        except Exception:
            pass


def find_source(filename: str) -> str | None:
    direct = os.path.join(BT_DIR, filename)
    if os.path.isfile(direct):
        return direct
    matches = glob.glob(os.path.join(BT_DIR, "**", glob.escape(filename)), recursive=True)
    if matches:
        return matches[0]
    return None


def load_settings() -> dict:
    s = {}
    try:
        for r in db.get_all_settings():
            s[r["key_name"]] = r["value"]
    except Exception:
        pass
    return s


def _wait_while_paused():
    while os.path.exists(PAUSE_FLAG) and not _stop_requested:
        time.sleep(1)


def main():
    global _current_rid, _current_dest

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        _run()
    finally:
        _cleanup()


def _run():
    global _current_rid, _current_dest

    settings = load_settings()
    max_height = int(settings.get("transcode_max_height", "720"))
    crf = settings.get("ffmpeg_crf", "23")
    preset = settings.get("ffmpeg_preset", "medium")
    threads = settings.get("ffmpeg_threads", "6")
    cpu_cores = settings.get("ffmpeg_cpu_cores", "0-5")
    dest_dir = settings.get("dest_dir", DEST_DIR)

    log.info("Settings: max_height=%d crf=%s preset=%s threads=%s cores=%s dest=%s",
             max_height, crf, preset, threads, cpu_cores, dest_dir)

    conn = db.get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, filename, action, src_height, src_size_mb, src_duration_sec FROM transcode_log "
        "WHERE status = 'queued' ORDER BY id"
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    log.info("Found %d queued entries", len(rows))

    for row in rows:
        if _stop_requested:
            log.info("Stop requested — exiting loop")
            break

        _wait_while_paused()
        if _stop_requested:
            break

        rid = row["id"]
        filename = row["filename"]
        action = row["action"]
        height = row["src_height"] or 0

        src = find_source(filename)
        if not src:
            log.warning("[%d] source not found: %s", rid, filename)
            _set_status(rid, "error")
            continue

        dest = os.path.join(dest_dir, filename)

        if os.path.isfile(dest):
            log.info("[%d] already exists in dest, marking done: %s", rid, filename)
            dest_mb = os.path.getsize(dest) // 1048576
            _set_done(rid, dest_mb)
            continue

        log.info("[%d] START %s: %s", rid, action.upper(), filename[:80])
        _current_rid = rid
        _current_dest = dest
        _set_running(rid)
        _write_current(rid, row, action, dest_dir)
        t0 = time.time()

        if action == "copy" or height <= max_height:
            try:
                os.makedirs(dest_dir, exist_ok=True)
                proc = subprocess.Popen(["cp", "--", src, dest])
                _write_ffmpeg_pid(proc.pid)
                proc.wait()
                _remove_ffmpeg_pid()
                if proc.returncode == 0:
                    dest_mb = os.path.getsize(dest) // 1048576
                    elapsed = int(time.time() - t0)
                    log.info("[%d] DONE copy in %ds  %dMB → %dMB", rid, elapsed, row["src_size_mb"], dest_mb)
                    _set_done(rid, dest_mb)
                else:
                    raise subprocess.CalledProcessError(proc.returncode, "cp")
            except Exception as e:
                log.error("[%d] copy ERROR: %s", rid, e)
                _set_status(rid, "error")
        else:
            cmd = [
                "taskset", "-c", cpu_cores,
                "ffmpeg", "-i", src,
                "-vf", f"scale=-2:{max_height}",
                "-c:v", "libx264", "-crf", crf, "-preset", preset,
                "-threads", threads,
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-progress", PROGRESS_FILE,
                "-y", dest,
            ]
            try:
                os.makedirs(dest_dir, exist_ok=True)
                with open("/var/log/torrent_parser/transcode.log", "a") as logf:
                    proc = subprocess.Popen(cmd, stdout=logf, stderr=logf)
                _write_ffmpeg_pid(proc.pid)
                proc.wait()
                _remove_ffmpeg_pid()
                if proc.returncode == 0:
                    dest_mb = os.path.getsize(dest) // 1048576
                    elapsed = int(time.time() - t0)
                    log.info("[%d] DONE transcode in %ds  %dMB → %dMB", rid, elapsed, row["src_size_mb"], dest_mb)
                    _set_done(rid, dest_mb)
                else:
                    raise subprocess.CalledProcessError(proc.returncode, "ffmpeg")
            except Exception as e:
                log.error("[%d] ffmpeg ERROR: %s", rid, e)
                if os.path.isfile(dest):
                    os.remove(dest)
                if not _stop_requested:
                    _set_status(rid, "error")

        _current_rid = None
        _current_dest = None
        _clear_current()

    log.info("Queue processor finished.")


def _write_ffmpeg_pid(pid: int):
    with open(FFMPEG_PID_FILE, "w") as f:
        f.write(str(pid))


def _remove_ffmpeg_pid():
    try:
        os.remove(FFMPEG_PID_FILE)
    except FileNotFoundError:
        pass


def _set_running(rid: int):
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE transcode_log SET status='running', started_at=NOW() WHERE id=%s", (rid,))
    conn.commit()
    c.close()
    conn.close()


def _set_done(rid: int, dest_mb: int):
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE transcode_log SET status='done', finished_at=NOW(), "
        "duration_sec=TIMESTAMPDIFF(SECOND, started_at, NOW()), dest_size_mb=%s "
        "WHERE id=%s",
        (dest_mb, rid),
    )
    conn.commit()
    c.close()
    conn.close()


def _set_status(rid: int, status: str):
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE transcode_log SET status=%s WHERE id=%s", (status, rid))
    conn.commit()
    c.close()
    conn.close()


if __name__ == "__main__":
    main()
