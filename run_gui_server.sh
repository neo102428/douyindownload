#!/bin/zsh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LOG_FILE="$SCRIPT_DIR/gui_server.log"
PID_FILE="$SCRIPT_DIR/gui_server.pid"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Server already running with PID $OLD_PID"
    exit 0
  fi
fi

nohup python3 "$SCRIPT_DIR/gui.py" --no-browser >"$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" >"$PID_FILE"
echo "Started server PID $NEW_PID"
