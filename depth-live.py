# -*- coding: utf-8 -*-
"""depth-live.py -- magnetic switch depth monitor with a local web UI.

Reads the ROG Falchion Ace 75 HE analog channel (usage page 0xFFC0,
report 03 7E <key> 00 <depth uint16 LE>, 0.01 mm units) and serves a
live page on 127.0.0.1.  No external dependencies, no server config:
one process, one browser tab, smooth 60 fps rendering.

  python depth-live.py                 start and open the browser
  python depth-live.py --no-browser    only print the URL
  python depth-live.py --port 8800     fixed port
"""

import argparse
import collections
import ctypes
import hashlib
import io
import json
import os
import secrets
import socket
import threading
import time
import webbrowser
from ctypes import wintypes as wt
try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                      # Python < 3.7
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import socketserver

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
from urllib.parse import urlparse, parse_qs, urlsplit

FULL = 3.50


def data_home():
    """Where mapping/logs live.  Tests point this at a temp dir."""
    h = os.environ.get("DEPTH_MONITOR_HOME")
    if h:
        return h
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "depth-monitor")


def recordings_dir():
    """Where saved recordings go.  Isolated together with data_home()."""
    h = os.environ.get("DEPTH_MONITOR_HOME")
    if h:
        return os.path.join(h, "recordings")
    return os.path.join(os.path.expanduser("~"), "Documents", "depth-recordings")
VERSION = "1.10.1"
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
hid = ctypes.WinDLL("hid")
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CancelIoEx.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]


# ------------------------------------------------------------------ HID layer

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


GUID_HID = GUID(0x4D1E55B2, 0xF16F, 0x11CF,
                (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))


class SP_INTERFACE(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class SP_DETAIL(ctypes.Structure):
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
hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]


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
            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), None, 0,
                                                     ctypes.byref(need), None)
            if need.value <= 0:
                continue
            size = max(need.value, ctypes.sizeof(SP_DETAIL) + 2)
            buf = ctypes.create_string_buffer(size)
            st = ctypes.cast(buf, ctypes.POINTER(SP_DETAIL))
            st.contents.cbSize = ctypes.sizeof(SP_DETAIL)
            if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), buf, size,
                                                        ctypes.byref(need), None):
                off = SP_DETAIL.DevicePath.offset
                out.append(ctypes.wstring_at(ctypes.addressof(buf) + off))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


def probe(path):
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
        pre = ctypes.c_void_p()
        caps = HIDP_CAPS()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(pre)):
            try:
                if hid.HidP_GetCaps(pre, ctypes.byref(caps)):
                    info["in_len"] = caps.InputReportByteLength
                    info["usage_page"] = caps.UsagePage
            finally:
                hid.HidD_FreePreparsedData(pre)
        return info
    finally:
        kernel32.CloseHandle(h)


def find_depth_collection(vid=0x0B05, pid=0x1C7D):
    cands = []
    for path in interface_paths():
        low = path.lower()
        if ("vid_%04x" % vid) not in low or ("pid_%04x" % pid) not in low:
            continue
        info = probe(path)
        if info and info["in_len"] >= 6 and info["usage_page"] >= 0xFF00:
            cands.append(info)
    for info in cands:
        if info["in_len"] in (21, 22, 23):
            return info
    return cands[0] if cands else None


def decode(report):
    if len(report) < 6 or report[0] != 0x03:
        return None
    if report[1] == 0x7E:
        return ("depth", report[2], (report[4] | (report[5] << 8)) * 0.01)
    if report[1] == 0x71:
        return ("event", report[2], report[3])
    return None


# ------------------------------------------------------------------- engine

KEYNAME = {0x20: "Space", 0x08: "Bksp", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc",
           0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt", 0xA0: "LShift", 0xA1: "RShift",
           0xA2: "LCtrl", 0xA3: "RCtrl", 0xA4: "LAlt", 0xA5: "RAlt",
           0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
           0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}

CAND_VKS = (list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) +
            [0x20, 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12, 0x1B,
             0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
             0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE])


VK_BY_LABEL = None


def label_to_vk(label):
    global VK_BY_LABEL
    if VK_BY_LABEL is None:
        VK_BY_LABEL = {}
        for vk in CAND_VKS:
            VK_BY_LABEL.setdefault(key_name(vk), vk)
    return VK_BY_LABEL.get(label)


def key_name(vk):
    if vk in KEYNAME:
        return KEYNAME[vk]
    try:
        return chr(vk).upper()
    except ValueError:
        return "0x%02X" % vk


class Engine(object):
    def __init__(self):
        self.lock = threading.RLock()   # re-entrant: run_keys holds it while _depth_near locks again
        self.samples = []            # (t, index, mm) live ring, for the UI only
        self._abs = 0                # absolute sample counter (monotonic)
        self.recording = False
        self.rec = []                # (t, index, mm) only while recording
        self.rec_t0 = None
        self.keys = {}               # index -> {"vk":..,"mm":..}
        self.recent = collections.deque(maxlen=12)
        self.presses = 0
        self.n_depth = 0
        self.n_event = 0
        self.depth_rate = 0.0
        self.event_rate = 0.0
        self.connected = False
        self.last_press = None
        self.press_state = {}        # index -> dict
        self._mark = 0               # absolute sample counter for SSE cursors
        self.t0 = time.time()
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        data_home = os.environ.get("DEPTH_MONITOR_HOME")
        if not data_home:
            data_home = os.path.join(
                os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "depth-monitor")
        self.map_path = os.path.join(data_home, "mapping.json")
        self.mapping = {}
        self.pending_bind = False
        self.pending_label = None
        self.bind_msg = ""
        self.notice = ""
        self.last_hotkey = None
        self.last_client = time.time()
        self.stop_io = threading.Event()
        self.token = secrets.token_hex(8)
        self.hold_n = 0
        self.auto = False
        self.vk_held = {}            # vk -> True while the OS reports it down
        self.key_state = {}          # vk -> {"gen": mapping generation, "ts": float}
        self.pending_learn = None    # (vk, deadline) waiting for its analog report
        self.mapping_gen = 0         # bumped whenever the index<->vk table changes
        self.suppressed = 0          # firmware switch-zero reports ignored
        self.released_by_watchdog = 0
        self._load_mapping()

    # ---- mapping persistence ----------------------------------------
    def _map_paths(self):
        paths = [self.map_path]
        appdata = os.environ.get("APPDATA")
        if appdata and not os.environ.get("DEPTH_MONITOR_HOME"):
            paths.append(os.path.join(appdata, "depth-monitor", "mapping.json"))
        return paths

    def _load_mapping(self):
        self.mapping = {}
        for path in self._map_paths():
            try:
                with io.open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                for k, v in raw.items():
                    self.mapping[int(k)] = int(v)
                if self.mapping:
                    self.map_path = path
                    self.bump_mapping()
                    return
            except Exception:
                continue

    def _write_atomic(self, path, text):
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            try:
                os.makedirs(d)
            except Exception:
                pass
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _save_mapping(self):
        text = json.dumps({str(k): v for k, v in self.mapping.items()})
        for path in self._map_paths():
            for attempt in range(3):
                try:
                    self._write_atomic(path, text)
                    if os.path.exists(path):
                        self.map_path = path
                        return True
                except Exception:
                    time.sleep(0.05)
        return False

    def reset_mapping(self):
        with self.lock:
            self.mapping = {}
            for st in self.keys.values():
                st["vk"] = None
                st["mm"] = 0.0
            # the old physical state must not leak into the new mapping
            self.vk_held.clear()
            self.key_state.clear()
            self.press_state.clear()
            self.pending_bind = False
            self.bump_mapping()
        self._save_mapping()

    def bind_by_press(self):
        """Next physical key press is bound to whichever analog index moves most."""
        with self.lock:
            self.pending_bind = True

    def _delta(self, idx, now, span=0.05):
        """Depth gained over the last `span` seconds for one key index."""
        first = None
        with self.lock:
            for ts, i, mm in reversed(self.samples[-400:]):
                if i == idx:
                    first = mm
                if now - ts > span:
                    break
        cur = self.keys.get(idx, {}).get("mm", 0.0)
        return 0.0 if first is None else cur - first

    # ---- reader -----------------------------------------------------
    def run_reader(self):
        while True:
            info = find_depth_collection()
            if not info:
                self.connected = False
                time.sleep(1.0)
                continue
            h = kernel32.CreateFileW(info["path"], GENERIC_READ,
                                     FILE_SHARE_READ | FILE_SHARE_WRITE,
                                     None, OPEN_EXISTING, 0, None)
            if h == INVALID_HANDLE_VALUE:
                self.connected = False
                time.sleep(1.0)
                continue
            self.connected = True
            self.io_handle = h
            length = max(info.get("in_len") or 21, 21)
            buf = (ctypes.c_ubyte * length)()
            nread = wt.DWORD(0)
            try:
                while not self.stop_io.is_set():
                    if not kernel32.ReadFile(h, buf, length, ctypes.byref(nread), None):
                        break
                    t = time.perf_counter()
                    n = nread.value
                    if n <= 0 or n > length:
                        continue
                    ev = decode(bytes(buf[:n]))
                    if ev is None:
                        continue
                    if ev[0] == "depth":
                        self.on_depth(t, ev[1], ev[2])
                    else:
                        self.n_event += 1
            except Exception:
                _log_exc("run_reader loop")
            finally:
                self.connected = False
                self.opened = False
                try:
                    kernel32.CancelIoEx(h, None)
                except Exception:
                    pass
                try:
                    kernel32.CloseHandle(h)
                except Exception:
                    pass
            time.sleep(0.6)

    def bump_mapping(self):
        """The index<->vk table changed: old physical state must not be trusted."""
        self.mapping_gen += 1
        for st in self.key_state.values():
            st["gen"] = -1

    def os_key_down(self, vk):
        """True only if the OS says the key is down AND that reading belongs to
        the current mapping generation."""
        if not self.vk_held.get(vk):
            return False
        st = self.key_state.get(vk)
        if st is None:
            return True
        return st.get("gen") == self.mapping_gen

    def learn_candidate(self, now, min_delta=0.12):
        """Pick the index that is *rising* right now, never an already-bound one.

        The old code took the deepest moving key, so pressing S while W was held
        bound S to whichever index happened to be deepest.
        """
        best = None
        for i, st0 in self.keys.items():
            if st0.get("vk") is not None:
                continue                      # never steal a bound key
            d = self._delta(i, now)
            if d < min_delta:
                continue
            if best is None or d > best[0]:
                best = (d, i)
        if best is not None:
            return best[1]
        # the report can lag the OS edge by a few ms: accept a single fresh index
        fresh = [i for i, st0 in self.keys.items()
                 if st0.get("vk") is None and st0.get("mm", 0.0) > 0.25
                 and now - st0.get("t_last", -9.0) < 0.15]
        return fresh[0] if len(fresh) == 1 else None

    def bind_index(self, idx, vk, now):
        st = self.keys.get(idx)
        if st is None or st.get("vk") is not None:
            return False
        st["vk"] = vk
        self.mapping[idx] = vk
        self.pending_learn = None
        self.bump_mapping()
        self._save_mapping()
        # the press that triggered this binding is still down: keep it valid
        self.refresh_key_state(vk, True, now)
        return True

    def key_known(self, vk):
        """True when the current table still maps this vk to some index."""
        return any(st.get("vk") == vk for st in self.keys.values())

    def refresh_key_state(self, vk, down, ts):
        """Re-stamp one key after a mapping change it caused itself.

        Only the key that triggered the change is refreshed: re-validating every
        key would resurrect state that belongs to the old mapping, which is
        exactly what the generation stamp exists to prevent.
        """
        if vk in self.key_state or down:
            self.key_state[vk] = {"gen": self.mapping_gen, "ts": ts}
            if down:
                self.vk_held[vk] = True
            else:
                self.vk_held.pop(vk, None)

    def mark_key(self, vk, down, ts):
        self.key_state[vk] = {"gen": self.mapping_gen, "ts": ts}
        if down:
            self.vk_held[vk] = True
        else:
            self.vk_held.pop(vk, None)

    def on_depth(self, t, idx, mm):
        with self.lock:
            vk = self.mapping.get(idx)
            if vk is None:
                vk = self.keys.get(idx, {}).get("vk")
            if mm <= 0.0 and vk is not None and self.os_key_down(vk):
                # firmware resets the previous key to 0 when another key becomes
                # active -- keep the value while the key is physically down
                self.suppressed += 1
                return
            self.n_depth += 1
            self.samples.append((t, idx, mm))
            if len(self.samples) > 4000:
                del self.samples[:1000]
            self._abs += 1
            if self.recording:
                if self.rec_t0 is None:
                    self.rec_t0 = t
                self.rec.append((t, idx, mm))
            st = self.keys.get(idx)
            if st is None:
                st = self.keys[idx] = {"vk": self.mapping.get(idx), "mm": 0.0,
                                       "count": 0, "speeds": []}
            st["mm"] = mm
            st["t_last"] = t
            if (self.pending_learn is not None and st.get("vk") is None
                    and mm > 0.25 and t <= self.pending_learn[1]):
                self.bind_index(idx, self.pending_learn[0], t)
            ps = self.press_state.get(idx)
            if mm > 0.15:
                if ps is None:
                    ps = self.press_state[idx] = {
                        "start": t, "peak": mm, "last_peak": t, "samples": [],
                        "down": None, "up": None, "vk": st["vk"]}
                ps["samples"].append((t, mm))
                if mm > ps["peak"]:
                    ps["peak"] = mm
                if mm >= ps["peak"] - 0.02:
                    ps["last_peak"] = t
            elif ps is not None:
                ps["samples"].append((t, mm))
                press = dict(ps)
                press["end"] = t
                press["rise_ms"] = (press["last_peak"] - press["start"]) * 1000.0
                press["fall_ms"] = (press["end"] - press["last_peak"]) * 1000.0
                dur = press["rise_ms"]
                press["speed"] = ((press["peak"] - ps["samples"][0][1]) / dur * 1000.0
                                  if dur > 1 else 0.0)
                press["rt"] = (press["peak"] - press["up"]) if press["up"] is not None else None
                press["key"] = key_name(st["vk"]) if st["vk"] else "#%d" % idx
                self.press_state[idx] = None
                st["count"] += 1
                if press["speed"] > 0:
                    st["speeds"] = (st["speeds"] + [press["speed"]])[-40:]
                self.presses += 1
                self.last_press = press
                self.recent.appendleft(press)

    def run_demo(self):
        """Synthetic presses, only used with --demo."""
        plan = ((31, "A", 1.40, 0.0), (32, "S", 1.90, 0.45),
                (33, "D", 1.10, 0.80), (61, "Space", 2.60, 1.30))
        t0 = time.perf_counter()
        while True:
            now = time.perf_counter()
            for idx, name, period, phase in plan:
                el = ((now - t0) + phase) % period
                hold = period * 0.55
                if el < 0.16:
                    mm = FULL * (el / 0.16)
                elif el < hold:
                    mm = FULL * (0.72 + 0.28 * abs(__import__("math").sin(el * 6.0)))
                elif el < hold + 0.14:
                    mm = FULL * (1 - (el - hold) / 0.14)
                else:
                    mm = 0.0
                with self.lock:
                    st = self.keys.get(idx)
                    if st is None:
                        st = self.keys[idx] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
                    st["vk"] = 0x41 if name == "A" else (0x53 if name == "S"
                                                         else (0x44 if name == "D" else 0x20))
                self.on_depth(now, idx, round(max(0.0, min(FULL, mm)), 2))
            time.sleep(0.01)

    def start_recording(self):
        with self.lock:
            self.rec = []
            self.rec_t0 = None
            self.recording = True
            self.notice = "\u5f55\u5236\u4e2d"
        return True

    def stop_recording(self, save=True):
        with self.lock:
            self.recording = False
        if save:
            return self.save_recording()
        return None

    def save_recording(self):
        """Write the current recording to Documents\\depth-recordings and report the path."""
        with self.lock:
            n = len(self.rec)
        if not n:
            with self.lock:
                self.notice = "\u6ca1\u6709\u5f55\u5236\u6570\u636e"
            return None
        base = recordings_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        version_dir = os.path.join(base, stamp)
        long_path = os.path.join(version_dir, "long.csv")
        wide_path = os.path.join(version_dir, "wide.csv")
        try:
            if not os.path.isdir(version_dir):
                os.makedirs(version_dir)
            # both files are written inside one fresh directory; the manifest
            # below is the only thing that makes the pair "committed"
            for path, chunks in ((long_path, self.long_lines()),
                                 (wide_path, self.wide_lines())):
                with io.open(path, "w", encoding="utf-8", newline="") as f:
                    for chunk in chunks:
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            manifest = {
                "stamp": stamp,
                "samples": n,
                "long": os.path.relpath(long_path, base).replace("\\", "/"),
                "wide": os.path.relpath(wide_path, base).replace("\\", "/"),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            tmp = os.path.join(base, ".latest.tmp")
            with io.open(tmp, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(base, "latest.json"))
        except Exception as exc:
            with self.lock:
                self.notice = "\u4fdd\u5b58\u5931\u8d25\uff1a%s" % exc
            return None
        with self.lock:
            self.notice = "\u5df2\u4fdd\u5b58 %d \u70b9\uff1a%s" % (n, stamp)
        return wide_path

    def run_hold(self):
        """Keep held keys alive in the recording.

        The keyboard streams only the active key and zeroes the previous one, so
        a key you keep holding stops updating.  While recording we re-emit the
        current depth of every held key, which makes the CSV (and the video)
        show several keys pressed at once, exactly like the live view.
        """
        while True:
            time.sleep(0.05)
            with self.lock:
                if not self.recording:
                    continue
                items = []
                for idx, st in self.keys.items():
                    vk = st.get("vk")
                    if vk and self.os_key_down(vk) and st["mm"] > 0.05:
                        items.append((idx, st["mm"]))
            for idx, mm in items:
                try:
                    self.on_depth(time.perf_counter(), idx, mm)
                    self.hold_n += 1
                except Exception:
                    pass

    def request_quit(self, srv, defer=False):
        """Save first; if the recording cannot be written, stay alive."""
        try:
            if self.recording:
                path = self.stop_recording(save=True)
                with self.lock:
                    pending = len(self.rec)
                if pending and not path:
                    self.notice = "\u4fdd\u5b58\u5931\u8d25\uff0c\u672a\u9000\u51fa"
                    return False
        except Exception:
            _log_exc("request_quit save")
            return False
        return self.shutdown_app(srv, defer)

    def shutdown_app(self, srv, defer=False):
        """Stop the HID reader, then the HTTP server -- same path for every exit."""
        self.stop_io.set()
        h = getattr(self, "io_handle", None)
        if h:
            try:
                kernel32.CancelIoEx(h, None)
            except Exception:
                pass
        if srv is None:
            return True
        if defer:
            threading.Timer(0.25, self._shutdown_srv, args=(srv,)).start()
        else:
            self._shutdown_srv(srv)
        return True

    def _shutdown_srv(self, srv):
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()                 # close the listening socket too
        except Exception:
            pass

    def run_idle_exit(self, seconds, srv):
        """Nobody has looked at the page for a while and nothing is recording:
        there is no reason to keep a HID reader and a process alive."""
        if seconds <= 0:
            return
        while True:
            time.sleep(5.0)
            with self.lock:
                busy = self.recording or self.auto
            if busy:
                self.last_client = time.time()
                continue
            if time.time() - self.last_client > seconds:
                self.shutdown_app(srv)          # same ordered path as /quit
                return

    def run_autosave(self, interval):
        """Auto mode: save a segment every `interval` seconds and keep going."""
        while True:
            time.sleep(max(15, interval))
            with self.lock:
                active = self.recording
                n = len(self.rec)
            if active and n > 20:
                self.save_recording()
                with self.lock:
                    self.rec = []
                    self.rec_t0 = None

    def rate_tick(self):
        while True:
            time.sleep(1.0)
            with self.lock:
                self.depth_rate = float(self.n_depth)
                self.event_rate = float(self.n_event)
                self.n_depth = 0
                self.n_event = 0

    # ---- key mapping + event depths ---------------------------------
    def run_keys(self):
        prev = {}
        down_at = {}
        hk_prev = {}
        while True:
            now = time.perf_counter()
            for vk in CAND_VKS:
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                was = prev.get(vk, False)
                if down == was:
                    continue
                prev[vk] = down
                self.apply_key_state(vk, down, now)
            # watchdog: a key marked held but no longer down is a missed edge.
            # Without this the page can keep a key highlighted forever.
            if self.pending_learn is not None and now > self.pending_learn[1]:
                self.pending_learn = None
            for vk in list(self.vk_held):
                if not (user32.GetAsyncKeyState(vk) & 0x8000):
                    prev[vk] = False
                    self.released_by_watchdog += 1
                    self.apply_key_state(vk, False, now)
            # global hotkeys: work even when the game has focus (no window switching)
            ctrl = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
            alt = bool(user32.GetAsyncKeyState(0x12) & 0x8000)
            for hk, action in ((0x52, "record"), (0x53, "save")):
                down = ctrl and alt and bool(user32.GetAsyncKeyState(hk) & 0x8000)
                if down and not hk_prev.get(hk):
                    self.last_hotkey = "%s %s" % (action, time.strftime("%H:%M:%S"))
                    try:
                        d = data_home()
                        if not os.path.isdir(d):
                            os.makedirs(d)
                        with io.open(os.path.join(d, "hotkeys.log"), "a",
                                     encoding="utf-8") as f:
                            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                  action))
                    except Exception:
                        pass
                    try:
                        if action == "record":
                            if self.recording:
                                self.stop_recording(save=True)
                            else:
                                self.start_recording()
                        else:
                            self.stop_recording(save=True)
                    except Exception:
                        pass
                hk_prev[hk] = down
            time.sleep(0.004)

    def apply_key_state(self, vk, down, now):
        """Apply one physical key transition. Shared by the edge detector and the
        watchdog so a missed edge cannot leave a key stuck as held."""
        try:
            with self.lock:
                self.mark_key(vk, down, now)
                if not down:
                    released = [i for i, st in self.keys.items()
                                if st.get("vk") == vk]
                    for i in released:
                        self.on_depth(now, i, 0.0)
                # hold the lock across the transition so a mapping reset from
                # another thread cannot slip in between
                bumped = self._key_transition(vk, down, now)
                if bumped and self.key_known(vk):
                    # this very press taught/bound the key: keep its state
                    self.refresh_key_state(vk, down, now)
        except Exception:
            now_s = time.time()
            if now_s - getattr(self, "_last_key_err", 0) > 1.0:
                self._last_key_err = now_s
                _log_exc("apply_key_state vk=%d" % vk)

    def _key_transition(self, vk, down, now):
        """Returns True when this call changed the index<->vk table."""
        bumped = False
        with self.lock:
            # a key we already know: attribute the event to it directly
            if down and self.pending_label:
                label = self.pending_label
                moving = [i for i, st0 in self.keys.items() if st0["mm"] > 0.3]
                moving.sort(key=lambda i: (-self._delta(i, now), -self.keys[i]["mm"]))
                target = label_to_vk(label)
                if moving and target is not None:
                    idx = moving[0]
                    self.keys[idx]["vk"] = target
                    self.mapping[idx] = target
                    self.bump_mapping()
                    bumped = True
                    for other, st0 in self.keys.items():
                        if other != idx and st0["vk"] == target:
                            st0["vk"] = None
                    saved = self._save_mapping()
                    self.bind_msg = ("已绑定 %s（已存盘）" if saved else "已绑定 %s（存盘失败）") % label
                else:
                    self.bind_msg = "没抓到按键，请再按一次"
                self.pending_label = None
                return bumped
            known = [i for i, st0 in self.keys.items() if st0["vk"] == vk]
            if known:
                idx = known[0]
            elif down:
                idx = self.learn_candidate(now)
                if idx is None:
                    # analog report has not arrived yet: remember the vk and bind
                    # it the moment its index starts rising (deferred learning)
                    self.pending_learn = (vk, now + 0.3)
                    return
            else:
                return                      # release of something we never mapped
            st = self.keys[idx]
            if not down:
                ps = self.press_state.get(idx)
                if ps is not None and ps["up"] is None:
                    ps["up"] = self._depth_near(idx, now)
                return
            if st["vk"] is None or getattr(self, "pending_bind", False):
                st["vk"] = vk
                self.pending_bind = False
                self.mapping[idx] = vk
                self.bump_mapping()
                bumped = True
                self._save_mapping()
            ps = self.press_state.get(idx)
            if ps is not None and ps["down"] is None:
                ps["down"] = self._depth_near(idx, now)
                ps["vk"] = vk
        return bumped

    def _depth_near(self, idx, t, window=0.03):
        best = None
        with self.lock:
            for ts, i, mm in reversed(self.samples[-2000:]):
                if i != idx:
                    continue
                d = abs(ts - t)
                if d <= window and (best is None or d < best[0]):
                    best = (d, mm)
                elif ts < t - window:
                    break
        return best[1] if best else None

    # ---- snapshots ---------------------------------------------------
    def snapshot(self, cursor):
        with self.lock:
            total = self._abs
            dropped = self._abs - len(self.samples)
            if cursor < dropped:
                cursor = dropped
            if cursor > self._abs:
                cursor = self._abs
            batch = self.samples[cursor - dropped:]
            out = {
                "s": [[round(t, 4), i, mm] for t, i, mm in batch],
                "n": total,
                "k": {str(i): (key_name(s["vk"]) if s["vk"] else None)
                      for i, s in self.keys.items()},
                "live": {str(i): round(s["mm"], 2) for i, s in self.keys.items()},
                "rate": [round(self.depth_rate, 1), round(self.event_rate, 1)],
                "conn": self.connected,
                "presses": self.presses,
                "held": [key_name(v) for v in list(self.vk_held)],
                "notice": self.notice,
                "hotkey": self.last_hotkey,
                "hold_n": self.hold_n,
                "suppressed": self.suppressed,
                "watchdog_releases": self.released_by_watchdog,
                "mapping_gen": self.mapping_gen,
                "auto": self.auto,
                "version": VERSION,
                "token": self.token,
                "binding": self.pending_label,
                "bind_msg": self.bind_msg,
                "recording": self.recording,
                "rec_n": len(self.rec),
                "rec_dur": (max(0.0, round(time.perf_counter() - self.rec_t0, 1))
                            if (self.recording and self.rec_t0) else 0.0),
                "st": self._stats_locked(),
                "rec": [[int(t - self.t0), p["key"], round(p["peak"], 2),
                         round(p["rise_ms"]), round(p["speed"]), p["rt"]]
                        for t, p in []][:0] or self._recent_locked(),
            }
        return out

    def _stats_locked(self):
        p = self.last_press
        if not p:
            return None
        return {"key": p["key"], "peak": round(p["peak"], 2),
                "rise": round(p["rise_ms"]), "fall": round(p["fall_ms"]),
                "speed": round(p["speed"]), "down": (None if p["down"] is None
                                                    else round(p["down"], 2)),
                "up": (None if p["up"] is None else round(p["up"], 2)),
                "rt": (None if p["rt"] is None else round(p["rt"], 2))}

    def _recent_locked(self):
        out = []
        for p in list(self.recent)[:6]:
            out.append([p["key"], round(p["peak"], 2), round(p["rise_ms"]),
                        round(p["speed"]), None if p["rt"] is None else round(p["rt"], 2)])
        return out

    # ---- exports -----------------------------------------------------
    def long_lines(self):
        """Yield the long-format CSV line by line (no giant string in memory)."""
        with self.lock:
            rows = list(self.rec)
            names = {i: (key_name(s["vk"]) if s["vk"] else "k%d" % i)
                     for i, s in self.keys.items()}
        yield "# tool=depth-live  device=ROG FALCHION ACE 75 HE (VID 0B05 PID 1C7D)\n"
        yield "# unit=0.01mm  full=3.50mm  started=%s\n" % self.started
        yield "t_s,wall,key,index,mm\n"
        if not rows:
            yield "# nothing recorded -- press 开始录制 first\n"
            return
        t0 = rows[0][0]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(0, len(rows), 500):
            chunk = []
            for t, idx, mm in rows[i:i + 500]:
                chunk.append("%.6f,%s,%s,%d,%.2f\n"
                             % (t - t0, stamp, names.get(idx, ""), idx, mm))
            yield "".join(chunk)

    def wide_lines(self, step=0.01):
        """Yield the fixed-rate matrix CSV line by line."""
        with self.lock:
            rows = list(self.rec)
            names = {i: (key_name(s["vk"]) if s["vk"] else "k%d" % i)
                     for i, s in self.keys.items()}
        yield "# tool=depth-live  wide matrix %.0f Hz  depth in mm\n" % (1.0 / step)
        if not rows:
            yield "# nothing recorded -- press 开始录制 first\nt_s\n"
            return
        t0 = rows[0][0]
        series = {}
        for t, idx, mm in rows:
            series.setdefault(idx, []).append((t - t0, mm))
        # keys touched outside the recorded window have no samples: drop them,
        # otherwise they contribute empty columns and break the duration calc
        series = {k: v for k, v in series.items() if v}
        cols = sorted(series)
        dur = max(v[-1][0] for v in series.values())
        yield "t_s,wall," + ",".join(names.get(i, "k%d" % i) for i in cols) + "\n"
        cur = dict((i, 0) for i in cols)
        n = int(dur / step) + 1
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        batch = []
        for k in range(n):
            ts = k * step
            cells = []
            for i in cols:
                arr = series[i]
                if not arr:
                    cells.append("0.00")
                    continue
                j = cur[i]
                while j + 1 < len(arr) and arr[j + 1][0] <= ts:
                    j += 1
                cur[i] = j
                if ts < arr[0][0]:
                    cells.append("0.00")
                elif j + 1 < len(arr) and arr[j + 1][0] - arr[j][0] <= 0.12:
                    a, b = arr[j], arr[j + 1]
                    f = (ts - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                    cells.append("%.2f" % (a[1] + (b[1] - a[1]) * f))
                else:
                    cells.append("%.2f" % (arr[j][1] if ts - arr[j][0] < 0.12 else 0.0))
            batch.append("%.3f,%s,%s\n" % (ts, stamp, ",".join(cells)))
            if len(batch) >= 500:
                yield "".join(batch)
                batch = []
        if batch:
            yield "".join(batch)


ENGINE = Engine()


def _log_exc(where):
    try:
        folder = data_home()
        if not os.path.isdir(folder):
            os.makedirs(folder)
        import traceback
        with io.open(os.path.join(folder, "error.log"), "a", encoding="utf-8") as f:
            f.write("\n=== %s %s ===\n%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), where, traceback.format_exc()))
    except Exception:
        pass


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    STATE_PATHS = ("/rec/start", "/rec/stop", "/rec/clear", "/auto", "/bind",
                   "/resetmap", "/reset", "/openfolder", "/quit")

    def _send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _host_ok(self):
        """Host must be loopback *and* name this very port."""
        raw = (self.headers.get("Host") or "").strip().lower()
        if not raw:
            return False
        try:
            parts = urlsplit("//" + raw)
            port = parts.port
        except ValueError:
            return False
        if parts.hostname not in ("127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"):
            return False
        return port == self.server.server_address[1]

    def _same_origin(self):
        """True only for a request that provably comes from this page."""
        port = self.server.server_address[1]
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return False                       # no Origin -> not a page request
        try:
            parts = urlsplit(origin)
            port_ok = parts.port == port
        except ValueError:
            return False
        return (parts.scheme == "http" and port_ok and
                parts.hostname in ("127.0.0.1", "localhost", "::1"))

    def _authorized(self, url):
        """Browser page: must be same-origin (a foreign Origin is always refused,
        even with a token). Local scripts: must present the process token."""
        if (self.headers.get("Origin") or "").strip():
            return self._same_origin()
        q = parse_qs(url.query)
        token = (q.get("t") or [""])[0] or (self.headers.get("X-Depth-Token") or "")
        return bool(token) and secrets.compare_digest(token, ENGINE.token)

    def _handle(self, method):
        if not self._host_ok():
            self._send_text(403, "bad host")
            return
        ENGINE.last_client = time.time()
        url = urlparse(self.path)
        if url.path in self.STATE_PATHS:
            if method != "POST":
                # a cross-site <img>/<a> can only issue GET; refuse it outright
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._authorized(url):
                self._send_text(403, "not authorized")
                return
        if url.path == "/":
            body = page_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/events":
            self.stream()
        elif url.path.startswith("/export/"):
            kind = url.path.rsplit("/", 1)[-1]
            q = parse_qs(url.query)
            try:
                step = float((q.get("rate") or ["100"])[0])
            except ValueError:
                step = 100.0
            step = 1.0 / max(10.0, min(500.0, step))
            parts = []
            size = 0
            capped = False
            try:
                gen = ENGINE.wide_lines(step) if kind == "wide" else ENGINE.long_lines()
                for chunk in gen:
                    parts.append(chunk)
                    size += len(chunk.encode("utf-8"))
                    if size > 32 * 1024 * 1024:
                        capped = True
                        break
            except Exception:
                _log_exc("export/%s" % kind)
                parts = ["# export failed, see %LOCALAPPDATA%\\depth-monitor\\error.log\n"]
                size = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="depth-%s-%s.csv"'
                             % (kind, time.strftime("%Y%m%d-%H%M%S")))
            if not capped:
                body = ("\ufeff" + "".join(parts)).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass
                return
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(("\ufeff" + "".join(parts)).encode("utf-8"))
                for chunk in gen:                 # keep streaming the rest
                    self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
        elif url.path == "/snapshot":
            snap = ENGINE.snapshot(0)
            snap.pop("s", None)
            body = json.dumps(snap, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path in ("/rec/start", "/rec/stop", "/rec/clear"):
            with ENGINE.lock:
                if url.path == "/rec/start":
                    ENGINE.rec = []
                    ENGINE.rec_t0 = None
                    ENGINE.recording = True
                elif url.path == "/rec/stop":
                    ENGINE.recording = False
                else:
                    ENGINE.rec = []
                    ENGINE.rec_t0 = None
            if url.path == "/rec/stop":
                ENGINE.save_recording()
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/auto":
            q = parse_qs(url.query)
            on = (q.get("on") or ["1"])[0] != "0"
            if on:
                ENGINE.auto = True
                ENGINE.start_recording()
                if not getattr(ENGINE, "_auto_started", False):
                    ENGINE._auto_started = True
                    t = threading.Thread(target=ENGINE.run_autosave, args=(120,))
                    t.daemon = True
                    t.start()
                with ENGINE.lock:
                    ENGINE.notice = "\u81ea\u52a8\u5f55\u5236\u4e2d\uff1a\u6bcf 2 \u5206\u949f\u81ea\u52a8\u5b58\u4e00\u6bb5"
            else:
                ENGINE.auto = False
                ENGINE.stop_recording(save=True)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/quit":
            # authorization already happened in _handle(): POST + origin/token
            ok = ENGINE.request_quit(srv_ref[0], defer=True)
            body = b"bye" if ok else "\u4fdd\u5b58\u5931\u8d25\uff0c\u672a\u9000\u51fa".encode("utf-8")
            self.send_response(200 if ok else 409)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/openfolder":
            try:
                folder = recordings_dir()
                if not os.path.isdir(folder):
                    os.makedirs(folder)
                os.startfile(folder)
            except Exception:
                pass
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/bind":
            q = parse_qs(url.query)
            label = (q.get("key") or [None])[0]
            with ENGINE.lock:
                ENGINE.pending_label = label
                ENGINE.bind_msg = ""
            body = b"ok"
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/resetmap":
            ENGINE.reset_mapping()
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/reset":
            with ENGINE.lock:
                ENGINE.samples = []
                ENGINE.rec = []
                ENGINE.rec_t0 = None
                ENGINE.recent.clear()
                ENGINE.presses = 0
                ENGINE.last_press = None
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            try:
                self.rfile.read(length)
            except Exception:
                pass
        self._handle("POST")

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        cursor = 0
        try:
            while True:
                snap = ENGINE.snapshot(cursor)
                cursor = snap["n"]
                ENGINE.last_client = time.time()   # the page is still watching
                self.wfile.write(("data: " + json.dumps(snap, separators=(",", ":")) +
                                  "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.018)
        except Exception:
            return


LAYOUT = [
    ("Esc", 0, 0.0, 1), ("F1", 0, 1.4, 1), ("F2", 0, 2.4, 1), ("F3", 0, 3.4, 1),
    ("F4", 0, 4.4, 1), ("F5", 0, 5.5, 1), ("F6", 0, 6.5, 1), ("F7", 0, 7.5, 1),
    ("F8", 0, 8.5, 1), ("F9", 0, 9.6, 1), ("F10", 0, 10.6, 1), ("F11", 0, 11.6, 1),
    ("F12", 0, 12.6, 1), ("Del", 0, 15.2, 1),
    ("`", 1, 0.0, 1), ("1", 1, 1, 1), ("2", 1, 2, 1), ("3", 1, 3, 1), ("4", 1, 4, 1),
    ("5", 1, 5, 1), ("6", 1, 6, 1), ("7", 1, 7, 1), ("8", 1, 8, 1), ("9", 1, 9, 1),
    ("0", 1, 10, 1), ("-", 1, 11, 1), ("=", 1, 12, 1), ("Bksp", 1, 13, 2),
    ("Home", 1, 15.2, 1),
    ("Tab", 2, 0.0, 1.5), ("Q", 2, 1.5, 1), ("W", 2, 2.5, 1), ("E", 2, 3.5, 1),
    ("R", 2, 4.5, 1), ("T", 2, 5.5, 1), ("Y", 2, 6.5, 1), ("U", 2, 7.5, 1),
    ("I", 2, 8.5, 1), ("O", 2, 9.5, 1), ("P", 2, 10.5, 1), ("[", 2, 11.5, 1),
    ("]", 2, 12.5, 1), ("\\\\", 2, 13.5, 1.5), ("PgUp", 2, 15.2, 1),
    ("Caps", 3, 0.0, 1.75), ("A", 3, 1.75, 1), ("S", 3, 2.75, 1), ("D", 3, 3.75, 1),
    ("F", 3, 4.75, 1), ("G", 3, 5.75, 1), ("H", 3, 6.75, 1), ("J", 3, 7.75, 1),
    ("K", 3, 8.75, 1), ("L", 3, 9.75, 1), (";", 3, 10.75, 1), ("'", 3, 11.75, 1),
    ("Enter", 3, 12.75, 2.25), ("PgDn", 3, 15.2, 1),
    ("LShift", 4, 0.0, 2.25), ("Z", 4, 2.25, 1), ("X", 4, 3.25, 1), ("C", 4, 4.25, 1),
    ("V", 4, 5.25, 1), ("B", 4, 6.25, 1), ("N", 4, 7.25, 1), ("M", 4, 8.25, 1),
    (",", 4, 9.25, 1), (".", 4, 10.25, 1), ("/", 4, 11.25, 1), ("RShift", 4, 12.25, 2.75),
    ("End", 4, 15.2, 1),
    ("LCtrl", 5, 0.0, 1.25), ("LWin", 5, 1.25, 1.25), ("LAlt", 5, 2.5, 1.25),
    ("Space", 5, 3.75, 6.5), ("RAlt", 5, 10.25, 1.25), ("Fn", 5, 11.5, 1.25),
    ("RCtrl", 5, 12.75, 1.25), ("\u2190", 5, 14.2, 0.9), ("\u2193", 5, 15.2, 0.9),
    ("\u2192", 5, 16.2, 0.9),
]


def page_html():
    data = [{"k": k, "r": r, "x": x, "w": w} for k, r, x, w in LAYOUT]
    return PAGE.replace("__LAYOUT__", json.dumps(data, ensure_ascii=False))


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>磁轴深度监测</title>
<style>
  :root { --u: 46px; --bg:#0a0d12; --panel:#111722; --edge:#1f2836;
          --fg:#e6edf3; --dim:#7d8797; --acc:#38bdf8; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; background:
      radial-gradient(1200px 600px at 20% -10%, #16202e 0%, transparent 60%),
      radial-gradient(900px 500px at 100% 0%, #131a26 0%, transparent 55%), var(--bg);
    color: var(--fg); font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;
    overflow:hidden; }
  header { display:flex; align-items:center; gap:16px; padding:14px 22px 10px; }
  h1 { font-size:16px; font-weight:600; letter-spacing:1px; margin:0; }
  h1 small { color:var(--dim); font-weight:400; font-size:11px; margin-left:8px; }
  .pill { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--dim);
          background:#0f1520; border:1px solid var(--edge); border-radius:999px;
          padding:4px 10px; }
  .dot { width:7px; height:7px; border-radius:50%; background:#3ddc84; }
  .dot.off { background:#64748b; }
  .spacer { flex:1; }
  button { background:#16202e; color:#cbd5e1; border:1px solid var(--edge);
           border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer;
           font-family:inherit; }
  button:hover { background:#1d2a3c; border-color:#2b3a4f; }
  button.rec { border-color:#7f1d1d; background:#2a1214; color:#fca5a5; }
  button.rec.on { background:#7f1d1d; color:#fff; border-color:#ef4444; }
  button.tgl { border-color:#1f4f3a; background:#0f2419; color:#5eead4; }
  button.tgl.off { border-color:#3b2b16; background:#221a0d; color:#a98a5c; }
  #kbOff { position:absolute; inset:0; display:none; align-items:center;
           justify-content:center; color:#3f4b5c; font-size:13px; letter-spacing:1px; }
  #kbc.off #kb { display:none; }
  #kbc.off #kbOff { display:flex; }
  #wrap { display:flex; gap:18px; padding:6px 22px 18px; height:calc(100% - 58px); }
  #left { flex:1; display:flex; flex-direction:column; gap:14px; min-width:0; min-height:0; }
  .card { background:linear-gradient(180deg,#121a26,#0f151f); border:1px solid var(--edge);
          border-radius:14px; }
  #kbc { padding:16px; display:flex; align-items:center; justify-content:center;
         flex:1; min-height:0; overflow:hidden; }
  #kb { position:relative; width:calc(17.1 * var(--u)); height:calc(6 * var(--u) + 40px); }
  .k { position:absolute; border-radius:calc(var(--u) * 0.14); overflow:hidden;
       background:linear-gradient(#1a2230,#141a25); border:1px solid #2a3444;
       box-shadow:inset 0 1px 0 #303c50, 0 2px 8px rgba(0,0,0,.5);
       transition:box-shadow .05s linear; }
  .k .fill { position:absolute; left:0; right:0; bottom:0; height:0%;
             background:#38bdf8; opacity:.88; }
  .k .lbl { position:absolute; inset:0; display:flex; align-items:center;
            justify-content:center; font-size:calc(var(--u) * 0.27); color:#8d99ac;
            text-shadow:0 1px 2px #000; }
  .k.hot .lbl { color:#fff; }
  .k.held { border-color:#3b82f6; box-shadow: inset 0 2px 0 #3b82f6, 0 0 10px #1d4ed866; }
  .k.held .lbl { color:#cfe4ff; }
  #right { width:352px; display:flex; flex-direction:column; gap:12px; }
  .card.pad { padding:14px 16px; }
  .big { display:flex; align-items:baseline; justify-content:space-between; }
  /* fixed height: the list grows while several keys are held, and a growing
     card would shrink the keyboard card and move the keys up and down */
  #livecard { flex:0 0 196px; height:196px; overflow:hidden; }
  #livecard #bars { min-height:0; }
  #curkey { font-size:30px; font-weight:700; letter-spacing:1px; }
  #curmm { font-size:34px; font-weight:700; color:var(--acc); font-variant-numeric:tabular-nums; }
  #curmm.idle { color:#48566b; }
  .sub { font-size:11px; color:var(--dim); margin-top:2px; }
  #bars { margin-top:8px; display:flex; flex-direction:column; gap:4px; min-height:0; }
  .bar { display:grid; grid-template-columns:44px 1fr 62px; align-items:center; gap:8px;
         font-size:11px; }
  .bar b { color:#cbd5e1; font-weight:600; }
  .bar .track { height:7px; border-radius:4px; background:#0b1017; overflow:hidden; }
  .bar .track i { display:block; height:100%; width:0%; background:#3fb6ff; }
  .bar span { text-align:right; color:#9aa6b6; font-variant-numeric:tabular-nums; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:8px 14px; font-size:12px; }
  .grid2 div { display:flex; justify-content:space-between; color:var(--dim); }
  .grid2 b { color:var(--fg); font-weight:600; font-variant-numeric:tabular-nums; }
  table { width:100%; border-collapse:collapse; font-size:11px; }
  th { text-align:left; color:#5f6b7c; font-weight:500; padding:3px 4px; }
  td { padding:3px 4px; color:#b6c2d2; font-variant-numeric:tabular-nums; }
  tbody tr:nth-child(odd) { background:#0e141d; }
  #bigtip { position:fixed; left:50%%; top:50%%; transform:translate(-50%%,-50%%);
            background:#2a1113f2; border:1px solid #7f1d1d; color:#fecaca;
            padding:22px 34px; border-radius:16px; font-size:16px; line-height:1.6;
            display:none; z-index:20; box-shadow:0 20px 60px #000a; text-align:center;
            max-width:520px; }
  #bigtip b { color:#fff; font-size:18px; }
  button.rec.pulse { animation: recpulse .9s ease-in-out 3; }
  @keyframes recpulse {
    0%%,100%% { box-shadow: 0 0 0 0 rgba(239,68,68,0); }
    50%% { box-shadow: 0 0 0 6px rgba(239,68,68,.35); }
  }
  #toast { position:fixed; left:50%; top:14px; transform:translateX(-50%);
           background:#1b2432ee; border:1px solid #33415a; color:#e6edf3;
           padding:8px 16px; border-radius:999px; font-size:13px; display:none;
           z-index:9; }
  .k.binding { border-color:#fbbf24 !important; box-shadow:0 0 0 2px #fbbf2488, inset 0 1px 0 #303c50; }
  #staleTip { display:none; position:fixed; left:50%; top:14px; transform:translateX(-50%);
              background:#2a1113f2; border:1px solid #7f1d1d; color:#fecaca;
              padding:10px 16px; border-radius:10px; font-size:13px; z-index:30; }
  #hint { position:fixed; right:20px; bottom:12px; font-size:11px; color:#3c4a5d; }
</style></head>
<body>
<header>
  <h1>ROG FALCHION ACE 75 HE<small data-v="1">磁轴深度实时监测</small></h1>
  <div class="pill"><span class="dot" id="dot"></span><span id="conn">连接中</span></div>
  <div class="pill">深度 <b id="dr" style="color:#cbd5e1">0</b>/s</div>
  <div class="pill">事件 <b id="er" style="color:#cbd5e1">0</b>/s</div>
  <div class="pill">按压 <b id="pc" style="color:#cbd5e1">0</b> 次</div>
  <div class="spacer"></div>
  <button id="kbToggle" class="tgl" onclick="toggleKb()">键盘渲染 ●</button>
  <button id="autoBtn" onclick="toggleAuto()">自动录制</button>
  <button id="recBtn" class="rec" onclick="toggleRec()">● 开始录制</button>
  <div class="pill" id="recPill" style="display:none">已录 <b id="recInfo" style="color:#fca5a5">0</b></div>
  <button onclick="downloadIfRecorded('/export/long')">导出长表</button>
  <button onclick="showTip('点击键盘上的某个键，再按一下对应的物理键，即可建立映射')">绑定说明</button>
  <button onclick="api('/resetmap').then(()=>location.reload())">重新识别</button>
  <select id="rate" title="宽表采样率" style="background:#16202e;color:#cbd5e1;border:1px solid #1f2836;border-radius:8px;padding:6px 8px;font-size:12px">
    <option value="100">100 Hz</option><option value="50">50 Hz</option><option value="25">25 Hz</option>
  </select>
  <button onclick="downloadIfRecorded('/export/wide?rate=' + document.getElementById('rate').value)">导出宽表</button>
  <button id="quitBtn" onclick="quitApp()">退出程序</button>
  <button onclick="api('/openfolder')">打开记录文件夹</button>
  <button onclick="api('/reset').then(()=>location.reload())">清空重录</button>
</header>

<div id="wrap">
  <div id="left">
    <div class="card" id="kbc"><div id="kb"></div>
      <div id="kbOff" onclick="toggleKb()" style="cursor:pointer">键盘渲染已关闭·点这里或右上角“键盘渲染”恢复</div></div>
    <div class="card pad" id="livecard">
      <div class="sub" style="margin:0 0 6px">同时按下</div>
      <div id="bars"></div>
    </div>
  </div>
  <div id="right">
    <div class="card pad">
      <div class="big"><div id="curkey">—</div><div id="curmm" class="idle">0.00</div></div>
      <div class="sub">当前最深按键 / 深度（mm）</div>
    </div>
    <div class="card pad">
      <div class="sub" style="margin:0 0 8px">本次按压</div>
      <div class="grid2">
        <div>触发点 <b id="s_down">—</b></div><div>复位点 <b id="s_up">—</b></div>
        <div>RT 距离 <b id="s_rt">—</b></div><div>峰值 <b id="s_peak">—</b></div>
        <div>上升 <b id="s_rise">—</b></div><div>下降 <b id="s_fall">—</b></div>
      </div>
    </div>
    <div class="card pad" style="flex:1; overflow:hidden">
      <div class="sub" style="margin:0 0 6px">最近按压</div>
      <table><thead><tr><th>键</th><th>峰值</th><th>上升</th><th>速度</th><th>RT</th></tr></thead>
        <tbody id="rec"></tbody></table>
    </div>
  </div>
</div>
<div id="bigtip"></div>
<div id="toast"></div>
<div id="staleTip">数据流已断开（服务端没在推快照），按住状态已重置 —— 刷新页面或检查程序是否还在运行</div>
<div id="hint">Ctrl+Alt+R 开始/停止录制 · Ctrl+Alt+S 保存到「文档\depth-recordings」 · 数据约 100 帧/秒</div>

<script>
const LAYOUT = __LAYOUT__;
const kb = document.getElementById('kb');
const els = {};
for (const k of LAYOUT) {
  const d = document.createElement('div');
  d.className = 'k';
  d.style.left = 'calc(' + k.x + ' * var(--u))';
  d.style.top = 'calc(' + k.r + ' * (var(--u) + 6px))';
  d.style.width = 'calc(' + k.w + ' * var(--u) - 5px)';
  d.style.height = 'calc(var(--u) - 5px)';
  d.innerHTML = '<div class="fill"></div><div class="lbl">' + k.k + '</div>';
  d.style.cursor = 'pointer';
  d.title = '点击后按一下对应的物理键';
  d.addEventListener('click', () => {
    api('/bind?key=' + encodeURIComponent(k.k));
    showTip('请按一下物理键：' + k.k);
  });
  kb.appendChild(d);
  els[k.k] = { box: d, fill: d.firstChild };
}
const FULL = 3.50;
const ALIAS = { Alt: 'LAlt', Ctrl: 'LCtrl', Shift: 'LShift', Win: 'LWin', Meta: 'LWin',
                LMeta: 'LWin', RMeta: 'RWin' };
function norm(n) { return ALIAS[n] || n; }
let live = {}, idxName = {}, nameIdx = {}, shown = {}, stats = null, recent = [];
let rate = [0, 0], presses = 0, connected = false;

function color(r) {
  const stops = [[0,56,189,248],[.35,52,211,153],[.7,250,204,21],[1,248,113,113]];
  for (let i = 1; i < stops.length; i++) {
    if (r <= stops[i][0]) {
      const a = stops[i-1], b = stops[i], f = (r - a[0]) / (b[0] - a[0]);
      return 'rgb(' + Math.round(a[1]+(b[1]-a[1])*f) + ',' + Math.round(a[2]+(b[2]-a[2])*f) +
             ',' + Math.round(a[3]+(b[3]-a[3])*f) + ')';
    }
  }
  return 'rgb(248,113,113)';
}

let rec = { on: false, n: 0, dur: 0 };
let autoMode = false;
let binding = null, bindMsg = '', toastTimer = null;
let kbOn = true;
try { kbOn = localStorage.getItem('kbOn') !== '0'; } catch (e) {}
const QSP = new URLSearchParams(location.search);
if (QSP.get('kb') === '0') kbOn = false;
if (QSP.get('kb') === '1') kbOn = true;
const lastRatio = {};
let drawing = false;
function toggleKb() {
  kbOn = !kbOn;
  try { localStorage.setItem('kbOn', kbOn ? '1' : '0'); } catch (e) {}
  try { applyKbState(); } catch (e) {}
  if (kbOn) {
    lastRatio && Object.keys(lastRatio).forEach(k => delete lastRatio[k]);
    startDraw();
  }
}
function startDraw() {
  if (drawing) return;
  drawing = true;
  requestAnimationFrame(draw);
}
function applyKbState() {
  const btn = document.getElementById('kbToggle');
  btn.textContent = '\u952e\u76d8\u6e32\u67d3 ' + (kbOn ? '\u25cf' : '\u25cb');
  btn.classList.toggle('off', !kbOn);
  document.getElementById('kbc').classList.toggle('off', !kbOn);
}
function showTip(text, ms) {
  const t = document.getElementById('toast');
  t.textContent = text; t.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.display = 'none'; }, ms || 2600);
}
const EMPTY_SET = new Set();
let lastHeld = {};
let held = new Set();
let lastMsg = Date.now();
let staleHeld = false;
function api(path) {
  return fetch(path, { method: 'POST',
                       headers: { 'X-Depth-Token': window.__token || '' } });
}

function quitApp() {
  if (!confirm('退出磁轴深度监测？录制中的数据会先自动存盘。')) return;
  api('/quit').then(r => {
    if (r.status === 409) { alert('保存失败，程序没有退出'); return; }
    document.body.innerHTML = '<div style="color:#8b93a7;font:16px system-ui;padding:40px">' +
      '已退出，可以关闭这个标签页了。</div>';
  });
}
function toggleAuto() {
  api('/auto?on=' + (autoMode ? '0' : '1')).then(() => showTip(autoMode ? '已关闭自动录制' : '自动录制已开启：每 2 分钟存一段'));
}
function toggleRec() { api(rec.on ? '/rec/stop' : '/rec/start'); }
function bigTip(html, ms) {
  const el = document.getElementById('bigtip');
  el.innerHTML = html;
  el.style.display = 'block';
  clearTimeout(window.__bigTipTimer);
  window.__bigTipTimer = setTimeout(() => { el.style.display = 'none'; }, ms || 4000);
}
function downloadIfRecorded(url) {
  if (!rec || !rec.n) {
    bigTip('<b>\u8fd8\u6ca1\u6709\u5f55\u5236\u6570\u636e</b><br>' +
           '1. \u70b9\u4e0a\u65b9\u7684\u300c\u25cf \u5f00\u59cb\u5f55\u5236\u300d<br>' +
           '2. \u770b\u5230\u300c\u5df2\u5f55 xx \u70b9\u300d\u5728\u6da8\uff0c\u5c31\u8bf4\u660e\u5728\u5f55\u4e86<br>' +
           '3. \u6309\u51e0\u4e0b\u952e\u76d8\uff0c\u518d\u70b9\u300c\u5bfc\u51fa\u300d');
    const btn = document.getElementById('recBtn');
    if (btn && !rec.on) { btn.classList.add('pulse');
      setTimeout(() => btn.classList.remove('pulse'), 3000); }
    return;
  }
  download(url);
}
function download(url) {
  const f = document.createElement('iframe');
  f.style.display = 'none';
  f.src = url;
  document.body.appendChild(f);
  setTimeout(() => { try { f.parentNode.removeChild(f); } catch (e) {} }, 180000);
  showTip('正在生成文件…');
}
function bindNow() {
  api('/bind').then(() => {
    document.getElementById('conn').textContent = '请按下想绑定的物理按键…';
  });
}
const es = new EventSource('/events');
es.onmessage = ev => {
  lastMsg = Date.now();
  const m = JSON.parse(ev.data);
  if (m.s) for (const p of m.s) live[p[1]] = p[2];
  if (m.k) {
    idxName = m.k; nameIdx = {};
    for (const i in m.k) if (m.k[i]) nameIdx[norm(m.k[i])] = i;
  }
  rate = m.rate; presses = m.presses; connected = m.conn;
  if (m.st) stats = m.st;
  if (m.rec) recent = m.rec;
  if (m.held) held = new Set(m.held.map(norm));
  if (m.binding !== undefined) {
    if (m.binding && m.binding !== binding) showTip('\u8bf7\u6309\u4e00\u4e0b\u7269\u7406\u952e：' + m.binding);
    binding = m.binding;
  }
  if (m.bind_msg && m.bind_msg !== bindMsg) showTip(m.bind_msg);
  bindMsg = m.bind_msg || '';
  if (m.notice && m.notice !== window.__lastNotice) {
    window.__lastNotice = m.notice;
    bigTip(m.notice, 3500);
  }
  if (m.token) window.__token = m.token;
  if (m.version) {
    const small = document.querySelector('h1 small');
    if (small) small.textContent = '\u78c1\u8f74\u6df1\u5ea6\u5b9e\u65f6\u76d1\u6d4b v' + m.version;
  }
  autoMode = !!m.auto;
  const ab = document.getElementById('autoBtn');
  if (ab) ab.classList.toggle('on', autoMode);
  rec.on = !!m.recording; rec.n = m.rec_n || 0; rec.dur = m.rec_dur || 0;
};

const bars = document.getElementById('bars');
function renderBars() {
  const active = Object.keys(live).map(i => [i, live[i]]).filter(x => x[1] > 0.03)
    .sort((a, b) => b[1] - a[1]).slice(0, 4);
  const seen = new Set(active.map(a => idxName[a[0]]));
  for (const name of held) {
    if (!seen.has(name) && active.length < 5 && els[name]) active.push([null, 0, name]);
  }
  bars.innerHTML = active.map(([i, mm, nmName]) => {
    const nm = nmName || norm(idxName[i]) || ('#' + i);
    if (i === null) {
      return '<div class="bar"><b>' + nm + '</b><div class="track"><i style="width:0%"></i></div>' +
             '<span>\u6309\u4e0b</span></div>';
    }
    const pct = Math.min(100, mm / FULL * 100);
    const c = color(mm / FULL);
    return '<div class="bar"><b>' + nm + '</b><div class="track"><i style="width:' +
      pct.toFixed(1) + '%;background:' + c + '"></i></div><span>' + mm.toFixed(2) +
      ' mm</span></div>';
  }).join('') || '<div class="sub">按下按键开始测量</div>';
}

function fmt(v, unit) { return v === null || v === undefined ? '—' : (v + unit); }

function renderPanel() {
  let best = 0, bestName = '—';
  for (const i in live) if (live[i] > best) { best = live[i]; bestName = idxName[i] || ('#' + i); }
  document.getElementById('curkey').textContent = bestName;
  const cm = document.getElementById('curmm');
  cm.textContent = best.toFixed(2);
  cm.className = best > 0.03 ? '' : 'idle';
  document.getElementById('dot').className = connected ? 'dot' : 'dot off';
  document.getElementById('conn').textContent = connected ? '已连接' : '等待设备';
  const rb = document.getElementById('recBtn');
  const pill = document.getElementById('recPill');
  const info = document.getElementById('recInfo');
  if (rec.on) {
    rb.textContent = '■ 停止录制';
    rb.classList.add('on');
    pill.style.display = '';
    info.textContent = rec.dur.toFixed(1) + ' s · ' + rec.n + ' 点';
  } else {
    rb.textContent = '● 开始录制';
    rb.classList.remove('on');
    if (rec.n > 0) {
      pill.style.display = '';
      info.textContent = rec.n + ' 点（已停止）';
    } else {
      pill.style.display = 'none';
    }
  }
  document.getElementById('dr').textContent = rate[0];
  document.getElementById('er').textContent = rate[1];
  document.getElementById('pc').textContent = presses;
  const s = stats || {};
  document.getElementById('s_down').textContent = fmt(s.down, ' mm');
  document.getElementById('s_up').textContent = fmt(s.up, ' mm');
  document.getElementById('s_rt').textContent = fmt(s.rt, ' mm');
  document.getElementById('s_peak').textContent = fmt(s.peak, ' mm');
  document.getElementById('s_rise').textContent = fmt(s.rise, ' ms');
  document.getElementById('s_fall').textContent = fmt(s.fall, ' ms');
  document.getElementById('rec').innerHTML = recent.map(r =>
    '<tr><td>' + r[0] + '</td><td>' + r[1].toFixed(2) + '</td><td>' + r[2] + ' ms</td><td>' +
    r[3] + ' mm/s</td><td>' + (r[4] === null ? '—' : r[4].toFixed(2)) + '</td></tr>').join('');
  renderBars();
}

function draw() {
  if (!kbOn) { drawing = false; return; } // idle: no work, but resumable
  const stale = Date.now() - lastMsg > 2000;
  if (stale !== staleHeld) {
    staleHeld = stale;
    const tipEl = document.getElementById('staleTip');
    if (tipEl) tipEl.style.display = stale ? 'block' : 'none';
  }
  const heldNow = stale ? EMPTY_SET : held;

  for (const name in els) {
    const idx = nameIdx[name];
    const mm = (idx !== undefined) ? (live[idx] || 0) : 0;
    const r = Math.max(0, Math.min(1, mm / FULL));
    const prev = lastRatio[name];
    const isHeld = heldNow.has(name);
    const wasHeld = lastHeld[name] === true;
    if (prev !== undefined && Math.abs(prev - r) < 0.004 &&
        !isHeld && !wasHeld && binding !== name) {
      continue;                          // nothing visibly changed
    }
    lastRatio[name] = r;
    lastHeld[name] = isHeld;             // a released key must repaint once
    const el = els[name];
    el.fill.style.height = (r * 100) + '%';
    el.fill.style.background = color(r);
    el.box.classList.toggle('hot', r > 0.05);
    el.box.classList.toggle('held', isHeld && r < 0.06);
    el.box.classList.toggle('binding', binding === name);
    el.box.style.boxShadow = r > 0.02
      ? '0 0 ' + (5 + r * 24).toFixed(0) + 'px ' + color(r) + ', inset 0 1px 0 #303c50'
      : 'inset 0 1px 0 #303c50, 0 2px 8px rgba(0,0,0,.5)';
  }
  requestAnimationFrame(draw);
}
startDraw();
setInterval(renderPanel, 90);
addEventListener('keydown', e => {
  if (e.code === 'KeyK' && !e.ctrlKey && !e.altKey && !e.metaKey) toggleKb();
});

function fitUnits() {
  const box = document.getElementById('kbc');
  const u = Math.max(24, Math.min(66, Math.floor((box.clientWidth - 40) / 17.4)));
  document.documentElement.style.setProperty('--u', u + 'px');
}
addEventListener('resize', fitUnits);
applyKbState();
fitUnits();
</script>
</body></html>
"""



def single_instance():
    """Only one monitor per user: two instances would fight over mapping.json."""
    try:
        kernel32.CreateMutexW.restype = wt.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, ctypes.c_wchar_p]
        tag = os.environ.get("DEPTH_MONITOR_HOME") or "default"
        name = "Local\\depth-monitor-single-" + hashlib.md5(tag.encode("utf-8")).hexdigest()[:8]
        h = kernel32.CreateMutexW(None, True, name)
        if not h:
            return None
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            return False
        return h
    except Exception as exc:
        print("warning: single-instance guard unavailable: %r" % (exc,))
        _log_exc("single_instance")
        return None


srv_ref = [None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--auto", action="store_true", help="start recording immediately")
    ap.add_argument("--idle-exit", type=int, default=90,
                    help="exit after N seconds with no page open (0 = never)")
    args = ap.parse_args()

    try:
        kernel32.timeBeginPeriod(1)
    except Exception:
        pass

    guard = single_instance()
    if guard is None:
        # without the guard two instances would overwrite mapping.json
        print("single-instance guard unavailable; refusing to start")
        if not args.no_browser:
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "无法创建单实例互斥量。\n"
                    "为了避免多个实例互相覆盖键位映射，程序不会启动。\n"
                    "请重启系统后再试。",
                    "启动失败", 0x30)
            except Exception:
                pass
        return
    if guard is False:
        print("another monitor is already running")
        # only bother with a modal box when a page was going to be opened;
        # in --no-browser/automated mode a modal would hang forever
        if not args.no_browser:
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "磁轴深度监测已经在运行了。\n"
                    "两个实例会互相覆盖键位映射，请先关掉另一个窗口。",
                    "已在运行", 0x40)
            except Exception:
                pass
        return

    start = (ENGINE.run_demo, ENGINE.rate_tick) if args.demo else \
            (ENGINE.run_reader, ENGINE.run_keys, ENGINE.rate_tick)
    for fn in start:
        t = threading.Thread(target=fn)
        t.daemon = True
        t.start()

    if args.auto:
        ENGINE.auto = True
        ENGINE.start_recording()
        t = threading.Thread(target=ENGINE.run_autosave, args=(120,))
        t.daemon = True
        t.start()

    if not args.demo:
        th = threading.Thread(target=ENGINE.run_hold)
        th.daemon = True
        th.start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv_ref[0] = srv
    th = threading.Thread(target=ENGINE.run_idle_exit, args=(args.idle_exit, srv))
    th.daemon = True
    th.start()
    port = srv.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    print("depth monitor on %s" % url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
