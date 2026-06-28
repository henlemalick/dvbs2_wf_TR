#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio DVB-S2 FULL-DUPLEX loopback on a SINGLE USRP B210.
#
# One process holds BOTH a uhd.usrp_sink (TX) and a uhd.usrp_source (RX), both
# pointed at the same B210 serial. UHD caches the motherboard per-process, so
# the two blocks share the single device. TX and RX center frequencies are set
# independently (RX = TX + offset) so a single-radio self-loopback works despite
# LO leakage / DC at the exact carrier.
#
# This is a headless, NON-Qt counterpart to the gr-dvbs2rx `dvbs2-tx` and
# `dvbs2-rx` apps. The TX and RX block chains below mirror those apps exactly
# (same constructor args and default parameters). It exists for an A/B test
# against the single-threaded aff3ct modem (dvbs2_TR) on the same B210/VM.
#
# TX chain:  file_descriptor_source(MPEG-TS)
#              -> dtv.dvb_bbheader_bb -> dtv.dvb_bbscrambler_bb -> dtv.dvb_bch_bb
#              -> dtv.dvb_ldpc_bb(MOD_OTHER) -> dtv.dvbs2_interleaver_bb
#              -> dtv.dvbs2_modulator_bc -> dtv.dvbs2_physical_cc (PL framer)
#              -> RRC interpolating filter -> uhd.usrp_sink
#
# RX chain:  uhd.usrp_source -> analog.agc_cc -> dvbs2rx.rotator_cc
#              -> digital.symbol_sync_cc OR dvbs2rx.symbol_sync_cc
#              -> dvbs2rx.plsync_cc -> dvbs2rx.xfecframe_demapper_cb
#              -> dvbs2rx.ldpc_decoder_bb -> dvbs2rx.bch_decoder_bb
#              -> dvbs2rx.bbdescrambler_bb -> dvbs2rx.bbdeheader_bb
#              -> file_descriptor_sink(MPEG-TS)

import json
import re
import signal
import sys
import time
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import pi, sqrt
from threading import Thread

from gnuradio import analog, blocks, digital, dtv, dvbs2rx, filter, gr, uhd
from gnuradio.filter import firdes


def scale_rrc_taps(taps, sps, fullscale):
    """Scale RRC taps so filtered I/Q stay within +-fullscale.

    Identical to the helper in the dvbs2-tx app: bounds the peak complex IQ
    magnitude produced by RRC interpolation and rescales the taps so each I and
    Q component fits within the SDR's +-1 (or +-fullscale) range.
    """
    mag_taps = list(map(abs, taps))
    max_sum = 0
    for i in range(sps):
        sum_i = sum(mag_taps[i::sps])
        if sum_i > max_sum:
            max_sum = sum_i
    return [sqrt(2) * fullscale * x / max_sum for x in taps]


class GrFullDuplex(gr.top_block):
    """Headless DVB-S2 TX + RX in one flowgraph on one B210."""

    def __init__(self, opts):
        gr.top_block.__init__(self, "GR DVB-S2 Full Duplex", catch_exceptions=True)

        # ----------------------------------------------------------------
        # Parameters
        # ----------------------------------------------------------------
        self.serial = opts.serial
        self.tx_freq = opts.tx_freq
        self.rx_freq = opts.tx_freq + opts.rx_offset_hz
        self.sym_rate = opts.sym_rate
        self.sps = opts.sps
        self.samp_rate = self.sym_rate * self.sps
        self.frame_size = opts.frame_size
        self.rolloff = opts.rolloff
        self.otw_format = getattr(opts, "otw_format", "sc16")
        # Host-side UHD queue caps -> bounds the end-to-end latency. Default
        # UHD buffers are ~32 MB each direction; at the per-radio byte rate
        # that's ~1 s queued and the round-trip latency starts at ~2 s. The
        # driver passes 'send_buff_size=2097152,recv_buff_size=2097152' here
        # by default -> ~62 ms queued each way -> ~220 ms steady-state.
        self.stream_args = getattr(opts, "stream_args", "")
        self.rrc_delay = opts.rrc_delay
        self.fullscale = opts.fullscale
        self.tx_gain = opts.tx_gain
        self.rx_gain = opts.rx_gain
        self.tx_antenna = opts.tx_antenna
        self.rx_antenna = opts.rx_antenna
        self.in_fd = opts.in_fd
        self.tx_iq = opts.tx_iq
        self.out_fd = opts.out_fd
        self.ldpc_iterations = opts.ldpc_iterations
        self.gold_code = opts.gold_code
        self.pilots = opts.pilots          # 'on' | 'off' | 'auto'
        self.multistream = opts.multistream
        self.pl_acm_vcm = opts.pl_acm_vcm
        self.pl_freq_est_period = opts.pl_freq_est_period
        self.rot_max_buf = opts.rot_max_buf
        self.debug = opts.debug

        # AGC (RX) defaults mirror dvbs2-rx
        self.agc_rate = opts.agc_rate
        self.agc_ref = opts.agc_ref
        self.agc_gain = opts.agc_gain

        # Symbol synchronizer (RX) defaults mirror dvbs2-rx
        self.sym_sync_impl = opts.sym_sync_impl
        self.sym_sync_loop_bw = opts.sym_sync_loop_bw
        self.sym_sync_damping = opts.sym_sync_damping
        self.sym_sync_rrc_nfilts = opts.sym_sync_rrc_nfilts

        # ----------------------------------------------------------------
        # Decode the MODCOD into constellation + code rate. Handles all DVB-S2
        # constellations incl. 16APSK/32APSK (a plain prefix-strip mis-parses
        # the APSK names, leaving an empty constellation).
        # ----------------------------------------------------------------
        self.modcod = opts.modcod
        _m = re.match(r"(QPSK|8PSK|16APSK|32APSK)(\d+/\d+)$", self.modcod.replace("_", ""))
        if not _m:
            raise ValueError("bad MODCOD: %s" % self.modcod)
        self.constellation, self.code_rate = _m.group(1), _m.group(2)

        # ----------------------------------------------------------------
        # Build the device first (so the actual sample rate from the USRP can
        # adjust sps, exactly like the apps do). The TX sink is created before
        # the RX source; both target the same serial in one process.
        #
        # --loopback: skip the USRP entirely and install a single shared
        # passthrough block (blocks.copy of gr_complex) as BOTH self.usrp_sink
        # and self.usrp_source. connect_dvbs2tx wires TX-final into
        # self.usrp_sink; connect_dvbs2rx wires self.usrp_source into the AGC.
        # With both pointing at the same 1:1 bridge, the TX output feeds
        # straight into the RX input — no radio, no air, no LO.
        # ----------------------------------------------------------------
        self.loopback = bool(getattr(opts, "loopback", False))
        if self.loopback:
            bridge = blocks.copy(gr.sizeof_gr_complex)
            self.usrp_sink = bridge
            self.usrp_source = bridge
        else:
            self.setup_usrp_sink()
            self.setup_usrp_source()

        # ----------------------------------------------------------------
        # Connect the TX and RX chains
        # ----------------------------------------------------------------
        self.connect_dvbs2tx()
        self.connect_dvbs2rx()

    # ====================================================================
    # USRP setup
    # ====================================================================
    def _dev_args(self):
        # Allow no-serial (caller passed "", "0", or "any") so a single attached
        # B210 is auto-picked. When the operator pins a concrete serial we
        # honour it. Either way force type=b200 so libuhd doesn't try to walk
        # the rest of the bus.
        s = (self.serial or "").strip()
        if s and s not in ("0", "any"):
            return "type=b200,serial={}".format(s)
        return "type=b200"

    def setup_usrp_sink(self):
        """Create the TX uhd.usrp_sink (mirrors dvbs2-tx setup_usrp_sink)."""
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

        # Adjust sps to the rate actually realized by the device so the nominal
        # symbol rate stays accurate (same as the app, must run before connect).
        actual_samp_rate = self.usrp_sink.get_samp_rate()
        self.sps = actual_samp_rate / self.sym_rate
        self.samp_rate = actual_samp_rate

    def setup_usrp_source(self):
        """Create the RX uhd.usrp_source (mirrors dvbs2-rx setup_usrp_source).

        Same process, same serial -> shares the single B210 with the sink.
        """
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

        # Re-read the realized rate and re-derive sps (matches the app).
        actual_samp_rate = self.usrp_source.get_samp_rate()
        self.sps = actual_samp_rate / self.sym_rate
        self.samp_rate = actual_samp_rate

        # Tune the RX with an LO offset (default = sampling rate) so the LO
        # leakage / DC component falls outside the observed band. Same approach
        # as dvbs2-rx (tune the LO to "freq - lo_offset" and let the FPGA DSP
        # shift the rest). The RX center frequency is set independently of TX.
        lo_offset = self.samp_rate
        self.usrp_source.set_center_freq(
            uhd.tune_request(self.rx_freq, lo_offset), chan)

    # ====================================================================
    # TX chain (mirrors dvbs2-tx connect_dvbs2tx)
    # ====================================================================
    def connect_dvbs2tx(self):
        # dvbs2_ML mode: transmit raw complex IQ from --in-fd (the MATLAB-Coder
        # dvbs2ml_tx already did BBFRAME->BCH->LDPC->map->PLframe->RRC at sps), so
        # gr just streams it to the radio. Skip the gr-dtv TX chain entirely.
        if self.tx_iq:
            iqsrc = blocks.file_descriptor_source(gr.sizeof_gr_complex,
                                                  self.in_fd, False)
            self.connect((iqsrc, 0), (self.usrp_sink, 0))
            self.tx_iq_src = iqsrc
            return

        # MPEG-TS source on a file descriptor (non-repeating).
        source = blocks.file_descriptor_source(gr.sizeof_char, self.in_fd, False)

        translated_params = dvbs2rx.params.translate(
            'DVB-S2', self.frame_size, self.code_rate, self.constellation,
            self.rolloff, self._tx_pilots())
        (standard, frame_size, code_rate, constellation, rolloff,
         pilots) = translated_params

        # Accurate net TS rate for BBFRAME null-packet stuffing.
        # kbch = user data bits per BBFRAME; plframe_sym = symbols per PLFRAME.
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

        # Cap byte-domain inter-block buffers at 4 FECFRAMEs each.
        # output_multiple() is the minimum processing granularity (= KBCH, NBCH,
        # or NLDPC bits at 1 bit/byte depending on position in the chain).
        # Capping at 4× reduces pipeline latency vs the 32 MB GNURadio default
        # while still giving the LDPC encoder enough headroom to batch-process
        # frames without UHD TX underflows. N=1 causes underflows on the Jetson
        # because a single short FECFRAME (4.15 ms at 2 MSps) doesn't give the
        # encoder enough slack between work() calls.
        # At 2 MSps QPSK1/2: 5 TX blocks × 4 × 16 ms ≈ 320 ms pipeline depth
        # + 8 ms UHD TX buf + 4 ms OTA + 8 ms UHD RX buf + 4 RX blocks ≈ 150 ms
        # → total end-to-end ≈ 200–300 ms (vs ~4 s with default buffers).
        source.set_max_output_buffer(8192)
        for _blk in (bbheader, bbscrambler, bch_encoder, ldpc_encoder, interleaver):
            _blk.set_max_output_buffer(4 * _blk.output_multiple())

        # The PL framer outputs a sequence already upsampled by 2 (zero-stuffed).
        # The RRC interpolator covers the remaining sps/2 ratio.
        interp_sps = self.sps / 2

        if interp_sps.is_integer():
            ntaps = int(2 * self.rrc_delay * self.sps) + 1
            rrc_taps = firdes.root_raised_cosine(
                self.sps,        # gain (== total interp factor)
                self.sps,        # overall oversampling ratio
                1.0,             # symbol rate
                self.rolloff,
                ntaps)
            if self.fullscale is not None:
                rrc_taps = scale_rrc_taps(rrc_taps, int(self.sps),
                                          self.fullscale)
            interp_filter = filter.interp_fir_filter_ccf(int(interp_sps),
                                                         rrc_taps)
            self.connect((pl_framer, 0), (interp_filter, 0))
        else:
            # Fractional remaining ratio: undo the PL framer's x2 then use a
            # polyphase arbitrary resampler at the full sps ratio.
            downsampler = blocks.keep_m_in_n(gr.sizeof_gr_complex, 1, 2, 0)
            nfilts = 32
            ntaps = int(2 * nfilts * self.rrc_delay * self.sps) + 1
            rrc_taps = firdes.root_raised_cosine(nfilts, nfilts, 1.0,
                                                 self.rolloff, ntaps)
            if self.fullscale is not None:
                rrc_taps = scale_rrc_taps(rrc_taps, nfilts, self.fullscale)
            interp_filter = filter.pfb_arb_resampler_ccf(self.sps, rrc_taps)
            self.connect((pl_framer, 0), (downsampler, 0), (interp_filter, 0))

        # Declare the interpolator delay over the full oversampling ratio
        # (the PL framer does not declare its x2 delay), matching the app.
        interp_filter.declare_sample_delay(int(self.rrc_delay * self.sps))

        self.connect((interp_filter, 0), (self.usrp_sink, 0))

        # Keep references for any later introspection.
        self.tx_pl_framer = pl_framer
        self.tx_interp_filter = interp_filter

    # ====================================================================
    # RX chain (mirrors dvbs2-rx connect_dvbs2rx)
    # ====================================================================
    def connect_dvbs2rx(self):
        sink = blocks.file_descriptor_sink(gr.sizeof_char, self.out_fd)

        translated_params = dvbs2rx.params.translate(
            'DVB-S2', self.frame_size, self.code_rate, self.constellation)
        standard, frame_size, code_rate, constellation = translated_params

        # Upper layer (FEC + BB processing)
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

        # Cap RX byte-domain buffers at 4 FECFRAMEs each (same principle as TX).
        # ldpc_decoder output_multiple = NBCH bits (14400 short, 64800 normal).
        ldpc_decoder.set_max_output_buffer(4 * ldpc_decoder.output_multiple())
        for _blk in (bch_decoder, bbdescrambler, bbdeheader):
            _blk.set_max_output_buffer(max(8192, 4 * _blk.output_multiple()))

        # Low layer (PHY)
        analog_agc = analog.agc_cc(self.agc_rate, self.agc_ref, self.agc_gain)
        analog_agc.set_max_gain(65536)

        rotator = dvbs2rx.rotator_cc(0, True)
        if self.rot_max_buf is not None:
            rotator.set_max_output_buffer(self.rot_max_buf)
        self.connect((analog_agc, 0), (rotator, 0))

        first_block = analog_agc  # source feeds the AGC

        # Symbol timing synchronizer (after the rotator).
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
            interp_method = 0  # polyphase interpolator
            symbol_sync = dvbs2rx.symbol_sync_cc(
                self.sps, self.sym_sync_loop_bw, self.sym_sync_damping,
                self.rolloff, self.rrc_delay, self.sym_sync_rrc_nfilts,
                interp_method)
        self.connect((rotator, 0), (symbol_sync, 0))

        # PL Sync
        plsync = dvbs2rx.plsync_cc(*self._plsync_params())
        self.msg_connect((plsync, 'rotator_phase_inc'), (rotator, 'cmd'))

        # XFECFRAME demapper
        xfecframe_demapper = dvbs2rx.xfecframe_demapper_cb(
            frame_size, code_rate, constellation)
        self.connect((symbol_sync, 0), (plsync, 0), (xfecframe_demapper, 0),
                     (ldpc_decoder, 0))

        # LDPC decoder feeds decoded LLRs back to the demapper for SNR estimation.
        self.msg_connect((ldpc_decoder, 'llr_pdu'),
                         (xfecframe_demapper, 'llr_pdu'))

        # Source into the first RX block.
        self.connect((self.usrp_source, 0), (first_block, 0))

        # Keep references for any later introspection.
        self.bbdeheader = bbdeheader
        self.bch_decoder = bch_decoder
        self.ldpc_decoder = ldpc_decoder
        self.xfecframe_demapper = xfecframe_demapper
        self.plsync = plsync

    # ====================================================================
    # Stats (mirrors dvbs2-rx get_stats; for --mon-server / dvbs2_TR --full-stats)
    # ====================================================================
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

    # ====================================================================
    # Helpers
    # ====================================================================
    def _tx_pilots(self):
        """Boolean pilot flag for the TX chain (translate expects a bool)."""
        # 'auto' is meaningless on TX; treat it as "off" so the TX is concrete.
        return self.pilots == 'on'

    def _plsync_params(self):
        """Build the plsync_cc constructor args (mirrors dvbs2-rx).

        Order (verified against include/.../plsync_cc.h and the pybind
        signature):
            (gold_code, freq_est_period, sps, debug_level,
             acm_vcm, multistream, pls_filter_lo, pls_filter_hi)
        """
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

        multistream_enabled = self.multistream in ["auto", "on"]

        return (self.gold_code, self.pl_freq_est_period, self.sps, self.debug,
                self.pl_acm_vcm, multistream_enabled, pls_filter_lo,
                pls_filter_hi)


def argument_parser():
    p = ArgumentParser(prog="gr_fullduplex.py",
                       description="Headless DVB-S2 full-duplex on a single USRP B210.",
                       formatter_class=ArgumentDefaultsHelpFormatter)

    # Device
    p.add_argument("--serial", type=str, default="8000691",
                   help="USRP B210 serial number")
    p.add_argument("--tx-freq", type=float, default=1.5e9,
                   help="TX center frequency in Hz")
    p.add_argument("--rx-offset-hz", type=float, default=100000.0,
                   help="RX center freq offset relative to TX (RX = TX + offset). "
                        "100 kHz gave the best single-B210 lock rate in testing.")

    # Link
    p.add_argument("--sym-rate", type=float, default=2e6,
                   help="Symbol rate in bauds")
    p.add_argument("--modcod", type=str, default="QPSK3/5",
                   help="MODCOD, e.g. QPSK3/5")
    p.add_argument("--frame-size", type=str, choices=['normal', 'short'],
                   default='short', help="FECFRAME size")
    p.add_argument("--sps", type=float, default=2.0,
                   help="Oversampling ratio (samples per symbol)")
    p.add_argument("--rolloff", type=float, choices=[0.35, 0.25, 0.2],
                   default=0.2, help="RRC rolloff factor")
    p.add_argument("--otw-format", choices=["sc16","sc8"], default="sc16",
                   help="Over-the-wire IQ format. sc8 halves USB bytes and is "
                        "enough headroom for DVB-S2 lock SNRs; useful when the "
                        "host can't drain samp_rate*sps fast enough (overflows)")
    p.add_argument("--stream-args", dest="stream_args", default="",
                   help="Forwarded into uhd.stream_args(args=...) on both the "
                        "sink and source. Use e.g. "
                        "'send_buff_size=2097152,recv_buff_size=2097152' to cap "
                        "host-side latency.")

    # Gains / antennas
    p.add_argument("--tx-gain", type=float, default=70.0, help="USRP TX gain")
    p.add_argument("--rx-gain", type=float, default=40.0, help="USRP RX gain")
    p.add_argument("--tx-antenna", type=str, default="TX/RX",
                   help="USRP TX antenna port")
    p.add_argument("--rx-antenna", type=str, default="RX2",
                   help="USRP RX antenna port")

    # Streams
    p.add_argument("--in-fd", type=int, default=0,
                   help="Input MPEG-TS file descriptor")
    p.add_argument("--tx-iq", action="store_true", default=False,
                   help="TX raw complex IQ from --in-fd instead of the built-in modulator")
    p.add_argument("--out-fd", type=int, default=1,
                   help="Output MPEG-TS file descriptor")

    # FEC / sync (defaults mirror the dvbs2-rx app)
    p.add_argument("--ldpc-iterations", type=int, default=25,
                   help="Max LDPC decoding iterations")
    p.add_argument("--rrc-delay", type=int, default=50,
                   help="RRC filter delay (symbol periods). DEFAULT 50: a short TX "
                        "shaping filter (delay 5) causes ISI -> LDPC fails -> garbage. "
                        "dvbs2-rx app default is 5 and the dvbs2-tx app default "
                        "is 50; both TX and RX here use this single value.")
    p.add_argument("--fullscale", type=float, default=1.0,
                   help="Target full-scale TX IQ amplitude (None to disable)")
    p.add_argument("--gold-code", type=int, default=0, help="Gold code")
    p.add_argument("--pilots", choices=['on', 'off', 'auto'], default='on',
                   help="Whether PLFRAMEs contain pilots. DEFAULT 'on': measured "
                        "single-B210 lock rate jumps 0%% -> ~83%% per launch with "
                        "pilots on, because the pilotless fine carrier estimator "
                        "for short QPSK3/5 can only track +-122 Hz residual vs "
                        "+-677 Hz in pilot mode (pl_freq_sync). 'on' makes TX "
                        "insert pilots AND RX expect them.")
    p.add_argument("--multistream", choices=['on', 'off', 'auto'], default='off',
                   help="Enable MIS processing on the PL Sync block")
    p.add_argument("--pl-acm-vcm", action='store_true', default=False,
                   help="Force PL Sync into ACM/VCM mode")
    p.add_argument("--pl-freq-est-period", type=int, default=10,
                   help="Coarse freq offset estimation period in frames. DEFAULT 10 "
                        "(GRC default): on a clean single-B210 loopback it locks "
                        "marginally faster than 30 with the same 100%% rate; range is "
                        "fixed at +-0.5 cyc/sym regardless. Don't go to 1 (noisy).")
    p.add_argument("--rot-max-buf", type=int, default=None,
                   help="Target max output buffer size for the rotator block")
    p.add_argument("--debug", type=int, default=0, help="Debug level")
    p.add_argument("--mon-server", action="store_true", default=False,
                   help="Launch an HTTP receiver-stats server (JSON) for --full-stats")
    p.add_argument("--mon-port", type=int, default=8011, help="Monitor server port")
    p.add_argument("--loopback", action="store_true", default=False,
                   help="In-process TX->RX loopback. Skip the UHD device and pipe "
                        "TX baseband samples straight into the RX chain via a 1:1 "
                        "passthrough block.")

    # AGC (defaults mirror dvbs2-rx)
    p.add_argument("--agc-rate", type=float, default=1e-5, help="AGC update rate")
    p.add_argument("--agc-ref", type=float, default=1.0, help="AGC reference")
    p.add_argument("--agc-gain", type=float, default=1.0, help="AGC initial gain")

    # Symbol synchronizer (defaults mirror dvbs2-rx)
    p.add_argument("--sym-sync-impl", choices=['in-tree', 'oot'], default='oot',
                   help="Symbol synchronizer implementation")
    p.add_argument("--sym-sync-loop-bw", type=float, default=1e-3,
                   help="Symbol synchronizer loop bandwidth")
    p.add_argument("--sym-sync-damping", type=float, default=1.0,
                   help="Symbol synchronizer damping factor")
    p.add_argument("--sym-sync-rrc-nfilts", type=int, default=128,
                   help="Polyphase RRC subfilters in the symbol synchronizer")

    opts = p.parse_args()

    if opts.fullscale is not None and opts.fullscale <= 0:
        opts.fullscale = None

    return opts


class _MonHandler(BaseHTTPRequestHandler):
    tb = None
    def log_message(self, *a): pass                  # silent (no chatter)
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(self.tb.get_stats()).encode())

def _mon_server(tb, port):
    _MonHandler.tb = tb
    HTTPServer(("", port), _MonHandler).serve_forever()

def main():
    opts = argument_parser()

    tb = GrFullDuplex(opts)
    tb.start()

    if opts.mon_server:
        Thread(target=_mon_server, args=(tb, opts.mon_port), daemon=True).start()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # Block the main thread until interrupted.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()


if __name__ == '__main__':
    main()
