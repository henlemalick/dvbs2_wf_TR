import json, re, signal, sys, time
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from math import sqrt
from threading import Thread

from gnuradio import blocks, dtv, dvbs2rx, filter, gr, uhd
from gnuradio.filter import firdes


def scale_rrc_taps(taps, sps, fullscale):
    mag_taps = list(map(abs, taps))
    max_sum = 0
    for i in range(sps):
        sum_i = sum(mag_taps[i::sps])
        if sum_i > max_sum:
            max_sum = sum_i
    return [sqrt(2) * fullscale * x / max_sum for x in taps]


class GrTxOnly(gr.top_block):
    """DVB-S2 TX-only flowgraph. Reads MPEG-TS from a file descriptor,
    modulates to DVB-S2, and transmits via USRP B210."""

    def __init__(self, opts):
        gr.top_block.__init__(self, "GR DVB-S2 TX Only", catch_exceptions=True)

        self.serial = opts.serial
        self.tx_freq = opts.tx_freq
        self.sym_rate = opts.sym_rate
        self.sps = opts.sps
        self.samp_rate = self.sym_rate * self.sps
        self.frame_size = opts.frame_size
        self.rolloff = opts.rolloff
        self.otw_format = opts.otw_format
        self.stream_args = opts.stream_args
        self.rrc_delay = opts.rrc_delay
        self.fullscale = opts.fullscale
        self.tx_gain = opts.tx_gain
        self.tx_antenna = opts.tx_antenna
        self.in_fd = opts.in_fd
        self.gold_code = opts.gold_code
        self.pilots = opts.pilots
        self.loopback = bool(getattr(opts, "loopback", False))

        _m = re.match(r"(QPSK|8PSK|16APSK|32APSK)(\d+/\d+)$", opts.modcod.replace("_", ""))
        if not _m:
            raise ValueError("bad MODCOD: %s" % opts.modcod)
        self.constellation, self.code_rate = _m.group(1), _m.group(2)

        if self.loopback:
            self.usrp_sink = None
        else:
            self.setup_usrp_sink()

        self.connect_dvbs2tx()

    def _dev_args(self):
        s = (self.serial or "").strip()
        if s and s not in ("0", "any"):
            return "type=b200,serial={}".format(s)
        return "type=b200"

    def setup_usrp_sink(self):
        self.usrp_sink = uhd.usrp_sink(
            self._dev_args(),
            uhd.stream_args(
                cpu_format="fc32",
                otw_format=self.otw_format,
                args=self.stream_args,
                channels=[0],
            ),
            "",
        )
        chan = 0
        self.usrp_sink.set_samp_rate(self.samp_rate)
        self.usrp_sink.set_center_freq(self.tx_freq, chan)
        self.usrp_sink.set_antenna(self.tx_antenna, chan)
        self.usrp_sink.set_gain(self.tx_gain, chan)
        actual_samp_rate = self.usrp_sink.get_samp_rate()
        self.sps = actual_samp_rate / self.sym_rate
        self.samp_rate = actual_samp_rate

    def _tx_pilots(self):
        return self.pilots == 'on'

    def connect_dvbs2tx(self):
        source = blocks.file_descriptor_source(gr.sizeof_char, self.in_fd, False)

        translated_params = dvbs2rx.params.translate(
            'DVB-S2', self.frame_size, self.code_rate, self.constellation,
            self.rolloff, self._tx_pilots())
        (standard, frame_size, code_rate, constellation, rolloff,
         pilots) = translated_params

        _KBCH = {
            "short": {
                "QPSK1/2": 7032,  "QPSK3/5": 9552,  "QPSK2/3": 10632,
                "QPSK3/4": 11712, "QPSK4/5": 12432, "QPSK5/6": 13152,
                "QPSK8/9": 14232,
                "8PSK3/5": 9552,  "8PSK2/3": 10632, "8PSK3/4": 11712,
                "8PSK5/6": 13152, "8PSK8/9": 14232,
                "16APSK2/3": 10632, "16APSK3/4": 11712, "16APSK4/5": 12432,
                "16APSK5/6": 13152, "16APSK8/9": 14232,
                "32APSK3/4": 11712, "32APSK4/5": 12432, "32APSK5/6": 13152,
                "32APSK8/9": 14232,
            },
            "normal": {
                "QPSK1/4": 16008,  "QPSK1/3": 21408,  "QPSK2/5": 25728,
                "QPSK1/2": 32208,  "QPSK3/5": 38688,  "QPSK2/3": 43040,
                "QPSK3/4": 48408,  "QPSK4/5": 51648,  "QPSK5/6": 53840,
                "QPSK8/9": 57472,  "QPSK9/10": 58192,
                "8PSK3/5": 38688,  "8PSK2/3": 43040,  "8PSK3/4": 48408,
                "8PSK5/6": 53840,  "8PSK8/9": 57472,  "8PSK9/10": 58192,
                "16APSK2/3": 43040, "16APSK3/4": 48408, "16APSK4/5": 51648,
                "16APSK5/6": 53840, "16APSK8/9": 57472, "16APSK9/10": 58192,
                "32APSK3/4": 48408, "32APSK4/5": 51648, "32APSK5/6": 53840,
                "32APSK8/9": 57472, "32APSK9/10": 58192,
            },
        }
        _NLDPC = {"short": 16200, "normal": 64800}
        _BITSYM = {"QPSK": 2, "8PSK": 3, "16APSK": 4, "32APSK": 5}
        _kbch = _KBCH[self.frame_size].get(
            self.constellation + self.code_rate, 7032)
        _plframe_sym = _NLDPC[self.frame_size] // _BITSYM[self.constellation] + 26
        _bbframe_ts_rate = int(_kbch * self.sym_rate / _plframe_sym)

        bbheader = dtv.dvb_bbheader_bb(standard, frame_size, code_rate, rolloff,
                                       dtv.INPUTMODE_NORMAL, dtv.INBAND_OFF,
                                       168, _bbframe_ts_rate)
        bbscrambler = dtv.dvb_bbscrambler_bb(standard, frame_size, code_rate)
        bch_encoder = dtv.dvb_bch_bb(standard, frame_size, code_rate)
        ldpc_encoder = dtv.dvb_ldpc_bb(standard, frame_size, code_rate,
                                       dtv.MOD_OTHER)
        interleaver = dtv.dvbs2_interleaver_bb(frame_size, code_rate,
                                               constellation)
        xfecframe_mapper = dtv.dvbs2_modulator_bc(frame_size, code_rate,
                                                  constellation,
                                                  dtv.INTERPOLATION_OFF)
        pl_framer = dtv.dvbs2_physical_cc(frame_size, code_rate, constellation,
                                          pilots, self.gold_code)

        self.connect((source, 0), (bbheader, 0), (bbscrambler, 0),
                     (bch_encoder, 0), (ldpc_encoder, 0), (interleaver, 0),
                     (xfecframe_mapper, 0), (pl_framer, 0))

        source.set_max_output_buffer(8192)
        for _blk in (bbheader, bbscrambler, bch_encoder, ldpc_encoder, interleaver):
            _blk.set_max_output_buffer(4 * _blk.output_multiple())

        interp_sps = self.sps / 2

        if interp_sps.is_integer():
            ntaps = int(2 * self.rrc_delay * self.sps) + 1
            rrc_taps = firdes.root_raised_cosine(
                self.sps, self.sps, 1.0, self.rolloff, ntaps)
            if self.fullscale is not None:
                rrc_taps = scale_rrc_taps(rrc_taps, int(self.sps), self.fullscale)
            interp_filter = filter.interp_fir_filter_ccf(int(interp_sps), rrc_taps)
            self.connect((pl_framer, 0), (interp_filter, 0))
        else:
            downsampler = blocks.keep_m_in_n(gr.sizeof_gr_complex, 1, 2, 0)
            nfilts = 32
            ntaps = int(2 * nfilts * self.rrc_delay * self.sps) + 1
            rrc_taps = firdes.root_raised_cosine(nfilts, nfilts, 1.0,
                                                 self.rolloff, ntaps)
            if self.fullscale is not None:
                rrc_taps = scale_rrc_taps(rrc_taps, nfilts, self.fullscale)
            interp_filter = filter.pfb_arb_resampler_ccf(self.sps, rrc_taps)
            self.connect((pl_framer, 0), (downsampler, 0), (interp_filter, 0))

        interp_filter.declare_sample_delay(int(self.rrc_delay * self.sps))

        if self.loopback:
            source_dbg = blocks.vector_source_f([0.0]*1024, True, gr.sizeof_float)
            sink_dbg = blocks.null_sink(gr.sizeof_gr_complex)
            self.connect((interp_filter, 0), (sink_dbg, 0))
        else:
            self.connect((interp_filter, 0), (self.usrp_sink, 0))


def argument_parser():
    p = ArgumentParser(prog="gr_txonly.py",
                       description="DVB-S2 TX-only flowgraph for one USRP B210.",
                       formatter_class=ArgumentDefaultsHelpFormatter)
    p.add_argument("--serial", type=str, default="",
                   help="USRP B210 serial number")
    p.add_argument("--tx-freq", type=float, default=1.5e9,
                   help="TX center frequency in Hz")
    p.add_argument("--sym-rate", type=float, default=2e6,
                   help="Symbol rate in bauds")
    p.add_argument("--modcod", type=str, default="QPSK3/5",
                   help="MODCOD, e.g. QPSK3/5")
    p.add_argument("--frame-size", type=str, choices=['normal', 'short'],
                   default='short', help="FECFRAME size")
    p.add_argument("--sps", type=float, default=2.0,
                   help="Oversampling ratio (samples per symbol)")
    p.add_argument("--rolloff", type=float, choices=[0.35, 0.25, 0.2],
                   default=0.35, help="RRC rolloff factor")
    p.add_argument("--otw-format", choices=["sc16","sc8"], default="sc16",
                   help="Over-the-wire IQ format")
    p.add_argument("--stream-args", dest="stream_args", default="",
                   help="UHD stream args (e.g. send_buff_size=2097152)")
    p.add_argument("--tx-gain", type=float, default=70.0, help="USRP TX gain")
    p.add_argument("--tx-antenna", type=str, default="TX/RX",
                   help="USRP TX antenna port")
    p.add_argument("--in-fd", type=int, default=0,
                   help="Input MPEG-TS file descriptor")
    p.add_argument("--rrc-delay", type=int, default=50,
                   help="RRC filter delay (symbol periods)")
    p.add_argument("--fullscale", type=float, default=1.0,
                   help="Target full-scale TX IQ amplitude")
    p.add_argument("--gold-code", type=int, default=0, help="Gold code")
    p.add_argument("--pilots", choices=['on', 'off'], default='on',
                   help="Whether PLFRAMEs contain pilots")
    p.add_argument("--loopback", action="store_true", default=False,
                   help="In-process TX self-test (no USRP)")
    opts = p.parse_args()
    if opts.fullscale is not None and opts.fullscale <= 0:
        opts.fullscale = None
    return opts


def main():
    opts = argument_parser()
    tb = GrTxOnly(opts)
    tb.start()
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
