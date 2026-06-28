#!/bin/bash
# =============================================================================
# Build the dvbs2_wf_TR PyInstaller binary from source
#
# Prerequisites:
#   - conda-packed engine at /usr/local/lib/.engine/ (Python 3.10)
#   - PyInstaller installed in the engine
#   - Helper scripts (*.py) present in ../src/
#
# Usage:
#   ./scripts/build_driver_binary.sh
#
# Output:
#   ./bin/dvbs2_wf_TR  (standalone single-file binary)
# =============================================================================
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src"
DIST_DIR="$REPO_DIR/bin"
ENGINE="/usr/local/lib/.engine"

cd "$REPO_DIR"

echo "=== PyInstaller build: dvbs2_wf_TR ==="
export PATH="$HOME/.local/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1

"$ENGINE/bin/python3" -m PyInstaller \
    --onefile \
    --name dvbs2_wf_TR \
    --add-data "$SRC_DIR/gr_fullduplex.py:." \
    --add-data "$SRC_DIR/ts_tee_stats.py:." \
    --add-data "$SRC_DIR/rawdata_gen.py:." \
    --add-data "$SRC_DIR/rawdata_rx.py:." \
    --clean \
    "$SRC_DIR/dvbs2_TR" 2>&1

mkdir -p "$DIST_DIR"
cp dist/dvbs2_wf_TR "$DIST_DIR/dvbs2_wf_TR"
echo "=== Built: $DIST_DIR/dvbs2_wf_TR ==="
ls -lh "$DIST_DIR/dvbs2_wf_TR"
