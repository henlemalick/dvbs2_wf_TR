#!/usr/bin/env python3
# =============================================================================
# rawdata_gen.py - generate a CONSTANT-rate MPEG-TS stream carrying sequenced,
# timestamped raw-data frames, for transmission over DVB-S2 (pipe -> grdtv_tx.py ts=-).
#
# The stream is emitted at the MODCOD NET rate (CBR) so the modulator never
# underruns: DATA-PID frames are interleaved at the target data rate and the
# rest is filled with NULL (0x1FFF) packets. Each application frame:
#   MAGIC(4) | SEQ(8) | TX_ts_ns(8) | LEN(4) | payload[LEN]
# TX timestamp is stamped as the frame is emitted -> accurate end-to-end latency.
#
# Payload (new): if --payload-text is given, each frame's payload is
#       "<text> : <SEQ>"     padded with 0x00 to plen if shorter; truncated if
#                            longer. RX-side decodes and prints it (so the
#                            operator literally watches the counter increment).
# When --payload-text is missing the legacy synthetic pattern is used:
#       payload[i] = (SEQ + i) & 0xff
#
# Usage: rawdata_gen.py --plen N --datarate BPS --netrate BPS [--pid 0x100]
#                      [--payload-text "..."]
# =============================================================================
import sys, os, time, struct, argparse, threading, socket
from collections import deque
ap = argparse.ArgumentParser()
ap.add_argument("--plen", type=int, default=200)
ap.add_argument("--datarate", type=float, default=300000)   # payload bits/s
ap.add_argument("--netrate", type=float, default=850000)    # MODCOD net bits/s (CBR out)
ap.add_argument("--pid", default="0x100")
ap.add_argument("--payload-text", dest="payload_text", default="",
                help="if set, frame payload is '<text> : <SEQ>' padded to plen "
                     "with NUL. RX-side decodes and prints. When empty, the "
                     "legacy synthetic byte pattern is generated.")
ap.add_argument("--udp-in", dest="udp_in", type=int, default=0,
                help="if >0, listen on 0.0.0.0:PORT and copy each received "
                     "UDP datagram into ONE frame payload (truncated/NUL-padded "
                     "to --plen). Overrides --payload-text and the legacy "
                     "synthetic pattern. When no datagram is waiting, an all-zero "
                     "payload is sent so CBR + seq stay deterministic.")
a = ap.parse_args()

UDP_Q = deque(); UDP_LOCK = threading.Lock(); UDP_MAX = 256
def _udp_in_listener(port, plen):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    sys.stderr.write("rawdata_gen: --udp-in listening on 0.0.0.0:%d\n" % port); sys.stderr.flush()
    while True:
        try: d, _ = s.recvfrom(65536)
        except Exception: continue
        if not d: continue
        body = d[:plen] if len(d) >= plen else d + b"\x00" * (plen - len(d))
        with UDP_LOCK:
            UDP_Q.append(body)
            while len(UDP_Q) > UDP_MAX: UDP_Q.popleft()
if a.udp_in > 0:
    threading.Thread(target=_udp_in_listener, args=(a.udp_in, a.plen), daemon=True).start()
PID = int(a.pid, 16); MAGIC = 0xA5A5C3C3
HDR = 4 + 8 + 8 + 4; FRAME = HDR + a.plen
PKT = 188; PKT_BITS = PKT * 8
net_pps = a.netrate / PKT_BITS
dpkts_per_frame = (FRAME + 183) // 184
frame_per_s = a.datarate / (FRAME * 8)
ratio = (frame_per_s * dpkts_per_frame) / net_pps          # fraction of slots that are data
NULL = bytes([0x47, 0x1f, 0xff, 0x10]) + b"\xff" * 184
out = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)
try:    # F_SETPIPE_SZ=1031: shrink the gen->grdtv pipe to cut latency. Keep it >=
        # one write burst (CHUNK*188) so writes aren't fragmented. 32K default here.
    import fcntl; fcntl.fcntl(sys.stdout.fileno(), 1031, int(os.environ.get("PIPE_SZ", "32768")))
except Exception: pass

PAYLOAD_TXT = a.payload_text.encode("utf-8", errors="replace") if a.payload_text else b""

sys.stderr.write("rawdata_gen: plen=%d frame=%dB datarate=%.0f netrate=%.0f -> %.1f frame/s, "
                 "net %.0f pkt/s, data-frac %.2f%s\n"
                 % (a.plen, FRAME, a.datarate, a.netrate, frame_per_s, net_pps, ratio,
                    f", payload-text=\"{a.payload_text}\"" if a.payload_text else "")); sys.stderr.flush()

cc = 0
ZERO_PAYLOAD = b"\x00" * a.plen
def build_payload(seq):
    """Build the frame's user payload of exactly a.plen bytes."""
    if a.udp_in > 0:
        with UDP_LOCK:
            if UDP_Q:
                return UDP_Q.popleft()
        return ZERO_PAYLOAD
    if PAYLOAD_TXT:
        body = PAYLOAD_TXT + b" : " + str(seq).encode("ascii")
        if len(body) >= a.plen:
            return body[:a.plen]
        return body + b"\x00" * (a.plen - len(body))
    return bytes((seq + i) & 0xff for i in range(a.plen))

def packetize_frame(seq):
    global cc
    txts = time.time_ns()
    payload = build_payload(seq)
    data = struct.pack(">IQQI", MAGIC, seq, txts, a.plen) + payload
    pkts = deque()
    for off in range(0, len(data), 184):
        chunk = data[off:off+184]
        if len(chunk) < 184: chunk += b"\xff" * (184 - len(chunk))
        pkts.append(bytes([0x47, (PID >> 8) & 0x1f, PID & 0xff, 0x10 | (cc & 0x0f)]) + chunk)
        cc = (cc + 1) & 0x0f
    return pkts

# No explicit pacing: gr-dtv consumes at the MODCOD net rate and backpressures
# this writer via the pipe, so we never outrun (no growing latency) and never
# starve the modulator (no underflows). data/null interleave sets the data rate.
CHUNK = 64
seq = 0; acc = 0.0; q = deque()
try:
    while True:
        burst = bytearray()
        for _ in range(CHUNK):
            acc += ratio
            if acc >= 1.0:
                if not q:
                    q = packetize_frame(seq); seq += 1   # txts stamped at emit time
                burst += q.popleft(); acc -= 1.0
            else:
                burst += NULL
        out.write(burst)     # blocks on gr-dtv backpressure -> paced at net rate
except (BrokenPipeError, KeyboardInterrupt):
    pass
