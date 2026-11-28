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
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FULL = 3.50
THEMES = {
    "ramp": dict(stops=[(0.0, (56, 189, 248)), (0.36, (52, 211, 153)),
                        (0.70, (251, 191, 36)), (1.0, (244, 63, 94))]),
    "smooth": dict(stops=[(0.0, (56, 189, 248)), (0.36, (52, 211, 153)),
                          (0.70, (251, 191, 36)), (1.0, (244, 63, 94))]),
    "cyan": dict(bot=(14, 165, 233, 199), top=(56, 189, 248, 133), line=(125, 211, 252), acc=(56, 189, 248)),
    "mint": dict(bot=(16, 185, 129, 199), top=(52, 211, 153, 128), line=(110, 231, 183), acc=(52, 211, 153)),
    "amber": dict(bot=(217, 119, 6, 204), top=(251, 191, 36, 133), line=(253, 230, 138), acc=(251, 191, 36)),
    "crimson": dict(bot=(225, 29, 72, 209), top=(244, 63, 94, 138), line=(253, 164, 175), acc=(244, 63, 94)),
    "violet": dict(bot=(126, 34, 206, 204), top=(168, 85, 247, 138), line=(216, 180, 254), acc=(168, 85, 247)),
}

LAYOUTS = {
    "wasd": [
        ("W", 1.0, 0.0, 1.0),
        ("A", 0.0, 1.0, 1.0), ("S", 1.0, 1.0, 1.0), ("D", 2.0, 1.0, 1.0),
        ("LAlt", 0.0, 2.0, 1.25), ("Space", 1.25, 2.0, 2.75),
    ],
    # compact cluster for overlaying on gameplay: movement keys, crouch, jump
    "hud": [
        ("Q", 0.0, 0.0, 1.0), ("W", 1.0, 0.0, 1.0), ("E", 2.0, 0.0, 1.0), ("R", 3.0, 0.0, 1.0),
        ("A", 0.0, 1.0, 1.0), ("S", 1.0, 1.0, 1.0), ("D", 2.0, 1.0, 1.0),
        ("LShift", 0.0, 2.0, 1.35), ("LCtrl", 1.35, 2.0, 1.2), ("LAlt", 2.55, 2.0, 1.15), ("Space", 3.7, 2.0, 2.6),
    ],
    "full": [
        ("Esc", 0.0, 0, 1), ("F1", 1.3, 0, 1), ("F2", 2.3, 0, 1), ("F3", 3.3, 0, 1), ("F4", 4.3, 0, 1),
        ("F5", 5.55, 0, 1), ("F6", 6.55, 0, 1), ("F7", 7.55, 0, 1), ("F8", 8.55, 0, 1),
        ("F9", 9.8, 0, 1), ("F10", 10.8, 0, 1), ("F11", 11.8, 0, 1), ("F12", 12.8, 0, 1),
        ("Del", 14.2, 0, 1), ("Home", 15.35, 0, 1),
        ("`", 0.0, 1, 1), ("1", 1, 1, 1), ("2", 2, 1, 1), ("3", 3, 1, 1), ("4", 4, 1, 1),
        ("5", 5, 1, 1), ("6", 6, 1, 1), ("7", 7, 1, 1), ("8", 8, 1, 1), ("9", 9, 1, 1),
        ("0", 10, 1, 1), ("-", 11, 1, 1), ("=", 12, 1, 1), ("Bksp", 13, 1, 2.15), ("PgUp", 15.35, 1, 1),
        ("Tab", 0.0, 2, 1.5), ("Q", 1.5, 2, 1), ("W", 2.5, 2, 1), ("E", 3.5, 2, 1), ("R", 4.5, 2, 1),
        ("T", 5.5, 2, 1), ("Y", 6.5, 2, 1), ("U", 7.5, 2, 1), ("I", 8.5, 2, 1), ("O", 9.5, 2, 1),
        ("P", 10.5, 2, 1), ("[", 11.5, 2, 1), ("]", 12.5, 2, 1), ("\\", 13.5, 2, 1.65), ("PgDn", 15.35, 2, 1),
        ("Caps", 0.0, 3, 1.75), ("A", 1.75, 3, 1), ("S", 2.75, 3, 1), ("D", 3.75, 3, 1), ("F", 4.75, 3, 1),
        ("G", 5.75, 3, 1), ("H", 6.75, 3, 1), ("J", 7.75, 3, 1), ("K", 8.75, 3, 1), ("L", 9.75, 3, 1),
        (";", 10.75, 3, 1), ("'", 11.75, 3, 1), ("Enter", 12.75, 3, 2.4), ("End", 15.35, 3, 1),
        ("LShift", 0.0, 4, 2.25), ("Z", 2.25, 4, 1), ("X", 3.25, 4, 1), ("C", 4.25, 4, 1), ("V", 5.25, 4, 1),
        ("B", 6.25, 4, 1), ("N", 7.25, 4, 1), ("M", 8.25, 4, 1), (",", 9.25, 4, 1), (".", 10.25, 4, 1),
        ("/", 11.25, 4, 1), ("RShift", 12.25, 4, 1.85), ("↑", 14.25, 4, 1),
        ("LCtrl", 0.0, 5, 1.25), ("LWin", 1.25, 5, 1.25), ("LAlt", 2.5, 5, 1.25),
        ("Space", 3.75, 5, 6.25), ("RAlt", 10.0, 5, 1.05), ("Fn", 11.05, 5, 1.05), ("RCtrl", 12.1, 5, 1.05),
        ("←", 13.25, 5, 1), ("↓", 14.25, 5, 1), ("→", 15.25, 5, 1),
    ],
}
LABELS = {"LAlt": "Alt", "RAlt": "Alt", "Space": "Space", "Bksp": "Back", "LShift": "Shift",
          "RShift": "Shift", "LCtrl": "Ctrl", "RCtrl": "Ctrl", "LWin": "Win",
          "Enter": "Enter", "Tab": "Tab"}

VIPER_PATH_D = (
    "M268.5 1268c-66.5-11-128.7-41.4-173.3-85-75-73.3-104.2-175-92.9-323.1 "
    "2.1-27 3.9-69.3 4.1-93.9 1.1-144.3.9-138.4 6.9-144.4 6.6-6.6 7.8-28.3 1.8-33.3"
    "-2.9-2.4-4.2-20-5.7-74.5-1.8-63.7-1.5-72.4 2.7-80.5 3.7-7.2 4.3-13.6 3-31.7"
    "-7-91.8-8.4-276.4-2.3-293.8 4.1-11.7 17.8-24.6 26-24.6 3.9 0 5.5 2.1 5.7 7.7"
    ".2 6.1.9 5.3 2.9-4l2.6-11.7 31.4-13.2C150.6 32 258.8-4 267.3-.8c3 1.1 4 7.5 4 25.6 "
    "0 21.5.5 24 4.9 23.2 2.7-.5 5.3-3.6 5.8-6.9 1.1-7.7 7.5-9.3 37.5-9.3 28.3 0 38.7 3 38.7 11.1 "
    "0 3.2 1.9 5.7 4.2 5.7 3.4 0 4.2-4.3 4.2-22.1 0-29.4 1.6-30.6 29.6-22.7 50.3 14.1 180.7 62.4 "
    "191.2 70.8 1.9 1.5 3.5 7.1 3.5 12.3s1 9.5 2.3 9.5c1.3 0 2.2-2.8 2.1-6.3-.4-11 10.2-9.4 21.5 3.1 "
    "8.1 8.9 10.5 14.3 12.2 27.5 3.7 27.6 2 154.9-3.2 243.5-8 136.1-6.8 246.9 4.1 398.2 "
    "14.8 205.3 9.9 262.2-29.6 341.6-41.4 83.1-117.7 139.9-215.6 160.3-30.7 6.4-86.6 7.8-116.4 2.9z"
)


def _eval_svg_path(d, steps=10):
    """Sample an SVG path (M/L/C/S/z) into polygon vertices."""
    toks = re.findall(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+)", d)
    pts, cx, cy, sx, sy, pcx, pcy, cmd, i = [], 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "", 0
    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]
            i += 1
            if cmd in ("z", "Z"):
                pts.append((sx, sy))
                cx, cy = sx, sy
            continue
        if cmd in ("M", "m"):
            x, y = float(toks[i]), float(toks[i + 1])
            i += 2
            cx, cy = (cx + x, cy + y) if cmd == "m" else (x, y)
            sx, sy, pcx, pcy = cx, cy, cx, cy
            pts.append((cx, cy))
            cmd = "l" if cmd == "m" else "L"
        elif cmd in ("L", "l"):
            x, y = float(toks[i]), float(toks[i + 1])
            i += 2
            cx, cy = (cx + x, cy + y) if cmd == "l" else (x, y)
            pcx, pcy = cx, cy
            pts.append((cx, cy))
        elif cmd in ("C", "c"):
            x1, y1, x2, y2, x3, y3 = [float(toks[i + k]) for k in range(6)]
            i += 6
            if cmd == "c":
                x1, y1, x2, y2, x3, y3 = cx + x1, cy + y1, cx + x2, cy + y2, cx + x3, cy + y3
            for s in range(1, steps + 1):
                t = s / float(steps)
                u = 1.0 - t
                pts.append((
                    u * u * u * cx + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                    u * u * u * cy + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
                ))
            pcx, pcy, cx, cy = x2, y2, x3, y3
        elif cmd in ("S", "s"):
            x2, y2, x3, y3 = [float(toks[i + k]) for k in range(4)]
            i += 4
            x1, y1 = 2 * cx - pcx, 2 * cy - pcy
            if cmd == "s":
                x2, y2, x3, y3 = cx + x2, cy + y2, cx + x3, cy + y3
            for s in range(1, steps + 1):
                t = s / float(steps)
                u = 1.0 - t
                pts.append((
                    u * u * u * cx + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                    u * u * u * cy + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
                ))
            pcx, pcy, cx, cy = x2, y2, x3, y3
        else:
            i += 1
    return pts

_W = {}          # per-worker state


# ------------------------------------------------------------------ data

def load_csv(path, hold=True, start=0.0, end=0.0):
    header = None
    lower = None
    keycols = None
    frames, t0 = [], None
    long_rows = []
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if header is None:
                header = [h.strip() for h in ln.strip().split(",")]
                lower = [h.lower() for h in header]
                if "key" not in lower and "mm" not in lower and len(header) >= 3:
                    keycols = [(i, header[i]) for i in range(1, len(header))
                               if lower[i] not in ("wall", "t_s", "t")]
                continue
            if keycols is not None:
                c1 = ln.find(",")
                if c1 <= 0:
                    continue
                try:
                    t = float(ln[:c1])
                except ValueError:
                    continue
                if t0 is None:
                    t0 = t
                rel = t - t0
                if start > 0.0 and rel < start - 0.25:
                    continue
                if end > 0.0 and rel > end + 0.25:
                    break
                parts = ln.rstrip("\r\n").split(",")
                if len(parts) < len(header):
                    continue
                row = {}
                for idx, name in keycols:
                    tok = parts[idx]
                    if tok == "0.00":
                        row[name] = 0.0
                    else:
                        try:
                            row[name] = float(tok)
                        except ValueError:
                            row[name] = 0.0
                frames.append((rel, row))
            else:
                long_rows.append(ln)
    if header is None:
        return {}, 0.0

    if keycols is not None:
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

    rd = csv.DictReader([",".join(header)] + long_rows)
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


def load_mouse_csv(path, start=0.0, end=0.0, speed=1.0):
    """Load mouse.csv and integrate normalized pad coordinates with smart lift-off recenter."""
    if not path or not os.path.isfile(path):
        return None
    span = 14800
    evts = []
    with open(path, encoding="utf-8-sig") as f:
        hdr = False
        for ln in f:
            if ln.lstrip().startswith("#"):
                m = re.search(r"mouse_span\s*=\s*(\d+)", ln)
                if m:
                    v = int(m.group(1))
                    if 500 <= v <= 200000:
                        span = v
                continue
            if not hdr:
                hdr = True
                continue
            p = ln.strip().split(",")
            if len(p) < 5:
                continue
            try:
                evts.append((float(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4])))
            except ValueError:
                continue
    if not evts:
        return None
    step_x = 2.0 / max(500.0, float(span))
    aspect = (306.0 - 44.0) / (254.0 - 84.0)
    tx = ty = px = py = 0.0
    last_move = -9.0
    prev_t = evts[0][0]
    dur_raw = evts[-1][0]
    # Sample at 100 Hz with live /overlay/mouse smoothing & tilt velocity
    n_pts = int(math.ceil(dur_raw * 100.0)) + 2
    idx = 0
    n_ev = len(evts)
    btn = 0
    samples = []
    for k in range(n_pts):
        t = k * 0.01
        dt = max(0.001, min(0.05, t - prev_t))
        prev_t = t
        sdx = sdy = w_acc = b_hit = 0
        while idx < n_ev and evts[idx][0] <= t + 1e-6:
            et, dx, dy, bm, wh = evts[idx]
            sdx += dx
            sdy += dy
            b_hit |= bm
            btn = bm
            if wh:
                w_acc += wh
            if dx or dy:
                last_move = et
            idx += 1
        if sdx or sdy:
            tx = max(-1.0, min(1.0, tx + sdx * step_x))
            ty = max(-1.0, min(1.0, ty + sdy * step_x * aspect))
        elif t - last_move > 0.20:
            kk = math.exp(-dt * (1.6 + min(1.4, math.hypot(tx, ty)) * 4.2))
            tx *= kk
            ty *= kk
        old_px = px
        alpha = 1.0 - math.exp(-dt * 24.0)
        px += (tx - px) * alpha
        py += (ty - py) * alpha
        vx = (px - old_px) / max(0.004, dt)
        moving = 1 if (t - last_move) < 0.18 else 0
        speed_r = min(1.0, math.hypot(tx - px, ty - py) * 3.5 + (0.5 if ((btn | b_hit) & 1) else 0.0))
        if t >= start and (not end or t <= end + 0.02):
            factor = 1.0 / max(1e-6, speed)
            samples.append(((t - start) * factor, px, py, btn | b_hit, w_acc, vx, moving, speed_r))
    return samples if samples else None


def sample_mouse(samples, t):
    if not samples:
        return 0.0, 0.0, 0, 0, 0, 0.0, 0, 0.0
    lo, hi = 0, len(samples) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid - 1
    i = max(0, min(len(samples) - 1, hi))
    j = min(len(samples) - 1, i + 1)
    dt = samples[j][0] - samples[i][0]
    f = max(0.0, min(1.0, (t - samples[i][0]) / dt)) if dt > 1e-6 else 0.0
    x = samples[i][1] + (samples[j][1] - samples[i][1]) * f
    y = samples[i][2] + (samples[j][2] - samples[i][2]) * f
    vx = samples[i][5] + (samples[j][5] - samples[i][5]) * f
    return x, y, samples[i][3], samples[i][4], i, vx, samples[i][6], samples[i][7]


# ------------------------------------------------------------------ art

def depth_color(ratio, theme):
    ratio = max(0.0, min(1.0, ratio))
    stops = theme.get("stops")
    if stops:
        for i in range(1, len(stops)):
            if ratio <= stops[i][0]:
                t = (ratio - stops[i - 1][0]) / max(1e-6, stops[i][0] - stops[i - 1][0])
                f = 0.5 - 0.5 * math.cos(math.pi * t)
                a, b = stops[i - 1][1], stops[i][1]
                return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))
        return stops[-1][1]
    return theme["acc"]


def theme_style(ratio, theme):
    """Return (top_rgba, bot_rgba, line_rgb, acc_rgb) matching COMMON_THEME_JS."""
    if theme.get("stops"):
        c_top = depth_color(ratio, theme)
        c_bot = depth_color(max(0.0, ratio * 0.55), theme)
        line = tuple(min(255, c_top[k] + 36) for k in range(3))
        return (c_top[0], c_top[1], c_top[2], 143), (c_bot[0], c_bot[1], c_bot[2], 204), line, c_top
    return theme["top"], theme["bot"], theme["line"], theme["acc"]


def font(size):
    for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def mono_font(size):
    for name in ("consolab.ttf", "CascadiaMono.ttf", "segoeuib.ttf", "arialbd.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _get_mm_font(size):
    cache = _W.setdefault("_mm_fonts", {})
    f = cache.get(size)
    if f is None:
        f = mono_font(size)
        cache[size] = f
    return f


def build_slot(label, x, y, w, unit, gap, theme, alpha, width_px):
    kx, ky = int(round(x * unit)), int(round(y * (unit + gap)))
    kw, kh = int(round(w * unit - gap)), int(round(unit - gap))
    r = max(5, int(round(unit * 0.125)))

    mask2 = Image.new("L", (kw * 2, kh * 2), 0)
    ImageDraw.Draw(mask2).rounded_rectangle([0, 0, kw * 2 - 1, kh * 2 - 1], radius=r * 2, fill=255)
    mask = mask2.resize((kw, kh), Image.LANCZOS)

    body = Image.new("RGBA", (kw, kh), (14, 16, 20, 255))
    body.putalpha(Image.eval(mask, lambda v: int(v * max(0.05, min(1.0, alpha)))))

    label_img = {}
    text = LABELS.get(label, label)
    if text:
        f = font(max(12, int(round(unit * 0.24))))
        sh = max(1, int(round(unit * 0.015)))
        for hot in (False, True):
            img = Image.new("RGBA", (kw, kh), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            col = (255, 255, 255, 255) if hot else (255, 255, 255, 184)
            shc = (0, 0, 0, 204) if hot else (0, 0, 0, 166)
            tw = d.textlength(text, font=f)
            bb = f.getbbox(text)
            tx, ty = (kw - tw) / 2, (kh - (bb[3] - bb[1])) / 2 - bb[1]
            d.text((tx, ty + sh), text, font=f, fill=shc)
            d.text((tx, ty), text, font=f, fill=col)
            label_img[hot] = img

    return dict(label=label, x=kx, y=ky, w=kw, h=kh, r=r, mask=mask, body=body,
                label_img=label_img, width=width_px, theme=theme,
                line_h=max(2, int(round(unit * 0.026))),
                mm_fs=max(11, int(round(unit * 0.17))),
                mm_pad_x=max(5, int(round(unit * 0.085))),
                mm_pad_y=max(4, int(round(unit * 0.07))))


def build_mouse_art(x, y, pw, ph, alpha, theme, mopacity=0.90):
    """Pre-render 1:1 live-style Zowie G-SR III pad & Razer Viper V4 Pro SVG layers."""
    pw, ph = max(60, int(round(pw))), max(50, int(round(ph)))
    sc = pw / 306.0
    rad = max(6, int(round(14.0 * sc)))
    # 2x supersampled mousepad matching MOUSE_OVERLAY_PAGE CSS
    p2 = Image.new("RGBA", (pw * 2, ph * 2), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(p2)
    d2.rounded_rectangle([0, 0, pw * 2 - 1, ph * 2 - 1], radius=rad * 2,
                         fill=(14, 16, 20, int(255 * max(0.08, min(1.0, alpha)))),
                         outline=(255, 255, 255, 36), width=max(2, int(round(2 * sc))))
    pad = p2.resize((pw, ph), Image.LANCZOS)
    pd = ImageDraw.Draw(pad)
    pd.line([(pw // 2, 0), (pw // 2, ph)], fill=(255, 255, 255, 16), width=max(1, int(round(sc))))
    pd.line([(0, ph // 2), (pw, ph // 2)], fill=(255, 255, 255, 16), width=max(1, int(round(sc))))
    c_len = max(8, int(round(10 * sc)))
    m = max(4, int(round(6 * sc)))
    w_c = max(1, int(round(1.5 * sc)))
    for (ax, ay, bx, by, cx, cy) in (
        (m, m + c_len, m, m, m + c_len, m),
        (pw - m - c_len, m, pw - m, m, pw - m, m + c_len),
        (m, ph - m - c_len, m, ph - m, m + c_len, ph - m),
        (pw - m - c_len, ph - m, pw - m, ph - m, pw - m, ph - m - c_len),
    ):
        pd.line([(ax, ay), (bx, by), (cx, cy)], fill=(255, 255, 255, 56), width=w_c)

    # Exact Viper V4 Pro size (44x84 at 306x254) rendered from SVG viewBox(-22,-18,682,1307)
    mw = max(20, int(round(44.0 * sc)))
    mh = max(38, int(round(84.0 * sc)))
    ss = 2
    sw, sh = mw * ss, mh * ss
    def spt(vx, vy):
        return ((vx + 22.0) / 682.0 * sw, (vy + 18.0) / 1307.0 * sh)

    raw_poly = [spt(vx, vy) for vx, vy in _eval_svg_path(VIPER_PATH_D, steps=10)]
    shell_mask2 = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(shell_mask2).polygon(raw_poly, fill=255)
    # Vertical gradient: #222631 (0%) -> #151820 (55%) -> #0f1117 (100%)
    grad2 = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    for yy in range(sh):
        f = yy / max(1, sh - 1)
        if f <= 0.55:
            u = f / 0.55
            c = (int(0x22 + (0x15 - 0x22) * u), int(0x26 + (0x18 - 0x26) * u),
                 int(0x31 + (0x20 - 0x31) * u), 235)
        else:
            u = (f - 0.55) / 0.45
            c = (int(0x15 + (0x0f - 0x15) * u), int(0x18 + (0x11 - 0x18) * u),
                 int(0x20 + (0x17 - 0x20) * u), 238)
        grad2.paste(c, (0, yy, sw, yy + 1))
    grad2.putalpha(Image.eval(shell_mask2, lambda v: int(v * 0.93)))
    # Curved L/R button masks clipped to Viper silhouette
    l_pts = [spt(0, 0), spt(312, 0), spt(312, 486)]
    r_pts = [spt(326, 0), spt(640, 0), spt(640, 462)]
    for k in range(1, 9):
        t = k / 8.0; u = 1.0 - t
        l_pts.append(spt(u * u * 312 + 2 * u * t * 160 + t * t * 0,
                         u * u * 486 + 2 * u * t * 510 + t * t * 462))
        r_pts.append(spt(u * u * 640 + 2 * u * t * 480 + t * t * 326,
                         u * u * 462 + 2 * u * t * 510 + t * t * 486))
    l_mask2 = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(l_mask2).polygon(l_pts, fill=255)
    r_mask2 = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(r_mask2).polygon(r_pts, fill=255)
    l_mask = Image.composite(l_mask2, Image.new("L", (sw, sh), 0), shell_mask2).resize((mw, mh), Image.LANCZOS)
    r_mask = Image.composite(r_mask2, Image.new("L", (sw, sh), 0), shell_mask2).resize((mw, mh), Image.LANCZOS)

    # Outer outline + curved seams
    stroke_w = max(2, int(round(18.0 / 682.0 * sw)))
    seam_w = max(1, int(round(12.0 / 682.0 * sw)))
    gd = ImageDraw.Draw(grad2)
    seam_pts = []
    for k in range(17):
        t = k / 16.0; u = 1.0 - t
        if t <= 0.5:
            q = t * 2.0; qu = 1.0 - q
            seam_pts.append(spt(qu * qu * 10 + 2 * qu * q * 165 + q * q * 319,
                                qu * qu * 462 + 2 * qu * q * 510 + q * q * 488))
        else:
            q = (t - 0.5) * 2.0; qu = 1.0 - q
            seam_pts.append(spt(qu * qu * 319 + 2 * qu * q * 473 + q * q * 628,
                                qu * qu * 488 + 2 * qu * q * 510 + q * q * 462))
    gd.line(seam_pts, fill=(255, 255, 255, 56), width=seam_w)
    gd.line([spt(319, 32), spt(319, 488)], fill=(255, 255, 255, 56), width=seam_w)
    gd.line(raw_poly, fill=(255, 255, 255, 134), width=stroke_w, joint="curve")

    shell_base = grad2.resize((mw, mh), Image.LANCZOS)
    shadow_pad = max(12, int(round(14 * sc)))
    sh_alpha = shell_mask2.resize((mw, mh), Image.LANCZOS)
    sh_img = Image.new("RGBA", (mw + shadow_pad * 2, mh + shadow_pad * 2), (0, 0, 0, 0))
    sh_fill = Image.new("RGBA", (mw, mh), (0, 0, 0, 140))
    sh_fill.putalpha(Image.eval(sh_alpha, lambda v: int(v * 0.55)))
    sh_img.paste(sh_fill, (shadow_pad, shadow_pad + max(2, int(round(4 * sc)))))
    sh_img = sh_img.filter(ImageFilter.GaussianBlur(radius=max(2.0, 5.0 * sc)))

    return dict(x=int(round(x)), y=int(round(y)), w=pw, h=ph, mw=mw, mh=mh, sc=sc,
                inset=max(4, int(round(10.0 * sc))), pad=pad, theme=theme,
                alpha=alpha, mopacity=max(0.15, min(1.0, float(mopacity))),
                shell_base=shell_base, l_mask=l_mask, r_mask=r_mask,
                shadow_img=sh_img, shadow_pad=shadow_pad,
                spt_scale=(mw / 682.0, mh / 1307.0, 22.0, 18.0))


def draw_mouse_overlay(canvas, t, mart, samples):
    """Composite mousepad, 0.62s smooth trail, and tilted Razer Viper V4 Pro SVG sprite."""
    if not mart or not samples:
        return
    px, py, pw, ph = mart["x"], mart["y"], mart["w"], mart["h"]
    mw, mh, inset, sc = mart["mw"], mart["mh"], mart["inset"], mart["sc"]
    canvas.alpha_composite(mart["pad"], (px, py))

    nx, ny, bmask, wh, idx, vx, moving, speed_r = sample_mouse(samples, t)
    def to_xy(px_v, py_v):
        max_ox = max(4.0, (pw - mw) * 0.5 - inset)
        max_oy = max(4.0, (ph - mh) * 0.5 - inset)
        return pw * 0.5 + px_v * max_ox, ph * 0.5 + py_v * max_oy

    acc = depth_color(speed_r, mart["theme"])
    trail_img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    td = ImageDraw.Draw(trail_img)
    win = 0.62
    k0 = idx
    while k0 > 0 and (t - samples[k0 - 1][0]) <= win:
        k0 -= 1
    pts_tr = [(to_xy(samples[k][1], samples[k][2]), max(0.0, 1.0 - (t - samples[k][0]) / win))
              for k in range(k0, idx + 1) if samples[k][6]]
    if pts_tr:
        for k in range(1, len(pts_tr)):
            p0, _ = pts_tr[k - 1]
            p1, age = pts_tr[k]
            if age > 0.02:
                td.line([p0, p1], fill=(acc[0], acc[1], acc[2], int(174 * age)),
                        width=max(2, int(round(max(1.4, age * 3.5 * sc)))))
    cx, cy = to_xy(nx, ny)
    canvas.alpha_composite(trail_img, (px, py))

    # Build Viper V4 Pro sprite matching MOUSE_OVERLAY_PAGE SVG
    m_img = mart["shell_base"].copy()
    if bmask & 1:
        ov = Image.new("RGBA", (mw, mh), (acc[0], acc[1], acc[2], 199))
        ov.putalpha(Image.eval(mart["l_mask"], lambda v: int(v * 0.78)))
        m_img.alpha_composite(ov)
    if bmask & 2:
        ov = Image.new("RGBA", (mw, mh), (acc[0], acc[1], acc[2], 199))
        ov.putalpha(Image.eval(mart["r_mask"], lambda v: int(v * 0.78)))
        m_img.alpha_composite(ov)

    sx, sy, ox0, oy0 = mart["spt_scale"]
    def vp(vx_c, vy_c):
        return (vx_c + ox0) * sx, (vy_c + oy0) * sy

    md = ImageDraw.Draw(m_img)
    # Scroll wheel (x=284, y=138, w=70, h=184, rx=35)
    w_on = bool((bmask & 4) or wh != 0)
    wx0, wy0 = vp(284, 138)
    wx1, wy1 = vp(284 + 70, 138 + 184)
    md.rounded_rectangle([wx0, wy0, wx1, wy1], radius=max(2, int(round(35 * sx))),
                         fill=(acc[0], acc[1], acc[2], 240) if w_on else (255, 255, 255, 42),
                         outline=(255, 255, 255, 255 if w_on else 124), width=max(1, int(round(12 * sx))))
    # Side buttons X2 (y=430), X1 (y=618)
    for bit, y_top in ((16, 430), (8, 618)):
        on = bool(bmask & bit)
        bx0, by0 = vp(-6, y_top)
        bx1, by1 = vp(26, y_top + 156)
        md.rounded_rectangle([bx0, by0, bx1, by1], radius=max(1, int(round(14 * sx))),
                             fill=(acc[0], acc[1], acc[2], 245) if on else (255, 255, 255, 42),
                             outline=(255, 255, 255, 255 if on else 108), width=max(1, int(round(10 * sx))))
    # Center LED dot (319, 560, r=15)
    lx, ly = vp(319, 560)
    lr = max(2.0, 15.0 * sx)
    hot_led = bool(moving or (bmask & 3))
    md.ellipse([lx - lr, ly - lr, lx + lr, ly + lr],
               fill=(acc[0], acc[1], acc[2], 255 if hot_led else 128))

    # Apply independent mopacity + soft drop shadow + velocity tilt (-6..+6 deg)
    sp = mart["shadow_pad"]
    comp = mart["shadow_img"].copy()
    if mart["mopacity"] < 0.999:
        ma = m_img.getchannel("A")
        m_img.putalpha(Image.eval(ma, lambda v: int(v * mart["mopacity"])))
        ca = comp.getchannel("A")
        comp.putalpha(Image.eval(ca, lambda v: int(v * mart["mopacity"])))
    comp.alpha_composite(m_img, (sp, sp))
    tilt = max(-6.0, min(6.0, vx * 1.2))
    if abs(tilt) > 0.05:
        comp = comp.rotate(-tilt, resample=Image.BILINEAR, expand=True)
    cw, ch = comp.size
    canvas.alpha_composite(comp, (int(round(px + cx - cw * 0.5)),
                                  int(round(py + cy - ch * 0.5))))


def render_frame(idx, fps, size, slots, series, bg=None, full=FULL,
                 mouse_art=None, mouse_samples=None, mm_mode="off"):
    W, H = size
    canvas = Image.new("RGBA", (W, H), bg if bg else (0, 0, 0, 0))
    t = idx / float(fps)
    full_mm = max(0.5, float(full or FULL))
    for s in slots:
        mm = sample_value(series.get(s["label"], []), t)
        ratio = max(0.0, min(1.0, mm / full_mm))
        kw, kh = s["w"], s["h"]
        tile = s["body"].copy()
        active = ratio > 0.04
        hot = ratio > 0.06
        if active:
            fh = max(1, int(round((kh - 1) * ratio)))
            top_c, bot_c, line_c, _ = theme_style(ratio, s["theme"])
            fill = Image.new("RGBA", (kw, fh))
            for yy in range(fh):
                f = yy / max(1, fh - 1)
                fill.paste(tuple(int(top_c[k] + (bot_c[k] - top_c[k]) * f) for k in range(4)),
                           (0, yy, kw, yy + 1))
            fill.putalpha(Image.composite(
                s["mask"].crop((0, kh - fh, kw, kh)),
                Image.new("L", (kw, fh), 0),
                fill.getchannel("A"),
            ))
            tile.alpha_composite(fill, (0, kh - fh))
            lh = min(fh, s.get("line_h", 2))
            line = Image.new("RGBA", (kw, lh), (line_c[0], line_c[1], line_c[2], 235))
            line.putalpha(s["mask"].crop((0, kh - fh, kw, kh - fh + lh)))
            tile.alpha_composite(line, (0, kh - fh))
        td = ImageDraw.Draw(tile)
        td.rounded_rectangle([0, 0, kw - 1, kh - 1], radius=s["r"],
                             outline=(255, 255, 255, 34), width=max(1, s["width"]))
        img = s["label_img"].get(hot)
        if img is not None:
            tile.alpha_composite(img, (0, 0))
        if mm_mode in ("press", "always") and (mm_mode == "always" or ratio > 0.08):
            mf = _get_mm_font(s.get("mm_fs", 14))
            mtxt = "%.1f" % mm
            tw = td.textlength(mtxt, font=mf)
            bb = mf.getbbox(mtxt)
            th = bb[3] - bb[1]
            tx = kw - s.get("mm_pad_x", 6) - tw
            ty = kh - s.get("mm_pad_y", 5) - th - bb[1]
            sh = max(1, s.get("mm_fs", 14) // 10)
            td.text((tx, ty + sh), mtxt, font=mf, fill=(0, 0, 0, 192))
            fg = (255, 255, 255, 230) if hot else (255, 255, 255, 158)
            td.text((tx, ty), mtxt, font=mf, fill=fg)
        if active:
            sc_k = 1.0 - ratio * 0.035
            sw_k = max(2, int(round(kw * sc_k)))
            sh_k = max(2, int(round(kh * sc_k)))
            if sw_k != kw or sh_k != kh:
                tile = tile.resize((sw_k, sh_k), Image.BILINEAR)
            dx = s["x"] + int(round((kw - sw_k) * 0.5))
            dy = s["y"] + int(round((kh - sh_k) * 0.5))
        else:
            dx, dy = s["x"], s["y"]
        canvas.alpha_composite(tile, (dx, dy))
    if mouse_art and mouse_samples:
        draw_mouse_overlay(canvas, t, mouse_art, mouse_samples)
    return canvas


# ------------------------------------------------------------------ workers

def _init(cfg):
    _W.clear()
    _W.update(cfg)


def _default_ffmpeg():
    env = os.environ.get("FFMPEG")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "ffmpeg.exe"),
        os.path.join(here, "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(os.path.expanduser("~"), "ffmpeg", "bin", "ffmpeg.exe"),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    return "ffmpeg"


FFMPEG = _default_ffmpeg()


def _segment_cmd(path, size, fps, ffmpeg=None):
    W, H = size
    return [ffmpeg or FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "%dx%d" % (W, H),
            "-r", str(fps), "-i", "-",
            "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
            "-alpha_bits", "16", "-vendor", "apl0", path]


def _render_segment(job):
    start, end, path = job
    size, fps = _W["size"], _W["fps"]
    proc = subprocess.Popen(_segment_cmd(path, size, fps, _W.get("ffmpeg")), stdin=subprocess.PIPE)
    written = 0
    error = None
    try:
        for i in range(start, end):
            img = render_frame(i, fps, size, _W["slots"], _W["series"],
                               _W.get("bg"), _W.get("full", FULL),
                               _W.get("mouse_art"), _W.get("mouse_samples"),
                               _W.get("mm", "off"))
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
        img = render_frame(i, _W["fps"], _W["size"], _W["slots"], _W["series"],
                           _W.get("bg"), _W.get("full", FULL),
                           _W.get("mouse_art"), _W.get("mouse_samples"),
                           _W.get("mm", "off"))
        img.save(os.path.join(out, "frame_%05d.png" % i), "PNG", compress_level=1)
    return end - start


def encode_webm(out_dir, movie, fps, jobs):
    """Encode PNG sequence into lightweight transparent WebM (VP9 yuva420p)."""
    pattern = os.path.join(out_dir, "frame_%05d.png")
    threads = max(2, min(16, int(jobs or 4) * 2))
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(fps), "-i", pattern,
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0", "-b:v", "0", "-crf", "30",
        "-deadline", "good", "-cpu-used", "4",
        "-row-mt", "1", "-threads", str(threads),
        movie,
    ]
    return subprocess.run(cmd).returncode


def encode_segments(out_dir, movie, fps, n, jobs):
    """Encode the PNG sequence with one ffmpeg per chunk, then concat."""
    pattern = os.path.join(out_dir, "frame_%05d.png")
    per = int(math.ceil(n / float(jobs)))
    seg_dir = out_dir + "-seg"
    shutil.rmtree(seg_dir, ignore_errors=True)
    os.makedirs(seg_dir)
    procs, segs = [], []
    try:
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
    except Exception as exc:
        # ffmpeg missing or refusing to start mid-way: reap what we already started
        print("failed to start encoder segment: %r" % (exc,))
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        shutil.rmtree(seg_dir, ignore_errors=True)
        return 5
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
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--layout", default="wasd", choices=sorted(LAYOUTS))
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--u", type=float, default=0)
    ap.add_argument("--theme", default="ramp", choices=sorted(THEMES))
    ap.add_argument("--full", type=float, default=FULL,
                    help="full switch travel in mm (default 3.50)")
    ap.add_argument("--ffmpeg", default="", help="path to ffmpeg executable")
    ap.add_argument("--alpha", type=float, default=0.86)
    ap.add_argument("--kb-alpha", type=float, default=-1.0,
                    help="override keyboard HUD opacity (defaults to --alpha)")
    ap.add_argument("--mouse-alpha", type=float, default=-1.0,
                    help="override mousepad HUD opacity (defaults to --alpha)")
    ap.add_argument("--mopacity", type=float, default=0.90,
                    help="independent mouse body opacity (default 0.90)")
    ap.add_argument("--mm", default="off", choices=("off", "press", "always"),
                    help="keycap mm depth readout: off | press | always")
    ap.add_argument("--alias", default="")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time scale: 0.5 = half speed, 2 = double speed")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0)
    ap.add_argument("--jobs", type=int, default=0, help="worker processes (0 = all cores)")
    ap.add_argument("--bg", default="", help="solid background colour, e.g. #101014")
    ap.add_argument("--mouse", default="", help="optional mouse.csv for synced mousepad overlay")
    ap.add_argument("--mouse-layout", default="side", choices=("side", "stack", "only", "stage"),
                    help="keyboard + mouse placement: side | stack | only | stage")
    ap.add_argument("--kb-box", default="", help="normalized stage rect x,y,w,h for keyboard")
    ap.add_argument("--mouse-box", default="", help="normalized stage rect x,y,w,h for mouse")
    ap.add_argument("--format", default="prores", choices=("prores", "webm", "png"),
                    help="output format: prores (.mov) | webm (VP9 alpha) | png")
    ap.add_argument("--png-only", action="store_true")
    ap.add_argument("--pipe", action="store_true", default=False,
                    help="stream frames into per-worker ffmpeg (slower on ProRes)")
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--mp4", default="", help="also write a quick preview mp4 (GPU/NVENC)")
    args = ap.parse_args()
    global FFMPEG
    if args.ffmpeg:
        FFMPEG = args.ffmpeg
    full_mm = max(1.0, min(5.0, float(args.full)))

    W, H = [int(v) for v in args.size.lower().split("x")]
    series, dur = load_csv(args.csv, start=args.start, end=args.end)
    if not series and not args.mouse:
        print("no usable rows in %s" % args.csv)
        return 2
    for src_m, dst_m in (("Shift", "LShift"), ("Alt", "LAlt"), ("Ctrl", "LCtrl")):
        if src_m in series and dst_m not in series:
            series[dst_m] = series[src_m]
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

    if abs(args.speed - 1.0) > 1e-6:
        factor = 1.0 / args.speed          # slower playback = longer timeline
        series = {k: [(t * factor, mm) for t, mm in v] for k, v in series.items()}
        dur = dur * factor

    mouse_samples = load_mouse_csv(args.mouse, start=args.start, end=args.end, speed=args.speed) if args.mouse else None
    if mouse_samples and dur <= 0.0:
        dur = mouse_samples[-1][0]
    mlay = args.mouse_layout if mouse_samples else "none"
    theme = THEMES[args.theme]
    layout_defs = LAYOUTS[args.layout]
    if mlay == "only":
        slots_def = []
    elif args.keys:
        want = set(args.keys)
        slots_def = [(label, x, y, w) for label, x, y, w in layout_defs if label in want]
    elif args.layout == "full":
        slots_def = list(layout_defs)
    else:
        default_keys = {"W", "A", "S", "D", "LAlt", "LCtrl", "LShift", "Space"}
        slots_def = [(label, x, y, w) for label, x, y, w in layout_defs if label in default_keys]
    if not slots_def and mlay != "only":
        print("no matching keys for layout %s" % args.layout)
        return 2

    def _parse_box(spec):
        if not spec:
            return None
        try:
            vals = [float(v) for v in spec.split(",")]
            if len(vals) == 4 and vals[2] > 0.01 and vals[3] > 0.01:
                return (max(0.0, min(0.95, vals[0])), max(0.0, min(0.95, vals[1])),
                        max(0.04, min(1.0, vals[2])), max(0.04, min(1.0, vals[3])))
        except Exception:
            pass
        return None

    kb_box = _parse_box(args.kb_box)
    ms_box = _parse_box(args.mouse_box)
    kb_alpha = args.alpha if args.kb_alpha < 0.0 else max(0.05, min(1.0, float(args.kb_alpha)))
    ms_alpha = args.alpha if args.mouse_alpha < 0.0 else max(0.05, min(1.0, float(args.mouse_alpha)))

    mouse_art = None
    slots = []
    pad_ratio = 470.0 / 390.0
    if mlay == "only":
        if ms_box is not None:
            mw, mh = ms_box[2] * W, ms_box[3] * H
            ph = min(mh, mw / pad_ratio)
            pw = ph * pad_ratio
            mouse_art = build_mouse_art(ms_box[0] * W, ms_box[1] * H, pw, ph, ms_alpha, theme, args.mopacity)
        else:
            ph = min(H * 0.78, (W * 0.82) / pad_ratio)
            pw = ph * pad_ratio
            mouse_art = build_mouse_art((W - pw) / 2.0, (H - ph) / 2.0, pw, ph, ms_alpha, theme, args.mopacity)
    else:
        maxx = max(x + w for _, x, y, w in slots_def)
        maxy = max(y + 1 for _, x, y, w in slots_def)
        if mlay == "stage":
            if kb_box is not None:
                bw, bh = kb_box[2] * W, kb_box[3] * H
                unit = args.u or min(bw / maxx, bh / (maxy * 1.11 - 0.11))
                ox, oy = kb_box[0] * W, kb_box[1] * H
            else:
                unit = args.u or min((W * 0.34) / maxx, (H * 0.28) / (maxy * 1.11 - 0.11))
                ox, oy = W * 0.03, H * 0.66
            gap = unit * 0.11
            if mouse_samples:
                bx, by, bwu, bhu = ms_box if ms_box is not None else (0.76, 0.64, 0.21, 0.27)
                ph = min(bhu * H, (bwu * W) / pad_ratio)
                pw = ph * pad_ratio
                mouse_art = build_mouse_art(bx * W, by * H, pw, ph, ms_alpha, theme, args.mopacity)
        elif kb_box is not None:
            bw, bh = kb_box[2] * W, kb_box[3] * H
            unit = args.u or min(bw / maxx, bh / (maxy * 1.11 - 0.11))
            gap = unit * 0.11
            ox, oy = kb_box[0] * W, kb_box[1] * H
        elif mlay == "side":
            kb_box_w, kb_box_h = W * 0.56, H * 0.76
            unit = args.u or min(kb_box_w / maxx, kb_box_h / maxy)
            gap = unit * 0.11
            kb_w, kb_h = maxx * unit, maxy * (unit + gap) - gap
            ph = min(H * 0.72, max(kb_h * 1.08, H * 0.38))
            pw = ph * pad_ratio
            sep = max(36.0, W * 0.03)
            tot_w = kb_w + sep + pw
            if tot_w > W * 0.90:
                sc = (W * 0.90) / tot_w
                unit *= sc; gap *= sc; kb_w *= sc; kb_h *= sc; pw *= sc; ph *= sc; sep *= sc
                tot_w = kb_w + sep + pw
            ox = (W - tot_w) / 2.0
            oy = (H - kb_h) / 2.0
            mouse_art = build_mouse_art(ox + kb_w + sep, (H - ph) / 2.0, pw, ph, ms_alpha, theme, args.mopacity)
        elif mlay == "stack":
            kb_box_w, kb_box_h = W * 0.80, H * 0.42
            unit = args.u or min(kb_box_w / maxx, kb_box_h / maxy)
            gap = unit * 0.11
            kb_w, kb_h = maxx * unit, maxy * (unit + gap) - gap
            ph = min(H * 0.40, (W * 0.65) / pad_ratio)
            pw = ph * pad_ratio
            sep = max(24.0, H * 0.03)
            tot_h = kb_h + sep + ph
            ox = (W - kb_w) / 2.0
            oy = (H - tot_h) / 2.0
            mouse_art = build_mouse_art((W - pw) / 2.0, oy + kb_h + sep, pw, ph, ms_alpha, theme, args.mopacity)
        else:
            unit = args.u or min(W * 0.82 / maxx, H * 0.78 / maxy)
            gap = unit * 0.11
            ox = (W - maxx * unit) / 2.0
            oy = (H - (maxy * (unit + gap) - gap)) / 2.0
        width_px = max(1, int(unit * 0.014))
        slots = [build_slot(label, (ox + x * unit) / unit, (oy + y * (unit + gap)) / (unit + gap),
                            w, unit, gap, theme, kb_alpha, width_px)
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
    bg = None
    if args.bg:
        hexval = args.bg.lstrip("#")
        if len(hexval) == 6:
            bg = (int(hexval[0:2], 16), int(hexval[2:4], 16), int(hexval[4:6], 16), 255)
        elif len(hexval) == 8:
            bg = (int(hexval[0:2], 16), int(hexval[2:4], 16),
                  int(hexval[4:6], 16), int(hexval[6:8], 16))
        else:
            print("bad --bg value, expected #RRGGBB or #RRGGBBAA")
            return 2
    cfg = dict(out=out_dir, fps=args.fps, size=(W, H), slots=slots, series=series,
               bg=bg, full=full_mm, ffmpeg=FFMPEG, mm=args.mm,
               mouse_art=mouse_art, mouse_samples=mouse_samples)
    out_fmt = "png" if args.png_only else args.format
    if jobs <= 1 or n < 40:
        _init(cfg)
        _render_range((0, n))
    else:
        if args.pipe and out_fmt == "prores":
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

    if out_fmt == "png":
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

    t_enc = time.perf_counter()
    if out_fmt == "webm":
        movie = out_dir + ".webm"
        print("encoding WebM (VP9 yuva420p alpha)…")
        rc = encode_webm(out_dir, movie, args.fps, jobs)
        fmt_desc = "WebM VP9 with alpha"
    else:
        movie = out_dir + ".mov"
        print("encoding with %d parallel segments…" % jobs)
        rc = encode_segments(out_dir, movie, args.fps, n, jobs)
        fmt_desc = "ProRes 4444 with alpha"
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
    print("video -> %s (%s)" % (movie, fmt_desc))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
