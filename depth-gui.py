# -*- coding: utf-8 -*-
"""depth-gui.py -- real-time magnetic switch depth monitor (Tkinter, no deps).

Reads the ROG Falchion Ace 75 HE analog HID channel (usage page 0xFFC0,
report 03 7E <key> 00 <depth16 LE>, unit 0.01 mm) and draws:
  * a live depth bar for the key you are touching
  * a depth-vs-time scope for the last 4 seconds
  * per-press numbers: actuation depth, reset depth, rapid-trigger distance,
    peak travel, rise/fall time, press speed

Run:  python depth-gui.py
      python depth-gui.py --demo        (synthetic input, UI self-test)
"""

import argparse
import collections
import ctypes
import importlib.util
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))


def load_core():
    spec = importlib.util.spec_from_file_location(
        "depth_core", os.path.join(HERE, "depth-probe.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = load_core()

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
try:
    kernel32.timeBeginPeriod(1)
except Exception:
    pass

BG = "#12151b"
PANEL = "#171c25"
EDGE = "#262c36"
FG = "#e6edf3"
DIM = "#8b949e"
GRID = "#232932"
BLUE = "#3fb6ff"
YELLOW = "#ffc400"
RED = "#ff5a5a"
GREEN = "#3ddc84"
FULL = 3.50
PALETTE = ["#3fb6ff", "#3ddc84", "#ffc400", "#ff5a5a", "#b07bff", "#ff9f43"]

CANDIDATE_VKS = (list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) +
                 [0x20, 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12, 0x1B,
                  0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
                  0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0,
                  0xDB, 0xDC, 0xDD, 0xDE])

KEYNAME = {0x20: "Space", 0x08: "Bksp", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc",
           0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt", 0xA0: "LShift", 0xA1: "RShift",
           0xA2: "LCtrl", 0xA3: "RCtrl", 0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-",
           0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}


def key_name(vk):
    if vk in KEYNAME:
        return KEYNAME[vk]
    try:
        return chr(vk).upper()
    except ValueError:
        return "0x%02X" % vk


class LiveReader(threading.Thread):
    """Reads the analog channel and reopens it whenever the device is replugged."""

    daemon = True

    def __init__(self, sink, finder):
        threading.Thread.__init__(self)
        self.sink = sink
        self.finder = finder
        self.handle = None
        self.connected = False
        self.error = None
        self.stop = False

    def run(self):
        while not self.stop:
            info = self.finder()
            if not info:
                self.connected = False
                time.sleep(1.0)
                continue
            h = kernel32.CreateFileW(info["path"], core.GENERIC_READ,
                                     core.FILE_SHARE_READ | core.FILE_SHARE_WRITE,
                                     None, core.OPEN_EXISTING, 0, None)
            if h == core.INVALID_HANDLE_VALUE:
                self.error = ctypes.get_last_error()
                self.connected = False
                time.sleep(1.0)
                continue
            self.handle = h
            self.connected = True
            self.error = None
            length = max(info.get("in_len") or 21, 21)
            buf = (ctypes.c_ubyte * length)()
            nread = ctypes.wintypes.DWORD(0)
            while not self.stop:
                if not kernel32.ReadFile(h, buf, length, ctypes.byref(nread), None):
                    break                      # device gone -> reopen
                self.sink.append((time.perf_counter(), bytes(buf[:nread.value])))
            self.connected = False
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass
            self.handle = None
            if self.stop:
                break
            time.sleep(0.6)


class Press(object):
    def __init__(self, t, mm):
        self.start = t
        self.samples = [(t, mm)]
        self.peak = mm
        self.peak_t = t
        self.last_peak_t = t
        self.end = None
        self.down_depth = None
        self.up_depth = None
        self.vk = None

    def feed(self, t, mm):
        self.samples.append((t, mm))
        if mm > self.peak:
            self.peak = mm
            self.peak_t = t
        if mm >= self.peak - 0.02:
            self.last_peak_t = t
        if mm <= 0.15:
            self.end = t
            return True
        return False

    @property
    def rise_ms(self):
        return max(0.0, (self.peak_t - self.start) * 1000.0)

    @property
    def fall_ms(self):
        return max(0.0, ((self.end or self.last_peak_t) - self.last_peak_t) * 1000.0)

    @property
    def speed(self):
        dur = self.rise_ms
        if dur < 1:
            return 0.0
        return (self.peak - self.samples[0][1]) / dur * 1000.0

    @property
    def rt(self):
        if self.up_depth is None or self.peak < 3.0:
            return None
        return self.peak - self.up_depth


class KeyState(object):
    def __init__(self, index):
        self.index = index
        self.vk = None
        self.mm = 0.0
        self.press = None
        self.last = None
        self.count = 0
        self.speeds = collections.deque(maxlen=40)
        self.history = collections.deque(maxlen=4000)


class App(object):
    def __init__(self, root, info, demo=False):
        self.root = root
        self.info = info
        self.vid = info["vid"] if info else 0x0B05
        self.pid = info.get("pid", 0x1C7D) if info else 0x1C7D
        self.demo = demo
        self.demo_start = time.perf_counter()
        self.demo_phase = 0
        self.keys = {}
        self.focus = None
        self.queue = []
        self.t0 = time.perf_counter()
        self.rate = 0.0
        self.rate_n = 0
        self.n_depth = 0
        self.n_event = 0
        self.depth_per_s = 0.0
        self.event_per_s = 0.0
        self.silent_warn = False
        self.rate_t = self.t0
        self.vk_prev = {}
        self.total_presses = 0
        self.buffer = collections.deque(maxlen=400000)
        self.reader = None

        root.title("磁轴深度实时监测 — ROG FALCHION ACE 75 HE")
        root.configure(bg=BG)
        root.geometry("980x800")
        root.minsize(880, 720)
        self.build()
        if not demo:
            vid, pid = info["vid"], info["pid"] if "pid" in info else None
            self.reader = LiveReader(self.queue,
                                     lambda: core.find_depth_collection(self.vid, self.pid))
            self.reader.start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(15, self.tick)

    # ------------------------------------------------------------------ UI
    def build(self):
        f = self.make_font
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="ROG FALCHION ACE 75 HE", bg=BG, fg=FG,
                 font=f(14, bold=True)).pack(side="left")
        self.status = tk.Label(head, text="", bg=BG, fg=DIM, font=f(9))
        self.status.pack(side="right")
        self.warn = tk.Label(self.root, text="", bg="#3a2f13", fg="#ffc400",
                             font=f(9), anchor="w", justify="left", padx=10, pady=6)
        if self.info:
            tk.Label(self.root,
                     text="HID 通道 usage page 0x%04X / 输入报文 %d 字节 / 单位 0.01 mm"
                          % (self.info.get("usage_page", 0), self.info.get("in_len", 0)),
                     bg=BG, fg=DIM, font=f(9)).pack(anchor="w", padx=18)

        # current key card
        card = tk.Frame(self.root, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        card.pack(fill="x", padx=18, pady=10)
        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=16, pady=(12, 0))
        self.key_label = tk.Label(top, text="—", bg=PANEL, fg=FG, font=f(26, bold=True))
        self.key_label.pack(side="left")
        self.hint = tk.Label(top, text="按下任意键开始识别", bg=PANEL, fg=DIM, font=f(10))
        self.hint.pack(side="left", padx=14)
        self.mm_label = tk.Label(top, text="0.00 mm", bg=PANEL, fg=BLUE, font=f(30, bold=True))
        self.mm_label.pack(side="right")

        self.bar = tk.Canvas(card, height=64, bg=PANEL, highlightthickness=0)
        self.bar.pack(fill="x", padx=16, pady=(4, 2))
        self.bar.bind("<Configure>", lambda e: self.draw_bar())

        self.multibar = tk.Canvas(card, height=104, bg=PANEL, highlightthickness=0)
        self.multibar.pack(fill="x", padx=16, pady=(2, 2))
        self.scope = tk.Canvas(card, height=190, bg=PANEL, highlightthickness=0)
        self.scope.pack(fill="x", padx=16, pady=(6, 14))

        # stats
        stats = tk.Frame(self.root, bg=BG)
        stats.pack(fill="x", padx=18)
        left = tk.Frame(stats, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(stats, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        tk.Label(left, text="本次按压", bg=PANEL, fg=DIM, font=f(9)).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 4))
        self.stat_vals = {}
        rows = [("触发点", "down"), ("复位点", "up"), ("RT 距离", "rt"), ("峰值", "peak"),
                ("上升", "rise"), ("下降", "fall")]
        for i, (name, key) in enumerate(rows):
            r, c = 1 + i // 2, (i % 2) * 2
            tk.Label(left, text=name, bg=PANEL, fg=DIM, font=f(9)).grid(
                row=r, column=c, sticky="e", padx=(14, 6), pady=2)
            v = tk.Label(left, text="—", bg=PANEL, fg=FG, font=f(11, bold=True))
            v.grid(row=r, column=c + 1, sticky="w", padx=(0, 22), pady=2)
            self.stat_vals[key] = v

        tk.Label(right, text="累计", bg=PANEL, fg=DIM, font=f(9)).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 4))
        self.tot_presses = tk.Label(right, text="0 次", bg=PANEL, fg=FG, font=f(11, bold=True))
        self.tot_presses.grid(row=1, column=1, sticky="w", padx=(0, 22))
        tk.Label(right, text="按压次数", bg=PANEL, fg=DIM, font=f(9)).grid(
            row=1, column=0, sticky="e", padx=(14, 6))
        self.tot_speed = tk.Label(right, text="—", bg=PANEL, fg=FG, font=f(11, bold=True))
        self.tot_speed.grid(row=2, column=1, sticky="w", padx=(0, 22))
        tk.Label(right, text="平均速度", bg=PANEL, fg=DIM, font=f(9)).grid(
            row=2, column=0, sticky="e", padx=(14, 6))
        self.tot_peak = tk.Label(right, text="—", bg=PANEL, fg=FG, font=f(11, bold=True))
        self.tot_peak.grid(row=3, column=1, sticky="w", padx=(0, 22))
        tk.Label(right, text="最大深度", bg=PANEL, fg=DIM, font=f(9)).grid(
            row=3, column=0, sticky="e", padx=(14, 6))

        hist = tk.Frame(self.root, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        hist.pack(fill="both", expand=True, padx=18, pady=(12, 0))
        tk.Label(hist, text="最近按压", bg=PANEL, fg=DIM,
                 font=f(9)).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 4))
        cols = ["键", "峰值", "上升", "速度", "RT"]
        for i, name in enumerate(cols):
            tk.Label(hist, text=name, bg=PANEL, fg=DIM, font=f(9)).grid(
                row=0, column=i + 1, sticky="w", padx=(6, 26), pady=(10, 4))
        self.hist_rows = []
        for r in range(1, 6):
            cells = []
            for c in range(5):
                lab = tk.Label(hist, text="", bg=PANEL, fg=FG, font=f(10))
                lab.grid(row=r, column=c, sticky="w", padx=(14 if c == 0 else 6, 26), pady=1)
                cells.append(lab)
            self.hist_rows.append(cells)
        self.recent = collections.deque(maxlen=5)

        # footer
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", side="bottom", padx=18, pady=12)
        tk.Button(foot, text="导出宽表", command=self.export_wide, bg=EDGE, fg=FG,
                  activebackground="#333a45", relief="flat", font=f(9),
                  padx=14, pady=5).pack(side="right", padx=(0, 8))
        tk.Button(foot, text="导出 CSV", command=self.export_csv, bg=EDGE, fg=FG,
                  activebackground="#333a45", relief="flat", font=f(9),
                  padx=14, pady=5).pack(side="right")
        tk.Button(foot, text="清空统计", command=self.reset_stats, bg=EDGE, fg=FG,
                  activebackground="#333a45", relief="flat", font=f(9),
                  padx=14, pady=5).pack(side="right", padx=8)
        self.sample_info = tk.Label(foot, text="", bg=BG, fg=DIM, font=f(9))
        self.sample_info.pack(side="left")

    def make_font(self, size, bold=False):
        return ("Microsoft YaHei UI", size, "bold" if bold else "normal")

    # -------------------------------------------------------------- input
    def poll_keys(self, now):
        for vk in CANDIDATE_VKS:
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            was = self.vk_prev.get(vk, False)
            if down == was:
                continue
            self.vk_prev[vk] = down
            self.on_key_event(now, vk, down)

    def on_key_event(self, now, vk, down):
        # which analog key is moving right now?
        moving = [st for st in self.keys.values() if st.mm > 0.3]
        if len(moving) != 1:
            return
        st = moving[0]
        if down:
            if st.vk is None:
                st.vk = vk
                self.hint.config(text="")
            if st.vk == vk and st.press is not None and st.press.down_depth is None:
                st.press.down_depth = self.depth_near(st, now)
        elif st.vk == vk and st.press is not None and st.press.up_depth is None:
            st.press.up_depth = self.depth_near(st, now)

    def depth_near(self, st, t, window=0.030):
        best = None
        for ts, mm in reversed(st.history):
            if abs(ts - t) <= window:
                if best is None or abs(ts - t) < abs(best[0] - t):
                    best = (ts, mm)
            elif ts < t - window:
                break
        return best[1] if best else None

    # -------------------------------------------------------------- update
    def tick(self):
        now = time.perf_counter()
        if self.demo:
            self.feed_demo(now)
        n = 0
        while self.queue:
            t, rep = self.queue.pop(0)
            ev = core.decode(rep)
            if ev and ev[0] == "depth":
                self.on_depth(t, ev[1], ev[2])
                self.n_depth += 1
            elif ev and ev[0] == "event":
                self.n_event += 1
        self.rate_n += n
        if now - self.rate_t >= 1.0:
            dt = now - self.rate_t
            self.rate = self.rate_n / dt
            self.depth_per_s = self.n_depth / dt
            self.event_per_s = self.n_event / dt
            self.rate_n = 0
            self.n_depth = 0
            self.n_event = 0
            self.rate_t = now
            self.silent_warn = (self.event_per_s > 0.5 and self.depth_per_s < 0.1)
        self.poll_keys(now)
        self.render(now)
        self.root.after(15, self.tick)

    def on_depth(self, t, index, mm):
        st = self.keys.get(index)
        if st is None:
            st = self.keys[index] = KeyState(index)
        st.mm = mm
        st.history.append((t, mm))
        self.buffer.append((t, index, mm))
        if mm > 0.15:
            self.focus = index
            if st.press is None:
                st.press = Press(t, mm)
                st.press.vk = st.vk
            else:
                st.press.feed(t, mm)
        elif st.press is not None:
            st.press.feed(t, mm)
            st.press.end = t
            st.last = st.press
            st.last.vk = st.vk
            st.press = None
            st.count += 1
            if st.last.speed > 0:
                st.speeds.append(st.last.speed)
            self.total_presses += 1
            self.recent.append(st.last)

    def feed_demo(self, now):
        """Synthetic presses on several keys so the UI can be checked without hardware."""
        specs = ((31, 0x41, 1.40, 0.00), (32, 0x53, 1.90, 0.45),
                 (33, 0x44, 1.10, 0.80), (61, 0x20, 2.60, 1.30))
        for idx, vk, period, phase in specs:
            el = ((now - self.demo_start) + phase) % period
            hold = period * 0.55
            if el < 0.16:
                mm = FULL * (el / 0.16)
            elif el < hold:
                mm = FULL * (0.72 + 0.28 * abs(math.sin(el * 6.0)))
            elif el < hold + 0.14:
                mm = FULL * (1 - (el - hold) / 0.14)
            else:
                mm = 0.0
            mm = round(max(0.0, min(FULL, mm)), 2)
            st = self.keys.get(idx)
            prev = st.mm if st else 0.0
            self.on_depth(now, idx, mm)
            st = self.keys[idx]
            if st.vk is None:
                st.vk = vk
                self.hint.config(text="")
            if prev <= 0.15 < mm and st.press is not None:
                st.press.down_depth = 0.66
            if 3.4 >= mm > 0.6 and prev > 3.4 and st.press is not None:
                st.press.up_depth = mm

    # -------------------------------------------------------------- render
    def draw_bar(self):
        c = self.bar
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 20:
            return
        mm = 0.0
        if self.focus is not None and self.focus in self.keys:
            mm = self.keys[self.focus].mm
        c.create_rectangle(0, 6, w, h - 18, fill="#0d1015", outline="")
        filled = int((w - 4) * min(1.0, mm / FULL))
        color = GREEN if mm < 1.5 else (YELLOW if mm < 2.8 else RED)
        if filled > 0:
            c.create_rectangle(2, 8, 2 + filled, h - 20, fill=color, outline="")
        for tick in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
            x = 2 + int((w - 4) * tick / FULL)
            c.create_line(x, 2, x, h - 18, fill="#3a424e")
            c.create_text(min(x, w - 12), h - 1, text="%.1f" % tick, fill=DIM,
                          anchor="s", font=("Segoe UI", 7))

    def draw_multibar(self):
        c = self.multibar
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 40:
            return
        active = sorted([x for x in self.keys.values() if x.mm > 0.03],
                        key=lambda x: -x.mm)[:5]
        if not active:
            c.create_text(4, h / 2, text="\u540c\u65f6\u6309\u4e0b\u7684\u952e\u4f1a\u663e\u793a\u5728\u8fd9\u91cc",
                          fill=DIM, anchor="w", font=("Microsoft YaHei UI", 9))
            return
        row_h = min(22.0, h / max(len(active), 1))
        for i, st in enumerate(active):
            y = i * row_h
            name = key_name(st.vk) if st.vk else "#%d" % st.index
            c.create_text(2, y + row_h / 2, text=name, fill=FG, anchor="w",
                          font=("Microsoft YaHei UI", 9, "bold"))
            bx = 64
            bw = w - 64 - 74
            c.create_rectangle(bx, y + row_h * 0.28, bx + bw, y + row_h * 0.78,
                               fill="#0d1015", outline="")
            ratio = max(0.0, min(1.0, st.mm / FULL))
            col = GREEN if ratio < 0.43 else (YELLOW if ratio < 0.8 else RED)
            if ratio > 0:
                c.create_rectangle(bx, y + row_h * 0.28, bx + bw * ratio,
                                   y + row_h * 0.78, fill=col, outline="")
            c.create_text(w - 4, y + row_h / 2, text="%.2f mm" % st.mm, fill=FG,
                          anchor="e", font=("Segoe UI", 9))

    def render(self, now):
        st = self.keys.get(self.focus) if self.focus is not None else None
        name = key_name(st.vk) if (st and st.vk) else "—"
        self.key_label.config(text=name)
        self.mm_label.config(text="%.2f mm" % (st.mm if st else 0.0),
                             fg=BLUE if (st and st.mm > 0.05) else DIM)
        if self.demo:
            rate_txt = "● 模拟输入"
        else:
            rate_txt = "● 深度 %.0f/s  过阈事件 %.0f/s" % (self.depth_per_s, self.event_per_s)
        conn = ""
        if not self.demo and self.reader is not None:
            conn = "  |  ● 已连接" if self.reader.connected else "  |  ○ 等待设备(自动重连)"
        self.status.config(text="%s  |  已识别 %d 键%s"
                                % (rate_txt, sum(1 for k in self.keys.values() if k.vk), conn))
        if self.silent_warn:
            self.warn.config(
                text="键盘只发过阈事件（%0.0f/s）、不发模拟量：先切到桌面再测；仍不行就拔插键盘一次，"
                     "或打开 Gear Link 重新应用一次按键设置" % self.event_per_s)
            self.warn.pack(fill="x", padx=18, pady=(0, 6))
        else:
            self.warn.pack_forget()
        self.sample_info.config(text="buffer: %d samples" % len(self.buffer))
        self.draw_bar()
        self.draw_multibar()
        self.draw_scope(now, st)
        self.draw_stats(st)

    def draw_scope(self, now, st):
        c = self.scope
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 40:
            return
        L, R, T, B = 44, w - 10, 12, h - 22
        span = 4.0
        c.create_rectangle(L, T, R, B, outline="#20252e")
        for half in range(0, 8):
            mm = half * 0.5
            y = B - (B - T) * mm / FULL
            c.create_line(L, y, R, y, fill=GRID if half else "#2f3640")
            c.create_text(L - 6, y, text="%.1f" % mm, fill=DIM, anchor="e",
                          font=("Segoe UI", 7))
        for i in range(5):
            t = i * 1.0
            x = R - (R - L) * t / span
            c.create_line(x, T, x, B, fill=GRID)
            c.create_text(x, B + 10, text="-%ds" % int(t), fill=DIM,
                          font=("Segoe UI", 7))
        c.create_text(L - 30, T - 2, text="mm", fill=DIM, anchor="e",
                      font=("Segoe UI", 7))

        if st is None or not st.history:
            c.create_text((L + R) / 2, (T + B) / 2, text="按下按键开始测量",
                          fill=DIM, font=("Microsoft YaHei UI", 11))
            return

        shown = 0
        legend = []
        ordered = sorted(self.keys.values(),
                         key=lambda s: -(s.history[-1][0] if s.history else 0))
        for stx in ordered:
            pts = [(t, mm) for t, mm in stx.history if now - span <= t <= now]
            if len(pts) < 2:
                continue
            color = PALETTE[shown % len(PALETTE)]
            name = key_name(stx.vk) if stx.vk else "#%d" % stx.index
            width = 3 if stx is st else 2
            prev = None
            for t, mm in pts:
                x = R - (R - L) * (now - t) / span
                y = B - (B - T) * mm / FULL
                if prev and abs(t - prev[0]) < 0.12:
                    c.create_line(prev[1], prev[2], x, y, fill=color, width=width)
                prev = (t, x, y)
            legend.append((name, color))
            shown += 1
            if shown >= 6:
                break
        for i, (name, color) in enumerate(legend):
            lx = L + 8 + i * 62
            c.create_rectangle(lx, T + 6, lx + 8, T + 14, fill=color, outline="")
            c.create_text(lx + 12, T + 10, text=name, fill=DIM, anchor="w",
                          font=("Segoe UI", 7))

        press = st.last if st.last is not None else st.press
        if press is not None:
            if press.down_depth is not None:
                for t, mm in press.samples[:6]:
                    x = R - (R - L) * (now - t) / span
                    c.create_line(x, T, x, B, fill=YELLOW, dash=(1, 3))
                    c.create_text(x + 4, T + 8, text="触发 %.2fmm" % press.down_depth,
                                  fill=YELLOW, anchor="w", font=("Segoe UI", 8))
                    break
            if press.up_depth is not None:
                for t, mm in reversed(press.samples):
                    if abs(mm - press.up_depth) < 0.15:
                        x = R - (R - L) * (now - t) / span
                        c.create_line(x, T, x, B, fill=RED, dash=(1, 3))
                        c.create_text(x - 4, T + 22, text="复位 %.2fmm" % press.up_depth,
                                      fill=RED, anchor="e", font=("Segoe UI", 8))
                        break

    def draw_stats(self, st):
        press = (st.last or st.press) if st else None
        def setv(key, text, color=FG):
            self.stat_vals[key].config(text=text, fg=color)
        if press is None:
            for k in self.stat_vals:
                setv(k, "—")
        else:
            setv("down", "%.2f mm" % press.down_depth if press.down_depth is not None else "—",
                 YELLOW if press.down_depth is not None else FG)
            setv("up", "%.2f mm" % press.up_depth if press.up_depth is not None else "—",
                 RED if press.up_depth is not None else FG)
            rt = press.rt
            setv("rt", "%.2f mm" % rt if rt is not None else "—", GREEN if rt else FG)
            setv("peak", "%.2f mm" % press.peak)
            setv("rise", "%.0f ms" % press.rise_ms)
            setv("fall", "%.0f ms" % press.fall_ms)
        self.tot_presses.config(text="%d 次" % self.total_presses)
        if st and st.speeds:
            self.tot_speed.config(text="%.0f mm/s" % (sum(st.speeds) / len(st.speeds)))
        if st and st.last is not None:
            self.tot_peak.config(text="%.2f mm" % st.last.peak)

        items = list(self.recent)
        for i, row in enumerate(self.hist_rows):
            if i >= len(items):
                for lab in row:
                    lab.config(text="")
                continue
            pr = items[-(i + 1)]
            vk = pr.vk
            rt = pr.rt
            row[0].config(text=key_name(vk) if vk else "?")
            row[1].config(text="%.2f mm" % pr.peak)
            row[2].config(text="%.0f ms" % pr.rise_ms)
            row[3].config(text="%.0f mm/s" % pr.speed)
            row[4].config(text="%.2f mm" % rt if rt is not None else "—")

    # -------------------------------------------------------------- actions
    def export_csv(self):
        if not self.buffer:
            messagebox.showinfo("导出", "还没有采样数据")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialdir=HERE,
            initialfile="depth-live-%s.csv" % time.strftime("%Y%m%d-%H%M%S"),
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        rev = {st.index: st.vk for st in self.keys.values() if st.vk is not None}
        t0 = self.buffer[0][0]
        wall_iso = core.wall_iso
        with open(path, "w", newline="") as f:
            f.write("# tool=depth-gui.py\n")
            f.write("# device=ROG FALCHION ACE 75 HE  VID 0B05 PID 1C7D  channel 0xFFC0"
                    "  unit 0.01 mm\n")
            f.write("# started=%s\n" % wall_iso(t0))
            f.write("# timebase: t_s = seconds since the first stored sample"
                    " (monotonic); wall = absolute local time\n")
            f.write("t_s,wall,key,index,mm\n")
            for t, idx, mm in self.buffer:
                vk = rev.get(idx)
                f.write("%.6f,%s,%s,%d,%.2f\n" %
                        (t - t0, wall_iso(t), key_name(vk) if vk else "", idx, mm))
        messagebox.showinfo("导出", "已写出 %d 条采样\n%s" % (len(self.buffer), path))

    def export_wide(self):
        """Fixed-rate matrix (one column per key) -- easier for NLE / spreadsheet tools."""
        if not self.buffer:
            messagebox.showinfo("导出", "还没有采样数据")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialdir=HERE,
            initialfile="depth-wide-%s.csv" % time.strftime("%Y%m%d-%H%M%S"),
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        step = 0.01                     # 100 Hz grid
        t0 = self.buffer[0][0]
        rev = {st.index: st.vk for st in self.keys.values() if st.vk is not None}
        names = []
        for idx in sorted({i for _, i, _ in self.buffer}):
            vk = rev.get(idx)
            names.append((idx, key_name(vk) if vk else "k%d" % idx))
        last = {}
        cursor = collections.defaultdict(int)
        series = collections.defaultdict(list)
        for t, idx, mm in self.buffer:
            series[idx].append((t - t0, mm))
        duration = max(series[i][-1][0] for i in series)
        wall_iso = core.wall_iso
        with open(path, "w", newline="") as f:
            f.write("# tool=depth-gui.py  wide matrix, %.0f Hz, depth in mm (0..%.2f)\n"
                    % (1.0 / step, FULL))
            f.write("# started=%s\n" % wall_iso(t0))
            f.write("# columns: t_s,wall," + ",".join(n for _, n in names) + "\n")
            f.write("t_s,wall," + ",".join(n for _, n in names) + "\n")
            n = int(duration / step) + 1
            for k in range(n):
                ts = k * step
                row = []
                for idx, _ in names:
                    arr = series[idx]
                    i = cursor[idx]
                    while i + 1 < len(arr) and arr[i + 1][0] <= ts:
                        i += 1
                    cursor[idx] = i
                    if not arr:
                        row.append("0.00")
                        continue
                    if ts < arr[0][0]:
                        row.append("0.00")
                        continue
                    if i + 1 < len(arr) and arr[i + 1][0] - arr[i][0] <= 0.12:
                        a, b = arr[i], arr[i + 1]
                        f2 = (ts - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                        row.append("%.2f" % (a[1] + (b[1] - a[1]) * f2))
                    else:
                        row.append("%.2f" % (arr[i][1] if ts - arr[i][0] < 0.12 else 0.0))
                f.write("%.3f,%s,%s\n" % (ts, wall_iso(t0 + ts), ",".join(row)))
        messagebox.showinfo("导出宽表",
                            "已写出 %d 行 × %d 键（%.0f Hz）\n%s"
                            % (n, len(names), 1.0 / step, path))


    def reset_stats(self):
        for st in self.keys.values():
            st.count = 0
            st.speeds.clear()
            st.last = None
        self.total_presses = 0

    def on_close(self):
        try:
            if self.reader:
                self.reader.stop = True
                if self.reader.handle:
                    kernel32.CancelIoEx(self.reader.handle, None)
        except Exception:
            pass
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--vid", default="0B05")
    ap.add_argument("--pid", default="1C7D")
    args = ap.parse_args()

    info = None
    if not args.demo:
        info = core.find_depth_collection(int(args.vid, 16), int(args.pid, 16))
        if not info:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("未找到设备",
                                 "找不到磁轴深度通道。\n"
                                 "请确认键盘已连接，或先用 hid-probe.py descriptors 排查。")
            return 2
    root = tk.Tk()
    App(root, info, demo=args.demo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
