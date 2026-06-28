#!/bin/bash
# =============================================================================
# build_deb_engine.sh — Build the dvbs2-wf-tr-engine .deb package
#
# RUN THIS ON THE EXISTING JETSON where /usr/local/lib/.engine/ is installed.
# The resulting .deb (~1.3 GB) can be scp'd to a new Jetson and installed via
#   sudo dpkg -i dvbs2-wf-tr-engine_1.0.0_arm64.deb
#
# Usage:
#   ./scripts/build_deb_engine.sh [output_dir]
#
#   output_dir   Where to write the .deb (default: ./dist/)
#   RUN ON THE JETSON — requires /usr/local/lib/.engine/ to exist. =============================================================================
set -e

OUTDIR="${1:-dist}"
mkdir -p "$OUTDIR"

ENGINE_SRC="/usr/local/lib/.engine"
PKG="dvbs2-wf-tr-engine"
VER="1.0.0"
ARCH="arm64"
DEB="${PKG}_${VER}_${ARCH}.deb"

if [ ! -d "$ENGINE_SRC" ]; then
    echo "ERROR: Engine not found at $ENGINE_SRC"
    echo "Run this script on the Jetson that has the engine installed."
    exit 1
fi

# ---- deps check ------------------------------------------------------------
for cmd in dpkg-deb fakeroot; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found — install it first:"
        echo "  sudo apt install $cmd"
        exit 1
    fi
done

BUILD="/tmp/deb-build-${PKG}"
rm -rf "$BUILD"

# ---- control file ----------------------------------------------------------
mkdir -p "$BUILD/DEBIAN"
cat > "$BUILD/DEBIAN/control" << 'CTRL'
Package: dvbs2-wf-tr-engine
Version: 1.0.0
Section: comm
Priority: optional
Architecture: arm64
Maintainer: dvbs2_wf_TR <nobody>
Depends: libc6 (>= 2.31), libstdc++6 (>= 10)
Description: DVBS2-WF-TR runtime engine (conda-packed GNU Radio 3.10.12)
 Installs the pre-built conda-packed GNU Radio runtime and all its
 dependencies to /usr/local/lib/.engine/. Required by dvbs2-wf-tr.
CTRL

# ---- engine files ----------------------------------------------------------
echo "  Copying engine from $ENGINE_SRC (may need sudo for read-protected files) ..."
mkdir -p "$BUILD/usr/local/lib/.engine"
sudo rsync -a --info=progress2 "$ENGINE_SRC/" "$BUILD/usr/local/lib/.engine/"

# ---- strip unneeded --------------------------------------------------------
echo "  Stripping unneeded files (pyc, __pycache__) ..."
find "$BUILD" -name '*.pyc' -delete
find "$BUILD" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# ---- set permissions -------------------------------------------------------
find "$BUILD" -type d -exec chmod 755 {} \;
find "$BUILD" -type f -exec chmod 644 {} \;
find "$BUILD" -type f \( -name '*.so' -o -name '*.so.*' \) -exec chmod 755 {} \;

# ---- build -----------------------------------------------------------------
echo "  Building $DEB (may take a few minutes for ~1.3 GB) ..."
fakeroot dpkg-deb --build "$BUILD" "$OUTDIR/$DEB"

# ---- clean (use sudo since some engine files may be root-owned) ------------
sudo rm -rf "$BUILD"

echo ""
echo "=== Done ==="
echo "  Package: $OUTDIR/$DEB"
echo "  Size:    $(du -sh "$OUTDIR/$DEB" | cut -f1)"
echo ""
echo "Install on new Jetson:"
echo "  sudo dpkg -i $OUTDIR/$DEB"
