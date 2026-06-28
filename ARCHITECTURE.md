# dvbs2_wf_TR — Architecture

## Overview

`dvbs2_wf_TR` is a **pipe-based orchestrator**, not a monolithic modem. The
binary (`dvbs2_TR`, PyInstaller-frozen) parses TOML config, spawns external
subprocesses (ffmpeg, GNU Radio `dvbs2-tx`, `dvbs2-rx`, helper scripts), and
monitors per-second stats. All heavy signal processing — BBFRAME→BCH→LDPC→
PLFRAME→RRC→USRP on TX, and USRP→AGC→rotator→symbol sync→PL sync→demapper→
LDPC→BCH→BBdeheader on RX — happens inside GNU Radio C++ executables
(`dvbs2-tx`, `dvbs2-rx`) or the `GrFullDuplex` flowgraph (`gr_fullduplex.py`).

---

## High-Level Block Diagram

```
  ┌──────────┐   MPEG-TS    ┌───────────┐   I/Q      ┌──────┐  RF   ┌──────┐
  │ Source   │──────────────▶│ dvbs2-tx  │───────────▶│ B210 │──────▶│ Air  │
  │ (ffmpeg/ │               │ (modulate)│            │ (TX) │      └──────┘
  │  rawdata │               └───────────┘            └──────┘
  │  _gen)   │                                                     ┌──────┐
  └──────────┘   MPEG-TS    ┌───────────┐   I/Q      ┌──────┐  RF  │      │
  ┌──────────┐◄──────────────│ dvbs2-rx  │◄──────────◀│ B210 │◄─────│ Air  │
  │ Sink     │               │ (demod)   │            │ (RX) │      └──────┘
  │ (ffplay/ │               └───────────┘            └──────┘
  │  rawdata │
  │  _rx)    │
  └──────────┘
        ▲
        │ JSON (--full-stats)
  ┌─────┴─────┐
  │ Monitor   │  poll_full_stats() → per-second DEMOD line
  └───────────┘
```

In **duplex** mode (`--duplex`), both TX and RX run in a single
`gr_fullduplex.py` process that holds one `uhd.usrp_sink` + one
`uhd.usrp_source` (sharing the same B210 serial), with a separate monitor
thread polling `gr_fullduplex.get_stats()` over HTTP.

In **loopback** mode (`--loopback`), the USRP is replaced by a 1:1
`blocks.copy(gr_complex)` bridge — TX complex samples feed directly into the
RX chain in memory.

---

## TX Pipeline (video)

```
[v4l2 camera]──┬──ffmpeg: v4l2→scale→libx264→mpegts──┐
[file source]──┘                                      │
                                                      │
                                                      ▼
                                             dvbs2-tx (GNU Radio app)
                                               ┌─────────────────┐
                                               │ BBHeader (MODE) │
                                               │ BBScrambler     │
                                               │ BCH Encoder     │
                                               │ LDPC Encoder    │
                                               │ Interleaver     │
                                               │ Modulator (map) │
                                               │ PL Framer       │
                                               │ RRC (MF)        │
                                               │ USRP Sink       │
                                               └────────┬────────┘
                                                        │ I/Q (fc32)
                                                        ▼
                                                   B210 TX
```

### TX Blocks

| Step | Block | Function |
|------|-------|----------|
| 1 | ffmpeg / rawdata_gen | Produce CBR MPEG-TS at the MODCOD net bitrate. ffmpeg: `-b:v RATE -maxrate RATE -bufsize RATE/2 -nal-hrd=cbr`. rawdata_gen: null-packet stuffing at `ratio = app_rate / net_rate`. |
| 2 | BBHeader | Wraps MPEG-TS into BBFRAMEs: add baseband header, null-packet deletion/insertion for rate matching. |
| 3 | BBScrambler | Energy-dispersal scrambling (PRBS). |
| 4 | BCH Encoder | Outer FEC: BCH(65535, 65343, 12) for normal, BCH(16200, 15888, 12) for short. Adds parity. |
| 5 | LDPC Encoder | Inner FEC: DVB‑S2 LDPC codes (rates 1/4…9/10). Frame sizes 16200 (short) or 64800 (normal). Fixed code rate, no rate-matching. |
| 6 | Interleaver | Bit-interleaver for 8PSK/16APSK/32APSK (not used for QPSK). |
| 7 | Modulator | Constellation mapping: QPSK, 8PSK, 16APSK, 32APSK. Output is complex symbols. |
| 8 | PL Framer | Frame formatting: PL header (SOF + PLS), pilots every 16 slots (optional), slot interleaving. |
| 9 | RRC Filter | Root-raised cosine pulse shaping at `rolloff × sps`. Delay modeled (default 50 symbols) to keep ISI negligible. |
| 10 | USRP Sink | Streams complex I/Q (fc32 → sc16/sc8) to B210 over USB 3.0. Stream args cap host buffers. |

net_ts_bps = `KBCH × sym_rate / PLFRAME_symbols`

**Example**: QPSK 1/2 short frame, 2 MSym/s, pilots on → `7032 × 2e6 / (8100 + 26) ≈ 1.73 Mbps net`.

---

## TX Pipeline (raw data)

```
[UDP injector]──┬──rawdata_gen.py──┬──┐
[synthetic gen]─┘                 │  │
                                  ▼  ▼
                              dvbs2-tx  (same blocks as video TX)
                                  │
                                  ▼
                               B210 TX
```

`rawdata_gen.py` produces MPEG-TS carrying sequenced, timestamped data frames:

```
 MAGIC(4) | SEQ(8) | TX_ts_ns(8) | LEN(4) | payload[LEN]
```

- CBR pacing via null-packet stuffing: `ratio = dpkts_per_frame × frame_rate / net_pps`.
- TX timestamp is the `time.time_ns()` at frame emission → accurate end-to-end latency.
- `--payload-text` mode: payload = `"<text> : <SEQ>"` so the operator watches the counter increment.
- `--udp-in PORT` mode: each incoming UDP datagram is one frame payload (NUL-padded/truncated to plen).

---

## RX Pipeline (video)

```
B210 RX ──▶ dvbs2-rx (GNU Radio app) ──▶ ts_tee_stats ──▶ ffplay/ffmpeg/UDP
             ┌──────────────────┐
             │ USRP Source       │  (sc16/sc8, LO offset)
             │ AGC               │  (rate=1e-5, ref=1.0)
             │ Rotator           │  (fine freq offset, msg-driven)
             │ Symbol Sync       │  (Gardner TED + polyphase RRC)
             │ PL Sync           │  (SOF detect, PLS decode, freq est)
             │ XFECFRAME Demapper│  (soft LLR demapping)
             │ LDPC Decoder      │  (iterative, max 25 iters)
             │ BCH Decoder       │  (outer FEC, error correction)
             │ BB Descrambler    │  (energy dispersal removal)
             │ BB Deheader       │  (TS packet recovery)
             └────────┬─────────┘
                      │ MPEG-TS
                      ▼
```

### RX Blocks

| Step | Block | Function |
|------|-------|----------|
| 1 | USRP Source | Stream complex I/Q from B210. LO offset places DC/LO leakage outside signal BW. |
| 2 | AGC | Automatic gain control. Sets signal level for downstream blocks. |
| 3 | Rotator | Fine carrier recovery. Phase increment driven by PL Sync's residual CFO estimate. |
| 4 | Symbol Sync | Timing recovery: Gardner TED with polyphase RRC interpolator. Locks to symbol clock. |
| 5 | PL Sync | Physical-layer frame detection: SOF correlation (90°-ambiguous), PLS decoding (MODCOD + frame size), coarse CFO estimation. |
| 6 | XFECFRAME Demapper | Soft-in soft-out demapper: converts complex symbols to LLRs for LDPC. LLR feedback from decoder for SNR estimation. |
| 7 | LDPC Decoder | Iterative min-sum decoder. `avg_ldpc_trials` reported in stats. Default max 25 iterations. |
| 8 | BCH Decoder | Outer code: corrects residual errors after LDPC. `frame_count` and `error_count` for FER. |
| 9 | BB Descrambler | Removes energy dispersal (PRBS XOR). |
| 10 | BB Deheader | Strips BBHeader → recovers MPEG-TS packets. `packet_count` / `error_count` for PER. |

### Sink Types

- **display**: MPEG-TS → `ts_smooth.py` (50 ms jitter buffer, meters output at net bitrate) → `ffplay -fflags nobuffer -flags low_delay -i -`
- **udp://host:port**: MPEG-TS → `ffmpeg -c copy -f mpegts udp://host:port?pkt_size=1316`
- **file:path**: Raw TS capture to file

`ts_tee_stats.py` sits between demod and sink: passes TS bytes through unchanged, prints per-second `VIDEO` / `NO VIDEO` line with bitrate and continuity errors.

---

## RX Pipeline (raw data)

```
B210 RX ──▶ dvbs2-rx ──▶ rawdata_rx.py ──▶ [UDP forwarder / console]
```

`rawdata_rx.py`:
1. Reads MPEG-TS from UDP port 5005 (default), stdin (`--stdin`), or a file descriptor (`--infd`).
2. Aligns to 0x47 sync byte, filters DATA PID, reassembles frames.
3. Validates: MAGIC, SEQ continuity, payload pattern (synthetic or `--payload-text`).
4. Prints per-second stats: frames/sec, PSR %, throughput Mbps, loss/s + cumulative, latency (min/avg/max).
5. Optional `--udp-out host:port`: forwards each recovered payload as one UDP datagram (NUL-stripped).

---

## Duplex Mode

`GrFullDuplex` (`gr_fullduplex.py`) combines both chains in one Python
flowgraph. Key design:

- **Single B210**: One `uhd.usrp_sink` + one `uhd.usrp_source` sharing the same
  serial → UHD caches the motherboard, both blocks use the same device.
- **Independent frequencies**: RX = TX + `rx_offset_hz` (default 100 kHz) to
  avoid LO leakage / DC at the exact carrier.
- **Buffer capping**: `set_max_output_buffer(4 × output_multiple())` on every
  TX/RX block → ~200-300 ms end-to-end vs ~4 s with GNURadio defaults.
- **Stats**: `get_stats()` returns lock, SNR, CFO, SOF/LDPC/BCH/TS counters
  for the `--full-stats` monitor.

---

## Loopback Mode

`--loopback` replaces `uhd.usrp_sink` and `uhd.usrp_source` with a single
`blocks.copy(gr_complex)` — TX I/Q flows directly into the RX chain at
infinite SNR. Used for software pipeline verification without RF hardware.

---

## Config Profiles

| Profile | Mode | Source | Sink | Use Case |
|---------|------|--------|------|----------|
| `video_local.toml` | local display | v4l2 camera | ffplay | Local monitoring |
| `video_remote.toml` | remote stream | file source | UDP | Streaming media to viewer |
| `raw_wo_udp.toml` | self-test | synthetic | rawdata_rx | Bench / loopback |
| `raw_w_udp.toml` | data relay | UDP in | UDP out | External data over satellite |

The old `video.toml` (v4l2 → remote UDP) exists but is **not used** by the
wrapper (default is now `video_local`).

---

## Deployment Layout

```
/opt/dvbs2_wf_TR/        (driver .deb install)
├── bin/dvbs2_wf_TR      PyInstaller binary
├── config/*.toml         5 profile configs
├── media/sample.mp4      Sample video
└── dvbs2_wf_TR.sh        Unified wrapper

/usr/local/lib/.engine/   (engine .deb install — 1.3 GB)
├── bin/                   python3, dvbs2-tx, dvbs2-rx, ffmpeg
├── lib/                   *.so (gr_python, dvbs2rx_python, uhd_python)
└── share/uhd/images/     B210 FPGA firmware
```

---

## Key Principles

1. **Orchestration, not signal processing**: `dvbs2_TR` is a pipeline
   orchestrator (config → subprocess → monitor). All radio DSP lives in
   GNU Radio C++.
2. **CBR MPEG-TS everywhere**: The pipe between source and modulator, and
   between demod and sink, is always a constant-bitrate MPEG-TS byte stream.
   The modulator expects TS at exactly the MODCOD net bitrate; the demod emits
   TS at the same rate.
3. **Buffer bounding**: Every block's internal buffer is capped at 4× its
   processing granularity. UHD host buffers are set to 2 MB each direction.
   This is the single most important latency optimization.
4. **One process per direction**: TX and RX are separate processes (or a single
   `gr_fullduplex.py` in duplex mode). They communicate only through the B210.
5. **Stats always tick**: `poll_full_stats()` (demod JSON) and `ts_tee_stats.py`
   (TS bytes) use `select()` with a 1 s timeout so the per-second line is
   printed every second even when the signal is lost — never a frozen console.

---

## Latency Budget

| Stage | Contribution (QPSK 1/2, short, 2 MSym/s) |
|-------|-------------------------------------------|
| Encoder (libx264 ultrafast) | ~30 ms |
| TX pipeline (5 blocks × 4 frames) | ~320 ms |
| UHD TX buffer (2 MB @ 16 MB/s) | ~125 ms |
| Air / cable propagation | <1 ms |
| UHD RX buffer (2 MB @ 16 MB/s) | ~125 ms |
| RX pipeline (symbol sync + PL sync + LDPC + BCH) | ~170 ms |
| Decoder (ffplay nobuffer) | ~10 ms |
| **Total** | **~770 ms** |

Normal frame (64800) multiplies the LDPC decoding latency ~4× → ~2.7 s total.
