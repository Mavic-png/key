# -*- coding: utf-8 -*-
"""kb-frames.py -- render a depth CSV into a transparent video for Premiere Pro.

Multi-process and cached: static key art (cap gradient, rounded mask, labels) is
built once per worker, then each frame only composites the moving parts.

  python kb-frames.py depth.csv                       # 1080p60, all cores
  python kb-frames.py depth.csv --jobs 12 --fps 30
  python kb-frames.py depth.csv --keys W A S D LAlt Space --alias Alt=LAlt
  python kb-frames.py depth.csv --png-only            # PNG sequence only
"""

import argparse
import csv
import math
import multiprocessing as mp
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

FULL = 3.50
THEMES = {
    "ramp": dict(fill_a=(46, 200, 96), fill_b=(250, 204, 21), fill_c=(239, 68, 68)),
    "violet": dict(fill_a=(139, 92, 246), fill_b=(139, 92, 246), fill_c=(139, 92, 246)),
}

LAYOUTS = {
    "wasd": [
        ("W", 1.0, 0.0, 1.0),
        ("A", 0.0, 1.0, 1.0), ("S", 1.0, 1.0, 1.0), ("D", 2.0, 1.0, 1.0),
        ("LAlt", 0.0, 2.0, 1.25), ("Space", 1.25, 2.0, 2.75),
    ],
}
LABELS = {"LAlt": "Alt", "Space": "", "Bksp": "Back", "LShift": "Shift",
          "LCtrl": "Ctrl", "Enter": "Enter", "Tab": "Tab"}

_W = {}          # per-worker state


# ------------------------------------------------------------------ data

def load_csv(path, hold=True):
    with open(path, encoding="utf-8-sig") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    if not lines:
        return {}, 0.0
    header = [h.strip() for h in lines[0].strip().split(",")]
    lower = [h.lower() for h in header]

    if "key" not in lower and "mm" not in lower and len(header) >= 3:
        keycols = [(i, header[i]) for i in range(1, len(header))
                   if lower[i] not in ("wall", "t_s", "t")]
        frames, t0 = [], None
        for ln in lines[1:]:
            parts = ln.strip().split(",")
            if len(parts) < len(header):
                continue
            try:
                t = float(parts[0])
            except ValueError:
                continue
            if t0 is None:
                t0 = t
            row = {}
            for idx, name in keycols:
                try:
                    row[name] = float(parts[idx])
                except (ValueError, IndexError):
                    row[name] = 0.0
            frames.append((t - t0, row))
        if hold:
            prev = {}
            for _, row in frames:
                rising = [n for n, v in row.items()
                          if v > 0.05 and prev.get(n, 0.0) <= 0.05]
                if rising:
                    for n, v in list(row.items()):
                        if v <= 0.05 and prev.get(n, 0.0) > 0.05:
                            row[n] = prev[n]
                prev = dict(row)
        series = {}
        for t, row in frames:
            for name, mm in row.items():
                series.setdefault(name, []).append((t, mm))
        if not series:
            return {}, 0.0
        return series, max(v[-1][0] for v in series.values())

    rd = csv.DictReader(lines)
    cols = {c.lower(): c for c in (rd.fieldnames or [])}
    tcol = cols.get("t_s") or cols.get("t")
    rows = []
    for r in rd:
        try:
            rows.append((float(r[tcol]), (r.get("key") or "").strip(), float(r["mm"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return {}, 0.0
    t0 = rows[0][0]
    series = {}
    for t, key, mm in rows:
        series.setdefault(key, []).append((t - t0, mm))
    return series, max(v[-1][0] for v in series.values())


def sample_value(arr, t):
    if not arr:
        return 0.0
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid - 1
    i = max(0, hi)
    if i + 1 < len(arr):
        a, b = arr[i], arr[i + 1]
        if b[0] - a[0] <= 0.12:
            f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
            return a[1] + (b[1] - a[1]) * f
    return arr[i][1] if 0 <= t - arr[i][0] <= 0.12 else 0.0


# ------------------------------------------------------------------ art

def depth_color(ratio, theme):
    ratio = max(0.0, min(1.0, ratio))
    a, b, c = theme["fill_a"], theme["fill_b"], theme["fill_c"]
    if ratio < 0.5:
        f = ratio / 0.5
        return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))
    f = (ratio - 0.5) / 0.5
    return tuple(int(b[k] + (c[k] - b[k]) * f) for k in range(3))


def font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf", "arial.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def build_slot(label, x, y, w, unit, gap, theme, alpha, width_px):
    kx, ky = int(round(x * unit)), int(round(y * (unit + gap)))
    kw, kh = int(round(w * unit - gap)), int(round(unit - gap))
    r = max(6, int(unit * 0.14))

    mask = Image.new("L", (kw, kh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, kw - 1, kh - 1], radius=r, fill=255)

    body = Image.new("RGBA", (kw, kh))
    top, bottom = (54, 54, 64), (22, 22, 28)
    for yy in range(kh):
        f = yy / max(1, kh - 1)
        body.paste((int(top[0] + (bottom[0] - top[0]) * f),
                    int(top[1] + (bottom[1] - top[1]) * f),
                    int(top[2] + (bottom[2] - top[2]) * f), 255), (0, yy, kw, yy + 1))
    body.putalpha(Image.eval(mask, lambda v: int(v * alpha)))

    label_img = {}
    text = LABELS.get(label, label)
    if text:
        f = font(max(12, int(unit * 0.25)))
        for hot in (False, True):
            img = Image.new("RGBA", (kw, kh), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            col = (255, 255, 255, 255) if hot else (198, 202, 214, 235)
            tw = d.textlength(text, font=f)
            bb = f.getbbox(text)
            d.text(((kw - tw) / 2, (kh - (bb[3] - bb[1])) / 2 - bb[1]), text,
                   font=f, fill=col)
            label_img[hot] = img

    return dict(label=label, x=kx, y=ky, w=kw, h=kh, r=r, mask=mask, body=body,
                label_img=label_img, width=width_px, theme=theme)


def render_frame(idx, fps, size, slots, series):
    W, H = size
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    t = idx / float(fps)
    draw = ImageDraw.Draw(canvas)
    for s in slots:
        mm = sample_value(series.get(s["label"], []), t)
        ratio = max(0.0, min(1.0, mm / FULL))
        kw, kh = s["w"], s["h"]
        canvas.alpha_composite(s["body"], (s["x"], s["y"]))
        if ratio > 0.02:
            fh = max(1, int((kh - 2) * ratio))
            col = depth_color(ratio, s["theme"])
            fill = Image.new("RGBA", (kw, fh))
            for yy in range(fh):
                f = yy / max(1, fh - 1)
                fill.paste((col[0], col[1], col[2], int(205 + 45 * f)),
                           (0, yy, kw, yy + 1))
            fill.putalpha(s["mask"].crop((0, kh - fh, kw, kh)))
            canvas.alpha_composite(fill, (s["x"], s["y"] + kh - fh))
            line = Image.new("RGBA", (kw, 2), (255, 255, 255, int(160 * min(1, ratio * 3))))
            canvas.alpha_composite(line, (s["x"], s["y"] + kh - fh))
        border = (depth_color(ratio, s["theme"]) + (int(190 + 60 * ratio),)) \
            if ratio > 0.03 else (255, 255, 255, 46)
        draw.rounded_rectangle([s["x"], s["y"], s["x"] + kw - 1, s["y"] + kh - 1],
                               radius=s["r"], outline=border, width=s["width"])
        img = s["label_img"].get(ratio > 0.05)
        if img is not None:
            canvas.alpha_composite(img, (s["x"], s["y"]))
    return canvas


# ------------------------------------------------------------------ workers

def _init(cfg):
    _W.clear()
    _W.update(cfg)


FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def _segment_cmd(path, size, fps):
    W, H = size
    return [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "%dx%d" % (W, H),
            "-r", str(fps), "-i", "-",
            "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
            "-alpha_bits", "16", "-vendor", "apl0", path]


def _render_segment(job):
    start, end, path = job
    size, fps = _W["size"], _W["fps"]
    proc = subprocess.Popen(_segment_cmd(path, size, fps), stdin=subprocess.PIPE)
    written = 0
    error = None
    try:
        for i in range(start, end):
            img = render_frame(i, fps, size, _W["slots"], _W["series"])
            proc.stdin.write(img.tobytes())
            written += 1
    except BrokenPipeError:
        error = "ffmpeg closed the pipe"
    except Exception as exc:                     # noqa: BLE001 - worker must report, not crash
        error = "%r" % (exc,)
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        if error is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    rc = proc.wait()
    if rc != 0:
        error = error or "ffmpeg exit %d" % rc
    return (0 if error is None else -1), written, path, error


def _render_range(rng):
    start, end = rng
    out = _W["out"]
    for i in range(start, end):
        img = render_frame(i, _W["fps"], _W["size"], _W["slots"], _W["series"])
        img.save(os.path.join(out, "frame_%05d.png" % i), "PNG", compress_level=1)
    return end - start


def encode_segments(out_dir, movie, fps, n, jobs):
    """Encode the PNG sequence with one ffmpeg per chunk, then concat."""
    pattern = os.path.join(out_dir, "frame_%05d.png")
    per = int(math.ceil(n / float(jobs)))
    seg_dir = out_dir + "-seg"
    shutil.rmtree(seg_dir, ignore_errors=True)
    os.makedirs(seg_dir)
    procs, segs = [], []
    for k, start in enumerate(range(0, n, per)):
        end = min(n, start + per)
        seg = os.path.join(seg_dir, "seg_%02d.mov" % k)
        segs.append(seg)
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-framerate", str(fps), "-start_number", str(start), "-i", pattern,
               "-frames:v", str(end - start),
               "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
               "-alpha_bits", "16", "-vendor", "apl0", seg]
        procs.append(subprocess.Popen(cmd))
    failed = []
    for proc, seg in zip(procs, segs):
        rc = proc.wait()
        if rc != 0:
            failed.append((seg, rc))
    if failed:
        for seg, rc in failed:
            print("encode segment failed: %s (exit %d)" % (os.path.basename(seg), rc))
        shutil.rmtree(seg_dir, ignore_errors=True)
        return 4
    lst = os.path.join(seg_dir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for seg in segs:
            f.write("file '%s'\n" % seg.replace("\\", "/"))
    rc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                         "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                         movie]).returncode
    shutil.rmtree(seg_dir, ignore_errors=True)
    return rc


def encode_nvenc(out_dir, path, fps, jobs):
    """Fast preview via NVENC (no alpha) -- GPU encode, CPU PNG decode."""
    pattern = os.path.join(out_dir, "frame_%05d.png")
    return subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                           "-framerate", str(fps), "-i", pattern,
                           "-vf", "format=rgb24",
                           "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23",
                           "-pix_fmt", "yuv420p", path]).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--keys", nargs="*", default=["W", "A", "S", "D", "LAlt", "Space"])
    ap.add_argument("--layout", default="wasd", choices=sorted(LAYOUTS))
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--u", type=float, default=0)
    ap.add_argument("--theme", default="ramp", choices=sorted(THEMES))
    ap.add_argument("--alpha", type=float, default=0.86)
    ap.add_argument("--alias", default="")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0)
    ap.add_argument("--jobs", type=int, default=0, help="worker processes (0 = all cores)")
    ap.add_argument("--png-only", action="store_true")
    ap.add_argument("--pipe", action="store_true", default=False,
                    help="stream frames into per-worker ffmpeg (slower on ProRes)")
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--mp4", default="", help="also write a quick preview mp4 (GPU/NVENC)")
    args = ap.parse_args()

    W, H = [int(v) for v in args.size.lower().split("x")]
    series, dur = load_csv(args.csv)
    if not series:
        print("no usable rows in %s" % args.csv)
        return 2
    if args.alias:
        for pair in args.alias.split(","):
            if "=" in pair:
                src, dst = [x.strip() for x in pair.split("=", 1)]
                if src in series:
                    series[dst] = series.pop(src)
    if args.start or args.end:
        clipped = {}
        for key, arr in series.items():
            part = [(t - args.start, mm) for t, mm in arr
                    if t >= args.start and (not args.end or t <= args.end)]
            if part:
                clipped[key] = part
        series = clipped
        dur = (args.end - args.start) if args.end else max(
            (v[-1][0] for v in series.values()), default=0.0)

    theme = THEMES[args.theme]
    slots_def = [(label, x, y, w) for label, x, y, w in LAYOUTS[args.layout]
                 if label in args.keys]
    maxx = max(x + w for _, x, y, w in slots_def)
    maxy = max(y + 1 for _, x, y, w in slots_def)
    unit = args.u or min(W * 0.82 / maxx, H * 0.78 / maxy)
    gap = unit * 0.08
    ox = (W - maxx * unit) / 2.0
    oy = (H - (maxy * (unit + gap) - gap)) / 2.0
    width_px = max(1, int(unit * 0.014))
    slots = [build_slot(label, (ox + x * unit) / unit, (oy + y * (unit + gap)) / (unit + gap),
                        w, unit, gap, theme, args.alpha, width_px)
             for label, x, y, w in slots_def]

    out_dir = args.out or (os.path.splitext(os.path.basename(args.csv))[0] + "-frames")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    n = max(1, int(math.ceil(dur * args.fps)) + 1)
    # physical cores beat SMT here (drawing is memory-bound-ish)
    jobs = args.jobs or max(1, (os.cpu_count() or 4) // 2)
    print("%d frames (%dx%d, %d fps, %.1fs), %d keys, %d workers"
          % (n, W, H, args.fps, dur, len(slots), jobs))

    import time
    t0 = time.perf_counter()
    cfg = dict(out=out_dir, fps=args.fps, size=(W, H), slots=slots, series=series)
    if jobs <= 1 or n < 40:
        _init(cfg)
        _render_range((0, n))
    else:
        if args.pipe and not args.png_only:
            seg_dir = os.path.join(out_dir, "..", os.path.basename(out_dir) + "-seg")
            seg_dir = os.path.abspath(seg_dir)
            shutil.rmtree(seg_dir, ignore_errors=True)
            os.makedirs(seg_dir)
            chunk = int(math.ceil(n / float(jobs)))
            jobs_list = []
            for k, i in enumerate(range(0, n, chunk)):
                last = min(n, i + chunk)
                jobs_list.append((i, last, os.path.join(seg_dir, "seg_%02d.mov" % k)))
            failures = []
            with mp.Pool(jobs, initializer=_init, initargs=(cfg,)) as pool:
                done = 0
                for rc, got, path, err in pool.imap_unordered(_render_segment, jobs_list):
                    done += got
                    if rc != 0:
                        failures.append((path, err))
                    sys.stdout.write("\r  %d/%d frames encoded" % (done, n))
                    sys.stdout.flush()
            print()
            if failures:
                for path, err in failures:
                    print("segment failed: %s (%s)" % (os.path.basename(path), err))
                shutil.rmtree(seg_dir, ignore_errors=True)
                return 4
            elapsed = time.perf_counter() - t0
            print("rendered+encoded in %.1fs (%.0f fps)" % (elapsed, n / max(0.001, elapsed)))
            movie = out_dir + ".mov"
            lst = os.path.join(seg_dir, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for _, _, path in jobs_list:
                    f.write("file '%s'\n" % path.replace("\\", "/"))
            rc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                                 "-f", "concat", "-safe", "0", "-i", lst,
                                 "-c", "copy", movie]).returncode
            shutil.rmtree(seg_dir, ignore_errors=True)
            if rc != 0:
                print("concat failed")
                return 3
            print("video -> %s (ProRes 4444 with alpha)" % movie)
            return 0
        chunk = max(8, n // (jobs * 4))
        ranges = [(i, min(n, i + chunk)) for i in range(0, n, chunk)]
        with mp.Pool(jobs, initializer=_init, initargs=(cfg,)) as pool:
            done = 0
            for got in pool.imap_unordered(_render_range, ranges):
                done += got
                sys.stdout.write("\r  %d/%d frames" % (done, n))
                sys.stdout.flush()
        print()
    elapsed = time.perf_counter() - t0
    print("frames -> %s (%.1fs, %.0f fps)" % (out_dir, elapsed, n / max(0.001, elapsed)))
    if dur > 120 and not args.png_only:
        print("hint: %.0fs of footage -- --png-only renders in ~%.0fs and Premiere "
              "imports the sequence directly" % (dur, n / 250.0))

    if args.png_only:
        if args.mp4:
            import time as _t
            t_mp4 = _t.perf_counter()
            rc = encode_nvenc(out_dir, args.mp4, args.fps, jobs)
            if rc != 0:
                print("preview failed (ffmpeg exit %d)" % rc)
            else:
                print("preview -> %s (%.1fs, NVENC h264, no alpha)"
                      % (args.mp4, _t.perf_counter() - t_mp4))
        print("PNG sequence: %s" % out_dir)
        print("Premiere: import frame_00000.png, tick 'Image Sequence', %d fps" % args.fps)
        print("(sequence is ~4x faster than ProRes and keeps alpha)")
        return 0

    movie = out_dir + ".mov"
    print("encoding with %d parallel segments…" % jobs)
    t_enc = time.perf_counter()
    rc = encode_segments(out_dir, movie, args.fps, n, jobs)
    if rc != 0:
        print("ffmpeg failed")
        return 3
    print("encoded in %.1fs" % (time.perf_counter() - t_enc))
    if args.mp4:
        import time as _t
        t_mp4 = _t.perf_counter()
        rc = encode_nvenc(out_dir, args.mp4, args.fps, jobs)
        if rc != 0:
            print("preview failed (ffmpeg exit %d)" % rc)
        else:
            print("preview -> %s (%.1fs, NVENC h264, no alpha)"
                  % (args.mp4, _t.perf_counter() - t_mp4))
    if not args.keep_frames:
        shutil.rmtree(out_dir, ignore_errors=True)
    print("video -> %s (ProRes 4444 with alpha)" % movie)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
