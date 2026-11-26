# -*- coding: utf-8 -*-
"""test_pipeline.py -- headless regression tests for the whole depth pipeline.

No keyboard and no browser needed: synthetic samples go in, exported files and
rendered frames come out.  Run with the venv python (needs Pillow for the frame
test):

    F:\\int\\.build\\venv\\Scripts\\python.exe test_pipeline.py
"""

import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print("  ok   %s" % name)
    except Exception as exc:
        FAIL.append((name, exc))
        print("  FAIL %s -> %r" % (name, exc))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dl = load("dl", os.path.join(HERE, "depth-live.py"))


def make_engine(mapped=5, unmapped=3, samples=120):
    """Engine with a realistic mix: mapped keys, unmapped keys, keys never sampled."""
    e = dl.Engine()
    base = 0x41
    for i in range(mapped):
        e.mapping[31 + i] = base + i          # A, B, C ...
    for i in range(unmapped):
        e.keys[200 + i] = {"vk": None, "mm": 0.0, "count": 0, "speeds": []}
    now = time.perf_counter()
    for k in range(samples):
        idx = 31 + (k % mapped)
        mm = round((k % 12) * 0.3, 2)
        e.on_depth(now + k * 0.01, idx, mm)
        if k % 7 == 0:
            e.on_depth(now + k * 0.01, 200 + (k % unmapped), round((k % 5) * 0.5, 2))
    return e


# ---------------------------------------------------------------- engine / csv

def t_recording_toggle():
    e = make_engine()
    assert len(e.rec) == 0, "should not record by default"
    e.start_recording()
    now = time.perf_counter()
    for k in range(10):
        e.on_depth(now + k * 0.01, 31, 1.0)
    assert len(e.rec) == 10, "recorded %d" % len(e.rec)
    e.stop_recording(save=False)
    assert not e.recording


def t_wide_export_mixed():
    e = make_engine()
    e.recording = True
    now = time.perf_counter()
    for k in range(60):
        e.on_depth(now + k * 0.01, 31, (k % 6) * 0.5)
        if k % 5 == 0:
            e.on_depth(now + k * 0.01, 202, 1.5)     # unmapped index
    chunks = list(e.wide_lines())
    text = "".join(chunks)
    assert text.startswith("# tool=depth-live"), text[:40]
    rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].startswith("#")]
    assert rows[0][0] == "t_s", rows[0]
    assert len(rows) > 10, "too few rows: %d" % len(rows)
    for r in rows[1:]:
        float(r[0])                              # timestamps parse


def t_wide_export_keys_without_samples():
    """The 01:37 crash: live keys with zero samples must not break the export."""
    e = dl.Engine()
    for idx in (120, 86, 25, 46):
        e.keys[idx] = {"vk": None, "mm": 0.0, "count": 0, "speeds": []}
    e.recording = True
    now = time.perf_counter()
    for k in range(30):
        e.on_depth(now + k * 0.01, 31, 2.0)
    text = "".join(e.wide_lines())
    rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].startswith("#")]
    assert len(rows) > 5
    assert len(rows[0]) == 3, rows[0]            # t_s, wall, only recorded key


def t_hold_without_mapping():
    """Hold writer must use the learned key table, not the (possibly empty) mapping."""
    e = dl.Engine()
    e.mapping.clear()
    e.keys[18] = {"vk": 0x57, "mm": 3.5, "count": 0, "speeds": []}
    e.keys[31] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
    e.recording = True
    import threading, time as _t
    th = threading.Thread(target=e.run_hold)
    th.daemon = True
    th.start()
    e.vk_held[0x57] = True
    _t.sleep(0.25)
    e.vk_held[0x41] = True
    e.keys[31]["mm"] = 3.0
    _t.sleep(0.25)
    e.recording = False
    assert e.hold_n > 0, "hold writer emitted nothing"
    text = "".join(e.wide_lines())
    import csv as _csv
    rows = [r for r in _csv.reader(io.StringIO(text)) if r and not r[0].startswith("#")]
    hdr = rows[0]
    w, a = hdr.index("W"), hdr.index("A")
    multi = sum(1 for r in rows[1:] if float(r[w]) > 0.05 and float(r[a]) > 0.05)
    assert multi > 0, "held key not preserved: %d" % multi


def t_long_export():
    e = make_engine()
    e.recording = True
    now = time.perf_counter()
    for k in range(40):
        e.on_depth(now + k * 0.01, 31, (k % 4) * 0.8)
    text = "".join(e.long_lines())
    rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].startswith("#")]
    assert rows[0][:2] == ["t_s", "wall"], rows[0]
    assert len(rows) == 41, len(rows)


def t_empty_exports():
    e = dl.Engine()
    wide = "".join(e.wide_lines())
    long = "".join(e.long_lines())
    assert "nothing recorded" in wide or "t_s" in wide
    assert "t_s" in long


def t_save_recording():
    e = make_engine()
    e.recording = True
    now = time.perf_counter()
    for k in range(50):
        e.on_depth(now + k * 0.01, 31, (k % 5) * 0.7)
    e.recording = False
    path = e.save_recording()
    assert path and os.path.exists(path), path
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    rows = [r for r in rows if r and not r[0].startswith("#")]
    assert len(rows) > 5
    assert e.notice, "notice not set"
    os.remove(path)
    long_path = path.replace("-wide", "-long")
    if os.path.exists(long_path):
        os.remove(long_path)


# ---------------------------------------------------------------- http layer

def start_server():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dl.Handler)
    t = threading.Thread(target=srv.serve_forever)
    t.daemon = True
    t.start()
    return srv, srv.server_address[1]


def fetch(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=15) as r:
        return r.status, r.read()


def t_http_endpoints():
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        status, body = fetch(port, "/")
        assert status == 200 and b"<html" in body.lower()
        status, body = fetch(port, "/snapshot")
        snap = json.loads(body.decode("utf-8"))
        assert "rec_n" in snap and "rate" in snap
        status, body = fetch(port, "/rec/start")
        e = dl.ENGINE
        now = time.perf_counter()
        for k in range(60):
            e.on_depth(now + k * 0.01, 31, (k % 6) * 0.5)
        status, body = fetch(port, "/rec/stop")
        assert status == 200
        for ep in ("/export/wide", "/export/long"):
            status, body = fetch(port, ep)
            assert status == 200, (ep, status)
            text = body.decode("utf-8-sig")
            assert "t_s" in text, "%s has no header: %r" % (ep, text[:60])
            assert text.count("\ufeff") <= 1, "%s has repeated BOM" % ep
            assert len(text.splitlines()) > 3, "%s too short" % ep
        status, body = fetch(port, "/bind?key=W")
        assert status == 200
        status, body = fetch(port, "/resetmap")
        assert status == 200
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- render layer

def t_frames_from_wide():
    try:
        kf = load("kf", os.path.join(HERE, "kb-frames.py"))
    except Exception as exc:
        raise AssertionError("kb-frames import failed: %r" % (exc,))
    e = make_engine()
    e.recording = True
    now = time.perf_counter()
    for k in range(80):
        e.on_depth(now + k * 0.01, 31, (k % 9) * 0.4)
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "wide.csv")
    with io.open(path, "w", encoding="utf-8") as f:
        for chunk in e.wide_lines():
            f.write(chunk)
    series, dur = kf.load_csv(path)
    assert series and dur > 0, (series, dur)
    kf_slots = [s for s in kf.LAYOUTS["wasd"] if s[0] in ("A", "B", "C", "D", "W")]
    assert kf_slots


def main():
    print("depth pipeline tests")
    for name, fn in [
        ("recording toggle", t_recording_toggle),
        ("wide export (mixed keys)", t_wide_export_mixed),
        ("wide export (keys without samples)", t_wide_export_keys_without_samples),
        ("long export", t_long_export),
        ("hold writer without mapping", t_hold_without_mapping),
        ("empty exports", t_empty_exports),
        ("save recording to disk", t_save_recording),
        ("http endpoints", t_http_endpoints),
        ("frames loader (wide csv)", t_frames_from_wide),
    ]:
        check(name, fn)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, exc in FAIL:
            print("  - %s: %r" % (name, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
