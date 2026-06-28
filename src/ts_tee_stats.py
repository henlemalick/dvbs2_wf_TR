#!/usr/bin/env python3
# =============================================================================
# ts_tee_stats.py - pass an MPEG-TS byte stream stdin->stdout UNCHANGED while
# printing a per-second link line to STDERR. Uses select() so the line ALWAYS
# ticks every second (with elapsed time) even when the signal drops and no TS
# flows -> you see "no signal" continuously, not a frozen console.
#   ... | ts_tee_stats.py [--pid auto|0x100|...] --modcod QPSK1/2 --frame short
#                         --symrate 2e6
# `--pid auto` (the default) discovers the video PID by parsing the PAT then
# the PMT, so the tee works regardless of which encoder is upstream: ffmpeg's
# mpegtsmux puts video on 0x100, GStreamer's puts it on 0x41 (or wherever
# the program-number sequence lands). Until a PID is locked, the per-second
# line stays "searching"; once locked, "[pid=0xNN]" is appended once for the
# operator to see what got picked.
# =============================================================================
import sys, os, time, select, argparse
ap = argparse.ArgumentParser()
ap.add_argument("--pid", default="auto",
                help="video TS PID. 'auto' (default) discovers from PMT; "
                     "or pass an explicit 0xNN to force.")
ap.add_argument("--modcod", default="?"); ap.add_argument("--frame", default="")
ap.add_argument("--symrate", default="0")
a = ap.parse_args()
PID = None if a.pid.lower() == "auto" else int(a.pid, 16)
AUTO = PID is None
try: SR = float(a.symrate)
except ValueError: SR = 0.0
INFD, OUTFD = 0, 1
buf = bytearray(); last_cc = None
vbytes = 0; cc_err = 0; tl = time.time(); t0 = tl
PMT_PIDS = set()                    # PMT PIDs discovered from PAT
ANNOUNCED = False                   # only print [pid=0xNN] once

# Video stream types that map to a recoverable video ES:
#   0x01 MPEG-1 video, 0x02 MPEG-2 video, 0x10 MPEG-4 part 2,
#   0x1B H.264 AVC, 0x24 H.265 HEVC, 0x42 CAVS.
VID_STREAM_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x42}

def parse_pat(payload):
    """PAT body -> set of PMT PIDs. Returns empty if the section isn't yet
    complete. Skips program_number==0 (NIT)."""
    if len(payload) < 8: return set()
    pointer = payload[0]; sec = payload[1+pointer:]
    if not sec or sec[0] != 0x00: return set()
    section_length = ((sec[1] & 0x0f) << 8) | sec[2]
    end = 3 + section_length - 4    # last 4 bytes are CRC32
    if end > len(sec): return set()
    pmts = set()
    i = 8
    while i + 4 <= end:
        prog = (sec[i] << 8) | sec[i+1]
        pid  = ((sec[i+2] & 0x1f) << 8) | sec[i+3]
        if prog != 0: pmts.add(pid)
        i += 4
    return pmts

def parse_pmt_for_video(payload):
    """PMT body -> first elementary PID with a video stream_type, or None."""
    if len(payload) < 12: return None
    pointer = payload[0]; sec = payload[1+pointer:]
    if not sec or sec[0] != 0x02: return None
    section_length = ((sec[1] & 0x0f) << 8) | sec[2]
    end = 3 + section_length - 4
    if end > len(sec): return None
    program_info_length = ((sec[10] & 0x0f) << 8) | sec[11]
    i = 12 + program_info_length
    while i + 5 <= end:
        st_type = sec[i]
        es_pid = ((sec[i+1] & 0x1f) << 8) | sec[i+2]
        es_info_length = ((sec[i+3] & 0x0f) << 8) | sec[i+4]
        if st_type in VID_STREAM_TYPES:
            return es_pid
        i += 5 + es_info_length
    return None

def el(t): s = int(t - t0); return f"{s//60:02d}:{s%60:02d}"
def emit(s): sys.stderr.write(s + "\n"); sys.stderr.flush()
ctx = f"{a.modcod} {a.frame} {SR/1e6:.3f} MSym/s".replace("  ", " ")
emit(f"  RX live stats ({ctx}) -- ticks every second, elapsed | state:")
try:
    while True:
        r, _, _ = select.select([INFD], [], [], 1.0)
        if r:
            d = os.read(INFD, 65536)
            if not d: break                 # EOF: TX/flowgraph ended
            os.write(OUTFD, d); buf += d
            while len(buf) >= 188:
                if buf[0] != 0x47:
                    k = buf.find(b"\x47", 1)
                    if k < 0: buf.clear(); break
                    del buf[:k]; continue
                pkt = buf[:188]; del buf[:188]
                pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
                cc = pkt[3] & 0x0f; afc = (pkt[3] >> 4) & 3
                pusi = (pkt[1] >> 6) & 1
                if AUTO and PID is None:
                    # Discover PAT (PID 0) -> PMT PIDs, then PMT -> video PID.
                    payload_start = 4
                    if afc & 2:
                        payload_start += 1 + pkt[4]
                    payload = pkt[payload_start:]
                    if pid == 0 and pusi and (afc & 1):
                        PMT_PIDS |= parse_pat(payload)
                    elif pid in PMT_PIDS and pusi and (afc & 1):
                        vp = parse_pmt_for_video(payload)
                        if vp is not None:
                            PID = vp
                            if not ANNOUNCED:
                                emit(f"  [tee] video PID auto-detected: 0x{PID:x} ({PID})")
                                ANNOUNCED = True
                if PID is not None and pid == PID and afc in (1, 3):
                    vbytes += 184
                    if last_cc is not None and cc != ((last_cc + 1) & 0x0f): cc_err += 1
                    last_cc = cc
        now = time.time()
        if now - tl >= 1.0:
            mbps = vbytes * 8 / (now - tl) / 1e6
            pid_tag = f" [pid=0x{PID:x}]" if (PID is not None and not ANNOUNCED) else ""
            if mbps > 0.05:
                # 'VIDEO' (was 'LOCKED') — names the layer: media bytes are
                # flowing through the TS deframer. Distinct from the demod's
                # green 'DEMOD' tag, which is the physical-layer concept.
                st = f"\033[1;32mVIDEO\033[0m {mbps:6.3f} Mbps | cont-err/s {cc_err}"
            elif AUTO and PID is None:
                st = "\033[1;31mNO VIDEO\033[0m (searching for PMT)"
            else:
                st = "\033[1;31mNO VIDEO\033[0m (searching)"
            emit(f"  [{el(now)}] {a.modcod} {a.frame} | {st}{pid_tag}")
            vbytes = 0; cc_err = 0; tl = now
except (BrokenPipeError, KeyboardInterrupt):
    pass
