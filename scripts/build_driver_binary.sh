#!/bin/bash
# Build the dvbs2_wf_TR PyInstaller binary on the Jetson target.
set -e
SRC="$HOME/dvbs2_build/pyinstaller/src"
mkdir -p "$SRC"
cd "$SRC"
/usr/local/lib/.engine/bin/python3 -m PyInstaller --onefile \
    --name dvbs2_wf_TR \
    --add-data gr_fullduplex.py:. \
    --add-data ts_tee_stats.py:. \
    --add-data ts_smooth.py:. \
    --add-data rawdata_gen.py:. \
    --add-data rawdata_rx.py:. \
    --clean dvbs2_TR
echo "Build complete: $SRC/dist/dvbs2_wf_TR"
