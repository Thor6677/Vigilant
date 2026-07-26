#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SCRIPT_DIR/vigilant.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "Already stopped — no PID file found."
    exit 0
fi

pid=$(cat "$PIDFILE")
if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    rm -f "$PIDFILE"
    echo "Stopped Vigilant (PID $pid)."
else
    echo "Process $pid not found — already stopped."
    rm -f "$PIDFILE"
fi
