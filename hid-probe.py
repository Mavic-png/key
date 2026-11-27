# -*- coding: utf-8 -*-
"""hid-probe.py -- probe a vendor HID device (default: ASUS ROG Falchion Ace 75 HE,
VID 0B05 PID 1C7D) for analog key-depth telemetry, without USBPcap.

Modes:
  python hid-probe.py descriptors          dump every HID collection + parsed report descriptor
  python hid-probe.py listen               log input reports while you press keys, then analyze
  python hid-probe.py listen --index 3     only that collection (index printed by descriptors)
  python hid-probe.py feature --index 3    poll feature reports (read-only, no writes)

Analyze goal of `listen`: does any report byte keep changing *while* a key is held?
If yes -> the keyboard streams key state and depth may be recoverable.
If no  -> depth never leaves the keyboard; need DKS/threshold probing or external sensor.
"""

import argparse
import collections
import datetime
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
IOCTL_HID_GET_REPORT_DESCRIPTOR = 0x000B0190

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
LLKHF_INJECTED = 0x10
WINM_QUIT = 0x0012


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


GUID_DEVINTERFACE_HID = GUID(0x4D1E55B2, 0xF16F, 0x11CF,
                             (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
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
                                                ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.DeviceIoControl.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                     ctypes.c_void_p, wt.DWORD,
                                     ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CancelIoEx.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL

hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidD_GetProductString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
hid.HidD_GetManufacturerString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
hid.HidD_GetSerialNumberString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]


# ---------------------------------------------------------------- enumeration

def hid_interface_paths():
    paths = []
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(GUID_DEVINTERFACE_HID), None, None,
                                        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        return paths
    try:
        idx = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None,
                                                       ctypes.byref(GUID_DEVINTERFACE_HID),
                                                       idx, ctypes.byref(iface)):
                break
            idx += 1
            needed = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), None, 0,
                                                      ctypes.byref(needed), None)
            if needed.value <= 0:
                continue
            size = max(needed.value, ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) + 2)
            buf = ctypes.create_string_buffer(size)
            struct = ctypes.cast(buf, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W))
            struct.contents.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), buf,
                                                          size, ctypes.byref(needed), None)
            if not ok:
                continue
            off = SP_DEVICE_INTERFACE_DETAIL_DATA_W.DevicePath.offset
            paths.append(ctypes.wstring_at(ctypes.addressof(buf) + off))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return paths


def wide_string(path, fn):
    buf = ctypes.create_unicode_buffer(256)
    if fn(path, buf, ctypes.sizeof(buf)):
        return buf.value
    return ""


def open_hid(path, write=False):
    """HID collections reject IOCTLs on a zero-access handle, so try widening."""
    base = [GENERIC_READ | (GENERIC_WRITE if write else 0), GENERIC_READ,
            GENERIC_READ | GENERIC_WRITE, 0]
    attempts = []
    for a in base:
        if a not in attempts:
            attempts.append(a)
    for access in attempts:
        h = kernel32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if h != INVALID_HANDLE_VALUE:
            return h
    return INVALID_HANDLE_VALUE


def describe(path):
    """Open read access to query attributes/caps/report descriptor."""
    h = open_hid(path)
    if h == INVALID_HANDLE_VALUE:
        return None
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if not hid.HidD_GetAttributes(h, ctypes.byref(attrs)):
            return None
        info = {"path": path, "vid": attrs.VendorID, "pid": attrs.ProductID,
                "version": attrs.VersionNumber,
                "product": wide_string(h, hid.HidD_GetProductString),
                "manufacturer": wide_string(h, hid.HidD_GetManufacturerString),
                "serial": wide_string(h, hid.HidD_GetSerialNumberString)}
        preparsed = ctypes.c_void_p()
        caps = HIDP_CAPS()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(preparsed)):
            try:
                if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)):
                    info.update({
                        "usage_page": caps.UsagePage, "usage": caps.Usage,
                        "in_len": caps.InputReportByteLength,
                        "out_len": caps.OutputReportByteLength,
                        "feat_len": caps.FeatureReportByteLength,
                        "in_value_caps": caps.NumberInputValueCaps,
                        "in_button_caps": caps.NumberInputButtonCaps,
                        "feat_value_caps": caps.NumberFeatureValueCaps,
                        "out_value_caps": caps.NumberOutputValueCaps,
                    })
            finally:
                hid.HidD_FreePreparsedData(preparsed)
        desc = ctypes.create_string_buffer(8192)
        got = wt.DWORD(0)
        if kernel32.DeviceIoControl(h, IOCTL_HID_GET_REPORT_DESCRIPTOR, None, 0, desc, 8192,
                                    ctypes.byref(got), None):
            info["descriptor"] = bytes(desc.raw[:got.value])
        else:
            info["descriptor"] = b""
        return info
    finally:
        self.api.CloseHandle(h)


# ------------------------------------------------------- report descriptor parse

USAGE_PAGES = {
    0x01: "Generic Desktop", 0x02: "Simulation", 0x05: "Game Pad", 0x06: "Keyboard",
    0x07: "LED", 0x08: "Button", 0x09: "Consumer", 0x0B: "Telephony", 0x0C: "Consumer",
    0x0D: "Digitizer", 0x0F: "PID", 0x12: "Haptics", 0x81: "Monitor", 0x82: "Monitor Enum",
    0x83: "VESA", 0x84: "Power", 0xFF00: "Vendor 0xFF00", 0xFF01: "Vendor 0xFF01",
    0xFF0F: "Vendor 0xFF0F", 0xFFA0: "Vendor 0xFFA0",
}


def _signed(value, size):
    if size == 1 and value >= 0x80:
        return value - 0x100
    if size == 2 and value >= 0x8000:
        return value - 0x10000
    return value


def hx(data):
    return " ".join("%02X" % b for b in data)


def parse_descriptor(data):
    """Return list of rows describing the descriptor structure."""
    rows = []
    g = {"usage_page": None, "logical_min": 0, "logical_max": 0, "report_id": 0,
         "report_size": 0, "report_count": 0, "unit": 0, "unit_exp": 0,
         "phys_min": 0, "phys_max": 0}
    stack = []
    local_usages = []
    coll = []  # stack of collection types
    i = 0
    n = len(data)
    while i < n:
        prefix = data[i]
        i += 1
        size = prefix & 0x03
        if size == 3:
            size = 4
        kind = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        raw = 0
        for k in range(size):
            if i + k < n:
                raw |= data[i + k] << (8 * k)
        i += size
        if kind == 0:  # main
            if tag in (0x8, 0x9, 0xB):  # Input / Output / Feature
                name = {0x8: "Input", 0x9: "Output", 0xB: "Feature"}[tag]
                usages = local_usages
                rows.append({
                    "kind": name, "depth": len(coll), "parent": coll[-1] if coll else None,
                    "usage_page": g["usage_page"], "usages": usages,
                    "size": g["report_size"], "count": g["report_count"],
                    "logical": (g["logical_min"], g["logical_max"]),
                    "report_id": g["report_id"], "flags": raw,
                })
                local_usages = []
            elif tag == 0xA:  # Collection
                ctype = raw
                coll.append(ctype)
                rows.append({"kind": "Collection", "depth": len(coll) - 1, "type": ctype,
                             "usages": local_usages, "usage_page": g["usage_page"]})
                local_usages = []
            elif tag == 0xC:  # EndCollection
                if coll:
                    coll.pop()
                rows.append({"kind": "EndCollection", "depth": len(coll)})
                local_usages = []
        elif kind == 1:  # global
            if tag == 0x0:
                g["usage_page"] = raw
            elif tag == 0x1:
                g["logical_min"] = _signed(raw, size) if size else 0
            elif tag == 0x2:
                g["logical_max"] = _signed(raw, size) if size else 0
            elif tag == 0x3:
                g["phys_min"] = _signed(raw, size) if size else 0
            elif tag == 0x4:
                g["phys_max"] = _signed(raw, size) if size else 0
            elif tag == 0x5:
                g["unit_exp"] = _signed(raw, size) if size else 0
            elif tag == 0x6:
                g["unit"] = raw
            elif tag == 0x7:
                g["report_size"] = raw
            elif tag == 0x8:
                g["report_id"] = raw
            elif tag == 0x9:
                g["report_count"] = raw
            elif tag == 0xA:
                stack.append(dict(g))
            elif tag == 0xB:
                if stack:
                    g = stack.pop()
        else:  # local
            if tag == 0x0:
                if size == 4:
                    local_usages.append((raw >> 16, raw & 0xFFFF))
                else:
                    local_usages.append((g["usage_page"], raw))
            elif tag == 0x1:
                local_usages.append(("min", raw))
            elif tag == 0x2:
                local_usages.append(("max", raw))
    return rows


def fmt_usage(pair, page):
    if isinstance(pair, tuple) and len(pair) == 2 and pair[0] is not None:
        pg, us = pair
    else:
        pg, us = page, pair
    if pg is None:
        return "0x%04X" % us
    name = USAGE_PAGES.get(pg, "page 0x%04X" % pg)
    return "%s 0x%02X" % (name, us) if us <= 0xFF else "%s 0x%04X" % (name, us)


def print_descriptor(info):
    data = info.get("descriptor") or b""
    print("    report descriptor: %d bytes" % len(data))
    if data:
        print("      " + " ".join("%02X" % b for b in data))
    rows = parse_descriptor(data)
    last_id = None
    for r in rows:
        pad = "      " + "  " * r["depth"]
        if r["kind"] == "Collection":
            us = ", ".join(fmt_usage(u, r["usage_page"]) for u in r["usages"]
                           if not (isinstance(u, tuple) and u[0] in ("min", "max")))
            print("%sCollection type=0x%02X usages=[%s]" % (pad, r["type"], us))
        elif r["kind"] == "EndCollection":
            print("%sEndCollection" % pad)
        else:
            if r["report_id"] != last_id:
                print("%s-- report id %d --" % (pad, r["report_id"]))
                last_id = r["report_id"]
            usages = [u for u in r["usages"]
                      if not (isinstance(u, tuple) and u[0] in ("min", "max"))]
            shown = ", ".join(fmt_usage(u, r["usage_page"]) for u in usages[:6])
            more = "" if len(usages) <= 6 else ", ... (%d total)" % len(usages)
            print("%s%s: %d x %d bit  logical %s..%s  usages=[%s%s]" % (
                pad, r["kind"], r["count"], r["size"],
                r["logical"][0], r["logical"][1], shown, more))


def cmd_descriptors(args):
    paths = hid_interface_paths()
    wanted = [p for p in paths if ("vid_%04x" % args.vid) in p.lower()
              and ("pid_%04x" % args.pid) in p.lower()]
    print("HID interfaces total=%d, matching VID %04X PID %04X: %d\n"
          % (len(paths), args.vid, args.pid, len(wanted)))
    index = 0
    for path in wanted:
        info = describe(path)
        if not info:
            print("[%d] %s\n    <open failed>\n" % (index, path))
            index += 1
            continue
        tail = path.split("#")
        mi = [t for t in tail if t.lower().startswith("mi_")]
        col = [t for t in tail if t.lower().startswith("col")]
        print("[%d] %s%s" % (index, mi[0] if mi else "?", " " + col[0] if col else ""))
        print("    path: %s" % path)
        print("    vid=%04X pid=%04X version=%04X product=%r mfg=%r serial=%r"
              % (info["vid"], info["pid"], info["version"], info["product"],
                 info["manufacturer"], info["serial"]))
        print("    usage page=0x%04X usage=0x%04X (%s)"
              % (info.get("usage_page", 0), info.get("usage", 0),
                 USAGE_PAGES.get(info.get("usage_page"), "?")))
        print("    IN=%s OUT=%s FEATURE=%s bytes | value caps in=%s feat=%s out=%s"
              % (info.get("in_len"), info.get("out_len"), info.get("feat_len"),
                 info.get("in_value_caps"), info.get("feat_value_caps"),
                 info.get("out_value_caps")))
        print_descriptor(info)
        print("")
        index += 1
    print("Next: python hid-probe.py listen            (press keys for 20s)")
    print("      python hid-probe.py listen --index N  (one collection)")


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


def _hook_proc(n_code, w_param, l_param):
    if n_code == HC_ACTION:
        try:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not (info.flags & LLKHF_INJECTED):
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    key_events.append((time.perf_counter(), info.vkCode, 1))
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    key_events.append((time.perf_counter(), info.vkCode, 0))
        except Exception:
            pass
    return user32.CallNextHookEx(None, n_code, w_param, l_param)


# -------------------------------------------------------------------- listen

class Reader(threading.Thread):
    """Reads one HID collection. Owns its handle: it opens and closes it itself so
    the main thread can never close a handle that is still blocked in ReadFile."""

    def __init__(self, index, path, length, sink, api=None):
        threading.Thread.__init__(self)
        self.daemon = True
        self.index = index
        self.path = path
        self.length = max(length, 64)
        self.sink = sink
        self.api = api or kernel32          # injectable for fault injection
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.handle = None
        self.error = None
        self.reads = 0

    def _last_error(self):
        fn = getattr(self.api, "get_last_error", None)
        return fn() if fn else ctypes.get_last_error()

    def stop(self, wait=1.0):
        """CancelIoEx only cancels I/O that is already pending, and the reader may
        not have issued its ReadFile yet, so keep cancelling until it stops."""
        self.stop_event.set()
        deadline = time.time() + wait
        while True:
            with self.lock:
                h = self.handle
            if h:
                try:
                    self.api.CancelIoEx(h, None)
                except Exception:
                    pass
            if not self.is_alive() or time.time() >= deadline:
                return not self.is_alive()
            time.sleep(0.01)

    def run(self):
        h = self.api.CreateFileW(self.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE:
            self.error = "CreateFile failed err=%d" % self._last_error()
            return
        with self.lock:
            self.handle = h
        if self.stop_event.is_set():          # stop() won the race: abort now
            with self.lock:
                self.handle = None
            try:
                self.api.CloseHandle(h)
            except Exception:
                pass
            return
        buf = (ctypes.c_ubyte * self.length)()
        nread = wt.DWORD(0)
        consecutive = 0
        try:
            while not self.stop_event.is_set():
                if not self.api.ReadFile(h, buf, self.length, ctypes.byref(nread), None):
                    err = self._last_error()
                    if err in (995, 6):
                        return                      # cancelled / invalid handle
                    if err in (1167, 109):
                        self.error = "device gone err=%d" % err
                        return
                    consecutive += 1
                    if consecutive >= 50:           # ~1 s of failures: give up
                        self.error = "read kept failing err=%d" % err
                        return
                    time.sleep(0.02)
                    continue
                consecutive = 0
                n = nread.value
                if n <= 0 or n > self.length:
                    continue
                self.reads += 1
                self.sink.append((time.perf_counter(), self.index, bytes(buf[:n])))
        finally:
            with self.lock:
                self.handle = None
            try:
                self.api.CloseHandle(h)
            except Exception:
                pass


_capture_incomplete = False


def cmd_listen(args):
    paths = hid_interface_paths()
    wanted = [p for p in paths if ("vid_%04x" % args.vid) in p.lower()
              and ("pid_%04x" % args.pid) in p.lower()]
    infos = []
    for p in wanted:
        info = describe(p)
        if info:
            infos.append(info)
    if not infos:
        print("no matching HID collections")
        return 1

    print("%-4s %-26s %-6s %-4s %-4s %-5s %s"
          % ("idx", "mi/col", "usage", "IN", "OUT", "FEAT", "value caps(in)"))
    for i, info in enumerate(infos):
        tail = info["path"].split("#")
        mi = [t for t in tail if t.lower().startswith("mi_")]
        col = [t for t in tail if t.lower().startswith("col")]
        tag = (mi[0] if mi else "?") + (" " + col[0] if col else "")
        print("%-4d %-26s 0x%04X %-4s %-4s %-5s %s"
              % (i, tag, info.get("usage_page", 0), info.get("in_len"),
                 info.get("out_len"), info.get("feat_len"), info.get("in_value_caps")))

    if args.index:
        chosen = [i for i in args.index if 0 <= i < len(infos)]
    else:
        chosen = [i for i, info in enumerate(infos)
                  if (info.get("in_len") or 0) > 1 and info.get("usage_page", 0) != 0x0001]
    if not chosen:
        print("\nno collection with input reports to read (only the standard keyboard set).")
        return 2
    print("\nreading collections: %s" % chosen)

    events = []
    readers = []
    for i in chosen:
        info = infos[i]
        length = info.get("in_len") or 64
        r = Reader(i, info["path"], length, events)
        readers.append(r)
        r.start()

    # low level hook lives in this thread
    hook_ref = HOOKPROC(_hook_proc)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_ref, None, 0)
    if not hook:
        print("warning: keyboard hook failed err=%d (key correlation disabled)"
              % ctypes.get_last_error())

    print("\n--- press keys now: hold A and slowly press deeper, wiggle, release slowly ---")
    print("--- recording %.0f s (ctrl+c to stop early) ---" % args.seconds)
    deadline = time.perf_counter() + args.seconds
    msg = wt.MSG()
    try:
        while time.perf_counter() < deadline:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.001)
            if readers and not any(r.is_alive() for r in readers):
                print("all readers stopped: %s"
                      % "; ".join(r.error for r in readers if r.error))
                break
    except KeyboardInterrupt:
        pass
    finally:
        for r in readers:
            r.stop()                     # cancel the blocked ReadFile, keep the handle alive
        for r in readers:
            r.join(2.0)                  # the reader closes its own handle in its finally
        still = [r.index for r in readers if r.is_alive()]
        if still:
            print("warning: readers %s did not stop in time -- output skipped,"
                  " the capture would be incomplete" % still)
            global _capture_incomplete
            _capture_incomplete = True
        if hook:
            user32.UnhookWindowsHookEx(hook)

    if globals().get("_capture_incomplete"):
        print("capture incomplete: csv/analysis skipped (readers still running)")
        return 3

    for r in readers:
        if r.error:
            print("collection %d: %s" % (r.index, r.error))

    analyze(events, key_events, infos, chosen)
    write_csvs(events, key_events)
    return 0


def analyze(reports, keys, infos, chosen):
    print("\n=== capture ===")
    print("reports=%d key events=%d" % (len(reports), len(keys)))
    if reports:
        span = reports[-1][0] - reports[0][0]
        print("report rate: %.1f/s over %.2fs" % (len(reports) / span if span else 0, span))
    if not reports:
        print("No input reports arrived at all -> the vendor collection is write/feature-only.")
        print("Depth cannot be read passively from this interface.")
        return

    # held intervals
    down = {}
    intervals = []
    for t, vk, d in keys:
        if d:
            down.setdefault(vk, t)
        else:
            t0 = down.pop(vk, None)
            if t0 is not None:
                intervals.append((t0, t))
    intervals.sort()

    def held(t):
        for a, b in intervals:
            if a <= t <= b:
                return True
            if a > t:
                break
        return False

    by_idx = collections.defaultdict(list)
    for t, idx, rep in reports:
        by_idx[idx].append((t, rep))

    print("\n=== per-collection analysis ===")
    for idx in sorted(by_idx):
        reps = by_idx[idx]
        print("\ncollection %d: %d reports, IN byte length %d"
              % (idx, len(reps), infos[idx].get("in_len")))
        patterns = collections.Counter(r for _, r in reps)
        print("  distinct report contents: %d" % len(patterns))
        for rep, cnt in patterns.most_common(5):
            print("    %3d x %s" % (cnt, hx(rep)))

        width = max(len(r) for _, r in reps)
        stats = []
        for off in range(width):
            last = None
            changes = []
            for t, rep in reps:
                if off < len(rep):
                    v = rep[off]
                    if last is not None and v != last:
                        changes.append((t, last, v))
                    last = v
            inside = [c for c in changes if held(c[0])]
            stats.append((off, len(changes), len(inside),
                          sorted(set(c[2] for c in changes))))
        interesting = [s for s in stats if s[2] > 0]
        if interesting:
            print("  offsets changing WHILE a key is held (this is the depth signal):")
            print("    off  changes  in-hold  values")
            for off, total, ins, vals in sorted(interesting, key=lambda s: -s[2])[:12]:
                vtxt = ",".join(str(v) for v in vals[:16])
                print("    %3d  %7d  %7d  %s" % (off, total, ins, vtxt))
        else:
            print("  no byte changes while keys were held -> no live telemetry in this read "
                  "direction")

    # tail of reports during holds, to eyeball
    during = [r for r in reports if held(r[0])]
    print("\nreports received while a key was held: %d" % len(during))
    for t, idx, rep in during[:20]:
        print("  %.6f  col%d  %s" % (t, idx, hx(rep)))


def wall_iso(t_perf):
    delta = time.perf_counter() - t_perf
    return datetime.datetime.fromtimestamp(time.time() - delta).astimezone().isoformat(
        timespec="milliseconds")


def write_csvs(reports, keys):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.dirname(os.path.abspath(__file__))
    rep_path = os.path.join(base, "hid-reports-%s.csv" % stamp)
    key_path = os.path.join(base, "hid-keys-%s.csv" % stamp)
    t0 = reports[0][0] if reports else time.perf_counter()
    with open(rep_path, "w", newline="") as f:
        f.write("# tool=hid-probe.py  t = monotonic seconds since started,"
                " wall = local time\n")
        f.write("# started=%s\n" % wall_iso(t0))
        f.write("t,wall,index,hex\n")
        for t, idx, rep in reports:
            f.write("%.6f,%s,%d,%s\n" % (t - t0, wall_iso(t), idx, rep.hex()))
    with open(key_path, "w", newline="") as f:
        f.write("# tool=hid-probe.py  OS keyboard events, same timebase as hid-reports\n")
        f.write("# started=%s\n" % wall_iso(t0))
        f.write("t,wall,vk,down\n")
        for t, vk, d in keys:
            f.write("%.6f,%s,0x%02X,%d\n" % (t - t0, wall_iso(t), vk, d))
    print("\nwrote %s" % os.path.basename(rep_path))
    print("wrote %s" % os.path.basename(key_path))


# -------------------------------------------------------------------- feature

def cmd_feature(args):
    paths = hid_interface_paths()
    wanted = [p for p in paths if ("vid_%04x" % args.vid) in p.lower()
              and ("pid_%04x" % args.pid) in p.lower()]
    infos = []
    for p in wanted:
        info = describe(p)
        if info:
            infos.append(info)
    if not infos or not args.index:
        print("need a valid --index (see descriptors)")
        return 1
    idx = args.index[0] if isinstance(args.index, list) else args.index
    if idx >= len(infos):
        print("index out of range")
        return 1
    info = infos[idx]
    length = info.get("feat_len") or 65
    print("feature poll: %s feat_len=%d ids=%s"
          % (info["path"].split("#")[-1], length, args.ids))
    h = kernel32.CreateFileW(info["path"], GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        print("CreateFile failed err=%d" % ctypes.get_last_error())
        return 2
    ids = [int(x, 0) for x in args.ids.split(",")]
    last = {}
    deadline = time.perf_counter() + args.seconds
    try:
        while time.perf_counter() < deadline:
            for rid in ids:
                buf = (ctypes.c_ubyte * max(length, 65))()
                buf[0] = rid
                if hid.HidD_GetFeature(h, buf, max(length, 65)):
                    data = bytes(buf)
                    if last.get(rid) != data:
                        print("%.3f id=%d %s" % (time.perf_counter(), rid, hx(data)))
                        last[rid] = data
                else:
                    err = self._last_error()
                    if last.get(rid) != "err":
                        print("id=%d unsupported/err=%d" % (rid, err))
                        last[rid] = "err"
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        self.api.CloseHandle(h)
    return 0


def main():
    p = argparse.ArgumentParser(description="probe vendor HID for analog key depth")
    p.add_argument("mode", choices=["descriptors", "listen", "feature"])
    p.add_argument("--vid", default="0B05")
    p.add_argument("--pid", default="1C7D")
    p.add_argument("--index", type=int, nargs="*", default=None)
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--ids", default="0,1,2,3")
    args = p.parse_args()
    args.vid = int(str(args.vid), 16)
    args.pid = int(str(args.pid), 16)

    try:
        kernel32.timeBeginPeriod(1)
    except Exception:
        pass
    if args.mode == "descriptors":
        cmd_descriptors(args)
    elif args.mode == "listen":
        return cmd_listen(args)
    else:
        return cmd_feature(args)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
