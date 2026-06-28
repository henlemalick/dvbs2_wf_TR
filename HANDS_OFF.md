# HANDS_OFF.md - Next-Session Context

This document gives a future AI agent (or human) all the context needed to
resume work without re-discovering everything. It is the **session memory**:
milestones, dead ends, decisions, and the exact current state.

---

## Session Summary

We took a loose collection of Python scripts (DVBS2_TR) running on a Jetson
Xavier + USRP B210 and produced a minimal, deployable, single-binary DVB-S2
link system with zero `.py` files at runtime and profile-based configuration.

---

## Milestones Achieved

### M1: PyInstaller Standalone Binary
- `dvbs2_TR` (1335-line CLI orchestrator) compiled via PyInstaller `--onefile`
- 4 helper scripts bundled as `--add-data` (extracted at runtime to `sys._MEIPASS`)
- Binary size: ~8 MB (ARM64 aarch64)
- Entry point baked into ELF — no external `.py` file needed

### M2: Helper Script Removal
- `gr_fullduplex.py`, `ts_tee_stats.py`, `rawdata_gen.py`, `rawdata_rx.py`
  deleted from `/usr/local/lib/sdr_driver/`
- Zero `.py` files in deploy path
- Helpers served entirely from PyInstaller runtime extraction

### M3: Config Profile System
- Single `link.toml` split into 5 profile configs
- Wrapper script auto-selects config + flags by profile name

### M4: Deploy Restructuring
- Clean top-level folder: `bin/`, `config/`, `media/`, wrapper, deploy script
- All stale directories removed
- Single `deploy.sh` for new Jetson provisioning

### M5: Verified Cabled Loopback
- B210 TX→RX cable link at 1.282 GHz
- QPSK 1/2 short frame, 2 MSym/s
- SNR 24.5 dB, FER 0.00%, stable 30+ seconds
- Latency ~770 ms (sub-second target achieved)

### M6: Verified Import Integrity
- All C extensions import correctly: `sdrcore.gr`, `sdrcore.link`, `sdrcore.rf`
- RF module lazy init fix prevents import-time UHD probe

---

## Key Decisions

| Decision | Rationale |
|---|---|
| PyInstaller over Nuitka/Cython | Simpler, proven `--onefile`, sufficient for orchestrator |
| Engine stays as `/usr/local/lib/.engine/` | Conda-pack path; renaming breaks more than it fixes |
| `.so` files keep original names | Cannot rename dynamic symbols without source rebuild |
| ffmpeg stays as system binary | 150+ MB, complex deps, always available on Jetson |
| Wrapper script in bash | Shell pipelines are the native runtime format |
| TOML config | Readable, minimal parser in 50 lines of Python |
| Profile-based configs | Encodes tested settings; operator just picks a named profile |

---

## Paths Not to Take (Dead Ends)

### `.so` Rename via ELF Patching
- **Tools tried**: `objcopy --redefine-sym` (binutils 2.34), direct `.dynstr`
  edit + hash table rebuild, `patchelf` 0.10
- **Why it fails**: `PyInit_<name>` is in `.dynsym`, `.gnu.hash`, and `.hash`.
  No tool on this system (binutils 2.34, patchelf 0.10) can update all three
  correctly for a fully-linked shared library.
- **Fix path**: Either recompile source with new module names, or wait for
  patchelf ≥0.13 with `--rename-dynamic-symbol`.
- **Current state**: REVERTED — all files and imports back to original names.

### Fully Static Single Binary
- **Why it's impossible**: USRP uses libusb (hotplug, udev), GNU Radio uses
  `dlopen()` for OOT modules, ffmpeg is separate. Architecture requires
  subprocess spawning and dynamic linking.

### Cython/Nuitka Compilation
- Not needed — Python overhead is negligible in the orchestrator. The hot
  path is in C++ (GNU Radio, ffmpeg).

---

## Paths Avoided (Could Be Taken but Skipped Due to Complexity)

| Path | Benefit | Complexity | Status |
|---|---|---|---|
| ACM/VCM auto-detect | Hands-off MODCOD switching | Moderate - implemented but not OTA validated | In code, needs testing |
| HW encoder (NVENC) | Lower CPU usage, possibly lower latency | Moderate - GStreamer pipeline tested | In code, default falls to SW |
| sc8 sampling | Halves USB bandwidth | Low - single config change | Documented, not default |
| Link hardening docs | Better OTA performance | Low - config tuning | Referenced in source |
| Engine rename to `/dvbs2` | Hides `engine` label | High - breaks conda-pack activation | Skipped |
| Systemd service | Auto-start on boot | Low - trivial | Not implemented |

---

## Tests Run

### Successful
- [x] **Preflight check** (`dvbs2_wf_TR.sh check`) — all green
- [x] **Python C extension imports** — `sdrcore.gr`, `sdbs2rx`, `uhd` all OK
- [x] **Cabled RF link** — QPSK 1/2, 2 MSym/s, 30+ sec stable
- [x] **Loopback** (no radio) — TX→RX in-memory, raw data verification
- [x] **Config net-rate calculation** — all MODCODs compute correctly
- [x] **PyInstaller binary** — builds, runs, extracts helpers

### Not Yet Run
- [ ] **Over-the-air link** (two Jetsons, real antenna)
- [ ] **ACM/VCM modcod change mid-stream**
- [ ] **HW encoder vs SW encoder latency comparison**
- [ ] **16APSK / 32APSK extended engine path**
- [ ] **Deploy to a fresh Jetson via deploy.sh**

---

## Configurations Verified

| Config | Symbol Rate | Frame | MODCOD | Pilots | Rolloff | Result |
|---|---|---|---|---|---|---|
| `video` (default) | 2.0e6 | short | QPSK_1/2 | on | 0.35 | Cabled OK, ~770ms |
| `raw_wo_udp` | 2.0e6 | short | QPSK_1/2 | on | 0.35 | Loopback OK |

---

## Latency Measurements

| MODCOD | Frame | SR | Latency | Notes |
|---|---|---|---|---|
| QPSK 1/2 | short | 2.0 MSym/s | ~770 ms | Sub-second |
| QPSK 1/2 | normal | 2.0 MSym/s | ~2.7 s | Normal frame |
| QPSK 1/2 | short | 1.54 MSym/s | ~1000 ms | Lower SR → more latency |

Formula: `base = 1.54e6 / sr` (base at short frame). Normal frame multiplies
by ~3.5x. APSK modes (16/32) add extended engine latency (~1.5-2 s).

---

## FFmpeg Modes

### Software (Default)
```bash
encoder = "sw"   # libx264, ultrafast, zerolatency, CBR
```

### Hardware (Jetson NVENC)
```bash
encoder = "hw"   # nvv4l2h264enc via GStreamer, piped through ffmpeg muxer
```

HW encoder not run OTA yet — needs GStreamer + nvidia-l4t-gstreamer package.

---

## Current File State (Jetson)

```
~/dvbs2_wf_TR/                    # Main deployment folder
├── bin/dvbs2_wf_TR               # PyInstaller binary (8 MB)
├── config/*.toml                 # 5 profile configs
├── media/sample.mp4              # Sample video
└── dvbs2_wf_TR.sh                # Entry point wrapper
```

The engine is at `/usr/local/lib/.engine/` (~1.3 GB, conda-packed GNU Radio).

---

## GitHub Repo

**Owner**: `henlemalick`  
**Contents**: Source code, configs, scripts, documentation, pre-built binary  
**Clone URL**: `git@github.com:henlemalick/dvbs2_wf_TR.git`  
**Push**: If not yet created, the next agent should create the repo and push.

---

## Next Session Starting Point

1. The deploy is clean and functional — `check` passes
2. If working on a **different Jetson**: run `deploy.sh TARGET_IP`
3. If **modifying the driver**: edit `src/dvbs2_TR`, rebuild with
   `scripts/build_driver_binary.sh`, recopy to deploy
4. If the **engine is missing**: conda-pack from a working Jetson or rebuild
   from GNU Radio source
5. Key files to read first: `src/dvbs2_TR` (entry point), `REPO_GUIDE.md`
6. The `.so` rename path is a dead end — do NOT attempt without patchelf ≥0.13
   or source recompilation
7. All 1335 lines of `dvbs2_TR` are the orchestrator — not a signal processing module
