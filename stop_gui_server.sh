#!/bin/zsh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PID_FILE="$SCRIPT_DIR/gui_server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file found"
  exit 0
fi

PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped server PID $PID"
else
  echo "Server not running"
fi

rm -f "$PID_FILE"
