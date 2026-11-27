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
import sys
import subprocess
import shutil
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


def find_kb_frames():
    """Locate kb-frames.py: next to the exe, then the cwd, then this file."""
    here = []
    if getattr(sys, "frozen", False):
        here.append(os.path.dirname(os.path.abspath(sys.executable)))
    here.append(os.getcwd())
    here.append(os.path.dirname(os.path.abspath(__file__)))
    for folder in here:
        path = os.path.join(folder, "kb-frames.py")
        if os.path.isfile(path):
            return path
    return None


def find_kb_viz():
    """Locate kb-viz.py alongside kb-frames.py."""
    here = []
    if getattr(sys, "frozen", False):
        here.append(os.path.dirname(os.path.abspath(sys.executable)))
    here.append(os.getcwd())
    here.append(os.path.dirname(os.path.abspath(__file__)))
    for folder in here:
        path = os.path.join(folder, "kb-viz.py")
        if os.path.isfile(path):
            return path
    return None


def _python_has_pillow(exe):
    try:
        out = subprocess.run([exe, "-c", "import PIL;print(PIL.__version__)"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=4)
        return out.returncode == 0
    except Exception:
        return False


def find_python(kb_path=None):
    """Interpreter that can run the renderer: needs Pillow, which the exe lacks."""
    env = os.environ.get("DEPTH_RENDER_PYTHON")
    if env and os.path.isfile(env):
        return env
    cands = []
    if kb_path:
        root = os.path.dirname(os.path.abspath(kb_path))
        cands += [os.path.join(root, r".build\venv\Scripts\python.exe"),
                  os.path.join(root, r"venv\Scripts\python.exe"),
                  os.path.join(root, r".venv\Scripts\python.exe")]
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder:
            for name in ("python.exe", "python3.exe", "python", "python3"):
                p = os.path.join(folder, name)
                if os.path.isfile(p):
                    cands.append(p)
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    seen = set()
    for exe in cands:
        norm = os.path.normcase(os.path.abspath(exe)) if exe else ""
        if exe and norm not in seen and os.path.isfile(exe):
            seen.add(norm)
            if _python_has_pillow(exe):
                return exe
    return None


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
VERSION = "1.16.3"
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

WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
HWND_MESSAGE = ctypes.c_void_p(-3)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class _RAWMOUSE_BTN_PARTS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wt.USHORT), ("usButtonData", wt.USHORT)]


class _RAWMOUSE_BTNS(ctypes.Union):
    _fields_ = [("ulButtons", wt.ULONG), ("parts", _RAWMOUSE_BTN_PARTS)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("btns",)
    _fields_ = [("usFlags", wt.USHORT), ("btns", _RAWMOUSE_BTNS),
                ("ulRawButtons", wt.ULONG), ("lLastX", wt.LONG),
                ("lLastY", wt.LONG), ("ulExtraInformation", wt.ULONG)]


class RAWINPUT_MOUSE(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT]
user32.GetRawInputData.restype = wt.UINT
user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_void_p, ctypes.POINTER(wt.UINT), wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostThreadMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.GetCurrentThreadId.restype = wt.DWORD


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
           0x14: "Caps", 0x21: "PgUp", 0x22: "PgDn", 0x23: "End", 0x24: "Home",
           0x25: "\u2190", 0x26: "\u2191", 0x27: "\u2192", 0x28: "\u2193", 0x2E: "Del",
           0x5B: "LWin", 0x5C: "RWin",
           0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt", 0xA0: "LShift", 0xA1: "RShift",
           0xA2: "LCtrl", 0xA3: "RCtrl", 0xA4: "LAlt", 0xA5: "RAlt",
           0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
           0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}
for _fn in range(1, 13):
    KEYNAME[0x6F + _fn] = "F%d" % _fn

CAND_VKS = (list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x70, 0x7C)) +
            [0x20, 0x08, 0x09, 0x0D, 0x1B, 0x14, 0x21, 0x22, 0x23, 0x24,
             0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B, 0x5C,
             0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
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
        self.rec_mouse = []          # (t, dx, dy, btn_mask, wheel) only while recording
        self.max_rec_mouse = 300000  # hard cap for long recordings (~20MB max)
        self.mouse_rec_dropped = 0
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
        self._key_events = []
        self.export = {"running": False, "pct": 0, "msg": "", "out": "",
                       "proc": None, "t0": 0.0, "eta": 0, "error": ""}
        self.gaps = collections.deque(maxlen=8000)   # inter-arrival seconds
        self._last_depth_t = None
        self._gap_anomalies = 0
        self.token = secrets.token_hex(8)
        self.hold_n = 0
        self.auto = False
        self.vk_held = {}            # vk -> True while the OS reports it down
        self.key_state = {}          # vk -> {"gen": mapping generation, "ts": float}
        self.pending_learn = None    # (vk, deadline) waiting for its analog report
        self.mapping_gen = 0         # bumped whenever the index<->vk table changes
        self.suppressed = 0          # firmware switch-zero reports ignored
        self.released_by_watchdog = 0
        self.strafe_pair = ("A", "D")
        self._strafe_active = {}     # label -> bool
        self._strafe_overlap_at = None
        self._strafe_last_up = None  # (label, t)
        self._strafe_log = collections.deque(maxlen=24)
        self.settings_path = os.path.join(data_home, "settings.json")
        self.full_mm = FULL
        self.theme = "ramp"
        self.mouse_span = 14800
        self.mouse_events = []       # (dx, dy, wheel) live ring for SSE cursors
        self._mouse_abs = 0
        self.mouse_btns = {"l": False, "r": False, "m": False, "x1": False, "x2": False}
        self._load_settings()
        self._load_mapping()

    # ---- settings persistence ---------------------------------------
    def _load_settings(self):
        try:
            with io.open(self.settings_path, encoding="utf-8") as f:
                raw = json.load(f)
            fm = float(raw.get("full_mm", FULL))
            if 1.0 <= fm <= 5.0:
                self.full_mm = round(fm, 2)
            th = (raw.get("theme") or "").strip().lower()
            if th in ("ramp", "cyan", "mint", "amber", "crimson", "violet"):
                self.theme = th
            ms = int(round(float(raw.get("mouse_span", 14800))))
            if 500 <= ms <= 200000:
                self.mouse_span = ms
        except Exception:
            pass

    def update_settings(self, full_mm=None, theme=None, mouse_span=None, save=True):
        with self.lock:
            if full_mm is not None:
                try:
                    fm = float(full_mm)
                    if 1.0 <= fm <= 5.0:
                        self.full_mm = round(fm, 2)
                except (TypeError, ValueError):
                    pass
            if theme is not None:
                th = str(theme).strip().lower()
                if th in ("ramp", "cyan", "mint", "amber", "crimson", "violet"):
                    self.theme = th
            if mouse_span is not None:
                try:
                    ms = int(round(float(mouse_span)))
                    if 500 <= ms <= 200000:
                        self.mouse_span = ms
                except (TypeError, ValueError):
                    pass
            snap = {"full_mm": self.full_mm, "theme": self.theme, "mouse_span": self.mouse_span}
        if save:
            try:
                self._write_atomic(self.settings_path, json.dumps(snap, ensure_ascii=False, indent=1))
            except Exception:
                pass
        return snap

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
            self.bump_mapping(invalidate=True)
        self._save_mapping()

    def unbind_key(self, label):
        """Remove binding for one logical key label without resetting others."""
        if not label:
            return False
        target = label_to_vk(label)
        removed = False
        with self.lock:
            for idx, st in self.keys.items():
                name = key_name(st["vk"]) if st.get("vk") else None
                if (target is not None and st.get("vk") == target) or name == label:
                    st["vk"] = None
                    st["mm"] = 0.0
                    self.press_state.pop(idx, None)
                    removed = True
            for idx in [k for k, v in self.mapping.items()
                        if (target is not None and v == target) or key_name(v) == label]:
                self.mapping.pop(idx, None)
                removed = True
            if target is not None:
                self.vk_held.pop(target, None)
                self.key_state.pop(target, None)
            if self.pending_label == label:
                self.pending_label = None
            if removed:
                self.bump_mapping()
                self.bind_msg = "已解除 %s 绑定" % label
        if removed:
            self._save_mapping()
        return removed

    def set_strafe_pair(self, a, b):
        a = (a or "").strip()
        b = (b or "").strip()
        if not a or not b or a == b:
            return False
        with self.lock:
            self.strafe_pair = (a, b)
            self._strafe_active.clear()
            self._strafe_overlap_at = None
            self._strafe_last_up = None
            self._strafe_log.clear()
        return True

    def _update_strafe_locked(self, t, label, mm):
        a, b = self.strafe_pair
        if label not in (a, b):
            return
        other = b if label == a else a
        now_down = mm > 0.30
        was_down = bool(self._strafe_active.get(label, False))
        if now_down == was_down:
            return
        self._strafe_active[label] = now_down
        other_down = bool(self._strafe_active.get(other, False))
        if now_down:
            if other_down:
                if self._strafe_overlap_at is None:
                    self._strafe_overlap_at = t
            elif self._strafe_last_up and self._strafe_last_up[0] == other:
                gap_ms = max(0.0, (t - self._strafe_last_up[1]) * 1000.0)
                if gap_ms <= 1200.0:
                    self._strafe_log.append({"overlap_ms": 0.0, "gap_ms": gap_ms, "clean": True})
                self._strafe_last_up = None
        else:
            if self._strafe_overlap_at is not None and other_down:
                ov_ms = max(0.0, (t - self._strafe_overlap_at) * 1000.0)
                self._strafe_log.append({"overlap_ms": ov_ms, "gap_ms": 0.0, "clean": ov_ms <= 15.0})
                self._strafe_overlap_at = None
            elif not other_down:
                self._strafe_last_up = (label, t)
                self._strafe_overlap_at = None

    def strafe_stats(self):
        with self.lock:
            items = list(self._strafe_log)
            pair = list(self.strafe_pair)
        n = len(items)
        if not n:
            return {"pair": pair, "n": 0, "clean_pct": None,
                    "avg_overlap_ms": None, "last_overlap_ms": None, "last_gap_ms": None}
        clean = sum(1 for x in items if x["clean"])
        last = items[-1]
        return {
            "pair": pair,
            "n": n,
            "clean_pct": round(clean * 100.0 / n),
            "avg_overlap_ms": round(sum(x["overlap_ms"] for x in items) / n, 1),
            "last_overlap_ms": round(last["overlap_ms"], 1),
            "last_gap_ms": round(last["gap_ms"], 1),
        }

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

    def bump_mapping(self, invalidate=False):
        """The index<->vk table changed.

        Learning or binding a new key must NOT invalidate the physical state of
        keys that are still held: the hold writer relies on that state, and a
        blanket invalidation silently disabled multi-key recording.
        Only an explicit reset (user pressed 重新识别) drops everything.
        """
        self.mapping_gen += 1
        if invalidate:
            self.key_state.clear()

    def os_key_down(self, vk):
        """Is this key physically down right now?

        This is OS state, independent of which matrix index the key maps to, so
        it must not be tied to the mapping generation: doing that made every
        "learn a new key" invalidate the keys you were still holding, which
        silently disabled multi-key recording.
        """
        return bool(self.vk_held.get(vk))

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
            if self._last_depth_t is not None:
                gap = t - self._last_depth_t
                if gap >= 0:
                    self.gaps.append(gap)
                    if gap > 0.05:                 # > 50 ms: suspicious
                        self._gap_anomalies += 1
            self._last_depth_t = t
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
            if st.get("vk") is not None:
                self._update_strafe_locked(t, key_name(st["vk"]), mm)
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

    def _mouse_btn_mask_locked(self):
        b = self.mouse_btns
        return ((1 if b["l"] else 0) | (2 if b["r"] else 0) |
                (4 if b["m"] else 0) | (8 if b["x1"] else 0) | (16 if b["x2"] else 0))

    def on_mouse(self, dx, dy, btns=None, wheel=0, t=None):
        with self.lock:
            changed = False
            if btns:
                for k, v in btns.items():
                    if self.mouse_btns.get(k) != bool(v):
                        changed = True
                    self.mouse_btns[k] = bool(v)
            if dx or dy or wheel:
                self.mouse_events.append((int(dx), int(dy), int(wheel)))
                if len(self.mouse_events) > 4000:
                    del self.mouse_events[:1000]
                self._mouse_abs += 1
            if self.recording and (dx or dy or wheel or changed):
                ts = time.perf_counter() if t is None else float(t)
                if self.rec_t0 is None:
                    self.rec_t0 = ts
                self.rec_mouse.append((ts, int(dx), int(dy), self._mouse_btn_mask_locked(), int(wheel)))
                if self.max_rec_mouse > 0 and len(self.rec_mouse) > self.max_rec_mouse:
                    drop = max(1, self.max_rec_mouse // 5)
                    del self.rec_mouse[:drop]
                    self.mouse_rec_dropped += drop
                    self.notice = ("\u9f20\u6807\u5f55\u5236\u7f13\u5b58\u5df2\u8fbe\u4e0a\u9650\uff0c"
                                   "\u5df2\u6eda\u52a8\u88c1\u526a\u6700\u65e7 %d \u6761\u4e8b\u4ef6"
                                   % self.mouse_rec_dropped)

    def run_mouse(self):
        """Passive read-only Windows Raw Input mouse listener (works in locked-cursor FPS games)."""
        hwnd = user32.CreateWindowExW(
            0, "STATIC", "depth-mouse-sink", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, None, None,
        )
        if not hwnd:
            return
        try:
            rid = RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
                return
            msg = wt.MSG()
            buf = (ctypes.c_ubyte * 128)()
            sz = wt.UINT(0)
            hdr_sz = ctypes.sizeof(RAWINPUTHEADER)
            while not self.stop_io.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), hwnd, 0, 0)
                if r <= 0:
                    break
                if msg.message == WM_INPUT:
                    sz.value = ctypes.sizeof(buf)
                    got = user32.GetRawInputData(
                        ctypes.c_void_p(msg.lParam), RID_INPUT,
                        buf, ctypes.byref(sz), hdr_sz,
                    )
                    if got and got >= ctypes.sizeof(RAWINPUT_MOUSE):
                        ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT_MOUSE)).contents
                        if ri.header.dwType == RIM_TYPEMOUSE:
                            rm = ri.mouse
                            dx = rm.lLastX if not (rm.usFlags & 0x01) else 0
                            dy = rm.lLastY if not (rm.usFlags & 0x01) else 0
                            bf = rm.parts.usButtonFlags
                            btns = {}
                            if bf & 0x0001: btns["l"] = True
                            if bf & 0x0002: btns["l"] = False
                            if bf & 0x0004: btns["r"] = True
                            if bf & 0x0008: btns["r"] = False
                            if bf & 0x0010: btns["m"] = True
                            if bf & 0x0020: btns["m"] = False
                            if bf & 0x0040: btns["x1"] = True
                            if bf & 0x0080: btns["x1"] = False
                            if bf & 0x0100: btns["x2"] = True
                            if bf & 0x0200: btns["x2"] = False
                            wheel = 0
                            if bf & 0x0400:
                                wd = ctypes.c_short(rm.parts.usButtonData).value
                                wheel = 1 if wd > 0 else (-1 if wd < 0 else 0)
                            if dx or dy or btns or wheel:
                                self.on_mouse(dx, dy, btns, wheel)
                else:
                    user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            _log_exc("run_mouse")
        finally:
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass

    def run_demo(self):
        """Synthetic presses + smooth mouse figure-8, only used with --demo."""
        import math
        plan = ((31, "A", 1.40, 0.0), (32, "S", 1.90, 0.45),
                (33, "D", 1.10, 0.80), (61, "Space", 2.60, 1.30))
        t0 = time.perf_counter()
        prev_mx, prev_my = 0.0, 0.0
        while True:
            now = time.perf_counter()
            dt_t = now - t0
            full = self.full_mm
            for idx, name, period, phase in plan:
                el = (dt_t + phase) % period
                hold = period * 0.55
                if el < 0.16:
                    mm = full * (el / 0.16)
                elif el < hold:
                    mm = full * (0.72 + 0.28 * abs(math.sin(el * 6.0)))
                elif el < hold + 0.14:
                    mm = full * (1 - (el - hold) / 0.14)
                else:
                    mm = 0.0
                with self.lock:
                    st = self.keys.get(idx)
                    if st is None:
                        st = self.keys[idx] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
                    st["vk"] = 0x41 if name == "A" else (0x53 if name == "S"
                                                         else (0x44 if name == "D" else 0x20))
                self.on_depth(now, idx, round(max(0.0, min(full, mm)), 2))
            # Synthetic mouse figure-8 + burst clicks (scaled to ~14800-count G-SR III pad)
            mx = math.sin(dt_t * 1.35) * 5200.0
            my = math.sin(dt_t * 2.70) * 3000.0
            dx, dy = int(round(mx - prev_mx)), int(round(my - prev_my))
            prev_mx += dx
            prev_my += dy
            l_down = (dt_t % 1.6) < 0.22
            r_down = 0.85 < (dt_t % 2.4) < 1.35
            x1_down = 2.0 < (dt_t % 3.2) < 2.25
            self.on_mouse(dx, dy, {"l": l_down, "r": r_down, "m": False, "x1": x1_down, "x2": False}, 0)
            time.sleep(0.01)

    def start_recording(self):
        with self.lock:
            self.rec = []
            self.rec_mouse = []
            self.mouse_rec_dropped = 0
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

    def mouse_lines(self, t0=None, rows=None):
        """Yield synchronized mouse CSV aligned to the same perf_counter t0 as long/wide CSV."""
        with self.lock:
            rows = list(self.rec_mouse) if rows is None else list(rows)
            if t0 is None and self.rec:
                t0 = self.rec[0][0]
            span = self.mouse_span
        yield "# tool=depth-live  mouse_span=%d  btn_mask=1:L,2:R,4:M,8:X1,16:X2\n" % span
        yield "t_s,dx,dy,btn,wheel\n"
        if not rows:
            return
        if t0 is None:
            t0 = rows[0][0]
        batch = []
        for t, dx, dy, mask, w in rows:
            rel = max(0.0, t - t0)
            batch.append("%.6f,%d,%d,%d,%d\n" % (rel, dx, dy, mask, w))
            if len(batch) >= 500:
                yield "".join(batch)
                batch = []
        if batch:
            yield "".join(batch)

    def save_recording(self, rec_rows=None, mouse_rows=None):
        """Write the current (or provided snapshot) recording and report wide.csv path."""
        with self.lock:
            r_rows = list(self.rec) if rec_rows is None else list(rec_rows)
            m_rows = list(self.rec_mouse) if mouse_rows is None else list(mouse_rows)
        n = len(r_rows)
        nm = len(m_rows)
        has_mouse = nm > 0
        if not n and not nm:
            with self.lock:
                self.notice = "\u6ca1\u6709\u5f55\u5236\u6570\u636e"
            return None
        starts = []
        if r_rows:
            starts.append(r_rows[0][0])
        if m_rows:
            starts.append(m_rows[0][0])
        t0 = min(starts)
        min_dur = max(0.02 if not r_rows else 0.0,
                      (m_rows[-1][0] - t0) if m_rows else 0.0)
        base = recordings_dir()
        base_stamp = time.strftime("%Y%m%d-%H%M%S")
        stamp = base_stamp
        version_dir = os.path.join(base, stamp)
        suffix = 1
        while os.path.exists(version_dir):
            stamp = "%s-%d" % (base_stamp, suffix)
            version_dir = os.path.join(base, stamp)
            suffix += 1
        long_path = os.path.join(version_dir, "long.csv")
        wide_path = os.path.join(version_dir, "wide.csv")
        mouse_path = os.path.join(version_dir, "mouse.csv")
        try:
            if not os.path.isdir(version_dir):
                os.makedirs(version_dir)
            # all files are written inside one fresh directory; the manifest
            # below is the only thing that makes the recording "committed"
            pairs = [
                (long_path, self.long_lines(rows=r_rows, t0=t0)),
                (wide_path, self.wide_lines(rows=r_rows, t0=t0, min_dur=min_dur)),
            ]
            if has_mouse:
                pairs.append((mouse_path, self.mouse_lines(t0=t0, rows=m_rows)))
            for path, chunks in pairs:
                with io.open(path, "w", encoding="utf-8", newline="") as f:
                    for chunk in chunks:
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            integrity = {}
            check_list = [("long", long_path, 2), ("wide", wide_path, 2)]
            if has_mouse:
                check_list.append(("mouse", mouse_path, 3))
            for key, path, min_lines in check_list:
                with io.open(path, encoding="utf-8", errors="replace") as f:
                    lines = 0
                    for _ in f:
                        lines += 1
                sz = os.path.getsize(path)
                integrity[key] = {"lines": lines, "bytes": sz,
                                  "ok": (lines >= min_lines and sz > 0)}
            integrity["ok"] = all(integrity[k]["ok"] for k, _, _ in check_list)
            if not integrity["ok"]:
                raise IOError("integrity check failed: %r" % (integrity,))
            manifest = {
                "stamp": stamp,
                "samples": n or nm,
                "mouse_samples": nm,
                "long": os.path.relpath(long_path, base).replace("\\", "/"),
                "wide": os.path.relpath(wide_path, base).replace("\\", "/"),
                "mouse": (os.path.relpath(mouse_path, base).replace("\\", "/")
                          if has_mouse else ""),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "integrity": integrity,
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
            self.notice = ("\u5df2\u81ea\u52a8\u4fdd\u5b58 %d \u70b9\uff1a"
                           "recordings\\%s\\wide.csv" % (n or nm, stamp))
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
                    pending = len(self.rec) + len(self.rec_mouse)
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
        step = min(1.5, max(0.2, seconds / 4.0))
        while True:
            time.sleep(step)
            with self.lock:
                busy = self.recording or self.auto
            if busy:
                self.last_client = time.time()
                continue
            if time.time() - self.last_client > seconds:
                self.shutdown_app(srv)          # same ordered path as /quit
                return

    def autosave_step(self, min_samples=20):
        """Atomically swap recording buffers before saving; restore on failure."""
        with self.lock:
            if not self.recording:
                return None
            if len(self.rec) <= min_samples and len(self.rec_mouse) <= min_samples:
                return None
            seg_rec, self.rec = self.rec, []
            seg_mouse, self.rec_mouse = self.rec_mouse, []
            seg_t0, self.rec_t0 = self.rec_t0, None
        saved = self.save_recording(rec_rows=seg_rec, mouse_rows=seg_mouse)
        if not saved:
            with self.lock:
                self.rec = seg_rec + self.rec
                self.rec_mouse = seg_mouse + self.rec_mouse
                if seg_t0 is not None:
                    self.rec_t0 = seg_t0
                elif self.rec_t0 is None:
                    if self.rec:
                        self.rec_t0 = self.rec[0][0]
                    elif self.rec_mouse:
                        self.rec_t0 = self.rec_mouse[0][0]
            return None
        return saved

    def run_autosave(self, interval):
        """Auto mode: save a segment every `interval` seconds and keep going."""
        while True:
            time.sleep(max(15, interval))
            self.autosave_step(min_samples=20)

    # ---- workbench -------------------------------------------------
    def _safe_stamp(self, stamp):
        s = (stamp or "").strip()
        if not s or "/" in s or "\\" in s or ".." in s:
            return None
        return s

    def wb_manifest(self):
        base = recordings_dir()
        path = os.path.join(base, "latest.json")
        if not os.path.isfile(path):
            return None, base
        try:
            with io.open(path, encoding="utf-8") as f:
                return json.load(f), base
        except Exception:
            return None, base

    def _read_wide_meta(self, wide_path):
        keys, dur, samples = [], 0.0, 0
        with io.open(wide_path, encoding="utf-8") as f:
            header = None
            for line in f:
                if line.startswith("#"):
                    continue
                if header is None:
                    header = [h.strip() for h in line.strip().split(",")]
                    keys = header[2:]
                    continue
                samples += 1
                dur = float(line.split(",")[0])
        return keys, dur, samples

    def _resolve_wide(self, stamp=None):
        base = recordings_dir()
        safe = self._safe_stamp(stamp)
        if safe:
            wide = os.path.join(base, safe, "wide.csv")
            long_p = os.path.join(base, safe, "long.csv")
            if not os.path.isfile(wide):
                return None, base, safe, None
            return {"stamp": safe, "wide": safe + "/wide.csv",
                    "long": safe + "/long.csv" if os.path.isfile(long_p) else "",
                    "created": safe}, base, safe, wide
        manifest, base = self.wb_manifest()
        if not manifest:
            return None, base, None, None
        wide = os.path.join(base, manifest.get("wide", ""))
        if not os.path.isfile(wide):
            return None, base, manifest.get("stamp"), None
        return manifest, base, manifest.get("stamp"), wide

    def wb_list(self):
        base = recordings_dir()
        manifest, _ = self.wb_manifest()
        latest_stamp = manifest.get("stamp") if manifest else None
        items = []
        if os.path.isdir(base):
            for name in sorted(os.listdir(base), reverse=True):
                if name in ("exports",) or name.startswith("."):
                    continue
                wide = os.path.join(base, name, "wide.csv")
                if not os.path.isfile(wide):
                    continue
                try:
                    keys, dur, samples = self._read_wide_meta(wide)
                    mouse_p = os.path.join(base, name, "mouse.csv")
                    items.append({
                        "stamp": name,
                        "duration": round(dur, 3),
                        "samples": samples,
                        "keys": keys,
                        "has_mouse": os.path.isfile(mouse_p),
                        "is_latest": name == latest_stamp,
                    })
                except Exception:
                    continue
        return {"ok": True, "base": base, "latest": latest_stamp, "items": items}

    def wb_delete(self, stamp):
        safe = self._safe_stamp(stamp)
        if not safe:
            return False, "无效的录制标识"
        base = os.path.abspath(recordings_dir())
        target = os.path.abspath(os.path.join(base, safe))
        if not target.startswith(base + os.sep) or not os.path.isdir(target):
            return False, "录制目录不存在"
        try:
            shutil.rmtree(target)
        except Exception as exc:
            return False, "删除失败：%s" % exc
        manifest, _ = self.wb_manifest()
        if manifest and manifest.get("stamp") == safe:
            remaining = self.wb_list().get("items") or []
            latest_path = os.path.join(base, "latest.json")
            if remaining:
                top = remaining[0]
                new_manifest = {
                    "stamp": top["stamp"],
                    "samples": top["samples"],
                    "long": top["stamp"] + "/long.csv",
                    "wide": top["stamp"] + "/wide.csv",
                    "created": top["stamp"],
                }
                self._write_atomic(latest_path, json.dumps(new_manifest, ensure_ascii=False, indent=1))
            elif os.path.isfile(latest_path):
                try:
                    os.remove(latest_path)
                except Exception:
                    pass
        return True, "已删除 %s" % safe

    def wb_info(self, stamp=None):
        manifest, base, _, wide = self._resolve_wide(stamp)
        if not manifest or not wide:
            return {"ok": False, "error": "\u8fd8\u6ca1\u6709\u5f55\u5236",
                    "base": base}
        try:
            keys, dur, samples = self._read_wide_meta(wide)
        except Exception as exc:
            return {"ok": False, "error": "\u8bfb\u53d6\u5f55\u5236\u5931\u8d25\uff1a%s" % exc,
                    "base": base}
        mouse_p = os.path.join(os.path.dirname(wide), "mouse.csv")
        return {"ok": True, "base": base, "stamp": manifest.get("stamp"),
                "created": manifest.get("created"), "duration": round(dur, 3),
                "samples": samples, "keys": keys,
                "has_mouse": os.path.isfile(mouse_p),
                "wide": manifest.get("wide"), "long": manifest.get("long"),
                "integrity": manifest.get("integrity")}

    def _load_wb_mouse(self, wide_path, times):
        """Integrate mouse.csv onto `times` with smart lift-off recenter for scrub-safe replay."""
        mouse_p = os.path.join(os.path.dirname(wide_path), "mouse.csv")
        if not os.path.isfile(mouse_p) or not times:
            return None
        import math, re
        span = self.mouse_span
        evts = []
        try:
            with io.open(mouse_p, encoding="utf-8") as f:
                header_seen = False
                for line in f:
                    if line.startswith("#"):
                        m = re.search(r"mouse_span\s*=\s*(\d+)", line)
                        if m:
                            v = int(m.group(1))
                            if 500 <= v <= 200000:
                                span = v
                        continue
                    if not header_seen:
                        header_seen = True
                        continue
                    parts = line.strip().split(",")
                    if len(parts) < 5:
                        continue
                    evts.append((float(parts[0]), int(parts[1]), int(parts[2]),
                                 int(parts[3]), int(parts[4])))
        except Exception:
            return None
        if not evts:
            return None
        xs, ys, bs, ws, spds = [], [], [], [], []
        clicks = []
        wheels = []
        tx = ty = 0.0
        btn = prev_btn = 0
        down_at = {}
        last_move = -9.0
        prev_t = times[0]
        idx = 0
        n_ev = len(evts)
        step_x = 2.0 / max(500.0, float(span))
        aspect = (306.0 - 44.0) / (254.0 - 84.0)
        btn_bits = (1, 2, 4, 8, 16)
        for t in times:
            dt = max(0.0, min(0.2, t - prev_t))
            prev_t = t
            sum_dx = sum_dy = w_acc = 0
            b_hit = 0
            while idx < n_ev and evts[idx][0] <= t + 1e-6:
                et, dx, dy, bmask, wh = evts[idx]
                sum_dx += dx
                sum_dy += dy
                rising = (~prev_btn) & bmask
                if rising:
                    for bit in btn_bits:
                        if rising & bit:
                            down_at[bit] = et
                falling = prev_btn & (~bmask)
                if falling:
                    for bit in btn_bits:
                        if falling & bit:
                            t_dn = down_at.pop(bit, et)
                            clicks.append([round(t_dn, 4), bit, round(max(0.0, (et - t_dn) * 1000.0), 1)])
                b_hit |= bmask
                prev_btn = bmask
                btn = bmask
                if wh:
                    w_acc += wh
                    wheels.append([round(et, 4), int(wh)])
                if dx or dy:
                    last_move = et
                idx += 1
            if sum_dx or sum_dy:
                tx = max(-1.0, min(1.0, tx + sum_dx * step_x))
                ty = max(-1.0, min(1.0, ty + sum_dy * step_x * aspect))
            elif t - last_move > 0.20 and dt > 0:
                dist = min(1.4, math.hypot(tx, ty))
                k = math.exp(-dt * (1.6 + dist * 4.2))
                tx *= k
                ty *= k
            spd = min(1.0, math.hypot(sum_dx, sum_dy) / max(1.0, span * max(0.015, dt) * 1.4))
            xs.append(round(tx, 3))
            ys.append(round(ty, 3))
            bs.append(btn | b_hit)
            ws.append(w_acc)
            spds.append(round(spd, 2))
        end_t = times[-1]
        for bit, t_dn in down_at.items():
            clicks.append([round(t_dn, 4), bit, round(max(0.0, (end_t - t_dn) * 1000.0), 1)])
        clicks.sort(key=lambda c: c[0])
        return {"span": span, "x": xs, "y": ys, "b": bs, "w": ws,
                "spd": spds, "clicks": clicks, "wheels": wheels}

    def wb_series(self, step=0.02, limit=1600, stamp=None):
        manifest, _, _, wide = self._resolve_wide(stamp)
        if not manifest or not wide:
            return {"ok": False, "error": "no recording"}
        rows = []
        keys = []
        try:
            with io.open(wide, encoding="utf-8") as f:
                header_seen = False
                for line in f:
                    if line.startswith("#"):
                        continue
                    parts = line.strip().split(",")
                    if not header_seen:
                        header_seen = True
                        keys = [p for p in parts[2:] if p]
                        continue
                    # column 1 is a wall-clock stamp, not a number
                    rows.append([float(parts[0])] + [float(x) for x in parts[2:2 + len(keys)]])
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not rows:
            return {"ok": False, "error": "empty recording"}
        stride = max(1, int(len(rows) / limit))
        rows = rows[::stride]
        ts = [round(r[0], 3) for r in rows]
        mouse = self._load_wb_mouse(wide, ts)
        return {"ok": True, "stamp": manifest.get("stamp"), "keys": keys, "duration": rows[-1][0],
                "step": round(rows[1][0] - rows[0][0], 4) if len(rows) > 1 else step,
                "t": ts,
                "v": {keys[i]: [round(r[1 + i], 2) for r in rows]
                      for i in range(len(keys))},
                "mouse": mouse}

    def export_status(self):
        with self.lock:
            job = dict(self.export)
        job.pop("proc", None)
        return job

    def cancel_export(self):
        with self.lock:
            proc = self.export.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            with self.lock:
                self.export.update(running=False, msg="\u5df2\u53d6\u6d88", pct=0)
            return True
        return False

    def start_export(self, params):
        with self.lock:
            if self.export.get("running"):
                return False, "\u5df2\u6709\u5bfc\u51fa\u4efb\u52a1\u5728\u8fd0\u884c"
        kb = find_kb_frames()
        py = find_python(kb)
        if not kb or not py:
            return False, ("\u627e\u4e0d\u5230\u6e32\u67d3\u5668\uff1a\u9700\u8981 kb-frames.py \u4e0e "
                           "Pillow\u3002\u8bf7\u628a kb-frames.py \u653e\u5728 exe \u540c\u76ee\u5f55\uff0c"
                           "\u5e76\u5b89\u88c5 python -m pip install pillow")
        manifest, base, _, wide = self._resolve_wide(params.get("stamp"))
        if not manifest or not wide:
            return False, "\u8fd8\u6ca1\u6709\u5f55\u5236"
        start = float(params.get("start") or 0.0)
        end = float(params.get("end") or 0.0)
        fps = int(params.get("fps") or 60)
        size = params.get("size") or "1920x1080"
        layout = params.get("layout") or "hud"
        theme = (params.get("theme") or self.theme or "ramp").strip()
        full_mm = float(params.get("full_mm") or self.full_mm)
        keys = [k for k in (params.get("keys") or []) if k]
        speed = float(params.get("speed") or 1.0)
        bg = (params.get("bg") or "").strip()
        png = bool(params.get("png"))
        out_name = params.get("out") or ("depth-%s" % time.strftime("%Y%m%d-%H%M%S"))
        exports = os.path.join(base, "exports")
        try:
            if not os.path.isdir(exports):
                os.makedirs(exports)
        except Exception:
            exports = base
        out_path = os.path.join(exports, out_name)
        argv = [py, kb, wide, "--fps", str(fps), "--size", size,
                "--layout", layout, "--theme", theme, "--full", "%.2f" % full_mm,
                "--speed", str(speed), "-o", out_path]
        if keys:
            argv += ["--keys"] + keys
        if start > 0:
            argv += ["--start", "%.3f" % start]
        if end > 0:
            argv += ["--end", "%.3f" % end]
        if bg:
            argv += ["--bg", bg]
        if png:
            argv += ["--png-only", "--keep-frames"]
        try:
            proc = subprocess.Popen(argv, cwd=os.path.dirname(kb),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    universal_newlines=True, encoding="utf-8",
                                    errors="replace")
        except Exception as exc:
            return False, "\u542f\u52a8\u6e32\u67d3\u5668\u5931\u8d25\uff1a%s" % exc
        with self.lock:
            self.export = {"running": True, "pct": 0, "msg": "\u6b63\u5728\u6e32\u67d3",
                           "out": out_path, "proc": proc, "t0": time.time(),
                           "eta": 0, "error": "", "argv": " ".join(argv)}

        def pump():
            last = 0
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                pct = None
                if "frames encoded" in line or "frames" in line and "/" in line:
                    try:
                        part = [w for w in line.split() if "/" in w][0]
                        done, total = part.split("/")
                        pct = int(float(done) * 100 / max(1.0, float(total)))
                    except Exception:
                        pct = None
                with self.lock:
                    if pct is not None:
                        self.export["pct"] = min(99, pct)
                        elapsed = time.time() - self.export["t0"]
                        if pct > 2:
                            self.export["eta"] = int(elapsed * (100 - pct) / pct)
                    self.export["msg"] = line[:120]
                last = pct or last
            rc = proc.wait()
            with self.lock:
                self.export.update(running=False, pct=100 if rc == 0 else 0,
                                   msg="\u5b8c\u6210" if rc == 0 else "\u5931\u8d25 (rc=%d)" % rc,
                                   error="" if rc == 0 else "rc=%d" % rc)

        t = threading.Thread(target=pump)
        t.daemon = True
        t.start()
        return True, out_path

    def sample_quality(self):
        """How healthy is the HID stream right now (or during the last N packets)."""
        with self.lock:
            gaps = sorted(self.gaps)
            n = len(gaps)
            if not n:
                return {"n": 0, "rate": round(self.depth_rate, 1), "mean_ms": 0.0,
                        "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0,
                        "anomalies": self._gap_anomalies, "dropouts": 0}
            def pct(q):
                idx = min(n - 1, max(0, int(round(q * (n - 1)))))
                return gaps[idx] * 1000.0
            mean = sum(gaps) / n
            return {
                "n": n,
                "rate": round(1.0 / mean, 1) if mean > 0 else 0.0,
                "mean_ms": round(mean * 1000.0, 2),
                "p95_ms": round(pct(0.95), 2),
                "p99_ms": round(pct(0.99), 2),
                "max_ms": round(gaps[-1] * 1000.0, 2),
                "anomalies": self._gap_anomalies,
                "dropouts": sum(1 for g in gaps if g > 0.5),
            }

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
            self.sweep_silent_released(now)
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

    def _log_key(self, vk, down):
        """Every OS-level transition, timestamped: used to tell a real release
        from a synthetic key-up sent by the keyboard firmware."""
        line = "%s vk=%-3d %-4s %s\n" % (
            time.strftime("%H:%M:%S") + (".%03d" % (int(time.time() * 1000) % 1000)),
            vk, "down" if down else "up",
            key_name(vk) if "key_name" in globals() else vk)
        try:
            path = os.path.join(data_home(), "keyevents.log")
            if not os.path.isdir(data_home()):
                os.makedirs(data_home())
            with io.open(path, "a", encoding="utf-8") as f:
                f.write(line)
            self._key_events.append(line.strip())
            if len(self._key_events) > 40:
                del self._key_events[:10]
        except Exception:
            pass

    def sweep_silent_released(self, now, silence=0.12):
        """Fallback zero for keys that are OS-up AND whose HID analog stream went silent.

        With Rapid Trigger or SOCD enabled, OS Key-Up fires while the physical magnet
        is still depressed (e.g. 3.25 mm) and the keyboard keeps streaming real 100 Hz
        travel until physical release (0.00 mm). Only zero here if the stream actually
        stopped updating for longer than `silence` seconds.
        """
        with self.lock:
            stale = []
            for idx, st in self.keys.items():
                vk = st.get("vk")
                if vk is None or st.get("mm", 0.0) <= 0.0:
                    continue
                if self.os_key_down(vk):
                    continue
                if now - st.get("t_last", 0.0) > silence:
                    stale.append(idx)
            for idx in stale:
                self.on_depth(now, idx, 0.0)

    def apply_key_state(self, vk, down, now):
        """Apply one physical key transition. Shared by the edge detector and the
        watchdog so a missed edge cannot leave a key stuck as held."""
        try:
            self._log_key(vk, down)
            with self.lock:
                self.mark_key(vk, down, now)
                # hold the lock across the transition so a mapping reset from
                # another thread cannot slip in between; also measure RT release
                # depth BEFORE any silent-fallback zeroing
                bumped = self._key_transition(vk, down, now)
                if bumped and self.key_known(vk):
                    # this very press taught/bound the key: keep its state
                    self.refresh_key_state(vk, down, now)
                if not down:
                    self.sweep_silent_released(now)
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
    def snapshot(self, cursor, mcursor=None):
        with self.lock:
            total = self._abs
            dropped = self._abs - len(self.samples)
            if cursor < dropped:
                cursor = dropped
            if cursor > self._abs:
                cursor = self._abs
            batch = self.samples[cursor - dropped:]
            mtotal = self._mouse_abs
            mdx = mdy = mw = 0
            if mcursor is not None:
                mdropped = mtotal - len(self.mouse_events)
                mc = max(mdropped, min(mtotal, mcursor))
                for dx, dy, w in self.mouse_events[mc - mdropped:]:
                    mdx += dx
                    mdy += dy
                    mw += w
            mstate = {
                "n": mtotal, "dx": mdx, "dy": mdy, "w": mw,
                "l": self.mouse_btns["l"], "r": self.mouse_btns["r"],
                "m": self.mouse_btns["m"], "x1": self.mouse_btns["x1"],
                "x2": self.mouse_btns["x2"],
            }
            out = {
                "s": [[round(t, 4), i, mm] for t, i, mm in batch],
                "n": total,
                "m": mstate,
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
                "quality": self.sample_quality(),
                "strafe": self.strafe_stats(),
                "key_events": self._key_events[-12:],
                "suppressed": self.suppressed,
                "watchdog_releases": self.released_by_watchdog,
                "mapping_gen": self.mapping_gen,
                "auto": self.auto,
                "version": VERSION,
                "token": self.token,
                "full_mm": round(self.full_mm, 2),
                "theme": self.theme,
                "mouse_span": self.mouse_span,
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
    def long_lines(self, rows=None, t0=None):
        """Yield the long-format CSV line by line (no giant string in memory)."""
        with self.lock:
            rows = list(self.rec) if rows is None else list(rows)
            names = {i: (key_name(s["vk"]) if s["vk"] else "k%d" % i)
                     for i, s in self.keys.items()}
        yield "# tool=depth-live  device=ROG FALCHION ACE 75 HE (VID 0B05 PID 1C7D)\n"
        yield "# unit=0.01mm  full=%.2fmm  started=%s\n" % (self.full_mm, self.started)
        yield "t_s,wall,key,index,mm\n"
        if not rows:
            yield "# nothing recorded -- press 开始录制 first\n"
            return
        if t0 is None:
            t0 = rows[0][0]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(0, len(rows), 500):
            chunk = []
            for t, idx, mm in rows[i:i + 500]:
                chunk.append("%.6f,%s,%s,%d,%.2f\n"
                             % (max(0.0, t - t0), stamp, names.get(idx, ""), idx, mm))
            yield "".join(chunk)

    def wide_lines(self, step=0.01, rows=None, t0=None, min_dur=0.0):
        """Yield the fixed-rate matrix CSV line by line."""
        with self.lock:
            rows = list(self.rec) if rows is None else list(rows)
            names = {i: (key_name(s["vk"]) if s["vk"] else "k%d" % i)
                     for i, s in self.keys.items()}
        yield "# tool=depth-live  wide matrix %.0f Hz  depth in mm\n" % (1.0 / step)
        if not rows and min_dur <= 0.0:
            yield "# nothing recorded -- press 开始录制 first\nt_s\n"
            return
        if t0 is None:
            t0 = rows[0][0] if rows else 0.0
        series = {}
        for t, idx, mm in rows:
            series.setdefault(idx, []).append((max(0.0, t - t0), mm))
        # keys touched outside the recorded window have no samples: drop them,
        # otherwise they contribute empty columns and break the duration calc
        series = {k: v for k, v in series.items() if v}
        cols = sorted(series)
        k_dur = max(v[-1][0] for v in series.values()) if series else 0.0
        dur = max(k_dur, float(min_dur))
        header = "t_s,wall" + (("," + ",".join(names.get(i, "k%d" % i) for i in cols)) if cols else "")
        yield header + "\n"
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
            line = ("%.3f,%s,%s\n" % (ts, stamp, ",".join(cells))) if cols else ("%.3f,%s\n" % (ts, stamp))
            batch.append(line)
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
                   "/unbind", "/strafe", "/settings", "/resetmap", "/reset",
                   "/openfolder", "/quit", "/wb/export", "/wb/cancel", "/wb/delete")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        elif url.path == "/overlay":
            body = overlay_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/overlay/mouse":
            body = mouse_overlay_html().encode("utf-8")
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
                    ENGINE.rec_mouse = []
                    ENGINE.rec_t0 = None
                    ENGINE.recording = True
                elif url.path == "/rec/stop":
                    ENGINE.recording = False
                else:
                    ENGINE.rec = []
                    ENGINE.rec_mouse = []
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
        elif url.path == "/wb":
            body = workbench_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/wb/list":
            self._send_json(ENGINE.wb_list())
        elif url.path == "/wb/info":
            q = parse_qs(url.query)
            stamp = (q.get("stamp") or [None])[0]
            self._send_json(ENGINE.wb_info(stamp=stamp))
        elif url.path == "/wb/series":
            q = parse_qs(url.query)
            step = float((q.get("step") or ["0.02"])[0] or 0.02)
            limit = int((q.get("limit") or ["1600"])[0] or 1600)
            stamp = (q.get("stamp") or [None])[0]
            try:
                self._send_json(ENGINE.wb_series(step, limit=limit, stamp=stamp))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
        elif url.path == "/wb/viz":
            q = parse_qs(url.query)
            stamp = (q.get("stamp") or [None])[0]
            start = float((q.get("start") or ["0"])[0] or 0.0)
            end = float((q.get("end") or ["0"])[0] or 0.0)
            theme = (q.get("theme") or ["ramp"])[0]
            keys_q = [k for k in (q.get("keys") or [""])[0].split(",") if k]
            html_doc = build_wb_viz_html(ENGINE, stamp=stamp, start=start, end=end,
                                         theme=theme, keys=keys_q)
            if html_doc is None:
                self._send_text(404, "no recording")
                return
            body = html_doc.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="depth-replay-%s.html"'
                             % (stamp or time.strftime("%Y%m%d-%H%M%S")))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/wb/delete":
            raw = getattr(self, "_body", b"") or b"{}"
            try:
                params = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                params = {}
            ok, msg = ENGINE.wb_delete(params.get("stamp"))
            self._send_json({"ok": ok, "message": msg})
        elif url.path == "/wb/status":
            self._send_json(ENGINE.export_status())
        elif url.path == "/wb/cancel":
            self._send_json({"ok": ENGINE.cancel_export()})
        elif url.path == "/wb/export":
            raw = getattr(self, "_body", b"") or b"{}"
            try:
                params = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                params = {}
            ok, msg = ENGINE.start_export(params)
            self._send_json({"ok": ok, "message": msg})
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
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/unbind":
            q = parse_qs(url.query)
            label = (q.get("key") or [None])[0]
            ok = ENGINE.unbind_key(label)
            self._send_json({"ok": ok})
        elif url.path == "/strafe":
            q = parse_qs(url.query)
            a = (q.get("a") or ["A"])[0]
            b = (q.get("b") or ["D"])[0]
            ok = ENGINE.set_strafe_pair(a, b)
            self._send_json({"ok": ok, "pair": list(ENGINE.strafe_pair)})
        elif url.path == "/settings":
            q = parse_qs(url.query)
            raw = getattr(self, "_body", b"") or b""
            try:
                body_obj = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body_obj = {}
            full_v = body_obj.get("full_mm", (q.get("full_mm") or [None])[0])
            theme_v = body_obj.get("theme", (q.get("theme") or [None])[0])
            span_v = body_obj.get("mouse_span", (q.get("mouse_span") or [None])[0])
            snap = ENGINE.update_settings(full_mm=full_v, theme=theme_v, mouse_span=span_v, save=True)
            self._send_json({"ok": True, "full_mm": snap["full_mm"],
                             "theme": snap["theme"], "mouse_span": snap["mouse_span"]})
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
                ENGINE.rec_mouse = []
                ENGINE.rec_t0 = None
                ENGINE.recent.clear()
                ENGINE.presses = 0
                ENGINE.last_press = None
                ENGINE._strafe_active.clear()
                ENGINE._strafe_overlap_at = None
                ENGINE._strafe_last_up = None
                ENGINE._strafe_log.clear()
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
        self._body = b""
        if length > 0:
            try:
                self._body = self.rfile.read(length)
            except Exception:
                self._body = b""
        self._handle("POST")

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        cursor = 0
        mcursor = ENGINE._mouse_abs
        try:
            while True:
                snap = ENGINE.snapshot(cursor, mcursor)
                cursor = snap["n"]
                mcursor = snap["m"]["n"]
                ENGINE.last_client = time.time()   # the page is still watching
                self.wfile.write(("data: " + json.dumps(snap, separators=(",", ":")) +
                                  "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.018)
        except Exception:
            return


LAYOUT = [
    ("Esc", 0, 0.0, 1), ("F1", 0, 1.3, 1), ("F2", 0, 2.3, 1), ("F3", 0, 3.3, 1),
    ("F4", 0, 4.3, 1), ("F5", 0, 5.55, 1), ("F6", 0, 6.55, 1), ("F7", 0, 7.55, 1),
    ("F8", 0, 8.55, 1), ("F9", 0, 9.8, 1), ("F10", 0, 10.8, 1), ("F11", 0, 11.8, 1),
    ("F12", 0, 12.8, 1), ("Del", 0, 14.2, 1), ("Home", 0, 15.35, 1),
    ("`", 1, 0.0, 1), ("1", 1, 1, 1), ("2", 1, 2, 1), ("3", 1, 3, 1), ("4", 1, 4, 1),
    ("5", 1, 5, 1), ("6", 1, 6, 1), ("7", 1, 7, 1), ("8", 1, 8, 1), ("9", 1, 9, 1),
    ("0", 1, 10, 1), ("-", 1, 11, 1), ("=", 1, 12, 1), ("Bksp", 1, 13, 2.15), ("PgUp", 1, 15.35, 1),
    ("Tab", 2, 0.0, 1.5), ("Q", 2, 1.5, 1), ("W", 2, 2.5, 1), ("E", 2, 3.5, 1),
    ("R", 2, 4.5, 1), ("T", 2, 5.5, 1), ("Y", 2, 6.5, 1), ("U", 2, 7.5, 1),
    ("I", 2, 8.5, 1), ("O", 2, 9.5, 1), ("P", 2, 10.5, 1), ("[", 2, 11.5, 1),
    ("]", 2, 12.5, 1), ("\\", 2, 13.5, 1.65), ("PgDn", 2, 15.35, 1),
    ("Caps", 3, 0.0, 1.75), ("A", 3, 1.75, 1), ("S", 3, 2.75, 1), ("D", 3, 3.75, 1),
    ("F", 3, 4.75, 1), ("G", 3, 5.75, 1), ("H", 3, 6.75, 1), ("J", 3, 7.75, 1),
    ("K", 3, 8.75, 1), ("L", 3, 9.75, 1), (";", 3, 10.75, 1), ("'", 3, 11.75, 1),
    ("Enter", 3, 12.75, 2.4), ("End", 3, 15.35, 1),
    ("LShift", 4, 0.0, 2.25), ("Z", 4, 2.25, 1), ("X", 4, 3.25, 1), ("C", 4, 4.25, 1),
    ("V", 4, 5.25, 1), ("B", 4, 6.25, 1), ("N", 4, 7.25, 1), ("M", 4, 8.25, 1),
    (",", 4, 9.25, 1), (".", 4, 10.25, 1), ("/", 4, 11.25, 1), ("RShift", 4, 12.25, 1.85),
    ("\u2191", 4, 14.25, 1),
    ("LCtrl", 5, 0.0, 1.25), ("LWin", 5, 1.25, 1.25), ("LAlt", 5, 2.5, 1.25),
    ("Space", 5, 3.75, 6.25), ("RAlt", 5, 10.0, 1.05), ("Fn", 5, 11.05, 1.05),
    ("RCtrl", 5, 12.1, 1.05), ("\u2190", 5, 13.25, 1), ("\u2193", 5, 14.25, 1),
    ("\u2192", 5, 15.25, 1),
]

COMMON_THEME_JS = r"""
let FULL = 3.50;
function setFullMm(v) {
  const n = parseFloat(v);
  if (!isNaN(n) && n >= 1.0 && n <= 5.0) FULL = Math.round(n * 100) / 100;
  return FULL;
}
const DISPLAY = { LCtrl:'Ctrl', RCtrl:'Ctrl', LAlt:'Alt', RAlt:'Alt', LShift:'Shift', RShift:'Shift', LWin:'Win', Bksp:'Back' };
const ALIAS = { Alt:'LAlt', Ctrl:'LCtrl', Shift:'LShift', Win:'LWin', Meta:'LWin', LMeta:'LWin', RMeta:'RWin', Backspace:'Bksp', Back:'Bksp' };
function norm(n) { return ALIAS[n] || n; }
function disp(n) { return DISPLAY[n] || n; }
function smoothRamp(r) {
  const stops = [[0.00,[56,189,248]],[0.36,[52,211,153]],[0.70,[251,191,36]],[1.00,[244,63,94]]];
  r = Math.max(0, Math.min(1, r));
  for (let i = 1; i < stops.length; i++) {
    if (r <= stops[i][0]) {
      const t = (r - stops[i-1][0]) / (stops[i][0] - stops[i-1][0]);
      const f = 0.5 - 0.5 * Math.cos(Math.PI * t);
      const a = stops[i-1][1], b = stops[i][1];
      return [Math.round(a[0]+(b[0]-a[0])*f), Math.round(a[1]+(b[1]-a[1])*f), Math.round(a[2]+(b[2]-a[2])*f)];
    }
  }
  return stops[stops.length - 1][1];
}
const FILL_PRESETS = {
  ramp:    { label:'随深度平滑', acc:'#38bdf8', style: r => {
    const [R,G,B] = smoothRamp(r), [R0,G0,B0] = smoothRamp(Math.max(0, r * 0.55));
    return { bg:`linear-gradient(to top, rgba(${R0},${G0},${B0},0.80), rgba(${R},${G},${B},0.56))`,
             line:`rgb(${Math.min(255,R+36)},${Math.min(255,G+36)},${Math.min(255,B+36)})`,
             acc:`rgb(${R},${G},${B})` };
  }},
  cyan:    { label:'冰川青', acc:'#38bdf8', style: () => ({ bg:'linear-gradient(to top, rgba(14,165,233,0.78), rgba(56,189,248,0.52))', line:'#7dd3fc', acc:'#38bdf8' }) },
  mint:    { label:'薄荷绿', acc:'#34d399', style: () => ({ bg:'linear-gradient(to top, rgba(16,185,129,0.78), rgba(52,211,153,0.50))', line:'#6ee7b7', acc:'#34d399' }) },
  amber:   { label:'琥珀金', acc:'#fbbf24', style: () => ({ bg:'linear-gradient(to top, rgba(217,119,6,0.80), rgba(251,191,36,0.52))',  line:'#fde68a', acc:'#fbbf24' }) },
  crimson: { label:'绯红',   acc:'#f43f5e', style: () => ({ bg:'linear-gradient(to top, rgba(225,29,72,0.82), rgba(244,63,94,0.54))',   line:'#fda4af', acc:'#f43f5e' }) },
  violet:  { label:'紫罗兰', acc:'#a855f7', style: () => ({ bg:'linear-gradient(to top, rgba(126,34,206,0.80), rgba(168,85,247,0.54))', line:'#d8b4fe', acc:'#a855f7' }) }
};
"""


def _layout_dicts():
    return [{"k": k, "r": r, "x": x, "w": w} for k, r, x, w in LAYOUT]


def page_html():
    return (PAGE
            .replace("__LAYOUT__", json.dumps(_layout_dicts(), ensure_ascii=False))
            .replace("__THEME_JS__", COMMON_THEME_JS))


def overlay_html():
    return (OVERLAY_PAGE
            .replace("__LAYOUT__", json.dumps(_layout_dicts(), ensure_ascii=False))
            .replace("__THEME_JS__", COMMON_THEME_JS))


def mouse_overlay_html():
    return MOUSE_OVERLAY_PAGE.replace("__THEME_JS__", COMMON_THEME_JS)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>磁轴深度监测</title>
<style>
  :root { --u: 46px; --bg:#101215; --fg:#f4f4f5; --dim:rgba(255,255,255,0.48); --acc:#38bdf8; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--fg);
    font-family:"Microsoft YaHei UI","Segoe UI",system-ui,sans-serif; overflow:hidden; }
  .num { font-variant-numeric:tabular-nums; font-family:ui-monospace,"Cascadia Mono",Consolas,monospace; }
  #app { height:100%; display:flex; flex-direction:column; padding:14px 24px 18px; }
  header { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:36px; z-index:12; }
  .hleft, .hright { display:flex; align-items:center; gap:10px; position:relative; }
  .dot { width:7px; height:7px; border-radius:50%; background:#34d399; transition:background .2s; }
  .dot.off { background:#52525b; }
  .brand { font-size:13px; font-weight:600; letter-spacing:.9px; color:rgba(255,255,255,.88); }
  .meta-btn { background:transparent; border:none; color:var(--dim); font-size:12px; padding:4px 7px;
              border-radius:6px; cursor:pointer; font-family:inherit; display:inline-flex; align-items:center; gap:5px; }
  .meta-btn:hover { color:#fff; background:rgba(255,255,255,.06); }
  .nav-sep { width:1px; height:12px; background:rgba(255,255,255,.12); margin:0 2px; }
  .nav-link { font-size:12px; color:var(--dim); padding:4px 9px; border-radius:6px; cursor:pointer;
              text-decoration:none; border:none; background:transparent; font-family:inherit; }
  .nav-link:hover { color:#fff; background:rgba(255,255,255,.06); }
  .nav-link.active { color:#fff; background:rgba(255,255,255,.10); }
  .rec-btn { background:rgba(239,68,68,.12); border:1px solid rgba(239,68,68,.38); color:#fca5a5;
             border-radius:7px; padding:5px 11px; font-size:12px; cursor:pointer; font-family:inherit; }
  .rec-btn:hover { background:rgba(239,68,68,.22); }
  .rec-btn.on { background:#dc2626; border-color:#ef4444; color:#fff; }
  .rec-btn.pulse { animation:recPulse .8s ease-in-out infinite; }
  @keyframes recPulse { 0%,100%{ box-shadow:0 0 0 0 rgba(239,68,68,.55); } 50%{ box-shadow:0 0 0 6px rgba(239,68,68,0); } }
  .auto-btn { background:transparent; border:1px solid rgba(255,255,255,.14); color:var(--dim);
              border-radius:7px; padding:5px 10px; font-size:12px; cursor:pointer; font-family:inherit; }
  .auto-btn:hover { color:#fff; border-color:rgba(255,255,255,.28); }
  .auto-btn.on { border-color:rgba(52,211,153,.55); color:#6ee7b7; background:rgba(16,185,129,.12); }

  .pop { position:absolute; top:38px; right:0; width:276px; background:rgba(18,20,25,.96);
         border:1px solid rgba(255,255,255,.12); border-radius:10px; padding:10px;
         backdrop-filter:blur(14px); box-shadow:0 20px 50px rgba(0,0,0,.65); display:none; z-index:40; }
  .pop.left { right:auto; left:0; width:268px; }
  .pop.bottom { top:auto; bottom:34px; right:auto; left:0; width:340px; }
  .pop.open { display:block; }
  .pop-sec { padding:6px 4px; border-bottom:1px solid rgba(255,255,255,.07); }
  .pop-sec:last-child { border-bottom:none; }
  .pop-lbl { font-size:11px; color:var(--dim); margin-bottom:6px; display:flex; justify-content:space-between; align-items:center; }
  .pop-row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .stat-grid { display:grid; grid-template-columns:1fr 1fr; gap:5px 12px; font-size:11px; color:var(--dim); }
  .stat-grid b { color:rgba(255,255,255,.90); font-weight:600; float:right; }
  .chip { background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.11); color:rgba(255,255,255,.78);
          border-radius:6px; padding:4px 8px; font-size:11px; cursor:pointer; display:inline-flex; align-items:center; gap:5px; font-family:inherit; }
  .chip:hover { background:rgba(255,255,255,.11); color:#fff; }
  .chip.on { border-color:rgba(255,255,255,.45); background:rgba(255,255,255,.14); color:#fff; }
  .chip .sw { width:8px; height:8px; border-radius:50%; }
  .menu-item { width:100%; text-align:left; background:transparent; border:none; color:rgba(255,255,255,.82);
               padding:6px 8px; border-radius:6px; font-size:12px; cursor:pointer; font-family:inherit;
               display:flex; justify-content:space-between; align-items:center; }
  .menu-item:hover { background:rgba(255,255,255,.08); color:#fff; }
  .menu-item.danger { color:#fca5a5; }
  .menu-item.danger:hover { background:rgba(239,68,68,.16); }
  .mini-sel, .mini-in { background:rgba(255,255,255,.06); color:#fff; border:1px solid rgba(255,255,255,.14);
                        border-radius:5px; padding:2px 6px; font-size:11px; font-family:inherit; }
  .mini-sel option { background:#14171f; color:#f4f4f5; }

  #kbc { flex:1; min-height:0; display:flex; align-items:center; justify-content:center; position:relative; }
  #kb { position:relative; width:calc(16.35 * var(--u)); height:calc(6 * (var(--u) + 5px) - 5px); }
  #kbOff { position:absolute; inset:0; display:none; align-items:center; justify-content:center;
           color:var(--dim); font-size:13px; cursor:pointer; }
  #kbc.off #kb { display:none; }
  #kbc.off #kbOff { display:flex; }

  .k { position:absolute; border-radius:calc(var(--u) * 0.125); overflow:hidden;
       background:rgba(14,16,20,.68); border:1px solid rgba(255,255,255,.13);
       backdrop-filter:blur(8px); cursor:pointer; user-select:none;
       transition:transform .05s linear, border-color .08s linear; }
  .k .fill { position:absolute; left:0; right:0; bottom:0; height:0%; pointer-events:none; }
  .k .lbl { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
            font-size:calc(var(--u) * 0.24); font-weight:600; color:rgba(255,255,255,.72);
            text-shadow:0 1px 2px rgba(0,0,0,.65); pointer-events:none; }
  .k.hot .lbl { color:#fff; text-shadow:0 1px 3px rgba(0,0,0,.8); }
  .k .mm { position:absolute; right:4px; bottom:3px; z-index:2; font-size:calc(var(--u) * 0.17);
           color:rgba(255,255,255,.9); text-shadow:0 1px 2px rgba(0,0,0,.75);
           opacity:0; pointer-events:none; }
  .k.hot .mm { opacity:1; }
  body.hide-mm .k .mm { display:none; }
  .k.dim { opacity:.42; }
  .k.held { border-bottom:2px solid rgba(255,255,255,.48); }
  .k.held .lbl { color:rgba(255,255,255,.92); }
  .k.binding { border-style:dashed; border-color:rgba(251,191,36,.95); opacity:1; }

  #hudBarWrap { display:flex; flex-wrap:wrap; align-items:flex-end; justify-content:space-between; gap:20px; padding-top:8px; }
  .hud-left { display:flex; align-items:flex-end; gap:22px; flex-wrap:wrap; }
  .cur-read { min-width:190px; }
  .cur-top { display:flex; align-items:baseline; gap:10px; }
  #curkey { font-size:24px; font-weight:700; min-width:30px; }
  #curmm { font-size:30px; font-weight:600; color:var(--acc); line-height:1; }
  #curmm.idle { color:rgba(255,255,255,.28); }
  .ruler { margin-top:8px; width:190px; height:3px; background:rgba(255,255,255,.10); position:relative; overflow:hidden; }
  .ruler .trig { position:absolute; top:0; bottom:0; width:1px; background:rgba(251,191,36,.75); left:11.4%; z-index:2; }
  .ruler .bar { height:100%; width:0%; background:var(--acc); transition:width .05s linear; }
  .metrics { display:flex; align-items:center; gap:16px; padding-left:20px; border-left:1px solid rgba(255,255,255,.10);
             font-size:12px; color:var(--dim); position:relative; flex-wrap:wrap; }
  .metrics b { color:rgba(255,255,255,.92); font-weight:600; margin-left:4px; }
  .scope-wrap { position:relative; background:rgba(255,255,255,.025); border:1px solid rgba(255,255,255,.08);
                border-radius:8px; padding:4px 6px; }
  #scope { display:block; width:340px; height:56px; }

  body.pure header, body.pure #hudBarWrap { opacity:0; pointer-events:none; }
  body.pure #pureExit { display:inline-flex; }
  #pureExit { position:fixed; right:16px; top:12px; display:none; z-index:30; opacity:.35; }
  #pureExit:hover { opacity:1; }

  #toast { position:fixed; left:50%; top:14px; transform:translateX(-50%);
           background:rgba(20,23,29,.94); border:1px solid rgba(255,255,255,.15); color:#f4f4f5;
           padding:7px 14px; border-radius:999px; font-size:12px; display:none; align-items:center; gap:10px; z-index:50;
           backdrop-filter:blur(10px); }
  #bigtip { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%);
            background:rgba(36,16,18,.95); border:1px solid rgba(239,68,68,.45); color:#fecaca;
            padding:20px 30px; border-radius:14px; font-size:14px; line-height:1.6;
            display:none; z-index:60; box-shadow:0 20px 60px rgba(0,0,0,.7); text-align:center; max-width:480px; }
  #bigtip b { color:#fff; font-size:16px; }
  #staleTip { display:none; position:fixed; left:50%; top:14px; transform:translateX(-50%);
              background:rgba(42,17,19,.95); border:1px solid #7f1d1d; color:#fecaca;
              padding:9px 16px; border-radius:10px; font-size:12px; z-index:55; }
  table { width:100%; border-collapse:collapse; font-size:11px; }
  th { text-align:left; color:var(--dim); font-weight:500; padding:4px; border-bottom:1px solid rgba(255,255,255,.08); }
  td { padding:4px; color:rgba(255,255,255,.86); }
</style></head>
<body>
<div id="app">
  <header>
    <div class="hleft">
      <span class="dot off" id="dot"></span>
      <span class="brand">FALCHION ACE 75 HE</span>
      <button class="meta-btn" id="statusBtn" type="button" onclick="togglePop('popStatus', event)">
        <span id="conn">连接中</span><span class="num" id="drMini">0/s</span><span>▾</span>
      </button>
      <div class="pop left" id="popStatus">
        <div class="pop-sec">
          <div class="pop-lbl"><span>采样质量</span><span class="num" id="ver">—</span></div>
          <div class="stat-grid">
            <div>实际<b class="num" id="q_rate">—</b></div>
            <div>平均<b class="num" id="q_mean">—</b></div>
            <div>P99<b class="num" id="q_p99">—</b></div>
            <div>最大<b class="num" id="q_max">—</b></div>
            <div>异常<b class="num" id="q_anom">0</b></div>
            <div>中断<b class="num" id="q_drop">0</b></div>
          </div>
        </div>
        <div class="pop-sec">
          <div class="pop-lbl"><span>数据流统计</span></div>
          <div class="stat-grid">
            <div>深度<b class="num" id="dr">0/s</b></div>
            <div>事件<b class="num" id="er">0/s</b></div>
            <div>按压<b class="num" id="pc">0</b></div>
            <div>按住<b class="num" id="heldList">—</b></div>
          </div>
        </div>
      </div>
      <span class="nav-sep"></span>
      <span class="nav-link active">监测</span>
      <a class="nav-link" href="/wb" target="_blank">素材工作台</a>
      <a class="nav-link" href="/overlay?panel=1" target="_blank">直播设备</a>
    </div>

    <div class="hright">
      <button id="recBtn" class="rec-btn" type="button" onclick="toggleRec(); this.blur()">● 录制</button>
      <button id="autoBtn" class="auto-btn" type="button" onclick="toggleAuto(); this.blur()">自动</button>
      <button class="meta-btn" id="lookBtn" type="button" onclick="togglePop('popLook', event)">外观 ▾</button>
      <button class="meta-btn" id="moreBtn" type="button" onclick="togglePop('popMore', event)">更多 ▾</button>

      <div class="pop" id="popLook">
        <div class="pop-sec">
          <div class="pop-lbl"><span>深度配色</span></div>
          <div class="pop-row" id="themeChips"></div>
        </div>
        <div class="pop-sec">
          <div class="pop-lbl"><span>满程行程 (mm)</span><span class="num" id="fullLbl">3.50 mm</span></div>
          <div class="pop-row">
            <button class="chip full-chip" data-full="3.5" type="button" onclick="applyFullMm(3.5)">3.50</button>
            <button class="chip full-chip" data-full="3.8" type="button" onclick="applyFullMm(3.8)">3.80</button>
            <button class="chip full-chip" data-full="4.0" type="button" onclick="applyFullMm(4.0)">4.00</button>
            <input id="fullIn" class="mini-in num" type="number" min="1.0" max="5.0" step="0.05" value="3.50" style="width:62px">
            <button class="chip" type="button" onclick="applyFullMm(document.getElementById('fullIn').value)">应用</button>
          </div>
        </div>
        <div class="pop-sec">
          <div class="pop-lbl"><span>显示开关</span></div>
          <div class="pop-row">
            <button id="kbToggle" class="chip on" type="button" onclick="toggleKb()">键盘渲染 (K)</button>
            <button id="mmToggle" class="chip on" type="button" onclick="toggleMm()">键帽 mm</button>
            <button id="pureBtn" class="chip" type="button" onclick="togglePure()">纯净 (P)</button>
          </div>
        </div>
      </div>

      <div class="pop" id="popMore">
        <div class="pop-sec">
          <div class="pop-lbl">
            <span>导出录制</span>
            <select id="rate" class="mini-sel" title="宽表采样率">
              <option value="100">100 Hz</option>
              <option value="50">50 Hz</option>
              <option value="25">25 Hz</option>
            </select>
          </div>
          <button class="menu-item" type="button" onclick="closePops(); downloadIfRecorded('/export/wide?rate=' + document.getElementById('rate').value)"><span>导出宽表 CSV</span><span style="color:var(--dim)">矩阵</span></button>
          <button class="menu-item" type="button" onclick="closePops(); downloadIfRecorded('/export/long')"><span>导出长表 CSV</span><span style="color:var(--dim)">原始</span></button>
          <button class="menu-item" type="button" onclick="closePops(); api('/openfolder')"><span>打开记录文件夹</span></button>
        </div>
        <div class="pop-sec">
          <div class="pop-lbl"><span>急停键对</span><span class="num" id="strafeDetail">—</span></div>
          <div class="pop-row">
            <input id="strafeA" class="mini-in" value="A" size="3" maxlength="8">
            <span style="color:var(--dim)">/</span>
            <input id="strafeB" class="mini-in" value="D" size="3" maxlength="8">
            <button class="chip" type="button" onclick="applyStrafe()">应用</button>
          </div>
        </div>
        <div class="pop-sec">
          <button class="menu-item" type="button" onclick="closePops(); showTip('左键点击键帽再按物理键绑定；右键点击键帽解除单个绑定', 3600)"><span>键位绑定说明</span></button>
          <button class="menu-item" type="button" onclick="closePops(); api('/resetmap').then(()=>location.reload())"><span>重新识别全部键位</span></button>
          <button class="menu-item" type="button" onclick="closePops(); api('/reset').then(()=>location.reload())"><span>清空统计与重录</span></button>
        </div>
        <div class="pop-sec">
          <button class="menu-item danger" type="button" onclick="closePops(); quitApp()"><span>退出程序</span><span style="font-size:11px">自动存盘</span></button>
        </div>
      </div>
    </div>
  </header>

  <div id="kbc">
    <div id="kb"></div>
    <div id="kbOff" onclick="toggleKb()">键盘渲染已关闭 · 点这里或按 K 键恢复</div>
  </div>

  <div id="hudBarWrap">
    <div class="hud-left">
      <div class="cur-read">
        <div class="cur-top">
          <div id="curkey">—</div>
          <div id="curmm" class="num idle">0.00</div>
        </div>
        <div class="ruler"><div class="trig"></div><div class="bar" id="curbar"></div></div>
      </div>
      <div class="metrics">
        <span>触发<b class="num" id="s_down">—</b></span>
        <span>复位<b class="num" id="s_up">—</b></span>
        <span>RT<b class="num" id="s_rt">—</b></span>
        <span>峰值<b class="num" id="s_peak">—</b></span>
        <span>上升<b class="num" id="s_rise">—</b></span>
        <span>急停<b class="num" id="s_strafe">—</b></span>
        <button class="meta-btn" type="button" onclick="togglePop('popRecent', event)">最近 ▾</button>
        <div class="pop bottom" id="popRecent">
          <div class="pop-lbl"><span>最近按压</span><span style="font-size:10px">Ctrl+Alt+R 录制 · S 存盘</span></div>
          <table>
            <thead><tr><th>键</th><th>峰值</th><th>上升</th><th>速度</th><th>RT</th></tr></thead>
            <tbody id="rec"></tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="scope-wrap">
      <canvas id="scope"></canvas>
    </div>
  </div>
</div>

<button id="pureExit" class="chip" type="button" onclick="togglePure(false)">退出纯净 (P)</button>
<div id="bigtip"></div>
<div id="toast"></div>
<div id="staleTip">数据流已断开（服务端没在推快照），按住状态已重置 —— 刷新页面或检查程序是否还在运行</div>

<script>
const LAYOUT = __LAYOUT__;
__THEME_JS__

let themeId = 'ramp';
let srvSynced = false;
try {
  const savedTheme = localStorage.getItem('depthTheme');
  if (savedTheme && FILL_PRESETS[savedTheme]) themeId = savedTheme;
  const savedFull = localStorage.getItem('depthFullMm');
  if (savedFull) setFullMm(savedFull);
} catch (e) {}
function activePreset() { return FILL_PRESETS[themeId] || FILL_PRESETS.ramp; }
function syncFullUI() {
  const lbl = document.getElementById('fullLbl');
  if (lbl) lbl.textContent = FULL.toFixed(2) + ' mm';
  const inp = document.getElementById('fullIn');
  if (inp && document.activeElement !== inp) inp.value = FULL.toFixed(2);
  document.querySelectorAll('.full-chip').forEach(b => {
    b.classList.toggle('on', Math.abs(parseFloat(b.dataset.full) - FULL) < 0.01);
  });
  const trig = document.querySelector('.ruler .trig');
  if (trig) trig.style.left = Math.min(95, (0.4 / FULL) * 100).toFixed(1) + '%';
}
function saveSettings(patch) {
  if (typeof api !== 'function') return;
  fetch('/settings', {
    method: 'POST',
    headers: { 'X-Depth-Token': window.__token || '' },
    body: JSON.stringify(patch)
  }).catch(() => {});
}
function applyFullMm(v, persist) {
  const prev = FULL;
  setFullMm(v);
  try { localStorage.setItem('depthFullMm', FULL.toFixed(2)); } catch (e) {}
  syncFullUI();
  if (typeof lastRatio === 'object') Object.keys(lastRatio).forEach(k => delete lastRatio[k]);
  if (persist !== false && Math.abs(FULL - prev) > 0.001) {
    saveSettings({ full_mm: FULL });
    if (typeof showTip === 'function') showTip('满程已设为 ' + FULL.toFixed(2) + ' mm');
  }
}
function setTheme(id, persist) {
  if (!FILL_PRESETS[id]) return;
  themeId = id;
  try { localStorage.setItem('depthTheme', id); } catch (e) {}
  document.documentElement.style.setProperty('--acc', activePreset().acc);
  const tc = document.getElementById('themeChips');
  if (tc) {
    for (const ch of tc.children) ch.classList.toggle('on', ch.dataset.theme === themeId);
  }
  if (typeof lastRatio === 'object') Object.keys(lastRatio).forEach(k => delete lastRatio[k]);
  if (persist !== false) saveSettings({ theme: id });
}
document.documentElement.style.setProperty('--acc', activePreset().acc);

const kb = document.getElementById('kb');
const els = {};
for (const k of LAYOUT) {
  const d = document.createElement('div');
  d.className = 'k';
  d.style.left = 'calc(' + k.x + ' * var(--u))';
  d.style.top = 'calc(' + k.r + ' * (var(--u) + 5px))';
  d.style.width = 'calc(' + k.w + ' * var(--u) - 5px)';
  d.style.height = 'calc(var(--u) - 5px)';
  d.innerHTML = '<div class="fill"></div><div class="lbl">' + disp(k.k) + '</div><div class="mm num"></div>';
  d.title = '左键点击绑定物理键 · 右键解除绑定';
  d.addEventListener('click', () => {
    api('/bind?key=' + encodeURIComponent(k.k));
    showTip('请按一下物理键：' + k.k);
  });
  d.addEventListener('contextmenu', e => {
    e.preventDefault();
    api('/unbind?key=' + encodeURIComponent(k.k))
      .then(r => r.json())
      .then(j => showTip(j.ok ? ('已解除绑定：' + k.k) : (k.k + ' 当前未绑定')));
  });
  kb.appendChild(d);
  els[k.k] = { box: d, fill: d.children[0], mm: d.children[2] };
}
let live = {}, idxName = {}, nameIdx = {}, shown = {}, stats = null, recent = [];
let rate = [0, 0], presses = 0, connected = false;
function color(r) { return activePreset().style(r).acc; }

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
let mmOn = true;
try { mmOn = localStorage.getItem('depthMmOn') !== '0'; } catch (e) {}
let pureMode = false;

function applyKbState() {
  const btn = document.getElementById('kbToggle');
  if (btn) btn.classList.toggle('on', kbOn);
  const kbc = document.getElementById('kbc');
  if (kbc) kbc.classList.toggle('off', !kbOn);
}
function applyMmState() {
  document.body.classList.toggle('hide-mm', !mmOn);
  const btn = document.getElementById('mmToggle');
  if (btn) btn.classList.toggle('on', mmOn);
}
function toggleMm() {
  mmOn = !mmOn;
  try { localStorage.setItem('depthMmOn', mmOn ? '1' : '0'); } catch (e) {}
  applyMmState();
}
function togglePure(force) {
  pureMode = (typeof force === 'boolean') ? force : !pureMode;
  document.body.classList.toggle('pure', pureMode);
  const btn = document.getElementById('pureBtn');
  if (btn) btn.classList.toggle('on', pureMode);
  closePops();
  fitUnits();
}
function closePops() {
  document.querySelectorAll('.pop.open').forEach(p => p.classList.remove('open'));
}
function togglePop(id, ev) {
  if (ev) ev.stopPropagation();
  const target = document.getElementById(id);
  if (!target) return;
  const wasOpen = target.classList.contains('open');
  closePops();
  if (!wasOpen) target.classList.add('open');
}
document.addEventListener('click', e => {
  if (!e.target.closest('.pop')) closePops();
});
function initLookControls() {
  const tc = document.getElementById('themeChips');
  if (tc && !tc.children.length) {
    for (const id in FILL_PRESETS) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (id === themeId ? ' on' : '');
      b.dataset.theme = id;
      b.innerHTML = '<span class="sw" style="background:' + FILL_PRESETS[id].acc + '"></span>' + FILL_PRESETS[id].label;
      b.addEventListener('click', () => setTheme(id));
      tc.appendChild(b);
    }
  }
  applyMmState();
}
function applyStrafe() {
  const a = (document.getElementById('strafeA').value || '').trim();
  const b = (document.getElementById('strafeB').value || '').trim();
  if (!a || !b || a === b) { showTip('请输入两个不同的按键名'); return; }
  api('/strafe?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b))
    .then(r => r.json())
    .then(j => {
      if (j.ok) showTip('急停键对已切换：' + j.pair.join(' / '));
      else showTip('无效的急停键对');
    });
}
function showTip(text, ms) {
  const t = document.getElementById('toast');
  t.textContent = text; t.style.display = 'flex';
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
let strafe = null;
const es = new EventSource('/events');
es.onmessage = ev => {
  lastMsg = Date.now();
  const m = JSON.parse(ev.data);
  if (m.live) for (const i in m.live) live[i] = m.live[i];
  if (m.s) {
    for (const p of m.s) live[p[1]] = p[2];
    if (typeof pushScopeSamples === 'function') pushScopeSamples(m.s);
  }
  if (m.k) {
    idxName = m.k; nameIdx = {};
    for (const i in m.k) if (m.k[i]) nameIdx[norm(m.k[i])] = i;
  }
  rate = m.rate || [0, 0]; presses = m.presses || 0; connected = !!m.conn;
  if (m.st) stats = m.st;
  if (m.rec) recent = m.rec;
  if (m.strafe) {
    strafe = m.strafe;
    const aEl = document.getElementById('strafeA'), bEl = document.getElementById('strafeB');
    if (strafe.pair && aEl && bEl && document.activeElement !== aEl && document.activeElement !== bEl) {
      aEl.value = strafe.pair[0] || 'A';
      bEl.value = strafe.pair[1] || 'D';
    }
  }
  if (m.held) {
    held = new Set(m.held.map(norm));
    const hl = document.getElementById('heldList');
    if (hl) hl.textContent = held.size ? Array.from(held).map(disp).join(' ') : '—';
  }
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
  if (!srvSynced) {
    srvSynced = true;
    if (m.full_mm) applyFullMm(m.full_mm, false);
    if (m.theme && FILL_PRESETS[m.theme]) setTheme(m.theme, false);
  } else {
    if (m.full_mm && Math.abs(m.full_mm - FULL) > 0.005) applyFullMm(m.full_mm, false);
    if (m.theme && m.theme !== themeId && FILL_PRESETS[m.theme]) setTheme(m.theme, false);
  }
  if (m.quality) {
    const q = m.quality;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set('q_rate', q.rate ? q.rate.toFixed(1) + '/s' : '—');
    set('q_mean', q.mean_ms ? q.mean_ms.toFixed(1) + ' ms' : '—');
    set('q_p99', q.p99_ms ? q.p99_ms.toFixed(1) + ' ms' : '—');
    set('q_max', q.max_ms ? q.max_ms.toFixed(1) + ' ms' : '—');
    set('q_anom', q.anomalies);
    set('q_drop', q.dropouts);
  }
  if (m.version) {
    const vEl = document.getElementById('ver');
    if (vEl) vEl.textContent = 'v' + m.version;
  }
  autoMode = !!m.auto;
  const ab = document.getElementById('autoBtn');
  if (ab) ab.classList.toggle('on', autoMode);
  rec.on = !!m.recording; rec.n = m.rec_n || 0; rec.dur = m.rec_dur || 0;
};

function fmt(v, unit) { return v === null || v === undefined ? '—' : (v + unit); }

function renderPanel() {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  let best = 0, bestName = '—';
  for (const i in live) if (live[i] > best) { best = live[i]; bestName = idxName[i] || ('#' + i); }
  set('curkey', bestName === '—' ? '—' : disp(norm(bestName)));
  const cm = document.getElementById('curmm');
  const cb = document.getElementById('curbar');
  const rBest = Math.max(0, Math.min(1, best / FULL));
  const stBest = activePreset().style(rBest);
  if (cm) {
    cm.textContent = best.toFixed(2);
    cm.className = best > 0.03 ? 'num' : 'num idle';
    cm.style.color = best > 0.03 ? stBest.acc : '';
  }
  if (cb) {
    cb.style.width = (rBest * 100).toFixed(1) + '%';
    cb.style.background = stBest.acc;
  }
  const dot = document.getElementById('dot');
  if (dot) dot.className = connected ? 'dot' : 'dot off';
  set('conn', connected ? '已连接' : '等待设备');
  set('drMini', Math.round(rate[0]) + '/s');
  set('dr', rate[0] + '/s');
  set('er', rate[1] + '/s');
  set('pc', presses);

  const rb = document.getElementById('recBtn');
  if (rb) {
    if (rec.on) {
      rb.textContent = '■ 停止 ' + rec.dur.toFixed(1) + 's · ' + rec.n;
      rb.classList.add('on');
    } else {
      rb.textContent = rec.n > 0 ? ('● 录制 (' + rec.n + ')') : '● 录制';
      rb.classList.remove('on');
    }
  }

  const s = stats || {};
  set('s_down', fmt(s.down, ' mm'));
  set('s_up', fmt(s.up, ' mm'));
  set('s_rt', fmt(s.rt, ' mm'));
  set('s_peak', fmt(s.peak, ' mm'));
  set('s_rise', fmt(s.rise, ' ms'));
  if (strafe && strafe.n > 0 && strafe.clean_pct !== null) {
    set('s_strafe', strafe.clean_pct + '%');
    set('strafeDetail', strafe.pair.join('/') + ' · 均值 ' + strafe.avg_overlap_ms + 'ms');
  } else {
    set('s_strafe', '—');
    set('strafeDetail', strafe && strafe.pair ? strafe.pair.join('/') : '—');
  }
  const recEl = document.getElementById('rec');
  if (recEl) {
    recEl.innerHTML = recent.map(r =>
      '<tr><td>' + disp(norm(r[0])) + '</td><td class="num">' + r[1].toFixed(2) + '</td><td class="num">' + r[2] +
      ' ms</td><td class="num">' + r[3] + '</td><td class="num">' + (r[4] === null ? '—' : r[4].toFixed(2)) + '</td></tr>'
    ).join('') || '<tr><td colspan="5" style="color:var(--dim);text-align:center;padding:8px">暂无按压记录</td></tr>';
  }
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
  const preset = activePreset();
  const hasMapped = Object.keys(nameIdx).length > 0;

  for (const name in els) {
    const idx = nameIdx[name];
    const mm = (!stale && idx !== undefined) ? (live[idx] || 0) : 0;
    const r = Math.max(0, Math.min(1, mm / FULL));
    const prev = lastRatio[name];
    const isHeld = heldNow.has(name);
    const wasHeld = lastHeld[name] === true;
    const isBind = binding === name;
    const el = els[name];
    el.box.classList.toggle('dim', hasMapped && idx === undefined && !isBind);
    if (prev !== undefined && Math.abs(prev - r) < 0.003 &&
        isHeld === wasHeld && !isBind && !el.box.classList.contains('binding')) {
      continue;
    }
    lastRatio[name] = r;
    lastHeld[name] = isHeld;
    const hot = r > 0.06;
    const active = r > 0.04;
    const st = preset.style(r);
    el.fill.style.height = (r * 100).toFixed(1) + '%';
    el.fill.style.background = active ? st.bg : 'transparent';
    el.fill.style.borderTop = active ? ('1.5px solid ' + st.line) : 'none';
    el.box.classList.toggle('hot', hot);
    el.box.classList.toggle('held', isHeld && !hot);
    el.box.classList.toggle('binding', isBind);
    el.box.style.transform = active ? ('scale(' + (1 - r * 0.035).toFixed(3) + ')') : '';
    if (el.mm) el.mm.textContent = r > 0.08 ? mm.toFixed(1) : '';
  }
  requestAnimationFrame(draw);
}
startDraw();
setInterval(renderPanel, 90);
// Space / Enter would "click" whatever button still has focus from the last
// click (e.g. 开始录制), so pressing Space stopped the recording instead of
// being recorded as a key. Swallow those activations on the page.
addEventListener('keydown', e => {
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (e.code === 'Escape') {
    if (document.querySelector('.pop.open')) closePops();
    else if (pureMode) togglePure(false);
    return;
  }
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  if ((e.code === 'Space' || e.code === 'Enter') &&
      (tag === 'BUTTON' || tag === 'SELECT')) {
    e.preventDefault();
  }
  if (!e.ctrlKey && !e.altKey && !e.metaKey) {
    if (e.code === 'KeyK') toggleKb();
    else if (e.code === 'KeyP') togglePure();
  }
}, true);

const SCOPE_WIN = 4.0;
const SCOPE_COLORS = ['#38bdf8', '#34d399', '#fbbf24', '#f472b6'];
const scopeCv = document.getElementById('scope');
const scopeCtx = scopeCv ? scopeCv.getContext('2d') : null;
const scopeSeries = {};
let scopeW = 340, scopeH = 56, scopeOff = null, scopeRunning = false;

function fitScope() {
  if (!scopeCv || !scopeCtx) return;
  const dpr = window.devicePixelRatio || 1;
  const r = scopeCv.getBoundingClientRect();
  scopeW = Math.max(160, Math.round(r.width || 340));
  scopeH = Math.max(40, Math.round(r.height || 56));
  scopeCv.width = Math.round(scopeW * dpr);
  scopeCv.height = Math.round(scopeH * dpr);
  scopeCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  renderScopeFrame();
}

function pushScopeSamples(batch) {
  if (!batch || !batch.length) return;
  const nowS = performance.now() / 1000;
  for (const p of batch) {
    const st = p[0], idx = String(p[1]), mm = p[2];
    if (scopeOff === null || Math.abs((st + scopeOff) - nowS) > 0.25) scopeOff = nowS - st;
    const t = st + scopeOff;
    let arr = scopeSeries[idx];
    if (!arr) arr = scopeSeries[idx] = [];
    arr.push([t, mm]);
  }
  if (!scopeRunning) {
    scopeRunning = true;
    requestAnimationFrame(scopeLoop);
  }
}

function renderScopeFrame() {
  if (!scopeCtx) return false;
  const w = scopeW, h = scopeH, padT = 11, padB = 4, plotH = h - padT - padB;
  const nowS = performance.now() / 1000, t0 = nowS - SCOPE_WIN;
  scopeCtx.clearRect(0, 0, w, h);

  scopeCtx.strokeStyle = 'rgba(255,255,255,0.06)';
  scopeCtx.lineWidth = 1;
  for (const f of [0, 0.5, 1]) {
    const y = Math.round(padT + plotH * (1 - f)) + 0.5;
    scopeCtx.beginPath(); scopeCtx.moveTo(0, y); scopeCtx.lineTo(w, y); scopeCtx.stroke();
  }
  const yTrig = Math.round(padT + plotH * (1 - 0.4 / FULL)) + 0.5;
  scopeCtx.save();
  scopeCtx.setLineDash([3, 3]);
  scopeCtx.strokeStyle = 'rgba(251,191,36,0.32)';
  scopeCtx.beginPath(); scopeCtx.moveTo(0, yTrig); scopeCtx.lineTo(w, yTrig); scopeCtx.stroke();
  scopeCtx.restore();

  const active = [];
  for (const idx in scopeSeries) {
    const arr = scopeSeries[idx];
    while (arr.length > 1 && arr[1][0] < t0 - 0.2) arr.shift();
    if (!arr.length || (arr[arr.length - 1][0] < t0 && (live[idx] || 0) <= 0.02)) {
      delete scopeSeries[idx];
      continue;
    }
    active.push([arr[arr.length - 1][0], idx, arr]);
  }
  active.sort((a, b) => b[0] - a[0]);
  const top = active.slice(0, 4);

  let lx = 4;
  scopeCtx.font = '10px ui-monospace, Consolas, monospace';
  top.forEach((entry, ci) => {
    const idx = entry[1], arr = entry[2], col = SCOPE_COLORS[ci % SCOPE_COLORS.length];
    scopeCtx.strokeStyle = col;
    scopeCtx.lineWidth = 1.6;
    scopeCtx.beginPath();
    let started = false, lastY = padT + plotH;
    for (let i = 0; i < arr.length; i++) {
      const x = ((arr[i][0] - t0) / SCOPE_WIN) * w;
      const y = padT + plotH * (1 - Math.max(0, Math.min(1, arr[i][1] / FULL)));
      if (!started) { scopeCtx.moveTo(x, y); started = true; } else scopeCtx.lineTo(x, y);
      lastY = y;
    }
    const curMm = live[idx] || 0;
    if (started && curMm > 0.02) {
      const cy = padT + plotH * (1 - Math.max(0, Math.min(1, curMm / FULL)));
      scopeCtx.lineTo(w, cy);
      lastY = cy;
    }
    scopeCtx.stroke();
    if (curMm > 0.02) {
      scopeCtx.fillStyle = col;
      scopeCtx.beginPath(); scopeCtx.arc(w - 2, lastY, 2.3, 0, Math.PI * 2); scopeCtx.fill();
    }
    const nm = idxName[idx] ? disp(norm(idxName[idx])) : ('#' + idx);
    scopeCtx.fillStyle = col;
    scopeCtx.fillText(nm, lx, 9);
    lx += scopeCtx.measureText(nm).width + 8;
  });

  scopeCtx.fillStyle = 'rgba(255,255,255,0.32)';
  scopeCtx.fillText('4s', w - 16, 9);
  return top.length > 0;
}

function scopeLoop() {
  if (renderScopeFrame()) requestAnimationFrame(scopeLoop);
  else scopeRunning = false;
}

function fitUnits() {
  const box = document.getElementById('kbc');
  if (!box) return;
  const uW = (box.clientWidth - 28) / 16.4;
  const uH = (box.clientHeight - 20) / 6.6;
  const u = Math.max(24, Math.min(72, Math.floor(Math.min(uW, uH))));
  document.documentElement.style.setProperty('--u', u + 'px');
}
addEventListener('resize', () => { fitUnits(); fitScope(); });
initLookControls();
syncFullUI();
applyKbState();
fitUnits();
fitScope();
</script>
</body></html>
"""


OVERLAY_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>磁轴深度 OBS 覆盖层</title>
<style>
  :root { --u: 58px; --gap: 6px; --op: 0.85; }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; width: 100%; height: 100%;
    background: transparent !important; overflow: hidden;
    color: #f4f4f5;
    font-family: "Microsoft YaHei UI", "Segoe UI", system-ui, sans-serif;
  }
  body.checker {
    background:
      linear-gradient(45deg, #1b1e26 25%, transparent 25%, transparent 75%, #1b1e26 75%),
      linear-gradient(45deg, #1b1e26 25%, #12141a 25%, #12141a 75%, #1b1e26 75%) !important;
    background-size: 24px 24px !important;
    background-position: 0 0, 12px 12px !important;
  }
  #stage {
    width: 100%; height: 100%; padding: 18px;
    display: flex; align-items: center; justify-content: center;
  }
  #stage.align-tl { align-items: flex-start; justify-content: flex-start; }
  #stage.align-tr { align-items: flex-start; justify-content: flex-end; }
  #stage.align-bl { align-items: flex-end; justify-content: flex-start; }
  #stage.align-br { align-items: flex-end; justify-content: flex-end; }

  #kb { position: relative; }
  .k {
    position: absolute;
    border-radius: calc(var(--u) * 0.125);
    overflow: hidden;
    background: rgba(14, 16, 20, var(--op));
    border: 1px solid rgba(255, 255, 255, 0.13);
    backdrop-filter: blur(8px);
    user-select: none;
    will-change: transform;
    transition: transform 0.05s linear, border-color 0.08s linear;
  }
  .k .fill {
    position: absolute; left: 0; right: 0; bottom: 0; height: 0%;
    pointer-events: none;
  }
  .k .lbl {
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: calc(var(--u) * 0.24); font-weight: 600;
    color: rgba(255, 255, 255, 0.72);
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.65);
    pointer-events: none;
  }
  .k.hot .lbl { color: #ffffff; text-shadow: 0 1px 3px rgba(0, 0, 0, 0.8); }
  .k.held { border-bottom: 2px solid rgba(255, 255, 255, 0.48); }
  .k.held .lbl { color: rgba(255, 255, 255, 0.92); }
  .k .mm {
    position: absolute; right: 5px; bottom: 4px; z-index: 2;
    font-size: calc(var(--u) * 0.17); font-weight: 600; line-height: 1;
    font-variant-numeric: tabular-nums;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    color: rgba(255, 255, 255, 0.9);
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.75);
    opacity: 0; pointer-events: none;
  }
  body.mm-0 .k .mm { display: none; }
  body.mm-1 .k.hot .mm { opacity: 1; }
  body.mm-always .k .mm { opacity: 0.62; }
  body.mm-always .k.hot .mm { opacity: 1; }

  #panel {
    position: fixed; top: 14px; right: 14px; width: 296px;
    background: rgba(16, 18, 24, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 12px; padding: 12px 14px;
    backdrop-filter: blur(12px);
    box-shadow: 0 18px 46px rgba(0, 0, 0, 0.65);
    font-size: 12px; display: none; z-index: 30;
  }
  #panel.open { display: block; }
  .p-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .p-head b { font-size: 13px; letter-spacing: 0.5px; }
  .p-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
  .p-row label { color: rgba(255, 255, 255, 0.62); flex-shrink: 0; }
  .p-row select, .p-row input[type=text] {
    flex: 1; min-width: 0;
    background: rgba(255, 255, 255, 0.07); color: #f4f4f5;
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 6px; padding: 4px 7px; font-size: 12px; font-family: inherit;
  }
  .p-row select option { background: #14171f; color: #f4f4f5; }
  .p-row input[type=range] { flex: 1; accent-color: #38bdf8; }
  .p-val { width: 38px; text-align: right; color: rgba(255, 255, 255, 0.85);
           font-variant-numeric: tabular-nums; font-family: ui-monospace, Consolas, monospace; }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; margin: 4px 0 10px; }
  .chip {
    background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12);
    color: rgba(255, 255, 255, 0.80); border-radius: 6px; padding: 3px 8px;
    font-size: 11px; cursor: pointer; display: inline-flex; align-items: center; gap: 5px;
    font-family: inherit;
  }
  .chip:hover { background: rgba(255, 255, 255, 0.12); color: #fff; }
  .chip.on { border-color: rgba(255, 255, 255, 0.55); background: rgba(255, 255, 255, 0.16); color: #fff; }
  .chip .sw { width: 8px; height: 8px; border-radius: 50%; }
  .p-url {
    width: 100%; background: rgba(0, 0, 0, 0.35); color: #93c5fd;
    border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px;
    padding: 5px 7px; font-size: 11px; font-family: ui-monospace, Consolas, monospace;
    margin-bottom: 8px;
  }
  .p-actions { display: flex; gap: 6px; }
  .p-btn {
    flex: 1; background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.15);
    color: #f4f4f5; border-radius: 7px; padding: 6px 8px; font-size: 12px; cursor: pointer;
    font-family: inherit;
  }
  .p-btn:hover { background: rgba(255, 255, 255, 0.15); }
  .p-btn.primary { background: rgba(56, 189, 248, 0.22); border-color: rgba(56, 189, 248, 0.55); color: #e0f2fe; }
  .p-btn.primary:hover { background: rgba(56, 189, 248, 0.34); }
  .p-tabs { display: flex; gap: 6px; margin-bottom: 10px; padding: 3px;
            background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; }
  .p-tab { flex: 1; text-align: center; text-decoration: none; padding: 5px 8px; border-radius: 6px;
           font-size: 12px; color: rgba(255, 255, 255, 0.62); transition: background .15s, color .15s; }
  .p-tab:hover { color: #fff; background: rgba(255, 255, 255, 0.06); }
  .p-tab.on { color: #fff; background: rgba(56, 189, 248, 0.22); font-weight: 600; }
  .p-hint { color: rgba(255, 255, 255, 0.42); font-size: 11px; margin-top: 8px; line-height: 1.4; }
</style></head>
<body>
<div id="stage"><div id="kb"></div></div>

<div id="panel">
  <div class="p-head">
    <b>直播设备配置</b>
    <button class="chip" id="pClose" type="button">关闭 (C)</button>
  </div>
  <div class="p-tabs">
    <a class="p-tab on" href="/overlay?panel=1">键盘 HUD</a>
    <a class="p-tab" href="/overlay/mouse?panel=1">鼠标垫追踪</a>
  </div>
  <div class="p-row">
    <label for="pCluster">键簇</label>
    <select id="pCluster">
      <option value="hud">HUD 紧凑键簇</option>
      <option value="wasd">WASD 极简</option>
      <option value="full">75% 整键盘</option>
    </select>
  </div>
  <div class="p-row">
    <label for="pKeys">自定义键</label>
    <input id="pKeys" type="text" placeholder="留空用预设，如 Tab,W,A,S,D,Shift,Alt,Space">
    <button id="pCap" class="chip" type="button" title="点击后直接按键盘按键（含 Tab/Shift/Alt）加入列表" style="flex-shrink:0">按键录入</button>
  </div>
  <div class="chips" id="pQuickKeys" style="margin-top:-2px"></div>
  <div class="p-row" style="margin-bottom:4px"><label>主题</label></div>
  <div class="chips" id="pThemes"></div>
  <div class="p-row">
    <label for="pScale">缩放</label>
    <input id="pScale" type="range" min="0.4" max="3.0" step="0.05" value="1">
    <span class="p-val" id="pScaleVal">1.00</span>
  </div>
  <div class="p-row">
    <label for="pOpacity">透明度</label>
    <input id="pOpacity" type="range" min="0.1" max="1.0" step="0.05" value="0.85">
    <span class="p-val" id="pOpacityVal">0.85</span>
  </div>
  <div class="p-row">
    <label for="pFull">满程 mm</label>
    <input id="pFull" type="range" min="1.5" max="4.5" step="0.05" value="3.5">
    <span class="p-val" id="pFullVal">3.50</span>
  </div>
  <div class="p-row">
    <label for="pMm">mm 读数</label>
    <select id="pMm">
      <option value="1">按下显示</option>
      <option value="always">常驻显示</option>
      <option value="0">隐藏</option>
    </select>
  </div>
  <div class="p-row">
    <label for="pAlign">对齐</label>
    <select id="pAlign">
      <option value="center">居中</option>
      <option value="tl">左上</option>
      <option value="tr">右上</option>
      <option value="bl">左下</option>
      <option value="br">右下</option>
    </select>
  </div>
  <div class="p-row">
    <label for="pChecker">预览透明格</label>
    <input id="pChecker" type="checkbox">
  </div>
  <input id="pUrl" class="p-url" type="text" readonly>
  <div class="p-actions">
    <button id="pCopy" class="p-btn primary" type="button">复制 OBS 链接</button>
    <button id="pReset" class="p-btn" type="button">恢复默认</button>
  </div>
  <div class="p-hint">OBS 添加「浏览器」源并粘贴上方链接即可透明叠加；浏览器中按 C 键可切换此面板。</div>
</div>

<script>
const FULL_LAYOUT = __LAYOUT__;
__THEME_JS__

const CLUSTERS = {
  hud: [
    {k:'Q', r:0, x:0.0, w:1.0}, {k:'W', r:0, x:1.0, w:1.0}, {k:'E', r:0, x:2.0, w:1.0}, {k:'R', r:0, x:3.0, w:1.0},
    {k:'A', r:1, x:0.0, w:1.0}, {k:'S', r:1, x:1.0, w:1.0}, {k:'D', r:1, x:2.0, w:1.0},
    {k:'LShift', r:2, x:0.0, w:1.35}, {k:'LCtrl', r:2, x:1.35, w:1.2}, {k:'LAlt', r:2, x:2.55, w:1.15}, {k:'Space', r:2, x:3.7, w:2.6}
  ],
  wasd: [
    {k:'W', r:0, x:1.0, w:1.0},
    {k:'A', r:1, x:0.0, w:1.0}, {k:'S', r:1, x:1.0, w:1.0}, {k:'D', r:1, x:2.0, w:1.0},
    {k:'LAlt', r:2, x:0.0, w:1.25}, {k:'Space', r:2, x:1.25, w:2.75}
  ],
  full: FULL_LAYOUT
};

const LOWER_KEY_MAP = {};
for (const item of FULL_LAYOUT) LOWER_KEY_MAP[item.k.toLowerCase()] = item.k;
for (const alias in ALIAS) LOWER_KEY_MAP[alias.toLowerCase()] = ALIAS[alias];
const EXTRA_ALIASES = {
  tap:'Tab', '制表':'Tab', '制表键':'Tab',
  '上档':'LShift', '上档键':'LShift', '左shift':'LShift', '右shift':'RShift', '右上档':'RShift',
  '左alt':'LAlt', '右alt':'RAlt', '换档':'LAlt',
  control:'LCtrl', '控制':'LCtrl', '左ctrl':'LCtrl', '右ctrl':'RCtrl',
  '空格':'Space', '空格键':'Space', spacebar:'Space',
  capslock:'Caps', '大写':'Caps', '大小写':'Caps',
  '回车':'Enter', return:'Enter',
  escape:'Esc', '退出':'Esc',
  '退格':'Bksp', delete:'Del', super:'LWin',
  up:'\u2191', down:'\u2193', left:'\u2190', right:'\u2192'
};
for (const k in EXTRA_ALIASES) LOWER_KEY_MAP[k] = EXTRA_ALIASES[k];

function parseCustomKeys(raw) {
  const out = [], seen = new Set();
  for (const part of String(raw || '').split(/[\s,，、;；+]+/)) {
    const s = part.trim();
    if (!s) continue;
    const mapped = LOWER_KEY_MAP[s.toLowerCase()] || norm(s);
    if (FULL_LAYOUT.some(it => it.k === mapped) && !seen.has(mapped)) {
      seen.add(mapped);
      out.push(mapped);
    }
  }
  return out;
}

function clamp(v, lo, hi, def) {
  const n = parseFloat(v);
  return isNaN(n) ? def : Math.max(lo, Math.min(hi, n));
}

function readConfig() {
  const q = new URLSearchParams(location.search);
  const rawCluster = (q.get('cluster') || q.get('layout') || 'hud').toLowerCase();
  const cluster = CLUSTERS[rawCluster] ? rawCluster : 'hud';
  const hasThemeQ = q.has('theme');
  const rawTheme = (q.get('theme') || 'ramp').toLowerCase();
  const theme = FILL_PRESETS[rawTheme] ? rawTheme : 'ramp';
  const hasFullQ = q.has('full');
  const full = clamp(q.get('full'), 1.0, 5.0, FULL);
  const rawMm = (q.get('mm') || '1').toLowerCase();
  const mm = (rawMm === '0' || rawMm === 'false' || rawMm === 'off') ? '0'
           : (rawMm === 'always' || rawMm === '2') ? 'always' : '1';
  const rawAlign = (q.get('align') || 'center').toLowerCase();
  const align = ['center','tl','tr','bl','br'].indexOf(rawAlign) >= 0 ? rawAlign : 'center';
  return {
    cluster,
    keys: (q.get('keys') || '').trim(),
    theme,
    full,
    hasThemeQ,
    hasFullQ,
    scale: clamp(q.get('scale'), 0.4, 3.0, 1.0),
    opacity: clamp(q.get('opacity') !== null ? q.get('opacity') : q.get('alpha'), 0.1, 1.0, 0.85),
    mm,
    align,
    panel: q.get('panel') === '1'
  };
}

let cfg = readConfig();
const kb = document.getElementById('kb');
const stage = document.getElementById('stage');
let els = {}, lastRatio = {}, lastHeld = {};

function resolveItems() {
  if (cfg.keys) {
    const wanted = new Set(parseCustomKeys(cfg.keys));
    const picked = FULL_LAYOUT.filter(item => wanted.has(item.k));
    if (picked.length) {
      let minX = Infinity, minR = Infinity;
      for (const it of picked) {
        if (it.x < minX) minX = it.x;
        if (it.r < minR) minR = it.r;
      }
      return picked.map(it => ({ k: it.k, r: it.r - minR, x: it.x - minX, w: it.w }));
    }
  }
  return CLUSTERS[cfg.cluster] || CLUSTERS.hud;
}

function buildLayout() {
  const items = resolveItems();
  const baseU = cfg.cluster === 'full' && !cfg.keys ? 44 : 58;
  const u = Math.round(baseU * cfg.scale);
  const gap = Math.max(4, Math.round(u * 0.11));
  document.documentElement.style.setProperty('--u', u + 'px');
  document.documentElement.style.setProperty('--gap', gap + 'px');
  document.documentElement.style.setProperty('--op', cfg.opacity.toFixed(2));

  let maxX = 0, maxR = 0;
  for (const it of items) {
    if (it.x + it.w > maxX) maxX = it.x + it.w;
    if (it.r > maxR) maxR = it.r;
  }
  kb.style.width = Math.max(1, Math.round(maxX * u - gap)) + 'px';
  kb.style.height = Math.max(1, Math.round((maxR + 1) * (u + gap) - gap)) + 'px';

  stage.className = 'align-' + cfg.align;
  document.body.classList.remove('mm-0', 'mm-1', 'mm-always');
  document.body.classList.add('mm-' + cfg.mm);

  kb.innerHTML = '';
  els = {}; lastRatio = {}; lastHeld = {};
  for (const it of items) {
    const d = document.createElement('div');
    d.className = 'k';
    d.style.left = Math.round(it.x * u) + 'px';
    d.style.top = Math.round(it.r * (u + gap)) + 'px';
    d.style.width = Math.max(12, Math.round(it.w * u - gap)) + 'px';
    d.style.height = Math.max(12, Math.round(u - gap)) + 'px';
    const fill = document.createElement('div');
    fill.className = 'fill';
    const lbl = document.createElement('div');
    lbl.className = 'lbl';
    lbl.textContent = disp(it.k);
    const mm = document.createElement('div');
    mm.className = 'mm';
    mm.textContent = '0.00';
    d.appendChild(fill);
    d.appendChild(lbl);
    d.appendChild(mm);
    kb.appendChild(d);
    els[it.k] = { box: d, fill, mm };
  }
}

function buildObsUrl(includePanel) {
  const p = new URLSearchParams();
  if (cfg.cluster !== 'hud') p.set('cluster', cfg.cluster);
  if (cfg.keys) p.set('keys', cfg.keys);
  if (cfg.theme !== 'ramp') p.set('theme', cfg.theme);
  if (Math.abs(cfg.full - 3.5) > 0.005) p.set('full', cfg.full.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
  if (Math.abs(cfg.scale - 1.0) > 0.001) p.set('scale', cfg.scale.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
  if (Math.abs(cfg.opacity - 0.85) > 0.001) p.set('opacity', cfg.opacity.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
  if (cfg.mm !== '1') p.set('mm', cfg.mm);
  if (cfg.align !== 'center') p.set('align', cfg.align);
  if (includePanel) p.set('panel', '1');
  const qs = p.toString();
  return location.origin + '/overlay' + (qs ? ('?' + qs) : '');
}

const panelEl = document.getElementById('panel');
const pThemes = document.getElementById('pThemes');
for (const id in FILL_PRESETS) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chip';
  b.dataset.theme = id;
  b.innerHTML = '<span class="sw" style="background:' + FILL_PRESETS[id].acc + '"></span>' + FILL_PRESETS[id].label;
  b.addEventListener('click', () => { cfg.theme = id; cfg.hasThemeQ = true; applyConfig(); });
  pThemes.appendChild(b);
}

const QUICK_KEYS = [
  ['Tab', 'Tab'], ['Caps', 'Caps'], ['LShift', 'Shift'], ['LCtrl', 'Ctrl'],
  ['LAlt', 'Alt'], ['Space', 'Space'], ['Esc', 'Esc'], ['Enter', 'Enter']
];
const pQuickKeys = document.getElementById('pQuickKeys');
const pKeysEl = document.getElementById('pKeys');
const pCapBtn = document.getElementById('pCap');
let capturingKeys = false;

function toggleCustomKey(k, forceAdd) {
  let list = cfg.keys ? parseCustomKeys(cfg.keys) : (CLUSTERS[cfg.cluster] || CLUSTERS.hud).map(it => it.k);
  const idx = list.indexOf(k);
  if (idx >= 0) {
    if (!forceAdd) list.splice(idx, 1);
  } else {
    list.push(k);
  }
  cfg.keys = list.join(',');
  pKeysEl.value = cfg.keys;
  applyConfig();
}

for (const [k, label] of QUICK_KEYS) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chip';
  b.dataset.qk = k;
  b.textContent = '+ ' + label;
  b.addEventListener('click', () => toggleCustomKey(k, false));
  pQuickKeys.appendChild(b);
}

const CODE_KEY_MAP = {
  Tab:'Tab', CapsLock:'Caps', ShiftLeft:'LShift', ShiftRight:'RShift',
  ControlLeft:'LCtrl', ControlRight:'RCtrl', AltLeft:'LAlt', AltRight:'RAlt',
  Space:'Space', Escape:'Esc', Enter:'Enter', Backspace:'Bksp', Delete:'Del',
  MetaLeft:'LWin', MetaRight:'RWin', Home:'Home', End:'End', PageUp:'PgUp', PageDown:'PgDn',
  ArrowUp:'\u2191', ArrowDown:'\u2193', ArrowLeft:'\u2190', ArrowRight:'\u2192',
  Backquote:'`', Minus:'-', Equal:'=', BracketLeft:'[', BracketRight:']',
  Backslash:'\\', Semicolon:';', Quote:"'", Comma:',', Period:'.', Slash:'/'
};
function eventToLayoutKey(e) {
  if (CODE_KEY_MAP[e.code]) return CODE_KEY_MAP[e.code];
  if (/^Key([A-Z])$/.test(e.code)) return e.code.slice(3);
  if (/^Digit([0-9])$/.test(e.code)) return e.code.slice(5);
  if (/^F([1-9]|1[0-2])$/.test(e.code)) return e.code;
  const k = (e.key || '').toLowerCase();
  return LOWER_KEY_MAP[k] || null;
}

function syncPanelUI() {
  setFullMm(cfg.full);
  document.getElementById('pCluster').value = cfg.cluster;
  if (document.activeElement !== pKeysEl) pKeysEl.value = cfg.keys;
  const activeSet = new Set(cfg.keys ? parseCustomKeys(cfg.keys) : (CLUSTERS[cfg.cluster] || CLUSTERS.hud).map(it => it.k));
  for (const ch of pQuickKeys.children) {
    const on = activeSet.has(ch.dataset.qk);
    ch.classList.toggle('on', on);
  }
  if (pCapBtn) {
    pCapBtn.classList.toggle('on', capturingKeys);
    pCapBtn.textContent = capturingKeys ? '录入中…(点此停止)' : '按键录入';
  }
  document.getElementById('pScale').value = cfg.scale;
  document.getElementById('pScaleVal').textContent = cfg.scale.toFixed(2);
  document.getElementById('pOpacity').value = cfg.opacity;
  document.getElementById('pOpacityVal').textContent = cfg.opacity.toFixed(2);
  document.getElementById('pFull').value = cfg.full;
  document.getElementById('pFullVal').textContent = cfg.full.toFixed(2);
  document.getElementById('pMm').value = cfg.mm;
  document.getElementById('pAlign').value = cfg.align;
  for (const ch of pThemes.children) ch.classList.toggle('on', ch.dataset.theme === cfg.theme);
  document.getElementById('pUrl').value = buildObsUrl(false);
  panelEl.classList.toggle('open', cfg.panel);
}

function applyConfig() {
  buildLayout();
  syncPanelUI();
  try { history.replaceState(null, '', buildObsUrl(cfg.panel)); } catch (e) {}
}

document.getElementById('pCluster').addEventListener('change', e => { cfg.cluster = e.target.value; applyConfig(); });
pKeysEl.addEventListener('input', e => { cfg.keys = e.target.value.trim(); applyConfig(); });
if (pCapBtn) pCapBtn.addEventListener('click', () => {
  capturingKeys = !capturingKeys;
  if (capturingKeys) pCapBtn.blur();
  syncPanelUI();
});
document.getElementById('pScale').addEventListener('input', e => { cfg.scale = parseFloat(e.target.value); applyConfig(); });
document.getElementById('pOpacity').addEventListener('input', e => { cfg.opacity = parseFloat(e.target.value); applyConfig(); });
document.getElementById('pFull').addEventListener('input', e => { cfg.full = parseFloat(e.target.value); cfg.hasFullQ = true; applyConfig(); });
document.getElementById('pMm').addEventListener('change', e => { cfg.mm = e.target.value; applyConfig(); });
document.getElementById('pAlign').addEventListener('change', e => { cfg.align = e.target.value; applyConfig(); });
document.getElementById('pChecker').addEventListener('change', e => { document.body.classList.toggle('checker', e.target.checked); });
document.getElementById('pClose').addEventListener('click', () => {
  cfg.panel = false;
  capturingKeys = false;
  document.body.classList.remove('checker');
  document.getElementById('pChecker').checked = false;
  syncPanelUI();
  try { history.replaceState(null, '', buildObsUrl(false)); } catch (e) {}
});
document.getElementById('pReset').addEventListener('click', () => {
  capturingKeys = false;
  cfg = { cluster:'hud', keys:'', theme:'ramp', full:3.5, hasThemeQ:false, hasFullQ:false, scale:1.0, opacity:0.85, mm:'1', align:'center', panel:cfg.panel };
  applyConfig();
});
document.getElementById('pCopy').addEventListener('click', () => {
  const url = buildObsUrl(false);
  const btn = document.getElementById('pCopy');
  const done = () => { btn.textContent = '已复制 ✓'; setTimeout(() => { btn.textContent = '复制 OBS 链接'; }, 1600); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(done, () => {
      const inp = document.getElementById('pUrl'); inp.select(); document.execCommand('copy'); done();
    });
  } else {
    const inp = document.getElementById('pUrl'); inp.select(); document.execCommand('copy'); done();
  }
});
addEventListener('keydown', e => {
  if (capturingKeys) {
    e.preventDefault();
    e.stopPropagation();
    const k = eventToLayoutKey(e);
    if (k) toggleCustomKey(k, true);
    return;
  }
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (e.code === 'KeyC' && !e.ctrlKey && !e.altKey && !e.metaKey) {
    cfg.panel = !cfg.panel;
    if (!cfg.panel) {
      capturingKeys = false;
      document.body.classList.remove('checker');
      document.getElementById('pChecker').checked = false;
    }
    syncPanelUI();
    try { history.replaceState(null, '', buildObsUrl(cfg.panel)); } catch (e2) {}
  }
}, true);

let live = {}, nameIdx = {}, held = new Set();
let lastMsg = Date.now();
const EMPTY_SET = new Set();

const es = new EventSource('/events');
es.onmessage = ev => {
  lastMsg = Date.now();
  const m = JSON.parse(ev.data);
  if (m.live) {
    for (const i in m.live) live[i] = m.live[i];
  }
  if (m.s) {
    for (const p of m.s) live[p[1]] = p[2];
  }
  if (m.k) {
    nameIdx = {};
    for (const i in m.k) if (m.k[i]) nameIdx[norm(m.k[i])] = i;
  }
  if (m.held) held = new Set(m.held.map(norm));
  if (!cfg.hasFullQ && m.full_mm && Math.abs(m.full_mm - cfg.full) > 0.005) {
    cfg.full = clamp(m.full_mm, 1.0, 5.0, FULL);
    setFullMm(cfg.full);
    lastRatio = {};
    syncPanelUI();
  }
  if (!cfg.hasThemeQ && m.theme && FILL_PRESETS[m.theme] && m.theme !== cfg.theme) {
    cfg.theme = m.theme;
    lastRatio = {};
    syncPanelUI();
  }
};

function draw() {
  const stale = Date.now() - lastMsg > 2000;
  const heldNow = stale ? EMPTY_SET : held;
  const preset = FILL_PRESETS[cfg.theme] || FILL_PRESETS.ramp;

  for (const name in els) {
    const idx = nameIdx[name];
    const mm = (!stale && idx !== undefined) ? (live[idx] || 0) : 0;
    const r = Math.max(0, Math.min(1, mm / FULL));
    const prev = lastRatio[name];
    const isHeld = heldNow.has(name);
    const wasHeld = lastHeld[name] === true;
    if (prev !== undefined && Math.abs(prev - r) < 0.003 && isHeld === wasHeld) continue;

    lastRatio[name] = r;
    lastHeld[name] = isHeld;
    const el = els[name];
    const st = preset.style(r);
    const hot = r > 0.06;
    const active = r > 0.04;

    el.fill.style.height = (r * 100).toFixed(1) + '%';
    el.fill.style.background = active ? st.bg : 'transparent';
    el.fill.style.borderTop = active ? ('1.5px solid ' + st.line) : 'none';
    el.box.classList.toggle('hot', hot);
    el.box.classList.toggle('held', isHeld && !hot);
    el.box.style.transform = active ? ('scale(' + (1 - r * 0.035).toFixed(3) + ')') : '';
    el.mm.textContent = (cfg.mm === 'always' || r > 0.08) ? mm.toFixed(1) : '';
  }
  requestAnimationFrame(draw);
}

applyConfig();
requestAnimationFrame(draw);
</script>
</body></html>
"""


MOUSE_OVERLAY_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>直播设备 · 鼠标垫追踪</title>
<style>
  :root { --op: 0.62; --mop: 0.90; --acc: #38bdf8; --padw: 306px; --padh: 254px; --sc: 1; }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; width: 100%; height: 100%;
    background: transparent !important; overflow: hidden;
    color: #f4f4f5;
    font-family: "Microsoft YaHei UI", "Segoe UI", system-ui, sans-serif;
  }
  body.checker {
    background:
      linear-gradient(45deg, #1b1e26 25%, transparent 25%, transparent 75%, #1b1e26 75%),
      linear-gradient(45deg, #1b1e26 25%, #12141a 25%, #12141a 75%, #1b1e26 75%) !important;
    background-size: 24px 24px !important;
    background-position: 0 0, 12px 12px !important;
  }
  #stage {
    width: 100%; height: 100%; padding: 18px;
    display: flex; align-items: center; justify-content: center;
  }
  #stage.align-tl { align-items: flex-start; justify-content: flex-start; }
  #stage.align-tr { align-items: flex-start; justify-content: flex-end; }
  #stage.align-bl { align-items: flex-end; justify-content: flex-start; }
  #stage.align-br { align-items: flex-end; justify-content: flex-end; }

  #pad {
    position: relative;
    width: var(--padw); height: var(--padh);
    border-radius: 14px;
    background: rgba(14, 16, 20, var(--op));
    border: 1px solid rgba(255, 255, 255, 0.14);
    backdrop-filter: blur(8px);
    overflow: hidden;
    user-select: none;
  }
  #pad::before {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background:
      linear-gradient(to right, transparent calc(50% - 0.5px), rgba(255,255,255,0.06) 50%, transparent calc(50% + 0.5px)),
      linear-gradient(to bottom, transparent calc(50% - 0.5px), rgba(255,255,255,0.06) 50%, transparent calc(50% + 0.5px));
  }
  .corner { position: absolute; width: 10px; height: 10px; pointer-events: none;
            border-color: rgba(255,255,255,0.22); border-style: solid; }
  .c-tl { top: 6px; left: 6px; border-width: 1.5px 0 0 1.5px; border-radius: 3px 0 0 0; }
  .c-tr { top: 6px; right: 6px; border-width: 1.5px 1.5px 0 0; border-radius: 0 3px 0 0; }
  .c-bl { bottom: 6px; left: 6px; border-width: 0 0 1.5px 1.5px; border-radius: 0 0 0 3px; }
  .c-br { bottom: 6px; right: 6px; border-width: 0 1.5px 1.5px 0; border-radius: 0 0 3px 0; }
  #trailCv { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }

  #mouse {
    position: absolute; left: 0; top: 0;
    width: 44px; height: 84px;
    opacity: var(--mop);
    pointer-events: none;
    will-change: transform;
    filter: drop-shadow(0 4px 10px rgba(0, 0, 0, 0.55));
  }
  #mouse svg { width: 100%; height: 100%; display: block; overflow: visible; }
  .m-shell { fill: url(#viperGrad); stroke: rgba(255, 255, 255, 0.50); stroke-width: 18; stroke-linejoin: round; }
  .m-seam { fill: none; stroke: rgba(255, 255, 255, 0.22); stroke-width: 12; stroke-linecap: round; }
  .m-btn { fill: rgba(255, 255, 255, 0.03); transition: fill 0.05s linear; }
  .m-btn.on { fill: var(--acc); fill-opacity: 0.78; }
  .m-wheel { fill: rgba(255, 255, 255, 0.16); stroke: rgba(255, 255, 255, 0.48); stroke-width: 12; transition: fill 0.06s linear; }
  .m-wheel.on { fill: var(--acc); stroke: #ffffff; }
  .m-side { fill: rgba(255, 255, 255, 0.16); stroke: rgba(255, 255, 255, 0.42); stroke-width: 10; transition: fill 0.05s linear; }
  .m-side.on { fill: var(--acc); stroke: #ffffff; }
  .m-led { fill: var(--acc); opacity: 0.50; transition: opacity 0.12s linear; }
  .m-led.hot { opacity: 1; }

  #panel {
    position: fixed; top: 14px; right: 14px; width: 296px;
    background: rgba(16, 18, 24, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 12px; padding: 12px 14px;
    backdrop-filter: blur(12px);
    box-shadow: 0 18px 46px rgba(0, 0, 0, 0.65);
    font-size: 12px; display: none; z-index: 30;
  }
  #panel.open { display: block; }
  .p-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .p-head b { font-size: 13px; letter-spacing: 0.5px; }
  .p-tabs { display: flex; gap: 6px; margin-bottom: 10px; padding: 3px;
            background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; }
  .p-tab { flex: 1; text-align: center; text-decoration: none; padding: 5px 8px; border-radius: 6px;
           font-size: 12px; color: rgba(255, 255, 255, 0.62); transition: background .15s, color .15s; }
  .p-tab:hover { color: #fff; background: rgba(255, 255, 255, 0.06); }
  .p-tab.on { color: #fff; background: rgba(56, 189, 248, 0.22); font-weight: 600; }
  .p-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
  .p-row label { color: rgba(255, 255, 255, 0.62); flex-shrink: 0; }
  .p-row select { flex: 1; min-width: 0; background: rgba(255, 255, 255, 0.07); color: #f4f4f5;
                  border: 1px solid rgba(255, 255, 255, 0.15); border-radius: 6px; padding: 4px 7px; font-size: 12px; font-family: inherit; }
  .p-row select option { background: #14171f; color: #f4f4f5; }
  .p-row input[type=range] { flex: 1; accent-color: #38bdf8; }
  .p-val { width: 38px; text-align: right; color: rgba(255, 255, 255, 0.85);
           font-variant-numeric: tabular-nums; font-family: ui-monospace, Consolas, monospace; }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; margin: 4px 0 10px; }
  .chip { background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12);
          color: rgba(255, 255, 255, 0.80); border-radius: 6px; padding: 3px 8px; font-size: 11px;
          cursor: pointer; display: inline-flex; align-items: center; gap: 5px; font-family: inherit; }
  .chip:hover { background: rgba(255, 255, 255, 0.12); color: #fff; }
  .chip.on { border-color: rgba(255, 255, 255, 0.55); background: rgba(255, 255, 255, 0.16); color: #fff; }
  .chip .sw { width: 8px; height: 8px; border-radius: 50%; }
  .p-url { width: 100%; background: rgba(0, 0, 0, 0.35); color: #93c5fd;
           border: 1px solid rgba(255, 255, 255, 0.10); border-radius: 6px;
           padding: 5px 7px; font-size: 11px; font-family: ui-monospace, Consolas, monospace; margin-bottom: 8px; }
  .p-actions { display: flex; gap: 6px; }
  .p-btn { flex: 1; background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.15);
           color: #f4f4f5; border-radius: 7px; padding: 6px 8px; font-size: 12px; cursor: pointer; font-family: inherit; }
  .p-btn:hover { background: rgba(255, 255, 255, 0.15); }
  .p-btn.primary { background: rgba(56, 189, 248, 0.22); border-color: rgba(56, 189, 248, 0.55); color: #e0f2fe; }
  .p-btn.primary:hover { background: rgba(56, 189, 248, 0.34); }
  .p-btn.cal { background: rgba(251, 191, 36, 0.16); border-color: rgba(251, 191, 36, 0.48); color: #fde68a; }
  .p-btn.cal:hover { background: rgba(251, 191, 36, 0.28); }
  .p-btn.cal.on { background: rgba(251, 191, 36, 0.32); color: #fff; }
  .p-hint { color: rgba(255, 255, 255, 0.42); font-size: 11px; margin-top: 8px; line-height: 1.4; }
  #calBanner {
    position: absolute; inset: 0; z-index: 20; display: none;
    flex-direction: column; align-items: center; justify-content: center; gap: 6px;
    background: rgba(12, 15, 21, 0.86); backdrop-filter: blur(6px);
    padding: 14px; text-align: center;
  }
  #calBanner.on { display: flex; }
  #calBanner b { font-size: 13px; color: #fde68a; }
  #calBanner span { font-size: 11px; color: rgba(255, 255, 255, 0.78); line-height: 1.45; }
  #calCount { font-size: 18px; font-weight: 700; color: #38bdf8;
              font-variant-numeric: tabular-nums; font-family: ui-monospace, Consolas, monospace; }
</style></head>
<body>
<div id="stage">
  <div id="pad">
    <div class="corner c-tl"></div><div class="corner c-tr"></div>
    <div class="corner c-bl"></div><div class="corner c-br"></div>
    <canvas id="trailCv"></canvas>
    <div id="mouse" title="Razer Viper V4 Pro (127.1×63.9mm)">
      <svg viewBox="-22 -18 682 1307" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="viperGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#222631" stop-opacity="0.92"/>
            <stop offset="55%" stop-color="#151820" stop-opacity="0.92"/>
            <stop offset="100%" stop-color="#0f1117" stop-opacity="0.94"/>
          </linearGradient>
          <path id="viperOutline" d="M268.5 1268c-66.5-11-128.7-41.4-173.3-85-75-73.3-104.2-175-92.9-323.1 2.1-27 3.9-69.3 4.1-93.9 1.1-144.3.9-138.4 6.9-144.4 6.6-6.6 7.8-28.3 1.8-33.3-2.9-2.4-4.2-20-5.7-74.5-1.8-63.7-1.5-72.4 2.7-80.5 3.7-7.2 4.3-13.6 3-31.7-7-91.8-8.4-276.4-2.3-293.8 4.1-11.7 17.8-24.6 26-24.6 3.9 0 5.5 2.1 5.7 7.7.2 6.1.9 5.3 2.9-4l2.6-11.7 31.4-13.2C150.6 32 258.8-4 267.3-.8c3 1.1 4 7.5 4 25.6 0 21.5.5 24 4.9 23.2 2.7-.5 5.3-3.6 5.8-6.9 1.1-7.7 7.5-9.3 37.5-9.3 28.3 0 38.7 3 38.7 11.1 0 3.2 1.9 5.7 4.2 5.7 3.4 0 4.2-4.3 4.2-22.1 0-29.4 1.6-30.6 29.6-22.7 50.3 14.1 180.7 62.4 191.2 70.8 1.9 1.5 3.5 7.1 3.5 12.3s1 9.5 2.3 9.5c1.3 0 2.2-2.8 2.1-6.3-.4-11 10.2-9.4 21.5 3.1 8.1 8.9 10.5 14.3 12.2 27.5 3.7 27.6 2 154.9-3.2 243.5-8 136.1-6.8 246.9 4.1 398.2 14.8 205.3 9.9 262.2-29.6 341.6-41.4 83.1-117.7 139.9-215.6 160.3-30.7 6.4-86.6 7.8-116.4 2.9z"/>
          <clipPath id="viperClip"><use href="#viperOutline"/></clipPath>
        </defs>
        <use href="#viperOutline" class="m-shell"/>
        <g clip-path="url(#viperClip)">
          <path id="mbL" class="m-btn" d="M 0 0 L 312 0 L 312 486 Q 160 510, 0 462 Z"/>
          <path id="mbR" class="m-btn" d="M 326 0 L 640 0 L 640 462 Q 480 510, 326 486 Z"/>
          <path class="m-seam" d="M 10 462 Q 165 510, 319 488 Q 473 510, 628 462"/>
          <line class="m-seam" x1="319" y1="32" x2="319" y2="488"/>
        </g>
        <use href="#viperOutline" fill="none" stroke="rgba(255,255,255,0.52)" stroke-width="18" stroke-linejoin="round"/>
        <rect id="mbM" class="m-wheel" x="284" y="138" width="70" height="184" rx="35"/>
        <rect id="mbX2" class="m-side" x="-6" y="430" width="32" height="156" rx="14"/>
        <rect id="mbX1" class="m-side" x="-6" y="618" width="32" height="156" rx="14"/>
        <circle id="mbLed" class="m-led" cx="319" cy="560" r="15"/>
      </svg>
    </div>
    <div id="calBanner">
      <b>垫宽校准中（左 → 右）</b>
      <span>请把鼠标放物理垫子最左侧<br>向右平推至最右侧后，按【鼠标中键】保存退出</span>
      <div id="calCount">0 counts</div>
      <span style="opacity:.55">未移动直接按中键 或 按 Esc 取消</span>
    </div>
  </div>
</div>

<div id="panel">
  <div class="p-head">
    <b>直播设备配置</b>
    <button class="chip" id="pClose" type="button">关闭 (C)</button>
  </div>
  <div class="p-tabs">
    <a class="p-tab" href="/overlay?panel=1">键盘 HUD</a>
    <a class="p-tab on" href="/overlay/mouse?panel=1">鼠标垫追踪</a>
  </div>
  <div class="p-row">
    <label for="pPad">鼠标垫尺寸</label>
    <select id="pPad">
      <option value="gsr3">卓威 G-SR III (470×390)</option>
      <option value="sq">方形 (500×500)</option>
      <option value="wide">宽垫 (16:10)</option>
      <option value="xl">桌垫 (900×400)</option>
    </select>
  </div>
  <div class="p-row" style="margin-bottom:4px"><label>主题</label></div>
  <div class="chips" id="pThemes"></div>
  <div class="p-row">
    <label for="pScale">缩放</label>
    <input id="pScale" type="range" min="0.5" max="2.5" step="0.05" value="1">
    <span class="p-val" id="pScaleVal">1.00</span>
  </div>
  <div class="p-row">
    <label for="pOpacity">垫面透明</label>
    <input id="pOpacity" type="range" min="0.1" max="0.95" step="0.05" value="0.62">
    <span class="p-val" id="pOpacityVal">0.62</span>
  </div>
  <div class="p-row">
    <label for="pMOpacity">鼠标透明</label>
    <input id="pMOpacity" type="range" min="0.15" max="1.0" step="0.05" value="0.9">
    <span class="p-val" id="pMOpacityVal">0.90</span>
  </div>
  <div class="p-row">
    <label for="pSens">灵敏度</label>
    <input id="pSens" type="range" min="0.2" max="4.0" step="0.05" value="1">
    <span class="p-val" id="pSensVal">1.00</span>
  </div>
  <div class="p-row">
    <label for="pRecenter">回中模式</label>
    <select id="pRecenter">
      <option value="smart">智能抬鼠回中（推荐）</option>
      <option value="fast">敏捷方向模式</option>
      <option value="off">不自动回中</option>
    </select>
  </div>
  <div class="p-row">
    <label for="pAlign">对齐</label>
    <select id="pAlign">
      <option value="center">居中</option>
      <option value="tl">左上</option>
      <option value="tr">右上</option>
      <option value="bl">左下</option>
      <option value="br">右下</option>
    </select>
  </div>
  <div class="p-row">
    <label for="pTrail">移动轨迹</label>
    <input id="pTrail" type="checkbox" checked>
  </div>
  <div class="p-row">
    <label for="pChecker">预览透明格</label>
    <input id="pChecker" type="checkbox">
  </div>
  <div class="p-actions" style="margin-bottom:8px">
    <button id="pCal" class="p-btn cal" type="button">校准垫宽 (左→右)</button>
    <button id="pCenter" class="p-btn" type="button" style="flex:0 0 68px">归中</button>
  </div>
  <div class="p-row" style="margin-bottom:6px;font-size:11px;color:rgba(255,255,255,.48)">
    <span>当前满宽基准</span><span id="pSpanVal" class="p-val" style="width:auto">14800 counts</span>
  </div>
  <input id="pUrl" class="p-url" type="text" readonly>
  <div class="p-actions">
    <button id="pCopy" class="p-btn primary" type="button">复制鼠标垫链接</button>
    <button id="pReset" class="p-btn" type="button">恢复默认</button>
  </div>
  <div class="p-hint">独立透明源，与键盘 HUD 链接分开。按 C 切面板，按 Home 归中（已避开游戏 R 换弹键）；校准后自动同步 OBS。</div>
</div>

<script>
__THEME_JS__
const PAD_SIZES = { gsr3: [306, 254], sq: [270, 270], wide: [340, 212], xl: [405, 180] };
function clamp(v, lo, hi, def) {
  const n = parseFloat(v);
  return isNaN(n) ? def : Math.max(lo, Math.min(hi, n));
}
function readConfig() {
  const q = new URLSearchParams(location.search);
  const rawPad = (q.get('pad') || 'gsr3').toLowerCase();
  const pad = PAD_SIZES[rawPad] ? rawPad : 'gsr3';
  const hasThemeQ = q.has('theme');
  const rawTheme = (q.get('theme') || 'ramp').toLowerCase();
  const theme = FILL_PRESETS[rawTheme] ? rawTheme : 'ramp';
  const hasSpanQ = q.has('span');
  const span = Math.round(clamp(q.get('span'), 500, 200000, 14800));
  const rawRec = (q.get('recenter') || 'smart').toLowerCase();
  const recenter = rawRec === 'gentle' ? 'smart'
                 : (['smart', 'fast', 'off'].indexOf(rawRec) >= 0 ? rawRec : 'smart');
  const rawAlign = (q.get('align') || 'center').toLowerCase();
  const align = ['center', 'tl', 'tr', 'bl', 'br'].indexOf(rawAlign) >= 0 ? rawAlign : 'center';
  const rawTrail = (q.get('trail') || '1').toLowerCase();
  return {
    pad, theme, hasThemeQ, span, hasSpanQ,
    scale: clamp(q.get('scale'), 0.5, 2.5, 1.0),
    opacity: clamp(q.get('opacity') !== null ? q.get('opacity') : q.get('alpha'), 0.1, 0.95, 0.62),
    mopacity: clamp(q.get('mopacity'), 0.15, 1.0, 0.90),
    sens: clamp(q.get('sens'), 0.2, 4.0, 1.0),
    recenter, align,
    trail: !(rawTrail === '0' || rawTrail === 'false' || rawTrail === 'off'),
    panel: q.get('panel') === '1'
  };
}

let cfg = readConfig();
const stage = document.getElementById('stage');
const padEl = document.getElementById('pad');
const mouseEl = document.getElementById('mouse');
const trailCv = document.getElementById('trailCv');
const tCtx = trailCv.getContext('2d');
const panelEl = document.getElementById('panel');
const pThemes = document.getElementById('pThemes');
let padW = 340, padH = 210, mouseW = 52, mouseH = 80;

for (const id in FILL_PRESETS) {
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'chip'; b.dataset.theme = id;
  b.innerHTML = '<span class="sw" style="background:' + FILL_PRESETS[id].acc + '"></span>' + FILL_PRESETS[id].label;
  b.addEventListener('click', () => { cfg.theme = id; cfg.hasThemeQ = true; applyConfig(); });
  pThemes.appendChild(b);
}

function buildObsUrl(includePanel) {
  const p = new URLSearchParams();
  if (cfg.pad !== 'gsr3') p.set('pad', cfg.pad);
  if (cfg.theme !== 'ramp') p.set('theme', cfg.theme);
  if (Math.abs(cfg.scale - 1.0) > 0.005) p.set('scale', cfg.scale.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
  if (Math.abs(cfg.opacity - 0.62) > 0.005) p.set('opacity', cfg.opacity.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
  if (Math.abs(cfg.mopacity - 0.90) > 0.005) p.set('mopacity', cfg.mopacity.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
  if (Math.abs(cfg.sens - 1.0) > 0.005) p.set('sens', cfg.sens.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
  if (cfg.hasSpanQ && cfg.span !== 14800) p.set('span', String(cfg.span));
  if (cfg.recenter !== 'smart') p.set('recenter', cfg.recenter);
  if (cfg.align !== 'center') p.set('align', cfg.align);
  if (!cfg.trail) p.set('trail', '0');
  if (includePanel) p.set('panel', '1');
  const qs = p.toString();
  return location.origin + '/overlay/mouse' + (qs ? ('?' + qs) : '');
}

function syncPanelUI() {
  document.getElementById('pPad').value = cfg.pad;
  document.getElementById('pScale').value = cfg.scale;
  document.getElementById('pScaleVal').textContent = cfg.scale.toFixed(2);
  document.getElementById('pOpacity').value = cfg.opacity;
  document.getElementById('pOpacityVal').textContent = cfg.opacity.toFixed(2);
  document.getElementById('pMOpacity').value = cfg.mopacity;
  document.getElementById('pMOpacityVal').textContent = cfg.mopacity.toFixed(2);
  document.getElementById('pSens').value = cfg.sens;
  document.getElementById('pSensVal').textContent = cfg.sens.toFixed(2);
  document.getElementById('pSpanVal').textContent = cfg.span + ' counts';
  document.getElementById('pRecenter').value = cfg.recenter;
  document.getElementById('pAlign').value = cfg.align;
  document.getElementById('pTrail').checked = cfg.trail;
  for (const ch of pThemes.children) ch.classList.toggle('on', ch.dataset.theme === cfg.theme);
  document.getElementById('pUrl').value = buildObsUrl(false);
  panelEl.classList.toggle('open', cfg.panel);
}

function applyConfig() {
  const base = PAD_SIZES[cfg.pad] || PAD_SIZES.gsr3;
  padW = Math.round(base[0] * cfg.scale);
  padH = Math.round(base[1] * cfg.scale);
  mouseW = Math.round(44 * cfg.scale);
  mouseH = Math.round(84 * cfg.scale);
  document.documentElement.style.setProperty('--padw', padW + 'px');
  document.documentElement.style.setProperty('--padh', padH + 'px');
  document.documentElement.style.setProperty('--op', cfg.opacity.toFixed(2));
  document.documentElement.style.setProperty('--mop', cfg.mopacity.toFixed(2));
  const preset = FILL_PRESETS[cfg.theme] || FILL_PRESETS.ramp;
  document.documentElement.style.setProperty('--acc', preset.acc);
  mouseEl.style.width = mouseW + 'px';
  mouseEl.style.height = mouseH + 'px';
  stage.className = 'align-' + cfg.align;
  const dpr = window.devicePixelRatio || 1;
  trailCv.width = Math.max(1, Math.round(padW * dpr));
  trailCv.height = Math.max(1, Math.round(padH * dpr));
  tCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  syncPanelUI();
  try { history.replaceState(null, '', buildObsUrl(cfg.panel)); } catch (e) {}
}

document.getElementById('pPad').addEventListener('change', e => { cfg.pad = e.target.value; applyConfig(); });
document.getElementById('pScale').addEventListener('input', e => { cfg.scale = parseFloat(e.target.value); applyConfig(); });
document.getElementById('pOpacity').addEventListener('input', e => { cfg.opacity = parseFloat(e.target.value); applyConfig(); });
document.getElementById('pMOpacity').addEventListener('input', e => { cfg.mopacity = parseFloat(e.target.value); applyConfig(); });
document.getElementById('pSens').addEventListener('input', e => { cfg.sens = parseFloat(e.target.value); applyConfig(); });
document.getElementById('pRecenter').addEventListener('change', e => { cfg.recenter = e.target.value; applyConfig(); });
document.getElementById('pAlign').addEventListener('change', e => { cfg.align = e.target.value; applyConfig(); });
document.getElementById('pTrail').addEventListener('change', e => { cfg.trail = e.target.checked; if (!cfg.trail) trail.length = 0; applyConfig(); });
document.getElementById('pChecker').addEventListener('change', e => { document.body.classList.toggle('checker', e.target.checked); });
document.getElementById('pClose').addEventListener('click', () => {
  cfg.panel = false; document.body.classList.remove('checker'); document.getElementById('pChecker').checked = false;
  syncPanelUI(); try { history.replaceState(null, '', buildObsUrl(false)); } catch (e) {}
});
let calibrating = false, calDx = 0, prevMidBtn = false, srvToken = '';
const calBanner = document.getElementById('calBanner');
const calCountEl = document.getElementById('calCount');
const calBtn = document.getElementById('pCal');

function setCalibrating(on, saveResult) {
  if (on) {
    calibrating = true; calDx = 0;
    tx = -1; ty = 0; trail.length = 0;
    calCountEl.textContent = '0 counts';
    calBanner.classList.add('on');
    calBtn.classList.add('on');
    calBtn.textContent = '校准中… (中键完成)';
    return;
  }
  const measured = Math.round(Math.abs(calDx));
  calibrating = false;
  calBanner.classList.remove('on');
  calBtn.classList.remove('on');
  calBtn.textContent = '校准垫宽 (左→右)';
  if (saveResult && measured >= 500) {
    cfg.span = Math.max(500, Math.min(200000, measured));
    cfg.sens = 1.0;
    cfg.hasSpanQ = false;
    fetch('/settings', {
      method: 'POST',
      headers: { 'X-Depth-Token': srvToken },
      body: JSON.stringify({ mouse_span: cfg.span })
    }).catch(() => {});
    applyConfig();
  }
  tx = ty = 0; trail.length = 0;
}

calBtn.addEventListener('click', () => { setCalibrating(!calibrating, false); calBtn.blur(); });
document.getElementById('pCenter').addEventListener('click', () => { tx = ty = 0; trail.length = 0; });
document.getElementById('pReset').addEventListener('click', () => {
  if (calibrating) setCalibrating(false, false);
  cfg = { pad:'gsr3', theme:'ramp', hasThemeQ:false, span:14800, hasSpanQ:false, scale:1.0, opacity:0.62, mopacity:0.90, sens:1.0, recenter:'smart', align:'center', trail:true, panel:cfg.panel };
  fetch('/settings', {
    method: 'POST',
    headers: { 'X-Depth-Token': srvToken },
    body: JSON.stringify({ mouse_span: 14800 })
  }).catch(() => {});
  tx = ty = px = py = 0; trail.length = 0; applyConfig();
});
document.getElementById('pCopy').addEventListener('click', () => {
  const url = buildObsUrl(false), btn = document.getElementById('pCopy');
  const done = () => { btn.textContent = '已复制 ✓'; setTimeout(() => { btn.textContent = '复制鼠标垫链接'; }, 1600); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(done, () => { const i = document.getElementById('pUrl'); i.select(); document.execCommand('copy'); done(); });
  } else { const i = document.getElementById('pUrl'); i.select(); document.execCommand('copy'); done(); }
});
addEventListener('keydown', e => {
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (e.code === 'Escape' && calibrating) { setCalibrating(false, false); return; }
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (e.code === 'KeyC' && !e.ctrlKey && !e.altKey && !e.metaKey) {
    cfg.panel = !cfg.panel;
    if (!cfg.panel) { document.body.classList.remove('checker'); document.getElementById('pChecker').checked = false; }
    syncPanelUI(); try { history.replaceState(null, '', buildObsUrl(cfg.panel)); } catch (e2) {}
  } else if (e.code === 'Home' && !e.ctrlKey && !e.altKey && !e.metaKey) {
    tx = ty = 0; trail.length = 0;
  }
});

let tx = 0, ty = 0, px = 0, py = 0, lastMoveMs = 0, wheelUntil = 0, lastFrameMs = performance.now();
const btns = { l: false, r: false, m: false, x1: false, x2: false };
const trail = [];
const mbL = document.getElementById('mbL'), mbR = document.getElementById('mbR');
const mbM = document.getElementById('mbM'), mbX1 = document.getElementById('mbX1');
const mbX2 = document.getElementById('mbX2'), mbLed = document.getElementById('mbLed');

const es = new EventSource('/events');
es.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.token) srvToken = m.token;
  if (!cfg.hasThemeQ && m.theme && FILL_PRESETS[m.theme] && m.theme !== cfg.theme) {
    cfg.theme = m.theme; applyConfig();
  }
  if (!cfg.hasSpanQ && m.mouse_span && Math.abs(m.mouse_span - cfg.span) >= 1) {
    cfg.span = Math.round(clamp(m.mouse_span, 500, 200000, 14800));
    syncPanelUI();
  }
  const ms = m.m;
  if (!ms) return;
  const midNow = !!ms.m;
  if (calibrating) {
    if (ms.dx) {
      calDx += ms.dx;
      const absDx = Math.round(Math.abs(calDx));
      calCountEl.textContent = absDx + ' counts';
      tx = Math.max(-1, Math.min(1, -1 + 2 * Math.min(1, absDx / Math.max(2000, cfg.span))));
      lastMoveMs = performance.now();
    }
    if (midNow && !prevMidBtn) setCalibrating(false, Math.abs(calDx) >= 500);
    prevMidBtn = midNow;
    btns.l = !!ms.l; btns.r = !!ms.r; btns.m = midNow;
    btns.x1 = !!ms.x1; btns.x2 = !!ms.x2;
    return;
  }
  prevMidBtn = midNow;
  if (ms.dx || ms.dy) {
    const step = (2.0 * cfg.sens) / Math.max(500, cfg.span);
    const rx = Math.max(1, (padW - mouseW) * 0.5);
    const ry = Math.max(1, (padH - mouseH) * 0.5);
    const ratio = rx / ry;
    tx = Math.max(-1, Math.min(1, tx + ms.dx * step));
    ty = Math.max(-1, Math.min(1, ty + ms.dy * step * ratio));
    lastMoveMs = performance.now();
  }
  if (ms.w) wheelUntil = performance.now() + 140;
  btns.l = !!ms.l; btns.r = !!ms.r; btns.m = midNow;
  btns.x1 = !!ms.x1; btns.x2 = !!ms.x2;
};

function loop(now) {
  const dt = Math.min(0.05, (now - lastFrameMs) / 1000);
  lastFrameMs = now;
  const idleMs = now - lastMoveMs;
  if (!calibrating) {
    if (cfg.recenter === 'fast' && idleMs > 60) {
      const k = Math.exp(-dt * 5.2);
      tx *= k; ty *= k;
    } else if (cfg.recenter === 'smart' && idleMs > 200) {
      const dist = Math.min(1.4, Math.hypot(tx, ty));
      const rate = 1.6 + dist * 4.2;
      const k = Math.exp(-dt * rate);
      tx *= k; ty *= k;
    }
  }
  const prevX = px, prevY = py;
  const alpha = 1 - Math.exp(-dt * 24);
  px += (tx - px) * alpha;
  py += (ty - py) * alpha;

  const padInset = 10 * cfg.scale;
  const maxOx = Math.max(4, (padW - mouseW) * 0.5 - padInset);
  const maxOy = Math.max(4, (padH - mouseH) * 0.5 - padInset);
  const cx = padW * 0.5 + px * maxOx;
  const cy = padH * 0.5 + py * maxOy;
  const vx = (px - prevX) / Math.max(0.004, dt);
  const tilt = Math.max(-6, Math.min(6, vx * 1.2));
  mouseEl.style.transform = 'translate3d(' + (cx - mouseW * 0.5).toFixed(1) + 'px,' +
    (cy - mouseH * 0.5).toFixed(1) + 'px,0) rotate(' + tilt.toFixed(2) + 'deg)';

  const moving = idleMs < 180;
  mbL.classList.toggle('on', btns.l);
  mbR.classList.toggle('on', btns.r);
  mbM.classList.toggle('on', btns.m || now < wheelUntil);
  mbX1.classList.toggle('on', btns.x1);
  mbX2.classList.toggle('on', btns.x2);
  mbLed.classList.toggle('hot', moving || btns.l || btns.r);

  if (cfg.trail) {
    if (moving || trail.length) {
      if (moving) trail.push([cx, cy, now]);
      while (trail.length && (now - trail[0][2] > 620 || trail.length > 48)) trail.shift();
      tCtx.clearRect(0, 0, padW, padH);
      if (trail.length > 1) {
        const speedR = Math.min(1, Math.hypot(tx - px, ty - py) * 3.5 + (btns.l ? 0.5 : 0));
        const st = (FILL_PRESETS[cfg.theme] || FILL_PRESETS.ramp).style(speedR);
        tCtx.lineCap = 'round'; tCtx.lineJoin = 'round';
        for (let i = 1; i < trail.length; i++) {
          const age = 1 - (now - trail[i][2]) / 620;
          if (age <= 0) continue;
          tCtx.strokeStyle = st.acc;
          tCtx.globalAlpha = age * 0.68;
          tCtx.lineWidth = Math.max(1.4, age * 3.5 * cfg.scale);
          tCtx.beginPath();
          tCtx.moveTo(trail[i - 1][0], trail[i - 1][1]);
          tCtx.lineTo(trail[i][0], trail[i][1]);
          tCtx.stroke();
        }
        tCtx.globalAlpha = 1;
      }
    }
  } else {
    tCtx.clearRect(0, 0, padW, padH);
  }
  requestAnimationFrame(loop);
}

applyConfig();
requestAnimationFrame(loop);
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


def workbench_html():
    """Segment picker + export workbench for recorded depth data."""
    return (r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>素材工作台</title>
<style>
  :root { --u:44px; --bg:#101215; --card:rgba(255,255,255,.03); --edge:rgba(255,255,255,.10);
          --fg:#f4f4f5; --dim:rgba(255,255,255,.52); --acc:#38bdf8; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:"Microsoft YaHei UI","Segoe UI",system-ui,sans-serif; font-size:13px; }
  .num { font-variant-numeric:tabular-nums; font-family:ui-monospace,"Cascadia Mono",Consolas,monospace; }
  header { display:flex; align-items:center; gap:12px; padding:12px 20px; border-bottom:1px solid var(--edge); }
  h1 { font-size:14px; margin:0; letter-spacing:.8px; font-weight:600; }
  a.nav { color:var(--dim); text-decoration:none; font-size:12px; padding:4px 9px; border-radius:6px; }
  a.nav:hover { color:#fff; background:rgba(255,255,255,.07); }
  .wrap { max-width:1280px; margin:0 auto; padding:12px 18px 20px; display:flex; flex-direction:column; gap:12px; }
  .card { background:var(--card); border:1px solid var(--edge); border-radius:10px; padding:12px 14px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .sub { color:var(--dim); font-size:11px; letter-spacing:.6px; }
  .pill { color:var(--dim); font-size:12px; }
  .pill b { color:#e4e4e7; font-weight:600; margin-left:4px; }
  input, select, button { background:rgba(255,255,255,.06); color:var(--fg); border:1px solid rgba(255,255,255,.14);
          border-radius:6px; padding:5px 9px; font-size:12px; font-family:inherit; }
  select option { background:#14171f; color:#f4f4f5; }
  button { cursor:pointer; }
  button:hover { background:rgba(255,255,255,.12); }
  button.primary { background:rgba(16,185,129,.18); border-color:rgba(52,211,153,.5); color:#6ee7b7; }
  button.primary:hover { background:rgba(16,185,129,.28); }
  button.play { background:rgba(56,189,248,.16); border-color:rgba(56,189,248,.45); color:#bae6fd; min-width:96px; }
  button.play.on { background:rgba(244,63,94,.22); border-color:rgba(244,63,94,.55); color:#fecdd3; }

  #prevStage { display:flex; align-items:center; justify-content:center; gap:24px; flex-wrap:wrap;
               min-height:170px; padding:10px 4px; overflow:hidden; }
  #kbPrev { position:relative; flex-shrink:0; }
  #wbPadWrap { position:relative; flex-shrink:0; display:flex; flex-direction:column; align-items:center; gap:4px; }
  #wbPadWrap.off { display:none; }
  #wbPad { position:relative; width:220px; height:182px; border-radius:11px;
           background:rgba(14,16,20,.72); border:1px solid rgba(255,255,255,.14);
           backdrop-filter:blur(8px); overflow:hidden; user-select:none; }
  #wbPad::before { content:""; position:absolute; inset:0; pointer-events:none;
                   background:linear-gradient(to right,transparent calc(50% - 0.5px),rgba(255,255,255,.06) 50%,transparent calc(50% + 0.5px)),
                              linear-gradient(to bottom,transparent calc(50% - 0.5px),rgba(255,255,255,.06) 50%,transparent calc(50% + 0.5px)); }
  .wb-c { position:absolute; width:8px; height:8px; pointer-events:none; border-color:rgba(255,255,255,.22); border-style:solid; }
  .wb-tl { top:5px; left:5px; border-width:1.5px 0 0 1.5px; border-radius:2px 0 0 0; }
  .wb-tr { top:5px; right:5px; border-width:1.5px 1.5px 0 0; border-radius:0 2px 0 0; }
  .wb-bl { bottom:5px; left:5px; border-width:0 0 1.5px 1.5px; border-radius:0 0 0 2px; }
  .wb-br { bottom:5px; right:5px; border-width:0 1.5px 1.5px 0; border-radius:0 0 2px 0; }
  #wbTrail { position:absolute; inset:0; width:100%; height:100%; display:block; pointer-events:none; }
  #wbMouse { position:absolute; left:0; top:0; width:32px; height:60px; pointer-events:none;
             will-change:transform; filter:drop-shadow(0 3px 7px rgba(0,0,0,.55)); }
  #wbMouse svg { width:100%; height:100%; display:block; overflow:visible; }
  .wm-shell { fill:url(#wbViperGrad); stroke:rgba(255,255,255,.5); stroke-width:18; stroke-linejoin:round; }
  .wm-seam { fill:none; stroke:rgba(255,255,255,.22); stroke-width:12; stroke-linecap:round; }
  .wm-btn { fill:rgba(255,255,255,.03); }
  .wm-btn.on { fill:var(--acc); fill-opacity:.82; }
  .wm-wheel { fill:rgba(255,255,255,.16); stroke:rgba(255,255,255,.48); stroke-width:12; }
  .wm-wheel.on { fill:var(--acc); stroke:#fff; }
  .wm-side { fill:rgba(255,255,255,.16); stroke:rgba(255,255,255,.42); stroke-width:10; }
  .wm-side.on { fill:var(--acc); stroke:#fff; }
  .wm-led { fill:var(--acc); opacity:.45; }
  .wm-led.hot { opacity:1; }
  #wbMouseEmpty { position:absolute; inset:0; display:none; align-items:center; justify-content:center;
                  text-align:center; padding:12px; font-size:11px; color:var(--dim);
                  background:rgba(12,14,19,.72); backdrop-filter:blur(4px); }
  #wbMouseEmpty.on { display:flex; }
  .k { position:absolute; border-radius:calc(var(--u) * 0.125); overflow:hidden;
       background:rgba(14,16,20,.68); border:1px solid rgba(255,255,255,.13);
       backdrop-filter:blur(8px); user-select:none; transition:transform .04s linear; }
  .k .fill { position:absolute; left:0; right:0; bottom:0; height:0%; pointer-events:none; }
  .k .lbl { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
            font-size:calc(var(--u) * 0.24); font-weight:600; color:rgba(255,255,255,.72);
            text-shadow:0 1px 2px rgba(0,0,0,.65); pointer-events:none; }
  .k.hot .lbl { color:#fff; }
  .k .mm { position:absolute; right:4px; bottom:3px; z-index:2; font-size:calc(var(--u) * 0.17);
           color:rgba(255,255,255,.9); text-shadow:0 1px 2px rgba(0,0,0,.75); opacity:0; pointer-events:none; }
  .k.hot .mm { opacity:1; }
  .k.dim { opacity:.32; }

  #hoverRead { font-size:12px; color:#cbd5e1; min-height:18px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  #hoverRead .hk { background:rgba(56,189,248,.12); border:1px solid rgba(56,189,248,.3); color:#bae6fd;
                   padding:1px 6px; border-radius:5px; font-size:11px; }
  #hoverRead .hk.m { background:rgba(244,63,94,.16); border-color:rgba(244,63,94,.42); color:#fecdd3; }
  #tracks { position:relative; margin-top:8px; }
  #cv { width:100%; height:264px; display:block; background:#0b0e13; border:1px solid rgba(255,255,255,.07);
        border-radius:8px; cursor:crosshair; }
  #hint { color:var(--dim); font-size:11px; margin-top:6px; display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; }
  .key-chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
  .kchip { display:inline-flex; align-items:center; gap:5px; padding:3px 8px; border-radius:6px; font-size:12px;
           background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.12); color:var(--dim); cursor:pointer; user-select:none; }
  .kchip.on { background:rgba(56,189,248,.16); border-color:rgba(56,189,248,.48); color:#e0f2fe; }
  .kchip input { margin:0; accent-color:#38bdf8; }
  #prog { height:5px; background:rgba(255,255,255,.07); border-radius:4px; overflow:hidden; margin-top:10px; }
  #prog i { display:block; height:100%; width:0%; background:linear-gradient(90deg,#38bdf8,#34d399); transition:width .15s; }
  #log { color:var(--dim); font-size:11px; margin-top:6px; white-space:pre-wrap; }
  #histDrawer { display:none; }
  #histDrawer.open { display:block; }
  .hist-list { display:flex; flex-direction:column; gap:6px; max-height:220px; overflow-y:auto; margin-top:8px; }
  .hist-item { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:7px 10px;
               border-radius:7px; background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.08);
               cursor:pointer; }
  .hist-item:hover { background:rgba(255,255,255,.07); }
  .hist-item.on { background:rgba(56,189,248,.13); border-color:rgba(56,189,248,.48); }
  .hist-meta { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:12px; }
  .hist-tag { font-size:10px; padding:1px 6px; border-radius:999px; background:rgba(52,211,153,.16);
              color:#6ee7b7; border:1px solid rgba(52,211,153,.35); }
  .hist-tag.m { background:rgba(56,189,248,.16); color:#bae6fd; border-color:rgba(56,189,248,.38); }
  .hist-keys { font-size:11px; color:var(--dim); max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  button.danger { color:#fca5a5; border-color:rgba(239,68,68,.32); background:rgba(239,68,68,.10); padding:3px 8px; font-size:11px; }
  button.danger:hover { background:rgba(239,68,68,.22); }
</style></head><body>
<header>
  <h1>素材工作台</h1>
  <select id="recSel" class="num" title="切换历史录制" onchange="onSelectRec(this.value)">
    <option value="">加载中…</option>
  </select>
  <button id="histBtn" type="button" onclick="toggleHist()">历史列表 ▾</button>
  <button type="button" onclick="loadList(currentStamp)">刷新</button>
  <span class="pill">时长<b id="r_dur" class="num">—</b></span>
  <span class="pill">采样<b id="r_n" class="num">—</b></span>
  <span id="r_stamp" style="display:none"></span>
  <span style="flex:1"></span>
  <a class="nav" href="/">← 返回监测</a>
  <a class="nav" href="/overlay?panel=1" target="_blank">直播设备</a>
</header>

<div class="wrap">
  <div id="histDrawer" class="card">
    <div class="row" style="justify-content:space-between">
      <span class="sub">历史录制列表（<span id="histCount" class="num">0</span> 条）</span>
      <button type="button" onclick="toggleHist(false)">收起</button>
    </div>
    <div id="histList" class="hist-list"><span class="sub">暂无录制</span></div>
  </div>
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="row">
        <button id="playBtn" class="play" type="button" onclick="togglePlay()">▶ 试播选区</button>
        <span class="pill">当前时刻<b id="cur_t" class="num">0.000 s</b></span>
        <div id="hoverRead" class="num"><span style="color:var(--dim)">移动鼠标到时间轴查看实时按键深度</span></div>
      </div>
      <div class="row">
        <label id="showMouseChip" class="kchip on" style="margin:0"><input id="showMouse" type="checkbox" checked onchange="toggleWbMouse()">同步鼠标</label>
        <span class="sub">布局</span>
        <select id="layout" onchange="buildPreviewKb()">
          <option value="hud">HUD 键簇</option>
          <option value="wasd">WASD 极简</option>
          <option value="full">75% 整键盘</option>
        </select>
        <span class="sub">主题</span>
        <select id="theme" onchange="renderPreviewAt(activeTime())">
          <option value="ramp">随深度平滑</option>
          <option value="cyan">冰川青</option>
          <option value="mint">薄荷绿</option>
          <option value="amber">琥珀金</option>
          <option value="crimson">绯红</option>
          <option value="violet">紫罗兰</option>
        </select>
        <span class="sub">满程</span>
        <input id="wbFull" class="num" type="number" min="1.0" max="5.0" step="0.05" value="3.50" style="width:66px" onchange="onWbFullChange(this.value)">
      </div>
    </div>
    <div id="prevStage">
      <div id="kbPrev"></div>
      <div id="wbPadWrap">
        <div id="wbPad" title="鼠标同步回放 (Zowie G-SR III · Viper V4 Pro)">
          <div class="wb-c wb-tl"></div><div class="wb-c wb-tr"></div>
          <div class="wb-c wb-bl"></div><div class="wb-c wb-br"></div>
          <canvas id="wbTrail"></canvas>
          <div id="wbMouse">
            <svg viewBox="-22 -18 682 1307" xmlns="http://www.w3.org/2000/svg">
              <defs>
                <linearGradient id="wbViperGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="#222631" stop-opacity="0.92"/>
                  <stop offset="55%" stop-color="#151820" stop-opacity="0.92"/>
                  <stop offset="100%" stop-color="#0f1117" stop-opacity="0.94"/>
                </linearGradient>
                <path id="wbViperOutline" d="M268.5 1268c-66.5-11-128.7-41.4-173.3-85-75-73.3-104.2-175-92.9-323.1 2.1-27 3.9-69.3 4.1-93.9 1.1-144.3.9-138.4 6.9-144.4 6.6-6.6 7.8-28.3 1.8-33.3-2.9-2.4-4.2-20-5.7-74.5-1.8-63.7-1.5-72.4 2.7-80.5 3.7-7.2 4.3-13.6 3-31.7-7-91.8-8.4-276.4-2.3-293.8 4.1-11.7 17.8-24.6 26-24.6 3.9 0 5.5 2.1 5.7 7.7.2 6.1.9 5.3 2.9-4l2.6-11.7 31.4-13.2C150.6 32 258.8-4 267.3-.8c3 1.1 4 7.5 4 25.6 0 21.5.5 24 4.9 23.2 2.7-.5 5.3-3.6 5.8-6.9 1.1-7.7 7.5-9.3 37.5-9.3 28.3 0 38.7 3 38.7 11.1 0 3.2 1.9 5.7 4.2 5.7 3.4 0 4.2-4.3 4.2-22.1 0-29.4 1.6-30.6 29.6-22.7 50.3 14.1 180.7 62.4 191.2 70.8 1.9 1.5 3.5 7.1 3.5 12.3s1 9.5 2.3 9.5c1.3 0 2.2-2.8 2.1-6.3-.4-11 10.2-9.4 21.5 3.1 8.1 8.9 10.5 14.3 12.2 27.5 3.7 27.6 2 154.9-3.2 243.5-8 136.1-6.8 246.9 4.1 398.2 14.8 205.3 9.9 262.2-29.6 341.6-41.4 83.1-117.7 139.9-215.6 160.3-30.7 6.4-86.6 7.8-116.4 2.9z"/>
                <clipPath id="wbViperClip"><use href="#wbViperOutline"/></clipPath>
              </defs>
              <use href="#wbViperOutline" class="wm-shell"/>
              <g clip-path="url(#wbViperClip)">
                <path id="wbMbL" class="wm-btn" d="M 0 0 L 312 0 L 312 486 Q 160 510, 0 462 Z"/>
                <path id="wbMbR" class="wm-btn" d="M 326 0 L 640 0 L 640 462 Q 480 510, 326 486 Z"/>
                <path class="wm-seam" d="M 10 462 Q 165 510, 319 488 Q 473 510, 628 462"/>
                <line class="wm-seam" x1="319" y1="32" x2="319" y2="488"/>
              </g>
              <use href="#wbViperOutline" fill="none" stroke="rgba(255,255,255,0.52)" stroke-width="18" stroke-linejoin="round"/>
              <rect id="wbMbM" class="wm-wheel" x="284" y="138" width="70" height="184" rx="35"/>
              <rect id="wbMbX2" class="wm-side" x="-6" y="430" width="32" height="156" rx="14"/>
              <rect id="wbMbX1" class="wm-side" x="-6" y="618" width="32" height="156" rx="14"/>
              <circle id="wbMbLed" class="wm-led" cx="319" cy="560" r="15"/>
            </svg>
          </div>
          <div id="wbMouseEmpty">该录制无鼠标数据<br>新录制将自动同步记录</div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="row">
        <span class="sub">起点</span>
        <input id="sel_start" class="num" size="7" value="0.000">
        <span class="sub">终点</span>
        <input id="sel_end" class="num" size="7" value="0.000">
        <span class="pill">选中<b id="sel_len" class="num">0.000</b> 秒</span>
      </div>
      <div class="row">
        <button type="button" onclick="zoom(0.5)">放大</button>
        <button type="button" onclick="zoom(2)">缩小</button>
        <button type="button" onclick="resetView()">全览</button>
      </div>
    </div>
    <div id="tracks"><canvas id="cv"></canvas></div>
    <div id="hint">
      <span>拖拽选区 · 滚轮缩放 · 悬停实时同步预览键鼠 · 双击清选 · Space 播/停 · ←/→ 逐帧微调</span>
      <span id="viewSpan" class="num">0.000s — 0.000s</span>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <span class="sub">导出键位（勾选控制导出与波形高亮）</span>
      <div class="row" style="gap:6px">
        <button type="button" onclick="selectKeysPreset('all')">全选</button>
        <button type="button" onclick="selectKeysPreset('hud')">常用 HUD</button>
        <button type="button" onclick="selectKeysPreset('none')">清空</button>
      </div>
    </div>
    <div id="keyChips" class="key-chips"><span class="sub">加载录制后显示可选键位…</span></div>
  </div>

  <div class="card">
    <div class="row">
      <span class="sub">分辨率</span>
      <select id="size">
        <option value="1920x1080">1080p</option>
        <option value="2560x1440">1440p</option>
        <option value="3840x2160">4K</option>
      </select>
      <span class="sub">帧率</span>
      <select id="fps"><option>30</option><option selected>60</option><option>120</option></select>
      <span class="sub">倍速</span>
      <select id="speed"><option value="0.25">0.25x</option><option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option></select>
      <span class="sub">格式</span>
      <select id="fmt">
        <option value="prores">ProRes 4444（透明）</option>
        <option value="png">PNG 序列（透明）</option>
      </select>
      <span class="sub">背景</span>
      <select id="bg"><option value="">透明</option><option value="#101014">深色 #101014</option><option value="#ffffff">白色</option></select>
      <span style="flex:1"></span>
      <button class="primary" type="button" onclick="startExport()">开始导出</button>
      <button type="button" onclick="cancelExport()">取消</button>
    </div>
    <div id="prog"><i></i></div>
    <div id="log">—</div>
  </div>
</div>

<script>
const FULL_LAYOUT = __LAYOUT__;
__THEME_JS__

const CLUSTERS = {
  hud: [
    {k:'Q', r:0, x:0.0, w:1.0}, {k:'W', r:0, x:1.0, w:1.0}, {k:'E', r:0, x:2.0, w:1.0}, {k:'R', r:0, x:3.0, w:1.0},
    {k:'A', r:1, x:0.0, w:1.0}, {k:'S', r:1, x:1.0, w:1.0}, {k:'D', r:1, x:2.0, w:1.0},
    {k:'LShift', r:2, x:0.0, w:1.35}, {k:'LCtrl', r:2, x:1.35, w:1.2}, {k:'LAlt', r:2, x:2.55, w:1.15}, {k:'Space', r:2, x:3.7, w:2.6}
  ],
  wasd: [
    {k:'W', r:0, x:1.0, w:1.0},
    {k:'A', r:1, x:0.0, w:1.0}, {k:'S', r:1, x:1.0, w:1.0}, {k:'D', r:1, x:2.0, w:1.0},
    {k:'LAlt', r:2, x:0.0, w:1.25}, {k:'Space', r:2, x:1.25, w:2.75}
  ],
  full: FULL_LAYOUT
};
const HUD_DEFAULT_KEYS = new Set(['Q','W','E','R','A','S','D','LShift','LCtrl','LAlt','Space']);

const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const wbTrailCv = document.getElementById('wbTrail'), wbTrailCtx = wbTrailCv ? wbTrailCv.getContext('2d') : null;
const RULER_H = 22, CV_H = 264;
let info = null, data = null, view = [0, 1], sel = [0, 0], dragging = false, dragFrom = 0;
let hoverT = null, playT = 0, playing = false, playLastMs = 0, showMouse = true;
let selectedKeys = new Set(), prevEls = {};

function fmt(t) { return (t || 0).toFixed(3); }
function activeTime() { return hoverT !== null ? hoverT : playT; }

function fit() {
  const dpr = window.devicePixelRatio || 1;
  const r = cv.getBoundingClientRect();
  cv.width = Math.max(200, Math.round(r.width * dpr));
  cv.height = Math.round(CV_H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function xOf(t) {
  const w = cv.getBoundingClientRect().width;
  return (t - view[0]) / Math.max(1e-6, view[1] - view[0]) * w;
}
function tOf(x) {
  const w = cv.getBoundingClientRect().width;
  return view[0] + x / Math.max(1, w) * (view[1] - view[0]);
}

function sampleAt(t) {
  const out = {};
  if (!data || !data.ok || !data.t || !data.t.length) return out;
  const ts = data.t, n = ts.length;
  let lo = 0, hi = n - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid + 1; else hi = mid - 1;
  }
  const i = Math.max(0, Math.min(n - 1, hi));
  const j = Math.min(n - 1, i + 1);
  const dt = ts[j] - ts[i];
  const f = (dt > 1e-6 && dt <= 0.12 && t >= ts[i]) ? Math.max(0, Math.min(1, (t - ts[i]) / dt)) : 0;
  for (const k of data.keys) {
    const arr = data.v[k];
    if (!arr) continue;
    const v = (f > 0) ? (arr[i] + (arr[j] - arr[i]) * f) : arr[i];
    out[k] = v;
    out[norm(k)] = v;
  }
  return out;
}

function sampleMouseAt(t) {
  if (!data || !data.ok || !data.mouse || !data.t || !data.t.length) return null;
  const ts = data.t, m = data.mouse, n = Math.min(ts.length, (m.x || []).length);
  if (!n) return null;
  let lo = 0, hi = n - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid + 1; else hi = mid - 1;
  }
  const i = Math.max(0, Math.min(n - 1, hi));
  const j = Math.min(n - 1, i + 1);
  const dt = ts[j] - ts[i];
  const f = (dt > 1e-6 && dt <= 0.12 && t >= ts[i]) ? Math.max(0, Math.min(1, (t - ts[i]) / dt)) : 0;
  const x = f > 0 ? (m.x[i] + (m.x[j] - m.x[i]) * f) : m.x[i];
  const y = f > 0 ? (m.y[i] + (m.y[j] - m.y[i]) * f) : m.y[i];
  return { x, y, b: (m.b && m.b[i]) || 0, w: (m.w && m.w[i]) || 0, spd: (m.spd && m.spd[i]) || 0, idx: i };
}

function chooseTickStep(span, pxW) {
  const target = Math.max(4, Math.floor(pxW / 90));
  const raw = span / target;
  const steps = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60];
  for (const s of steps) if (s >= raw) return s;
  return 60;
}

function draw() {
  const w = cv.getBoundingClientRect().width, h = CV_H;
  ctx.clearRect(0, 0, w, h);
  if (!data || !data.ok) {
    ctx.fillStyle = 'rgba(255,255,255,0.45)'; ctx.font = '13px sans-serif';
    ctx.fillText(data && data.error ? ('读取失败：' + data.error) : '没有录制数据（请先在主监测页录制并停止）', 16, 44);
    return;
  }
  const span = Math.max(1e-3, view[1] - view[0]);
  const vs = document.getElementById('viewSpan');
  if (vs) vs.textContent = fmt(view[0]) + 's — ' + fmt(view[1]) + 's';

  // Top adaptive time ruler + vertical grid
  ctx.fillStyle = 'rgba(255,255,255,0.03)';
  ctx.fillRect(0, 0, w, RULER_H);
  ctx.strokeStyle = 'rgba(255,255,255,0.09)';
  ctx.beginPath(); ctx.moveTo(0, RULER_H + 0.5); ctx.lineTo(w, RULER_H + 0.5); ctx.stroke();

  const step = chooseTickStep(span, w);
  const firstTick = Math.ceil(view[0] / step) * step;
  const dec = step < 0.1 ? 2 : (step < 1 ? 1 : 0);
  ctx.font = '10px ui-monospace, Consolas, monospace';
  for (let t = firstTick; t <= view[1] + 1e-6; t += step) {
    const x = Math.round(xOf(t)) + 0.5;
    ctx.strokeStyle = 'rgba(255,255,255,0.05)';
    ctx.beginPath(); ctx.moveTo(x, RULER_H); ctx.lineTo(x, h); ctx.stroke();
    ctx.strokeStyle = 'rgba(255,255,255,0.28)';
    ctx.beginPath(); ctx.moveTo(x, RULER_H - 6); ctx.lineTo(x, RULER_H); ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,0.52)';
    ctx.fillText(t.toFixed(dec) + 's', x + 3, 13);
  }

  // Selection band
  const x0 = xOf(sel[0]), x1 = xOf(sel[1]);
  if (sel[1] > sel[0]) {
    ctx.fillStyle = 'rgba(56,189,248,0.13)';
    ctx.fillRect(x0, RULER_H, Math.max(1, x1 - x0), h - RULER_H);
    ctx.strokeStyle = 'rgba(56,189,248,0.85)';
    ctx.beginPath();
    ctx.moveTo(x0, RULER_H); ctx.lineTo(x0, h);
    ctx.moveTo(x1, RULER_H); ctx.lineTo(x1, h);
    ctx.stroke();
  }

  // Per-key tracks (+ optional bottom Mouse lane)
  const keys = data.keys || [], n = keys.length;
  const hasMouseLane = !!(showMouse && data.mouse && data.mouse.x && data.mouse.x.length);
  const MOUSE_H = hasMouseLane ? (n ? 34 : (h - RULER_H - 6)) : 0;
  const keyAreaH = h - RULER_H - 6 - MOUSE_H;
  const rowH = keyAreaH / Math.max(1, n);
  const trackCols = ['#38bdf8', '#34d399', '#fbbf24', '#f472b6', '#a78bfa', '#fb7185'];
  keys.forEach((k, row) => {
    const on = selectedKeys.has(k);
    const y = RULER_H + 3 + row * rowH, base = y + rowH - 3;
    ctx.fillStyle = on ? 'rgba(255,255,255,0.78)' : 'rgba(255,255,255,0.24)';
    ctx.fillText(disp(norm(k)), 5, y + 10);
    ctx.strokeStyle = 'rgba(255,255,255,0.04)';
    ctx.beginPath(); ctx.moveTo(0, base); ctx.lineTo(w, base); ctx.stroke();
    const vals = data.v[k] || [];
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < vals.length; i++) {
      const t = data.t[i];
      if (t < view[0] || t > view[1]) continue;
      const x = xOf(t);
      const yv = base - Math.min(1, vals[i] / FULL) * Math.max(6, rowH - 11);
      if (!started) { ctx.moveTo(x, yv); started = true; } else ctx.lineTo(x, yv);
    }
    ctx.strokeStyle = on ? trackCols[row % trackCols.length] : 'rgba(255,255,255,0.16)';
    ctx.lineWidth = on ? 1.35 : 0.8;
    ctx.stroke();
  });

  if (hasMouseLane) {
    const m = data.mouse, my = RULER_H + 3 + keyAreaH, mBase = h - 4, mH = Math.max(12, MOUSE_H - 12);
    ctx.strokeStyle = 'rgba(255,255,255,0.11)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, my + 0.5); ctx.lineTo(w, my + 0.5); ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,0.72)';
    ctx.fillText('Mouse', 5, my + 11);
    const spds = m.spd || [], bs = m.b || [], ws = m.w || [];
    const nM = Math.min(data.t.length, spds.length);
    let maxS = 1.2;
    for (let i = 0; i < nM; i++) if (spds[i] > maxS) maxS = spds[i];
    maxS = Math.min(maxS, 5.0);
    ctx.beginPath();
    let started = false, firstX = 0, lastX = 0;
    for (let i = 0; i < nM; i++) {
      const t = data.t[i];
      if (t < view[0] || t > view[1]) continue;
      const x = xOf(t);
      const yv = mBase - Math.min(1, spds[i] / maxS) * mH;
      if (!started) { ctx.moveTo(x, yv); firstX = x; started = true; } else ctx.lineTo(x, yv);
      lastX = x;
    }
    if (started) {
      ctx.strokeStyle = 'rgba(56,189,248,0.82)';
      ctx.lineWidth = 1.25;
      ctx.stroke();
      ctx.lineTo(lastX, mBase); ctx.lineTo(firstX, mBase); ctx.closePath();
      ctx.fillStyle = 'rgba(56,189,248,0.14)';
      ctx.fill();
    }
    const clicks = m.clicks, wheels = m.wheels;
    if (Array.isArray(clicks)) {
      for (let i = 0; i < clicks.length; i++) {
        const [ct, bit, durMs] = clicks[i];
        const ctEnd = ct + (durMs || 0) / 1000;
        if (ctEnd < view[0] || ct > view[1]) continue;
        const x0m = xOf(ct), bw = Math.max(2, xOf(ctEnd) - x0m);
        if (bit === 1) { ctx.fillStyle = 'rgba(244,63,94,0.84)'; ctx.fillRect(x0m, my + 2, bw, 5); }
        else if (bit === 2) { ctx.fillStyle = 'rgba(251,191,36,0.84)'; ctx.fillRect(x0m, my + 8, bw, 4); }
        else { ctx.fillStyle = 'rgba(52,211,153,0.84)'; ctx.fillRect(x0m, my + 13, bw, 3); }
      }
      if (Array.isArray(wheels)) {
        ctx.fillStyle = 'rgba(125,211,252,0.88)';
        for (let i = 0; i < wheels.length; i++) {
          const tw = wheels[i][0];
          if (tw < view[0] || tw > view[1]) continue;
          ctx.fillRect(xOf(tw), my + 13, 2, 4);
        }
      }
    } else {
      for (let i = 0; i < nM; i++) {
        const t = data.t[i];
        if (t < view[0] || t > view[1]) continue;
        const b = bs[i] || 0, wh = ws[i] || 0;
        if (!b && !wh) continue;
        const x0m = xOf(t), x1m = i + 1 < nM ? xOf(data.t[i + 1]) : x0m + 2;
        const bw = Math.max(2, x1m - x0m);
        if (b & 1) { ctx.fillStyle = 'rgba(244,63,94,0.78)'; ctx.fillRect(x0m, my + 2, bw, 5); }
        if (b & 2) { ctx.fillStyle = 'rgba(251,191,36,0.78)'; ctx.fillRect(x0m, my + 8, bw, 4); }
        if ((b & 28) || wh) { ctx.fillStyle = 'rgba(52,211,153,0.78)'; ctx.fillRect(x0m, my + 13, bw, 3); }
      }
    }
  }

  // Playhead + Hover cursor
  if (playT >= view[0] && playT <= view[1]) {
    const px = Math.round(xOf(playT)) + 0.5;
    ctx.strokeStyle = playing ? '#f43f5e' : 'rgba(244,63,94,0.55)';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
  }
  if (hoverT !== null && hoverT >= view[0] && hoverT <= view[1]) {
    const hx = Math.round(xOf(hoverT)) + 0.5;
    ctx.strokeStyle = 'rgba(255,255,255,0.65)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(hx, RULER_H); ctx.lineTo(hx, h); ctx.stroke();
    ctx.setLineDash([]);
  }
}

function setSel(a, b, skipInputs) {
  const dur = (data && data.ok) ? data.duration : Math.max(a, b, 0);
  const lo = Math.max(0, Math.min(dur, Math.min(a, b)));
  const hi = Math.max(0, Math.min(dur, Math.max(a, b)));
  sel = [lo, hi];
  if (!skipInputs) {
    document.getElementById('sel_start').value = fmt(sel[0]);
    document.getElementById('sel_end').value = fmt(sel[1]);
  }
  document.getElementById('sel_len').textContent = fmt(sel[1] - sel[0]);
  if (playT < sel[0] || playT > sel[1]) playT = sel[0];
  draw();
  renderPreviewAt(activeTime());
}

function syncSelFromInputs(normalizeFields) {
  const aEl = document.getElementById('sel_start'), bEl = document.getElementById('sel_end');
  const a = parseFloat(aEl.value), b = parseFloat(bEl.value);
  if (isNaN(a) || isNaN(b)) return;
  setSel(a, b, !normalizeFields);
}
['sel_start', 'sel_end'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('input', () => syncSelFromInputs(false));
  el.addEventListener('change', () => syncSelFromInputs(true));
  el.addEventListener('keydown', e => { if (e.key === 'Enter') { syncSelFromInputs(true); el.blur(); } });
});

cv.addEventListener('mousedown', e => {
  dragging = true;
  dragFrom = Math.max(0, tOf(e.offsetX));
  playT = dragFrom;
  setSel(dragFrom, dragFrom);
});
cv.addEventListener('mousemove', e => {
  const t = Math.max(0, Math.min(data && data.ok ? data.duration : 0, tOf(e.offsetX)));
  hoverT = t;
  if (dragging) setSel(dragFrom, t);
  else { draw(); renderPreviewAt(t); }
});
cv.addEventListener('mouseleave', () => {
  hoverT = null;
  draw();
  renderPreviewAt(playT);
});
window.addEventListener('mouseup', () => { dragging = false; });
cv.addEventListener('dblclick', () => setSel(0, 0));
cv.addEventListener('wheel', e => {
  e.preventDefault();
  const dur = (data && data.ok) ? data.duration : 1;
  const z = e.deltaY > 0 ? 1.25 : 0.8;
  const pivot = Math.max(0, Math.min(dur, tOf(e.offsetX)));
  const a = Math.max(0, pivot - (pivot - view[0]) * z);
  const b = Math.min(dur, pivot + (view[1] - pivot) * z);
  if (b - a >= 0.05) view = [a, b];
  draw();
}, { passive: false });

function zoom(f) {
  const dur = (data && data.ok) ? data.duration : 1;
  const mid = (view[0] + view[1]) / 2, half = Math.max(0.05, (view[1] - view[0]) / 2 * f);
  view = [Math.max(0, mid - half), Math.min(dur, mid + half)];
  draw();
}
function resetView() { view = [0, data && data.ok ? data.duration : 1]; draw(); }

function buildPreviewKb() {
  const lay = document.getElementById('layout').value || 'hud';
  const items = CLUSTERS[lay] || CLUSTERS.hud;
  const host = document.getElementById('kbPrev');
  const stage = document.getElementById('prevStage');
  let maxX = 0, maxR = 0;
  for (const it of items) {
    if (it.x + it.w > maxX) maxX = it.x + it.w;
    if (it.r > maxR) maxR = it.r;
  }
  const stW = stage ? stage.clientWidth : 800;
  const padReserve = (showMouse && stW > 560) ? 252 : 0;
  const availW = Math.max(240, stW - 24 - padReserve);
  const baseU = lay === 'full' ? 38 : 50;
  const u = Math.max(24, Math.min(baseU, Math.floor(availW / (maxX + 0.2))));
  const gap = Math.max(4, Math.round(u * 0.11));
  document.documentElement.style.setProperty('--u', u + 'px');
  host.style.width = Math.max(1, Math.round(maxX * u - gap)) + 'px';
  host.style.height = Math.max(1, Math.round((maxR + 1) * (u + gap) - gap)) + 'px';
  host.innerHTML = '';
  prevEls = {};
  for (const it of items) {
    const d = document.createElement('div');
    d.className = 'k';
    d.style.left = Math.round(it.x * u) + 'px';
    d.style.top = Math.round(it.r * (u + gap)) + 'px';
    d.style.width = Math.max(12, Math.round(it.w * u - gap)) + 'px';
    d.style.height = Math.max(12, Math.round(u - gap)) + 'px';
    d.innerHTML = '<div class="fill"></div><div class="lbl">' + disp(it.k) + '</div><div class="mm num"></div>';
    host.appendChild(d);
    prevEls[it.k] = { box: d, fill: d.children[0], mm: d.children[2] };
  }
  renderPreviewAt(activeTime());
}

function toggleWbMouse() {
  const cb = document.getElementById('showMouse');
  showMouse = cb ? cb.checked : true;
  const chip = document.getElementById('showMouseChip');
  if (chip) chip.classList.toggle('on', showMouse);
  const wrap = document.getElementById('wbPadWrap');
  if (wrap) wrap.classList.toggle('off', !showMouse);
  buildPreviewKb();
  draw();
}

function renderMousePreviewAt(t) {
  const pad = document.getElementById('wbPad');
  const mEl = document.getElementById('wbMouse');
  const emptyEl = document.getElementById('wbMouseEmpty');
  if (!pad || !mEl) return;
  const preset = FILL_PRESETS[document.getElementById('theme').value] || FILL_PRESETS.ramp;
  const acc = preset.style(0.78).line || '#38bdf8';
  pad.style.setProperty('--acc', acc);
  if (!showMouse) return;
  const ms = sampleMouseAt(t);
  const hasData = !!ms;
  if (emptyEl) emptyEl.classList.toggle('on', !hasData);
  mEl.style.display = hasData ? '' : 'none';
  const PW = 220, PH = 182, MW = 32, MH = 60, IN = 7;
  if (wbTrailCv && wbTrailCtx) {
    const dpr = window.devicePixelRatio || 1;
    if (wbTrailCv.width !== Math.round(PW * dpr) || wbTrailCv.height !== Math.round(PH * dpr)) {
      wbTrailCv.width = Math.round(PW * dpr); wbTrailCv.height = Math.round(PH * dpr);
    }
    wbTrailCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    wbTrailCtx.clearRect(0, 0, PW, PH);
  }
  if (!hasData) return;
  const toXY = (nx, ny) => [
    IN + MW / 2 + (nx + 1) * 0.5 * (PW - MW - IN * 2),
    IN + MH / 2 + (ny + 1) * 0.5 * (PH - MH - IN * 2)
  ];
  const [cx, cy] = toXY(ms.x, ms.y);
  mEl.style.transform = 'translate(' + (cx - MW / 2).toFixed(1) + 'px,' + (cy - MH / 2).toFixed(1) + 'px)';
  const b = ms.b || 0;
  document.getElementById('wbMbL').classList.toggle('on', !!(b & 1));
  document.getElementById('wbMbR').classList.toggle('on', !!(b & 2));
  document.getElementById('wbMbM').classList.toggle('on', !!((b & 4) || ms.w !== 0));
  document.getElementById('wbMbX1').classList.toggle('on', !!(b & 8));
  document.getElementById('wbMbX2').classList.toggle('on', !!(b & 16));
  document.getElementById('wbMbLed').classList.toggle('hot', b !== 0 || ms.spd > 0.25);
  if (wbTrailCtx && data.t && data.mouse) {
    const ts = data.t, mx = data.mouse.x, my = data.mouse.y, win = 0.55;
    let k0 = ms.idx;
    while (k0 > 0 && (t - ts[k0 - 1]) <= win) k0--;
    let prev = null;
    wbTrailCtx.lineCap = 'round';
    for (let k = k0; k <= ms.idx; k++) {
      const p = toXY(mx[k], my[k]);
      const age = Math.max(0, t - ts[k]);
      const a = Math.max(0, 1 - age / win);
      if (prev) {
        wbTrailCtx.strokeStyle = acc;
        wbTrailCtx.globalAlpha = a * 0.65;
        wbTrailCtx.lineWidth = 1.2 + a * 1.4;
        wbTrailCtx.beginPath(); wbTrailCtx.moveTo(prev[0], prev[1]); wbTrailCtx.lineTo(p[0], p[1]); wbTrailCtx.stroke();
      }
      prev = p;
    }
    if (prev) {
      wbTrailCtx.strokeStyle = acc;
      wbTrailCtx.globalAlpha = 0.72;
      wbTrailCtx.lineWidth = 2.4;
      wbTrailCtx.beginPath(); wbTrailCtx.moveTo(prev[0], prev[1]); wbTrailCtx.lineTo(cx, cy); wbTrailCtx.stroke();
    }
    wbTrailCtx.globalAlpha = 1;
  }
}

function renderPreviewAt(t) {
  const curEl = document.getElementById('cur_t');
  if (curEl) curEl.textContent = fmt(t) + ' s';
  const vals = sampleAt(t);
  const preset = FILL_PRESETS[document.getElementById('theme').value] || FILL_PRESETS.ramp;
  const selectedNorm = new Set(Array.from(selectedKeys).map(norm));
  const hasSel = selectedKeys.size > 0;

  for (const name in prevEls) {
    const el = prevEls[name];
    const inSel = !hasSel || selectedNorm.has(name);
    const mm = inSel ? (vals[name] || 0) : 0;
    const r = Math.max(0, Math.min(1, mm / FULL));
    const hot = r > 0.06, active = r > 0.04;
    const st = preset.style(r);
    el.box.classList.toggle('dim', hasSel && !inSel);
    el.box.classList.toggle('hot', hot);
    el.fill.style.height = (r * 100).toFixed(1) + '%';
    el.fill.style.background = active ? st.bg : 'transparent';
    el.fill.style.borderTop = active ? ('1.5px solid ' + st.line) : 'none';
    el.box.style.transform = active ? ('scale(' + (1 - r * 0.035).toFixed(3) + ')') : '';
    el.mm.textContent = r > 0.08 ? mm.toFixed(1) : '';
  }

  renderMousePreviewAt(t);

  const hr = document.getElementById('hoverRead');
  if (hr && data && data.ok) {
    const activeList = data.keys
      .filter(k => selectedKeys.has(k) && (vals[k] || 0) > 0.03)
      .sort((a, b) => (vals[b] || 0) - (vals[a] || 0));
    const chips = activeList.map(k => '<span class="hk">' + disp(norm(k)) + ' ' + (vals[k] || 0).toFixed(2) + 'mm</span>');
    const ms = showMouse ? sampleMouseAt(t) : null;
    if (ms) {
      const b = ms.b || 0;
      if (b & 1) chips.push('<span class="hk m">LMB</span>');
      if (b & 2) chips.push('<span class="hk m">RMB</span>');
      if (b & 4) chips.push('<span class="hk m">MMB</span>');
      if (b & 8) chips.push('<span class="hk m">X1</span>');
      if (b & 16) chips.push('<span class="hk m">X2</span>');
      if (ms.w > 0) chips.push('<span class="hk m">滚轮↑' + (ms.w > 1 ? ('×' + ms.w) : '') + '</span>');
      if (ms.w < 0) chips.push('<span class="hk m">滚轮↓' + (ms.w < -1 ? ('×' + (-ms.w)) : '') + '</span>');
    }
    hr.innerHTML = chips.length ? chips.join('') : '<span style="color:var(--dim)">当前时刻无按下键</span>';
  }
}

function togglePlay() {
  if (!data || !data.ok) return;
  playing = !playing;
  const btn = document.getElementById('playBtn');
  if (btn) {
    btn.classList.toggle('on', playing);
    btn.textContent = playing ? '⏸ 暂停试播' : '▶ 试播选区';
  }
  if (playing) {
    const lo = sel[1] > sel[0] ? sel[0] : 0;
    const hi = sel[1] > sel[0] ? sel[1] : data.duration;
    if (playT < lo || playT >= hi) playT = lo;
    playLastMs = performance.now();
    requestAnimationFrame(playLoop);
  }
}

function playLoop(now) {
  if (!playing || !data || !data.ok) return;
  const dt = Math.min(0.1, (now - playLastMs) / 1000);
  playLastMs = now;
  const spd = Number(document.getElementById('speed').value) || 1;
  const lo = sel[1] > sel[0] ? sel[0] : 0;
  const hi = sel[1] > sel[0] ? sel[1] : data.duration;
  playT += dt * spd;
  if (playT > hi) playT = lo;
  draw();
  if (hoverT === null) renderPreviewAt(playT);
  requestAnimationFrame(playLoop);
}

function renderKeyChips() {
  const box = document.getElementById('keyChips');
  if (!box) return;
  if (!info || !info.ok || !(info.keys || []).length) {
    box.innerHTML = '<span class="sub">无可用键位</span>';
    return;
  }
  box.innerHTML = '';
  for (const k of info.keys) {
    const lbl = document.createElement('label');
    const checked = selectedKeys.has(k);
    lbl.className = 'kchip' + (checked ? ' on' : '');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = checked;
    cb.addEventListener('change', () => {
      if (cb.checked) selectedKeys.add(k); else selectedKeys.delete(k);
      lbl.classList.toggle('on', cb.checked);
      draw();
      renderPreviewAt(activeTime());
    });
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(disp(norm(k))));
    box.appendChild(lbl);
  }
}

function selectKeysPreset(mode) {
  if (!info || !info.ok) return;
  selectedKeys.clear();
  if (mode === 'all') {
    for (const k of info.keys) selectedKeys.add(k);
  } else if (mode === 'hud') {
    for (const k of info.keys) if (HUD_DEFAULT_KEYS.has(norm(k))) selectedKeys.add(k);
    if (!selectedKeys.size) for (const k of info.keys) selectedKeys.add(k);
  }
  renderKeyChips();
  draw();
  renderPreviewAt(activeTime());
}

let historyItems = [];
let currentStamp = new URLSearchParams(location.search).get('stamp') || '';

function toggleHist(force) {
  const d = document.getElementById('histDrawer');
  if (!d) return;
  const open = typeof force === 'boolean' ? force : !d.classList.contains('open');
  d.classList.toggle('open', open);
}

function renderHistoryUI() {
  const selEl = document.getElementById('recSel');
  const listEl = document.getElementById('histList');
  const cntEl = document.getElementById('histCount');
  if (cntEl) cntEl.textContent = String(historyItems.length);
  if (selEl) {
    selEl.innerHTML = '';
    if (!historyItems.length) {
      const opt = document.createElement('option');
      opt.value = ''; opt.textContent = '暂无录制';
      selEl.appendChild(opt);
    } else {
      for (const it of historyItems) {
        const opt = document.createElement('option');
        opt.value = it.stamp;
        opt.textContent = it.stamp + ' · ' + (it.duration || 0).toFixed(1) + 's' + (it.has_mouse ? ' [键鼠]' : '') + (it.is_latest ? ' [最新]' : '');
        if (it.stamp === currentStamp) opt.selected = true;
        selEl.appendChild(opt);
      }
    }
  }
  if (listEl) {
    listEl.innerHTML = '';
    if (!historyItems.length) {
      listEl.innerHTML = '<span class="sub">暂无历史录制</span>';
      return;
    }
    for (const it of historyItems) {
      const row = document.createElement('div');
      row.className = 'hist-item' + (it.stamp === currentStamp ? ' on' : '');
      row.addEventListener('click', () => { onSelectRec(it.stamp); });
      const meta = document.createElement('div');
      meta.className = 'hist-meta';
      const ks = (it.keys || []).map(k => disp(norm(k))).join(' ');
      meta.innerHTML = '<b class="num">' + it.stamp + '</b>' +
        (it.is_latest ? '<span class="hist-tag">最新</span>' : '') +
        (it.has_mouse ? '<span class="hist-tag m">键鼠</span>' : '') +
        '<span class="num" style="color:var(--dim)">' + (it.duration || 0).toFixed(2) + 's · ' + it.samples + ' 点</span>' +
        '<span class="hist-keys">' + (ks || '—') + '</span>';
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger';
      del.textContent = '删除';
      del.addEventListener('click', ev => { ev.stopPropagation(); deleteRecording(it.stamp); });
      row.appendChild(meta);
      row.appendChild(del);
      listEl.appendChild(row);
    }
  }
}

function onSelectRec(stamp) {
  if (!stamp) return;
  currentStamp = stamp;
  try { history.replaceState(null, '', '/wb?stamp=' + encodeURIComponent(stamp)); } catch (e) {}
  renderHistoryUI();
  load(stamp);
}

function deleteRecording(stamp) {
  if (!confirm('确认永久删除录制 ' + stamp + '？')) return;
  fetch('/wb/delete', {
    method: 'POST',
    headers: { 'X-Depth-Token': window.__token || '' },
    body: JSON.stringify({ stamp })
  }).then(r => r.json()).then(j => {
    document.getElementById('log').textContent = j.message || '';
    if (j.ok) {
      if (currentStamp === stamp) currentStamp = '';
      loadList(currentStamp);
    }
  });
}

function loadList(preferredStamp) {
  fetch('/wb/list').then(r => r.json()).then(j => {
    historyItems = (j && j.ok && j.items) ? j.items : [];
    const exists = s => historyItems.some(it => it.stamp === s);
    if (preferredStamp && exists(preferredStamp)) currentStamp = preferredStamp;
    else if (currentStamp && exists(currentStamp)) { /* keep */ }
    else currentStamp = historyItems.length ? historyItems[0].stamp : '';
    renderHistoryUI();
    load(currentStamp);
  }).catch(() => { load(currentStamp); });
}

function load(stamp) {
  if (playing) togglePlay();
  const qs = stamp ? ('?stamp=' + encodeURIComponent(stamp)) : '';
  fetch('/wb/info' + qs).then(r => r.json()).then(j => {
    info = j;
    if (!j.ok) {
      document.getElementById('r_stamp').textContent = '—';
      document.getElementById('r_dur').textContent = '—';
      document.getElementById('r_n').textContent = '—';
      document.getElementById('log').textContent = j.error || '没有录制';
      data = j; selectedKeys.clear(); renderKeyChips(); draw(); buildPreviewKb(); return;
    }
    currentStamp = j.stamp || stamp || '';
    document.getElementById('r_stamp').textContent = currentStamp || '—';
    document.getElementById('r_dur').textContent = (j.duration || 0).toFixed(2) + ' s';
    document.getElementById('r_n').textContent = j.samples;
    renderHistoryUI();
    selectedKeys = new Set(j.keys || []);
    renderKeyChips();
    const sq = '?step=0.02' + (currentStamp ? ('&stamp=' + encodeURIComponent(currentStamp)) : '');
    fetch('/wb/series' + sq).then(r => r.json()).then(k => {
      data = k;
      if (k.ok) {
        view = [0, k.duration];
        playT = 0;
        hoverT = null;
        setSel(0, Math.min(k.duration, 5));
      }
      buildPreviewKb();
      draw();
    });
  });
}

function onWbFullChange(v) {
  setFullMm(v);
  const el = document.getElementById('wbFull');
  if (el) el.value = FULL.toFixed(2);
  draw();
  renderPreviewAt(activeTime());
}

function startExport() {
  if (!data || !data.ok) { alert('还没有可导出的录制'); return; }
  if (!(sel[1] > sel[0])) { alert('请先在时间轴上拖出一段区间'); return; }
  const picked = (info.keys || []).filter(k => selectedKeys.has(k));
  if (!picked.length) { alert('请至少勾选一个导出键位'); return; }
  const params = {
    stamp: currentStamp || undefined,
    start: sel[0], end: sel[1],
    layout: document.getElementById('layout').value,
    theme: document.getElementById('theme').value,
    full_mm: FULL,
    size: document.getElementById('size').value,
    fps: Number(document.getElementById('fps').value),
    speed: Number(document.getElementById('speed').value),
    bg: document.getElementById('bg').value,
    png: document.getElementById('fmt').value === 'png',
    keys: picked
  };
  const tok = window.__token || '';
  fetch('/wb/export', { method:'POST', headers:{'X-Depth-Token': tok}, body: JSON.stringify(params) })
    .then(r => r.json())
    .then(j => { document.getElementById('log').textContent = j.message || ''; poll(); });
}

function poll() {
  fetch('/wb/status').then(r => r.json()).then(j => {
    document.querySelector('#prog i').style.width = (j.pct || 0) + '%';
    let text = (j.msg || '') + (j.running ? (j.eta ? (' · 剩余约 ' + j.eta + ' s') : '') : '');
    if (j.out) text += '  |  输出：' + j.out;
    document.getElementById('log').textContent = text || '—';
    if (j.running) setTimeout(poll, 700);
  });
}

function cancelExport() {
  fetch('/wb/cancel', { method:'POST', headers:{'X-Depth-Token': window.__token || ''} }).then(() => poll());
}

fetch('/snapshot').then(r => r.json()).then(j => {
  window.__token = j.token || '';
  if (j.full_mm) onWbFullChange(j.full_mm);
  if (j.theme && FILL_PRESETS[j.theme]) {
    const tEl = document.getElementById('theme');
    if (tEl) tEl.value = j.theme;
    renderPreviewAt(activeTime());
  }
});
window.addEventListener('keydown', e => {
  const tag = (e.target && e.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.code === 'Space') {
    e.preventDefault();
    togglePlay();
  } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    if (!data || !data.ok) return;
    e.preventDefault();
    if (playing) togglePlay();
    const step = e.shiftKey ? 0.1 : 0.02;
    const dir = e.key === 'ArrowRight' ? 1 : -1;
    const lo = sel[1] > sel[0] ? sel[0] : 0;
    const hi = sel[1] > sel[0] ? sel[1] : (data.duration || 0);
    playT = Math.max(lo, Math.min(hi, playT + dir * step));
    hoverT = null;
    draw();
    renderPreviewAt(playT);
  }
});
addEventListener('resize', () => { fit(); buildPreviewKb(); });
buildPreviewKb();
loadList(currentStamp);
fit();
</script></body></html>"""
            .replace("__LAYOUT__", json.dumps(_layout_dicts(), ensure_ascii=False))
            .replace("__THEME_JS__", COMMON_THEME_JS))


srv_ref = [None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--auto", action="store_true", help="start recording immediately")
    ap.add_argument("--full", type=float, default=None,
                    help="full switch travel in mm (default 3.50, range 1.0-5.0)")
    ap.add_argument("--idle-exit", type=int, default=90,
                    help="exit after N seconds with no page open (0 = never)")
    args = ap.parse_args()
    if args.full is not None:
        ENGINE.update_settings(full_mm=args.full, save=False)

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
            (ENGINE.run_reader, ENGINE.run_keys, ENGINE.run_mouse, ENGINE.rate_tick)
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
