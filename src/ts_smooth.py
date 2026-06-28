#!/usr/bin/env python3
"""TS jitter-buffer / smoother for DVB-S2 playback.

Reads packetized mpegts from stdin, meters it onto stdout at a steady
rate so the downstream player (ffplay) always has a smooth feed even
when the demod emits TS in bursts per LDPC-frame decode.
"""
import argparse, fcntl, os, sys, time

TS_LEN = 188

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bitrate", type=float, required=True,
                    help="TS net bitrate in bits/sec")
    ap.add_argument("--buf-ms", type=float, default=50.0,
                    help="Target jitter buffer depth in ms (default 50)")
    args = ap.parse_args()

    pkt_interval = (TS_LEN * 8.0) / args.bitrate
    target_pkts = int((args.buf_ms / 1000.0) / pkt_interval)

    fd = sys.stdin.buffer.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    stdout = sys.stdout.buffer
    buf = bytearray()
    warmed = False

    try:
        while True:
            try:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf.extend(chunk)
            except (BlockingIOError, OSError):
                pass

            now = time.monotonic()

            if not warmed:
                if len(buf) >= target_pkts * TS_LEN:
                    warmed = True
                    next_output = now
                else:
                    time.sleep(0.001)
                    continue

            n_out = 0
            while len(buf) >= TS_LEN and now >= next_output:
                stdout.write(buf[:TS_LEN])
                buf = buf[TS_LEN:]
                next_output += pkt_interval
                n_out += 1
            if n_out:
                stdout.flush()

            if len(buf) >= target_pkts * TS_LEN:
                delay = next_output - now
                time.sleep(max(min(delay, 0.002), 0) if delay > 0 else 0.001)
            elif len(buf) > 0:
                time.sleep(0.001)
            else:
                time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        if buf:
            stdout.write(buf)
            stdout.flush()

if __name__ == "__main__":
    main()
