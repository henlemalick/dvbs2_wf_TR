# dvbs2_wf_TR - Repository Guide

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Repository Structure](#2-repository-structure)
3. [What Changed vs. Original](#3-what-changed-vs-original)
4. [Build Process](#4-build-process)
5. [Deployment to Jetson](#5-deployment-to-jetson)
6. [Configuration Reference](#6-configuration-reference)
7. [Operation Modes](#7-operation-modes)
8. [Dependencies & Source Code](#8-dependencies--source-code)
9. [Dead Ends & Avoided Paths](#9-dead-ends--avoided-paths)
10. [Tests & Verification](#10-tests--verification)
11. [Latency Optimization](#11-latency-optimization)
12. [FFmpeg Modes & Tuning](#12-ffmpeg-modes--tuning)

---

## 1. Architecture Overview

```
                          dvbs2_wf_TR.sh (wrapper)
                                │
                    ┌───────────┴───────────┐
                    │                       │
            bin/dvbs2_wf_TR           config/*.toml
           (PyInstaller binary)        (per-mode configs)
                    │
        ┌───────────┼───────────┐
        │           │           │
    dvbs2-tx   dvbs2-rx    helpers/*.py
    (modulate)  (demodulate) (bundled in binary)
        │           │
        └──────┬────┘
               │
          USRP B210
          (AD9361 radio)
```

**Key principle**: `dvbs2_wf_TR` is an **orchestrator** — it parses TOML config,
builds subprocess pipelines (ffmpeg → dvbs2-tx → radio → dvbs2-rx → ffplay),
and monitors per-second stats. It does NOT do SDR signal processing itself;
that is handled by the GNU Radio C++ extensions (gr_python, dvbs2rx_python,
uhd_python) which live in the engine.

### Runtime Components

| Component | Location | Type |
|---|---|---|
| Engine (GNU Radio) | `/usr/local/lib/.engine/` | conda-packed Python 3.10 + C extensions |
| dvbs2_wf_TR binary | `bin/dvbs2_wf_TR` | PyInstaller single-file ELF |
| Config files | `config/*.toml` | TOML text |
| Media files | `media/` | MP4/H264 samples |
| System ffmpeg | `/usr/bin/ffmpeg` | System or engine-bundled |

---

## 2. Repository Structure

```
dvbs2_wf_TR/
├── bin/
│   └── dvbs2_wf_TR          # Pre-built PyInstaller binary (ARM64)
├── config/
│   ├── video.toml           # Live camera → remote UDP
│   ├── video_local.toml     # Live camera → local display
│   ├── video_remote.toml    # File source → remote UDP
│   ├── raw_wo_udp.toml      # Synthetic data self-test
│   └── raw_w_udp.toml       # External UDP data relay
├── media/
│   └── sample.mp4           # Sample video for file-source mode
├── scripts/
│   ├── build_driver_binary.sh  # PyInstaller build
│   ├── dvbs2_wf_TR.sh          # Unified entry point wrapper
│   └── deploy.sh               # Deploy to remote Jetson
├── src/
│   ├── dvbs2_TR              # Main driver Python source (1335 lines)
│   ├── gr_fullduplex.py      # Full-duplex flowgraph helper
│   ├── ts_tee_stats.py       # MPEG-TS stats monitor
│   ├── rawdata_gen.py        # Synthetic data generator
│   └── rawdata_rx.py         # Synthetic data receiver/validator
├── README.md
├── REPO_GUIDE.md
└── HANDS_OFF.md
```

---

## 3. What Changed vs. Original

### Original State (first session)

- Source files at `/usr/local/lib/dvbs2_TR/` with branded names
- Thin launcher binary that spawned Python scripts
- Helper scripts (`.py`) exposed alongside the driver
- C extensions named `gr_python.so`, `dvbs2rx_python.so`, `uhd_python.so`
- `run.sh` as a thin env-setter + binary launcher
- Multiple stale deploy directories
- No version control

### Changes Made

#### 3.1 Driver Directory Rename
- `/usr/local/lib/dvbs2_TR/` → `/usr/local/lib/sdr_driver/`
- All operator-facing paths use neutral names; no `gr`, `gnuradio`, `dvbs2` leaks

#### 3.2 PyInstaller Binary Build
- `dvbs2_TR.py` compiled via PyInstaller `--onefile` into a standalone ELF
- 4 helper scripts (`gr_fullduplex.py`, `ts_tee_stats.py`, `rawdata_gen.py`, `rawdata_rx.py`) bundled as `--add-data`
- Binary renamed to `dvbs2_wf_TR`
- Binary deployed at `/home/wftest/dvbs2_binary_deploy/` (now restructured to `~/dvbs2_wf_TR/`)

#### 3.3 Helper Script Source Removal
- All helper `.py` files removed from `/usr/local/lib/sdr_driver/`
- Zero `.py` files in the deploy path
- Helper scripts are extracted at runtime by PyInstaller into `sys._MEIPASS`

#### 3.4 Config Restructuring
- Single `link.toml` split into 5 profile-specific configs
- Dedicated configs for: video, video_local, video_remote, raw_wo_udp, raw_w_udp

#### 3.5 Wrapper Script
- `run.sh` replaced by `dvbs2_wf_TR.sh`
- Profile-based config selection
- Auto-`--raw` flag for raw profiles
- Auto-`--loopback` for raw bench/duplex

#### 3.6 Cleanup
- Old deploy dirs: `DVBS2_TR_Deployed`, `DVBS2_TR_Deployed (copy)`, `dvbs2_tr_opencode`
- Stale source files: `/usr/local/bin/dvbs2_TR`, `/usr/local/lib/sdr_driver/dvbs2_TR`
- Build artifacts: `pyinstaller/src/build/`, `pyinstaller/src/dist/` (old ones)

#### 3.7 `.so` Rename Attempt (Failed - Documented)
We attempted to rename the C extension `.so` files to remove generic names:
- `gr_python.*.so` → `rt_python.*.so` (PARTIAL - broke hash tables)
- `dvbs2rx_python.*.so` → `demod_python.*.so` (PARTIAL - broke hash tables)
- `uhd_python.*.so` → `rf_python.*.so` (PARTIAL - broke hash tables)

**Why it failed**: Each `.so` exports `PyInit_<name>()`. Changing the `.dynstr`
string without updating `.gnu.hash`/`.hash` tables breaks `dlsym()`. Available
tools (binutils 2.34, patchelf 0.10) cannot properly rename dynamic symbols
in fully-linked shared libraries.

**Resolution**: Reverted all renames. The old names persist deep inside the
engine (`/usr/local/lib/.engine/`) but are invisible in the deploy directory.
The namespace-level directories (`sdrcore/gr/`, `sdrcore/link/`, `sdrcore/rf/`)
are already generic.

#### 3.8 `sdrcore/rf/__init__.py` Lazy Import Fix
- Fixed import crash by deferring `uhd_python` initialization to `__getattr__`
- Prevents UHD probe from blocking module load

---

## 4. Build Process

### 4.1 Prerequisites on the Build Host (Jetson)

```bash
# Engine (conda-packed GNU Radio 3.10 + Python 3.10)
/usr/local/lib/.engine/    # ~1.3 GB

# PyInstaller (installed in engine)
/usr/local/lib/.engine/bin/pip install pyinstaller

# Source files
src/dvbs2_TR              # Main driver
src/gr_fullduplex.py      # Full-duplex flowgraph
src/ts_tee_stats.py       # TS stats
src/rawdata_gen.py        # Raw data generator
src/rawdata_rx.py         # Raw data receiver
```

### 4.2 Building

```bash
cd dvbs2_wf_TR
./scripts/build_driver_binary.sh
```

What happens:
1. PyInstaller reads `src/dvbs2_TR` as entry point
2. Bundles 4 helper scripts as `--add-data`
3. Produces `bin/dvbs2_wf_TR` (single-file ARM64 ELF, ~8 MB)

### 4.3 Build Output

```
bin/dvbs2_wf_TR  (ELF 64-bit, ARM aarch64, statically linked Python runtime)
```

The binary is **not** fully static — it still has runtime `dlopen()` dependencies:
- Engine `.so` files (loaded at startup via `LD_LIBRARY_PATH`)
- System libraries (libc, libstdc++, libusb, librt, etc.)
- UHD FPGA firmware (loaded at runtime from `$DVBS2_ENV/share/uhd/images`)

---

## 5. Deployment to Jetson

### 5.1 Manual Deployment

```bash
# On the build Jetson:
./scripts/deploy.sh <TARGET_IP> [ssh_user]
```

The deploy script:
1. Creates `~/dvbs2_wf_TR/` on the target
2. Copies `bin/dvbs2_wf_TR`, `config/*.toml`, `media/*`
3. Copies wrapper script as `~/dvbs2_wf_TR/dvbs2_wf_TR.sh`
4. Copies engine via rsync (`/usr/local/lib/.engine/`)
5. Sets permissions and runs `check`

### 5.2 Manual Steps if deploy.sh is not available

```bash
# On the target Jetson:
sudo mkdir -p /usr/local/lib/.engine
# Copy engine from build host
rsync -avz BUILD_HOST:/usr/local/lib/.engine/ /usr/local/lib/.engine/

# Copy application
scp -r BUILD_HOST:~/dvbs2_wf_TR ~/dvbs2_wf_TR

# Set permissions
chmod +x ~/dvbs2_wf_TR/bin/dvbs2_wf_TR ~/dvbs2_wf_TR/dvbs2_wf_TR.sh

# Verify
~/dvbs2_wf_TR/dvbs2_wf_TR.sh check
```

### 5.3 Engine Installation (if engine is not pre-installed)

The engine is a conda-pack of GNU Radio 3.10.12 + Python 3.10.20 + UHD 4.10
+ custom DVB-S2 modules. Build it on the Jetson Xavier:

```bash
# (Inside the conda environment with all packages)
conda pack -n dvbs2_gnuradio -o /tmp/engine.tar.gz
sudo mkdir -p /usr/local/lib/.engine
sudo tar xzf /tmp/engine.tar.gz -C /usr/local/lib/.engine/
```

---

## 6. Configuration Reference

### 6.1 `[node]` Section

| Key | Default | Description |
|---|---|---|
| `device_args` | `"type=b200"` | UHD device arguments (serial, type) |

### 6.2 `[tx]` / `[rx]` Sections

| Key | Default | Description |
|---|---|---|
| `device_args` | `"type=b200"` | Per-direction device args |
| `freq_hz` | — | Center frequency (Hz) |
| `modcod` | — | Modulation + code rate (e.g. `QPSK_1/2`) |
| `frame_size` | `"short"` | FECFRAME: `short` (16200) or `normal` (64800) |
| `pilots` | `true` | PL pilot symbols on/off |
| `rolloff` | `0.35` | RRC rolloff factor |
| `symbol_rate` | — | Symbol rate (Hz), typically 500e3..4e6 |
| `oversample` | `2` (tx) / `4` (rx) | Samples per symbol |
| `gain_db` | — | RF gain (dB) |
| `antenna` | — | Antenna port: `TX/RX`, `RX2`, `TX/RX` |
| `lo_offset_hz` | (rx only) | LO offset for RX (Hz) |

### 6.3 `[payload]` Section

| Key | Default | Description |
|---|---|---|
| `source` | `"file:media/sample.mp4"` | Video source: `v4l2:/dev/video0`, `file:path`, `udp://` |
| `sink` | `"udp://192.168.0.100:1234"` | Video sink: `display`, `file:path`, `udp://host:port` |
| `codec` | `"h264"` | Video codec |
| `resolution` | `"640x480"` | Output resolution or `"auto"` |
| `framerate` | `30` | Output framerate (fps) |
| `encoder` | `"sw"` | Encoder: `sw` (libx264), `hw` (nvv4l2h264enc) |

### 6.4 `[raw]` Section

| Key | Default | Description |
|---|---|---|
| `payload` | `"TAG 786 latency test"` | Text payload for raw data frames |

### 6.5 `[fec]` Section

| Key | Default | Description |
|---|---|---|
| `ldpc_iters` | `25` | Max LDPC decoder iterations |

---

## 7. Operation Modes

### 7.1 `check` - Preflight

Validates:
- Engine presence and interpreter
- Media tools (ffmpeg/ffplay)
- Modem core (dvbs2-tx/rx)
- Radio (B210) detection
- Video encoder availability
- Config net-rate calculation

### 7.2 `tx` - Transmit

Starts encode → modulate pipeline:
- File source: pre-encodes to cached CBR mpegts, loops
- Live source (v4l2): realtime ffmpeg/libx264 or GStreamer NVENC
- Raw: synthetic data generator or UDP injection

Output is filtered by link.toml rates; per-sec TX stats show PLFRAME count.

### 7.3 `rx` - Receive

Starts demodulate → decode → sink pipeline:
- Demodulates USRP stream through dvbs2-rx
- Pipes recovered MPEG-TS through ts_tee_stats (live stats)
- Sends to sink: display, file, or UDP
- `--full-stats` enables JSON monitor on port 8011 with SNR/CFO/LDPC/FER

### 7.4 `bench` - Self-test

TX + RX on two B210s (or loopback on one):
- TX raw data, RX validates
- Measures throughput, latency, continuity errors
- Default 20s, configurable with `--secs`

### 7.5 `duplex` - Full Duplex

TX + RX simultaneously on ONE B210:
- Combined flowgraph via `gr_fullduplex.py`
- `--loopback` for internal self-test (no radio)
- `--full-stats` for two-column per-sec readout

### 7.6 `rates` - Print MODCOD Rates

Calculates and prints net bitrate and auto-scaled resolution for the configured MODCOD.

---

## 8. Dependencies & Source Code

### 8.1 Required for Runtime

| Dependency | Location | Purpose |
|---|---|---|
| Engine (GNU Radio 3.10) | `/usr/local/lib/.engine/` | Python runtime + C extensions |
| UHD firmware | `$DVBS2_ENV/share/uhd/images/` | B210 FPGA images |
| ffmpeg | `/usr/bin/ffmpeg` (or engine) | H264 encoding, mpegts muxing |
| ffplay | `/usr/bin/ffplay` (or engine) | Video display |
| gst-launch-1.0 | (optional) | HW encoder (Jetson NVENC) |

### 8.2 Required for Building

| Dependency | Location | Purpose |
|---|---|---|
| PyInstaller | engine pip package | Binary compilation |
| Python 3.10 | engine | Source interpretation |
| conda-pack engine | `/usr/local/lib/.engine/` | Build-time Python environment |

### 8.3 Source Files in This Repo

| File | Purpose | Notes |
|---|---|---|
| `src/dvbs2_TR` | Main CLI orchestrator | 1335 lines - config, pipelines, stats |
| `src/gr_fullduplex.py` | Full-duplex GNU Radio flowgraph | Combined TX+RX on one B210 |
| `src/ts_tee_stats.py` | MPEG-TS continuity + bitrate monitor | Reads TS from pipe, prints stats |
| `src/rawdata_gen.py` | Synthetic test frame generator | Produces padded frames at target rate |
| `src/rawdata_rx.py` | Test frame receiver/validator | Counts frames, checks continuity |
| `scripts/build_driver_binary.sh` | PyInstaller build script | Compiles dvbs2_TR + helpers |

### 8.4 Source Files NOT in This Repo (Engine)

These are the GNU Radio + DVB-S2 modem C++ sources. They are compiled into
the engine's `.so` files and are **not** Python source:

| Component | Function |
|---|---|
| `gr_python.so` | GNU Radio core bindings |
| `dvbs2rx_python.so` | DVB-S2 demodulator (LDPC/BCH decoder) |
| `uhd_python.so` | USRP hardware driver bindings |
| `dvbs2-tx` | DVB-S2 modulator (BBFRAME → PLFRAME) |
| `dvbs2-rx` | DVB-S2 demodulator (PLFRAME → TS) |

These must be rebuilt from the upstream sources if modifications are needed:
- GNU Radio: https://github.com/gnuradio/gnuradio (v3.10)
- gr-dvbs2rx: DVB-S2 receiver OOT module
- gr-uhd: UHD bindings
- dvb_fpga: DVB-S2 encoder (OpenResearchInstitute)

---

## 9. Dead Ends & Avoided Paths

### 9.1 `.so` File Renaming (DEAD END)

**Attempted**: Rename `gr_python.so` → `rt_python.so`, `dvbs2rx_python.so` →
`demod_python.so`, `uhd_python.so` → `rf_python.so` by:
- `objcopy --redefine-sym` (binutils 2.34) — only affects `.symtab`, not `.dynsym`
- Direct `.dynstr` patching — breaks `.gnu.hash` → `dlsym()` fails
- `patchelf` 0.10 — lacks `--rename-dynamic-symbol` (added in 0.13+)

**Verdict**: NOT POSSIBLE without source recompilation or patchelf ≥0.13.
The `PyInit_<name>` symbol is baked into the ELF hash tables.

**Current state**: Engine retains original filenames, buried in
`/usr/local/lib/.engine/` — not visible in deploy.

### 9.2 Nuitka Compilation (AVOIDED)

**Considered**: Use Nuitka instead of PyInstaller for faster startup.

**Reason avoided**: Nuitka compiles Python to C then to native code, but:
- Requires all dependencies (including C extensions) to be importable at compile time
- Plugin system is less mature for PyInstaller-style `--add-data`
- GNU Radio C extensions (`gr_python.so`, etc.) need special handling
- PyInstaller's `--onefile` is proven and sufficient for our use case

### 9.3 Cython Compilation (AVOIDED)

**Considered**: Compile individual hot-path modules with Cython.

**Reason avoided**: The hot path is in the GNU Radio C++ extensions and
ffmpeg — Python overhead is negligible in the orchestrator. Cython would add
complexity with minimal gain.

### 9.4 Single Fully-Static Binary (DEAD END)

**Considered**: Link everything (USRP drivers, GNU Radio, ffmpeg, Python)
into one monolithic static binary.

**Reason avoided**: Not feasible. USRP requires `libusb` (udev integration),
GNU Radio requires `dlopen()` for OOT modules, ffmpeg is a separate system
executable. The architecture fundamentally requires subprocess spawning.

### 9.5 Rename Engine to "/dvbs2" or Hide it Completely (AVOIDED)

**Considered**: Rename `/usr/local/lib/.engine/` to `/usr/local/lib/.sdr/` or
similar.

**Reason avoided**: The engine path is referenced in:
- `LD_LIBRARY_PATH` in the wrapper script
- `sitecustomize.py` (Python's `sys.path` additions)
- UHD images path (`$DVBS2_ENV/share/uhd/images`)
- Build scripts

Renaming would require updating every reference and would break the
conda-pack activation scripts. The `.engine` directory is already hidden
(dot-prefixed) which is sufficient.

### 9.6 Bundling ffmpeg Inside PyInstaller (AVOIDED)

**Considered**: Add ffmpeg as `--add-binary` in PyInstaller.

**Reason avoided**: ffmpeg is 150+ MB with all codecs and has complex library
dependencies. PyInstaller would need to extract it to a temp dir at every
startup, slowing launch. System ffmpeg is always available on Jetson
(flashed via SDK Manager) and more reliable.

---

## 10. Tests & Verification

### 10.1 Preflight Check

```bash
./dvbs2_wf_TR.sh check
```

Validates every component and prints a pass/fail summary:

```
== DVBS2_TR preflight ==
  [OK] runtime present
  [OK] runtime interpreter
  [OK] media tool
  [OK] modem core
  [OK] radio (B210) detected
  [OK] live video encoder = sw (libx264 via ffmpeg)
  [OK] config + net-rate calc
== ALL GOOD - link ready ==
```

### 10.2 Loopback Test (No Radio)

```bash
./dvbs2_wf_TR.sh bench raw_wo_udp --secs 30
```

Verifies the software pipeline end-to-end without RF:
- TX generates synthetic frames
- Frames pass through modulator → demodulator in memory
- RX validates frame continuity and data integrity
- Loopback SNR is effectively infinite (no RF channel)

### 10.3 Cabled RF Loopback Test

- B210 TX port → cable → B210 RX port
- Signal at 1.282 GHz, 2 MSym/s QPSK 1/2
- Verified: SNR 24.5 dB, FER 0.00%, stable for 30+ seconds
- Video plays with ~0.77s latency (short frame)

### 10.4 Over-the-Air Test

- Two Jetsons with B210s, ~10 m apart
- QPSK 1/2 short frame, 2 MSym/s
- Verified link margin sufficient for clean decode

### 10.5 Import Verification

```bash
# Python import validation (engine context)
cd /usr/local/lib/.engine && LD_LIBRARY_PATH=lib bin/python3 -c "
import sdrcore.gr
import sdrcore.link
import sdrcore.rf
print('All C extension imports OK')
"
```

---

## 11. Latency Optimization

### 11.1 Measured Latency

| MODCOD | Frame | Symbol Rate | Latency | Notes |
|---|---|---|---|---|
| QPSK 1/2 | short | 2 MSym/s | ~770 ms | Sub-second target achieved |
| QPSK 1/2 | normal | 2 MSym/s | ~2.7 s | Coding gain at latency cost |
| 8PSK 3/5 | short | 2 MSym/s | ~770 ms | Higher spectral efficiency |

### 11.2 Techniques Applied

#### Short FECFRAME
- `frame_size = "short"` (16200 bits vs 64800 for normal)
- Reduces LDPC decoding latency ~4x
- Minimal coding gain loss at typical SNRs

#### Low Symbol Rate
- 2 MSym/s stays within B210 USB3 bandwidth
- Prevents USB host-buffer overflows (the 'OOOO' signature in radio logs)
- Enables `sc16` sampling (16-bit I/Q) without overflow

#### UHD Stream Args
```
num_send_frames=8, send_frame_size=16384
num_recv_frames=8, recv_frame_size=16384
```
- Tiny host queues (~8 ms per direction at 16 MB/s)
- Keeps the FX3 FIFO + FPGA SRAM from buffering excess samples
- Key setting: without these, duplex steady-state latency is ~4 s

#### ffmpeg Tuning
- `preset=ultrafast`, `tune=zerolatency`
- `-bf 0` (no B-frames), `-g fps` (1 s GOP)
- `nal-hrd=cbr` (constant bitrate) for stable TS rate
- `-flush_packets 1` for immediate mux output

#### Pilot Tones
- `pilots = true` enables PL pilot symbols
- Better carrier recovery at low SNR
- Small overhead (every 16 slots)

#### No B210 Internal Loopback
- TX/RX channels are physically separated on the B210
- Full duplex uses different frequency or TDD
- Self-interference managed by antenna isolation

### 11.3 Not Yet Optimized

- **HW encoder**: Jetson nvv4l2h264enc is available but not extensively tested
- **sc8 sampling**: Halves USB bandwidth vs. sc16. Can reduce overflow at
  extreme rates but needs SNR testing at low link margins
- **ACM/VCM**: Auto-detection of MODCOD changes mid-stream. Implemented but
  not fully validated over-the-air

---

## 12. FFmpeg Modes & Tuning

### 12.1 Software Encoder (libx264)

Used when `encoder = "sw"` in config:

```bash
ffmpeg [source] \
  -an -sn \
  -vf "scale=WxH,fps=N" \
  -c:v libx264 \
  -preset ultrafast \
  -tune zerolatency \
  -bf 0 \
  -b:v RATE -maxrate RATE -bufsize RATE/2 \
  -g N -keyint_min N -sc_threshold 0 \
  -x264-params "nal-hrd=cbr:force-cfr=1:no-mbtree=1" \
  -flush_packets 1 \
  -f mpegts -muxrate MUXRATE \
  -mpegts_flags +pat_pmt_at_frames \
  pipe:1
```

Key parameters:
- **preset ultrafast**: Minimum encoding latency (vs. veryfast/slow)
- **zerolatency tune**: Disables lookahead, reduces delay
- **no B-frames** (`-bf 0`): B-frames add decode delay
- **1 s GOP** (`-g fps`): Keyframe every second for channel switching
- **CBR** (`nal-hrd=cbr`): Constant bitrate, essential for mpegts muxrate
- **no scene-cut** (`-sc_threshold 0`): Prevents irregular keyframes
- **no mb-tree** (`no-mbtree=1`): Disables macroblock tree rate control

### 12.2 Hardware Encoder (nvv4l2h264enc)

Used when `encoder = "hw"` in config:

```bash
gst-launch-1.0 \
  v4l2src device=/dev/video0 ! \
  video/x-raw ! \
  videoconvert ! videoscale ! videorate ! \
  video/x-raw,format=I420,width=W,height=H,framerate=N/1 ! \
  nvvidconv ! \
  video/x-raw(memory:NVMM),format=NV12 ! \
  nvv4l2h264enc \
    bitrate=BPS control-rate=1 \
    iframeinterval=N idrinterval=N \
    insert-sps-pps=true insert-vui=true insert-aud=true \
    preset-level=1 maxperf-enable=true \
    num-B-Frames=0 num-Ref-Frames=1 \
    profile=2 poc-type=2 \
    vbv-size=VBV \
  ! h264parse config-interval=1 ! \
  fdsink fd=1 sync=false
```

The HW path pipes raw H264 through ffmpeg's mpegts muxer for CBR padding
(the `-muxrate` parameter is essential for stable TX timing).

### 12.3 Media Tool Path Resolution

The binary resolves ffmpeg/ffplay in this order:
1. `shutil.which("ffmpeg")` - system path first
2. `os.path.join(ENV, "bin", "ffmpeg")` - engine fallback
3. Helper scripts searched via `find_helper()` in `sys._MEIPASS` (PyInstaller
   temp dir), then `.internal/`, then flat, then system

---

## Appendix A: Jetson-Specific Details

- **Board**: NVIDIA Jetson Xavier NX / AGX Xavier
- **Kernel**: 4.9 (L4T R32.x) or 5.10 (L4T R35.x)
- **USB**: B210 on USB 3.0 (requires UHD built with libusb)
- **HW encoder**: nvv4l2h264enc via GStreamer (requires `nvidia-l4t-gstreamer`)
- **Build tools**: gcc 7.5 / 9.3, cmake, Python 3.10 via conda

---

## Appendix B: B210-Specific Details

- **Radio**: USRP B210, AD9361, 2 TX + 2 RX, 70 MHz–6 GHz
- **Serial**: 8000693 (this unit)
- **Bandwidth**: Up to 56 MHz, B210 ~8 MSym/s practical max
- **USB**: USB 3.0 required for full duplex at 2 MSym/s
- **UHD**: Version 4.10 (engine-bundled)
- **Firmware**: FX3 firmware + FPGA bitstream loaded at runtime from
  `$DVBS2_ENV/share/uhd/images/`

---

## Appendix C: Common Issues

### "USB OF (overflow)" in stats
1. Switch to `sc8` sampling: `[node] samp_type = "sc8"`
2. Reduce RX oversample: `[rx] oversample = 2`
3. Run `sudo jetson_clocks`
4. Lower symbol rate or use `num_recv_frames=4`

### "No radio detected"
1. Check USB: `uhd_find_devices`
2. Check serial in config matches `uhd_usrp_probe`
3. Kill stale processes: `pkill -9 -f dvbs2`

### "ffmpeg encoder not found"
1. Install: `sudo apt install ffmpeg`
2. Or ensure engine has `bin/ffmpeg`

### Binary crashes on startup
1. Check `LD_LIBRARY_PATH` includes engine lib
2. Verify engine at `/usr/local/lib/.engine/`
3. Run `dvbs2_wf_TR.sh check` for diagnostics
