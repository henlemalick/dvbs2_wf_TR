#!/bin/bash
# =============================================================================
# build_deb_driver.sh — Build the dvbs2-wf-tr .deb package
#
# Can run anywhere (no engine required). Packages the PyInstaller binary + all
# configs + media + wrapper into a .deb that installs to /opt/dvbs2_wf_TR/.
#
# Usage:
#   ./scripts/build_deb_driver.sh [output_dir] [source_dir]
#
#   output_dir   Where to write the .deb (default: ./dist/)
#   source_dir   Path to dvbs2_wf_TR deploy/repo root (default: .. relative to script)
#
# After installing, the user runs:
#   /opt/dvbs2_wf_TR/dvbs2_wf_TR.sh check
#
# Or a symlink is placed at /usr/local/bin/dvbs2_wf_TR for convenience:
#   dvbs2_wf_TR check
# =============================================================================
set -e

HERE="${2:-$(cd "$(dirname "$0")/.." && pwd)}"
OUTDIR="${1:-dist}"
mkdir -p "$OUTDIR"

PKG="dvbs2-wf-tr"
VER="1.0.0"
ARCH="arm64"
DEB="${PKG}_${VER}_${ARCH}.deb"

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
Package: dvbs2-wf-tr
Version: 1.0.0
Section: comm
Priority: optional
Architecture: arm64
Depends: dvbs2-wf-tr-engine (>= 1.0.0)
Maintainer: dvbs2_wf_TR <nobody>
Description: DVBS2-WF-TR SDR driver — one-binary DVB-S2 link for USRP B210
 Installs the dvbs2_wf_TR driver and supporting files to /opt/dvbs2_wf_TR/
 and creates a convenience symlink at /usr/local/bin/dvbs2_wf_TR.
 Requires the engine package (dvbs2-wf-tr-engine) at /usr/local/lib/.engine/.
CTRL

# ---- postinst ---------------------------------------------------------------
mkdir -p "$BUILD/DEBIAN"
cat > "$BUILD/DEBIAN/postinst" << 'POST'
#!/bin/sh
set -e
# Make sure the engine is at the expected location
if [ ! -d /usr/local/lib/.engine ]; then
    echo "WARNING: dvbs2-wf-tr-engine not installed yet."
    echo "  Install it first:  sudo dpkg -i dvbs2-wf-tr-engine_*.deb"
fi
# Ensure binary is executable
chmod 755 /opt/dvbs2_wf_TR/bin/dvbs2_wf_TR
chmod 755 /opt/dvbs2_wf_TR/dvbs2_wf_TR.sh
exit 0
POST
chmod 755 "$BUILD/DEBIAN/postinst"

# ---- files -----------------------------------------------------------------
# binary
BIN_SRC="$HERE/bin/dvbs2_wf_TR"
if [ ! -f "$BIN_SRC" ]; then
    echo "ERROR: dvbs2_wf_TR binary not found at $BIN_SRC"
    echo "Build it first:  ./scripts/build_driver_binary.sh"
    exit 1
fi
mkdir -p "$BUILD/opt/dvbs2_wf_TR/bin"
cp "$BIN_SRC" "$BUILD/opt/dvbs2_wf_TR/bin/"
chmod 755 "$BUILD/opt/dvbs2_wf_TR/bin/dvbs2_wf_TR"

# configs
mkdir -p "$BUILD/opt/dvbs2_wf_TR/config"
cp "$HERE/config/"*.toml "$BUILD/opt/dvbs2_wf_TR/config/"
chmod 644 "$BUILD/opt/dvbs2_wf_TR/config/"*.toml

# media
mkdir -p "$BUILD/opt/dvbs2_wf_TR/media"
if ls "$HERE/media/"* >/dev/null 2>&1; then
    cp "$HERE/media/"* "$BUILD/opt/dvbs2_wf_TR/media/"
    chmod 644 "$BUILD/opt/dvbs2_wf_TR/media/"*
fi

# wrapper (deploy root or repo scripts/)
if [ -f "$HERE/dvbs2_wf_TR.sh" ]; then
    cp "$HERE/dvbs2_wf_TR.sh" "$BUILD/opt/dvbs2_wf_TR/"
elif [ -f "$HERE/scripts/dvbs2_wf_TR.sh" ]; then
    cp "$HERE/scripts/dvbs2_wf_TR.sh" "$BUILD/opt/dvbs2_wf_TR/"
else
    echo "ERROR: dvbs2_wf_TR.sh not found at $HERE or $HERE/scripts/"
    exit 1
fi
chmod 755 "$BUILD/opt/dvbs2_wf_TR/dvbs2_wf_TR.sh"

# convenience symlink
mkdir -p "$BUILD/usr/local/bin"
ln -s /opt/dvbs2_wf_TR/dvbs2_wf_TR.sh "$BUILD/usr/local/bin/dvbs2_wf_TR"

# ---- strip unneeded --------------------------------------------------------
find "$BUILD" -name '*.pyc' -delete 2>/dev/null || true

# ---- set permissions -------------------------------------------------------
find "$BUILD/opt" -type d -exec chmod 755 {} \;
find "$BUILD/opt" -type f -exec chmod 644 {} \;
chmod 755 "$BUILD/opt/dvbs2_wf_TR/bin/dvbs2_wf_TR"
chmod 755 "$BUILD/opt/dvbs2_wf_TR/dvbs2_wf_TR.sh"

# ---- build -----------------------------------------------------------------
echo "  Building $DEB ..."
fakeroot dpkg-deb --build "$BUILD" "$OUTDIR/$DEB"

# ---- clean -----------------------------------------------------------------
rm -rf "$BUILD"

echo ""
echo "=== Done ==="
echo "  Package: $OUTDIR/$DEB"
echo "  Size:    $(du -sh "$OUTDIR/$DEB" | cut -f1)"
echo ""
echo "Install on new Jetson (after engine):"
echo "  sudo dpkg -i $OUTDIR/$DEB"
echo ""
echo "Run:"
echo "  dvbs2_wf_TR check"
echo "  dvbs2_wf_TR tx video"
