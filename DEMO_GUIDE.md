# dvbs2_wf_TR — Demo Guide

## Prerequisites

- 2× Jetson Xavier NX / AGX with B210 SDRs (control plane at `.41` and `.109`)
- Both Jetsons have `dvbs2_wf_TR` installed (via `.deb` or `deploy.sh`)
- Both B210s on USB 3.0, UHD finds them
- Runtime: `/usr/local/lib/.engine/` present
- Network trust: SSH keys or password for both Jetsons

### Preflight (both stations)

```bash
dvbs2_wf_TR check
```

Expected output:
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

---

## Demo 1: Loopback Self-Test (Software-Only)

No radio needed. TX→RX in memory on a single Jetson. Quickest way to verify
the software pipeline.

### Command

```bash
# 30-second benchmark, synthetic data
dvbs2_wf_TR bench raw_wo_udp --secs 30
```

### What You See

```
== DVBS2_TR bench ==
   TX raw-data: QPSK_1/2 short @ 2.000 MSym/s (net 1.73 Mbps)
   RX raw-data readout: auto(ACM) short @ 2.000 MSym/s
   +--------------------------------------------------------------------+
   | DVB-S2 RAW-DATA LINK TEST                                          |
   +--------------------------------------------------------------------+
     mode      : DVBS2_TR link
     MODCOD    : QPSK_1/2              symbol rate : 2.000 MSym/s
     frequency : 1282.000 MHz          data PID    : 0x100
     payload   : 200 B/frame           target rate : 600 kbit/s
   +--------------------------------------------------------------------+

  rx seq=0          len=200 payload=00010203.. lat=    0.2ms
  rx seq=1          len=200 payload=01020304.. lat=    0.1ms
  rx seq=2          len=200 payload=02030405.. lat=    0.2ms
  ...
  [    30s] rx   353 fr/s PSR 100.0% |  0.565 Mbps | loss/s 0 cum 0 | lat 0/0/0 ms (ts-resync 0)
```

- PSR should be 100.0% for the full duration
- Latency ~0 ms (in-memory loopback)
- Zero TS resyncs

### What It Verifies

- PyInstaller binary can find and invoke helper scripts (`rawdata_gen.py`,
  `rawdata_rx.py`)
- The modulator/demodulator chain produces valid MPEG-TS
- Frame continuity is maintained end-to-end
- SEQ counter, TX timestamps, and payload patterns all match

---

## Demo 2: Cabled RF Link (Video)

One B210 TX port → coax cable → second B210 RX port (or same B210 in simplex
mode with a cable between TX and RX ports). Use **two stations** if available,
or one station with separate TX/RX paths on the same B210.

### Setup

```bash
# Station A (TX): 192.168.0.41
dvbs2_wf_TR tx video_local

# Station B (RX): 192.168.0.109
# If receiver has a display:
dvbs2_wf_TR rx video_local
# If receiver is headless (UDP to laptop):
dvbs2_wf_TR rx video_remote
```

**Important**: On a single desk with two B210s close together, reduce TX gain
to prevent RF overload of the RX front-end:

```bash
# Override TX gain on both stations
dvbs2_wf_TR tx video_local --tx-gain 10
# RX station may need to use --rx-gain 40 (default) or lower depending on
# cable loss / distance
```

### Cable Connections

| TX Station | RX Station |
|------------|------------|
| B210 TX/RX port | B210 RX2 port |
| 30 dB attenuator (or cable loss) | — |
| Coax SMA→SMA | Coax SMA→SMA |

Add 20-30 dB attenuation if the B210s are on the same desk to avoid receiver
saturation. At 1.282 GHz a 3 m SMA cable has ~1.5 dB loss — not enough.

### What You See (TX)

```
TX video (live, encoder=sw): v4l2:/dev/video0 -> 640x480@1.40Mbps -> QPSK_1/2 short @ 2.000 MSym/s
  [TX 00:00] tx 1477 PLFRM/s  1.728 Mbps  MODCOD QPSK_1/2  2.00 MSym/s
  [TX 00:01] tx 1480 PLFRM/s  1.731 Mbps  MODCOD QPSK_1/2  2.00 MSym/s
```

### What You See (RX with display)

```
RX video -> display: QPSK_1/2 short @ 2.000 MSym/s  (live stats below)
  RX live stats (QPSK_1/2 short 2.000 MSym/s) -- ticks every second, elapsed | state:
  [00:00] QPSK_1/2 short | VIDEO  1.730 Mbps | cont-err/s 0
  [00:01] QPSK_1/2 short | VIDEO  1.730 Mbps | cont-err/s 0
```

ffplay window opens showing the camera feed with ~770 ms latency.

### What You See (RX with full stats)

```bash
dvbs2_wf_TR rx video_local --full-stats
```

```
RX video -> display (FULL STATS): QPSK_1/2 short @ 2.000 MSym/s
  RX FULL STATS [QPSK_1/2 short 2.000 MSym/s] -- elapsed | demod | SNR(Es/N0) | CFO | LDPC iters | FER | TS:
  [RX 00:00] QPSK_1/2 short | DEMOD | SNR(Es/N0) 24.5 dB | CFO +0.0 kHz | LDPC 1.2 it/frm | FER  0.00%/s | 1480 frm/s, 10478 TS pkt/s
  [RX 00:01] QPSK_1/2 short | DEMOD | SNR(Es/N0) 24.5 dB | CFO +0.0 kHz | LDPC 1.2 it/frm | FER  0.00%/s | 1480 frm/s, 10478 TS pkt/s
```

- **SNR**: 24.5 dB (cabled, QPSK 1/2 requires ~2.5 dB for quasi-error-free)
- **CFO**: ~0 Hz (cabled TCXO)
- **LDPC iters**: 1.2 (very easy decode — no retries)
- **FER**: 0.00%

---

## Demo 3: Full Duplex (One B210, TX+RX Simultaneously)

Single B210 transmits and receives at once using two different frequencies.

### Config Setup

Edit `config/video_local.toml` (or create a `duplex_video.toml`):

```toml
[tx]
freq_hz = 1.282e9

[rx]
freq_hz = 1.282e9 + 100e6    # 100 MHz separation
```

Duplex uses a single `GrFullDuplex` flowgraph (`gr_fullduplex.py`) that holds
both `uhd.usrp_sink` and `uhd.usrp_source` on the same B210 serial.

### Command

```bash
dvbs2_wf_TR duplex video_local
```

### Two-Column Stats

```
  [00:00] TX 1480 PLFRM/s  1.73 Mbps  |  RX QPSK_1/2 short | DEMOD | SNR 22.1 dB | LDPC 1.5 it/frm | FER 0.00%
  [00:01] TX 1480 PLFRM/s  1.73 Mbps  |  RX QPSK_1/2 short | DEMOD | SNR 22.1 dB | LDPC 1.5 it/frm | FER 0.00%
```

### Loopback (Full Duplex, No Radio)

Most reliable way to validate the entire duplex code path:

```bash
dvbs2_wf_TR duplex raw_wo_udp --loopback --secs 10
```

---

## Demo 4: MODCOD / Symbol Rate Sweep

Demonstrate link margin at different MODCODs. Useful for OTA range testing.

### Prerequisites

Two cables/antennas with known attenuation, or OTA path loss estimate.

### Procedure

1. **QPSK 1/2** (most robust, lowest throughput)

```bash
# Edit config: set [tx] modcod = "QPSK_1/2", symbol_rate = 2.0e6
dvbs2_wf_TR tx video_local --tx-gain 10
dvbs2_wf_TR rx video_local --full-stats
# Expected: SNR 24.5 dB (cabled), FER 0.00%
```

2. **QPSK 3/4** (higher rate, same constellation)

```bash
# Edit config: [tx] modcod = "QPSK_3/4"
dvbs2_wf_TR tx video_local --tx-gain 10
# Expected: SNR 24.5 dB, FER 0.00%, net rate ~2.88 Mbps
```

3. **8PSK 3/5** (3 bits/symbol)

```bash
# Edit config: [tx] modcod = "8PSK_3/5"
# May need slightly higher TX gain or less attenuation
# Expected: SNR 24.5 dB, FER 0.00%, net rate ~2.35 Mbps (same symrate,
# more bits/symbol but lower code rate)
```

4. **16APSK 2/3** (extended engine — uses slower C++ LDPC decoder)

```bash
# Edit config: [tx] modcod = "16APSK_2/3"
# WARNING: extended engine latency ~1.5-2 s
# Set [tx] symbol_rate = 1.54e6 (lower to avoid USB overflow)
```

### Symbol Rate Sweep

At a fixed MODCOD (e.g. QPSK 1/2), sweep symbol rate:

| Sym Rate | Net Bps | Latency | USB Load | Notes |
|----------|---------|---------|----------|-------|
| 500 kSym/s | ~0.43 Mbps | ~3 s | Very low | Worst latency (longest PLFRAME fill time) |
| 1.0 MSym/s | ~0.86 Mbps | ~1.5 s | Low | |
| 2.0 MSym/s | ~1.73 Mbps | ~770 ms | Moderate | Sweet spot for sub-second latency |
| 4.0 MSym/s | ~3.46 Mbps | ~400 ms | High | May cause USB overflow on Jetson |

Change `symbol_rate` in the config, or set it high and use `--tx-gain` to
simulate range loss:

```bash
dvbs2_wf_TR tx video_local --tx-gain 50    # nominal
dvbs2_wf_TR tx video_local --tx-gain 30    # -20 dB (simulate range)
dvbs2_wf_TR tx video_local --tx-gain 10    # -40 dB (near loss of lock)
```

---

## Demo 5: Raw Data UDP Test (Two Stations)

Transmit application data (not video) over the DVB-S2 link using
`raw_w_udp` profile.

### Station A (TX with UDP injection)

```bash
# TX listens on local UDP port 5005; each datagram = one frame payload
dvbs2_wf_TR tx raw_w_udp --raw --raw-udp-in 5005
```

### Station B (RX with UDP forward)

```bash
# RX forwards each recovered payload to VIEWER_IP:5006
dvbs2_wf_TR rx raw_w_udp --raw --raw-udp-out 192.168.0.100:5006
```

### Inject Data on Station A

From another terminal on Station A:

```bash
# Inject text payloads (they'll appear on the viewer)
echo "Hello DVB-S2!" | nc -u 127.0.0.1 5005
```

### Viewer on Laptop

```bash
nc -lu 5006
# You should see "Hello DVB-S2!" with ~770 ms delay
```

### Raw Frame Console

Both stations show per-second stats:

```
TX raw-data: QPSK_1/2 short @ 2.000 MSym/s (net 1.73 Mbps)  | UDP-in :5005
RX raw-data readout: QPSK_1/2 short @ 2.000 MSym/s  | UDP-out 192.168.0.100:5006

  [    10s] rx   719 fr/s PSR 100.0% |  1.150 Mbps | loss/s 0 cum 0 | lat 765/789/812 ms
```

---

## Demo 6: GNU Radio Compatibility

Demonstrate that the dvbs2_wf_TR TX can be received by gr-dvbs2rx (or vice
versa). Useful for interop with standard GNU Radio tools.

### TX from dvbs2_wf_TR, RX on GNU Radio

```bash
# Station A (TX): transmit with dvbs2_wf_TR
dvbs2_wf_TR tx video_local
```

On the GNU Radio host (or second Jetson running GNU Radio):

```bash
# Use gr-dvbs2rx's dvbs2-rx directly:
/usr/local/lib/.engine/bin/dvbs2-rx \
  -m QPSK1/2 -s 2000000 --sps 4 \
  --frame-size short --source usrp \
  --usrp-args "type=b200,serial=8000880" \
  --usrp-gain 40 -f 1.282e9 \
  --sink file --out-file /tmp/rx.ts

# Play the captured TS:
ffplay -fflags nobuffer -flags low_delay -i /tmp/rx.ts
```

### TX from GNU Radio, RX on dvbs2_wf_TR

```bash
# Transmit using gr-dvbs2tx:
/usr/local/lib/.engine/bin/dvbs2-tx \
  -m QPSK1/2 -s 2000000 --sps 2 \
  --frame-size short --source file \
  --in-file /opt/dvbs2_wf_TR/media/sample.mp4 \
  --sink usrp --usrp-args "type=b200,serial=8000693" \
  --usrp-gain 70 -f 1.282e9 --pilots

# On the dvbs2_wf_TR station:
dvbs2_wf_TR rx video_local --full-stats
```

---

## Troubleshooting

### "Option buffer_size not found" / Receiver crash

Removed in commit `d360c1b`. Ensure you're running binary built after Jun 28 2025.

### RF overload (two B210s on same desk)

```bash
dvbs2_wf_TR tx video_local --tx-gain 10
```

Or add 20-30 dB inline attenuation. At 70 dB TX gain, a nearby B210's RX
front-end saturates (LNA compresses, ADC clips, decoder sees garbage).

### USB overflow (OOOO in radio log)

```bash
# Halve USB wire bytes:
edit config: [tx] oversample=2, [rx] oversample=4 -> [node] samp_type = "sc8"
# Or drop RX oversample:
edit config: [rx] oversample=2 (also halve lo_offset_hz)
# Or lower symbol rate:
edit config: [rx] symbol_rate = 1.54e6
# Or run:
sudo jetson_clocks
```

### No radio detected

```bash
uhd_find_devices
uhd_usrp_probe
pkill -9 -f dvbs2    # kill stale processes holding the USB device
```

### No display (headless Jetson)

```bash
# Option 1: X forwarding over SSH
ssh -Y wftest@192.168.0.41 dvbs2_wf_TR rx video_local

# Option 2: sink to UDP (view on laptop)
dvbs2_wf_TR rx video_remote    # sends TS to laptop UDP
# On laptop:
nc -lu 1234 | ffplay -i -
```

### LDPC stuck at 25 iters, FER ~100%, zero TS

MODCOD mismatch between TX and RX. Check configs match, or run RX with
`--full-stats` to see the MODCOD detection:

```bash
dvbs2_wf_TR rx video_local --full-stats
# Look for "MODCOD mismatch suspected" message
```

### Engine path errors

```bash
# Verify engine exists:
ls /usr/local/lib/.engine/bin/python3
# Set env var override:
DVBS2_ENV=/path/to/engine dvbs2_wf_TR check
```

---

## Quick Reference

| Command | What It Does |
|---------|-------------|
| `dvbs2_wf_TR check` | Preflight all components |
| `dvbs2_wf_TR tx video_local` | TX camera → local display |
| `dvbs2_wf_TR rx video_local` | RX from radio → local display |
| `dvbs2_wf_TR tx video_local --tx-gain 10` | TX at reduced power |
| `dvbs2_wf_TR rx video_local --full-stats` | RX with SNR/CFO/LDPC/FER |
| `dvbs2_wf_TR bench raw_wo_udp --secs 30` | 30 s loopback self-test |
| `dvbs2_wf_TR duplex video_local` | Full duplex on one B210 |
| `dvbs2_wf_TR duplex raw_wo_udp --loopback` | Duplex loopback self-test |
| `dvbs2_wf_TR rates video_local` | Print calculated rates |
| `dvbs2_wf_TR tx raw_w_udp --raw-udp-in 5005` | TX with UDP data injection |
| `dvbs2_wf_TR rx raw_w_udp --raw-udp-out HOST:5006` | RX forwarding to UDP |
