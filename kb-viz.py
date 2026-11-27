# -*- coding: utf-8 -*-
"""kb-viz.py -- turn a depth CSV into a self-contained keyboard visualizer HTML.

Input : depth-live-*.csv / depth-*.csv (long format, one row per key change)
Output: one .html file with the data embedded -- open it, press H for controls,
        screen-record it with OBS. No server, no dependencies.

    python kb-viz.py depth-live-20261125-200949.csv
    python kb-viz.py in.csv -o viz.html --title "ROG FALCHION ACE 75 HE" --u 70
    python kb-viz.py in.csv --fps 120 --speed 0.5 --loop
"""

import argparse
import csv
import io
import json
import os
import sys

FULL = 3.50

# 75% layout, units: label, row, x, width
LAYOUT = [
    # row 0 - function row
    ("Esc", 0, 0.0, 1), ("F1", 0, 1.4, 1), ("F2", 0, 2.4, 1), ("F3", 0, 3.4, 1),
    ("F4", 0, 4.4, 1), ("F5", 0, 5.5, 1), ("F6", 0, 6.5, 1), ("F7", 0, 7.5, 1),
    ("F8", 0, 8.5, 1), ("F9", 0, 9.6, 1), ("F10", 0, 10.6, 1), ("F11", 0, 11.6, 1),
    ("F12", 0, 12.6, 1), ("Del", 0, 15.2, 1),
    # row 1 - numbers
    ("`", 1, 0.0, 1), ("1", 1, 1, 1), ("2", 1, 2, 1), ("3", 1, 3, 1), ("4", 1, 4, 1),
    ("5", 1, 5, 1), ("6", 1, 6, 1), ("7", 1, 7, 1), ("8", 1, 8, 1), ("9", 1, 9, 1),
    ("0", 1, 10, 1), ("-", 1, 11, 1), ("=", 1, 12, 1), ("Bksp", 1, 13, 2),
    ("Home", 1, 15.2, 1),
    # row 2
    ("Tab", 2, 0.0, 1.5), ("Q", 2, 1.5, 1), ("W", 2, 2.5, 1), ("E", 2, 3.5, 1),
    ("R", 2, 4.5, 1), ("T", 2, 5.5, 1), ("Y", 2, 6.5, 1), ("U", 2, 7.5, 1),
    ("I", 2, 8.5, 1), ("O", 2, 9.5, 1), ("P", 2, 10.5, 1), ("[", 2, 11.5, 1),
    ("]", 2, 12.5, 1), ("\\", 2, 13.5, 1.5), ("PgUp", 2, 15.2, 1),
    # row 3
    ("Caps", 3, 0.0, 1.75), ("A", 3, 1.75, 1), ("S", 3, 2.75, 1), ("D", 3, 3.75, 1),
    ("F", 3, 4.75, 1), ("G", 3, 5.75, 1), ("H", 3, 6.75, 1), ("J", 3, 7.75, 1),
    ("K", 3, 8.75, 1), ("L", 3, 9.75, 1), (";", 3, 10.75, 1), ("'", 3, 11.75, 1),
    ("Enter", 3, 12.75, 2.25), ("PgDn", 3, 15.2, 1),
    # row 4
    ("LShift", 4, 0.0, 2.25), ("Z", 4, 2.25, 1), ("X", 4, 3.25, 1), ("C", 4, 4.25, 1),
    ("V", 4, 5.25, 1), ("B", 4, 6.25, 1), ("N", 4, 7.25, 1), ("M", 4, 8.25, 1),
    (",", 4, 9.25, 1), (".", 4, 10.25, 1), ("/", 4, 11.25, 1), ("RShift", 4, 12.25, 2.75),
    ("End", 4, 15.2, 1),
    # row 5
    ("LCtrl", 5, 0.0, 1.25), ("LWin", 5, 1.25, 1.25), ("LAlt", 5, 2.5, 1.25),
    ("Space", 5, 3.75, 6.5), ("RAlt", 5, 10.25, 1.25), ("Fn", 5, 11.5, 1.25),
    ("RCtrl", 5, 12.75, 1.25), ("←", 5, 14.2, 0.9), ("↓", 5, 15.2, 0.9),
    ("→", 5, 16.2, 0.9),
]

ALIAS = {"Backspace": "Bksp", "Back": "Bksp", "<-": "←", "Up": "↑", "Down": "↓", "->": "→",
         "Right": "→", "Left": "←", "LShift": "LShift", "RShift": "RShift",
         "Space": "Space", "LMeta": "LWin", "RMeta": "RWin", "菜单": "Fn"}


def detect_full_mm(path):
    """Read '# ... full=3.80mm' from CSV header comments if present."""
    import re
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as f:
            for _ in range(12):
                ln = f.readline()
                if not ln:
                    break
                if not ln.lstrip().startswith("#"):
                    break
                m = re.search(r"full\s*=\s*([0-9]+(?:\.[0-9]+)?)", ln, re.I)
                if m:
                    v = float(m.group(1))
                    if 1.0 <= v <= 5.0:
                        return round(v, 2)
    except Exception:
        pass
    return None


def read_rows(path):
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        rows = [ln for ln in f if not ln.lstrip().startswith("#") and ln.strip()]
    if not rows:
        return {}, 0.0
    header = [h.strip() for h in rows[0].strip().split(",")]
    lower = [h.lower() for h in header]
    if "key" not in lower and "mm" not in lower and len(header) >= 3:
        keycols = [(i, ALIAS.get(header[i], header[i])) for i in range(1, len(header))
                   if lower[i] not in ("wall", "t_s", "t")]
        data = {}
        tmin = None
        for ln in rows[1:]:
            parts = ln.strip().split(",")
            if len(parts) < len(header):
                continue
            try:
                t = float(parts[0])
            except ValueError:
                continue
            if tmin is None or t < tmin:
                tmin = t
            for idx, name in keycols:
                try:
                    mm = float(parts[idx])
                except (ValueError, IndexError):
                    mm = 0.0
                data.setdefault(name, []).append((t, mm))
        return data, (tmin or 0.0)
    reader = csv.DictReader(rows)
    cols = {c.lower(): c for c in (reader.fieldnames or [])}
    tcol = cols.get("t_s") or cols.get("t") or "t_s"
    data = {}
    tmin = None
    for r in reader:
        try:
            t = float(r[tcol])
            mm = float(r["mm"])
        except (KeyError, ValueError, TypeError):
            continue
        key = (r.get("key") or "").strip() or ("#" + str(r.get("index", "?")))
        key = ALIAS.get(key, key)
        if tmin is None or t < tmin:
            tmin = t
        data.setdefault(key, []).append((t, mm))
    return {k: v for k, v in data.items()}, (tmin or 0.0)


HTML = u"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%(title)s</title>
<style>
  :root {
    --u: %(u)spx;
    --gap: 5px;
    --radius: 9px;
    --idle-bg-top: #232326;
    --idle-bg-bottom: #17171a;
    --outline: #4a4a52;
    --label-idle: #8b8b93;
    --label-hot: #ffffff;
    --glow: 139, 92, 246;
    --glow-2: 217, 70, 239;
    --fill-b: 99, 102, 241;
    --key-a: #33333d;
    --key-b: #1a1a20;
    --hot-bg: #262629;
    --ease: cubic-bezier(.2,.7,.2,1);
  }
  body.theme-amber {
    --glow: 245, 158, 11; --glow-2: 251, 191, 36; --fill-b: 217, 119, 6;
  }
  body.theme-ice, body.theme-cyan {
    --glow: 56, 189, 248; --glow-2: 34, 211, 238; --fill-b: 14, 165, 233;
  }
  body.theme-mint {
    --glow: 52, 211, 153; --glow-2: 110, 231, 183; --fill-b: 16, 185, 129;
  }
  body.theme-crimson {
    --glow: 244, 63, 94; --glow-2: 253, 164, 175; --fill-b: 225, 29, 72;
  }
  body.theme-violet {
    --glow: 168, 85, 247; --glow-2: 216, 180, 254; --fill-b: 126, 34, 206;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; height: 100%%; overflow: hidden;
    background: #0a0a0c;
    background-image:
      radial-gradient(900px 520px at 50%% -12%%, #181826 0%%, transparent 62%%),
      radial-gradient(700px 420px at 88%% 108%%, #141420 0%%, transparent 60%%);
    font-family: "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif;
  }
  body.transparent { background: transparent; background-image: none; }
  #stage { position: absolute; inset: 0; display: flex; flex-direction: column;
           align-items: center; justify-content: center; gap: 20px; }
  #caption { color: #6f6f7a; font-size: 13px; letter-spacing: 3px; text-transform: uppercase; }
  body.transparent #caption { display: none; }
  #kb { position: relative; }

  .k {
    position: absolute;
    display: flex; align-items: flex-end; justify-content: center;
    border-radius: var(--radius);
    background: linear-gradient(180deg, var(--idle-bg-top), var(--idle-bg-bottom));
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,.055),
      inset 0 -1px 0 rgba(0,0,0,.55),
      0 2px 5px rgba(0,0,0,.55);
    outline: 1px solid var(--outline);
    outline-offset: -1px;
    overflow: hidden;
    transform: translateY(0) scale(1);
    transition: transform .09s cubic-bezier(.2,.8,.3,1.2), box-shadow .09s linear,
                background .09s linear, outline-color .09s linear;
    will-change: transform, box-shadow;
  }
  .k .fill {
    position: absolute; left: 0; right: 0; bottom: 0; height: 0%%;
    background: linear-gradient(to top,
                rgba(var(--glow), .74) 0%%,
                rgba(var(--fill-b), .42) 72%%,
                rgba(var(--fill-b), .20) 100%%);
    opacity: .95; pointer-events: none;
  }
  .k .lbl {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center;
    font-size: calc(var(--u) * .235); font-weight: 600;
    color: var(--label-idle); letter-spacing: .5px;
    text-shadow: 0 1px 2px rgba(0,0,0,.8);
    transition: color .09s linear;
  }
  .k.hot .lbl { color: var(--label-hot); }

  #hud { position: absolute; left: 26px; top: 20px; color: #5d5d68;
         font-size: 12px; letter-spacing: 1px; }
  #hud b { color: #c9c9d4; font-weight: 600; }
  #plate {
    position: relative; padding: 18px; border-radius: 24px;
    background:
      radial-gradient(120%% 90%% at 50%% 0%%, rgba(var(--glow), .10) 0%%, transparent 55%%),
      linear-gradient(180deg, #23232c 0%%, #15151b 100%%);
    border: 1px solid rgba(255,255,255,.10);
    box-shadow:
      0 1px 0 rgba(255,255,255,.10) inset,
      0 -2px 3px rgba(0,0,0,.85) inset,
      0 30px 70px rgba(0,0,0,.70),
      0 8px 22px rgba(0,0,0,.55);
  }
  #plate::after {
    content: ""; position: absolute; left: 14px; right: 14px; top: 0; height: 1px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.22), transparent);
  }
  #meter { position: absolute; left: 28px; top: 48px; width: 148px; height: 5px;
           background: #202028; border-radius: 3px; overflow: hidden;
           box-shadow: inset 0 1px 0 rgba(255,255,255,.06); }
  #meter i { display: block; height: 100%%; width: 0%%;
             background: linear-gradient(90deg, rgba(var(--glow),.55), rgba(var(--glow-2),.95)); }
  #ctrl { position: absolute; left: 50%%; bottom: 18px; transform: translateX(-50%%);
          display: none; align-items: center; gap: 10px; padding: 8px 14px;
          background: #15151acc; border: 1px solid #2a2a33; border-radius: 10px;
          color: #9a9aa6; font-size: 12px; backdrop-filter: blur(6px); }
  #ctrl.on { display: flex; }
  #ctrl button { background: #1e1e24; color: #d2d2dc; border: 1px solid #32323c;
                 border-radius: 7px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
  #ctrl button:hover { background: #26262f; }
  #bar { width: 320px; height: 4px; background: #22222a; border-radius: 2px;
         position: relative; cursor: pointer; }
  #bar i { position: absolute; left: 0; top: 0; bottom: 0; width: 0%%;
           background: rgb(var(--glow)); border-radius: 2px; }
  #tip { position: absolute; right: 24px; bottom: 18px; color: #35353d; font-size: 11px; }
</style></head>
<body>
<div id="stage">
  <div id="caption">%(caption)s</div>
  <div id="plate"><div id="kb"></div></div>
</div>
<div id="meter"><i></i></div>
<div id="hud"><b id="hudkey">—</b> <span id="hudmm"></span></div>
<div id="ctrl">
  <button id="play">暂停</button>
  <span id="time">0.00 s</span>
  <div id="bar"><i></i></div>
  <span id="spd">1.0x</span>
</div>
<div id="tip">H 显示/隐藏控制条 · 空格 暂停 · R 重播 · 点击进度条跳转</div>
<script>
const LAYOUT = %(layout)s, DATA = %(data)s, DUR = %(dur)s;
const Q = new URLSearchParams(location.search);
const FULL = Math.max(1.0, Math.min(5.0, parseFloat(Q.get('full')) || %(full)s));
const THEME = (Q.get('theme') || %(theme)s).toLowerCase();
const U = %(u)s, GAP = 5;
const kb = document.getElementById('kb');
if (Q.get('bg') === '0') document.body.classList.add('transparent');
if (['amber','ice','cyan','mint','crimson','violet'].includes(THEME))
  document.body.classList.add('theme-' + THEME);
function smoothRamp(r) {
  const stops = [[0,[56,189,248]],[0.36,[52,211,153]],[0.70,[251,191,36]],[1,[244,63,94]]];
  r = Math.max(0, Math.min(1, r));
  for (let i = 1; i < stops.length; i++) {
    if (r <= stops[i][0]) {
      const f = 0.5 - 0.5 * Math.cos(Math.PI * (r - stops[i-1][0]) / (stops[i][0] - stops[i-1][0]));
      const a = stops[i-1][1], b = stops[i][1];
      return [Math.round(a[0]+(b[0]-a[0])*f), Math.round(a[1]+(b[1]-a[1])*f), Math.round(a[2]+(b[2]-a[2])*f)];
    }
  }
  return stops[stops.length - 1][1];
}

let maxX = 0, maxY = 0;
for (const k of LAYOUT) { maxX = Math.max(maxX, k.x + k.w); maxY = Math.max(maxY, k.r + 1); }
kb.style.width = (maxX * U + 'px');
kb.style.height = (maxY * (U + GAP) + 'px');

const els = {};
for (const k of LAYOUT) {
  const d = document.createElement('div');
  d.className = 'k';
  d.style.left = (k.x * U) + 'px';
  d.style.top = (k.r * (U + GAP)) + 'px';
  d.style.width = (k.w * U - GAP) + 'px';
  d.style.height = (U - GAP) + 'px';
  d.innerHTML = '<div class="fill"></div><div class="lbl">' + k.k + '</div>';
  kb.appendChild(d);
  els[k.k] = { box: d, fill: d.firstChild };
}

const series = {}, ptr = {};
for (const name in DATA) { series[name] = DATA[name]; ptr[name] = 0; }
function sample(name, t) {
  const a = series[name];
  if (!a || !a.length) return 0;
  let i = ptr[name];
  if (i >= a.length) i = a.length - 1;
  while (i > 0 && a[i][0] > t) i--;
  while (i < a.length - 1 && a[i + 1][0] <= t) i++;
  ptr[name] = i;
  const cur = a[i], nxt = a[i + 1];
  if (!nxt) return (t - cur[0] < 0.12) ? cur[1] : 0;
  if (t < cur[0]) return 0;
  if (nxt[0] - cur[0] > 0.12) return (t - cur[0] < 0.12) ? cur[1] : 0;
  const f = (t - cur[0]) / (nxt[0] - cur[0]);
  return cur[1] + (nxt[1] - cur[1]) * f;
}

const hudKey = document.getElementById('hudkey'), hudMm = document.getElementById('hudmm');
const QS = new URLSearchParams(location.search);
let t = QS.get('t') ? parseFloat(QS.get('t')) : 0;
let playing = QS.get('play') !== '0';
let speed = QS.get('speed') ? parseFloat(QS.get('speed')) : %(speed)s;
let last = performance.now();
t = Math.max(0, Math.min(DUR, t));

function render() {
  let best = 0, bestName = null;
  for (const name in series) {
    const el = els[name];
    if (!el) continue;
    const mm = sample(name, t);
    const r = Math.max(0, Math.min(1, mm / FULL));
    el.fill.style.height = (r * 100) + '%%';
    if (THEME === 'ramp' && r > 0.04) {
      const [R, G, B] = smoothRamp(r), [R0, G0, B0] = smoothRamp(Math.max(0, r * 0.55));
      el.fill.style.background = 'linear-gradient(to top, rgba(' + R0 + ',' + G0 + ',' + B0 + ',.80), rgba(' + R + ',' + G + ',' + B + ',.56))';
      el.fill.style.borderTop = '1.5px solid rgb(' + Math.min(255, R + 36) + ',' + Math.min(255, G + 36) + ',' + Math.min(255, B + 36) + ')';
    } else {
      el.fill.style.background = '';
      el.fill.style.borderTop = '';
    }
    el.box.classList.toggle('hot', r > 0.04);
    if (r > 0.04) {
      const blur = 10 + r * 26;
      el.box.style.boxShadow =
        '0 0 ' + (blur * .55).toFixed(0) + 'px rgba(var(--glow), ' + (.32 + r * .34).toFixed(2) + '),' +
        ' 0 0 ' + blur.toFixed(0) + 'px rgba(var(--glow-2), ' + (.16 + r * .30).toFixed(2) + '),' +
        ' inset 0 1px 0 rgba(255,255,255,.12), inset 0 -2px 2px rgba(0,0,0,.5)';
      el.box.style.transform = 'scale(' + (1 + 0.05 * r).toFixed(3) + ')';
      el.box.style.background = 'linear-gradient(180deg, #2b2b31, #1d1d22)';
      if (mm > best) { best = mm; bestName = name; }
    } else {
      el.box.style.boxShadow = '';
      el.box.style.transform = 'scale(1)';
      el.box.style.background = '';
    }
  }
  hudKey.textContent = bestName || '—';
  hudMm.textContent = bestName ? best.toFixed(2) + ' mm' : '';
  const meterEl = document.querySelector('#meter i');
  if (meterEl) meterEl.style.width = (best / FULL * 100) + '%%';
  document.getElementById('time').textContent = t.toFixed(2) + ' s';
  document.getElementById('bar').firstChild.style.width = (t / DUR * 100) + '%%';
}

function loop(now) {
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  if (playing) {
    t += dt * speed;
    if (t > DUR) { t = %(loop)s; for (const n in ptr) ptr[n] = 0; }
  }
  render();
  requestAnimationFrame(loop);
}
const bar = document.getElementById('bar');
bar.addEventListener('click', e => {
  const r = bar.getBoundingClientRect();
  t = Math.max(0, Math.min(DUR, (e.clientX - r.left) / r.width * DUR));
  for (const n in ptr) ptr[n] = 0;
});
const ctrl = document.getElementById('ctrl'), tip = document.getElementById('tip');
const playBtn = document.getElementById('play');
playBtn.addEventListener('click', () => {
  playing = !playing; playBtn.textContent = playing ? '暂停' : '播放';
});
document.getElementById('spd').addEventListener('click', e => {
  const steps = [0.25, 0.5, 1, 2, 4];
  const i = steps.indexOf(speed);
  speed = steps[(i + 1) %% steps.length];
  e.target.textContent = speed.toFixed(2).replace(/0+$/, '').replace(/\.$/, '') + 'x';
});
addEventListener('keydown', e => {
  if (e.code === 'KeyH') { ctrl.classList.toggle('on'); tip.style.display = 'none'; }
  if (e.code === 'Space') { e.preventDefault(); playing = !playing;
    playBtn.textContent = playing ? '暂停' : '播放'; }
  if (e.code === 'KeyR') { t = 0; for (const n in ptr) ptr[n] = 0; }
  if (e.code === 'ArrowRight') t = Math.min(DUR, t + 0.25);
  if (e.code === 'ArrowLeft') t = Math.max(0, t - 0.25);
  if (e.code === 'KeyT') document.body.classList.toggle('transparent');
});
requestAnimationFrame(loop);
</script></body></html>
"""


def build(path, out, title, caption, unit, speed, loop, full=None, theme="ramp"):
    data, t0 = read_rows(path)
    if not data:
        print("no samples in %s" % path)
        return 2
    full_mm = full if full is not None else (detect_full_mm(path) or FULL)
    full_mm = max(1.0, min(5.0, float(full_mm)))
    clean = {}
    for k, pts in data.items():
        pts.sort()
        clean[k] = [[round(t - t0, 3), round(mm, 2)] for t, mm in pts]
    duration = max(p[-1][0] for p in clean.values())
    layout = [{"k": k, "r": r, "x": x, "w": w} for k, r, x, w in LAYOUT]
    payload = dict(clean)
    html = HTML % {
        "title": title, "caption": caption, "u": unit, "speed": speed,
        "layout": json.dumps(layout, ensure_ascii=False),
        "data": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "dur": "%.3f" % duration, "full": "%.2f" % full_mm,
        "theme": json.dumps(theme), "hud": "true",
        "loop": "0" if loop else "dur",
    }
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("wrote %s  (%.1f s, %d keys, %d samples, %.1f KB)"
          % (os.path.basename(out), duration, len(clean),
             sum(len(v) for v in clean.values()), len(html) / 1024.0))
    return 0


def main():
    ap = argparse.ArgumentParser(description="depth CSV -> keyboard visualizer HTML")
    ap.add_argument("csv")
    ap.add_argument("-o", "--out")
    ap.add_argument("--title", default="ROG FALCHION ACE 75 HE")
    ap.add_argument("--caption", default="")
    ap.add_argument("--u", type=float, default=64, help="key unit in px")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--full", type=float, default=None,
                    help="full switch travel in mm (default: auto from CSV or 3.50)")
    ap.add_argument("--theme", default="ramp",
                    choices=("ramp", "cyan", "mint", "amber", "crimson", "violet", "ice"))
    ap.add_argument("--loop", action="store_true", default=True)
    args = ap.parse_args()
    out = args.out or os.path.splitext(args.csv)[0] + "-viz.html"
    caption = args.caption or ("%s  ·  %s" % (args.title, os.path.basename(args.csv)))
    return build(args.csv, out, args.title, caption, args.u, args.speed, args.loop,
                 full=args.full, theme=args.theme)


if __name__ == "__main__":
    sys.exit(main() or 0)
