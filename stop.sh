#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SCRIPT_DIR/capsuleerai.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "No PID file found — is CapsuleerAI running?"
    exit 1
fi

pid=$(cat "$PIDFILE")
if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    rm -f "$PIDFILE"
    echo "Stopped CapsuleerAI (PID $pid)."
else
    echo "Process $pid not found — already stopped."
    rm -f "$PIDFILE"
fi
