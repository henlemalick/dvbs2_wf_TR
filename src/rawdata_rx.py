#!/usr/bin/env python3
# =============================================================================
# rawdata_rx.py - raw-data link receiver + professional per-second readout.
#
# Reads the recovered MPEG-TS from SDRangel DATVDemod (udpTS, default :5005),
# reassembles the DATA PID payload into the sequenced frames produced by
# rawdata_gen.py:  MAGIC(4) | SEQ(8) | TX_ts_ns(8) | LEN(4) | payload[LEN].
# Verifies the SEQ counter (continuity / loss), validates the payload pattern,
# and measures end-to-end latency (TX and RX share the host clock).
#
# Output (modeled on the wf-h_satcom / ts_link_stats readout):
#   - a header: MODCOD, symbol rate, mode, freq, payload length, target rate
#   - a live counter line per received frame (preview of payload) [--verbose]
#   - a per-second stats line: uptime | frames rx/exp | PSR% | throughput
#       | loss/s + cumulative | latency min/avg/max ms
#
# Usage: rawdata_rx.py [--port 5005] [--pid 0x100] [--rate BPS] [--plen N]
#                      [--modcod STR] [--symrate HZ] [--freq HZ] [--mode STR]
#                      [--verbose] [--every N]
# =============================================================================
import sys, os, socket, struct, time, select, argparse

MAGIC = 0xA5A5C3C3
HDR = 4 + 8 + 8 + 4
G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"; Dim="\033[2m"; B="\033[1m"; X="\033[0m"

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=5005)
ap.add_argument("--pid", default="0x100")
ap.add_argument("--rate", type=float, default=0)      # target app bits/s (for expected/PSR)
ap.add_argument("--plen", type=int, default=0)
ap.add_argument("--modcod", default="?"); ap.add_argument("--symrate", default="?")
ap.add_argument("--freq", default="?"); ap.add_argument("--mode", default="DVB-S2 raw-data")
ap.add_argument("--verbose", action="store_true",
                help="show every frame's payload (--full-stats in dvbs2_TR raw mode)")
ap.add_argument("--every", type=int, default=0,
                help="show one payload line per N frames; 0=off (default)")
ap.add_argument("--stdin", action="store_true", help="read TS from stdin instead of UDP")
ap.add_argument("--infd", type=int, default=-1, help="read TS from this file descriptor")
ap.add_argument("--payload-text", dest="payload_text", default="",
                help="if set, RX decodes each payload as '<text> : <SEQ>' (NUL-stripped) "
                     "and prints it on every tick. The TX side's --payload-text must match.")
ap.add_argument("--udp-out", dest="udp_out", default="",
                help="forward each recovered payload as one UDP datagram to host:port. "
                     "Independent of --verbose / --payload-text / --every; the console "
                     "print rules are untouched. Trailing NUL padding is stripped so "
                     "`nc -lu PORT` displays the original operator string verbatim.")
a = ap.parse_args()

UDP_OUT_SOCK = None; UDP_OUT_ADDR = None
if a.udp_out:
    try:
        _host, _port = a.udp_out.rsplit(":", 1)
        UDP_OUT_ADDR = (_host, int(_port))
        UDP_OUT_SOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sys.stderr.write("rawdata_rx: --udp-out forwarding payloads to %s:%d\n"
                         % (UDP_OUT_ADDR[0], UDP_OUT_ADDR[1])); sys.stderr.flush()
    except Exception as e:
        sys.stderr.write("rawdata_rx: --udp-out parse/socket failed (%s); disabled\n" % e)
        UDP_OUT_SOCK = None; UDP_OUT_ADDR = None
PID = int(a.pid, 16)
FRAME = HDR + a.plen if a.plen else 0
exp_fps = (a.rate / (FRAME * 8)) if (a.rate and FRAME) else 0

def hdr():
    sr = a.symrate
    try: sr = "%.3f MSym/s" % (float(a.symrate)/1e6)
    except Exception: pass
    fq = a.freq
    try: fq = "%.3f MHz" % (float(a.freq)/1e6)
    except Exception: pass
    print(f"{B}+{'-'*68}+{X}")
    print(f"{B}| DVB-S2 RAW-DATA LINK TEST{' '*43}|{X}")
    print(f"{B}+{'-'*68}+{X}")
    print(f"  mode      : {a.mode}")
    print(f"  MODCOD    : {a.modcod:<22} symbol rate : {sr}")
    print(f"  frequency : {fq:<22} data PID    : {a.pid}")
    print(f"  payload   : {a.plen} B/frame{' '*9} target rate : {a.rate/1e3:.0f} kbit/s"
          + (f"  (~{exp_fps:.0f} frame/s)" if exp_fps else ""))
    print(f"{B}+{'-'*68}+{X}")
    sys.stdout.flush()

# Source: UDP socket (default) or a byte stream (stdin / a file descriptor).
# Stream mode (dvbs2-rx --out-fd ...) needs cross-read 188-byte realignment,
# so we keep a persistent tsbuf for BOTH modes and select() for the 1 s tick.
USE_FD = a.stdin or a.infd >= 0
if USE_FD:
    src_fd = 0 if a.stdin else a.infd
    sock = None
else:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", a.port)); src_fd = sock.fileno()
hdr()
tsbuf = bytearray()

def depacketize(d):
    """append bytes, emit DATA-PID payloads from complete 0x47-aligned TS packets"""
    global tsbuf, last_cc, ts_loss
    tsbuf += d
    # align to first sync byte
    j = tsbuf.find(b"\x47")
    if j > 0: del tsbuf[:j]
    while len(tsbuf) >= 188:
        if tsbuf[0] != 0x47:                       # resync
            k = tsbuf.find(b"\x47", 1)
            if k < 0: tsbuf.clear(); break
            del tsbuf[:k]; continue
        pkt = tsbuf[:188]; del tsbuf[:188]
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pid == PID:
            cc = pkt[3] & 0x0f
            if last_cc is not None and cc != ((last_cc + 1) & 0x0f): ts_loss += 1
            last_cc = cc
            asm.extend(pkt[4:188])

asm = bytearray()          # reassembled DATA-PID byte stream
last_cc = None; ts_loss = 0
last_seq = None
iv_rx = iv_lost = iv_bytes = 0; iv_lat = []
cum_rx = cum_lost = 0
t0 = time.time(); tl = t0; started = None

def parse_frames():
    global asm, last_seq, iv_rx, iv_lost, iv_bytes, cum_rx, cum_lost, iv_lat, started
    while True:
        m = asm.find(b"\xA5\xA5\xC3\xC3")
        if m < 0:
            if len(asm) > 1 << 16: del asm[:-4]
            return
        if len(asm) - m < HDR:
            if m: del asm[:m]
            return
        magic, seq, txts, ln = struct.unpack(">IQQI", asm[m:m+HDR])
        if ln > 65535:            # bogus -> skip this magic
            del asm[:m+4]; continue
        if len(asm) - m < HDR + ln:
            if m: del asm[:m]
            return
        payload = asm[m+HDR:m+HDR+ln]
        del asm[:m+HDR+ln]
        stripped = bytes(payload).rstrip(b"\x00")
        if UDP_OUT_SOCK is not None and stripped:
            try:
                UDP_OUT_SOCK.sendto(stripped, UDP_OUT_ADDR)
            except Exception:
                pass
        now = time.time_ns()
        if started is None: started = time.time()
        lat = (now - txts) / 1e6
        ok = all(payload[i] == ((seq + i) & 0xff) for i in range(0, ln, max(1, ln//16)))  # sampled check
        iv_rx += 1; cum_rx += 1; iv_bytes += ln; iv_lat.append(lat)
        if last_seq is not None and seq > last_seq + 1:
            gap = seq - last_seq - 1; iv_lost += gap; cum_lost += gap
        last_seq = seq
        has_data = bool(stripped)
        show_frame = (
            a.verbose or
            (a.every > 0 and cum_rx % a.every == 0) or
            (a.udp_out and has_data)   # always surface injected data in UDP mode
        )
        if show_frame:
            lat_show = f" lat={lat:6.1f}ms" if lat <= 1000.0 else ""
            if a.payload_text:
                txt = stripped.decode("utf-8", errors="replace")
                print(f"  {Dim}rx{X} seq={B}{seq:<10}{X} len={ln} payload=\"{txt}\""
                      f"{lat_show}")
            else:
                pv = payload[:8].hex()
                print(f"  {Dim}rx{X} seq={B}{seq:<10}{X} len={ln} payload={pv}.."
                      f"{lat_show} {'ok' if ok else R+'BADPATTERN'+X}")
            sys.stdout.flush()

# Cleanly handle Ctrl-C / pipe close around the whole main loop. Without this
# wrapper, an interrupt during select() leaks a Python traceback to the
# operator console.
try:
 eof = False
 while not eof:
    r, _, _ = select.select([src_fd], [], [], 1.0)
    if r:
        if sock is not None:
            try: d = sock.recv(65536)
            except socket.timeout: d = b""
        else:
            d = os.read(src_fd, 65536)
            if d == b"": eof = True          # pipe closed -> TX ended
        if d:
            depacketize(d); parse_frames()
    t = time.time()
    if t - tl >= 1.0:
        dt = t - tl; up = t - (started or t0)
        fps = iv_rx / dt; mbps = iv_bytes * 8 / dt / 1e6
        # PSR (operator request): just count SEQ gaps inside the frames we
        # received this 1-second window and report (rx - gaps) / rx * 100. No
        # 'expected fps' anywhere — if the operator wants to know about the
        # throttled-receiver case they can read the fr/s number directly.
        if iv_rx > 0:
            psr = max(0.0, 100.0 * (iv_rx - iv_lost) / iv_rx)
        else:
            psr = 0.0
        pc = G if psr >= 99.9 else (Y if psr >= 95 else R)
        lc = G if iv_lost == 0 else R
        # Latency: only show if it stays under 1 second; over 1 s and the
        # number is meaningless to the operator (means the link is buffer-
        # bound rather than time-bound — they should be looking at USB OF or
        # MODCOD instead).
        lat_arr = iv_lat or []
        if lat_arr and max(lat_arr) <= 1000.0:
            lat_str = (f" | lat {min(lat_arr):.0f}/"
                       f"{sum(lat_arr)/len(lat_arr):.0f}/"
                       f"{max(lat_arr):.0f} ms")
        else:
            lat_str = ""
        print(f"[{up:6.0f}s] {B}rx {fps:5.0f}{X} fr/s "
              f"{pc}PSR {psr:5.1f}%{X} | {mbps:6.3f} Mbps | "
              f"loss/s {lc}{iv_lost}{X} {Dim}cum {cum_lost}{X}"
              f"{lat_str}"
              f" {Dim}(ts-resync {ts_loss}){X}")
        sys.stdout.flush()
        iv_rx = iv_lost = iv_bytes = 0; iv_lat = []; tl = t
except (KeyboardInterrupt, BrokenPipeError):
    pass
finally:
    try: sys.stdout.flush()
    except Exception: pass
    try: sys.stderr.flush()
    except Exception: pass
