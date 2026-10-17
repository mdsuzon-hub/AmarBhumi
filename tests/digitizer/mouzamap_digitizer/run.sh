#!/usr/bin/env bash
# Start MouzaMap Digitizer using the virtual environment created by install.sh.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
if [ -x "$DIR/.venv/bin/python" ]; then
    exec "$DIR/.venv/bin/python" "$DIR/main.py" "$@"
fi
echo "No virtual environment found - run ./install.sh first." >&2
exit 1
