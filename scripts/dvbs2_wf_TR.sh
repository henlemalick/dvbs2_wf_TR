#!/bin/bash
# =============================================================================
# dvbs2_wf_TR - unified entry point
# Minimal CLI:  ./dvbs2_wf_TR.sh <operation> [profile] [extra_args...]
#
# Operations:
#   check              preflight: runtime, radio, config, rates
#   tx                 transmit (video or raw data)
#   rx                 receive  (video or raw data)
#   bench              self-test (TX feeds, RX reads, latency)
#   duplex             transmit + receive on one radio
#   rates              print auto-scaled MODCOD net-rate / resolution
#
# Profiles (config templates):
#   video       (default)  live camera (v4l2) to remote UDP sink
#   video_local            live camera to local display (ffplay)
#   video_remote           file source to remote UDP sink
#   raw_wo_udp             synthetic data loopback (no UDP)
#   raw_w_udp              external data via UDP injection/extraction
#
# Examples:
#   ./dvbs2_wf_TR.sh check
#   ./dvbs2_wf_TR.sh tx video
#   ./dvbs2_wf_TR.sh rx video --full-stats
#   ./dvbs2_wf_TR.sh duplex video
#   ./dvbs2_wf_TR.sh bench raw_wo_udp --secs 30
#   ./dvbs2_wf_TR.sh tx raw_w_udp --raw-udp-in 5005
# =============================================================================
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
BINARY="$HERE/bin/dvbs2_wf_TR"
CONFIG_DIR="$HERE/config"

# ---- parse operation and optional profile ----------------------------------
_OP="$1"
_PROFILE="${2:-video}"
_RAW_FLAG=()

case "$_PROFILE" in
    video)       _CONFIG="video.toml" ;;
    video_local) _CONFIG="video_local.toml" ;;
    video_remote)_CONFIG="video_remote.toml" ;;
    raw_wo_udp)  _CONFIG="raw_wo_udp.toml";  _RAW_FLAG=(--raw) ;;
    raw_w_udp)   _CONFIG="raw_w_udp.toml";   _RAW_FLAG=(--raw) ;;
    *)
        echo "error: unknown profile '$_PROFILE'"
        echo "valid profiles: video, video_local, video_remote, raw_wo_udp, raw_w_udp"
        exit 1
        ;;
esac
_CONFIG_PATH="$CONFIG_DIR/$_CONFIG"

# ---- shift off operation + profile, keep remaining args for binary ----------
if [ $# -ge 2 ]; then
    shift 2
elif [ $# -eq 1 ]; then
    shift 1
fi

# ---- validations -----------------------------------------------------------
if [ ! -f "$BINARY" ]; then
    echo "error: binary not found at $BINARY"
    echo "Run 'scripts/build_driver_binary.sh' first, or deploy via 'deploy.sh'."
    exit 1
fi
if [ ! -f "$_CONFIG_PATH" ]; then
    echo "error: config not found at $_CONFIG_PATH"
    exit 1
fi

# ---- auto-add --loopback for bench/duplex raw modes ------------------------
_EXTRA_ARGS=()
if [ "$_OP" = "bench" ] || [ "$_OP" = "duplex" ]; then
    if [ "$_PROFILE" = "raw_wo_udp" ] || [ "$_PROFILE" = "raw_w_udp" ]; then
        _EXTRA_ARGS+=(--loopback)
    fi
fi

exec "$BINARY" \
    --config "$_CONFIG_PATH" \
    "${_RAW_FLAG[@]}" \
    "${_EXTRA_ARGS[@]}" \
    "$_OP" \
    "$@"
