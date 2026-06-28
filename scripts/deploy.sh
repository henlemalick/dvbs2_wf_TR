#!/bin/bash
# =============================================================================
# deploy.sh - Deploy dvbs2_wf_TR to a target Jetson
#
# This script copies the entire dvbs2_wf_TR deployment folder to a remote
# Jetson and installs the engine (conda-packed GNU Radio) at the expected
# location. Run it from the repository root.
#
# Usage:
#   ./scripts/deploy.sh TARGET_IP [SSH_USER]
#
# Examples:
#   ./scripts/deploy.sh 192.168.0.41
#   ./scripts/deploy.sh 192.168.0.42 wftest
#
# What it does:
#   1. SCPs the bin/, config/, and media/ directories to the target
#   2. SCPs the wrapper script
#   3. Copies the engine (/usr/local/lib/.engine) to the target
#      (requires the engine to be present on the BUILD host, or use
#       --engine-path to point to a tarball or existing install)
#   4. Verifies the deployment
# =============================================================================
set -e

if [ $# -lt 1 ]; then
    echo "Usage: $0 TARGET_IP [SSH_USER]"
    echo "  TARGET_IP  IP address of the target Jetson"
    echo "  SSH_USER   SSH user (default: wftest)"
    exit 1
fi

TARGET="$1"
USER="${2:-wftest}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="/home/$USER/dvbs2_wf_TR"
ENGINE_SRC="/usr/local/lib/.engine"
ENGINE_DST="/usr/local/lib/.engine"

echo "=== Deploying dvbs2_wf_TR to $USER@$TARGET ==="

# ---- 1. Create remote directory structure ---------------------------------
echo "  [1] Creating remote directories ..."
ssh "$USER@$TARGET" "mkdir -p $TARGET_DIR/{bin,config,media}"

# ---- 2. Copy binary, configs, media, wrapper ------------------------------
echo "  [2] Copying dvbs2_wf_TR binary ..."
scp "$HERE/bin/dvbs2_wf_TR" "$USER@$TARGET:$TARGET_DIR/bin/"

echo "  [3] Copying config files ..."
scp "$HERE/config/"*.toml "$USER@$TARGET:$TARGET_DIR/config/"

echo "  [4] Copying media files ..."
scp "$HERE/media/"* "$USER@$TARGET:$TARGET_DIR/media/" 2>/dev/null || true

echo "  [5] Copying wrapper script ..."
scp "$HERE/scripts/dvbs2_wf_TR.sh" "$USER@$TARGET:$TARGET_DIR/dvbs2_wf_TR.sh"

# ---- 3. Copy engine (conda-packed GNU Radio) ------------------------------
if [ -d "$ENGINE_SRC" ]; then
    echo "  [6] Copying engine ($ENGINE_SRC) to $USER@$TARGET ..."
    echo "      (this may take several minutes - engine is ~1.3 GB)"
    ssh "$USER@$TARGET" "echo $USER | sudo -S mkdir -p $ENGINE_DST 2>/dev/null; echo $USER | sudo -S chown $USER:$USER $ENGINE_DST"
    rsync -avz --progress "$ENGINE_SRC/" "$USER@$TARGET:$ENGINE_DST/"
else
    echo "  [6] WARNING: Engine not found at $ENGINE_SRC"
    echo "      The engine must be installed separately on the target."
    echo "      Copy or conda-pack an engine to $ENGINE_DST on the target."
fi

# ---- 4. Set permissions ---------------------------------------------------
echo "  [7] Setting permissions ..."
ssh "$USER@$TARGET" "chmod +x $TARGET_DIR/bin/dvbs2_wf_TR $TARGET_DIR/dvbs2_wf_TR.sh"

# ---- 5. Verify ------------------------------------------------------------
echo "  [8] Verifying deployment ..."
ssh "$USER@$TARGET" "ls -la $TARGET_DIR/bin/ $TARGET_DIR/config/ && echo '=== preflight check ===' && cd $TARGET_DIR && ./dvbs2_wf_TR.sh check"

echo ""
echo "=== Deployment complete ==="
echo "  Target:  $USER@$TARGET:$TARGET_DIR"
echo "  Run:     ssh $USER@$TARGET 'cd $TARGET_DIR && ./dvbs2_wf_TR.sh tx video'"
