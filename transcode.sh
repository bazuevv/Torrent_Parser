#!/usr/bin/env bash
# Called by qBittorrent on torrent completion.
# Usage: transcode.sh <file_or_dir>   (%F from qBittorrent)
#
# Only one ffmpeg runs at a time — others wait via flock on LOCK_FILE.
# DB statuses: queued → running → done / error

set -euo pipefail

INPUT="${1:-}"
PROJECT_DIR="/home/ubuntu/Dedicated_Server/Projects/Torrent_Parser"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python3"
TRANSCODE_DB="$PROJECT_DIR/transcode_db.py"

# ── defaults (overridden by DB settings below) ────────────────────────────────
DEST_DIR="/home/ubuntu/Videos"
LOG_FILE="/var/log/torrent_parser/transcode.log"
LOCK_FILE="/var/lock/transcode.lock"
MAX_HEIGHT=720
FFMPEG_CRF=23
FFMPEG_PRESET=medium
FFMPEG_THREADS=6
FFMPEG_CPU_CORES=0-5

# ── load settings from DB ─────────────────────────────────────────────────────
_db_settings=$("$VENV_PYTHON" "$PROJECT_DIR/get_settings.py" transcoder 2>/dev/null || true)
if [[ -n "$_db_settings" ]]; then
    _v=$(echo "$_db_settings" | grep '^transcode_max_height=' | cut -d= -f2-)
    [[ -n "$_v" ]] && MAX_HEIGHT="$_v"
    _v=$(echo "$_db_settings" | grep '^ffmpeg_crf=' | cut -d= -f2-)
    [[ -n "$_v" ]] && FFMPEG_CRF="$_v"
    _v=$(echo "$_db_settings" | grep '^ffmpeg_preset=' | cut -d= -f2-)
    [[ -n "$_v" ]] && FFMPEG_PRESET="$_v"
    _v=$(echo "$_db_settings" | grep '^ffmpeg_threads=' | cut -d= -f2-)
    [[ -n "$_v" ]] && FFMPEG_THREADS="$_v"
    _v=$(echo "$_db_settings" | grep '^ffmpeg_cpu_cores=' | cut -d= -f2-)
    [[ -n "$_v" ]] && FFMPEG_CPU_CORES="$_v"
    _v=$(echo "$_db_settings" | grep '^dest_dir=' | cut -d= -f2-)
    [[ -n "$_v" ]] && DEST_DIR="$_v"
fi

# Open lock file on fd 9 — used by flock inside process_file
exec 9>>"$LOCK_FILE"

# ── helpers ───────────────────────────────────────────────────────────────────

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

video_height() {
    ffprobe -v error \
        -select_streams v:0 \
        -show_entries stream=height \
        -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null || echo "0"
}

video_duration() {
    local raw
    raw=$(ffprobe -v error \
        -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null || echo "0")
    printf "%.0f" "${raw:-0}"
}

process_file() {
    local src="$1"
    local name
    name=$(basename "$src")
    local dest="$DEST_DIR/$name"

    # Skip non-video by extension
    case "${name,,}" in
        *.mp4|*.mkv|*.avi|*.mov|*.wmv|*.flv|*.webm) ;;
        *)
            log "SKIP (not video): $name"
            return
            ;;
    esac

    local height
    height=$(video_height "$src")

    local src_duration
    src_duration=$(video_duration "$src")

    local src_mb
    src_mb=$(( $(stat -c%s "$src") / 1048576 ))

    # Determine action before queuing
    local action
    if [[ "$height" -le 0 ]]; then
        action="copy"
    elif [[ "$height" -gt "$MAX_HEIGHT" ]]; then
        action="transcode"
    else
        action="copy"
    fi

    # ── 1. Register as QUEUED (immediately visible in UI) ──────────────────
    local record_id
    record_id=$("$VENV_PYTHON" "$TRANSCODE_DB" queue "$name" "$src_mb" "$height" "$src_duration" "$action" 2>/dev/null || echo "0")
    log "QUEUED: $name (${height}p, ${src_mb} MB, action=$action)"

    # ── 2. Wait for exclusive lock (serialises all transcode operations) ───
    flock -x 9

    # ── 3. Mark as RUNNING ─────────────────────────────────────────────────
    [[ "$record_id" != "0" ]] && "$VENV_PYTHON" "$TRANSCODE_DB" running "$record_id" 2>/dev/null || true
    log "START: $name"

    # ── 4. Do the work ─────────────────────────────────────────────────────
    if [[ "$height" -le 0 ]]; then
        log "  WARN: cannot read height — copying as-is"
        cp "$src" "$dest"
        local dest_mb
        dest_mb=$(( $(stat -c%s "$dest") / 1048576 ))
        log "  DONE: ${src_mb} MB → ${dest_mb} MB"
        [[ "$record_id" != "0" ]] && "$VENV_PYTHON" "$TRANSCODE_DB" end "$record_id" "$dest_mb" 2>/dev/null || true

    elif [[ "$height" -gt "$MAX_HEIGHT" ]]; then
        log "  → transcoding ${height}p → ${MAX_HEIGHT}p"
        if taskset -c "$FFMPEG_CPU_CORES" ffmpeg -i "$src" \
            -vf "scale=-2:${MAX_HEIGHT}" \
            -c:v libx264 -crf "$FFMPEG_CRF" -preset "$FFMPEG_PRESET" \
            -threads "$FFMPEG_THREADS" \
            -c:a copy \
            -movflags +faststart \
            -y "$dest" \
            >> "$LOG_FILE" 2>&1
        then
            local dest_mb
            dest_mb=$(( $(stat -c%s "$dest") / 1048576 ))
            log "  DONE: ${src_mb} MB → ${dest_mb} MB"
            [[ "$record_id" != "0" ]] && "$VENV_PYTHON" "$TRANSCODE_DB" end "$record_id" "$dest_mb" 2>/dev/null || true
        else
            log "  ERROR: ffmpeg failed for $name"
            [[ "$record_id" != "0" ]] && "$VENV_PYTHON" "$TRANSCODE_DB" error "$record_id" 2>/dev/null || true
        fi

    else
        log "  → copying (${height}p ≤ ${MAX_HEIGHT}p)"
        cp "$src" "$dest"
        local dest_mb
        dest_mb=$(( $(stat -c%s "$dest") / 1048576 ))
        log "  DONE: ${src_mb} MB → ${dest_mb} MB"
        [[ "$record_id" != "0" ]] && "$VENV_PYTHON" "$TRANSCODE_DB" end "$record_id" "$dest_mb" 2>/dev/null || true
    fi

    # ── 5. Release lock — next queued file can proceed ─────────────────────
    flock -u 9
}

# ── entry point ───────────────────────────────────────────────────────────────

if [[ -z "$INPUT" ]]; then
    log "ERROR: no argument provided"
    exit 1
fi

log "=== torrent completed: $INPUT ==="

if [[ -f "$INPUT" ]]; then
    process_file "$INPUT"
elif [[ -d "$INPUT" ]]; then
    found=0
    while IFS= read -r -d '' f; do
        process_file "$f"
        ((found++)) || true
    done < <(find "$INPUT" -type f \
        \( -iname "*.mp4" -o -iname "*.mkv" -o -iname "*.avi" \
           -o -iname "*.mov" -o -iname "*.wmv" -o -iname "*.flv" \
           -o -iname "*.webm" \) -print0)
    [[ "$found" -eq 0 ]] && log "WARN: no video files found in $INPUT"
else
    log "ERROR: '$INPUT' is neither a file nor a directory"
    exit 1
fi

log "=== done: $INPUT ==="
