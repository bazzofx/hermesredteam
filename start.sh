#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Morpheus Mission Control — Start Launcher
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.server.pid"
LOG_FILE="$SCRIPT_DIR/server.log"

# ── Kill existing ────────────────────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[Morpheus] Stopping existing server (PID $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

# ── Verify dependencies ──────────────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/index.html" ]; then
    echo "[Morpheus] ERROR: index.html not found in $SCRIPT_DIR"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/server.py" ]; then
    echo "[Morpheus] ERROR: server.py not found in $SCRIPT_DIR"
    exit 1
fi

# ── Check port ───────────────────────────────────────────────────────────────
if ss -tlnp 2>/dev/null | grep -q ":51763 "; then
    echo "[Morpheus] WARNING: Port 51763 is already in use. Attempting to free it..."
    fuser -k 51763/tcp 2>/dev/null || true
    sleep 1
fi

# ── Launch ───────────────────────────────────────────────────────────────────
echo "[Morpheus] Starting Mission Control on http://127.0.0.1:51763 ..."
cd "$SCRIPT_DIR"
python3 server.py >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# ── Wait for readiness ───────────────────────────────────────────────────────
for i in $(seq 1 10); do
    if ss -tlnp 2>/dev/null | grep -q ":51763 .*pid=$SERVER_PID"; then
        break
    fi
    sleep 0.5
done

# ── Verify ───────────────────────────────────────────────────────────────────
sleep 0.5
if kill -0 "$SERVER_PID" 2>/dev/null; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:51763/ 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "[Morpheus] ✓ Dashboard live on http://127.0.0.1:51763"
        echo "[Morpheus] ✓ SSE stream  on http://127.0.0.1:51763/events"
        echo "[Morpheus] ✓ PID: $SERVER_PID  Log: $LOG_FILE"
        exit 0
    else
        echo "[Morpheus] ✗ Server started but returned HTTP $HTTP_CODE"
        exit 1
    fi
else
    echo "[Morpheus] ✗ Server process died. Check $LOG_FILE"
    cat "$LOG_FILE" 2>/dev/null || true
    exit 1
fi
