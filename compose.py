# -*- coding: utf-8 -*-
"""compose.py -- put the depth overlay on top of the gameplay recording.

Both recordings start at the same moment (the depth tool starts when you press
record), so normally `--offset 0` is right.  If the overlay looks a few seconds
early/late, nudge it:

  --offset  3    the overlay starts 3s too early -> skip its first 3s
  --offset -3    the overlay starts 3s too late  -> delay it by 3s

  python compose.py game.mp4 overlay.mov -o final.mp4
  python compose.py game.mp4 overlay.mov -o final.mp4 --offset -1.5
  python compose.py game.mp4 overlay.mov -o final.mov --codec prores
"""

import argparse
import os
import shutil
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def probe_duration(path):
    exe = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe")
    if not os.path.exists(exe):
        exe = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe")
    out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", path],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("game")
    ap.add_argument("overlay")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--offset", type=float, default=0.0,
                    help="seconds to shift the overlay (+ skip head, - delay)")
    ap.add_argument("--codec", default="gpu", choices=["gpu", "prores", "x264"],
                    help="gpu = h264_nvenc (fast, no alpha needed here)")
    ap.add_argument("--crf", type=int, default=23)
    args = ap.parse_args()

    game_dur = probe_duration(args.game)
    ov_dur = probe_duration(args.overlay)
    off = args.offset

    if off >= 0:
        trim = "trim=start=%.3f,setpts=PTS-STARTPTS" % off
        usable = max(0.0, ov_dur - off)
    else:
        trim = "setpts=PTS+%.3f/TB" % (-off)
        usable = ov_dur

    dur = min(game_dur, usable) if game_dur else usable
    print("game %.2fs | overlay %.2fs | offset %+.2fs -> composite %.2fs"
          % (game_dur, ov_dur, off, dur))

    if off >= 0:
        prep = "trim=start=%.3f:duration=%.3f,setpts=PTS-STARTPTS" % (off, dur)
    else:
        prep = "setpts=PTS+%.3f/TB,trim=duration=%.3f" % (-off, dur)
    graph = "[1:v]%s[ov];[0:v][ov]overlay=0:0:eof_action=pass,format=yuv420p[v]" % prep

    if args.codec == "prores":
        enc = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]
    elif args.codec == "x264":
        enc = ["-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf)]
    else:
        enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(args.crf),
               "-pix_fmt", "yuv420p"]

    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", args.game, "-i", args.overlay,
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
           "-t", "%.3f" % dur] + enc + [args.out]
    print("running ffmpeg…")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print("ffmpeg failed")
        return 3
    size = os.path.getsize(args.out) / (1024 * 1024)
    print("done -> %s (%.0f MB)" % (args.out, size))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
