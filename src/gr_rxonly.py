import json, re, signal, sys, time
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from gnuradio import analog, blocks, digital, dvbs2rx, filter, gr, uhd
from gnuradio.filter import firdes


class GrRxOnly(gr.top_block):
    """DVB-S2 RX-only flowgraph. Receives from USRP B210, demodulates DVB-S2,
    and writes recovered MPEG-TS to a file descriptor."""

    def __init__(self, opts):
        gr.top_block.__init__(self, "GR DVB-S2 RX Only", catch_exceptions=True)

        self.serial = opts.serial
        self.rx_freq = opts.rx_freq
        self.sym_rate = opts.sym_rate
        self.sps = opts.sps
        self.samp_rate = self.sym_rate * self.sps
        self.frame_size = opts.frame_size
        self.rolloff = opts.rolloff
        self.otw_format = opts.otw_format
        self.stream_args = opts.stream_args
        self.rrc_delay = opts.rrc_delay
        self.rx_gain = opts.rx_gain
        self.rx_antenna = opts.rx_antenna
        self.lo_offset = opts.lo_offset
        self.out_fd = opts.out_fd
        self.ldpc_iterations = opts.ldpc_iterations
        self.gold_code = opts.gold_code
        self.pilots = opts.pilots
        self.rot_max_buf = opts.rot_max_buf
        self.debug = opts.debug
        self.pl_acm_vcm = opts.pl_acm_vcm
        self.pl_freq_est_period = opts.pl_freq_est_period

        self.agc_rate = opts.agc_rate
        self.agc_ref = opts.agc_ref
        self.agc_gain = opts.agc_gain
        self.sym_sync_impl = opts.sym_sync_impl
        self.sym_sync_loop_bw = opts.sym_sync_loop_bw
        self.sym_sync_damping = opts.sym_sync_damping
        self.sym_sync_rrc_nfilts = opts.sym_sync_rrc_nfilts

        _m = re.match(r"(QPSK|8PSK|16APSK|32APSK)(\d+/\d+)$", opts.modcod.replace("_", ""))
        if not _m:
            raise ValueError("bad MODCOD: %s" % opts.modcod)
        self.constellation, self.code_rate = _m.group(1), _m.group(2)

        self.mon_server = opts.mon_server
        self.mon_port = opts.mon_port
        self.loopback = bool(getattr(opts, "loopback", False))

        if self.loopback:
            self.usrp_source = None
        else:
            self.setup_usrp_source()

        self.connect_dvbs2rx()

    def _dev_args(self):
        s = (self.serial or "").strip()
        if s and s not in ("0", "any"):
            return "type=b200,serial={}".format(s)
        return "type=b200"

    def setup_usrp_source(self):
        self.usrp_source = uhd.usrp_source(
            self._dev_args(),
            uhd.stream_args(
                cpu_format="fc32",
                otw_format=self.otw_format,
                args=self.stream_args,
                channels=[0],
            ),
        )
        chan = 0
        self.usrp_source.set_samp_rate(self.samp_rate)
        self.usrp_source.set_antenna(self.rx_antenna, chan)
        self.usrp_source.set_gain(self.rx_gain, chan)
        actual_samp_rate = self.usrp_source.get_samp_rate()
        self.sps = actual_samp_rate / self.sym_rate
        self.samp_rate = actual_samp_rate
        lo_offset = self.samp_rate
        self.usrp_source.set_center_freq(
            uhd.tune_request(self.rx_freq, lo_offset), chan)

    def connect_dvbs2rx(self):
        sink = blocks.file_descriptor_sink(gr.sizeof_char, self.out_fd)

        translated_params = dvbs2rx.params.translate(
            'DVB-S2', self.frame_size, self.code_rate, self.constellation)
        standard, frame_size, code_rate, constellation = translated_params

        ldpc_decoder = dvbs2rx.ldpc_decoder_bb(
            standard, frame_size, code_rate, constellation, dvbs2rx.OM_MESSAGE,
            dvbs2rx.INFO_OFF, self.ldpc_iterations, self.debug)
        bch_decoder = dvbs2rx.bch_decoder_bb(standard, frame_size, code_rate,
                                             dvbs2rx.OM_MESSAGE, self.debug)
        bbdescrambler = dvbs2rx.bbdescrambler_bb(standard, frame_size, code_rate)
        bbdeheader = dvbs2rx.bbdeheader_bb(standard, frame_size, code_rate,
                                           self.debug)

        self.connect((ldpc_decoder, 0), (bch_decoder, 0), (bbdescrambler, 0),
                     (bbdeheader, 0), (sink, 0))

        ldpc_decoder.set_max_output_buffer(4 * ldpc_decoder.output_multiple())
        for _blk in (bch_decoder, bbdescrambler, bbdeheader):
            _blk.set_max_output_buffer(max(8192, 4 * _blk.output_multiple()))

        analog_agc = analog.agc_cc(self.agc_rate, self.agc_ref, self.agc_gain)
        analog_agc.set_max_gain(65536)
        rotator = dvbs2rx.rotator_cc(0, True)
        if self.rot_max_buf is not None:
            rotator.set_max_output_buffer(self.rot_max_buf)
        self.connect((analog_agc, 0), (rotator, 0))
        first_block = analog_agc

        sps_is_even_int = float(self.sps).is_integer() and int(self.sps) % 2 == 0
        sym_sync_impl = self.sym_sync_impl
        if sym_sync_impl == "oot" and not sps_is_even_int:
            gr.log.warn("OOT symbol synchronizer needs an even integer sps>=2 "
                        "(sps={}); switching to in-tree".format(self.sps))
            sym_sync_impl = "in-tree"

        if sym_sync_impl == "in-tree":
            n_rrc_taps = int(2 * self.rrc_delay * self.sps) + 1
            n_poly_rrc_taps = ((n_rrc_taps - 1) * self.sym_sync_rrc_nfilts) + 1
            poly_rrc_taps = firdes.root_raised_cosine(
                self.sym_sync_rrc_nfilts,
                self.samp_rate * self.sym_sync_rrc_nfilts, self.sym_rate,
                self.rolloff, n_poly_rrc_taps)
            symbol_sync = digital.symbol_sync_cc(
                digital.TED_GARDNER, self.sps, self.sym_sync_loop_bw,
                self.sym_sync_damping, 1.0, 1.5, 1,
                digital.constellation_bpsk().base(), digital.IR_PFB_MF,
                self.sym_sync_rrc_nfilts, poly_rrc_taps)
        else:
            interp_method = 0
            symbol_sync = dvbs2rx.symbol_sync_cc(
                self.sps, self.sym_sync_loop_bw, self.sym_sync_damping,
                self.rolloff, self.rrc_delay, self.sym_sync_rrc_nfilts,
                interp_method)
        self.connect((rotator, 0), (symbol_sync, 0))

        plsync = dvbs2rx.plsync_cc(*self._plsync_params())
        self.msg_connect((plsync, 'rotator_phase_inc'), (rotator, 'cmd'))

        xfecframe_demapper = dvbs2rx.xfecframe_demapper_cb(
            frame_size, code_rate, constellation)
        self.connect((symbol_sync, 0), (plsync, 0), (xfecframe_demapper, 0),
                     (ldpc_decoder, 0))

        self.msg_connect((ldpc_decoder, 'llr_pdu'),
                         (xfecframe_demapper, 'llr_pdu'))

        if self.loopback:
            vec = blocks.vector_source_c([0.0]*1024, True, gr.sizeof_gr_complex)
            self.connect((vec, 0), (first_block, 0))
        else:
            self.connect((self.usrp_source, 0), (first_block, 0))

        self.bbdeheader = bbdeheader
        self.bch_decoder = bch_decoder
        self.ldpc_decoder = ldpc_decoder
        self.xfecframe_demapper = xfecframe_demapper
        self.plsync = plsync

    def get_stats(self):
        fec_frames = self.bch_decoder.get_frame_count()
        fec_errors = self.bch_decoder.get_error_count()
        has = fec_frames > 0
        return {
            "lock": self.plsync.get_locked(),
            "snr": self.xfecframe_demapper.get_snr() if has else None,
            "plsync": {
                "freq_offset_hz": self.plsync.get_freq_offset() * self.sym_rate,
                "sof_count": self.plsync.get_sof_count(),
                "frame_count": {"processed": self.plsync.get_frame_count(),
                                "rejected": self.plsync.get_rejected_count(),
                                "dummy": self.plsync.get_dummy_count()},
            },
            "fec": {"frames": fec_frames, "errors": fec_errors,
                    "fer": (fec_errors / fec_frames) if has else None,
                    "avg_ldpc_trials": self.ldpc_decoder.get_average_trials() if has else None},
            "mpeg-ts": {"packets": self.bbdeheader.get_packet_count(),
                        "errors": self.bbdeheader.get_error_count(), "per": None},
        }

    def _tx_pilots(self):
        return self.pilots == 'on'

    def _plsync_params(self):
        if self.pl_acm_vcm:
            pls_filter_lo = 0xFFFFFFFFFFFFFFFF
            pls_filter_hi = 0xFFFFFFFFFFFFFFFF
        else:
            target_pls = list()
            if self.pilots == "auto":
                for pilots_enabled in [False, True]:
                    target_pls.append(
                        dvbs2rx.params.dvbs2_pls(self.constellation,
                                                 self.code_rate,
                                                 self.frame_size,
                                                 pilots_enabled))
            else:
                pilots_enabled = self.pilots == "on"
                target_pls.append(
                    dvbs2rx.params.dvbs2_pls(self.constellation, self.code_rate,
                                             self.frame_size, pilots_enabled))
            pls_filter_lo, pls_filter_hi = dvbs2rx.params.pls_filter(*target_pls)
        return (self.gold_code, self.pl_freq_est_period, self.sps, self.debug,
                self.pl_acm_vcm, False, pls_filter_lo, pls_filter_hi)


def argument_parser():
    p = ArgumentParser(prog="gr_rxonly.py",
                       description="DVB-S2 RX-only flowgraph for one USRP B210.",
                       formatter_class=ArgumentDefaultsHelpFormatter)
    p.add_argument("--serial", type=str, default="",
                   help="USRP B210 serial number")
    p.add_argument("--rx-freq", type=float, default=1.5e9,
                   help="RX center frequency in Hz")
    p.add_argument("--sym-rate", type=float, default=2e6,
                   help="Symbol rate in bauds")
    p.add_argument("--modcod", type=str, default="QPSK3/5",
                   help="MODCOD, e.g. QPSK3/5")
    p.add_argument("--frame-size", type=str, choices=['normal', 'short'],
                   default='short', help="FECFRAME size")
    p.add_argument("--sps", type=float, default=4.0,
                   help="Oversampling ratio (samples per symbol)")
    p.add_argument("--rolloff", type=float, choices=[0.35, 0.25, 0.2],
                   default=0.35, help="RRC rolloff factor")
    p.add_argument("--otw-format", choices=["sc16","sc8"], default="sc16",
                   help="Over-the-wire IQ format")
    p.add_argument("--stream-args", dest="stream_args", default="",
                   help="UHD stream args")
    p.add_argument("--rx-gain", type=float, default=40.0, help="USRP RX gain")
    p.add_argument("--rx-antenna", type=str, default="RX2",
                   help="USRP RX antenna port")
    p.add_argument("--lo-offset", type=float, default=4e6,
                   help="RX LO offset in Hz")
    p.add_argument("--out-fd", type=int, default=1,
                   help="Output MPEG-TS file descriptor")
    p.add_argument("--rrc-delay", type=int, default=50,
                   help="RRC filter delay (symbol periods)")
    p.add_argument("--ldpc-iterations", type=int, default=25,
                   help="Max LDPC decoding iterations")
    p.add_argument("--gold-code", type=int, default=0, help="Gold code")
    p.add_argument("--pilots", choices=['on', 'off', 'auto'], default='on',
                   help="Whether PLFRAMEs contain pilots")
    p.add_argument("--rot-max-buf", type=int, default=None,
                   help="Target max output buffer for the rotator")
    p.add_argument("--debug", type=int, default=0, help="Debug level")
    p.add_argument("--mon-server", action="store_true", default=False,
                   help="Launch HTTP receiver-stats server (JSON)")
    p.add_argument("--mon-port", type=int, default=8011,
                   help="Monitor server port")
    p.add_argument("--pl-acm-vcm", action='store_true', default=False,
                   help="Force PL Sync into ACM/VCM mode")
    p.add_argument("--pl-freq-est-period", type=int, default=10,
                   help="Coarse freq offset estimation period in frames")
    p.add_argument("--loopback", action="store_true", default=False,
                   help="Self-test: feed zeros into RX chain (no USRP)")
    p.add_argument("--agc-rate", type=float, default=1e-5, help="AGC update rate")
    p.add_argument("--agc-ref", type=float, default=1.0, help="AGC reference")
    p.add_argument("--agc-gain", type=float, default=1.0, help="AGC initial gain")
    p.add_argument("--sym-sync-impl", choices=['in-tree', 'oot'], default='oot',
                   help="Symbol synchronizer implementation")
    p.add_argument("--sym-sync-loop-bw", type=float, default=1e-3,
                   help="Symbol synchronizer loop bandwidth")
    p.add_argument("--sym-sync-damping", type=float, default=1.0,
                   help="Symbol synchronizer damping factor")
    p.add_argument("--sym-sync-rrc-nfilts", type=int, default=128,
                   help="Polyphase RRC subfilters")
    opts = p.parse_args()
    return opts


class _MonHandler(BaseHTTPRequestHandler):
    tb = None
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(self.tb.get_stats()).encode())

def _mon_server(tb, port):
    _MonHandler.tb = tb
    HTTPServer(("", port), _MonHandler).serve_forever()

def main():
    opts = argument_parser()
    tb = GrRxOnly(opts)
    tb.start()
    if opts.mon_server:
        Thread(target=_mon_server, args=(tb, opts.mon_port), daemon=True).start()
    def sig_handler(sig=None, frame=None):
        tb.stop(); tb.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        tb.stop(); tb.wait()


if __name__ == '__main__':
    main()
