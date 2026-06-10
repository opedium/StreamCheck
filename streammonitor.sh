#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "venv/bin/activate" ]; then
    echo "ERROR: venv not found at $SCRIPT_DIR/venv/bin/activate" >&2
    exit 1
fi

source venv/bin/activate
cd StreamMonitor
exec python main.py --record-stats
