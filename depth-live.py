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
    """Locate kb-frames.py: next to the exe, then next to this file, then cwd."""
    here = []
    if getattr(sys, "frozen", False):
        here.append(os.path.dirname(os.path.abspath(sys.executable)))
    here.append(os.path.dirname(os.path.abspath(__file__)))
    here.append(os.getcwd())
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
    here.append(os.path.dirname(os.path.abspath(__file__)))
    here.append(os.getcwd())
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


def find_ffmpeg_bin(name="ffmpeg"):
    """Locate ffmpeg or ffprobe across PATH, standard Windows installs, or next to exe."""
    exe = name if name.lower().endswith(".exe") else (name + ".exe")
    cands = [shutil.which(name), shutil.which(exe)]
    here = []
    if getattr(sys, "frozen", False):
        here.append(os.path.dirname(os.path.abspath(sys.executable)))
    here.append(os.getcwd())
    here.append(os.path.dirname(os.path.abspath(__file__)))
    for d in here:
        cands.append(os.path.join(d, exe))
    home = os.path.expanduser("~")
    cands += [
        os.path.join(home, "ffmpeg", "bin", exe),
        os.path.join("C:\\", "ffmpeg", "bin", exe),
        os.path.join("F:\\", "tools", "ffmpeg", "bin", exe),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
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
VERSION = "1.19.0"
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD),
                ("hEvent", ctypes.c_void_p)]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
hid = ctypes.WinDLL("hid")
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.WriteFile.restype = wt.BOOL
kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
kernel32.GetOverlappedResult.restype = wt.BOOL
kernel32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(wt.DWORD), wt.BOOL]
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
user32.PeekMessageW.restype = wt.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.MsgWaitForMultipleObjects.restype = wt.DWORD
user32.MsgWaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostThreadMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetCursorPos.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.SystemParametersInfoW.restype = wt.BOOL
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wt.WCHAR * 32), ("dmSpecVersion", wt.WORD), ("dmDriverVersion", wt.WORD),
        ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD), ("dmFields", wt.DWORD),
        ("dmPositionX", wt.LONG), ("dmPositionY", wt.LONG), ("dmDisplayOrientation", wt.DWORD),
        ("dmDisplayFixedOutput", wt.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
        ("dmFormName", wt.WCHAR * 32), ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD),
        ("dmPelsWidth", wt.DWORD), ("dmPelsHeight", wt.DWORD), ("dmDisplayFlags", wt.DWORD),
        ("dmDisplayFrequency", wt.DWORD),
    ]


user32.EnumDisplaySettingsW.restype = wt.BOOL
user32.EnumDisplaySettingsW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(_DEVMODEW)]
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetCurrentProcess.restype = wt.HANDLE
advapi32.OpenProcessToken.restype = wt.BOOL
advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi32.GetTokenInformation.restype = wt.BOOL
advapi32.GetTokenInformation.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wt.DWORD)
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wt.DWORD]


def _token_is_elevated(proc_handle):
    tok = wt.HANDLE()
    if not advapi32.OpenProcessToken(proc_handle, 0x0008, ctypes.byref(tok)):
        return ctypes.get_last_error() == 5
    try:
        buf = (ctypes.c_ubyte * 256)()
        need = wt.DWORD(0)
        if not advapi32.GetTokenInformation(tok, 25, buf, 256, ctypes.byref(need)):
            return False
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents
        cnt_p = advapi32.GetSidSubAuthorityCount(sid)
        n = cnt_p.contents.value if cnt_p else 0
        if n <= 0:
            return False
        rid = advapi32.GetSidSubAuthority(sid, n - 1).contents.value
        return rid >= 0x3000
    finally:
        kernel32.CloseHandle(tok)


def is_self_elevated():
    try:
        return _token_is_elevated(kernel32.GetCurrentProcess())
    except Exception:
        return False


def is_foreground_elevated():
    """True when the foreground window belongs to a higher-integrity (admin) process."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ctypes.get_last_error() == 5
        try:
            return _token_is_elevated(h)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False


def win_mouse_speed_factor(speed):
    """Map Windows SPI_GETMOUSESPEED (1..20) to its cursor-to-raw multiplier."""
    s = max(1, min(20, int(speed)))
    if s == 1:
        return 1.0 / 32.0
    if s == 2:
        return 1.0 / 16.0
    if s <= 10:
        return (s - 2) / 8.0
    return 1.0 + (s - 10) * 0.25


def cursor_to_raw_scale():
    """Estimate raw mouse counts per logical GetCursorPos pixel from Windows speed & DPI."""
    spd = ctypes.c_int(10)
    try:
        if not user32.SystemParametersInfoW(0x0070, 0, ctypes.byref(spd), 0):
            spd.value = 10
    except Exception:
        spd.value = 10
    inv = 1.0 / win_mouse_speed_factor(spd.value)
    dpi_scale = 1.0
    try:
        lw = user32.GetSystemMetrics(0)
        dm = _DEVMODEW()
        dm.dmSize = ctypes.sizeof(_DEVMODEW)
        if lw > 0 and user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)) and dm.dmPelsWidth > 0:
            dpi_scale = max(1.0, min(4.0, float(dm.dmPelsWidth) / float(lw)))
    except Exception:
        dpi_scale = 1.0
    return max(0.25, min(32.0, inv * dpi_scale))


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
                "in_len": 0, "out_len": 0, "usage_page": 0, "usage": 0}
        pre = ctypes.c_void_p()
        caps = HIDP_CAPS()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(pre)):
            try:
                if hid.HidP_GetCaps(pre, ctypes.byref(caps)):
                    info["in_len"] = caps.InputReportByteLength
                    info["out_len"] = caps.OutputReportByteLength
                    info["usage_page"] = caps.UsagePage
                    info["usage"] = caps.Usage
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


def build_key_travel_notify_report(enable, out_len=65):
    """Build ROG Gear Link WebHID setKeyTravelNotify output report (ID 0x00 + 51 61 <01|00>)."""
    size = max(65, int(out_len or 65))
    buf = bytearray(size)
    buf[0] = 0x00
    buf[1] = 0x51
    buf[2] = 0x61
    buf[3] = 0x01 if enable else 0x00
    return bytes(buf)


def find_control_collection(vid=0x0B05, pid=0x1C7D):
    """Locate the vendor control HID collection (0xFF00:0x0001) with >=65B output reports."""
    cands = []
    for path in interface_paths():
        low = path.lower()
        if ("vid_%04x" % vid) not in low or ("pid_%04x" % pid) not in low:
            continue
        info = probe(path)
        if info and info.get("usage_page") == 0xFF00 and info.get("out_len", 0) >= 65:
            cands.append(info)
    for info in cands:
        if info.get("usage") == 0x0001:
            return info
    return cands[0] if cands else None


def _hid_xfer_overlapped(h, payload, in_len=65, timeout_ms=400):
    """Send an output report via overlapped WriteFile and drain an optional ACK reply."""
    ev_w = kernel32.CreateEventW(None, True, False, None)
    ev_r = kernel32.CreateEventW(None, True, False, None)
    try:
        if not ev_w:
            return False, b""
        ov_w = _OVERLAPPED()
        ov_w.hEvent = ev_w
        n_pay = len(payload)
        wbuf = (ctypes.c_ubyte * n_pay).from_buffer_copy(payload)
        nw = wt.DWORD(0)
        ok_w = bool(kernel32.WriteFile(h, wbuf, n_pay, ctypes.byref(nw), ctypes.byref(ov_w)))
        if not ok_w:
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                return False, b""
            if kernel32.WaitForSingleObject(ev_w, int(timeout_ms)) != WAIT_OBJECT_0:
                kernel32.CancelIoEx(h, ctypes.byref(ov_w))
                return False, b""
            if not kernel32.GetOverlappedResult(h, ctypes.byref(ov_w), ctypes.byref(nw), False):
                return False, b""
        if nw.value <= 0:
            return False, b""
        if not in_len or in_len <= 0 or not ev_r:
            return True, b""
        ov_r = _OVERLAPPED()
        ov_r.hEvent = ev_r
        rsize = max(65, int(in_len))
        rbuf = (ctypes.c_ubyte * rsize)()
        nr = wt.DWORD(0)
        ok_r = bool(kernel32.ReadFile(h, rbuf, rsize, ctypes.byref(nr), ctypes.byref(ov_r)))
        if not ok_r:
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                return True, b""
            if kernel32.WaitForSingleObject(ev_r, min(int(timeout_ms), 250)) != WAIT_OBJECT_0:
                kernel32.CancelIoEx(h, ctypes.byref(ov_r))
                return True, b""
            if not kernel32.GetOverlappedResult(h, ctypes.byref(ov_r), ctypes.byref(nr), False):
                return True, b""
        return True, bytes(rbuf[:nr.value])
    finally:
        if ev_w:
            kernel32.CloseHandle(ev_w)
        if ev_r:
            kernel32.CloseHandle(ev_r)


def set_key_travel_notify(enable, vid=0x0B05, pid=0x1C7D, timeout_ms=400):
    """Toggle ROG analog depth streaming (03 7E) without opening Gear Link Web."""
    if os.environ.get("DEPTH_MONITOR_NO_WAKE") == "1":
        return False, "disabled"
    try:
        info = find_control_collection(vid, pid)
        if not info:
            return False, "no-control-hid"
        payload = build_key_travel_notify_report(enable, info.get("out_len") or 65)
        h = kernel32.CreateFileW(
            info["path"], GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED, None,
        )
        if h == INVALID_HANDLE_VALUE:
            return False, "open-err-%d" % ctypes.get_last_error()
        try:
            ok, ack = _hid_xfer_overlapped(h, payload, info.get("in_len") or 65, timeout_ms)
            if not ok:
                return False, "write-err-%d" % ctypes.get_last_error()
            if ack[:4] == payload[:4]:
                return True, "ack"
            return True, "sent"
        finally:
            kernel32.CloseHandle(h)
    except Exception as exc:
        return False, "exc-%s" % exc


def decode(report):
    if len(report) < 6 or report[0] != 0x03:
        return None
    if report[1] == 0x7E:
        return ("depth", report[2], (report[4] | (report[5] << 8)) * 0.01)
    if report[1] == 0x71:
        return ("event", report[2], report[3])
    return None


MOUSE_HUD_DEFAULTS = {
    "pad": "gsr3",
    "theme": "ramp",
    "scale": 1.0,
    "opacity": 0.62,
    "mopacity": 0.90,
    "sens": 1.0,
    "recenter": "smart",
    "align": "center",
    "trail": True,
}


def _sanitize_mouse_hud(raw, base=None):
    out = dict(base if base is not None else MOUSE_HUD_DEFAULTS)
    if not isinstance(raw, dict):
        return out
    if "pad" in raw:
        v = str(raw["pad"]).strip().lower()
        if v in ("gsr3", "sq", "wide", "xl"):
            out["pad"] = v
    if "theme" in raw:
        v = str(raw["theme"]).strip().lower()
        if v in ("ramp", "cyan", "mint", "amber", "crimson", "violet"):
            out["theme"] = v
    for key, lo, hi in (("scale", 0.5, 2.5), ("opacity", 0.1, 0.95),
                        ("mopacity", 0.15, 1.0), ("sens", 0.2, 4.0)):
        if key in raw:
            try:
                v = float(raw[key])
                if lo <= v <= hi:
                    out[key] = round(v, 2)
            except (TypeError, ValueError):
                pass
    if "recenter" in raw:
        v = str(raw["recenter"]).strip().lower()
        if v == "gentle":
            v = "smart"
        if v in ("smart", "fast", "off"):
            out["recenter"] = v
    if "align" in raw:
        v = str(raw["align"]).strip().lower()
        if v in ("center", "tl", "tr", "bl", "br"):
            out["align"] = v
    if "trail" in raw:
        v = raw["trail"]
        if isinstance(v, bool):
            out["trail"] = v
        else:
            out["trail"] = str(v).strip().lower() not in ("0", "false", "off", "")
    return out


KB_HUD_DEFAULTS = {
    "cluster": "hud",
    "keys": "",
    "theme": "ramp",
    "full": 3.5,
    "scale": 1.0,
    "opacity": 0.85,
    "mm": "1",
    "align": "center",
}


def _sanitize_kb_hud(raw, base=None):
    out = dict(base if base is not None else KB_HUD_DEFAULTS)
    if not isinstance(raw, dict):
        return out
    if "cluster" in raw:
        v = str(raw["cluster"]).strip().lower()
        if v == "layout":
            v = "hud"
        if v in ("hud", "wasd", "full"):
            out["cluster"] = v
    if "keys" in raw:
        out["keys"] = str(raw["keys"] or "").strip()[:400]
    if "theme" in raw:
        v = str(raw["theme"]).strip().lower()
        if v in ("ramp", "cyan", "mint", "amber", "crimson", "violet"):
            out["theme"] = v
    for key, lo, hi in (("full", 1.0, 5.0), ("scale", 0.4, 3.0), ("opacity", 0.1, 1.0)):
        if key in raw:
            try:
                v = float(raw[key])
                if lo <= v <= hi:
                    out[key] = round(v, 2)
            except (TypeError, ValueError):
                pass
    if "mm" in raw:
        v = str(raw["mm"]).strip().lower()
        out["mm"] = ("0" if v in ("0", "false", "off")
                     else ("always" if v in ("always", "2") else "1"))
    if "align" in raw:
        v = str(raw["align"]).strip().lower()
        if v in ("center", "tl", "tr", "bl", "br"):
            out["align"] = v
    return out


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


WB_STAGE_DEFAULTS = {
    "kb": {"x": 0.03, "y": 0.66, "scale": 0.85, "alpha": 0.72},
    "mouse": {"x": 0.76, "y": 0.64, "scale": 0.90, "alpha": 0.55, "mopacity": 0.85},
}


def _sanitize_wb_stage(raw, base=None):
    b = base if isinstance(base, dict) else WB_STAGE_DEFAULTS
    out = {
        "kb": dict(b.get("kb", WB_STAGE_DEFAULTS["kb"])),
        "mouse": dict(b.get("mouse", WB_STAGE_DEFAULTS["mouse"])),
    }
    if not isinstance(raw, dict):
        return out
    for part, keys in (("kb", (("x", 0.0, 0.95), ("y", 0.0, 0.95), ("scale", 0.35, 2.5), ("alpha", 0.1, 1.0))),
                       ("mouse", (("x", 0.0, 0.95), ("y", 0.0, 0.95), ("scale", 0.35, 2.5), ("alpha", 0.1, 0.95), ("mopacity", 0.15, 1.0)))):
        src = raw.get(part)
        if not isinstance(src, dict):
            continue
        for k, lo, hi in keys:
            if k in src:
                try:
                    v = float(src[k])
                    if lo <= v <= hi:
                        out[part][k] = round(v, 4 if k in ("x", "y") else 2)
                except (TypeError, ValueError):
                    pass
    return out


def parse_filename_wall_epoch(name):
    """Extract local wall-clock epoch from ShadowPlay/OBS/compact video filenames."""
    import re
    base = os.path.basename(name or "")
    pats = (
        r"(20\d{2})[.\-_](\d{2})[.\-_](\d{2})\s*[-_ ]\s*(\d{2})[.\-_:](\d{2})[.\-_:](\d{2})",
        r"(20\d{2})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})",
    )
    for pat in pats:
        m = re.search(pat, base)
        if m:
            try:
                vals = [int(x) for x in m.groups()]
                return time.mktime((vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], 0, 0, -1))
            except Exception:
                pass
    return None


def _parse_iso_utc_epoch(text):
    import calendar, re
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", (text or "").strip())
    if not m:
        return None
    try:
        vals = [int(x) for x in m.groups()]
        return float(calendar.timegm((vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], 0, 0, 0)))
    except Exception:
        return None


def pick_video_file_win32(initial_dir=None):
    """Open native Windows file picker for local recordings without changing CWD."""
    try:
        comdlg = ctypes.WinDLL("comdlg32")
    except Exception:
        return None
    class _OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wt.DWORD), ("hwndOwner", wt.HWND), ("hInstance", wt.HINSTANCE),
            ("lpstrFilter", wt.LPCWSTR), ("lpstrCustomFilter", wt.LPWSTR),
            ("nMaxCustFilter", wt.DWORD), ("nFilterIndex", wt.DWORD),
            ("lpstrFile", wt.LPWSTR), ("nMaxFile", wt.DWORD),
            ("lpstrFileTitle", wt.LPWSTR), ("nMaxFileTitle", wt.DWORD),
            ("lpstrInitialDir", wt.LPCWSTR), ("lpstrTitle", wt.LPCWSTR),
            ("Flags", wt.DWORD), ("nFileOffset", wt.WORD), ("nFileExtension", wt.WORD),
            ("lpstrDefExt", wt.LPCWSTR), ("lCustData", wt.LPARAM),
            ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wt.LPCWSTR),
            ("pvReserved", ctypes.c_void_p), ("dwReserved", wt.DWORD), ("FlagsEx", wt.DWORD),
        ]
    buf = ctypes.create_unicode_buffer(1024)
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.lpstrFilter = "视频文件 (*.mp4;*.mkv;*.mov;*.webm;*.avi)\0*.mp4;*.mkv;*.mov;*.webm;*.avi;*.m4v\0所有文件 (*.*)\0*.*\0\0"
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, wt.LPWSTR)
    ofn.nMaxFile = 1024
    if initial_dir and os.path.isdir(initial_dir):
        ofn.lpstrInitialDir = initial_dir
    ofn.lpstrTitle = "选择游戏录像文件"
    ofn.Flags = 0x00001000 | 0x00000800 | 0x00000008  # FILEMUSTEXIST | PATHMUSTEXIST | NOCHANGEDIR
    if comdlg.GetOpenFileNameW(ctypes.byref(ofn)):
        return buf.value
    return None


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
        self._export_cancel = threading.Event()
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
        self.mouse_hud = dict(MOUSE_HUD_DEFAULTS)
        self.mouse_hud_rev = 0
        self.kb_hud = dict(KB_HUD_DEFAULTS)
        self.kb_hud_rev = 0
        self.mouse_events = []       # (dx, dy, wheel) live ring for SSE cursors
        self._mouse_abs = 0
        self.mouse_btns = {"l": False, "r": False, "m": False, "x1": False, "x2": False}
        self._last_raw_mouse_t = 0.0
        self.mouse_uipi_fallback = False
        self._cursor_raw_ratio = cursor_to_raw_scale()
        self.hid_wake = False
        self.hid_wake_msg = ""
        self._hid_wake_sent = False
        self._last_wake_t = 0.0
        self._wb_meta_cache = {}
        self._wb_mouse_cache = {}
        self._wb_analysis_cache = {}
        self.wb_video = None
        self.wb_stage = _sanitize_wb_stage(WB_STAGE_DEFAULTS)
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
            mh = raw.get("mouse_hud")
            if isinstance(mh, dict):
                self.mouse_hud = _sanitize_mouse_hud(mh, MOUSE_HUD_DEFAULTS)
                self.mouse_hud_rev = 1
            else:
                self.mouse_hud["theme"] = self.theme
            kh = raw.get("kb_hud")
            if isinstance(kh, dict):
                self.kb_hud = _sanitize_kb_hud(kh, KB_HUD_DEFAULTS)
                self.kb_hud_rev = 1
            else:
                self.kb_hud["theme"] = self.theme
                self.kb_hud["full"] = self.full_mm
            ws = raw.get("wb_stage")
            if isinstance(ws, dict):
                self.wb_stage = _sanitize_wb_stage(ws, WB_STAGE_DEFAULTS)
        except Exception:
            pass

    def update_settings(self, full_mm=None, theme=None, mouse_span=None, mouse_hud=None, kb_hud=None, wb_stage=None, save=True):
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
            if isinstance(mouse_hud, dict):
                self.mouse_hud = _sanitize_mouse_hud(mouse_hud, self.mouse_hud)
                self.mouse_hud_rev += 1
            if isinstance(kb_hud, dict):
                self.kb_hud = _sanitize_kb_hud(kb_hud, self.kb_hud)
                self.kb_hud_rev += 1
            if isinstance(wb_stage, dict):
                self.wb_stage = _sanitize_wb_stage(wb_stage, self.wb_stage)
            snap = {
                "full_mm": self.full_mm,
                "theme": self.theme,
                "mouse_span": self.mouse_span,
                "mouse_hud": dict(self.mouse_hud),
                "kb_hud": dict(self.kb_hud),
                "wb_stage": dict(self.wb_stage),
            }
            out = dict(snap)
            out["mouse_hud_rev"] = self.mouse_hud_rev
            out["kb_hud_rev"] = self.kb_hud_rev
        if save:
            try:
                self._write_atomic(self.settings_path, json.dumps(snap, ensure_ascii=False, indent=1))
            except Exception:
                pass
        return out

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

    def _ensure_hid_wake(self, force=False):
        now = time.perf_counter()
        if not force and (now - self._last_wake_t) < 5.0:
            return False
        self._last_wake_t = now
        ok, msg = set_key_travel_notify(True)
        with self.lock:
            self.hid_wake = bool(ok)
            self.hid_wake_msg = msg
            if ok:
                self._hid_wake_sent = True
        return ok

    def _disable_hid_wake(self):
        with self.lock:
            if not self._hid_wake_sent:
                return
            self._hid_wake_sent = False
        set_key_travel_notify(False)

    # ---- reader -----------------------------------------------------
    def run_reader(self):
        while not self.stop_io.is_set():
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
            self._ensure_hid_wake(force=True)
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
                if self.stop_io.is_set():
                    self._disable_hid_wake()
                try:
                    kernel32.CancelIoEx(h, None)
                except Exception:
                    pass
                try:
                    kernel32.CloseHandle(h)
                except Exception:
                    pass
            if not self.stop_io.is_set():
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

    def _poll_mouse_fallback(self, state, now=None):
        """Desktop cursor + button fallback when UIPI blocks WM_INPUT behind an elevated foreground app."""
        if now is None:
            now = time.perf_counter()
        state.setdefault("rx", 0.0)
        state.setdefault("ry", 0.0)
        if state["self_elev"] or (now - self._last_raw_mouse_t) < 0.05:
            self.mouse_uipi_fallback = False
            state["pt"] = None
            state["rx"] = 0.0
            state["ry"] = 0.0
            return
        if now - state["chk_t"] >= 0.25:
            state["chk_t"] = now
            state["elev"] = is_foreground_elevated()
        if not state["elev"]:
            self.mouse_uipi_fallback = False
            state["pt"] = None
            state["rx"] = 0.0
            state["ry"] = 0.0
            return
        self.mouse_uipi_fallback = True
        dx = dy = 0
        pt = wt.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            cur = (pt.x, pt.y)
            if state["pt"] is not None:
                dx_px = cur[0] - state["pt"][0]
                dy_px = cur[1] - state["pt"][1]
                if abs(dx_px) >= 800 or abs(dy_px) >= 800:
                    dx_px = dy_px = 0
                ratio = max(0.25, min(32.0, float(self._cursor_raw_ratio)))
                fx = state["rx"] + dx_px * ratio
                fy = state["ry"] + dy_px * ratio
                dx = int(round(fx))
                dy = int(round(fy))
                state["rx"] = fx - dx
                state["ry"] = fy - dy
            state["pt"] = cur
        btns = {}
        for name, vk in (("l", 0x01), ("r", 0x02), ("m", 0x04), ("x1", 0x05), ("x2", 0x06)):
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if self.mouse_btns.get(name) != down:
                btns[name] = down
        if dx or dy or btns:
            self.on_mouse(dx, dy, btns)

    def run_mouse(self):
        """Passive read-only Windows Raw Input mouse listener with UIPI desktop fallback."""
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
            fb_state = {"pt": None, "elev": False, "chk_t": 0.0, "self_elev": is_self_elevated(),
                        "rx": 0.0, "ry": 0.0, "scale_t": time.perf_counter()}
            while not self.stop_io.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, 1):
                    if msg.message == 0x0012:   # WM_QUIT
                        return
                    if msg.message == WM_INPUT:
                        sz.value = ctypes.sizeof(buf)
                        got = user32.GetRawInputData(
                            ctypes.c_void_p(msg.lParam), RID_INPUT,
                            buf, ctypes.byref(sz), hdr_sz,
                        )
                        if got and got >= ctypes.sizeof(RAWINPUT_MOUSE):
                            ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT_MOUSE)).contents
                            if ri.header.dwType == RIM_TYPEMOUSE:
                                self._last_raw_mouse_t = time.perf_counter()
                                self.mouse_uipi_fallback = False
                                fb_state["pt"] = None
                                fb_state["rx"] = 0.0
                                fb_state["ry"] = 0.0
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
                now_loop = time.perf_counter()
                if now_loop - fb_state["scale_t"] >= 2.0:
                    fb_state["scale_t"] = now_loop
                    self._cursor_raw_ratio = cursor_to_raw_scale()
                self._poll_mouse_fallback(fb_state, now_loop)
                user32.MsgWaitForMultipleObjects(0, None, False, 10, 0x04FF)
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
            now_wall = time.time()
            now_perf = time.perf_counter()
            end_candidates = [min_dur]
            if r_rows:
                end_candidates.append(max(0.0, r_rows[-1][0] - t0))
            rec_dur = max(end_candidates)
            t0_ep = now_wall - max(0.0, now_perf - t0)
            meta_obj = {"stamp": stamp, "t0_epoch": round(t0_ep, 3),
                        "t1_epoch": round(t0_ep + rec_dur, 3), "duration": round(rec_dur, 3)}
            self._write_atomic(os.path.join(version_dir, "meta.json"),
                               json.dumps(meta_obj, ensure_ascii=False, indent=1))
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
        self._disable_hid_wake()
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

    def _wb_file_sig(self, path):
        try:
            st = os.stat(path)
            return (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
        except Exception:
            return None

    def _read_wide_meta(self, wide_path):
        sig = self._wb_file_sig(wide_path)
        if sig is not None:
            cached = self._wb_meta_cache.get(wide_path)
            if cached and cached[0] == sig:
                return cached[1]
        keys, dur, samples = [], 0.0, 0
        with io.open(wide_path, "rb") as f:
            header_line = None
            while True:
                raw = f.readline()
                if not raw:
                    break
                if raw.startswith(b"#"):
                    continue
                header_line = raw.decode("utf-8", "replace").strip()
                break
            if header_line is None:
                return [], 0.0, 0
            header = [h.strip() for h in header_line.split(",")]
            keys = header[2:]
            data_off = f.tell()
            fsize = sig[1] if sig else os.path.getsize(wide_path)
            if fsize <= 512 * 1024:
                for raw in f:
                    s = raw.strip()
                    if not s or s.startswith(b"#"):
                        continue
                    c1 = s.find(b",")
                    if c1 > 0:
                        samples += 1
                        dur = float(s[:c1])
            else:
                first_raw = f.readline()
                t_first = 0.0
                if first_raw:
                    c1 = first_raw.find(b",")
                    if c1 > 0:
                        t_first = float(first_raw[:c1])
                f.seek(max(data_off, fsize - 8192))
                tail = f.read().splitlines()
                for raw in reversed(tail):
                    s = raw.strip()
                    if not s or s.startswith(b"#"):
                        continue
                    c1 = s.find(b",")
                    if c1 > 0:
                        dur = float(s[:c1])
                        break
                samples = max(1, int(round(max(0.0, dur - t_first) * 100.0)) + 1)
        res = (keys, dur, samples)
        if sig is not None:
            if len(self._wb_meta_cache) > 128:
                self._wb_meta_cache.clear()
            self._wb_meta_cache[wide_path] = (sig, res)
        return res

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
        for cache in (self._wb_meta_cache, self._wb_mouse_cache, self._wb_analysis_cache):
            for k in list(cache):
                kp = k[0] if isinstance(k, tuple) else k
                if isinstance(kp, str) and kp.startswith(target + os.sep):
                    cache.pop(k, None)
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

    def _load_mouse_events(self, wide_path):
        """Parse and cache (span, evts) from sibling mouse.csv keyed by file mtime+size."""
        mouse_p = os.path.join(os.path.dirname(wide_path), "mouse.csv")
        if not os.path.isfile(mouse_p):
            return None, None
        sig = self._wb_file_sig(mouse_p)
        if sig is not None:
            cached = self._wb_mouse_cache.get(mouse_p)
            if cached and cached[0] == sig:
                return cached[1]
        import re
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
            return None, None
        if not evts:
            return None, None
        if sig is not None:
            if len(self._wb_mouse_cache) >= 2:
                self._wb_mouse_cache.clear()
            self._wb_mouse_cache[mouse_p] = (sig, (span, evts))
        return span, evts

    def _load_wb_mouse(self, wide_path, times, start=None, end=None):
        """Integrate mouse.csv onto `times` with smart lift-off recenter for scrub-safe replay."""
        if not times:
            return None
        span, evts = self._load_mouse_events(wide_path)
        if not evts:
            return None
        import bisect, math
        xs, ys, bs, ws, spds = [], [], [], [], []
        clicks = []
        wheels = []
        tx = ty = 0.0
        btn = prev_btn = 0
        down_at = {}
        last_move = -9.0
        t0_out = times[0]
        t1_out = times[-1]
        warmup_t = max(0.0, t0_out - 3.0) if t0_out > 0.0 else 0.0
        idx = bisect.bisect_left([e[0] for e in evts], warmup_t) if warmup_t > 0.0 else 0
        prev_t = evts[idx][0] if idx < len(evts) else t0_out
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
                            if et >= t0_out - 1e-6 and t_dn <= t1_out + 1e-6:
                                clicks.append([round(t_dn, 4), bit, round(max(0.0, (et - t_dn) * 1000.0), 1)])
                b_hit |= bmask
                prev_btn = bmask
                btn = bmask
                if wh:
                    w_acc += wh
                    if et >= t0_out - 1e-6 and et <= t1_out + 1e-6:
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
            if t_dn <= end_t + 1e-6:
                clicks.append([round(t_dn, 4), bit, round(max(0.0, (end_t - t_dn) * 1000.0), 1)])
        clicks.sort(key=lambda c: c[0])
        return {"span": span, "x": xs, "y": ys, "b": bs, "w": ws,
                "spd": spds, "clicks": clicks, "wheels": wheels}

    def _wb_analyze_strafes(self, ts, v1, v2, k1, k2, thresh=0.25):
        events = []
        prev_single = None
        in_both_t0 = None
        gap_t0 = None
        for i in range(len(ts)):
            t = ts[i]
            d1 = v1[i] > thresh
            d2 = v2[i] > thresh
            if d1 and d2:
                if in_both_t0 is None:
                    in_both_t0 = t
                gap_t0 = None
            elif d1 or d2:
                cur = 0 if d1 else 1
                if in_both_t0 is not None:
                    if prev_single is not None and prev_single != cur:
                        ms = round(max(0.0, (t - in_both_t0) * 1000.0), 1)
                        d_str = "%s\u2192%s" % (k1 if prev_single == 0 else k2,
                                               k2 if prev_single == 0 else k1)
                        events.append({"t0": round(in_both_t0, 3), "t1": round(t, 3),
                                       "ms": ms, "kind": "overlap", "dir": d_str,
                                       "clean": ms <= 15.0})
                    in_both_t0 = None
                elif gap_t0 is not None and prev_single is not None and prev_single != cur:
                    ms = round(max(0.0, (t - gap_t0) * 1000.0), 1)
                    if ms <= 350.0:
                        d_str = "%s\u2192%s" % (k1 if prev_single == 0 else k2,
                                               k2 if prev_single == 0 else k1)
                        events.append({"t0": round(gap_t0, 3), "t1": round(t, 3),
                                       "ms": ms, "kind": "gap", "dir": d_str,
                                       "clean": ms <= 45.0})
                gap_t0 = None
                prev_single = cur
            else:
                in_both_t0 = None
                if prev_single is not None and gap_t0 is None:
                    gap_t0 = t
        overlaps = [e for e in events if e["kind"] == "overlap"]
        gaps = [e for e in events if e["kind"] == "gap"]
        clean_n = sum(1 for e in events if e["clean"])
        worst = sorted([e for e in overlaps if not e["clean"]],
                       key=lambda e: -e["ms"])[:10]
        return {
            "pair": [k1, k2],
            "total": len(events),
            "clean_count": clean_n,
            "clean_pct": round(clean_n * 100.0 / len(events), 1) if events else None,
            "overlap_count": len(overlaps),
            "avg_overlap_ms": round(sum(e["ms"] for e in overlaps) / len(overlaps), 1) if overlaps else 0.0,
            "max_overlap_ms": max((e["ms"] for e in overlaps), default=0.0),
            "avg_gap_ms": round(sum(e["ms"] for e in gaps) / len(gaps), 1) if gaps else 0.0,
            "events": events,
            "worst": worst,
        }

    def _wb_analyze_keys(self, ts, keys, cols):
        import math
        n_rows = max(1, len(ts))
        out = []
        for idx, k in enumerate(keys):
            arr = cols[idx]
            presses = 0
            active_n = 0
            active_sum = 0.0
            peaks = []
            trigs = []
            in_p = False
            cur_peak = 0.0
            cur_trig = None
            for v in arr:
                if v > 0.15:
                    active_n += 1
                    active_sum += v
                    if not in_p:
                        in_p = True
                        presses += 1
                        cur_peak = v
                        cur_trig = v if v >= 0.25 else None
                    else:
                        if v > cur_peak:
                            cur_peak = v
                        if cur_trig is None and v >= 0.25:
                            cur_trig = v
                elif in_p:
                    peaks.append(cur_peak)
                    if cur_trig is not None:
                        trigs.append(cur_trig)
                    in_p = False
            if in_p:
                peaks.append(cur_peak)
                if cur_trig is not None:
                    trigs.append(cur_trig)
            p_mean = sum(peaks) / len(peaks) if peaks else 0.0
            p_std = math.sqrt(sum((x - p_mean) ** 2 for x in peaks) / len(peaks)) if len(peaks) > 1 else 0.0
            t_mean = sum(trigs) / len(trigs) if trigs else 0.0
            t_std = math.sqrt(sum((x - t_mean) ** 2 for x in trigs) / len(trigs)) if len(trigs) > 1 else 0.0
            out.append({
                "key": k,
                "presses": presses,
                "active_pct": round(active_n * 100.0 / n_rows, 1),
                "avg_mm": round(active_sum / active_n, 2) if active_n else 0.0,
                "peak_mean": round(p_mean, 2),
                "peak_std": round(p_std, 2),
                "trig_mean": round(t_mean, 2),
                "trig_std": round(t_std, 2),
            })
        out.sort(key=lambda x: (-x["presses"], -x["active_pct"]))
        return out

    def _wb_analyze_mouse(self, wide_path, ts, keys, cols):
        import bisect, math
        if not ts:
            return None, None
        span, evts = self._load_mouse_events(wide_path)
        if not evts:
            return None, None

        # LMB shot stability against movement keys (W/A/S/D)
        move_idxs = [(i, k) for i, k in enumerate(keys) if k.upper() in ("W", "A", "S", "D")]
        shots_all = []
        moving = []
        prev_b = 0
        last_shot = -1.0
        for et, dx, dy, bmask, wh in evts:
            if ((~prev_b) & bmask) & 1 and (et - last_shot) >= 0.04:
                last_shot = et
                ri = bisect.bisect_right(ts, et) - 1
                ri = max(0, min(len(ts) - 1, ri))
                mm_max = 0.0
                mk = ""
                for ci, kn in move_idxs:
                    v = cols[ci][ri]
                    if v > mm_max:
                        mm_max = v
                        mk = kn
                still = mm_max < 0.30
                shots_all.append([round(et, 3), round(mm_max, 2), 1 if still else 0])
                if not still:
                    moving.append({"t": round(et, 3), "mm": round(mm_max, 2), "key": mk})
            prev_b = bmask
        n_sh = len(shots_all)
        still_n = sum(1 for s in shots_all if s[2])
        shots = {
            "total": n_sh,
            "still_count": still_n,
            "still_pct": round(still_n * 100.0 / n_sh, 1) if n_sh else None,
            "avg_move_mm": round(sum(s[1] for s in shots_all) / n_sh, 2) if n_sh else 0.0,
            "moving": sorted(moving, key=lambda x: -x["mm"])[:12],
            "all": shots_all[:300],
        }

        # 24x20 mousepad activity heatmap + LMB click positions
        gw, gh = 24, 20
        grid = [0] * (gw * gh)
        lmb_pts = []
        tx = ty = 0.0
        last_move = -9.0
        prev_t = evts[0][0]
        prev_b = 0
        step_x = 2.0 / max(500.0, float(span))
        aspect = (306.0 - 44.0) / (254.0 - 84.0)
        for et, dx, dy, bmask, wh in evts:
            dt = max(0.0, min(0.2, et - prev_t))
            prev_t = et
            if dx or dy:
                tx = max(-1.0, min(1.0, tx + dx * step_x))
                ty = max(-1.0, min(1.0, ty + dy * step_x * aspect))
                last_move = et
            elif et - last_move > 0.20 and dt > 0:
                k = math.exp(-dt * (1.6 + min(1.4, math.hypot(tx, ty)) * 4.2))
                tx *= k
                ty *= k
            gx = max(0, min(gw - 1, int((tx + 1.0) * 0.5 * gw)))
            gy = max(0, min(gh - 1, int((ty + 1.0) * 0.5 * gh)))
            grid[gy * gw + gx] += 1
            if (((~prev_b) & bmask) & 1) and len(lmb_pts) < 200:
                lmb_pts.append([round(tx, 3), round(ty, 3)])
            prev_b = bmask
        heat = {"w": gw, "h": gh, "grid": grid, "max": max(grid) if grid else 0, "lmb": lmb_pts}
        return shots, heat

    def wb_analysis(self, stamp=None, pair=("A", "D")):
        manifest, _, _, wide = self._resolve_wide(stamp)
        if not manifest or not wide:
            return {"ok": False, "error": "no recording"}
        mouse_p = os.path.join(os.path.dirname(wide), "mouse.csv")
        w_sig = self._wb_file_sig(wide)
        m_sig = self._wb_file_sig(mouse_p) if os.path.isfile(mouse_p) else None
        ck = (wide, w_sig, m_sig, tuple(pair))
        if w_sig is not None and ck in self._wb_analysis_cache:
            return self._wb_analysis_cache[ck]
        ts = []
        keys = []
        cols = []
        try:
            with io.open(wide, encoding="utf-8") as f:
                hdr = False
                for line in f:
                    if line.startswith("#"):
                        continue
                    parts = line.strip().split(",")
                    if not hdr:
                        hdr = True
                        keys = [p for p in parts[2:] if p]
                        cols = [[] for _ in keys]
                        continue
                    ts.append(float(parts[0]))
                    n_p = len(parts)
                    for ci in range(len(keys)):
                        tok = parts[2 + ci] if 2 + ci < n_p else "0.00"
                        cols[ci].append(0.0 if tok == "0.00" else float(tok))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not ts:
            return {"ok": False, "error": "empty recording"}
        kmap = {k.upper(): i for i, k in enumerate(keys)}
        k1, k2 = pair[0], pair[1]
        if k1.upper() in kmap and k2.upper() in kmap:
            i1, i2 = kmap[k1.upper()], kmap[k2.upper()]
            strafes = self._wb_analyze_strafes(ts, cols[i1], cols[i2], keys[i1], keys[i2])
        else:
            strafes = {"pair": [k1, k2], "total": 0, "clean_count": 0, "clean_pct": None,
                       "overlap_count": 0, "avg_overlap_ms": 0.0, "max_overlap_ms": 0.0,
                       "avg_gap_ms": 0.0, "events": [], "worst": []}
        key_stats = self._wb_analyze_keys(ts, keys, cols)
        shots, heat = self._wb_analyze_mouse(wide, ts, keys, cols)
        res = {
            "ok": True,
            "stamp": manifest.get("stamp"),
            "duration": round(ts[-1], 3),
            "full_mm": round(self.full_mm, 2),
            "keys": keys,
            "strafes": strafes,
            "shots": shots,
            "key_stats": key_stats,
            "mouse_heat": heat,
        }
        if w_sig is not None:
            if len(self._wb_analysis_cache) >= 4:
                self._wb_analysis_cache.clear()
            self._wb_analysis_cache[ck] = res
        return res

    def _seek_wide_before(self, f, data_off, fsize, target_t):
        """Binary-search wide.csv to the start of a line shortly before `target_t`."""
        lo = data_off
        hi = fsize
        while hi - lo > 4096:
            mid = (lo + hi) // 2
            f.seek(mid)
            f.readline()                  # advance to next complete line boundary
            pos = f.tell()
            if pos >= hi:
                break
            line = f.readline()
            if not line:
                break
            c1 = line.find(b",")
            if c1 <= 0:
                lo = pos
                continue
            try:
                t = float(line[:c1])
            except Exception:
                lo = pos
                continue
            if t < target_t:
                lo = pos
            else:
                hi = mid
        f.seek(lo)

    def wb_series(self, step=0.02, limit=1600, stamp=None, start=None, end=None):
        manifest, _, _, wide = self._resolve_wide(stamp)
        if not manifest or not wide:
            return {"ok": False, "error": "no recording"}
        try:
            keys, full_dur, _ = self._read_wide_meta(wide)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        limit = max(200, min(4000, int(limit or 1600)))
        is_lod = (start is not None or end is not None)
        w_start = max(0.0, float(start or 0.0)) if is_lod else 0.0
        w_end = float(end) if (is_lod and end is not None) else full_dur
        if w_end <= w_start:
            w_start, w_end, is_lod = 0.0, full_dur, False
        win_dur = max(0.01, w_end - w_start)
        bucket_s = win_dur / float(limit)
        downsample = bucket_s > 0.012
        ts = []
        v_cols = []
        b_idx = None
        b_t = 0.0
        b_max = []
        n_keys = 0
        try:
            with io.open(wide, "rb") as f:
                header_line = None
                while True:
                    raw = f.readline()
                    if not raw:
                        break
                    if raw.startswith(b"#"):
                        continue
                    header_line = raw.decode("utf-8", "replace").strip()
                    break
                if header_line is None:
                    return {"ok": False, "error": "empty recording"}
                keys = [p for p in header_line.split(",")[2:] if p]
                n_keys = len(keys)
                v_cols = [[] for _ in range(n_keys)]
                b_max = [0.0] * n_keys
                data_off = f.tell()
                if is_lod and w_start > 0.5:
                    fsize = os.path.getsize(wide)
                    if fsize > 128 * 1024:
                        self._seek_wide_before(f, data_off, fsize, max(0.0, w_start - 0.05))
                for raw in f:
                    if not raw or raw.startswith(b"#"):
                        continue
                    c1 = raw.find(b",")
                    if c1 <= 0:
                        continue
                    t = float(raw[:c1])
                    if t < w_start - 1e-6:
                        continue
                    if is_lod and t > w_end + 1e-6:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    parts = line.rstrip("\r\n").split(",")
                    if not downsample:
                        ts.append(round(t, 3))
                        for ci in range(n_keys):
                            tok = parts[2 + ci] if 2 + ci < len(parts) else "0.00"
                            v_cols[ci].append(0.0 if tok == "0.00" else round(float(tok), 2))
                    else:
                        bi = int((t - w_start) / bucket_s)
                        if b_idx is None:
                            b_idx = bi
                            b_t = t
                            for ci in range(n_keys):
                                tok = parts[2 + ci] if 2 + ci < len(parts) else "0.00"
                                b_max[ci] = 0.0 if tok == "0.00" else float(tok)
                        elif bi != b_idx:
                            ts.append(round(b_t, 3))
                            for ci in range(n_keys):
                                v_cols[ci].append(round(b_max[ci], 2))
                                tok = parts[2 + ci] if 2 + ci < len(parts) else "0.00"
                                b_max[ci] = 0.0 if tok == "0.00" else float(tok)
                            b_idx = bi
                            b_t = t
                        else:
                            b_t = t
                            for ci in range(n_keys):
                                tok = parts[2 + ci] if 2 + ci < len(parts) else "0.00"
                                if tok != "0.00":
                                    val = float(tok)
                                    if val > b_max[ci]:
                                        b_max[ci] = val
                if downsample and b_idx is not None:
                    ts.append(round(b_t, 3))
                    for ci in range(n_keys):
                        v_cols[ci].append(round(b_max[ci], 2))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not ts:
            return {"ok": False, "error": "empty recording"}
        dur_out = full_dur if full_dur > 0.0 else ts[-1]
        mouse = self._load_wb_mouse(wide, ts, start=w_start, end=w_end)
        return {
            "ok": True, "stamp": manifest.get("stamp"), "keys": keys,
            "duration": round(dur_out, 3),
            "view_start": round(ts[0], 3), "view_end": round(ts[-1], 3), "lod": is_lod,
            "step": round((ts[-1] - ts[0]) / max(1, len(ts) - 1), 4) if len(ts) > 1 else step,
            "t": ts, "v": {keys[i]: v_cols[i] for i in range(n_keys)},
            "mouse": mouse,
        }

    def _recording_wall_span(self, wide_path, stamp, dur):
        """Return (t0_epoch, t1_epoch, src) for a recording folder."""
        rdir = os.path.dirname(wide_path)
        meta_p = os.path.join(rdir, "meta.json")
        if os.path.isfile(meta_p):
            try:
                with io.open(meta_p, encoding="utf-8") as f:
                    m = json.load(f)
                t0 = float(m["t0_epoch"])
                t1 = float(m.get("t1_epoch") or (t0 + dur))
                return t0, t1, "meta"
            except Exception:
                pass
        fn_t1 = parse_filename_wall_epoch(stamp)
        if fn_t1 is not None:
            return fn_t1 - float(dur), fn_t1, "stamp"
        try:
            mt = os.path.getmtime(wide_path)
            return mt - float(dur), mt, "mtime"
        except Exception:
            return None, None, "none"

    def _probe_video(self, path):
        ffp = find_ffmpeg_bin("ffprobe")
        if not ffp:
            return None, "未找到 ffprobe.exe（请安装 ffmpeg 或放在同目录）"
        cmd = [ffp, "-v", "error", "-show_entries",
               "format=duration,size:format_tags=creation_time:stream=codec_type,width,height,r_frame_rate",
               "-of", "json", path]
        try:
            si = None
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            raw = subprocess.check_output(cmd, stderr=subprocess.STDOUT, startupinfo=si, timeout=15)
            info = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            return None, "读取视频信息失败：%s" % exc
        fmt = info.get("format") or {}
        dur = float(fmt.get("duration") or 0.0)
        if dur <= 0.0:
            return None, "视频时长无效"
        tags = fmt.get("tags") or {}
        w = h = 0
        fps = 60.0
        has_audio = False
        for st in (info.get("streams") or []):
            ct = st.get("codec_type")
            if ct == "video" and not w:
                w = int(st.get("width") or 0)
                h = int(st.get("height") or 0)
                fr = st.get("r_frame_rate") or "60/1"
                if "/" in fr:
                    a, b = fr.split("/", 1)
                    if float(b) > 0:
                        fps = round(float(a) / float(b), 3)
            elif ct == "audio":
                has_audio = True
        ct_ep = _parse_iso_utc_epoch(tags.get("creation_time") or "")
        fn_ep = parse_filename_wall_epoch(os.path.basename(path))
        mt_ep = os.path.getmtime(path)
        if ct_ep is not None:
            if fn_ep is not None and abs(ct_ep - fn_ep) < 120.0:
                start_ep, src = ct_ep, "creation_time_start"
            elif fn_ep is not None and abs((ct_ep - dur) - fn_ep) < 120.0:
                start_ep, src = ct_ep - dur, "creation_time_end"
            elif abs(ct_ep - mt_ep) < 120.0:
                start_ep, src = ct_ep - dur, "creation_time_mtime"
            else:
                start_ep, src = ct_ep - dur, "creation_time"
        elif fn_ep is not None:
            if abs(mt_ep - fn_ep) < 120.0 and dur > 120.0:
                start_ep, src = fn_ep - dur, "filename_end"
            else:
                start_ep, src = fn_ep, "filename_start"
        else:
            start_ep, src = mt_ep - dur, "mtime"
        return {
            "path": os.path.abspath(path),
            "name": os.path.basename(path),
            "size": os.path.getsize(path),
            "duration": round(dur, 3),
            "width": w, "height": h, "fps": fps,
            "has_audio": has_audio,
            "start_epoch": round(start_ep, 3),
            "end_epoch": round(start_ep + dur, 3),
            "time_src": src,
        }, None

    def wb_video_load(self, path, preferred_stamp=None):
        p = (path or "").strip().strip('"').strip("'")
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": "视频文件不存在"}
        ext = os.path.splitext(p)[1].lower()
        if ext not in (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"):
            return {"ok": False, "error": "不支持的视频格式：%s" % (ext or "未知")}
        meta, err = self._probe_video(p)
        if not meta:
            return {"ok": False, "error": err or "无法解析视频"}
        with self.lock:
            self.wb_video = meta
        v0, v1 = meta["start_epoch"], meta["end_epoch"]
        best = None
        pref = None
        base = recordings_dir()
        for it in (self.wb_list().get("items") or []):
            st = it["stamp"]
            wide = os.path.join(base, st, "wide.csv")
            r0, r1, rsrc = self._recording_wall_span(wide, st, it.get("duration") or 0.0)
            if r0 is None:
                continue
            ov = min(v1, r1) - max(v0, r0)
            cand = {"stamp": st, "overlap": round(ov, 2), "offset": round(v0 - r0, 3), "rec_src": rsrc}
            if best is None or ov > best["overlap"]:
                best = cand
            if preferred_stamp and st == preferred_stamp:
                pref = cand
        chosen = pref if (pref and pref["overlap"] > 0.0) else (best if (best and best["overlap"] > 0.0) else pref or best)
        pub = {k: meta[k] for k in ("name", "size", "duration", "width", "height", "fps", "has_audio", "time_src")}
        pub["start_str"] = time.strftime("%H:%M:%S", time.localtime(v0))
        pub["end_str"] = time.strftime("%H:%M:%S", time.localtime(v1))
        return {
            "ok": True,
            "video": pub,
            "matched_stamp": chosen["stamp"] if chosen else None,
            "overlap_s": chosen["overlap"] if chosen else 0.0,
            "offset": chosen["offset"] if chosen else 0.0,
            "stage": _sanitize_wb_stage(self.wb_stage),
        }

    def wb_video_fine_align(self, stamp=None, video_t=None, coarse_offset=0.0, search_s=3.0):
        with self.lock:
            vid = dict(self.wb_video) if self.wb_video else None
        if not vid or not os.path.isfile(vid.get("path") or ""):
            return {"ok": False, "error": "请先导入录像"}
        if not vid.get("has_audio"):
            return {"ok": False, "error": "该录像没有音轨，请使用手动微调"}
        ff = find_ffmpeg_bin("ffmpeg")
        if not ff:
            return {"ok": False, "error": "未找到 ffmpeg.exe"}
        manifest, _, _, wide = self._resolve_wide(stamp)
        if not manifest or not wide:
            return {"ok": False, "error": "没有可用录制"}
        _, evts = self._load_mouse_events(wide)
        if not evts:
            return {"ok": False, "error": "该录制无鼠标左键点击数据"}
        clicks = []
        pb = 0
        for et, _, _, bm, _ in evts:
            if ((~pb) & bm) & 1:
                if not clicks or et - clicks[-1] >= 0.04:
                    clicks.append(et)
            pb = bm
        if len(clicks) < 2:
            return {"ok": False, "error": "录制中左键开火样本不足"}
        import bisect, math, struct
        vdur = float(vid["duration"])
        off0 = float(coarse_offset or 0.0)
        sr_s = max(0.5, min(6.0, float(search_s or 3.0)))
        win_len = 24.0
        va = None
        if video_t is not None:
            vt = max(0.0, min(vdur, float(video_t)))
            a0 = max(0.0, vt - win_len * 0.5)
            b0 = min(vdur, a0 + win_len)
            ra, rb = a0 + off0, b0 + off0
            if bisect.bisect_right(clicks, rb) - bisect.bisect_left(clicks, ra) >= 2:
                va = a0
        if va is None:
            r_lo = max(0.0, off0)
            r_hi = min(clicks[-1], vdur + off0)
            best_c = (0, 0.0)
            j = 0
            for i, ct in enumerate(clicks):
                if ct < r_lo or ct > r_hi:
                    continue
                while j < len(clicks) and clicks[j] - ct <= win_len:
                    j += 1
                cnt = j - i
                if cnt > best_c[0]:
                    best_c = (cnt, max(0.0, ct - off0 - 4.0))
            if best_c[0] < 2:
                return {"ok": False, "offset": round(off0, 3), "confidence": 0.0,
                        "message": "重叠区间内缺少足够左键开火样本，已保留粗对齐"}
            va = best_c[1]
        vb = min(vdur, va + win_len)
        seg_d = max(4.0, vb - va)
        cmd = [ff, "-v", "error", "-ss", "%.3f" % va, "-t", "%.3f" % seg_d,
               "-i", vid["path"], "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
        try:
            si = None
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            pcm = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, startupinfo=si, timeout=20)
        except Exception as exc:
            return {"ok": False, "error": "提取音频失败：%s" % exc}
        n_samp = len(pcm) // 2
        if n_samp < 8000:
            return {"ok": False, "error": "音频片段过短"}
        bin_s = 0.02
        spb = int(8000 * bin_s)
        n_bins = n_samp // spb
        rms = [0.0] * n_bins
        for bi in range(n_bins):
            chunk = struct.unpack_from("<%dh" % spb, pcm, bi * spb * 2)
            rms[bi] = math.sqrt(sum(x * x for x in chunk) / float(spb))
        onset = [0.0] * n_bins
        for bi in range(1, n_bins):
            d = rms[bi] - rms[bi - 1]
            if d > 0.0:
                onset[bi] = d
        c_lo = va + off0 - sr_s
        c_hi = vb + off0 + sr_s
        rel_clicks = [c - (va + off0) for c in clicks if c_lo <= c <= c_hi]
        if len(rel_clicks) < 2:
            return {"ok": False, "offset": round(off0, 3), "confidence": 0.0,
                    "message": "当前位置附近无左键点击，请拖到交火处或点自动窗口"}
        k_max = int(round(sr_s / bin_s))
        scores = []
        for k in range(-k_max, k_max + 1):
            sh = k * bin_s
            sc = 0.0
            for rc in rel_clicks:
                bi = int(round((rc - sh) / bin_s))
                if 0 <= bi < n_bins:
                    sc += onset[bi]
                    if bi + 1 < n_bins:
                        sc += 0.5 * onset[bi + 1]
                    if bi - 1 >= 0:
                        sc += 0.3 * onset[bi - 1]
            scores.append((sc, sh))
        mean_s = sum(s for s, _ in scores) / float(len(scores))
        var_s = sum((s - mean_s) ** 2 for s, _ in scores) / float(len(scores))
        std_s = math.sqrt(var_s) if var_s > 0.0 else 1.0
        best_sc, best_sh = max(scores, key=lambda x: x[0])
        z = (best_sc - mean_s) / std_s if std_s > 0.0 else 0.0
        if z < 2.2:
            return {"ok": False, "offset": round(off0, 3), "confidence": round(z, 2),
                    "clicks": len(rel_clicks), "window": [round(va, 2), round(vb, 2)],
                    "message": "枪声与左键脉冲相关性较弱 (z=%.1f)，建议微调或换到开火片段再试" % z}
        return {
            "ok": True,
            "offset": round(off0 + best_sh, 3),
            "delta": round(best_sh, 3),
            "confidence": round(z, 2),
            "clicks": len(rel_clicks),
            "window": [round(va, 2), round(vb, 2)],
        }

    def export_status(self):
        with self.lock:
            job = dict(self.export)
        job.pop("proc", None)
        return job

    def cancel_export(self):
        self._export_cancel.set()
        with self.lock:
            proc = self.export.get("proc")
            was_running = bool(self.export.get("running"))
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if proc is not None or was_running:
            with self.lock:
                self.export.update(running=False, msg="\u5df2\u53d6\u6d88", pct=0, proc=None)
            return True
        return False

    def _atempo_chain(self, speed):
        s = max(0.25, min(2.0, float(speed or 1.0)))
        parts = []
        while s < 0.5:
            parts.append("atempo=0.5")
            s *= 2.0
        if abs(s - 1.0) > 1e-4:
            parts.append("atempo=%.4f" % s)
        return ",".join(parts)

    def _run_subproc(self, argv, cwd=None, pct_lo=0, pct_hi=100, label=""):
        si = None
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding="utf-8", errors="replace", startupinfo=si,
        )
        with self.lock:
            self.export["proc"] = proc
            if label:
                self.export["msg"] = label
        span = max(1, pct_hi - pct_lo)
        for line in proc.stdout:
            if self._export_cancel.is_set():
                break
            line = line.strip()
            if not line:
                continue
            pct = None
            if "/" in line and ("frames" in line or "fps" in line):
                try:
                    part = [w for w in line.split() if "/" in w][0]
                    done, total = part.split("/", 1)
                    pct = pct_lo + int(float(done) * span / max(1.0, float(total)))
                except Exception:
                    pct = None
            with self.lock:
                if pct is not None:
                    self.export["pct"] = min(99, max(self.export["pct"], pct))
                    elapsed = time.time() - self.export["t0"]
                    p = self.export["pct"]
                    if p > 2:
                        self.export["eta"] = int(elapsed * (100 - p) / p)
                self.export["msg"] = (("%s · %s" % (label, line)) if label else line)[:120]
        rc = proc.wait()
        with self.lock:
            if self.export.get("proc") is proc:
                self.export["proc"] = None
        return rc

    def _trim_video_clip(self, ff, src_vid, v_start, v_dur, out_mp4, speed=1.0, pct_lo=0, pct_hi=25):
        pre = max(0.0, float(v_start) - 10.0)
        fine = float(v_start) - pre
        spd = float(speed or 1.0)
        atempo = self._atempo_chain(spd)
        for enc in (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "17"],
                    ["-c:v", "libx264", "-preset", "fast", "-crf", "17"]):
            cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", "%.3f" % pre, "-i", src_vid,
                   "-ss", "%.3f" % fine, "-t", "%.3f" % float(v_dur),
                   "-map", "0:v:0", "-map", "0:a?"]
            if abs(spd - 1.0) > 1e-4:
                cmd += ["-vf", "setpts=PTS/%.4f" % spd]
                if atempo:
                    cmd += ["-af", atempo]
            cmd += enc + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_mp4]
            rc = self._run_subproc(cmd, pct_lo=pct_lo, pct_hi=pct_hi, label="裁剪游戏录像")
            if rc == 0 and os.path.isfile(out_mp4):
                return 0
            if self._export_cancel.is_set():
                return rc
        return 1

    def _composite_video_with_pngs(self, ff, src_vid, v_start, v_dur, png_dir, fps, out_mp4, W, H, speed=1.0, pct_lo=68, pct_hi=100):
        pre = max(0.0, float(v_start) - 10.0)
        fine = float(v_start) - pre
        spd = float(speed or 1.0)
        vchain = "[0:v:0]" + (("setpts=PTS/%.4f," % spd) if abs(spd - 1.0) > 1e-4 else "")
        vchain += "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black[bg];" % (W, H, W, H)
        fgraph = vchain + "[1:v]format=rgba[hud];[bg][hud]overlay=0:0:shortest=1:format=auto[outv]"
        atempo = self._atempo_chain(spd)
        pat = os.path.join(png_dir, "frame_%05d.png")
        for enc in (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "17"],
                    ["-c:v", "libx264", "-preset", "fast", "-crf", "17"]):
            cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", "%.3f" % pre, "-i", src_vid,
                   "-ss", "%.3f" % fine, "-t", "%.3f" % float(v_dur),
                   "-framerate", str(fps), "-i", pat,
                   "-filter_complex", fgraph, "-map", "[outv]", "-map", "0:a?"]
            if atempo:
                cmd += ["-af", atempo]
            cmd += enc + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_mp4]
            rc = self._run_subproc(cmd, pct_lo=pct_lo, pct_hi=pct_hi, label="合成 HUD 视频")
            if rc == 0 and os.path.isfile(out_mp4):
                return 0
            if self._export_cancel.is_set():
                return rc
        return 1

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
        fmt = (params.get("fmt") or ("png" if params.get("png") else "prores")).strip().lower()
        if fmt not in ("prores", "webm", "png"):
            fmt = "prores"
        mode = (params.get("mode") or "hud").strip().lower()
        if mode not in ("hud", "composite", "tracks"):
            mode = "hud"
        offset = float(params.get("offset") or 0.0)
        mm_mode = (params.get("mm") or "press").strip().lower()
        if mm_mode not in ("off", "press", "always"):
            mm_mode = "press"
        inc_mouse = bool(params.get("mouse"))
        mlay = (params.get("mouse_layout") or "side").strip().lower()
        if mlay not in ("side", "stack", "only", "stage"):
            mlay = "side"
        mouse_csv = os.path.join(os.path.dirname(wide), "mouse.csv")
        has_mouse = inc_mouse and os.path.isfile(mouse_csv)
        ff = find_ffmpeg_bin("ffmpeg")
        if mode in ("composite", "tracks"):
            with self.lock:
                vid = dict(self.wb_video) if self.wb_video else None
            if not vid or not os.path.isfile(vid.get("path") or ""):
                return False, "该导出模式需要先导入录像"
            if not ff:
                return False, "未找到 ffmpeg.exe"
            if end <= start:
                return False, "请先选择有效的导出区间"
        def _box_str(val):
            if isinstance(val, (list, tuple)) and len(val) == 4:
                return "%.4f,%.4f,%.4f,%.4f" % tuple(float(x) for x in val)
            return ""
        kb_box = _box_str(params.get("kb_box"))
        ms_box = _box_str(params.get("mouse_box"))
        kb_alpha = params.get("kb_alpha")
        ms_alpha = params.get("mouse_alpha")
        mopacity = params.get("mopacity")
        out_name = params.get("out") or ("depth-%s" % time.strftime("%Y%m%d-%H%M%S"))
        exports = os.path.join(base, "exports")
        try:
            if not os.path.isdir(exports):
                os.makedirs(exports)
        except Exception:
            exports = base
        out_path = os.path.join(exports, out_name)

        def _base_kb_argv(target, extra_fmt=None):
            a = [py, kb, wide, "--fps", str(fps), "--size", size,
                 "--layout", layout, "--theme", theme, "--full", "%.2f" % full_mm,
                 "--speed", str(speed), "--mm", mm_mode,
                 "--format", extra_fmt or fmt, "-o", target]
            if ff:
                a += ["--ffmpeg", ff]
            if keys:
                a += ["--keys"] + keys
            if start > 0:
                a += ["--start", "%.3f" % start]
            if end > 0:
                a += ["--end", "%.3f" % end]
            if bg and mode == "hud":
                a += ["--bg", bg]
            if kb_alpha is not None:
                a += ["--kb-alpha", "%.2f" % float(kb_alpha)]
            if ms_alpha is not None:
                a += ["--mouse-alpha", "%.2f" % float(ms_alpha)]
            if mopacity is not None:
                a += ["--mopacity", "%.2f" % float(mopacity)]
            return a

        self._export_cancel.clear()
        if mode == "hud":
            argv = _base_kb_argv(out_path)
            if has_mouse:
                argv += ["--mouse", mouse_csv, "--mouse-layout", mlay]
            elif mlay == "stage":
                argv += ["--mouse-layout", "stage"]
            if kb_box:
                argv += ["--kb-box", kb_box]
            if ms_box:
                argv += ["--mouse-box", ms_box]
            if fmt == "png":
                argv += ["--png-only", "--keep-frames"]
            final_out = (out_path + ".webm") if fmt == "webm" else ((out_path + ".mov") if fmt == "prores" else out_path)
            with self.lock:
                self.export = {"running": True, "pct": 0, "msg": "正在渲染 HUD",
                               "out": final_out, "proc": None, "t0": time.time(),
                               "eta": 0, "error": "", "argv": " ".join(argv)}
            def pump_hud():
                rc = self._run_subproc(argv, cwd=os.path.dirname(kb), pct_lo=0, pct_hi=100, label="渲染 HUD")
                with self.lock:
                    self.export.update(running=False, pct=100 if rc == 0 else 0,
                                       msg="完成" if rc == 0 else ("已取消" if self._export_cancel.is_set() else ("失败 (rc=%d)" % rc)),
                                       error="" if rc == 0 else "rc=%d" % rc)
            t = threading.Thread(target=pump_hud); t.daemon = True; t.start()
            return True, final_out

        v_start = max(0.0, start - offset)
        v_dur = max(0.1, end - start)
        W, H = [int(x) for x in size.lower().split("x")]
        if mode == "composite":
            final_out = out_path + "_hud.mp4"
            with self.lock:
                self.export = {"running": True, "pct": 0, "msg": "准备合成成片",
                               "out": final_out, "proc": None, "t0": time.time(),
                               "eta": 0, "error": ""}
            def pump_comp():
                import tempfile
                tmp_png = tempfile.mkdtemp(prefix="dl-comp-")
                rc = 1
                try:
                    argv = _base_kb_argv(tmp_png, "png") + ["--png-only", "--keep-frames"]
                    if has_mouse:
                        argv += ["--mouse", mouse_csv, "--mouse-layout", "stage"]
                    else:
                        argv += ["--mouse-layout", "stage"]
                    if kb_box:
                        argv += ["--kb-box", kb_box]
                    if ms_box:
                        argv += ["--mouse-box", ms_box]
                    rc = self._run_subproc(argv, cwd=os.path.dirname(kb), pct_lo=0, pct_hi=68, label="渲染透明 HUD 序列")
                    if rc == 0 and not self._export_cancel.is_set():
                        rc = self._composite_video_with_pngs(ff, vid["path"], v_start, v_dur, tmp_png, fps, final_out, W, H, speed, 68, 100)
                finally:
                    for _ in range(4):
                        shutil.rmtree(tmp_png, ignore_errors=True)
                        if not os.path.isdir(tmp_png):
                            break
                        time.sleep(0.3)
                    with self.lock:
                        self.export.update(running=False, pct=100 if rc == 0 else 0,
                                           msg="合成完成" if rc == 0 else ("已取消" if self._export_cancel.is_set() else ("失败 (rc=%d)" % rc)),
                                           error="" if rc == 0 else "rc=%d" % rc)
            t = threading.Thread(target=pump_comp); t.daemon = True; t.start()
            return True, final_out

        tdir = out_path + "_tracks"
        if not os.path.isdir(tdir):
            os.makedirs(tdir)
        ext_hud = ".webm" if fmt == "webm" else ".mov"
        use_fmt = "webm" if fmt == "webm" else "prores"
        with self.lock:
            self.export = {"running": True, "pct": 0, "msg": "准备三轨分开导出",
                           "out": tdir, "proc": None, "t0": time.time(),
                           "eta": 0, "error": ""}
        def pump_tracks():
            rc = self._trim_video_clip(ff, vid["path"], v_start, v_dur, os.path.join(tdir, "clip.mp4"), speed, 0, 25)
            if rc == 0 and not self._export_cancel.is_set():
                a_kb = _base_kb_argv(os.path.join(tdir, "kb"), use_fmt)
                if kb_box:
                    a_kb += ["--kb-box", kb_box]
                rc = self._run_subproc(a_kb, cwd=os.path.dirname(kb), pct_lo=25, pct_hi=(62 if has_mouse else 100), label="渲染键盘透明轨")
            if rc == 0 and has_mouse and not self._export_cancel.is_set():
                a_ms = _base_kb_argv(os.path.join(tdir, "mouse"), use_fmt) + ["--mouse", mouse_csv, "--mouse-layout", "only"]
                if ms_box:
                    a_ms += ["--mouse-box", ms_box]
                rc = self._run_subproc(a_ms, cwd=os.path.dirname(kb), pct_lo=62, pct_hi=100, label="渲染鼠标透明轨")
            with self.lock:
                self.export.update(running=False, pct=100 if rc == 0 else 0,
                                   msg="三轨导出完成" if rc == 0 else ("已取消" if self._export_cancel.is_set() else ("失败 (rc=%d)" % rc)),
                                   error="" if rc == 0 else "rc=%d" % rc)
        t = threading.Thread(target=pump_tracks); t.daemon = True; t.start()
        return True, tdir

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
                should_rewake = (
                    self.connected and not self.stop_io.is_set() and bool(self.vk_held)
                    and (self._last_depth_t is None or (time.perf_counter() - self._last_depth_t) > 1.5)
                    and (time.perf_counter() - self._last_wake_t) > 5.0
                )
            if should_rewake:
                self._ensure_hid_wake(force=False)

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
                "mouse_hud": dict(self.mouse_hud),
                "mouse_hud_rev": self.mouse_hud_rev,
                "kb_hud": dict(self.kb_hud),
                "kb_hud_rev": self.kb_hud_rev,
                "mouse_uipi": self.mouse_uipi_fallback,
                "mouse_ratio": round(self._cursor_raw_ratio, 2),
                "hid_wake": self.hid_wake,
                "hid_wake_msg": self.hid_wake_msg,
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
                   "/openfolder", "/quit", "/wb/export", "/wb/cancel", "/wb/delete",
                   "/wb/export/open", "/wb/video/pick", "/wb/video/load",
                   "/wb/video/clear", "/wb/video/align", "/wb/video/layout")

    def _send_video_stream(self):
        with ENGINE.lock:
            vid = dict(ENGINE.wb_video) if ENGINE.wb_video else None
        path = vid.get("path") if vid else None
        if not path or not os.path.isfile(path):
            self._send_text(404, "no video loaded")
            return
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        ctype = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
                 ".mov": "video/quicktime", ".mkv": "video/x-matroska",
                 ".avi": "video/x-msvideo"}.get(ext, "application/octet-stream")
        rng = self.headers.get("Range")
        start = 0
        end = size - 1
        status = 200
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",", 1)[0].strip()
            a_s, b_s = (spec.split("-", 1) + [""])[:2]
            if a_s == "" and b_s:
                n = min(size, int(b_s))
                start = max(0, size - n)
                end = size - 1
            else:
                start = max(0, int(a_s or 0))
                end = min(size - 1, int(b_s)) if b_s else size - 1
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        rem = length
        with io.open(path, "rb") as f:
            f.seek(start)
            while rem > 0:
                chunk = f.read(min(262144, rem))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    break
                rem -= len(chunk)

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
            start = (q.get("start") or [None])[0]
            end = (q.get("end") or [None])[0]
            start_v = float(start) if start not in (None, "") else None
            end_v = float(end) if end not in (None, "") else None
            try:
                self._send_json(ENGINE.wb_series(step, limit=limit, stamp=stamp,
                                                 start=start_v, end=end_v))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
        elif url.path == "/wb/analysis":
            q = parse_qs(url.query)
            stamp = (q.get("stamp") or [None])[0]
            pair_s = (q.get("pair") or ["A,D"])[0]
            parts = [p.strip() for p in pair_s.split(",") if p.strip()]
            pair = (parts[0], parts[1]) if len(parts) >= 2 else ("A", "D")
            try:
                self._send_json(ENGINE.wb_analysis(stamp=stamp, pair=pair))
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
        elif url.path == "/wb/export/open":
            exports = os.path.join(recordings_dir(), "exports")
            out = (ENGINE.export.get("out") or "")
            target = out if os.path.isdir(out) else (os.path.dirname(out) if out and os.path.isfile(out) else exports)
            if not os.path.isdir(target):
                try:
                    os.makedirs(target)
                except Exception:
                    target = recordings_dir()
            try:
                os.startfile(target)
            except Exception:
                pass
            self._send_json({"ok": True, "path": target})
        elif url.path == "/wb/video/info":
            with ENGINE.lock:
                vid = dict(ENGINE.wb_video) if ENGINE.wb_video else None
            self._send_json({"ok": True, "video": vid and {
                k: vid[k] for k in ("name", "size", "duration", "width", "height", "fps", "has_audio", "time_src")
            }, "stage": _sanitize_wb_stage(ENGINE.wb_stage)})
        elif url.path == "/wb/video/stream":
            self._send_video_stream()
        elif url.path == "/wb/video/pick":
            p = pick_video_file_win32(r"F:\录屏" if os.path.isdir(r"F:\录屏") else None)
            if not p:
                self._send_json({"ok": False, "cancelled": True})
            else:
                self._send_json(ENGINE.wb_video_load(p))
        elif url.path == "/wb/video/load":
            raw = getattr(self, "_body", b"") or b"{}"
            try:
                params = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                params = {}
            self._send_json(ENGINE.wb_video_load(params.get("path"), params.get("stamp")))
        elif url.path == "/wb/video/clear":
            with ENGINE.lock:
                ENGINE.wb_video = None
            self._send_json({"ok": True})
        elif url.path == "/wb/video/align":
            raw = getattr(self, "_body", b"") or b"{}"
            try:
                params = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                params = {}
            self._send_json(ENGINE.wb_video_fine_align(
                stamp=params.get("stamp"),
                video_t=params.get("video_t"),
                coarse_offset=params.get("offset", 0.0),
                search_s=params.get("search_s", 3.0),
            ))
        elif url.path == "/wb/video/layout":
            raw = getattr(self, "_body", b"") or b"{}"
            try:
                params = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                params = {}
            out = ENGINE.update_settings(wb_stage=params, save=True)
            self._send_json({"ok": True, "stage": out.get("wb_stage")})
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
            mh_v = body_obj.get("mouse_hud") if isinstance(body_obj.get("mouse_hud"), dict) else None
            kh_v = body_obj.get("kb_hud") if isinstance(body_obj.get("kb_hud"), dict) else None
            snap = ENGINE.update_settings(full_mm=full_v, theme=theme_v, mouse_span=span_v,
                                          mouse_hud=mh_v, kb_hud=kh_v, save=True)
            self._send_json({"ok": True, "full_mm": snap["full_mm"],
                             "theme": snap["theme"], "mouse_span": snap["mouse_span"],
                             "mouse_hud": snap["mouse_hud"], "mouse_hud_rev": snap["mouse_hud_rev"],
                             "kb_hud": snap["kb_hud"], "kb_hud_rev": snap["kb_hud_rev"]})
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
    eng = globals().get("ENGINE")
    kh = dict(eng.kb_hud) if eng else dict(KB_HUD_DEFAULTS)
    krev = eng.kb_hud_rev if eng else 0
    return (OVERLAY_PAGE
            .replace("__LAYOUT__", json.dumps(_layout_dicts(), ensure_ascii=False))
            .replace("__THEME_JS__", COMMON_THEME_JS)
            .replace("__KB_HUD__", json.dumps(kh, ensure_ascii=False))
            .replace("__KB_HUD_REV__", str(krev)))


def mouse_overlay_html():
    eng = globals().get("ENGINE")
    mh = dict(eng.mouse_hud) if eng else dict(MOUSE_HUD_DEFAULTS)
    mspan = eng.mouse_span if eng else 14800
    mrev = eng.mouse_hud_rev if eng else 0
    return (MOUSE_OVERLAY_PAGE
            .replace("__THEME_JS__", COMMON_THEME_JS)
            .replace("__MOUSE_HUD__", json.dumps(mh, ensure_ascii=False))
            .replace("__MOUSE_SPAN__", str(mspan))
            .replace("__MOUSE_HUD_REV__", str(mrev)))


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
    contain: layout style paint;
    transition: transform 0.05s linear, border-color 0.08s linear;
  }
  #kb.many .k { backdrop-filter: none; }
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
  <div class="p-hint">所有样式与参数调整自动保存并实时同步至 OBS/直播姬；浏览器中按 C 键可切换此面板。</div>
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

const SAVED_KBHUD = __KB_HUD__;
const SAVED_KBHUD_REV = __KB_HUD_REV__;

function readConfig() {
  const q = new URLSearchParams(location.search);
  const rawCl = q.get('cluster') !== null ? q.get('cluster') : (q.get('layout') !== null ? q.get('layout') : (SAVED_KBHUD.cluster || 'hud'));
  const rawCluster = String(rawCl).toLowerCase();
  const cluster = CLUSTERS[rawCluster] ? rawCluster : 'hud';
  const hasThemeQ = q.has('theme');
  const rawTheme = String(q.get('theme') !== null ? q.get('theme') : (SAVED_KBHUD.theme || 'ramp')).toLowerCase();
  const theme = FILL_PRESETS[rawTheme] ? rawTheme : 'ramp';
  const hasFullQ = q.has('full');
  const full = clamp(q.get('full') !== null ? q.get('full') : SAVED_KBHUD.full, 1.0, 5.0, FULL);
  const rawMm = String(q.get('mm') !== null ? q.get('mm') : (SAVED_KBHUD.mm || '1')).toLowerCase();
  const mm = (rawMm === '0' || rawMm === 'false' || rawMm === 'off') ? '0'
           : (rawMm === 'always' || rawMm === '2') ? 'always' : '1';
  const rawAlign = String(q.get('align') !== null ? q.get('align') : (SAVED_KBHUD.align || 'center')).toLowerCase();
  const align = ['center','tl','tr','bl','br'].indexOf(rawAlign) >= 0 ? rawAlign : 'center';
  const opQ = q.get('opacity') !== null ? q.get('opacity') : q.get('alpha');
  return {
    cluster,
    keys: (q.get('keys') !== null ? q.get('keys') : (SAVED_KBHUD.keys || '')).trim(),
    theme,
    full,
    hasThemeQ,
    hasFullQ,
    scale: clamp(q.get('scale') !== null ? q.get('scale') : SAVED_KBHUD.scale, 0.4, 3.0, 1.0),
    opacity: clamp(opQ !== null ? opQ : SAVED_KBHUD.opacity, 0.1, 1.0, 0.85),
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

  kb.classList.toggle('many', items.length > 24);
  kb.textContent = '';
  els = {}; lastRatio = {}; lastHeld = {};
  const frag = document.createDocumentFragment();
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
    frag.appendChild(d);
    els[it.k] = { box: d, fill, mm };
  }
  kb.appendChild(frag);
}

let srvToken = '', lastSrvRev = SAVED_KBHUD_REV, localDirty = false, saveTimer = null, pendingSave = false;
function kbHudPayload() {
  return {
    cluster: cfg.cluster, keys: cfg.keys, theme: cfg.theme,
    full: Number(cfg.full.toFixed(2)),
    scale: Number(cfg.scale.toFixed(2)),
    opacity: Number(cfg.opacity.toFixed(2)),
    mm: cfg.mm, align: cfg.align
  };
}
function flushKbHudSave() {
  if (!srvToken) { pendingSave = true; return; }
  pendingSave = false;
  fetch('/settings', {
    method: 'POST',
    headers: { 'X-Depth-Token': srvToken },
    body: JSON.stringify({ kb_hud: kbHudPayload() })
  }).then(r => r.json()).then(res => {
    if (res && res.kb_hud_rev !== undefined) lastSrvRev = Math.max(lastSrvRev, res.kb_hud_rev);
    if (!saveTimer) localDirty = false;
  }).catch(() => { if (!saveTimer) localDirty = false; });
}
function scheduleKbHudSave() {
  localDirty = true;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveTimer = null; flushKbHudSave(); }, 120);
}

function buildObsUrl(includePanel) {
  return location.origin + '/overlay' + (includePanel ? '?panel=1' : '');
}

const panelEl = document.getElementById('panel');
const pThemes = document.getElementById('pThemes');
for (const id in FILL_PRESETS) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chip';
  b.dataset.theme = id;
  b.innerHTML = '<span class="sw" style="background:' + FILL_PRESETS[id].acc + '"></span>' + FILL_PRESETS[id].label;
  b.addEventListener('click', () => { cfg.theme = id; cfg.hasThemeQ = true; applyConfig(false, true); });
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
  applyConfig(true, true);
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

let _urlTimer = null;
function scheduleUrlSync() {
  if (_urlTimer) clearTimeout(_urlTimer);
  _urlTimer = setTimeout(() => {
    _urlTimer = null;
    try { history.replaceState(null, '', buildObsUrl(cfg.panel)); } catch (e) {}
  }, 120);
}

function applyConfig(rebuild, persist) {
  if (rebuild !== false) {
    buildLayout();
  } else {
    document.documentElement.style.setProperty('--op', cfg.opacity.toFixed(2));
    stage.className = 'align-' + cfg.align;
    document.body.classList.remove('mm-0', 'mm-1', 'mm-always');
    document.body.classList.add('mm-' + cfg.mm);
    lastRatio = {};
    lastHeld = {};
  }
  syncPanelUI();
  if (typeof drawOnce === 'function') drawOnce();
  scheduleUrlSync();
  if (persist) scheduleKbHudSave();
}

document.getElementById('pCluster').addEventListener('change', e => {
  capturingKeys = false;
  cfg.cluster = e.target.value;
  cfg.keys = '';
  pKeysEl.value = '';
  applyConfig(true, true);
});
pKeysEl.addEventListener('input', e => { cfg.keys = e.target.value.trim(); applyConfig(true, true); });
if (pCapBtn) pCapBtn.addEventListener('click', () => {
  capturingKeys = !capturingKeys;
  if (capturingKeys) pCapBtn.blur();
  syncPanelUI();
});
document.getElementById('pScale').addEventListener('input', e => { cfg.scale = parseFloat(e.target.value); applyConfig(true, true); });
document.getElementById('pOpacity').addEventListener('input', e => { cfg.opacity = parseFloat(e.target.value); applyConfig(false, true); });
document.getElementById('pFull').addEventListener('input', e => { cfg.full = parseFloat(e.target.value); cfg.hasFullQ = true; applyConfig(false, true); });
document.getElementById('pMm').addEventListener('change', e => { cfg.mm = e.target.value; applyConfig(false, true); });
document.getElementById('pAlign').addEventListener('change', e => { cfg.align = e.target.value; applyConfig(false, true); });
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
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  localDirty = false;
  flushKbHudSave();
  applyConfig(true, false);
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
let es = null, esTimer = null;

function connectSSE() {
  if (es) { try { es.close(); } catch (e) {} }
  es = new EventSource('/events');
  es.onmessage = ev => {
    lastMsg = Date.now();
    const m = JSON.parse(ev.data);
    if (m.token) {
      srvToken = m.token;
      if (pendingSave) flushKbHudSave();
    }
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
    if (!localDirty && m.kb_hud && typeof m.kb_hud_rev === 'number' && m.kb_hud_rev > lastSrvRev) {
      lastSrvRev = m.kb_hud_rev;
      const h = m.kb_hud;
      const prevCl = cfg.cluster, prevKeys = cfg.keys, prevScale = cfg.scale;
      if (h.cluster && CLUSTERS[h.cluster]) cfg.cluster = h.cluster;
      if (h.keys !== undefined) cfg.keys = String(h.keys || '').trim();
      if (h.theme && FILL_PRESETS[h.theme]) cfg.theme = h.theme;
      if (h.full !== undefined) cfg.full = clamp(h.full, 1.0, 5.0, cfg.full);
      if (h.scale !== undefined) cfg.scale = clamp(h.scale, 0.4, 3.0, cfg.scale);
      if (h.opacity !== undefined) cfg.opacity = clamp(h.opacity, 0.1, 1.0, cfg.opacity);
      if (h.mm && ['0', '1', 'always'].indexOf(h.mm) >= 0) cfg.mm = h.mm;
      if (h.align && ['center', 'tl', 'tr', 'bl', 'br'].indexOf(h.align) >= 0) cfg.align = h.align;
      const needRebuild = (cfg.cluster !== prevCl || cfg.keys !== prevKeys || Math.abs(cfg.scale - prevScale) > 0.001);
      applyConfig(needRebuild, false);
    } else if (lastSrvRev === 0) {
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
    }
  };
  es.onerror = () => {
    if (es && es.readyState === 2 && !esTimer) {
      esTimer = setTimeout(() => { esTimer = null; connectSSE(); }, 1200);
    }
  };
}
connectSSE();
setInterval(() => {
  if (Date.now() - lastMsg > 4000 && !esTimer) { lastMsg = Date.now(); connectSSE(); }
}, 2000);

function drawOnce() {
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
    if (!el || !el.box) continue;
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
}

function draw() {
  try {
    drawOnce();
  } catch (e) {
    /* keep rAF loop alive */
  } finally {
    requestAnimationFrame(draw);
  }
}

applyConfig(true);
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
    content: ""; position: absolute; top: 0; right: 0; bottom: 0; left: 0; inset: 0; pointer-events: none;
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
  #trailCv { position: absolute; top: 0; right: 0; bottom: 0; left: 0; inset: 0; width: 100%; height: 100%; pointer-events: none; }

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
    position: absolute; top: 0; right: 0; bottom: 0; left: 0; inset: 0; z-index: 20; display: none;
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
      <svg viewBox="-22 -18 682 1307" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
        <defs>
          <linearGradient id="viperGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#222631" stop-opacity="0.92"/>
            <stop offset="55%" stop-color="#151820" stop-opacity="0.92"/>
            <stop offset="100%" stop-color="#0f1117" stop-opacity="0.94"/>
          </linearGradient>
          <path id="viperOutline" d="M268.5 1268c-66.5-11-128.7-41.4-173.3-85-75-73.3-104.2-175-92.9-323.1 2.1-27 3.9-69.3 4.1-93.9 1.1-144.3.9-138.4 6.9-144.4 6.6-6.6 7.8-28.3 1.8-33.3-2.9-2.4-4.2-20-5.7-74.5-1.8-63.7-1.5-72.4 2.7-80.5 3.7-7.2 4.3-13.6 3-31.7-7-91.8-8.4-276.4-2.3-293.8 4.1-11.7 17.8-24.6 26-24.6 3.9 0 5.5 2.1 5.7 7.7.2 6.1.9 5.3 2.9-4l2.6-11.7 31.4-13.2C150.6 32 258.8-4 267.3-.8c3 1.1 4 7.5 4 25.6 0 21.5.5 24 4.9 23.2 2.7-.5 5.3-3.6 5.8-6.9 1.1-7.7 7.5-9.3 37.5-9.3 28.3 0 38.7 3 38.7 11.1 0 3.2 1.9 5.7 4.2 5.7 3.4 0 4.2-4.3 4.2-22.1 0-29.4 1.6-30.6 29.6-22.7 50.3 14.1 180.7 62.4 191.2 70.8 1.9 1.5 3.5 7.1 3.5 12.3s1 9.5 2.3 9.5c1.3 0 2.2-2.8 2.1-6.3-.4-11 10.2-9.4 21.5 3.1 8.1 8.9 10.5 14.3 12.2 27.5 3.7 27.6 2 154.9-3.2 243.5-8 136.1-6.8 246.9 4.1 398.2 14.8 205.3 9.9 262.2-29.6 341.6-41.4 83.1-117.7 139.9-215.6 160.3-30.7 6.4-86.6 7.8-116.4 2.9z"/>
          <clipPath id="viperClip"><use href="#viperOutline" xlink:href="#viperOutline"/></clipPath>
        </defs>
        <use href="#viperOutline" xlink:href="#viperOutline" class="m-shell"/>
        <g clip-path="url(#viperClip)">
          <path id="mbL" class="m-btn" d="M 0 0 L 312 0 L 312 486 Q 160 510, 0 462 Z"/>
          <path id="mbR" class="m-btn" d="M 326 0 L 640 0 L 640 462 Q 480 510, 326 486 Z"/>
          <path class="m-seam" d="M 10 462 Q 165 510, 319 488 Q 473 510, 628 462"/>
          <line class="m-seam" x1="319" y1="32" x2="319" y2="488"/>
        </g>
        <use href="#viperOutline" xlink:href="#viperOutline" fill="none" stroke="rgba(255,255,255,0.52)" stroke-width="18" stroke-linejoin="round"/>
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
  <div id="pUipiWarn" class="p-hint" style="display:none;color:#fde68a">检测到管理员权限窗口（如直播姬/游戏），已自动启用桌面光标兜底；若锁视角 FPS 内不动，请以管理员身份运行本程序。</div>
  <div class="p-hint">所有样式与参数调整自动保存并实时同步至 OBS/直播姬；按 C 切面板，按 Home 归中（已避开游戏 R 换弹键）。</div>
</div>

<script>
__THEME_JS__
const PAD_SIZES = { gsr3: [306, 254], sq: [270, 270], wide: [340, 212], xl: [405, 180] };
const SAVED_MHUD = __MOUSE_HUD__;
const SAVED_MSPAN = __MOUSE_SPAN__;
const SAVED_MHUD_REV = __MOUSE_HUD_REV__;
function clamp(v, lo, hi, def) {
  const n = parseFloat(v);
  return isNaN(n) ? def : Math.max(lo, Math.min(hi, n));
}
function readConfig() {
  const q = new URLSearchParams(location.search);
  const rawPad = (q.get('pad') !== null ? q.get('pad') : (SAVED_MHUD.pad || 'gsr3')).toLowerCase();
  const pad = PAD_SIZES[rawPad] ? rawPad : 'gsr3';
  const hasThemeQ = q.has('theme');
  const rawTheme = (q.get('theme') !== null ? q.get('theme') : (SAVED_MHUD.theme || 'ramp')).toLowerCase();
  const theme = FILL_PRESETS[rawTheme] ? rawTheme : 'ramp';
  const hasSpanQ = q.has('span');
  const span = Math.round(clamp(q.get('span') !== null ? q.get('span') : SAVED_MSPAN, 500, 200000, 14800));
  const rawRec = (q.get('recenter') !== null ? q.get('recenter') : (SAVED_MHUD.recenter || 'smart')).toLowerCase();
  const recenter = rawRec === 'gentle' ? 'smart'
                 : (['smart', 'fast', 'off'].indexOf(rawRec) >= 0 ? rawRec : 'smart');
  const rawAlign = (q.get('align') !== null ? q.get('align') : (SAVED_MHUD.align || 'center')).toLowerCase();
  const align = ['center', 'tl', 'tr', 'bl', 'br'].indexOf(rawAlign) >= 0 ? rawAlign : 'center';
  const rawTrail = q.get('trail');
  const trailVal = rawTrail !== null
    ? !(rawTrail.toLowerCase() === '0' || rawTrail.toLowerCase() === 'false' || rawTrail.toLowerCase() === 'off')
    : (SAVED_MHUD.trail !== false);
  const opQ = q.get('opacity') !== null ? q.get('opacity') : q.get('alpha');
  return {
    pad, theme, hasThemeQ, span, hasSpanQ,
    scale: clamp(q.get('scale') !== null ? q.get('scale') : SAVED_MHUD.scale, 0.5, 2.5, 1.0),
    opacity: clamp(opQ !== null ? opQ : SAVED_MHUD.opacity, 0.1, 0.95, 0.62),
    mopacity: clamp(q.get('mopacity') !== null ? q.get('mopacity') : SAVED_MHUD.mopacity, 0.15, 1.0, 0.90),
    sens: clamp(q.get('sens') !== null ? q.get('sens') : SAVED_MHUD.sens, 0.2, 4.0, 1.0),
    recenter, align,
    trail: trailVal,
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
  b.addEventListener('click', () => { cfg.theme = id; cfg.hasThemeQ = true; applyConfig(true); });
  pThemes.appendChild(b);
}

let lastSrvRev = SAVED_MHUD_REV, localDirty = false, saveTimer = null, pendingSave = false;
function mouseHudPayload() {
  return {
    pad: cfg.pad, theme: cfg.theme,
    scale: Number(cfg.scale.toFixed(2)),
    opacity: Number(cfg.opacity.toFixed(2)),
    mopacity: Number(cfg.mopacity.toFixed(2)),
    sens: Number(cfg.sens.toFixed(2)),
    recenter: cfg.recenter, align: cfg.align,
    trail: !!cfg.trail
  };
}
function flushMouseHudSave(extra) {
  if (!srvToken) { pendingSave = true; return; }
  pendingSave = false;
  const body = Object.assign({ mouse_hud: mouseHudPayload() }, extra || {});
  fetch('/settings', {
    method: 'POST',
    headers: { 'X-Depth-Token': srvToken },
    body: JSON.stringify(body)
  }).then(r => r.json()).then(res => {
    if (res && res.mouse_hud_rev !== undefined) lastSrvRev = Math.max(lastSrvRev, res.mouse_hud_rev);
    if (!saveTimer) localDirty = false;
  }).catch(() => { if (!saveTimer) localDirty = false; });
}
function scheduleMouseHudSave() {
  localDirty = true;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveTimer = null; flushMouseHudSave(); }, 120);
}

function buildObsUrl(includePanel) {
  return location.origin + '/overlay/mouse' + (includePanel ? '?panel=1' : '');
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

function applyConfig(persist) {
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
  if (persist) scheduleMouseHudSave();
}

document.getElementById('pPad').addEventListener('change', e => { cfg.pad = e.target.value; applyConfig(true); });
document.getElementById('pScale').addEventListener('input', e => { cfg.scale = parseFloat(e.target.value); applyConfig(true); });
document.getElementById('pOpacity').addEventListener('input', e => { cfg.opacity = parseFloat(e.target.value); applyConfig(true); });
document.getElementById('pMOpacity').addEventListener('input', e => { cfg.mopacity = parseFloat(e.target.value); applyConfig(true); });
document.getElementById('pSens').addEventListener('input', e => { cfg.sens = parseFloat(e.target.value); applyConfig(true); });
document.getElementById('pRecenter').addEventListener('change', e => { cfg.recenter = e.target.value; applyConfig(true); });
document.getElementById('pAlign').addEventListener('change', e => { cfg.align = e.target.value; applyConfig(true); });
document.getElementById('pTrail').addEventListener('change', e => { cfg.trail = e.target.checked; if (!cfg.trail) trail.length = 0; applyConfig(true); });
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
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    localDirty = false;
    flushMouseHudSave({ mouse_span: cfg.span });
    applyConfig(false);
  }
  tx = ty = 0; trail.length = 0;
}

calBtn.addEventListener('click', () => { setCalibrating(!calibrating, false); calBtn.blur(); });
document.getElementById('pCenter').addEventListener('click', () => { tx = ty = 0; trail.length = 0; });
document.getElementById('pReset').addEventListener('click', () => {
  if (calibrating) setCalibrating(false, false);
  cfg = { pad:'gsr3', theme:'ramp', hasThemeQ:false, span:14800, hasSpanQ:false, scale:1.0, opacity:0.62, mopacity:0.90, sens:1.0, recenter:'smart', align:'center', trail:true, panel:cfg.panel };
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  localDirty = false;
  flushMouseHudSave({ mouse_span: 14800 });
  tx = ty = px = py = 0; trail.length = 0; applyConfig(false);
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

let tx = 0, ty = 0, px = 0, py = 0, lastMoveMs = 0, wheelUntil = 0, lastFrameMs = Date.now(), lastMsgMs = Date.now();
const btns = { l: false, r: false, m: false, x1: false, x2: false };
const trail = [];
const mbL = document.getElementById('mbL'), mbR = document.getElementById('mbR');
const mbM = document.getElementById('mbM'), mbX1 = document.getElementById('mbX1');
const mbX2 = document.getElementById('mbX2'), mbLed = document.getElementById('mbLed');
const pUipiWarn = document.getElementById('pUipiWarn');
let es = null, esTimer = null;

function connectSSE() {
  if (es) { try { es.close(); } catch (e) {} }
  es = new EventSource('/events');
  es.onmessage = ev => {
    lastMsgMs = Date.now();
    const m = JSON.parse(ev.data);
    if (m.token) {
      srvToken = m.token;
      if (pendingSave) flushMouseHudSave();
    }
    if (pUipiWarn) pUipiWarn.style.display = m.mouse_uipi ? 'block' : 'none';
    if (!localDirty && m.mouse_hud && typeof m.mouse_hud_rev === 'number' && m.mouse_hud_rev > lastSrvRev) {
      lastSrvRev = m.mouse_hud_rev;
      const h = m.mouse_hud;
      if (h.pad && PAD_SIZES[h.pad]) cfg.pad = h.pad;
      if (h.theme && FILL_PRESETS[h.theme]) cfg.theme = h.theme;
      if (h.scale !== undefined) cfg.scale = clamp(h.scale, 0.5, 2.5, cfg.scale);
      if (h.opacity !== undefined) cfg.opacity = clamp(h.opacity, 0.1, 0.95, cfg.opacity);
      if (h.mopacity !== undefined) cfg.mopacity = clamp(h.mopacity, 0.15, 1.0, cfg.mopacity);
      if (h.sens !== undefined) cfg.sens = clamp(h.sens, 0.2, 4.0, cfg.sens);
      if (h.recenter && ['smart', 'fast', 'off'].indexOf(h.recenter) >= 0) cfg.recenter = h.recenter;
      if (h.align && ['center', 'tl', 'tr', 'bl', 'br'].indexOf(h.align) >= 0) cfg.align = h.align;
      if (h.trail !== undefined) {
        cfg.trail = !!h.trail;
        if (!cfg.trail) trail.length = 0;
      }
      applyConfig(false);
    } else if (lastSrvRev === 0 && !cfg.hasThemeQ && m.theme && FILL_PRESETS[m.theme] && m.theme !== cfg.theme) {
      cfg.theme = m.theme; applyConfig(false);
    }
    if (!cfg.hasSpanQ && m.mouse_span && Math.abs(m.mouse_span - cfg.span) >= 1) {
      cfg.span = Math.round(clamp(m.mouse_span, 500, 200000, 14800));
      syncPanelUI();
    }
    const ms = m.m;
    if (!ms) return;
    const midNow = !!ms.m;
    const nowMs = Date.now();
    if (calibrating) {
      if (ms.dx) {
        calDx += ms.dx;
        const absDx = Math.round(Math.abs(calDx));
        calCountEl.textContent = absDx + ' counts';
        tx = Math.max(-1, Math.min(1, -1 + 2 * Math.min(1, absDx / Math.max(2000, cfg.span))));
        lastMoveMs = nowMs;
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
      lastMoveMs = nowMs;
    }
    if (ms.w) wheelUntil = nowMs + 140;
    btns.l = !!ms.l; btns.r = !!ms.r; btns.m = midNow;
    btns.x1 = !!ms.x1; btns.x2 = !!ms.x2;
  };
  es.onerror = () => {
    if (es && es.readyState === 2 && !esTimer) {
      esTimer = setTimeout(() => { esTimer = null; connectSSE(); }, 1200);
    }
  };
}
connectSSE();
setInterval(() => {
  if (Date.now() - lastMsgMs > 4000 && !esTimer) { lastMsgMs = Date.now(); connectSSE(); }
}, 2000);

function loopOnce() {
  const now = Date.now();
  const dt = Math.max(0.001, Math.min(0.05, (now - lastFrameMs) / 1000));
  lastFrameMs = now;
  const idleMs = Math.max(0, now - lastMoveMs);
  if (!isFinite(tx)) tx = 0;
  if (!isFinite(ty)) ty = 0;
  if (!isFinite(px)) px = 0;
  if (!isFinite(py)) py = 0;
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
  mouseEl.style.transform = 'translate(' + (cx - mouseW * 0.5).toFixed(1) + 'px,' +
    (cy - mouseH * 0.5).toFixed(1) + 'px) rotate(' + tilt.toFixed(2) + 'deg)';

  const moving = idleMs < 180;
  mbL.classList.toggle('on', btns.l);
  mbR.classList.toggle('on', btns.r);
  mbM.classList.toggle('on', btns.m || now < wheelUntil);
  mbX1.classList.toggle('on', btns.x1);
  mbX2.classList.toggle('on', btns.x2);
  mbLed.classList.toggle('hot', moving || btns.l || btns.r);

  if (tCtx) {
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
  }
}

function loop() {
  try {
    loopOnce();
  } catch (e) {
    /* keep rAF loop alive */
  } finally {
    requestAnimationFrame(loop);
  }
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
  :root { --u:44px; --bg:#0a0c10; --card:#11151d; --card2:#161b26; --edge:rgba(255,255,255,.08);
          --edge-hi:rgba(255,255,255,.16); --fg:#f1f5f9; --dim:#8b95a7; --acc:#38bdf8; --ok:#34d399; --bad:#f43f5e; }
  * { box-sizing:border-box; }
  body { margin:0; color:var(--fg); background:radial-gradient(1200px 420px at 50% -120px, rgba(56,189,248,.09), transparent 60%), var(--bg);
         font-family:"Microsoft YaHei UI","Segoe UI",system-ui,sans-serif; font-size:13px; -webkit-font-smoothing:antialiased; }
  .num { font-variant-numeric:tabular-nums; font-family:ui-monospace,"Cascadia Mono",Consolas,monospace; }
  header { position:sticky; top:0; z-index:20; display:flex; align-items:center; gap:10px; padding:10px 20px;
           background:rgba(10,12,16,.84); backdrop-filter:blur(12px); border-bottom:1px solid var(--edge); }
  h1 { display:flex; align-items:center; gap:8px; font-size:14px; margin:0; letter-spacing:.6px; font-weight:600; color:#f8fafc; }
  .brand-dot { width:8px; height:8px; border-radius:50%; background:var(--acc); box-shadow:0 0 10px rgba(56,189,248,.8); }
  a.nav { color:var(--dim); text-decoration:none; font-size:12px; padding:5px 10px; border-radius:7px; border:1px solid transparent; transition:all .15s; }
  a.nav:hover { color:#fff; background:rgba(255,255,255,.06); border-color:var(--edge); }
  .wrap { max-width:1520px; margin:0 auto; padding:14px 20px 28px; display:flex; flex-direction:column; gap:14px; }
  .card { background:var(--card); border:1px solid var(--edge); border-radius:12px; padding:14px 16px; box-shadow:0 6px 24px rgba(0,0,0,.22); }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .sub { color:var(--dim); font-size:11px; letter-spacing:.4px; }
  .pill { display:inline-flex; align-items:center; gap:5px; color:var(--dim); font-size:12px; padding:3px 9px;
          border-radius:7px; background:rgba(255,255,255,.035); border:1px solid var(--edge); }
  .pill b { color:#f1f5f9; font-weight:600; }
  input, select, button { background:#0d1017; color:var(--fg); border:1px solid var(--edge-hi);
          border-radius:7px; height:30px; padding:0 10px; font-size:12px; font-family:inherit; transition:border-color .15s, background .15s; }
  input:focus, select:focus { outline:none; border-color:rgba(56,189,248,.6); }
  select option { background:#11151d; color:#f1f5f9; }
  button { cursor:pointer; display:inline-flex; align-items:center; justify-content:center; gap:5px; font-weight:500; }
  button:hover { background:rgba(255,255,255,.09); border-color:rgba(255,255,255,.24); }
  button.primary { background:rgba(16,185,129,.18); border-color:rgba(52,211,153,.48); color:#6ee7b7; }
  button.primary:hover { background:rgba(16,185,129,.28); border-color:rgba(52,211,153,.72); }
  button.play { background:rgba(56,189,248,.16); border-color:rgba(56,189,248,.48); color:#bae6fd; min-width:104px; font-weight:600; }
  button.play:hover { background:rgba(56,189,248,.24); }
  button.play.on { background:rgba(244,63,94,.22); border-color:rgba(244,63,94,.58); color:#fecdd3; }
  .seg { display:inline-flex; }
  .seg button { border-radius:0; margin-left:-1px; flex:1; padding:0 8px; }
  .seg button:first-child { border-radius:7px 0 0 7px; margin-left:0; }
  .seg button:last-child { border-radius:0 7px 7px 0; }
  input[type="range"].srange { -webkit-appearance:none; appearance:none; width:100%; height:4px; padding:0; border:none;
                               border-radius:999px; background:rgba(255,255,255,.14); cursor:pointer; }
  input[type="range"].srange::-webkit-slider-thumb { -webkit-appearance:none; width:13px; height:13px; border-radius:50%;
                               background:#38bdf8; border:2px solid #0a0c10; box-shadow:0 0 0 1px rgba(56,189,248,.45); }

  .studio-grid { display:grid; grid-template-columns:minmax(0,1fr) 336px; gap:14px; align-items:start; }
  .studio-main { display:flex; flex-direction:column; gap:14px; min-width:0; }
  .stage-card { display:flex; flex-direction:column; gap:10px; padding:12px 14px; }
  .stage-head { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
  .vid-badge { display:inline-flex; align-items:center; gap:7px; padding:4px 10px; border-radius:8px;
               background:rgba(255,255,255,.035); border:1px solid var(--edge); max-width:560px; }
  .vid-badge b { font-size:12px; color:#e2e8f0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #vidStatus { font-size:11px; color:#7dd3fc; }
  #vidPathRow { display:none; align-items:center; gap:8px; }
  #vidPathRow.on { display:flex; }
  #prevStage { display:flex; align-items:center; justify-content:center; gap:26px; flex-wrap:wrap;
               min-height:240px; padding:18px 10px; overflow:hidden; position:relative; border-radius:10px;
               border:1px solid var(--edge); background-color:#0b0e14;
               background-image:radial-gradient(rgba(255,255,255,.06) 1px, transparent 1px); background-size:18px 18px; }
  #prevStage.has-video { display:block; width:min(100%, calc(62vh * var(--vid-ar, 1.7778)));
                          aspect-ratio:var(--vid-ar, 16/9); min-height:260px; margin:0 auto; padding:0;
                          background:#05070a; background-image:none; border:1px solid var(--edge-hi);
                          box-shadow:0 14px 36px rgba(0,0,0,.48); }
  #wbVideo { display:none; width:100%; height:100%; object-fit:contain; background:#000; }
  #prevStage.has-video #wbVideo { display:block; }
  .transport-bar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding-top:4px; border-top:1px solid var(--edge); }
  .inspector { display:flex; flex-direction:column; padding:0; overflow:hidden; position:sticky; top:64px; }
  .ins-sec { padding:12px 14px; display:flex; flex-direction:column; gap:9px; }
  .ins-sec + .ins-sec { border-top:1px solid var(--edge); }
  .ins-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .ins-title { font-size:11px; font-weight:600; letter-spacing:.8px; color:#cbd5e1; }
  .fgrid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .field { display:flex; flex-direction:column; gap:4px; min-width:0; }
  .field label { font-size:11px; color:var(--dim); }
  .field input, .field select { width:100%; }
  .srow { display:grid; grid-template-columns:64px 1fr 34px; align-items:center; gap:8px; font-size:11px; color:var(--dim); }
  .srow b { text-align:right; color:#e2e8f0; font-weight:500; }
  #vidCtrlBar { display:none; flex-direction:column; gap:9px; }
  #vidCtrlBar.on { display:flex; }
  .hud-layer { position:relative; flex-shrink:0; }
  #prevStage.has-video .hud-layer { position:absolute; left:0; top:0; transform-origin:0 0; cursor:grab;
                                    touch-action:none; z-index:4; pointer-events:auto; border-radius:8px; }
  #prevStage.has-video .hud-layer:hover { outline:1px dashed rgba(56,189,248,.65); outline-offset:4px; }
  #prevStage.has-video .hud-layer.dragging { cursor:grabbing; outline:1.5px solid #38bdf8; outline-offset:4px; }
  .hud-handle { display:none; position:absolute; right:-8px; bottom:-8px; width:14px; height:14px; border-radius:4px;
                background:#38bdf8; border:2px solid #0b0e13; cursor:nwse-resize; z-index:6; }
  #prevStage.has-video .hud-layer:hover .hud-handle,
  #prevStage.has-video .hud-layer.dragging .hud-handle { display:block; }
  .hud-tag { display:none; position:absolute; left:0; top:-20px; font-size:10px; padding:1px 6px; border-radius:4px;
             background:rgba(11,14,19,.86); color:#bae6fd; border:1px solid rgba(56,189,248,.45); white-space:nowrap; pointer-events:none; }
  #prevStage.has-video .hud-layer:hover .hud-tag,
  #prevStage.has-video .hud-layer.dragging .hud-tag { display:block; }
  .stage-guide { display:none; position:absolute; pointer-events:none; z-index:3; background:rgba(56,189,248,.55); }
  #guideV { top:0; bottom:0; left:50%; width:1px; }
  #guideH { left:0; right:0; top:50%; height:1px; }
  .stage-guide.on { display:block; }
  #kbPrev { position:relative; flex-shrink:0; }
  #wbPadWrap { position:relative; flex-shrink:0; display:flex; flex-direction:column; align-items:center; gap:4px; }
  #wbPadWrap.off { display:none; }
  #wbPad { position:relative; width:220px; height:182px; border-radius:11px;
           background:rgba(14,16,20,.84); border:1px solid rgba(255,255,255,.14);
           overflow:hidden; user-select:none; }
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
       background:rgba(14,16,20,.82); border:1px solid rgba(255,255,255,.13);
       user-select:none; }
  .k .fill { position:absolute; left:0; right:0; bottom:0; height:0%; pointer-events:none; }
  .k .lbl { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
            font-size:calc(var(--u) * 0.24); font-weight:600; color:rgba(255,255,255,.72);
            text-shadow:0 1px 2px rgba(0,0,0,.65); pointer-events:none; }
  .k.hot .lbl { color:#fff; }
  .k .mm { position:absolute; right:4px; bottom:3px; z-index:2; font-size:calc(var(--u) * 0.17);
           color:rgba(255,255,255,.9); text-shadow:0 1px 2px rgba(0,0,0,.75); opacity:0; pointer-events:none; }
  .k.hot .mm { opacity:1; }
  .k.dim { opacity:.32; }

  #hoverRead { font-size:12px; color:#cbd5e1; min-height:22px; display:flex; gap:6px; flex-wrap:wrap; align-items:center; flex:1; }
  #hoverRead .hk { background:rgba(56,189,248,.12); border:1px solid rgba(56,189,248,.32); color:#bae6fd;
                   padding:2px 7px; border-radius:6px; font-size:11px; }
  #hoverRead .hk.m { background:rgba(244,63,94,.16); border-color:rgba(244,63,94,.42); color:#fecdd3; }
  #tracks { position:relative; margin-top:10px; }
  #cv { width:100%; height:264px; display:block; background:#090c11; border:1px solid var(--edge);
        border-radius:9px; cursor:crosshair; }
  #hint { color:var(--dim); font-size:11px; margin-top:8px; display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; }
  .key-chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .kchip { display:inline-flex; align-items:center; gap:5px; padding:3px 9px; border-radius:7px; font-size:11px;
           background:rgba(255,255,255,.04); border:1px solid var(--edge); color:var(--dim); cursor:pointer; user-select:none; transition:all .15s; }
  .kchip.on { background:rgba(56,189,248,.16); border-color:rgba(56,189,248,.48); color:#e0f2fe; }
  .kchip input { margin:0; width:auto; height:auto; padding:0; accent-color:#38bdf8; }
  #prog { height:6px; background:rgba(255,255,255,.07); border-radius:999px; overflow:hidden; margin-top:4px; }
  #prog i { display:block; height:100%; width:0%; background:linear-gradient(90deg,#38bdf8,#34d399); transition:width .15s; }
  #log { color:var(--dim); font-size:11px; white-space:pre-wrap; line-height:1.45; min-height:16px; }
  #histDrawer { display:none; }
  #histDrawer.open { display:block; }
  .hist-list { display:flex; flex-direction:column; gap:6px; max-height:220px; overflow-y:auto; margin-top:8px; }
  .hist-item { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:7px 10px;
               border-radius:8px; background:rgba(255,255,255,.03); border:1px solid var(--edge); cursor:pointer; }
  .hist-item:hover { background:rgba(255,255,255,.06); }
  .hist-item.on { background:rgba(56,189,248,.13); border-color:rgba(56,189,248,.48); }
  .hist-meta { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:12px; }
  .hist-tag { font-size:10px; padding:1px 6px; border-radius:999px; background:rgba(52,211,153,.16);
              color:#6ee7b7; border:1px solid rgba(52,211,153,.35); }
  .hist-tag.m { background:rgba(56,189,248,.16); color:#bae6fd; border-color:rgba(56,189,248,.38); }
  .hist-keys { font-size:11px; color:var(--dim); max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  button.danger { color:#fca5a5; border-color:rgba(239,68,68,.35); background:rgba(239,68,68,.10); padding:0 9px; font-size:11px; }
  button.danger:hover { background:rgba(239,68,68,.22); }
  .kpi-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:10px; }
  .kpi { background:linear-gradient(180deg, rgba(255,255,255,.035), rgba(255,255,255,.015));
         border:1px solid var(--edge); border-radius:9px; padding:10px 12px; }
  .kpi .kl { font-size:11px; color:var(--dim); }
  .kpi .kv { font-size:19px; font-weight:700; margin-top:3px; color:#f8fafc; }
  .kpi .ks { font-size:11px; color:var(--dim); margin-top:2px; }
  .diag-cols { display:grid; grid-template-columns:1fr 1fr 1.25fr; gap:10px; margin-top:10px; }
  .diag-box { background:#0d1017; border:1px solid var(--edge); border-radius:9px; padding:9px 11px; }
  .diag-list { display:flex; flex-direction:column; gap:5px; max-height:142px; overflow-y:auto; margin-top:6px; }
  .diag-item { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:5px 8px; border-radius:6px;
               background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.07); cursor:pointer; font-size:12px; }
  .diag-item:hover { background:rgba(56,189,248,.12); border-color:rgba(56,189,248,.38); }
  .tag-bad { color:#fda4af; font-weight:600; }
  .tag-ok { color:#6ee7b7; font-weight:600; }
  .ks-tbl { width:100%; border-collapse:collapse; font-size:11px; margin-top:6px; }
  .ks-tbl th { text-align:left; color:var(--dim); font-weight:500; padding:3px 4px; border-bottom:1px solid var(--edge); }
  .ks-tbl td { padding:4px 4px; border-bottom:1px solid rgba(255,255,255,.04); }
  @media (max-width:1120px) { .studio-grid { grid-template-columns:1fr; } .inspector { position:static; } }
  @media (max-width:920px) { .kpi-grid { grid-template-columns:repeat(2,1fr); } .diag-cols { grid-template-columns:1fr; } }
</style></head><body>
<header>
  <h1><span class="brand-dot"></span>素材工作台</h1>
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

  <div class="studio-grid">
    <div class="studio-main">
    <div class="card stage-card">
      <div class="stage-head">
        <div class="row" style="min-width:0;flex:1">
          <span class="vid-badge"><span class="sub">录像</span><b id="vidName" class="num">未导入（纯 HUD 预览）</b></span>
          <span id="vidStatus"></span>
        </div>
        <div class="row">
          <button type="button" class="primary" onclick="pickVideo()">导入录像…</button>
          <button type="button" onclick="togglePathInput()">粘贴路径</button>
          <button id="vidCloseBtn" type="button" class="danger" style="display:none" onclick="clearVideo()">关闭录像</button>
        </div>
      </div>
      <div id="vidPathRow">
        <input id="vidPathIn" placeholder="输入本地录像路径，例如 F:\录屏\Valorant\xxx.mp4" style="flex:1">
        <button type="button" class="primary" onclick="loadVideoByPath()">加载</button>
      </div>
      <div id="prevStage">
      <video id="wbVideo" controls preload="metadata" playsinline></video>
      <div id="guideV" class="stage-guide"></div>
      <div id="guideH" class="stage-guide"></div>
      <div id="kbLayer" class="hud-layer">
        <span class="hud-tag">键盘 HUD（拖动 · 右下角缩放）</span>
        <div id="kbPrev"></div>
        <div class="hud-handle" data-target="kb" title="拖动缩放键盘 HUD"></div>
      </div>
      <div id="msLayer" class="hud-layer">
        <span class="hud-tag">鼠标 HUD（拖动 · 右下角缩放）</span>
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
        <div class="hud-handle" data-target="mouse" title="拖动缩放鼠标 HUD"></div>
      </div>
      </div>
      <div class="transport-bar">
        <button id="playBtn" class="play" type="button" onclick="togglePlay()">▶ 试播选区</button>
        <span class="pill">时刻<b id="cur_t" class="num">0.000 s</b></span>
        <div id="hoverRead" class="num"><span style="color:var(--dim)">移动鼠标到时间轴查看实时按键深度</span></div>
      </div>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between">
        <div class="row">
          <span class="sub">起点</span><input id="sel_start" class="num" size="7" value="0.000">
          <span class="sub">终点</span><input id="sel_end" class="num" size="7" value="0.000">
          <span class="pill">选中<b id="sel_len" class="num">0.000</b> 秒</span>
        </div>
        <div class="row">
          <label id="showDiagChip" class="kchip on" style="margin:0"><input id="showDiag" type="checkbox" checked onchange="toggleDiag()">急停标注</label>
          <div class="seg"><button type="button" onclick="zoom(0.5)">放大</button><button type="button" onclick="zoom(2)">缩小</button><button type="button" onclick="resetView()">全览</button></div>
        </div>
      </div>
      <div id="tracks"><canvas id="cv"></canvas></div>
      <div id="hint">
        <span>拖拽选区 · 滚轮缩放 · 悬停实时同步预览键鼠 · 红/绿区段=急停重叠/完美 · 双击清选 · Space 播/停 · ←/→ 微调</span>
        <span id="viewSpan" class="num">0.000s — 0.000s</span>
      </div>
    </div>
    </div>

    <div class="card inspector">
      <div id="vidCtrlBar" class="ins-sec">
        <div class="ins-head">
          <span class="ins-title">录像对齐与剪辑</span>
          <button type="button" style="height:24px;font-size:11px;padding:0 8px" onclick="resetStageLayout()">复位排版</button>
        </div>
        <div class="row" style="gap:6px">
          <div class="field" style="flex:1"><label>时间偏移 (s)</label><input id="vidOffIn" class="num" type="number" step="0.016" value="0.000" onchange="setVideoOffset(this.value)"></div>
          <button type="button" style="align-self:flex-end" onclick="fineAlignVideo()" title="按当前画面附近枪声与左键脉冲自动对齐">声画精对齐</button>
        </div>
        <div class="seg" style="width:100%">
          <button type="button" onclick="nudgeVideoOffset(-0.1)">-0.1s</button>
          <button type="button" onclick="nudgeVideoOffset(-0.016)">-1帧</button>
          <button type="button" onclick="nudgeVideoOffset(0.016)">+1帧</button>
          <button type="button" onclick="nudgeVideoOffset(0.1)">+0.1s</button>
        </div>
        <div class="seg" style="width:100%">
          <button type="button" onclick="markSelFromVideo(0)" title="把当前视频时刻设为选区起点">设选区起点</button>
          <button type="button" onclick="markSelFromVideo(1)" title="把当前视频时刻设为选区终点">设选区终点</button>
        </div>
        <div class="srow"><span>键盘透度</span><input id="stKbAlpha" class="srange" type="range" min="0.15" max="1" step="0.05" value="0.72" oninput="onStageOpacityChange()"><b id="stKbAlphaVal" class="num">72%</b></div>
        <div class="srow"><span>鼠标垫底</span><input id="stMsAlpha" class="srange" type="range" min="0.1" max="0.95" step="0.05" value="0.55" oninput="onStageOpacityChange()"><b id="stMsAlphaVal" class="num">55%</b></div>
        <div class="srow"><span>鼠标本体</span><input id="stMsBody" class="srange" type="range" min="0.2" max="1" step="0.05" value="0.85" oninput="onStageOpacityChange()"><b id="stMsBodyVal" class="num">85%</b></div>
      </div>

      <div class="ins-sec">
        <div class="ins-head">
          <span class="ins-title">HUD 外观</span>
          <label id="showMouseChip" class="kchip on" style="margin:0"><input id="showMouse" type="checkbox" checked onchange="toggleWbMouse()">同步鼠标</label>
        </div>
        <div class="fgrid">
          <div class="field"><label>键位布局</label>
            <select id="layout" onchange="buildPreviewKb()"><option value="hud">HUD 键簇</option><option value="wasd">WASD 极简</option><option value="full">75% 整键盘</option></select>
          </div>
          <div class="field"><label>深度配色</label>
            <select id="theme" onchange="renderPreviewAt(activeTime())"><option value="ramp">随深度平滑</option><option value="cyan">冰川青</option><option value="mint">薄荷绿</option><option value="amber">琥珀金</option><option value="crimson">绯红</option><option value="violet">紫罗兰</option></select>
          </div>
        </div>
        <div class="field"><label>满程基准 (mm)</label><input id="wbFull" class="num" type="number" min="1.0" max="5.0" step="0.05" value="3.50" onchange="onWbFullChange(this.value)"></div>
      </div>

      <div class="ins-sec">
        <div class="ins-head">
          <span class="ins-title">导出成片 / HUD</span>
          <label class="kchip on" id="expMouseChip" style="margin:0"><input id="expMouse" type="checkbox" checked onchange="this.parentNode.classList.toggle('on',this.checked)">含鼠标</label>
        </div>
        <div class="field"><label>导出方式</label>
          <select id="expMode" title="选择导出合成视频、三轨文件或纯透明 HUD"><option value="hud">仅透明 HUD</option><option value="composite">一键合成成片 MP4</option><option value="tracks">三轨分开导出（视频+键+鼠）</option></select>
        </div>
        <div class="fgrid">
          <div class="field"><label>分辨率</label><select id="size"><option value="1920x1080">1080p</option><option value="2560x1440">1440p</option><option value="3840x2160">4K</option></select></div>
          <div class="field"><label>帧率</label><select id="fps"><option>30</option><option selected>60</option><option>120</option></select></div>
          <div class="field"><label>倍速</label><select id="speed"><option value="0.25">0.25x</option><option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option></select></div>
          <div class="field"><label>透明格式</label><select id="fmt"><option value="webm" selected>WebM VP9</option><option value="prores">ProRes 4444</option><option value="png">PNG 序列</option></select></div>
          <div class="field"><label>背景底色</label><select id="bg"><option value="">透明</option><option value="#101014">深色 #101014</option><option value="#ffffff">白色</option></select></div>
          <div class="field"><label>HUD 排版</label><select id="expMouseLayout" title="仅透明 HUD 模式下的键鼠排版"><option value="side">左右并排</option><option value="stack">上下叠放</option><option value="only">仅鼠标垫</option></select></div>
        </div>
        <button class="primary" type="button" style="width:100%;height:34px;font-weight:600" onclick="startExport()">开始导出</button>
        <div class="row" style="gap:6px">
          <button type="button" style="flex:1" onclick="openExportsFolder()">打开目录</button>
          <button type="button" style="flex:1" onclick="cancelExport()">取消导出</button>
        </div>
        <div id="prog"><i></i></div>
        <div id="log">—</div>
      </div>
    </div>
  </div>

  <div id="diagCard" class="card">
    <div class="row" style="justify-content:space-between">
      <div class="row">
        <span class="sub" style="color:#e4e4e7;font-weight:600">FPS 实战诊断与复盘</span>
        <span class="sub">急停键对</span>
        <select id="diagPair" onchange="loadAnalysis()">
          <option value="A,D">A / D</option>
          <option value="W,S">W / S</option>
        </select>
      </div>
      <div class="row">
        <button id="exportCardBtn" class="primary" type="button" onclick="exportReviewCard()">一键导出复盘统计图 (PNG)</button>
      </div>
    </div>
    <div class="kpi-grid">
      <div class="kpi"><div class="kl">急停纯净度</div><div id="k_clean" class="kv num">—</div><div id="k_clean_s" class="ks num">暂无急停切换</div></div>
      <div class="kpi"><div class="kl">平均重叠 / 最差</div><div id="k_ov" class="kv num">—</div><div id="k_ov_s" class="ks num">平均空窗 —</div></div>
      <div class="kpi"><div class="kl">急停到位开枪率</div><div id="k_shot" class="kv num">—</div><div id="k_shot_s" class="ks num">需含鼠标录制</div></div>
      <div class="kpi"><div class="kl">触发/峰值一致性</div><div id="k_cons" class="kv num">—</div><div id="k_cons_s" class="ks num">标准差越小越稳</div></div>
    </div>
    <div class="diag-cols">
      <div class="diag-box">
        <div class="sub">最差急停（点击跳转复盘）</div>
        <div id="worstStrafeList" class="diag-list"><span class="sub">—</span></div>
      </div>
      <div class="diag-box">
        <div class="sub">移动开枪瞬间（点击跳转复盘）</div>
        <div id="movingShotList" class="diag-list"><span class="sub">—</span></div>
      </div>
      <div class="diag-box">
        <div class="sub">单键频次 / 均深 / 峰值一致性 (mm)</div>
        <div style="max-height:142px;overflow-y:auto">
          <table class="ks-tbl">
            <thead><tr><th>键</th><th>次</th><th>均深</th><th>峰值均值±σ</th><th>首触±σ</th></tr></thead>
            <tbody id="keyStatsBody"><tr><td colspan="5" class="sub">—</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <span class="sub">导出键位（勾选控制导出与波形高亮）</span>
      <div class="seg">
        <button type="button" onclick="selectKeysPreset('all')">全选</button>
        <button type="button" onclick="selectKeysPreset('hud')">常用 HUD</button>
        <button type="button" onclick="selectKeysPreset('none')">清空</button>
      </div>
    </div>
    <div id="keyChips" class="key-chips"><span class="sub">加载录制后显示可选键位…</span></div>
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
const bgCv = document.createElement('canvas'), bgCtx = bgCv.getContext('2d');
const wbTrailCv = document.getElementById('wbTrail'), wbTrailCtx = wbTrailCv ? wbTrailCv.getContext('2d') : null;
const RULER_H = 22, CV_H = 264;
let info = null, data = null, detailData = null, playData = null, analysis = null, view = [0, 1], sel = [0, 0], dragging = false, dragFrom = 0;
let hoverT = null, playT = 0, playing = false, playLastMs = 0, showMouse = true, showDiag = true;
let selectedKeys = new Set(), selectedNorm = new Set(), prevEls = {}, lastHoverHtml = '', lastPadAcc = '', wbTrailSized = false;
let cvW = 800, cvDpr = 1, waveDirty = true, lodTimer = null, lodReqId = 0, playLodReqId = 0, playLodInflight = false, playLodTargetEnd = -1;

function syncSelectedNorm() { selectedNorm = new Set(Array.from(selectedKeys).map(norm)); }
function fmt(t) { return (t || 0).toFixed(3); }
function activeTime() { return hoverT !== null ? hoverT : playT; }

function fit() {
  const dpr = window.devicePixelRatio || 1;
  const r = cv.getBoundingClientRect();
  cvW = Math.max(200, r.width);
  cvDpr = dpr;
  const pw = Math.round(cvW * dpr), ph = Math.round(CV_H * dpr);
  cv.width = pw; cv.height = ph;
  bgCv.width = pw; bgCv.height = ph;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  bgCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  waveDirty = true;
  draw(true);
}

function xOf(t) { return (t - view[0]) / Math.max(1e-6, view[1] - view[0]) * cvW; }
function tOf(x) { return view[0] + x / Math.max(1, cvW) * (view[1] - view[0]); }

function lowerBound(arr, target) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] < target) lo = mid + 1; else hi = mid;
  }
  return lo;
}
function lowerBoundBy(arr, target, getT) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (getT(arr[mid]) < target) lo = mid + 1; else hi = mid;
  }
  return lo;
}
function activeSeriesFor(t) {
  if (playData && playData.ok && playData.t && playData.t.length &&
      t >= (playData.view_start || 0) - 0.02 && t <= (playData.view_end || 0) + 0.02) {
    return playData;
  }
  if (detailData && detailData.ok && detailData.t && detailData.t.length &&
      t >= (detailData.view_start || 0) - 0.02 && t <= (detailData.view_end || 0) + 0.02) {
    return detailData;
  }
  return data;
}
function ensurePlaybackHd(t, forceNow) {
  if (!data || !data.ok || (data.step || 0) <= 0.025) return;
  const dur = data.duration || 0;
  if (dur <= 0) return;
  const needHi = Math.min(dur, t + 4.0);
  if (playData && playData.ok && (playData.step || 1) <= 0.025 &&
      t >= (playData.view_start || 0) - 0.02 && needHi <= (playData.view_end || 0) + 0.02) {
    return;
  }
  if (detailData && detailData.ok && (detailData.step || 1) <= 0.025 &&
      t >= (detailData.view_start || 0) - 0.02 && needHi <= (detailData.view_end || 0) + 0.02) {
    return;
  }
  if (playLodInflight && !forceNow) return;
  if (playLodInflight && t <= playLodTargetEnd && t >= (playData ? (playData.view_start || 0) : 0)) return;
  const reqId = ++playLodReqId;
  const a = Math.max(0, t - 2.0);
  const b = Math.min(dur, t + 18.0);
  playLodInflight = true;
  playLodTargetEnd = b;
  const q = '?limit=2200&start=' + a.toFixed(3) + '&end=' + b.toFixed(3) +
            (currentStamp ? ('&stamp=' + encodeURIComponent(currentStamp)) : '');
  fetch('/wb/series' + q).then(r => r.json()).then(res => {
    if (reqId !== playLodReqId) return;
    playLodInflight = false;
    if (res && res.ok) {
      playData = res;
      if (!playing || hoverT === null) renderPreviewAt(activeTime());
    }
  }).catch(() => { if (reqId === playLodReqId) playLodInflight = false; });
}
function activeWaveData() {
  if (detailData && detailData.ok && detailData.t && detailData.t.length &&
      (detailData.view_start || 0) <= view[0] + 0.05 && (detailData.view_end || 0) >= view[1] - 0.05) {
    return detailData;
  }
  return data;
}

function sampleAt(t) {
  const out = {};
  const src = activeSeriesFor(t);
  if (!src || !src.ok || !src.t || !src.t.length) return out;
  const ts = src.t, n = ts.length;
  let lo = 0, hi = n - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid + 1; else hi = mid - 1;
  }
  const i = Math.max(0, Math.min(n - 1, hi));
  const j = Math.min(n - 1, i + 1);
  const dt = ts[j] - ts[i];
  const f = (dt > 1e-6 && dt <= 0.12 && t >= ts[i]) ? Math.max(0, Math.min(1, (t - ts[i]) / dt)) : 0;
  for (const k of src.keys) {
    const arr = src.v[k];
    if (!arr) continue;
    const v = (f > 0) ? (arr[i] + (arr[j] - arr[i]) * f) : arr[i];
    out[k] = v;
    out[norm(k)] = v;
  }
  return out;
}

function sampleMouseAt(t) {
  const src = activeSeriesFor(t);
  if (!src || !src.ok || !src.mouse || !src.t || !src.t.length) return null;
  const ts = src.t, m = src.mouse, n = Math.min(ts.length, (m.x || []).length);
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
  return { x, y, b: (m.b && m.b[i]) || 0, w: (m.w && m.w[i]) || 0, spd: (m.spd && m.spd[i]) || 0, idx: i, src };
}

function chooseTickStep(span, pxW) {
  const target = Math.max(4, Math.floor(pxW / 90));
  const raw = span / target;
  const steps = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60];
  for (const s of steps) if (s >= raw) return s;
  return 60;
}

function renderWaveCache() {
  const w = cvW, h = CV_H, c = bgCtx;
  c.clearRect(0, 0, w, h);
  const src = activeWaveData();
  if (!src || !src.ok) {
    c.fillStyle = 'rgba(255,255,255,0.45)'; c.font = '13px sans-serif';
    c.fillText(src && src.error ? ('读取失败：' + src.error) : '没有录制数据（请先在主监测页录制并停止）', 16, 44);
    return;
  }
  const span = Math.max(1e-3, view[1] - view[0]);
  const vs = document.getElementById('viewSpan');
  if (vs) vs.textContent = fmt(view[0]) + 's — ' + fmt(view[1]) + 's' + (src === detailData ? ' · 高清' : '');

  c.fillStyle = 'rgba(255,255,255,0.03)';
  c.fillRect(0, 0, w, RULER_H);
  c.strokeStyle = 'rgba(255,255,255,0.09)';
  c.beginPath(); c.moveTo(0, RULER_H + 0.5); c.lineTo(w, RULER_H + 0.5); c.stroke();

  const step = chooseTickStep(span, w);
  const firstTick = Math.ceil(view[0] / step) * step;
  const dec = step < 0.1 ? 2 : (step < 1 ? 1 : 0);
  c.font = '10px ui-monospace, Consolas, monospace';
  for (let t = firstTick; t <= view[1] + 1e-6; t += step) {
    const x = Math.round(xOf(t)) + 0.5;
    c.strokeStyle = 'rgba(255,255,255,0.05)';
    c.beginPath(); c.moveTo(x, RULER_H); c.lineTo(x, h); c.stroke();
    c.strokeStyle = 'rgba(255,255,255,0.28)';
    c.beginPath(); c.moveTo(x, RULER_H - 6); c.lineTo(x, RULER_H); c.stroke();
    c.fillStyle = 'rgba(255,255,255,0.52)';
    c.fillText(t.toFixed(dec) + 's', x + 3, 13);
  }

  const x0 = xOf(sel[0]), x1 = xOf(sel[1]);
  if (sel[1] > sel[0]) {
    c.fillStyle = 'rgba(56,189,248,0.13)';
    c.fillRect(x0, RULER_H, Math.max(1, x1 - x0), h - RULER_H);
    c.strokeStyle = 'rgba(56,189,248,0.85)';
    c.beginPath();
    c.moveTo(x0, RULER_H); c.lineTo(x0, h);
    c.moveTo(x1, RULER_H); c.lineTo(x1, h);
    c.stroke();
  }

  if (showDiag && analysis && analysis.ok && analysis.strafes && analysis.strafes.events) {
    const sevs = analysis.strafes.events;
    let si = lowerBoundBy(sevs, view[0], e => e.t1);
    for (; si < sevs.length; si++) {
      const ev = sevs[si];
      if (ev.t0 > view[1]) break;
      const sx0 = xOf(ev.t0), sw = Math.max(2, xOf(ev.t1) - sx0);
      const isBad = ev.kind === 'overlap' && !ev.clean;
      c.fillStyle = isBad ? 'rgba(244,63,94,0.22)' : (ev.clean ? 'rgba(52,211,153,0.16)' : 'rgba(251,191,36,0.16)');
      c.fillRect(sx0, RULER_H, sw, h - RULER_H);
      c.fillStyle = isBad ? '#f43f5e' : (ev.clean ? '#34d399' : '#fbbf24');
      c.fillRect(sx0, RULER_H, sw, 3);
    }
  }

  const keys = src.keys || [], n = keys.length, ts = src.t || [];
  const i0 = Math.max(0, lowerBound(ts, view[0]) - 1);
  const i1 = Math.min(ts.length - 1, lowerBound(ts, view[1]));
  const hasMouseLane = !!(showMouse && src.mouse && src.mouse.x && src.mouse.x.length);
  const MOUSE_H = hasMouseLane ? (n ? 34 : (h - RULER_H - 6)) : 0;
  const keyAreaH = h - RULER_H - 6 - MOUSE_H;
  const rowH = keyAreaH / Math.max(1, n);
  const trackCols = ['#38bdf8', '#34d399', '#fbbf24', '#f472b6', '#a78bfa', '#fb7185'];
  keys.forEach((k, row) => {
    const on = selectedKeys.has(k);
    const y = RULER_H + 3 + row * rowH, base = y + rowH - 3;
    c.fillStyle = on ? 'rgba(255,255,255,0.78)' : 'rgba(255,255,255,0.24)';
    c.fillText(disp(norm(k)), 5, y + 10);
    c.strokeStyle = 'rgba(255,255,255,0.04)';
    c.beginPath(); c.moveTo(0, base); c.lineTo(w, base); c.stroke();
    const vals = src.v[k] || [];
    c.beginPath();
    let started = false;
    const scaleH = Math.max(6, rowH - 11);
    for (let i = i0; i <= i1; i++) {
      const x = xOf(ts[i]);
      const yv = base - Math.min(1, vals[i] / FULL) * scaleH;
      if (!started) { c.moveTo(x, yv); started = true; } else c.lineTo(x, yv);
    }
    c.strokeStyle = on ? trackCols[row % trackCols.length] : 'rgba(255,255,255,0.16)';
    c.lineWidth = on ? 1.35 : 0.8;
    c.stroke();
  });

  if (hasMouseLane) {
    const m = src.mouse, my = RULER_H + 3 + keyAreaH, mBase = h - 4, mH = Math.max(12, MOUSE_H - 12);
    c.strokeStyle = 'rgba(255,255,255,0.11)';
    c.lineWidth = 1;
    c.beginPath(); c.moveTo(0, my + 0.5); c.lineTo(w, my + 0.5); c.stroke();
    c.fillStyle = 'rgba(255,255,255,0.72)';
    c.fillText('Mouse', 5, my + 11);
    const spds = m.spd || [], bs = m.b || [], ws = m.w || [];
    const nM = Math.min(ts.length, spds.length);
    const mi0 = Math.max(0, lowerBound(ts, view[0]) - 1);
    const mi1 = Math.min(nM - 1, lowerBound(ts, view[1]));
    let maxS = 1.2;
    for (let i = mi0; i <= mi1; i++) if (spds[i] > maxS) maxS = spds[i];
    maxS = Math.min(maxS, 5.0);
    c.beginPath();
    let started = false, firstX = 0, lastX = 0;
    for (let i = mi0; i <= mi1; i++) {
      const x = xOf(ts[i]);
      const yv = mBase - Math.min(1, spds[i] / maxS) * mH;
      if (!started) { c.moveTo(x, yv); firstX = x; started = true; } else c.lineTo(x, yv);
      lastX = x;
    }
    if (started) {
      c.strokeStyle = 'rgba(56,189,248,0.82)';
      c.lineWidth = 1.25;
      c.stroke();
      c.lineTo(lastX, mBase); c.lineTo(firstX, mBase); c.closePath();
      c.fillStyle = 'rgba(56,189,248,0.14)';
      c.fill();
    }
    const clicks = m.clicks, wheels = m.wheels;
    if (Array.isArray(clicks)) {
      let ci = lowerBoundBy(clicks, view[0] - 5.0, cl => cl[0]);
      for (; ci < clicks.length; ci++) {
        const [ct, bit, durMs] = clicks[ci];
        if (ct > view[1]) break;
        const ctEnd = ct + (durMs || 0) / 1000;
        if (ctEnd < view[0]) continue;
        const x0m = xOf(ct), bw = Math.max(2, xOf(ctEnd) - x0m);
        if (bit === 1) { c.fillStyle = 'rgba(244,63,94,0.84)'; c.fillRect(x0m, my + 2, bw, 5); }
        else if (bit === 2) { c.fillStyle = 'rgba(251,191,36,0.84)'; c.fillRect(x0m, my + 8, bw, 4); }
        else { c.fillStyle = 'rgba(52,211,153,0.84)'; c.fillRect(x0m, my + 13, bw, 3); }
      }
      if (Array.isArray(wheels)) {
        c.fillStyle = 'rgba(125,211,252,0.88)';
        let wi = lowerBoundBy(wheels, view[0], wh => wh[0]);
        for (; wi < wheels.length; wi++) {
          const tw = wheels[wi][0];
          if (tw > view[1]) break;
          c.fillRect(xOf(tw), my + 13, 2, 4);
        }
      }
    } else {
      for (let i = mi0; i <= mi1; i++) {
        const b = bs[i] || 0, wh = ws[i] || 0;
        if (!b && !wh) continue;
        const x0m = xOf(ts[i]), x1m = i + 1 < nM ? xOf(ts[i + 1]) : x0m + 2;
        const bw = Math.max(2, x1m - x0m);
        if (b & 1) { c.fillStyle = 'rgba(244,63,94,0.78)'; c.fillRect(x0m, my + 2, bw, 5); }
        if (b & 2) { c.fillStyle = 'rgba(251,191,36,0.78)'; c.fillRect(x0m, my + 8, bw, 4); }
        if ((b & 28) || wh) { c.fillStyle = 'rgba(52,211,153,0.78)'; c.fillRect(x0m, my + 13, bw, 3); }
      }
    }
    if (showDiag && analysis && analysis.ok && analysis.shots && analysis.shots.all) {
      c.fillStyle = '#fb923c';
      const shAll = analysis.shots.all;
      let shi = lowerBoundBy(shAll, view[0], s => s[0]);
      for (; shi < shAll.length; shi++) {
        const s = shAll[shi];
        if (s[0] > view[1]) break;
        if (s[2]) continue;
        const sx = xOf(s[0]);
        c.beginPath(); c.moveTo(sx, my + 1); c.lineTo(sx - 4, my - 5); c.lineTo(sx + 4, my - 5); c.closePath(); c.fill();
      }
    }
  }
}

function draw(forceWave) {
  if (forceWave || waveDirty) {
    renderWaveCache();
    waveDirty = false;
  }
  const w = cvW, h = CV_H;
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(bgCv, 0, 0, w, h);
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

function scheduleLodFetch() {
  clearTimeout(lodTimer);
  if (!data || !data.ok || (data.step || 0) <= 0.025) return;
  const dur = data.duration || 1;
  const span = Math.max(0.05, view[1] - view[0]);
  if (span >= dur * 0.7) {
    if (detailData) { detailData = null; draw(true); }
    return;
  }
  const wantStep = Math.max(0.015, span / 1200);
  if (detailData && detailData.ok &&
      (detailData.view_start || 0) <= view[0] + 0.02 &&
      (detailData.view_end || 0) >= view[1] - 0.02 &&
      (detailData.step || 1) <= wantStep) {
    return;
  }
  lodTimer = setTimeout(() => {
    const reqId = ++lodReqId;
    const pad = Math.min(8, Math.max(0.5, span * 0.2));
    const a = Math.max(0, view[0] - pad).toFixed(3);
    const b = Math.min(dur, view[1] + pad).toFixed(3);
    const q = '?limit=2000&start=' + a + '&end=' + b + (currentStamp ? ('&stamp=' + encodeURIComponent(currentStamp)) : '');
    fetch('/wb/series' + q).then(r => r.json()).then(res => {
      if (reqId !== lodReqId || !res || !res.ok) return;
      detailData = res;
      draw(true);
      renderPreviewAt(activeTime());
    }).catch(() => {});
  }, 140);
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
  draw(true);
  if (!dragging) {
    ensurePlaybackHd(activeTime(), true);
    syncVideoToPlayT();
  }
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
  else { draw(false); renderPreviewAt(t); }
});
cv.addEventListener('mouseleave', () => {
  hoverT = null;
  draw(false);
  renderPreviewAt(playT);
});
window.addEventListener('mouseup', () => {
  if (dragging) {
    dragging = false;
    ensurePlaybackHd(activeTime(), true);
    syncVideoToPlayT();
  }
});
cv.addEventListener('dblclick', () => setSel(0, 0));
cv.addEventListener('wheel', e => {
  e.preventDefault();
  const dur = (data && data.ok) ? data.duration : 1;
  const z = e.deltaY > 0 ? 1.25 : 0.8;
  const pivot = Math.max(0, Math.min(dur, tOf(e.offsetX)));
  const a = Math.max(0, pivot - (pivot - view[0]) * z);
  const b = Math.min(dur, pivot + (view[1] - pivot) * z);
  if (b - a >= 0.05) view = [a, b];
  draw(true);
  scheduleLodFetch();
}, { passive: false });

function zoom(f) {
  const dur = (data && data.ok) ? data.duration : 1;
  const mid = (view[0] + view[1]) / 2, half = Math.max(0.05, (view[1] - view[0]) / 2 * f);
  view = [Math.max(0, mid - half), Math.min(dur, mid + half)];
  draw(true);
  scheduleLodFetch();
}
function resetView() { view = [0, data && data.ok ? data.duration : 1]; detailData = null; draw(true); }

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
  const baseU = lay === 'full' ? (vidInfo ? 30 : 38) : (vidInfo ? 42 : 50);
  const u = vidInfo ? baseU : Math.max(24, Math.min(baseU, Math.floor(availW / (maxX + 0.2))));
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
  applyStageLayout();
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
  draw(true);
}

const elCurT = document.getElementById('cur_t'), elHoverRead = document.getElementById('hoverRead');
const elTheme = document.getElementById('theme'), elPad = document.getElementById('wbPad');
const elMouse = document.getElementById('wbMouse'), elMouseEmpty = document.getElementById('wbMouseEmpty');
const elMbL = document.getElementById('wbMbL'), elMbR = document.getElementById('wbMbR');
const elMbM = document.getElementById('wbMbM'), elMbX1 = document.getElementById('wbMbX1');
const elMbX2 = document.getElementById('wbMbX2'), elMbLed = document.getElementById('wbMbLed');

function renderMousePreviewAt(t) {
  if (!elPad || !elMouse) return;
  const preset = FILL_PRESETS[(elTheme && elTheme.value) || 'ramp'] || FILL_PRESETS.ramp;
  const acc = preset.style(0.78).line || '#38bdf8';
  if (acc !== lastPadAcc) {
    lastPadAcc = acc;
    elPad.style.setProperty('--acc', acc);
  }
  if (!showMouse) return;
  const ms = sampleMouseAt(t);
  const hasData = !!ms;
  if (elMouseEmpty) elMouseEmpty.classList.toggle('on', !hasData);
  elMouse.style.display = hasData ? '' : 'none';
  const PW = 220, PH = 182, MW = 32, MH = 60, IN = 7;
  if (wbTrailCv && wbTrailCtx) {
    const dpr = window.devicePixelRatio || 1;
    if (!wbTrailSized || wbTrailCv.width !== Math.round(PW * dpr) || wbTrailCv.height !== Math.round(PH * dpr)) {
      wbTrailCv.width = Math.round(PW * dpr); wbTrailCv.height = Math.round(PH * dpr);
      wbTrailCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
      wbTrailSized = true;
    }
    wbTrailCtx.clearRect(0, 0, PW, PH);
  }
  if (!hasData) return;
  const toXY = (nx, ny) => [
    IN + MW / 2 + (nx + 1) * 0.5 * (PW - MW - IN * 2),
    IN + MH / 2 + (ny + 1) * 0.5 * (PH - MH - IN * 2)
  ];
  const [cx, cy] = toXY(ms.x, ms.y);
  elMouse.style.transform = 'translate(' + (cx - MW / 2).toFixed(1) + 'px,' + (cy - MH / 2).toFixed(1) + 'px)';
  const b = ms.b || 0;
  if (elMbL) elMbL.classList.toggle('on', !!(b & 1));
  if (elMbR) elMbR.classList.toggle('on', !!(b & 2));
  if (elMbM) elMbM.classList.toggle('on', !!((b & 4) || ms.w !== 0));
  if (elMbX1) elMbX1.classList.toggle('on', !!(b & 8));
  if (elMbX2) elMbX2.classList.toggle('on', !!(b & 16));
  if (elMbLed) elMbLed.classList.toggle('hot', b !== 0 || ms.spd > 0.25);
  const src = ms.src || data;
  if (wbTrailCtx && src && src.t && src.mouse) {
    const ts = src.t, mx = src.mouse.x, my = src.mouse.y, win = 0.55;
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

let vidInfo = null, videoOffset = 0.0, syncingVid = false, syncVidTimer = null, loopSelection = false, saveStageTimer = null;
let stageCfg = { kb: { x: 0.03, y: 0.66, scale: 0.85, alpha: 0.72 }, mouse: { x: 0.76, y: 0.64, scale: 0.90, alpha: 0.55, mopacity: 0.85 } };

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function applyStageLayout() {
  const stage = document.getElementById('prevStage');
  const kbL = document.getElementById('kbLayer'), msL = document.getElementById('msLayer');
  const kbEl = document.getElementById('kbPrev'), padEl = document.getElementById('wbPad'), msEl = document.getElementById('wbMouse');
  if (!stage || !kbL || !msL) return;
  if (!vidInfo) {
    kbL.style.left = ''; kbL.style.top = ''; kbL.style.transform = '';
    msL.style.left = ''; msL.style.top = ''; msL.style.transform = '';
    if (kbEl) kbEl.style.opacity = '';
    if (padEl) padEl.style.background = '';
    if (msEl) msEl.style.opacity = '';
    return;
  }
  const sw = Math.max(1, stage.clientWidth), sh = Math.max(1, stage.clientHeight);
  kbL.style.left = (clamp(stageCfg.kb.x, 0, 0.94) * sw).toFixed(1) + 'px';
  kbL.style.top = (clamp(stageCfg.kb.y, 0, 0.94) * sh).toFixed(1) + 'px';
  kbL.style.transform = 'scale(' + clamp(stageCfg.kb.scale, 0.35, 2.5).toFixed(3) + ')';
  msL.style.left = (clamp(stageCfg.mouse.x, 0, 0.94) * sw).toFixed(1) + 'px';
  msL.style.top = (clamp(stageCfg.mouse.y, 0, 0.94) * sh).toFixed(1) + 'px';
  msL.style.transform = 'scale(' + clamp(stageCfg.mouse.scale, 0.35, 2.5).toFixed(3) + ')';
  const ka = stageCfg.kb.alpha != null ? stageCfg.kb.alpha : 0.72;
  const ma = stageCfg.mouse.alpha != null ? stageCfg.mouse.alpha : 0.55;
  const mp = stageCfg.mouse.mopacity != null ? stageCfg.mouse.mopacity : 0.85;
  if (kbEl) kbEl.style.opacity = String(ka);
  if (padEl) padEl.style.background = 'rgba(14,16,20,' + ma.toFixed(2) + ')';
  if (msEl) msEl.style.opacity = String(mp);
  const s1 = document.getElementById('stKbAlpha'), s2 = document.getElementById('stMsAlpha'), s3 = document.getElementById('stMsBody');
  if (s1) s1.value = String(ka);
  if (s2) s2.value = String(ma);
  if (s3) s3.value = String(mp);
  const v1 = document.getElementById('stKbAlphaVal'), v2 = document.getElementById('stMsAlphaVal'), v3 = document.getElementById('stMsBodyVal');
  if (v1) v1.textContent = Math.round(ka * 100) + '%';
  if (v2) v2.textContent = Math.round(ma * 100) + '%';
  if (v3) v3.textContent = Math.round(mp * 100) + '%';
}

function onStageOpacityChange() {
  const s1 = document.getElementById('stKbAlpha'), s2 = document.getElementById('stMsAlpha'), s3 = document.getElementById('stMsBody');
  if (s1) stageCfg.kb.alpha = parseFloat(s1.value) || 0.72;
  if (s2) stageCfg.mouse.alpha = parseFloat(s2.value) || 0.55;
  if (s3) stageCfg.mouse.mopacity = parseFloat(s3.value) || 0.85;
  applyStageLayout();
  saveStageSoon();
}

function saveStageSoon() {
  clearTimeout(saveStageTimer);
  saveStageTimer = setTimeout(() => {
    fetch('/wb/video/layout', {
      method: 'POST', headers: { 'X-Depth-Token': window.__token || '' },
      body: JSON.stringify(stageCfg)
    }).catch(() => {});
  }, 180);
}

function resetStageLayout() {
  stageCfg = { kb: { x: 0.03, y: 0.66, scale: 0.85, alpha: 0.72 }, mouse: { x: 0.76, y: 0.64, scale: 0.90, alpha: 0.55, mopacity: 0.85 } };
  applyStageLayout();
  saveStageSoon();
}

function initHudDrag() {
  const stage = document.getElementById('prevStage');
  const gV = document.getElementById('guideV'), gH = document.getElementById('guideH');
  for (const [id, key] of [['kbLayer', 'kb'], ['msLayer', 'mouse']]) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.addEventListener('pointerdown', ev => {
      if (!vidInfo || ev.button !== 0) return;
      ev.preventDefault(); ev.stopPropagation();
      const isScale = ev.target && ev.target.classList && ev.target.classList.contains('hud-handle');
      const sw = Math.max(1, stage.clientWidth), sh = Math.max(1, stage.clientHeight);
      const sx = ev.clientX, sy = ev.clientY;
      const orig = Object.assign({}, stageCfg[key]);
      const baseW = Math.max(40, el.offsetWidth), baseH = Math.max(40, el.offsetHeight);
      el.classList.add('dragging');
      if (el.setPointerCapture) try { el.setPointerCapture(ev.pointerId); } catch (e) {}
      const onMove = mv => {
        const dx = mv.clientX - sx, dy = mv.clientY - sy;
        if (isScale) {
          stageCfg[key].scale = clamp(orig.scale + (dx + dy) / 220, 0.35, 2.4);
        } else {
          let nx = clamp(orig.x + dx / sw, 0.0, 0.94);
          let ny = clamp(orig.y + dy / sh, 0.0, 0.94);
          const cx = nx + (baseW * stageCfg[key].scale * 0.5) / sw;
          const cy = ny + (baseH * stageCfg[key].scale * 0.5) / sh;
          const snapV = Math.abs(cx - 0.5) < 0.015;
          const snapH = Math.abs(cy - 0.5) < 0.015;
          if (snapV) nx = 0.5 - (baseW * stageCfg[key].scale * 0.5) / sw;
          if (snapH) ny = 0.5 - (baseH * stageCfg[key].scale * 0.5) / sh;
          if (Math.abs(nx - 0.03) < 0.012) nx = 0.03;
          if (Math.abs(ny - 0.04) < 0.012) ny = 0.04;
          if (gV) gV.classList.toggle('on', snapV);
          if (gH) gH.classList.toggle('on', snapH);
          stageCfg[key].x = clamp(nx, 0.0, 0.94);
          stageCfg[key].y = clamp(ny, 0.0, 0.94);
        }
        applyStageLayout();
      };
      const onUp = () => {
        el.classList.remove('dragging');
        if (gV) gV.classList.remove('on');
        if (gH) gH.classList.remove('on');
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        saveStageSoon();
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    });
  }
}

function renderPreviewAt(t) {
  if (elCurT) elCurT.textContent = fmt(t) + ' s';
  const vals = sampleAt(t);
  const preset = FILL_PRESETS[(elTheme && elTheme.value) || 'ramp'] || FILL_PRESETS.ramp;
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

  if (elHoverRead && data && data.ok) {
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
    const html = chips.length ? chips.join('') : '<span style="color:var(--dim)">当前时刻无按下键</span>';
    if (html !== lastHoverHtml) {
      lastHoverHtml = html;
      elHoverRead.innerHTML = html;
    }
  }
}

function setPlayUI(on) {
  playing = !!on;
  const btn = document.getElementById('playBtn');
  if (btn) {
    btn.classList.toggle('on', playing);
    btn.textContent = playing ? '⏸ 暂停试播' : '▶ 试播选区';
  }
}

function selValidForVideo() {
  if (!(sel[1] > sel[0])) return false;
  if (!vidInfo) return true;
  const vDur = vidInfo.duration || (document.getElementById('wbVideo') || {}).duration || 0;
  return sel[1] > videoOffset + 0.05 && sel[0] < videoOffset + vDur - 0.05;
}

function syncVideoToPlayT() {
  const v = document.getElementById('wbVideo');
  if (!vidInfo || !v || !v.src || syncingVid) return;
  const vt = clamp(playT - videoOffset, 0, Math.max(0, (vidInfo.duration || v.duration || 0) - 0.02));
  if (Math.abs((v.currentTime || 0) - vt) > 0.04) {
    syncingVid = true;
    clearTimeout(syncVidTimer);
    try { v.currentTime = vt; } catch (e) {}
    syncVidTimer = setTimeout(() => { syncingVid = false; }, 250);
  }
}

function togglePlay() {
  if (!data || !data.ok) return;
  const v = document.getElementById('wbVideo');
  const next = !playing;
  if (next) {
    const useSel = selValidForVideo();
    loopSelection = useSel;
    const vDur = vidInfo ? (vidInfo.duration || (v && v.duration) || data.duration) : data.duration;
    const lo = useSel ? (vidInfo ? Math.max(sel[0], videoOffset) : sel[0]) : 0;
    const hi = useSel ? (vidInfo ? Math.min(sel[1], videoOffset + vDur) : sel[1]) : data.duration;
    if (useSel && (playT < lo || playT >= hi)) playT = lo;
    ensurePlaybackHd(playT, true);
    if (vidInfo && v && v.src) {
      if (useSel) syncVideoToPlayT();
      else playT = clamp((v.currentTime || 0) + videoOffset, 0, data.duration || 1e9);
      v.playbackRate = Number(document.getElementById('speed').value) || 1;
      setPlayUI(true);
      v.play().catch(() => { setPlayUI(false); });
    } else {
      setPlayUI(true);
    }
    playLastMs = performance.now();
    requestAnimationFrame(playLoop);
  } else {
    loopSelection = false;
    setPlayUI(false);
    if (vidInfo && v && !v.paused) v.pause();
  }
}

function playLoop(now) {
  if (!playing || !data || !data.ok) return;
  const spd = Number(document.getElementById('speed').value) || 1;
  const useSel = loopSelection && selValidForVideo();
  const v = document.getElementById('wbVideo');
  const vDur = vidInfo ? (vidInfo.duration || (v && v.duration) || data.duration) : data.duration;
  const lo = useSel ? (vidInfo ? Math.max(sel[0], videoOffset) : sel[0]) : 0;
  const hi = useSel ? (vidInfo ? Math.min(sel[1], videoOffset + vDur) : sel[1]) : data.duration;
  if (vidInfo && v && !v.paused && !v.seeking) {
    v.playbackRate = spd;
    playT = clamp((v.currentTime || 0) + videoOffset, 0, data.duration || 1e9);
    if (useSel && playT >= hi) {
      playT = lo;
      syncVideoToPlayT();
    }
    ensurePlaybackHd(playT, false);
  } else if (!(vidInfo && v && !v.paused)) {
    const dt = Math.min(0.1, (now - playLastMs) / 1000);
    playT += dt * spd;
    if (playT > hi) { playT = lo; ensurePlaybackHd(playT, true); }
    else { ensurePlaybackHd(playT, false); }
  }
  playLastMs = now;
  draw(false);
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
      syncSelectedNorm();
      lbl.classList.toggle('on', cb.checked);
      draw(true);
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
  syncSelectedNorm();
  renderKeyChips();
  draw(true);
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
  detailData = null;
  playData = null;
  lodReqId++;
  playLodReqId++;
  playLodInflight = false;
  playLodTargetEnd = -1;
  const qs = stamp ? ('?stamp=' + encodeURIComponent(stamp)) : '';
  fetch('/wb/info' + qs).then(r => r.json()).then(j => {
    info = j;
    if (!j.ok) {
      document.getElementById('r_stamp').textContent = '—';
      document.getElementById('r_dur').textContent = '—';
      document.getElementById('r_n').textContent = '—';
      document.getElementById('log').textContent = j.error || '没有录制';
      data = j; selectedKeys.clear(); syncSelectedNorm(); renderKeyChips(); draw(true); buildPreviewKb(); return;
    }
    currentStamp = j.stamp || stamp || '';
    document.getElementById('r_stamp').textContent = currentStamp || '—';
    document.getElementById('r_dur').textContent = (j.duration || 0).toFixed(2) + ' s';
    document.getElementById('r_n').textContent = j.samples;
    renderHistoryUI();
    selectedKeys = new Set(j.keys || []);
    syncSelectedNorm();
    renderKeyChips();
    const sq = '?step=0.02' + (currentStamp ? ('&stamp=' + encodeURIComponent(currentStamp)) : '');
    fetch('/wb/series' + sq).then(r => r.json()).then(k => {
      data = k;
      if (k.ok) {
        view = [0, k.duration];
        const v = document.getElementById('wbVideo');
        if (vidInfo && v) {
          if (!selValidForVideo()) {
            sel = [0, 0];
            document.getElementById('sel_start').value = '0.000';
            document.getElementById('sel_end').value = '0.000';
            document.getElementById('sel_len').textContent = '0.000';
          }
          playT = clamp((v.currentTime || 0) + videoOffset, 0, k.duration);
          hoverT = null;
          ensurePlaybackHd(playT, true);
        } else {
          playT = 0;
          hoverT = null;
          setSel(0, Math.min(k.duration, 5));
        }
      }
      buildPreviewKb();
      draw(true);
      loadAnalysis();
    });
  });
}

function toggleDiag() {
  const cb = document.getElementById('showDiag');
  showDiag = cb ? cb.checked : true;
  const chip = document.getElementById('showDiagChip');
  if (chip) chip.classList.toggle('on', showDiag);
  draw(true);
}

function jumpToMoment(t) {
  if (!data || !data.ok) return;
  if (playing) togglePlay();
  const dur = data.duration || 1;
  playT = Math.max(0, Math.min(dur, t));
  hoverT = null;
  const half = Math.min(0.8, Math.max(0.25, dur * 0.2));
  view = [Math.max(0, playT - half), Math.min(dur, playT + half)];
  draw(true);
  ensurePlaybackHd(playT, true);
  syncVideoToPlayT();
  scheduleLodFetch();
  renderPreviewAt(playT);
}

function loadAnalysis() {
  const pairEl = document.getElementById('diagPair');
  const pair = pairEl ? pairEl.value : 'A,D';
  const q = '?pair=' + encodeURIComponent(pair) + (currentStamp ? ('&stamp=' + encodeURIComponent(currentStamp)) : '');
  fetch('/wb/analysis' + q).then(r => r.json()).then(j => {
    analysis = j;
    renderAnalysis();
    draw(true);
  }).catch(() => { analysis = null; renderAnalysis(); });
}

function renderAnalysis() {
  const sEl = document.getElementById('worstStrafeList');
  const mEl = document.getElementById('movingShotList');
  const kEl = document.getElementById('keyStatsBody');
  const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  if (!analysis || !analysis.ok) {
    setTxt('k_clean', '—'); setTxt('k_clean_s', '暂无数据');
    setTxt('k_ov', '—'); setTxt('k_ov_s', '—');
    setTxt('k_shot', '—'); setTxt('k_shot_s', '需含鼠标录制');
    setTxt('k_cons', '—'); setTxt('k_cons_s', '—');
    if (sEl) sEl.innerHTML = '<span class="sub">—</span>';
    if (mEl) mEl.innerHTML = '<span class="sub">—</span>';
    if (kEl) kEl.innerHTML = '<tr><td colspan="5" class="sub">—</td></tr>';
    return;
  }
  const st = analysis.strafes || {};
  setTxt('k_clean', st.clean_pct != null ? (st.clean_pct.toFixed(1) + '%') : '—');
  setTxt('k_clean_s', st.total ? (st.clean_count + '/' + st.total + ' 次切换纯净') : '当前键对无切换');
  setTxt('k_ov', st.overlap_count ? (st.avg_overlap_ms.toFixed(1) + ' ms') : '0.0 ms');
  setTxt('k_ov_s', '最差 ' + (st.max_overlap_ms || 0).toFixed(1) + 'ms · 均空窗 ' + (st.avg_gap_ms || 0).toFixed(1) + 'ms');
  const sh = analysis.shots;
  if (sh && sh.total) {
    setTxt('k_shot', sh.still_pct.toFixed(1) + '%');
    setTxt('k_shot_s', sh.still_count + '/' + sh.total + ' 枪到位 · 均移深 ' + sh.avg_move_mm.toFixed(2) + 'mm');
  } else {
    setTxt('k_shot', '—');
    setTxt('k_shot_s', sh ? '未检测到左键开火' : '该录制无鼠标数据');
  }
  const ks = analysis.key_stats || [];
  const mainKs = ks.filter(x => x.presses >= 2);
  if (mainKs.length) {
    const avgStd = mainKs.reduce((a, b) => a + b.peak_std, 0) / mainKs.length;
    setTxt('k_cons', '±' + avgStd.toFixed(2) + ' mm');
    setTxt('k_cons_s', '统计 ' + mainKs.length + ' 个高频键峰值标准差');
  } else {
    setTxt('k_cons', '—'); setTxt('k_cons_s', '按键次数不足 2 次');
  }
  if (sEl) {
    const ws = st.worst || [];
    sEl.innerHTML = ws.length
      ? ws.map(e => '<div class="diag-item" onclick="jumpToMoment(' + e.t0 + ')"><span><b class="num">' + fmt(e.t0) + 's</b> ' + e.dir + '</span><span class="tag-bad num">重叠 ' + e.ms.toFixed(1) + 'ms</span></div>').join('')
      : '<span class="tag-ok" style="font-size:12px">' + (st.total ? '全部急停均很纯净 ✓' : '未检测到重叠急停') + '</span>';
  }
  if (mEl) {
    const mv = (sh && sh.moving) || [];
    mEl.innerHTML = mv.length
      ? mv.map(e => '<div class="diag-item" onclick="jumpToMoment(' + e.t + ')"><span><b class="num">' + fmt(e.t) + 's</b> 按住 ' + disp(norm(e.key)) + '</span><span class="tag-bad num">' + e.mm.toFixed(2) + ' mm</span></div>').join('')
      : '<span class="sub">' + (sh && sh.total ? '所有开枪均已急停到位 ✓' : '无移动开枪记录') + '</span>';
  }
  if (kEl) {
    kEl.innerHTML = ks.length
      ? ks.slice(0, 12).map(x => '<tr><td><b>' + disp(norm(x.key)) + '</b></td><td class="num">' + x.presses + '</td><td class="num">' + x.avg_mm.toFixed(2) + '</td><td class="num">' + x.peak_mean.toFixed(2) + '±' + x.peak_std.toFixed(2) + '</td><td class="num">' + x.trig_mean.toFixed(2) + '±' + x.trig_std.toFixed(2) + '</td></tr>').join('')
      : '<tr><td colspan="5" class="sub">无键盘采样</td></tr>';
  }
}

function _rrect(c, x, y, w, h, r) {
  c.beginPath();
  c.moveTo(x + r, y);
  c.arcTo(x + w, y, x + w, y + h, r);
  c.arcTo(x + w, y + h, x, y + h, r);
  c.arcTo(x, y + h, x, y, r);
  c.arcTo(x, y, x + w, y, r);
  c.closePath();
}

function renderReviewCardCanvas() {
  if (!analysis || !analysis.ok) return null;
  const W = 1920, H = 1080;
  const off = document.createElement('canvas');
  off.width = W; off.height = H;
  const g = off.getContext('2d');
  // Background + subtle ambient glow
  g.fillStyle = '#0b0e14'; g.fillRect(0, 0, W, H);
  const bg1 = g.createRadialGradient(340, 120, 40, 340, 120, 680);
  bg1.addColorStop(0, 'rgba(56,189,248,0.12)'); bg1.addColorStop(1, 'rgba(11,14,20,0)');
  g.fillStyle = bg1; g.fillRect(0, 0, W, H);
  const bg2 = g.createRadialGradient(1620, 920, 40, 1620, 920, 640);
  bg2.addColorStop(0, 'rgba(244,63,94,0.10)'); bg2.addColorStop(1, 'rgba(11,14,20,0)');
  g.fillStyle = bg2; g.fillRect(0, 0, W, H);

  // Header
  g.fillStyle = '#f8fafc'; g.font = '700 34px "Microsoft YaHei UI","Segoe UI",sans-serif';
  g.fillText('FPS 磁轴实战复盘卡片', 60, 74);
  g.fillStyle = 'rgba(226,232,240,0.58)'; g.font = '500 18px ui-monospace,Consolas,monospace';
  const meta = 'ROG Falchion Ace 75 HE  ·  ' + (analysis.stamp || '—') + '  ·  ' + (analysis.duration || 0).toFixed(2) + 's  ·  满程 ' + FULL.toFixed(2) + 'mm';
  g.textAlign = 'right'; g.fillText(meta, W - 60, 72); g.textAlign = 'left';

  // Top 4 KPI cards
  const st = analysis.strafes || {}, sh = analysis.shots, ks = analysis.key_stats || [];
  const mainKs = ks.filter(x => x.presses >= 2);
  const avgStd = mainKs.length ? (mainKs.reduce((a, b) => a + b.peak_std, 0) / mainKs.length) : null;
  const kpis = [
    ['急停纯净度 (' + (st.pair ? st.pair.join('/') : 'A/D') + ')', st.clean_pct != null ? st.clean_pct.toFixed(1) + '%' : '—',
     st.total ? (st.clean_count + '/' + st.total + ' 次切换纯净') : '无切换数据', st.clean_pct >= 80 ? '#34d399' : '#fbbf24'],
    ['平均重叠 / 最差重叠', st.overlap_count ? st.avg_overlap_ms.toFixed(1) + ' ms' : '0.0 ms',
     '最差 ' + (st.max_overlap_ms || 0).toFixed(1) + ' ms · 均空窗 ' + (st.avg_gap_ms || 0).toFixed(1) + ' ms', st.avg_overlap_ms > 25 ? '#fb7185' : '#38bdf8'],
    ['急停到位开枪率', (sh && sh.total) ? sh.still_pct.toFixed(1) + '%' : '—',
     (sh && sh.total) ? (sh.still_count + '/' + sh.total + ' 枪到位 · 均移深 ' + sh.avg_move_mm.toFixed(2) + 'mm') : '无鼠标开火数据', (sh && sh.still_pct >= 80) ? '#34d399' : '#fb923c'],
    ['磁轴峰值一致性', avgStd != null ? ('±' + avgStd.toFixed(2) + ' mm') : '—',
     ks.length ? ('Top 键 ' + disp(norm(ks[0].key)) + ' (' + ks[0].presses + '次 / 均深 ' + ks[0].avg_mm.toFixed(2) + 'mm)') : '无按键数据', '#a78bfa']
  ];
  const kw = 432, kgap = 24;
  kpis.forEach((kp, idx) => {
    const x = 60 + idx * (kw + kgap), y = 104, kh = 132;
    _rrect(g, x, y, kw, kh, 14);
    g.fillStyle = 'rgba(255,255,255,0.035)'; g.fill();
    g.strokeStyle = 'rgba(255,255,255,0.10)'; g.lineWidth = 1.5; g.stroke();
    g.fillStyle = 'rgba(226,232,240,0.62)'; g.font = '500 17px "Microsoft YaHei UI",sans-serif';
    g.fillText(kp[0], x + 22, y + 34);
    g.fillStyle = kp[3]; g.font = '700 40px ui-monospace,Consolas,sans-serif';
    g.fillText(kp[1], x + 22, y + 84);
    g.fillStyle = 'rgba(226,232,240,0.56)'; g.font = '400 15px "Microsoft YaHei UI",sans-serif';
    g.fillText(kp[2], x + 22, y + 114);
  });
  return off;
}

function drawReviewCardBody(g, an) {
  // Left panel: 75% keyboard frequency & avg depth heatmap
  const lx = 60, ly = 260, lw = 1100, lh = 466;
  _rrect(g, lx, ly, lw, lh, 16);
  g.fillStyle = 'rgba(255,255,255,0.03)'; g.fill();
  g.strokeStyle = 'rgba(255,255,255,0.10)'; g.lineWidth = 1.5; g.stroke();
  g.fillStyle = '#e2e8f0'; g.font = '600 20px "Microsoft YaHei UI",sans-serif';
  g.fillText('键盘频次 & 平均深度热图', lx + 24, ly + 38);
  const kmap = {};
  let maxP = 1;
  for (const s of (an.key_stats || [])) {
    kmap[norm(s.key)] = s;
    if (s.presses > maxP) maxP = s.presses;
  }
  const u = 62, gap = 6, ox = lx + 30, oy = ly + 64;
  for (const it of FULL_LAYOUT) {
    const kx = ox + it.x * u, ky = oy + it.r * (u + gap), kw = it.w * u - gap, kh = u - gap;
    const s = kmap[it.k];
    _rrect(g, kx, ky, kw, kh, 8);
    if (s && s.presses > 0) {
      const heat = Math.min(1, 0.18 + 0.82 * (s.presses / maxP));
      const [R, G, B] = smoothRamp(heat);
      g.fillStyle = 'rgba(' + R + ',' + G + ',' + B + ',0.38)'; g.fill();
      g.strokeStyle = 'rgba(' + R + ',' + G + ',' + B + ',0.85)'; g.lineWidth = 1.5; g.stroke();
      g.fillStyle = '#ffffff'; g.font = '700 15px "Segoe UI",sans-serif';
      g.fillText(disp(it.k), kx + 7, ky + 20);
      g.fillStyle = 'rgba(255,255,255,0.92)'; g.font = '600 11px ui-monospace,Consolas,monospace';
      g.fillText(s.presses + '×', kx + 7, ky + 36);
      g.fillStyle = 'rgba(255,255,255,0.72)';
      g.fillText(s.avg_mm.toFixed(1) + 'm', kx + 7, ky + 49);
    } else {
      g.fillStyle = 'rgba(255,255,255,0.03)'; g.fill();
      g.strokeStyle = 'rgba(255,255,255,0.08)'; g.lineWidth = 1; g.stroke();
      g.fillStyle = 'rgba(255,255,255,0.30)'; g.font = '500 13px "Segoe UI",sans-serif';
      g.fillText(disp(it.k), kx + 7, ky + 22);
    }
  }

  // Right panel: Zowie G-SR III mousepad activity heatmap + LMB shot points
  const rx = 1184, ry = 260, rw = 676, rh = 466;
  _rrect(g, rx, ry, rw, rh, 16);
  g.fillStyle = 'rgba(255,255,255,0.03)'; g.fill();
  g.strokeStyle = 'rgba(255,255,255,0.10)'; g.lineWidth = 1.5; g.stroke();
  g.fillStyle = '#e2e8f0'; g.font = '600 20px "Microsoft YaHei UI",sans-serif';
  g.fillText('鼠标垫活动热力图 · 红点=左键开火', rx + 24, ry + 38);
  const px = rx + 114, py = ry + 64, pw = 448, ph = 372;
  _rrect(g, px, py, pw, ph, 14);
  g.fillStyle = '#0e1118'; g.fill();
  g.strokeStyle = 'rgba(255,255,255,0.18)'; g.lineWidth = 1.5; g.stroke();
  g.strokeStyle = 'rgba(255,255,255,0.07)'; g.lineWidth = 1;
  g.beginPath(); g.moveTo(px + pw / 2, py); g.lineTo(px + pw / 2, py + ph); g.moveTo(px, py + ph / 2); g.lineTo(px + pw, py + ph / 2); g.stroke();
  const mh = an.mouse_heat;
  if (mh && mh.max > 0) {
    const cw = pw / mh.w, ch = ph / mh.h;
    for (let gy = 0; gy < mh.h; gy++) {
      for (let gx = 0; gx < mh.w; gx++) {
        const v = mh.grid[gy * mh.w + gx];
        if (!v) continue;
        const r = Math.pow(v / mh.max, 0.55);
        const [R, G, B] = smoothRamp(r);
        const cx = px + (gx + 0.5) * cw, cy = py + (gy + 0.5) * ch;
        const rad = Math.max(cw, ch) * 1.15;
        const rg = g.createRadialGradient(cx, cy, 1, cx, cy, rad);
        rg.addColorStop(0, 'rgba(' + R + ',' + G + ',' + B + ',' + (0.18 + r * 0.55).toFixed(2) + ')');
        rg.addColorStop(1, 'rgba(' + R + ',' + G + ',' + B + ',0)');
        g.fillStyle = rg; g.beginPath(); g.arc(cx, cy, rad, 0, Math.PI * 2); g.fill();
      }
    }
    for (const pt of (mh.lmb || [])) {
      const sx = px + (pt[0] + 1) * 0.5 * pw, sy = py + (pt[1] + 1) * 0.5 * ph;
      g.fillStyle = 'rgba(244,63,94,0.88)';
      g.beginPath(); g.arc(sx, sy, 4.2, 0, Math.PI * 2); g.fill();
    }
  } else {
    g.fillStyle = 'rgba(226,232,240,0.42)'; g.font = '500 18px "Microsoft YaHei UI",sans-serif';
    g.textAlign = 'center'; g.fillText('该录制无鼠标数据', px + pw / 2, py + ph / 2 + 6); g.textAlign = 'left';
  }

  // Bottom bar: left = strafe overlap distribution, right = top key peak consistency
  const bx = 60, by = 750, bw = 1800, bh = 276;
  _rrect(g, bx, by, bw, bh, 16);
  g.fillStyle = 'rgba(255,255,255,0.03)'; g.fill();
  g.strokeStyle = 'rgba(255,255,255,0.10)'; g.lineWidth = 1.5; g.stroke();
  g.fillStyle = '#e2e8f0'; g.font = '600 19px "Microsoft YaHei UI",sans-serif';
  g.fillText('急停切换耗时分布（绿=纯净 · 红=重叠超标）', bx + 24, by + 36);
  const evs = (an.strafes && an.strafes.events) ? an.strafes.events.slice(-48) : [];
  if (evs.length) {
    const cx0 = bx + 28, cy0 = by + 56, cw0 = 860, ch0 = 186;
    const maxMs = Math.max(60, ...evs.map(e => e.ms));
    const stepW = cw0 / evs.length, barW = Math.max(4, stepW - 4);
    evs.forEach((e, i) => {
      const bh0 = Math.max(4, (e.ms / maxMs) * (ch0 - 20));
      g.fillStyle = (e.kind === 'overlap' && !e.clean) ? '#f43f5e' : (e.clean ? '#34d399' : '#fbbf24');
      _rrect(g, cx0 + i * stepW, cy0 + ch0 - bh0, barW, bh0, 3); g.fill();
    });
    g.fillStyle = 'rgba(226,232,240,0.48)'; g.font = '400 13px ui-monospace,Consolas,monospace';
    g.fillText('峰值 ' + maxMs.toFixed(0) + ' ms  ·  展示最近 ' + evs.length + ' 次急停切换', cx0, by + 260);
  } else {
    g.fillStyle = 'rgba(226,232,240,0.42)'; g.font = '400 16px "Microsoft YaHei UI",sans-serif';
    g.fillText('未检测到急停键对切换', bx + 28, by + 138);
  }
  const rx2 = bx + 930;
  g.fillStyle = '#e2e8f0'; g.font = '600 19px "Microsoft YaHei UI",sans-serif';
  g.fillText('单键峰值深度与一致性 (mean ± σ)', rx2, by + 36);
  const topK = (an.key_stats || []).slice(0, 6);
  topK.forEach((k, i) => {
    const y = by + 62 + i * 33;
    g.fillStyle = '#f8fafc'; g.font = '600 15px ui-monospace,Consolas,monospace';
    g.fillText(disp(norm(k.key)), rx2, y + 16);
    const barX = rx2 + 78, barMax = 520;
    _rrect(g, barX, y + 4, barMax, 14, 7); g.fillStyle = 'rgba(255,255,255,0.06)'; g.fill();
    const bw1 = Math.max(8, Math.min(1, k.peak_mean / FULL) * barMax);
    _rrect(g, barX, y + 4, bw1, 14, 7); g.fillStyle = '#38bdf8'; g.fill();
    g.fillStyle = 'rgba(226,232,240,0.82)'; g.font = '500 14px ui-monospace,Consolas,monospace';
    g.fillText(k.peak_mean.toFixed(2) + ' ± ' + k.peak_std.toFixed(2) + ' mm  (' + k.presses + '次)', barX + barMax + 14, y + 16);
  });
}

function exportReviewCard() {
  const off = renderReviewCardCanvas();
  if (!off) { alert('请先加载一段录制数据'); return; }
  const g = off.getContext('2d');
  drawReviewCardBody(g, analysis);
  const name = 'fps-review-' + (analysis.stamp || 'card') + '.png';
  const a = document.createElement('a');
  a.href = off.toDataURL('image/png');
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  const logEl = document.getElementById('log');
  if (logEl) logEl.textContent = '已导出复盘统计卡片：' + name;
}

function onWbFullChange(v) {
  setFullMm(v);
  const el = document.getElementById('wbFull');
  if (el) el.value = FULL.toFixed(2);
  draw(true);
  renderPreviewAt(activeTime());
}

function getStageBoxes() {
  const stage = document.getElementById('prevStage');
  const kbL = document.getElementById('kbLayer'), msL = document.getElementById('msLayer');
  if (!vidInfo || !stage || !kbL || !msL) return {};
  const sr = stage.getBoundingClientRect();
  let vx = 0, vy = 0, vw = sr.width, vh = sr.height;
  const aspect = (vidInfo.width && vidInfo.height) ? (vidInfo.width / vidInfo.height) : (16 / 9);
  if (sr.width / Math.max(1, sr.height) > aspect) {
    vw = sr.height * aspect;
    vx = (sr.width - vw) * 0.5;
  } else {
    vh = sr.width / aspect;
    vy = (sr.height - vh) * 0.5;
  }
  const normRect = (el) => {
    const r = el.getBoundingClientRect();
    return [
      clamp((r.left - sr.left - vx) / Math.max(1, vw), 0, 0.96),
      clamp((r.top - sr.top - vy) / Math.max(1, vh), 0, 0.96),
      clamp(r.width / Math.max(1, vw), 0.04, 1.0),
      clamp(r.height / Math.max(1, vh), 0.04, 1.0),
    ];
  };
  return { kb_box: normRect(kbL), mouse_box: normRect(msL) };
}

function openExportsFolder() {
  fetch('/wb/export/open', { method: 'POST', headers: { 'X-Depth-Token': window.__token || '' } }).catch(() => {});
}

function startExport() {
  if (!data || !data.ok) { alert('还没有可导出的录制'); return; }
  if (!(sel[1] > sel[0])) { alert('请先在时间轴上拖出一段区间'); return; }
  const mode = (document.getElementById('expMode') || {}).value || 'hud';
  if ((mode === 'composite' || mode === 'tracks') && !vidInfo) {
    alert('合成成片或三轨导出需要先在上方导入录像');
    return;
  }
  const incMouse = !!(document.getElementById('expMouse') && document.getElementById('expMouse').checked && data.mouse);
  const mLay = (document.getElementById('expMouseLayout') || {}).value || 'side';
  const picked = (info.keys || []).filter(k => selectedKeys.has(k));
  if (!picked.length && !(incMouse && mLay === 'only')) { alert('请至少勾选一个导出键位'); return; }
  const fmt = document.getElementById('fmt').value || 'webm';
  const boxes = vidInfo ? getStageBoxes() : {};
  const params = {
    mode: mode,
    offset: vidInfo ? videoOffset : 0.0,
    stamp: currentStamp || undefined,
    start: sel[0], end: sel[1],
    layout: document.getElementById('layout').value,
    theme: document.getElementById('theme').value,
    full_mm: FULL,
    size: document.getElementById('size').value,
    fps: Number(document.getElementById('fps').value),
    speed: Number(document.getElementById('speed').value),
    bg: document.getElementById('bg').value,
    fmt: fmt,
    png: fmt === 'png',
    mouse: incMouse,
    mouse_layout: mLay,
    mm: 'press',
    kb_box: boxes.kb_box,
    mouse_box: boxes.mouse_box,
    kb_alpha: vidInfo ? stageCfg.kb.alpha : undefined,
    mouse_alpha: vidInfo ? stageCfg.mouse.alpha : undefined,
    mopacity: vidInfo ? stageCfg.mouse.mopacity : undefined,
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

function setVidStatus(msg) {
  const el = document.getElementById('vidStatus');
  if (el) el.textContent = msg || '';
}
function togglePathInput(force) {
  const r = document.getElementById('vidPathRow');
  if (!r) return;
  const on = typeof force === 'boolean' ? force : !r.classList.contains('on');
  r.classList.toggle('on', on);
}
function applyVideoLoaded(res) {
  if (!res || !res.ok || !res.video) { setVidStatus((res && res.error) || '加载失败'); return; }
  vidInfo = res.video;
  videoOffset = Number(res.offset || 0);
  if (res.stage) stageCfg = res.stage;
  const stg = document.getElementById('prevStage');
  if (stg) {
    stg.classList.add('has-video');
    if (vidInfo.width && vidInfo.height) stg.style.setProperty('--vid-ar', (vidInfo.width / vidInfo.height).toFixed(4));
  }
  document.getElementById('vidCtrlBar').classList.add('on');
  const cb = document.getElementById('vidCloseBtn');
  if (cb) cb.style.display = '';
  document.getElementById('vidName').textContent = vidInfo.name + ' (' + vidInfo.start_str + '~' + vidInfo.end_str + ')';
  document.getElementById('vidOffIn').value = videoOffset.toFixed(3);
  const em = document.getElementById('expMode');
  if (em && em.value === 'hud') em.value = 'composite';
  const v = document.getElementById('wbVideo');
  v.src = '/wb/video/stream?v=' + Date.now();
  buildPreviewKb();
  fit();
  const ov = Number(res.overlap_s || 0);
  if (res.matched_stamp) {
    setVidStatus('已自动匹配 ' + res.matched_stamp + (ov > 0 ? (' · 重叠 ' + ov.toFixed(0) + 's') : ' · 无重叠请手动调偏移'));
    if (res.matched_stamp !== currentStamp) {
      onSelectRec(res.matched_stamp);
      return;
    }
  } else {
    setVidStatus('未找到重合录制，请手动选择历史并调整偏移');
  }
  if (data && data.ok) {
    if (!selValidForVideo()) setSel(0, 0);
    playT = clamp(videoOffset, 0, data.duration);
    hoverT = null;
    ensurePlaybackHd(playT, true);
    draw(true);
    renderPreviewAt(playT);
  }
}
function pickVideo() {
  setVidStatus('请在系统窗口中选择录像文件…');
  fetch('/wb/video/pick', { method: 'POST', headers: { 'X-Depth-Token': window.__token || '' } })
    .then(r => r.json()).then(res => {
      if (res && res.cancelled) { setVidStatus(''); return; }
      applyVideoLoaded(res);
    }).catch(() => setVidStatus('选择失败'));
}
function loadVideoByPath() {
  const p = (document.getElementById('vidPathIn').value || '').trim();
  if (!p) return;
  setVidStatus('正在解析录像时间戳…');
  fetch('/wb/video/load', {
    method: 'POST', headers: { 'X-Depth-Token': window.__token || '' },
    body: JSON.stringify({ path: p, stamp: currentStamp || undefined })
  }).then(r => r.json()).then(res => {
    if (res && res.ok) togglePathInput(false);
    applyVideoLoaded(res);
  }).catch(() => setVidStatus('加载失败'));
}
function clearVideo() {
  fetch('/wb/video/clear', { method: 'POST', headers: { 'X-Depth-Token': window.__token || '' } }).catch(() => {});
  vidInfo = null;
  const v = document.getElementById('wbVideo');
  if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
  const stg = document.getElementById('prevStage');
  if (stg) { stg.classList.remove('has-video'); stg.style.removeProperty('--vid-ar'); }
  document.getElementById('vidCtrlBar').classList.remove('on');
  const cb = document.getElementById('vidCloseBtn');
  if (cb) cb.style.display = 'none';
  const vn = document.getElementById('vidName');
  if (vn) vn.textContent = '未导入（纯 HUD 预览）';
  const em = document.getElementById('expMode');
  if (em) em.value = 'hud';
  setVidStatus('');
  buildPreviewKb();
  fit();
}

function setVideoOffset(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return;
  videoOffset = Math.round(n * 1000) / 1000;
  document.getElementById('vidOffIn').value = videoOffset.toFixed(3);
  const vid = document.getElementById('wbVideo');
  if (vidInfo && vid && data && data.ok) {
    playT = clamp((vid.currentTime || 0) + videoOffset, 0, data.duration || 1e9);
    hoverT = null;
    ensurePlaybackHd(playT, true);
    draw(false);
    renderPreviewAt(playT);
  }
}
function nudgeVideoOffset(d) { setVideoOffset(videoOffset + d); }
function fineAlignVideo() {
  if (!vidInfo) return;
  const vid = document.getElementById('wbVideo');
  setVidStatus('正在分析枪声音频与左键脉冲…');
  fetch('/wb/video/align', {
    method: 'POST', headers: { 'X-Depth-Token': window.__token || '' },
    body: JSON.stringify({ stamp: currentStamp || undefined, video_t: vid ? (vid.currentTime || 0) : 0, offset: videoOffset, search_s: 3.0 })
  }).then(r => r.json()).then(res => {
    if (res && res.ok) {
      setVideoOffset(res.offset);
      const ms = Math.round((res.delta || 0) * 1000);
      setVidStatus('精对齐成功 (' + (ms >= 0 ? '+' : '') + ms + 'ms · ' + res.clicks + ' 枪 · z=' + res.confidence + ')');
    } else {
      setVidStatus((res && (res.message || res.error)) || '精对齐未命中');
    }
  }).catch(() => setVidStatus('精对齐请求失败'));
}
function markSelFromVideo(which) {
  if (!vidInfo || !data || !data.ok) return;
  const vid = document.getElementById('wbVideo');
  const rt = clamp((vid.currentTime || 0) + videoOffset, 0, data.duration || 0);
  if (which === 0) setSel(rt, Math.max(rt, sel[1]));
  else setSel(Math.min(sel[0], rt), rt);
}
function bindVideoEvents() {
  const v = document.getElementById('wbVideo');
  if (!v) return;
  v.addEventListener('play', () => {
    if (!vidInfo || !data || !data.ok) return;
    if (!playing) loopSelection = false;
    setPlayUI(true);
    ensurePlaybackHd(clamp((v.currentTime || 0) + videoOffset, 0, data.duration), true);
    playLastMs = performance.now();
    requestAnimationFrame(playLoop);
  });
  v.addEventListener('pause', () => { if (vidInfo && playing) { loopSelection = false; setPlayUI(false); } });
  const onSeek = () => {
    if (!vidInfo || syncingVid || !data || !data.ok) return;
    playT = clamp((v.currentTime || 0) + videoOffset, 0, data.duration || 1e9);
    hoverT = null;
    ensurePlaybackHd(playT, true);
    draw(false);
    renderPreviewAt(playT);
  };
  v.addEventListener('timeupdate', () => { if (vidInfo && !playing && !syncingVid) onSeek(); });
  v.addEventListener('loadedmetadata', () => {
    const stg = document.getElementById('prevStage');
    if (stg && v.videoWidth && v.videoHeight) {
      stg.style.setProperty('--vid-ar', (v.videoWidth / v.videoHeight).toFixed(4));
      applyStageLayout();
    }
  });
  v.addEventListener('seeked', () => {
    if (syncingVid) {
      syncingVid = false;
      clearTimeout(syncVidTimer);
      return;
    }
    onSeek();
  });
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
    draw(false);
    ensurePlaybackHd(playT, false);
    syncVideoToPlayT();
    renderPreviewAt(playT);
  }
});
addEventListener('resize', () => { fit(); buildPreviewKb(); applyStageLayout(); });
if (window.ResizeObserver) {
  const _stgObs = new ResizeObserver(() => { if (vidInfo) applyStageLayout(); });
  const _stgEl = document.getElementById('prevStage');
  if (_stgEl) _stgObs.observe(_stgEl);
}
initHudDrag();
bindVideoEvents();
buildPreviewKb();
loadList(currentStamp);
fetch('/wb/video/info').then(r => r.json()).then(j => {
  if (j && j.stage) stageCfg = j.stage;
}).catch(() => {});
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
    ap.add_argument("--no-wake", action="store_true",
                    help="do not send automatic setKeyTravelNotify wake report")
    ap.add_argument("--idle-exit", type=int, default=90,
                    help="exit after N seconds with no page open (0 = never)")
    args = ap.parse_args()
    if args.no_wake:
        os.environ["DEPTH_MONITOR_NO_WAKE"] = "1"
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
    finally:
        ENGINE.shutdown_app(srv)


if __name__ == "__main__":
    main()
