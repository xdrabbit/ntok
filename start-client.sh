#!/bin/bash
set -e

cd "$(dirname "$0")"

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
else
  echo "No .venv found, activate manually first"
  exit 1
fi

export XDG_RUNTIME_DIR=/tmp/ntok-runtime
mkdir -p "$XDG_RUNTIME_DIR"

echo "Killing old client-daemon if any..."
ps aux | grep -v grep | grep 'ntok client-daemon' | awk '{print $2}' | xargs kill 2>/dev/null || true
sleep 0.5
rm -f "$XDG_RUNTIME_DIR/ntok-client.sock" 2>/dev/null || true

echo "Starting ntok client-daemon detached (logs to /tmp/ntok-client.log)..."
nohup ntok client-daemon > /tmp/ntok-client.log 2>&1 &
DAEMON_PID=$!
disown $DAEMON_PID 2>/dev/null || true

echo "Started (PID $DAEMON_PID)"
echo "To see logs: tail -f /tmp/ntok-client.log"
sleep 2

echo "Current status:"
ntok client status
