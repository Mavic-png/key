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

from PIL import Image, ImageDraw, ImageFont

FULL = 3.50
THEMES = {
    "ramp": dict(stops=[(0.0, (56, 189, 248)), (0.36, (52, 211, 153)),
                        (0.70, (251, 191, 36)), (1.0, (244, 63, 94))]),
    "smooth": dict(stops=[(0.0, (56, 189, 248)), (0.36, (52, 211, 153)),
                          (0.70, (251, 191, 36)), (1.0, (244, 63, 94))]),
    "cyan": dict(fill_a=(14, 165, 233), fill_b=(56, 189, 248), fill_c=(125, 211, 252)),
    "mint": dict(fill_a=(16, 185, 129), fill_b=(52, 211, 153), fill_c=(110, 231, 183)),
    "amber": dict(fill_a=(217, 119, 6), fill_b=(251, 191, 36), fill_c=(253, 230, 138)),
    "crimson": dict(fill_a=(225, 29, 72), fill_b=(244, 63, 94), fill_c=(253, 164, 175)),
    "violet": dict(fill_a=(139, 92, 246), fill_b=(139, 92, 246), fill_c=(139, 92, 246)),
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
LABELS = {"LAlt": "Alt", "RAlt": "Alt", "Space": "", "Bksp": "Back", "LShift": "Shift",
          "RShift": "Shift", "LCtrl": "Ctrl", "RCtrl": "Ctrl", "LWin": "Win",
          "Enter": "Enter", "Tab": "Tab"}

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
    tx = ty = 0.0
    last_move = -9.0
    prev_t = evts[0][0]
    dur_raw = evts[-1][0]
    # Sample at 100 Hz from 0 to dur_raw so smart recenter matches /wb & /overlay/mouse
    n_pts = int(math.ceil(dur_raw * 100.0)) + 2
    idx = 0
    n_ev = len(evts)
    btn = 0
    samples = []
    for k in range(n_pts):
        t = k * 0.01
        dt = max(0.0, min(0.2, t - prev_t))
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
        elif t - last_move > 0.20 and dt > 0:
            kk = math.exp(-dt * (1.6 + min(1.4, math.hypot(tx, ty)) * 4.2))
            tx *= kk
            ty *= kk
        if t >= start and (not end or t <= end + 0.02):
            factor = 1.0 / max(1e-6, speed)
            samples.append(((t - start) * factor, tx, ty, btn | b_hit, w_acc))
    return samples if samples else None


def sample_mouse(samples, t):
    if not samples:
        return 0.0, 0.0, 0, 0, 0
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
    return x, y, samples[i][3], samples[i][4], i


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


def build_mouse_art(x, y, pw, ph, alpha, theme):
    """Pre-render static Zowie G-SR III mousepad RGBA layer."""
    pw, ph = max(60, int(round(pw))), max(50, int(round(ph)))
    rad = max(8, int(min(pw, ph) * 0.055))
    pad = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(pad)
    d.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=rad,
                        fill=(14, 16, 22, int(255 * min(1.0, alpha * 0.82))),
                        outline=(255, 255, 255, 56), width=max(1, pw // 160))
    # Subtle center crosshair + corner brackets
    d.line([(pw // 2, 10), (pw // 2, ph - 10)], fill=(255, 255, 255, 18), width=1)
    d.line([(10, ph // 2), (pw - 10, ph // 2)], fill=(255, 255, 255, 18), width=1)
    c_len = max(8, pw // 22)
    c_col = (255, 255, 255, 72)
    m = max(6, pw // 36)
    w_c = max(1, pw // 140)
    for (ax, ay, bx, by, cx, cy) in (
        (m, m + c_len, m, m, m + c_len, m),
        (pw - m - c_len, m, pw - m, m, pw - m, m + c_len),
        (m, ph - m - c_len, m, ph - m, m + c_len, ph - m),
        (pw - m - c_len, ph - m, pw - m, ph - m, pw - m, ph - m - c_len),
    ):
        d.line([(ax, ay), (bx, by), (cx, cy)], fill=c_col, width=w_c)
    mw = max(18, int(round(pw * (63.9 / 470.0))))
    mh = max(34, int(round(ph * (127.1 / 390.0))))
    return dict(x=int(round(x)), y=int(round(y)), w=pw, h=ph, mw=mw, mh=mh,
                inset=max(6, pw // 28), pad=pad, theme=theme, alpha=alpha)


def draw_mouse_overlay(canvas, t, mart, samples):
    """Composite mousepad, 0.55s fading trail, and Viper-proportioned mouse onto RGBA canvas."""
    if not mart or not samples:
        return
    px, py, pw, ph = mart["x"], mart["y"], mart["w"], mart["h"]
    mw, mh, inset = mart["mw"], mart["mh"], mart["inset"]
    canvas.alpha_composite(mart["pad"], (px, py))

    nx, ny, bmask, wh, idx = sample_mouse(samples, t)
    def to_xy(vx, vy):
        cx = inset + mw * 0.5 + (vx + 1.0) * 0.5 * (pw - mw - inset * 2)
        cy = inset + mh * 0.5 + (vy + 1.0) * 0.5 * (ph - mh - inset * 2)
        return cx, cy

    acc = depth_color(0.75, mart["theme"])
    # 0.55s fading trail inside pad bounds
    trail_img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    td = ImageDraw.Draw(trail_img)
    win = 0.55
    k0 = idx
    while k0 > 0 and (t - samples[k0 - 1][0]) <= win:
        k0 -= 1
    prev = None
    for k in range(k0, idx + 1):
        pt = to_xy(samples[k][1], samples[k][2])
        age = max(0.0, t - samples[k][0])
        a = max(0.0, 1.0 - age / win)
        if prev is not None and a > 0.02:
            td.line([prev, pt], fill=(acc[0], acc[1], acc[2], int(185 * a)),
                    width=max(2, int(round((1.4 + a * 2.4) * (pw / 306.0)))))
        prev = pt
    cx, cy = to_xy(nx, ny)
    if prev is not None:
        td.line([prev, (cx, cy)], fill=(acc[0], acc[1], acc[2], 195),
                width=max(2, int(round(3.2 * (pw / 306.0)))))
    canvas.alpha_composite(trail_img, (px, py))

    # Symmetrical Viper V4 Pro silhouette with button highlights clipped to shell mask
    m_img = Image.new("RGBA", (mw + 8, mh + 8), (0, 0, 0, 0))
    md = ImageDraw.Draw(m_img)
    ox, oy = 4, 4
    r_m = max(6, mw // 2)
    a_m = int(255 * min(1.0, mart["alpha"] * 1.02))
    md.rounded_rectangle([ox, oy, ox + mw - 1, oy + mh - 1], radius=r_m,
                         fill=(26, 30, 39, a_m))
    split_y = oy + int(mh * 0.40)
    mid_x = ox + mw // 2
    if bmask & 3:
        s_mask = Image.new("L", (mw + 8, mh + 8), 0)
        ImageDraw.Draw(s_mask).rounded_rectangle([ox + 1, oy + 1, ox + mw - 2, oy + mh - 2],
                                                 radius=max(4, r_m - 1), fill=220)
        b_ov = Image.new("RGBA", (mw + 8, mh + 8), (0, 0, 0, 0))
        bd = ImageDraw.Draw(b_ov)
        if bmask & 1:
            bd.rectangle([ox, oy, mid_x - 1, split_y], fill=(acc[0], acc[1], acc[2], 255))
        if bmask & 2:
            bd.rectangle([mid_x + 1, oy, ox + mw, split_y], fill=(acc[0], acc[1], acc[2], 255))
        b_a = b_ov.getchannel("A")
        b_ov.putalpha(Image.composite(s_mask, Image.new("L", s_mask.size, 0), b_a))
        m_img.alpha_composite(b_ov)
        md = ImageDraw.Draw(m_img)
    md.rounded_rectangle([ox, oy, ox + mw - 1, oy + mh - 1], radius=r_m,
                         outline=(255, 255, 255, 165), width=max(1, mw // 22))
    md.line([(ox + 2, split_y), (ox + mw - 3, split_y)], fill=(255, 255, 255, 75), width=1)
    md.line([(mid_x, oy + 3), (mid_x, split_y)], fill=(255, 255, 255, 75), width=1)
    # Scroll wheel
    ww = max(3, int(mw * 0.14))
    wh_px = max(6, int(mh * 0.15))
    wx0, wy0 = mid_x - ww // 2, oy + int(mh * 0.11)
    w_on = bool((bmask & 4) or wh != 0)
    md.rounded_rectangle([wx0, wy0, wx0 + ww, wy0 + wh_px], radius=max(2, ww // 2),
                         fill=(acc[0], acc[1], acc[2], 240) if w_on else (160, 170, 185, 165),
                         outline=(255, 255, 255, 200), width=1)
    # Side buttons (X2 top, X1 bottom on left edge)
    sw = max(2, int(mw * 0.08))
    sh_px = max(5, int(mh * 0.12))
    for bit, frac in ((16, 0.34), (8, 0.49)):
        sy0 = oy + int(mh * frac)
        on = bool(bmask & bit)
        md.rounded_rectangle([ox - 1, sy0, ox + sw, sy0 + sh_px], radius=2,
                             fill=(acc[0], acc[1], acc[2], 245) if on else (160, 170, 185, 140))
    # Center LED dot
    led_r = max(2, mw // 16)
    ly = oy + int(mh * 0.48)
    md.ellipse([mid_x - led_r, ly - led_r, mid_x + led_r, ly + led_r],
               fill=(acc[0], acc[1], acc[2], 255 if bmask else 155))
    canvas.alpha_composite(m_img, (int(round(px + cx - mw * 0.5 - ox)),
                                   int(round(py + cy - mh * 0.5 - oy))))


def render_frame(idx, fps, size, slots, series, bg=None, full=FULL,
                 mouse_art=None, mouse_samples=None):
    W, H = size
    canvas = Image.new("RGBA", (W, H), bg if bg else (0, 0, 0, 0))
    t = idx / float(fps)
    draw = ImageDraw.Draw(canvas)
    full_mm = max(0.5, float(full or FULL))
    for s in slots:
        mm = sample_value(series.get(s["label"], []), t)
        ratio = max(0.0, min(1.0, mm / full_mm))
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
        border = (255, 255, 255, 46)
        draw.rounded_rectangle([s["x"], s["y"], s["x"] + kw - 1, s["y"] + kh - 1],
                               radius=s["r"], outline=border, width=s["width"])
        img = s["label_img"].get(ratio > 0.05)
        if img is not None:
            canvas.alpha_composite(img, (s["x"], s["y"]))
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
                               _W.get("mouse_art"), _W.get("mouse_samples"))
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
                           _W.get("mouse_art"), _W.get("mouse_samples"))
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
    ap.add_argument("--alias", default="")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time scale: 0.5 = half speed, 2 = double speed")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0)
    ap.add_argument("--jobs", type=int, default=0, help="worker processes (0 = all cores)")
    ap.add_argument("--bg", default="", help="solid background colour, e.g. #101014")
    ap.add_argument("--mouse", default="", help="optional mouse.csv for synced mousepad overlay")
    ap.add_argument("--mouse-layout", default="side", choices=("side", "stack", "only"),
                    help="keyboard + mouse placement: side | stack | only")
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
    series, dur = load_csv(args.csv)
    if not series and not args.mouse:
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

    mouse_art = None
    slots = []
    pad_ratio = 470.0 / 390.0
    if mlay == "only":
        ph = min(H * 0.78, (W * 0.82) / pad_ratio)
        pw = ph * pad_ratio
        mouse_art = build_mouse_art((W - pw) / 2.0, (H - ph) / 2.0, pw, ph, args.alpha, theme)
    else:
        maxx = max(x + w for _, x, y, w in slots_def)
        maxy = max(y + 1 for _, x, y, w in slots_def)
        if mlay == "side":
            kb_box_w, kb_box_h = W * 0.56, H * 0.76
            unit = args.u or min(kb_box_w / maxx, kb_box_h / maxy)
            gap = unit * 0.08
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
            mouse_art = build_mouse_art(ox + kb_w + sep, (H - ph) / 2.0, pw, ph, args.alpha, theme)
        elif mlay == "stack":
            kb_box_w, kb_box_h = W * 0.80, H * 0.42
            unit = args.u or min(kb_box_w / maxx, kb_box_h / maxy)
            gap = unit * 0.08
            kb_w, kb_h = maxx * unit, maxy * (unit + gap) - gap
            ph = min(H * 0.40, (W * 0.65) / pad_ratio)
            pw = ph * pad_ratio
            sep = max(24.0, H * 0.03)
            tot_h = kb_h + sep + ph
            ox = (W - kb_w) / 2.0
            oy = (H - tot_h) / 2.0
            mouse_art = build_mouse_art((W - pw) / 2.0, oy + kb_h + sep, pw, ph, args.alpha, theme)
        else:
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
               bg=bg, full=full_mm, ffmpeg=FFMPEG,
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
