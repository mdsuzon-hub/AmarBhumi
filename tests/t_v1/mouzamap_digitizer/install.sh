#!/usr/bin/env bash
# MouzaMap Digitizer - installer for Debian (12 "bookworm", 13 "trixie") and derivatives (Ubuntu, Mint ...).
#
#   ./install.sh               install system libraries (needs sudo), create .venv, add menu launcher
#   ./install.sh --no-apt      skip the apt step (libraries already present)
#   ./install.sh --no-desktop  do not create the desktop launcher
#   ./install.sh --dev         also install pytest for running the test-suite
#   ./install.sh --uninstall   remove the launcher and .venv
#
# Normal operation is fully offline; internet is only needed during installation.
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APT=1; DESKTOP=1; DEV=0; UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --no-apt) APT=0 ;;
        --no-desktop) DESKTOP=0 ;;
        --dev) DEV=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$APP_DIR/mouzamap-digitizer.desktop"
BIN_LINK="$HOME/.local/bin/mouzamap-digitizer"

if [ "$UNINSTALL" = 1 ]; then
    rm -f "$DESKTOP_FILE" "$BIN_LINK"
    rm -rf "$DIR/.venv"
    echo "Removed launcher and virtual environment. (Your projects and exports are untouched.)"
    exit 0
fi

echo "==> Checking Python"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "python3 not found. Install it with: sudo apt install python3" >&2; exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11+ is required (found $("$PY" -V 2>&1)). On Debian 12+ the default python3 is new enough." >&2
    exit 1
fi

if [ "$APT" = 1 ]; then
    echo "==> Installing system libraries (Qt/XCB runtime, venv, fonts)"
    SUDO=""
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else
            echo "Not root and sudo is unavailable. Re-run as root or use --no-apt after installing the packages listed in this script." >&2
            exit 1
        fi
    fi
    $SUDO apt-get update
    # libxcb-cursor0 is required by Qt >= 6.5 on X11; the other libxcb-* / libxkbcommon-x11 / libegl1 / libgl1
    # are the standard Qt xcb platform-plugin dependencies. fonts-noto-core provides Bengali glyphs for dag numbers.
    $SUDO apt-get install -y python3-venv python3-pip \
        libegl1 libgl1 libglib2.0-0 libdbus-1-3 libfontconfig1 libxkbcommon0 libxkbcommon-x11-0 \
        libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
        libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 fonts-noto-core
fi

echo "==> Creating virtual environment in $DIR/.venv"
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
if [ "$DEV" = 1 ]; then REQ="$DIR/requirements-dev.txt"; else REQ="$DIR/requirements.txt"; fi
echo "==> Installing Python packages from $(basename "$REQ") (this downloads ~300 MB once)"
"$DIR/.venv/bin/python" -m pip install -r "$REQ"

echo "==> Verifying the installation"
QT_QPA_PLATFORM=offscreen "$DIR/.venv/bin/python" - <<'PYEOF'
import cv2, numpy, shapely, geopandas, rasterio, pyogrio, PySide6
try:
    import pymupdf
except ImportError:
    import fitz as pymupdf
print("  OpenCV", cv2.__version__, "| NumPy", numpy.__version__, "| Shapely", shapely.__version__,
      "| GeoPandas", geopandas.__version__, "| Rasterio", rasterio.__version__, "| PySide6", PySide6.__version__)
PYEOF

chmod +x "$DIR/run.sh" "$DIR/main.py" "$DIR"/scripts/*.py

if [ "$DESKTOP" = 1 ]; then
    echo "==> Creating desktop launcher"
    mkdir -p "$APP_DIR" "$HOME/.local/bin"
    sed "s|@INSTALL_DIR@|$DIR|g" "$DIR/mouzamap-digitizer.desktop.in" > "$DESKTOP_FILE"
    chmod +x "$DESKTOP_FILE"
    ln -sf "$DIR/run.sh" "$BIN_LINK"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
    echo "    Menu entry: $DESKTOP_FILE"
    echo "    Command:    mouzamap-digitizer   (make sure ~/.local/bin is on your PATH)"
fi

echo
echo "Done. Start it from the application menu, with ./run.sh, or: ./run.sh samples/sample_mouza_map.pdf"
