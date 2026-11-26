# -*- coding: utf-8 -*-
"""depth-probe.py -- measure magnetic (Hall effect) key travel depth in mm, live.

Verified on: ROG FALCHION ACE 75 HE (VID 0B05 PID 1C7D)
  HID collection: usage page 0xFFC0, input report 21 bytes
  report[1] == 0x7E : analog key update
      report[2]    = key index (matrix position, A=31 S=32 D=33 ...)
      report[4..5] = depth, uint16 little endian, unit 0.01 mm, range 0..350 (=3.50 mm)
  report[1] == 0x71 : key crossing event (report[3]=0x10 down / 0x00 up)
  Stream is change-driven at ~100 Hz (10 ms), both directions (press and release).

Modes:
  python depth-probe.py                       learn keys, then record 20 s, print report
  python depth-probe.py --seconds 60          longer session
  python depth-probe.py --replay hid-reports-X.csv --keys hid-keys-X.csv
  python depth-probe.py --svg curve.svg       also write an SVG of the deepest press
"""

import argparse
import collections
import datetime
import csv
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes as wt

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
hid = ctypes.WinDLL("hid")
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
LLKHF_INJECTED = 0x10

MM = 0.01            # report unit -> mm
FULL = 3.50          # observed full travel of this keyboard, mm


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


GUID_HID = GUID(0x4D1E55B2, 0xF16F, 0x11CF,
                (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))


class SP_INTERFACE(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class SP_DETAIL_W(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("DevicePath", wt.WCHAR * 1)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.DWORD), ("VendorID", wt.WORD),
                ("ProductID", wt.WORD), ("VersionNumber", wt.WORD)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.WORD), ("UsagePage", wt.WORD),
                ("InputReportByteLength", wt.WORD), ("OutputReportByteLength", wt.WORD),
                ("FeatureReportByteLength", wt.WORD), ("Reserved", wt.WORD * 17),
                ("NumberLinkCollectionNodes", wt.WORD),
                ("NumberInputButtonCaps", wt.WORD), ("NumberInputValueCaps", wt.WORD),
                ("NumberInputDataIndices", wt.WORD),
                ("NumberOutputButtonCaps", wt.WORD), ("NumberOutputValueCaps", wt.WORD),
                ("NumberOutputDataIndices", wt.WORD),
                ("NumberFeatureButtonCaps", wt.WORD), ("NumberFeatureValueCaps", wt.WORD),
                ("NumberFeatureDataIndices", wt.WORD)]


setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), ctypes.c_wchar_p,
                                         ctypes.c_void_p, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.POINTER(GUID), wt.DWORD,
                                                ctypes.POINTER(SP_INTERFACE)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_INTERFACE), ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]

FAIL = "  [FAIL]"


# ------------------------------------------------------------------ HID layer

def interface_paths():
    out = []
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(GUID_HID), None, None,
                                        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        return out
    try:
        idx = 0
        while True:
            iface = SP_INTERFACE()
            iface.cbSize = ctypes.sizeof(SP_INTERFACE)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(GUID_HID),
                                                       idx, ctypes.byref(iface)):
                break
            idx += 1
            buf = ctypes.create_string_buffer(2048)
            need = wt.DWORD(0)
            struct = ctypes.cast(buf, ctypes.POINTER(SP_DETAIL_W))
            struct.contents.cbSize = ctypes.sizeof(SP_DETAIL_W)
            if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), buf, 2048,
                                                         ctypes.byref(need), None):
                off = SP_DETAIL_W.DevicePath.offset
                out.append(ctypes.wstring_at(ctypes.addressof(buf) + off))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


def caps(path):
    h = kernel32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        return None
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if not hid.HidD_GetAttributes(h, ctypes.byref(attrs)):
            return None
        info = {"path": path, "vid": attrs.VendorID, "pid": attrs.ProductID,
                "in_len": 0, "usage_page": 0}
        preparsed = ctypes.c_void_p()
        cap = HIDP_CAPS()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(preparsed)):
            try:
                if hid.HidP_GetCaps(preparsed, ctypes.byref(cap)):
                    info["in_len"] = cap.InputReportByteLength
                    info["usage_page"] = cap.UsagePage
                    info["feat_len"] = cap.FeatureReportByteLength
            finally:
                hid.HidD_FreePreparsedData(preparsed)
        return info
    finally:
        kernel32.CloseHandle(h)


def find_depth_collection(vid, pid):
    """The analog stream is the vendor page collection with a 21 byte input report."""
    cands = []
    for path in interface_paths():
        low = path.lower()
        if ("vid_%04x" % vid) not in low or ("pid_%04x" % pid) not in low:
            continue
        info = caps(path)
        if info and info["in_len"] >= 6 and info["usage_page"] >= 0xFF00:
            cands.append(info)
    for info in cands:
        if info["in_len"] in (21, 22, 23):
            return info
    return cands[0] if cands else None


def decode(report):
    """-> ('depth', key_index, mm) | ('event', key_index, flags) | None"""
    n = len(report)
    if n < 6 or report[0] != 0x03:
        return None
    if report[1] == 0x7E:
        return ("depth", report[2], ((report[4] | (report[5] << 8)) * MM))
    if report[1] == 0x71:
        return ("event", report[2], report[3])
    return None


# --------------------------------------------------------------- keyboard hook

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]

key_events = []
held = {}


def _hook(n_code, w_param, l_param):
    if n_code == HC_ACTION:
        try:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not (info.flags & LLKHF_INJECTED):
                t = time.perf_counter()
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if info.vkCode not in held:
                        held[info.vkCode] = t
                        key_events.append((t, info.vkCode, 1))
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    held.pop(info.vkCode, None)
                    key_events.append((t, info.vkCode, 0))
        except Exception:
            pass
    return user32.CallNextHookEx(None, n_code, w_param, l_param)


# ------------------------------------------------------------------- capture

class Reader(threading.Thread):
    daemon = True

    def __init__(self, path, length, sink):
        threading.Thread.__init__(self)
        self.path = path
        self.length = max(length, 21)
        self.sink = sink
        self.handle = None
        self.error = None

    def run(self):
        h = kernel32.CreateFileW(self.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE:
            self.error = ctypes.get_last_error()
            return
        self.handle = h
        buf = (ctypes.c_ubyte * self.length)()
        nread = wt.DWORD(0)
        while not self.stop:
            if not kernel32.ReadFile(h, buf, self.length, ctypes.byref(nread), None):
                if ctypes.get_last_error() in (995, 1167):
                    return
                time.sleep(0.02)
                continue
            self.sink.append((time.perf_counter(), bytes(buf[:nread.value])))

    stop = False


def bar(mm, width=28):
    filled = int(round(mm / FULL * width))
    return "|" + "#" * filled + "." * (width - filled) + "|"


def run_live(args):
    info = find_depth_collection(args.vid, args.pid)
    if not info:
        print("no vendor HID collection found for VID %04X PID %04X" % (args.vid, args.pid))
        return 2
    print("depth channel: %s" % info["path"].split("#")[1])
    print("  usage page 0x%04X, input report %d bytes" % (info["usage_page"], info["in_len"]))

    reports = []
    reader = Reader(info["path"], info["in_len"] or 21, reports)
    reader.start()
    time.sleep(0.2)
    if reader.error:
        print("open failed err=%d (another app holds this collection?)" % reader.error)
        return 3

    hook_ref = HOOKPROC(_hook)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_ref, None, 0)

    print("\n--- learning step: press each key once, a bit more than half way ---")
    print("    (A S D W Space etc). 12 s.\n")
    vk_of_index = collections.defaultdict(collections.Counter)
    t_end = time.perf_counter() + args.learn
    msg = wt.MSG()
    last_print = 0
    while time.perf_counter() < t_end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        while reports:
            t, rep = reports.pop(0)
            ev = decode(rep)
            if ev and ev[0] == "depth" and ev[2] > 0.3 and len(held) == 1:
                vk_of_index[ev[1]][list(held)[0]] += 1
        now = time.perf_counter()
        if now - last_print > 0.1:
            last_print = now
            live = [ev for ev in (decode(r) for _, r in reports[-1:]) if ev and ev[0] == "depth"]
            left = int(t_end - now)
            sys.stdout.write("\r  learning... %2ds  keys mapped: %d  " %
                             (max(0, left), len(vk_of_index)))
            sys.stdout.flush()
        time.sleep(0.001)
    print("\n")

    index_of_vk = {}
    for idx, counter in vk_of_index.items():
        vk = counter.most_common(1)[0][0]
        index_of_vk[vk] = idx
    print("mapped: " + "  ".join("%s->%d" % (vk_name(v), i)
                                  for v, i in sorted(index_of_vk.items(), key=lambda x: x[1])))

    print("\n--- measuring: %ds. Press slowly, press fast, hold, release slowly ---\n"
          % int(args.seconds))
    depth_samples = []
    events = []
    reports[:] = []
    key_events[:] = []
    t_end = time.perf_counter() + args.seconds
    while time.perf_counter() < t_end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        while reports:
            t, rep = reports.pop(0)
            ev = decode(rep)
            if ev and ev[0] == "depth":
                depth_samples.append((t, ev[1], ev[2]))
            elif ev and ev[0] == "event":
                events.append((t, ev[1], ev[2]))
        time.sleep(0.001)

    for h in (reader.handle,):
        if h:
            kernel32.CancelIoEx(h, None)
    reader.stop = True
    if hook:
        user32.UnhookWindowsHookEx(hook)

    # echo what the hardware reported, per key, as a live check
    peaks = {}
    for _, idx, mm in depth_samples:
        peaks[idx] = max(peaks.get(idx, 0.0), mm)
    print("peak depth seen per key index: " +
          "  ".join("%d:%.2f" % (i, m) for i, m in sorted(peaks.items())))
    stats = analyze(depth_samples, key_events, index_of_vk)
    print_report(stats)
    write_csv(depth_samples, index_of_vk, events)
    if args.svg:
        write_svg(stats, args.svg)
    if args.png:
        write_png(stats, args.png)
    return 0


def vk_name(vk):
    special = {0x20: "Space", 0x08: "Bksp", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc",
               0xA0: "LShift", 0xA1: "RShift", 0xA2: "LCtrl", 0xA3: "RCtrl", 0x10: "Shift"}
    if vk in special:
        return special[vk]
    try:
        return chr(vk)
    except ValueError:
        return "0x%02X" % vk


# ------------------------------------------------------------------ analysis

def analyze(samples, keys, index_of_vk):
    """Split the depth stream into presses and derive travel profile stats."""
    presses = []
    by_index = collections.defaultdict(list)
    for t, idx, mm in samples:
        by_index[idx].append((t, mm))

    down_t = {}
    hold_events = []
    for t, vk, d in keys:
        if d:
            down_t.setdefault(vk, t)
        else:
            t0 = down_t.pop(vk, None)
            if t0 is not None:
                hold_events.append((t0, t, vk))

    for idx, seq in by_index.items():
        seq.sort()
        active = None
        for t, mm in seq:
            if mm > 0.15 and active is None:
                active = [(t, mm)]
            elif active is not None:
                active.append((t, mm))
                if mm <= 0.15 and len(active) > 3:
                    presses.append({"index": idx, "samples": active[:]})
                    active = None
        if active and len(active) > 3:
            presses.append({"index": idx, "samples": active})

    for p in presses:
        seq = p["samples"]
        p["peak"] = max(mm for _, mm in seq)
        peak_i = max(range(len(seq)), key=lambda i: seq[i][1])
        first_peak = min(i for i, (_, mm) in enumerate(seq) if mm >= p["peak"] - 0.02)
        last_peak = max(i for i, (_, mm) in enumerate(seq) if mm >= p["peak"] - 0.02)
        p["rise_ms"] = (seq[first_peak][0] - seq[0][0]) * 1000
        p["fall_ms"] = (seq[-1][0] - seq[last_peak][0]) * 1000 if last_peak < len(seq) - 1 else 0
        p["bottom_ms"] = (seq[last_peak][0] - seq[0][0]) * 1000
        p["speed"] = ((p["peak"] - seq[0][1]) / p["rise_ms"] * 1000) if p["rise_ms"] > 1 else 0
        p["vk"] = None
        for vk, i in index_of_vk.items():
            if i == p["index"]:
                p["vk"] = vk
        # align with the OS key event of the same key
        if p["vk"] is not None:
            for t0, t1, vk in hold_events:
                if vk == p["vk"] and t0 - 0.05 <= seq[0][0] <= t0 + 0.25:
                    p["down_at"] = _depth_at(seq, t0)
                    p["up_at"] = _depth_at(seq, t1)
                    p["press_latency"] = (t0 - seq[0][0]) * 1000
                    if p.get("up_at") is not None and p["peak"] >= 3.0:
                        p["rt"] = p["peak"] - p["up_at"]
                    break
    return {"presses": presses, "peaks": {p["index"]: p["peak"] for p in presses}}


def _depth_at(seq, t, window=0.012):
    """Depth the key was at, linearly interpolated at time t."""
    before = [(ts, mm) for ts, mm in seq if ts <= t]
    after = [(ts, mm) for ts, mm in seq if ts > t]
    if before and after:
        t0, d0 = before[-1]
        t1, d1 = after[0]
        if t1 - t0 <= 0.05:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return d0 + (d1 - d0) * f
        return d0
    if before and abs(t - before[-1][0]) <= window:
        return before[-1][1]
    if after and abs(after[0][0] - t) <= window:
        return after[0][1]
    return None


def print_report(stats):
    presses = stats["presses"]
    if not presses:
        print("\nno press segments detected -- was the collection readable and did you press keys?")
        return
    print("\n=== %d press segments ===" % len(presses))
    print("%-6s %6s %8s %8s %8s %9s %8s %8s" %
          ("key", "index", "peak", "rise", "fall", "speed", "down@", "up@"))
    for p in sorted(presses, key=lambda p: -p["peak"])[:15]:
        print("%-6s %6d %7.2fmm %7.0fms %7.0fms %8.0fmm/s %7s %7s" % (
            vk_name(p["vk"]) if p["vk"] is not None else "?",
            p["index"], p["peak"], p["rise_ms"], p["fall_ms"], p["speed"],
            ("%.2f" % p["down_at"]) if p.get("down_at") is not None else "-",
            ("%.2f" % p["up_at"]) if p.get("up_at") is not None else "-"))
    peaks = [p["peak"] for p in presses]
    rises = [p["rise_ms"] for p in presses if p["rise_ms"] > 1]
    speeds = [p["speed"] for p in presses if p["speed"] > 0]
    if peaks:
        print("\npeak depth: max %.2f mm   median %.2f mm   (full travel %.2f mm)"
              % (max(peaks), sorted(peaks)[len(peaks) // 2], FULL))
    if rises:
        print("press ramp: median %.0f ms" % sorted(rises)[len(rises) // 2])
    if speeds:
        print("press speed: median %.0f mm/s   max %.0f mm/s"
              % (sorted(speeds)[len(speeds) // 2], max(speeds)))
    downs = [p["down_at"] for p in presses if p.get("down_at") is not None]
    if downs:
        print("depth when the OS saw key-down (actuation point): median %.2f mm  (n=%d)"
              % (sorted(downs)[len(downs) // 2], len(downs)))
    ups = [p["up_at"] for p in presses if p.get("up_at") is not None]
    if ups:
        print("depth when the OS saw key-up: median %.2f mm  (n=%d)"
              % (sorted(ups)[len(ups) // 2], len(ups)))
    rts = [p["rt"] for p in presses if p.get("rt") is not None]
    if rts:
        print("rapid trigger: release fired after rising median %.2f mm from the bottom (n=%d)"
              % (sorted(rts)[len(rts) // 2], len(rts)))
    lat = [p["press_latency"] for p in presses if p.get("press_latency") is not None]
    if lat:
        print("first depth sample -> OS key-down: median %.1f ms"
              % sorted(lat)[len(lat) // 2])


def wall_iso(t_perf):
    """Absolute local wall-clock time of a perf_counter timestamp, ISO 8601 with ms."""
    delta = time.perf_counter() - t_perf
    return datetime.datetime.fromtimestamp(time.time() - delta).astimezone().isoformat(
        timespec="milliseconds")


def write_csv(samples, index_of_vk, events):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "depth-%s.csv" % stamp)
    rev = {i: v for v, i in index_of_vk.items()}
    t0 = samples[0][0] if samples else time.perf_counter()
    with open(path, "w", newline="") as f:
        f.write("# tool=depth-probe.py\n")
        f.write("# device=ROG FALCHION ACE 75 HE  VID 0B05 PID 1C7D  channel 0xFFC0"
                "  unit 0.01 mm\n")
        f.write("# started=%s\n" % wall_iso(t0))
        f.write("# timebase: t_s = seconds since started (monotonic, immune to clock changes);"
                " wall = absolute local time\n")
        f.write("t_s,wall,key,index,mm\n")
        for t, idx, mm in samples:
            f.write("%.6f,%s,%s,%d,%.2f\n" % (
                t - t0, wall_iso(t), vk_name(rev[idx]) if idx in rev else "", idx, mm))
    print("\nwrote %s (%d samples)" % (os.path.basename(path), len(samples)))
    return path


def write_svg(stats, path):
    presses = stats["presses"]
    if not presses:
        return
    p = max(presses, key=lambda p: p["rise_ms"])
    seq = p["samples"]
    t0 = seq[0][0]
    xs = [(t - t0) * 1000 for t, _ in seq]
    ys = [mm for _, mm in seq]
    W, H, L, B = 900, 320, 60, 40
    span_x = max(xs) or 1
    def px(x):
        return L + (W - L - 20) * x / span_x
    def py(y):
        return H - B - (H - B - 20) * y / FULL

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'font-family="Segoe UI, sans-serif" font-size="12">' % (W, H),
           '<rect width="100%" height="100%" fill="#12151b"/>']
    for mm in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#2a2f38"/>'
                   % (L, py(mm), W - 20, py(mm)))
        out.append('<text x="%d" y="%.1f" fill="#7d8590" text-anchor="end">%.1f</text>'
                   % (L - 8, py(mm) + 4, mm))
    pts = " ".join("%.1f,%.1f" % (px(x), py(y)) for x, y in zip(xs, ys))
    out.append('<polyline fill="none" stroke="#3fb6ff" stroke-width="2" points="%s"/>' % pts)
    out.append('<text x="%d" y="20" fill="#e6edf3">%s travel: peak %.2f mm, rise %.0f ms, '
               'fall %.0f ms, %.0f mm/s</text>'
               % (L, vk_name(p["vk"]) if p["vk"] is not None else "key",
                  p["peak"], p["rise_ms"], p["fall_ms"], p["speed"]))
    out.append('<text x="%d" y="%d" fill="#7d8590">time (ms)</text>' % (W - 90, H - 12))
    out.append('<text x="14" y="%d" fill="#7d8590" transform="rotate(-90 20,%d)">'
               'depth (mm)</text>' % (H // 2, H // 2))
    out.append("</svg>")
    open(path, "w", encoding="utf-8").write("\n".join(out))
    print("wrote %s" % os.path.basename(path))


# -------------------------------------------------------------------- replay

def run_replay(args):
    base = os.path.dirname(os.path.abspath(__file__))
    samples = []
    t_origin = None
    with open(os.path.join(base, args.replay)) as f:
        rows = [ln for ln in f if not ln.lstrip().startswith("#")]
        for row in csv.DictReader(rows):
            rep = bytes.fromhex(row["hex"])
            t = float(row["t"])
            if t_origin is None:
                t_origin = t
            ev = decode(rep)
            if ev and ev[0] == "depth":
                samples.append((t, ev[1], ev[2]))
    keys = []
    if args.keys:
        with open(os.path.join(base, args.keys)) as f:
            rows = [ln for ln in f if not ln.lstrip().startswith("#")]
            for row in csv.DictReader(rows):
                keys.append((float(row["t"]), int(row["vk"], 16), int(row["down"])))

    # learn vk -> index from the replay itself
    down = {}
    iv = []
    for t, vk, d in keys:
        if d:
            down.setdefault(vk, t)
        else:
            t0 = down.pop(vk, None)
            if t0 is not None:
                iv.append((t0, t, vk))
    votes = collections.defaultdict(collections.Counter)
    for t, idx, mm in samples:
        if mm <= 0.3:
            continue
        cur = [vk for a, b, vk in iv if a - 0.004 <= t <= b + 0.004]
        if len(cur) == 1:
            votes[cur[0]][idx] += 1
    index_of_vk = {}
    taken = set()
    for vk, c in sorted(votes.items(), key=lambda kv: -kv[1].most_common(1)[0][1]):
        idx = c.most_common(1)[0][0]
        if idx not in taken and c.most_common(1)[0][1] >= 3:
            index_of_vk[vk] = idx
            taken.add(idx)
    print("replay: %d depth samples, %d key events, %d keys mapped"
          % (len(samples), len(keys), len(index_of_vk)))
    stats = analyze(samples, keys, index_of_vk)
    print_report(stats)
    if args.svg:
        write_svg(stats, args.svg)
    if args.png:
        write_png(stats, args.png)
    return 0



FONT_3x5 = {
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
    ".": ("000", "000", "000", "110", "110"), "-": ("000", "000", "111", "000", "000"),
    "m": ("000", "111", "111", "101", "101"), "s": ("011", "100", "010", "001", "110"),
    " ": ("000", "000", "000", "000", "000"),
}


def _text(put, x, y, text, rgb, scale=2):
    for ch in text:
        glyph = FONT_3x5.get(ch, FONT_3x5[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            put(x + gx * scale + sx, y + gy * scale + sy, rgb)
        x += 4 * scale


def write_png(stats, path, W=900, H=340):
    """Minimal dependency-free PNG (zlib is stdlib): depth-vs-time of one press."""
    import struct
    import zlib

    presses = [p for p in stats["presses"] if p["peak"] >= 3.4 and p["rise_ms"] >= 40]
    if not presses:
        presses = stats["presses"]
    if not presses:
        return
    p = max(presses, key=lambda p: p["rise_ms"])
    seq = p["samples"]
    t0 = seq[0][0]

    px = bytearray(b"\x14\x18\x22" * (W * H))

    def put(x, y, rgb):
        if 0 <= x < W and 0 <= y < H:
            i = (y * W + x) * 3
            px[i:i + 3] = rgb

    def line(x0, y0, x1, y1, rgb):
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx - dy
        while True:
            put(x0, y0, rgb)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    L, R, T, B = 60, W - 20, 20, H - 40
    span = ((seq[-1][0] - t0) * 1000) or 1

    def gx(t):
        return L + int((R - L) * (t - t0) * 1000 / span)

    def gy(mm):
        return B - int((B - T) * mm / FULL)

    for half in range(1, 8):
        y = gy(half * 0.5)
        for x in range(L, R, 6):
            put(x, y, (42, 47, 56))
    line(L, B, R, B, (70, 78, 90))
    line(L, T, L, B, (70, 78, 90))

    prev = None
    for t, mm in seq:
        cur = (gx(t), gy(mm))
        if prev:
            line(prev[0], prev[1], cur[0], cur[1], (63, 182, 255))
        prev = cur

    # markers: where the OS fired key-down (yellow) and key-up (red)
    if p.get("down_at") is not None:
        x = L
        for t, mm in seq:
            if (t - t0) * 1000 > 0 and abs(mm - p["down_at"]) < 0.12:
                x = gx(t)
                break
        for yy in range(T, B, 4):
            put(x, yy, (255, 196, 0))
    if p.get("up_at") is not None:
        for t, mm in reversed(seq):
            if abs(mm - p["up_at"]) < 0.12:
                x = gx(t)
                for yy in range(T, B, 4):
                    put(x, yy, (255, 90, 90))
                break
    for half in range(0, 8):
        mm = half * 0.5
        _text(put, 8, gy(mm) - 4, ("%.1f" % mm), (140, 150, 165))
    step = max(100, int(span / 5 / 100) * 100)
    for ms in range(0, int(span) + 1, step):
        x = L + int((R - L) * ms / span)
        if x > R - 40:
            continue
        line(x, B, x, B + 4, (70, 78, 90))
        _text(put, x - 6, B + 8, str(ms), (140, 150, 165))
    _text(put, R - 22, B + 22, "ms", (140, 150, 165))

    raw = b"".join(b"\x00" + bytes(px[y * W * 3:(y + 1) * W * 3]) for y in range(H))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    print("wrote %s  (key %s: peak %.2f mm, rise %.0f ms, fall %.0f ms)"
          % (os.path.basename(path), vk_name(p["vk"]) if p["vk"] is not None else "?",
             p["peak"], p["rise_ms"], p["fall_ms"]))


def main():
    p = argparse.ArgumentParser(description="measure magnetic switch key depth in mm")
    p.add_argument("--vid", default="0B05")
    p.add_argument("--pid", default="1C7D")
    p.add_argument("--learn", type=float, default=12.0)
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--replay")
    p.add_argument("--keys")
    p.add_argument("--svg")
    p.add_argument("--png")
    args = p.parse_args()
    args.vid = int(args.vid, 16)
    args.pid = int(args.pid, 16)
    try:
        kernel32.timeBeginPeriod(1)
    except Exception:
        pass
    if args.replay:
        return run_replay(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
