#!/bin/bash
# Wrapper for launchd to run ntok client-daemon hands-off on macOS

set -e

# Find project root relative to this script (macos/bin -> project root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Error: Could not find venv at $VENV_DIR" >&2
    exit 1
fi

source "$VENV_DIR/bin/activate"

# Consistent runtime dir for macOS
export XDG_RUNTIME_DIR="${TMPDIR%/}/ntok-runtime"
mkdir -p "$XDG_RUNTIME_DIR"

export PYTHONUNBUFFERED=1

# Exec the daemon - launchd will capture stdout/stderr
exec ntok client-daemon
