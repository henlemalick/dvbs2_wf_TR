# dvbs2_wf_TR

Minimal DVB-S2 video/data link over a USRP B210 SDR on a Jetson platform.  
One binary, one folder, zero Python at runtime.

## Quick Start

```bash
# Preflight check
./dvbs2_wf_TR.sh check

# Transmit live camera video to remote UDP viewer
./dvbs2_wf_TR.sh tx video

# Receive video from remote link
./dvbs2_wf_TR.sh rx video

# Full duplex (TX + RX simultaneously on one B210)
./dvbs2_wf_TR.sh duplex video

# Raw data self-test (loopback, no radio)
./dvbs2_wf_TR.sh bench raw_wo_udp --secs 30

# Print MODCOD rates for current config
./dvbs2_wf_TR.sh rates video
```

## Profiles

| Profile       | Source         | Sink           | Use case                    |
|---------------|----------------|----------------|-----------------------------|
| `video`       | v4l2 camera    | remote UDP     | Live cam to viewer          |
| `video_local` | v4l2 camera    | local display  | Local monitoring            |
| `video_remote`| file source    | remote UDP     | File streaming to viewer    |
| `raw_wo_udp`  | synthetic      | stdout          | Self-test / bench           |
| `raw_w_udp`   | UDP injection  | UDP forward    | External data relay         |

## Deploy to Another Jetson

```bash
./scripts/deploy.sh TARGET_IP [ssh_user]
```

## Build from Source

```bash
./scripts/build_driver_binary.sh
```

## Full Documentation

See `REPO_GUIDE.md` for comprehensive architecture, build, and deployment docs.  
See `HANDS_OFF.md` for the session-autonomy context.
