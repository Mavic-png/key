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


# tests must never read or write the real mapping file
_TEST_HOME = tempfile.mkdtemp(prefix="dl-test-")
os.environ["DEPTH_MONITOR_HOME"] = _TEST_HOME

dl = load("dl", os.path.join(HERE, "depth-live.py"))

assert os.path.abspath(dl.ENGINE.map_path).startswith(os.path.abspath(_TEST_HOME)), (
    "tests would touch the real mapping file: %s" % dl.ENGINE.map_path)


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


def t_firmware_zero_while_held():
    """A zero report while the OS still sees the key down is a firmware switch."""
    e = dl.Engine()
    e.keys[31] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
    e.vk_held[0x41] = True
    e.on_depth(1.0, 31, 2.5)
    e.on_depth(1.1, 31, 0.0)
    assert e.keys[31]["mm"] == 2.5, e.keys[31]["mm"]


def t_physical_release_zeroes():
    """Once the OS reports the key up, the zero is a real release."""
    e = dl.Engine()
    e.keys[31] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
    e.vk_held[0x41] = True
    e.on_depth(1.0, 31, 2.5)
    e.vk_held.pop(0x41, None)
    e.on_depth(1.1, 31, 0.0)
    assert e.keys[31]["mm"] == 0.0, e.keys[31]["mm"]


def t_learning_key_keeps_state():
    """The press that triggers learning must not invalidate its own state."""
    e = dl.Engine()
    e.keys[31] = {"vk": None, "mm": 2.5, "count": 0, "speeds": []}
    e.mark_key(0x41, True, 1.0)
    # _key_transition learnt the key -> generation bumped
    e.mapping[31] = 0x41
    e.keys[31]["vk"] = 0x41
    e.bump_mapping()
    e.refresh_key_state(0x41, True, 1.0)      # the fix under test
    e.on_depth(1.05, 31, 0.0)                 # firmware switch-zero
    assert e.keys[31]["mm"] == 2.5, "learning press lost its held state"


def t_refresh_does_not_resurrect_others():
    """Re-stamping one key must not make a released key look pressed."""
    e = dl.Engine()
    for idx, vk in ((31, 0x41), (33, 0x44)):
        e.keys[idx] = {"vk": vk, "mm": 1.0, "count": 0, "speeds": []}
    e.mark_key(0x41, True, 1.0)            # A held
    e.mark_key(0x44, False, 1.0)           # D released
    e.bump_mapping()
    e.refresh_key_state(0x41, True, 1.0)
    assert e.os_key_down(0x41), "held key stays held"
    assert not e.os_key_down(0x44), "released key must not be resurrected"


def t_learning_keeps_held_state():
    """Learning a new key must not invalidate a key that is still held.

    This is the regression that silently disabled multi-key recording: every
    learn bumped the generation and wiped the held state, so run_hold emitted
    nothing for the key you were still holding.
    """
    e = dl.Engine()
    e.keys[18] = {"vk": 0x57, "mm": 3.5, "count": 0, "speeds": []}
    e.mark_key(0x57, True, 1.0)
    assert e.os_key_down(0x57)
    e.keys[31] = {"vk": None, "mm": 2.0, "count": 0, "speeds": []}
    e.mapping[31] = 0x41
    e.bump_mapping()                       # what learning/binding does
    assert e.os_key_down(0x57), "held key must survive a learn"


def t_reset_still_invalidates():
    """An explicit reset must still drop the physical state."""
    e = dl.Engine()
    e.keys[18] = {"vk": 0x57, "mm": 3.5, "count": 0, "speeds": []}
    e.mark_key(0x57, True, 1.0)
    e.reset_mapping()
    assert not e.os_key_down(0x57)


def t_hold_writer_survives_learning():
    """run_hold must keep emitting for a held key after another key is learnt."""
    import threading, time as _t
    e = dl.Engine()
    e.keys[18] = {"vk": 0x57, "mm": 3.5, "count": 0, "speeds": []}
    e.mark_key(0x57, True, 1.0)
    e.recording = True
    th = threading.Thread(target=e.run_hold)
    th.daemon = True
    th.start()
    _t.sleep(0.12)
    before = e.hold_n
    e.keys[31] = {"vk": None, "mm": 2.0, "count": 0, "speeds": []}
    e.mapping[31] = 0x41
    e.bump_mapping()                        # learn while W is still held
    _t.sleep(0.2)
    e.recording = False
    assert e.hold_n > before, "hold writer stopped emitting after a learn"


def t_stale_state_after_mapping_change():
    """Learning a key must not turn a held key into a released one (v1.10.2),
    while a genuinely released key stays released."""
    e = dl.Engine()
    e.keys[31] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
    e.on_depth(1.0, 31, 2.5)
    e.vk_held[0x41] = True
    e.bump_mapping()                       # mapping change while still held
    assert e.os_key_down(0x41), "held key must survive a mapping change"
    e.vk_held.pop(0x41, None)              # real release
    e.bump_mapping()
    assert not e.os_key_down(0x41), "released key must stay released"


def t_suppressed_counter():
    """Switch-zero suppressed while held counts as a diagnostic, not a sample."""
    e = dl.Engine()
    e.keys[31] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
    e.vk_held[0x41] = True
    e.key_state[0x41] = {"gen": e.mapping_gen, "ts": 1.0}
    e.on_depth(1.0, 31, 2.5)
    before = e.suppressed
    e.on_depth(1.05, 31, 0.0)
    assert e.suppressed == before + 1
    assert e.keys[31]["mm"] == 2.5


def t_reset_mapping_clears_state():
    import tempfile
    e = dl.Engine()
    e.map_path = os.path.join(tempfile.mkdtemp(), "map.json")
    e.mapping[31] = 0x41
    e.keys[31] = {"vk": 0x41, "mm": 2.0, "count": 3, "speeds": []}
    e.vk_held[0x41] = True
    e.press_state[31] = {"peak": 2.0}
    e.pending_bind = True
    e.reset_mapping()
    assert not e.mapping, e.mapping
    assert not e.vk_held, e.vk_held
    assert not e.press_state, e.press_state
    assert e.keys[31]["vk"] is None and e.keys[31]["mm"] == 0.0
    assert not e.pending_bind


def _render_cli(csv_path, out_dir, jobs):
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    return subprocess.run([sys.executable, os.path.join(here, "kb-frames.py"), csv_path,
                           "--png-only", "--jobs", str(jobs), "--fps", "30",
                           "--size", "320x240", "--keys", "W", "A", "S", "D", "LAlt", "Space",
                           "-o", out_dir], capture_output=True, text=True)


def t_render_alpha_and_multiprocess_match():
    """Rendering must keep RGBA, and 1 worker must equal N workers byte for byte."""
    import filecmp, glob, shutil, tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    tmp = tempfile.mkdtemp()
    csv_path = os.path.join(tmp, "t.csv")
    with io.open(csv_path, "w", encoding="utf-8") as f:
        f.write("# t_s,wall,W,A\n")
        f.write("t_s,wall,W,A\n")
        for i in range(30):
            t = i / 30.0
            f.write("%.3f,2026-01-01T00:00:00,%.2f,%.2f\n"
                    % (t, 3.5 if t > 0.3 else 0.0, 2.0 if t > 0.5 else 0.0))
    single = os.path.join(tmp, "single")
    multi = os.path.join(tmp, "multi")
    r1 = _render_cli(csv_path, single, 1)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = _render_cli(csv_path, multi, 4)
    assert r2.returncode == 0, r2.stdout + r2.stderr

    from PIL import Image
    frames = sorted(glob.glob(os.path.join(single, "*.png")))
    assert len(frames) == 31, len(frames)
    img = Image.open(frames[20])
    assert img.mode == "RGBA", img.mode
    alpha_min, alpha_max = img.getchannel("A").getextrema()
    assert alpha_min == 0 and alpha_max > 200, (alpha_min, alpha_max)

    for f in frames:
        mate = os.path.join(multi, os.path.basename(f))
        assert os.path.exists(mate), mate
        assert filecmp.cmp(f, mate, shallow=False), "frame mismatch: " + f
    shutil.rmtree(tmp, ignore_errors=True)


def t_learning_never_steals_bound():
    """Pressing a new key while another is held must not rebind the held index."""
    e = dl.Engine()
    e.keys[18] = {"vk": 0x57, "mm": 3.5, "t_last": 0.0, "count": 0, "speeds": []}
    e.keys[31] = {"vk": None, "mm": 0.0, "t_last": 0.0, "count": 0, "speeds": []}
    e.samples.append((1.0, 18, 3.5))
    cand = e.learn_candidate(1.0)
    assert cand != 18, "held key must not be a learning candidate"


def t_deferred_learning():
    """The OS edge arrives before the analog report: bind when the report lands."""
    e = dl.Engine()
    e.keys[18] = {"vk": 0x57, "mm": 3.5, "t_last": 0.0, "count": 0, "speeds": []}
    e.keys[31] = {"vk": None, "mm": 0.0, "t_last": 0.0, "count": 0, "speeds": []}
    e.apply_key_state(0x53, True, 1.0)          # S pressed, no report yet
    assert e.pending_learn is not None, "should wait for the analog report"
    e.on_depth(1.02, 31, 0.8)                   # report for index 31 arrives
    assert e.mapping.get(31) == 0x53, e.mapping
    assert e.keys[18]["vk"] == 0x57, "held key must keep its binding"
    assert e.pending_learn is None


def t_missed_keyup_edge():
    """A missed key-up edge must not leave the key stuck in the held set."""
    e = dl.Engine()
    e.keys[31] = {"vk": 0x41, "mm": 2.0, "count": 0, "speeds": []}
    e.mark_key(0x41, True, 1.0)
    assert 0x41 in e.vk_held
    e.apply_key_state(0x41, False, 1.1)      # what the watchdog calls
    assert 0x41 not in e.vk_held, "key stuck as held"
    assert e.keys[31]["mm"] == 0.0, e.keys[31]["mm"]


def t_refresh_blocked_after_reset():
    """A late refresh must not resurrect a key the reset has just forgotten."""
    e = dl.Engine()
    e.keys[31] = {"vk": 0x41, "mm": 2.0, "count": 0, "speeds": []}
    e.mapping[31] = 0x41
    assert e.key_known(0x41)
    e.mark_key(0x41, True, 1.0)
    e.bump_mapping()
    e.reset_mapping()
    assert not e.key_known(0x41), "reset must drop the mapping"
    assert not e.os_key_down(0x41)


def t_save_mapping_failure_keeps_memory():
    """When the file cannot be written the in-memory table must stay usable."""
    e = dl.Engine()
    e.mapping[31] = 0x41
    e.keys[31] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
    e._map_paths = lambda: [os.path.join("Z:\\definitely\\not\\here", "map.json")]
    saved = e._save_mapping()
    assert saved is False, saved
    assert e.mapping == {31: 0x41}
    assert e.keys[31]["vk"] == 0x41


def t_ffmpeg_start_failure():
    """encode_segments must clean up and return 5 when ffmpeg cannot start."""
    import shutil, tempfile
    kf = load("kf", os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb-frames.py"))
    tmp = tempfile.mkdtemp()
    frames = os.path.join(tmp, "frames")
    os.makedirs(frames)
    with open(os.path.join(frames, "frame_00000.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    old_ff = kf.FFMPEG
    kf.FFMPEG = os.path.join(tmp, "no-such-ffmpeg.exe")
    try:
        rc = kf.encode_segments(frames, os.path.join(tmp, "out.mov"), 30, 1, 2)
    finally:
        kf.FFMPEG = old_ff
    assert rc == 5, rc
    assert not os.path.exists(os.path.join(tmp, "out.mov"))
    assert not os.path.exists(frames + "-seg")
    shutil.rmtree(tmp, ignore_errors=True)


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


def t_sample_quality():
    """Gap statistics must reflect real inter-arrival times and anomalies."""
    e = dl.Engine()
    t = 0.0
    for k in range(100):
        t += 0.06 if k in (30, 70) else 0.01
        e.on_depth(t, 31, 1.0)
    q = e.sample_quality()
    assert q["n"] == 99, q["n"]
    assert 10.0 <= q["mean_ms"] <= 12.0, q["mean_ms"]
    assert q["max_ms"] >= 55, q["max_ms"]
    assert q["anomalies"] == 2, q["anomalies"]
    assert q["dropouts"] == 0, q["dropouts"]


def t_manifest_integrity():
    """The manifest must record line counts and a validity flag."""
    e = make_engine()
    e.recording = True
    now = time.perf_counter()
    for k in range(50):
        e.on_depth(now + k * 0.01, 31, (k % 5) * 0.6)
    path = e.save_recording()
    assert path
    base = dl.recordings_dir()
    with io.open(os.path.join(base, "latest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    integ = manifest.get("integrity")
    assert integ and integ["ok"] is True, integ
    assert integ["long"]["lines"] > 5 and integ["wide"]["lines"] > 5, integ


def t_save_is_isolated_and_manifested():
    """Saved recordings go under the temp home and are committed by a manifest."""
    base = dl.recordings_dir()
    assert base.startswith(dl.data_home()), (base, dl.data_home())
    e = make_engine()
    e.recording = True
    now = time.perf_counter()
    for k in range(40):
        e.on_depth(now + k * 0.01, 31, (k % 5) * 0.6)
    path = e.save_recording()
    assert path and path.startswith(base), path
    manifest_path = os.path.join(base, "latest.json")
    assert os.path.exists(manifest_path), manifest_path
    with io.open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    for key in ("long", "wide"):
        full = os.path.join(base, manifest[key])
        assert os.path.exists(full), full
        assert os.path.getsize(full) > 0


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


def fetch(port, path, method="GET", token=None, origin=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
    if token:
        req.add_header("X-Depth-Token", token)
    if origin:
        req.add_header("Origin", origin)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read()


def api(port, path):
    """What the page does for every state change."""
    return fetch(port, path, method="POST", token=dl.ENGINE.token,
                 origin="http://127.0.0.1:%d" % port)


def t_http_endpoints():
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        status, body = fetch(port, "/")
        assert status == 200 and b"<html" in body.lower()
        status, body = fetch(port, "/snapshot")
        snap = json.loads(body.decode("utf-8"))
        assert "rec_n" in snap and "rate" in snap
        status, body = api(port, "/rec/start")
        e = dl.ENGINE
        now = time.perf_counter()
        for k in range(60):
            e.on_depth(now + k * 0.01, 31, (k % 6) * 0.5)
        status, body = api(port, "/rec/stop")
        assert status == 200
        for ep in ("/export/wide", "/export/long"):
            status, body = fetch(port, ep)
            assert status == 200, (ep, status)
            text = body.decode("utf-8-sig")
            assert "t_s" in text, "%s has no header: %r" % (ep, text[:60])
            assert text.count("\ufeff") <= 1, "%s has repeated BOM" % ep
            assert len(text.splitlines()) > 3, "%s too short" % ep
        status, body = api(port, "/bind?key=W")
        assert status == 200
        status, body = api(port, "/resetmap")
        assert status == 200
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- render layer

def t_quit_requires_token():
    """A random page must not be able to stop the monitor with GET /quit."""
    import urllib.error
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        token = dl.ENGINE.token
        code = None
        try:
            fetch(port, "/quit", method="POST")
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 403, "expected 403 without token, got %r" % (code,)
        assert srv.fileno() >= 0, "server must still be up after a rejected quit"
        status, body = fetch(port, "/quit", method="POST", token=token)
        assert status == 200, status
    finally:
        srv.shutdown()


def t_quit_blocked_when_save_fails():
    """A failed save must keep the process alive instead of dropping data."""
    e = dl.Engine()
    e.recording = True
    e.rec.append((0.0, 31, 1.0))
    e.save_recording = lambda: None          # simulate a disk error
    assert e.request_quit(None) is False
    assert "\u5931\u8d25" in e.notice, e.notice     # "failed"
    assert not e.stop_io.is_set(), "must not stop the HID reader"


def t_sse_keeps_idle_alive():
    """An open /events stream must keep the server alive past the idle timeout."""
    import threading, time, urllib.request
    dl.ENGINE = make_engine()
    srv, port = start_server()
    idle = threading.Thread(target=dl.ENGINE.run_idle_exit, args=(2, srv), daemon=True)
    idle.start()
    resp = urllib.request.urlopen("http://127.0.0.1:%d/events" % port, timeout=10)
    try:
        got = 0
        deadline = time.time() + 6
        while got < 4 and time.time() < deadline:
            if resp.readline().startswith(b"data:"):
                got += 1
        assert got >= 3, "expected SSE frames, got %d" % got
        time.sleep(3.2)                      # longer than the idle timeout
        stamp = dl.ENGINE.last_client
        assert time.time() - stamp < 1.5, "last_client not refreshed by SSE"
        # the server must still answer while the stream is open
        fetch(port, "/snapshot")
    finally:
        resp.close()
        srv.shutdown()


def t_manual_bind_refreshes_state():
    """The press that performs a manual bind must stay valid afterwards."""
    e = dl.Engine()
    e.keys[31] = {"vk": None, "mm": 2.5, "t_last": 1.0, "count": 0, "speeds": []}
    e.samples.append((1.0, 31, 2.5))
    e.pending_label = "A"
    e.mark_key(0x41, True, 1.0)             # what apply_key_state does first
    bumped = e._key_transition(0x41, True, 1.0)
    assert bumped is True, "manual bind must report that it bumped the mapping"
    if e.key_known(0x41):
        e.refresh_key_state(0x41, True, 1.0)
    assert e.os_key_down(0x41), "the binding press must not be treated as stale"


def t_deferred_bind_refreshes_state():
    """bind_index() bumps the mapping too, so it must refresh its own key."""
    e = dl.Engine()
    e.keys[31] = {"vk": None, "mm": 0.0, "t_last": 0.0, "count": 0, "speeds": []}
    e.pending_learn = (0x53, 2.0)
    e.on_depth(1.0, 31, 0.9)                # report arrives -> deferred bind
    assert e.mapping.get(31) == 0x53
    assert e.os_key_down(0x53), "deferred binding must leave the key held"


def t_idle_exit_uses_full_shutdown():
    """The idle timer must run the same ordered shutdown as /quit."""
    import threading, time
    dl.ENGINE = make_engine()
    srv, port = start_server()
    dl.ENGINE.last_client = time.time() - 10
    dl.ENGINE.run_idle_exit(1, srv)
    assert dl.ENGINE.stop_io.is_set(), "idle exit must stop the HID reader"
    try:
        srv.fileno()
        raise AssertionError("server should be closed")
    except Exception:
        pass


def t_quit_http_409_when_save_fails():
    """A real /quit request must return 409 and keep the server alive."""
    dl.ENGINE = make_engine()
    dl.ENGINE.recording = True
    dl.ENGINE.rec.append((0.0, 31, 1.0))
    dl.ENGINE.save_recording = lambda: None
    srv, port = start_server()
    try:
        code = None
        try:
            fetch(port, "/quit", method="POST", token=dl.ENGINE.token)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 409, "expected 409 on save failure, got %r" % (code,)
        fetch(port, "/snapshot")            # still serving
        assert not dl.ENGINE.stop_io.is_set(), "must not stop I/O when save failed"
    finally:
        srv.shutdown()


def t_rejects_foreign_host():
    """DNS-rebinding style requests with a foreign Host must be refused."""
    import http.client
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/snapshot", headers={"Host": "evil.example.com"})
        resp = conn.getresponse()
        assert resp.status == 403, resp.status
        conn.close()
    finally:
        srv.shutdown()


def t_ipv6_host_allowed():
    """Host: [::1] must parse correctly (rsplit broke it before)."""
    import http.client
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/snapshot", headers={"Host": "[::1]:%d" % port})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 200, "IPv6 loopback host rejected: %d" % resp.status
        conn.close()
    finally:
        srv.shutdown()


def t_cross_origin_state_change_rejected():
    """A page on another origin must not be able to reset the mapping."""
    import http.client
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/resetmap", headers={"Origin": "http://evil.example.com"},
                     body=b"")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 403, resp.status
        conn.close()
        # a same-origin request still works
        api(port, "/resetmap")
    finally:
        srv.shutdown()


def t_foreign_origin_beats_token():
    """A foreign Origin is refused even if the request carries the token."""
    import urllib.error
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        code = None
        try:
            fetch(port, "/resetmap", method="POST", token=dl.ENGINE.token,
                  origin="http://evil.example.com")
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 403, "foreign origin must win over a token, got %r" % (code,)
    finally:
        srv.shutdown()


def t_state_rejects_get():
    """A cross-site <img>/<a> can only issue GET, so GET must not change state."""
    import urllib.error
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        code = None
        try:
            fetch(port, "/resetmap", token=dl.ENGINE.token)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 405, "expected 405 for GET /resetmap, got %r" % (code,)
    finally:
        srv.shutdown()


def t_single_instance_guard():
    """Second instance on the same config dir must exit; another dir must start."""
    import subprocess, tempfile, time

    def up(port):
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/snapshot" % port, timeout=1).read()
            return True
        except Exception:
            return False

    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "depth-live.py")
    home1, home2 = tempfile.mkdtemp(), tempfile.mkdtemp()

    def launch(home, port):
        env = dict(os.environ, DEPTH_MONITOR_HOME=home)
        return subprocess.Popen([sys.executable, script, "--no-browser",
                                 "--port", str(port), "--idle-exit", "0"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    p1 = launch(home1, 8899)
    p3 = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not up(8899):
            time.sleep(0.2)
        assert up(8899), "first instance did not come up"

        p2 = launch(home1, 8898)
        try:
            out, err = p2.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            p2.kill()
            raise AssertionError("second instance kept running: the guard is dead")
        assert p2.returncode == 0, (p2.returncode, out, err)
        assert "another monitor is already running" in (out or ""), out

        p3 = launch(home2, 8897)
        deadline = time.time() + 20
        while time.time() < deadline and not up(8897):
            time.sleep(0.2)
        assert up(8897), "a different config dir must be allowed to run"
    finally:
        for proc in (p1, p3):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except Exception:
                    proc.kill()


class FakeHidApi(object):
    """Minimal kernel32 stand-in: ReadFile blocks until a cancel arrives."""

    def __init__(self):
        self.read_calls = 0
        self.cancels = 0
        self.closed = 0
        self.blocked = threading.Event()
        self.release = threading.Event()

    def CreateFileW(self, *a):
        return 1234

    def ReadFile(self, h, buf, n, byref_n, overlapped):
        self.read_calls += 1
        self.blocked.set()
        self.release.wait(2.0)          # stands in for a blocking kernel call
        return False                    # -> caller reads the error code

    def CancelIoEx(self, h, ov):
        self.cancels += 1
        self.release.set()

    def CloseHandle(self, h):
        self.closed += 1

    def get_last_error(self):
        return 995                       # ERROR_OPERATION_ABORTED


def t_reader_cancel_window():
    """stop() must also cover 'cancel arrived before ReadFile was issued'."""
    hp = load("hp", os.path.join(HERE, "hid-probe.py"))
    api = FakeHidApi()
    sink = []
    r = hp.Reader(0, "fake", 21, sink, api=api)
    r.start()
    assert api.blocked.wait(3.0), "reader never reached ReadFile"
    stopped = r.stop(wait=2.0)
    r.join(3.0)
    assert stopped or not r.is_alive(), "reader did not stop"
    assert api.closed == 1, "handle must be closed exactly once"
    assert not r.is_alive()


def t_reader_stop_before_io():
    """A stop that lands before the loop starts must not call ReadFile."""
    hp = load("hp", os.path.join(HERE, "hid-probe.py"))
    api = FakeHidApi()
    r = hp.Reader(0, "fake", 21, [], api=api)
    r.stop_event.set()                  # stop() before start()
    r.start()
    r.join(3.0)
    assert api.read_calls == 0, "no read should be issued after an early stop"
    assert api.closed == 1


def t_wb_info_and_series():
    """The workbench reads the latest manifest and returns per-key tracks."""
    base = dl.recordings_dir()
    stamp = "20260101-000000"
    vdir = os.path.join(base, stamp)
    os.makedirs(vdir, exist_ok=True)
    wide = os.path.join(vdir, "wide.csv")
    with io.open(wide, "w", encoding="utf-8") as f:
        f.write("# test\nt_s,wall,W,A\n")
        for i in range(50):
            f.write("%.2f,2026-01-01T00:00:00,%.2f,%.2f\n"
                    % (i * 0.01, 3.5 if 10 <= i < 30 else 0.0, 2.0 if 20 <= i < 40 else 0.0))
    with io.open(os.path.join(base, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "wide": stamp + "/wide.csv",
                   "long": stamp + "/long.csv", "samples": 50}, f)
    e = dl.Engine()
    info = e.wb_info()
    assert info["ok"] and info["keys"] == ["W", "A"], info
    assert abs(info["duration"] - 0.49) < 0.02, info["duration"]
    ser = e.wb_series()
    assert ser["ok"] and ser["keys"] == ["W", "A"], ser
    assert len(ser["t"]) == len(ser["v"]["W"])
    peak = max(ser["v"]["W"])
    assert 3.0 <= peak <= 3.5, peak


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


def t_sync_mouse_record_and_wb_replay():
    """Synced keyboard + mouse recording persists mouse.csv and aligns in /wb/series."""
    e = make_engine()
    e.mouse_span = 10000
    e.start_recording()
    t0 = time.perf_counter()
    for i in range(40):
        ti = t0 + i * 0.02
        e.on_depth(ti, 31, 3.0 if 10 <= i < 25 else 0.0)
        btns = {"l": True} if 15 <= i < 22 else {"l": False}
        dx = 180 if i < 20 else -90
        dy = 40 if i < 20 else -30
        e.on_mouse(dx, dy, btns=btns, wheel=(1 if i == 12 else 0), t=ti)
    out = e.save_recording()
    assert out and os.path.isfile(out), out
    vdir = os.path.dirname(out)
    stamp = os.path.basename(vdir)
    mouse_csv = os.path.join(vdir, "mouse.csv")
    assert os.path.isfile(mouse_csv), mouse_csv
    with io.open(os.path.join(dl.recordings_dir(), "latest.json"), "r", encoding="utf-8") as f:
        man = json.load(f)
    assert man.get("mouse") == stamp + "/mouse.csv", man

    info = e.wb_info(stamp)
    assert info["ok"] and info.get("has_mouse") is True, info
    ser = e.wb_series(step=0.02, stamp=stamp)
    assert ser["ok"] and ser.get("mouse"), ser
    m = ser["mouse"]
    assert len(m["x"]) == len(ser["t"]) and len(m["b"]) == len(ser["t"])
    assert max(abs(v) for v in m["x"]) > 0.05, "mouse motion not integrated"
    assert any((b & 1) for b in m["b"]), "LMB clicks not preserved"
    assert any(w > 0 for w in m["w"]), "wheel event not preserved"

    # Legacy recording without mouse.csv stays backwards-compatible
    os.remove(mouse_csv)
    info2 = e.wb_info(stamp)
    assert info2["ok"] and info2.get("has_mouse") is False, info2
    ser2 = e.wb_series(step=0.02, stamp=stamp)
    assert ser2["ok"] and ser2.get("mouse") is None, ser2


def t_autosave_preserves_inflight_events():
    """New keyboard/mouse events arriving during autosave must never be wiped."""
    e = make_engine()
    e.start_recording()
    t0 = time.perf_counter()
    for i in range(25):
        e.on_depth(t0 + i * 0.01, 31, 2.0)
        e.on_mouse(10, 0, t=t0 + i * 0.01)
    orig_save = e.save_recording

    def hooked_save(rec_rows=None, mouse_rows=None):
        # Simulate live input arriving while disk I/O is in progress
        e.on_depth(t0 + 1.0, 31, 3.3)
        e.on_mouse(77, -33, btns={"l": True}, t=t0 + 1.0)
        return orig_save(rec_rows=rec_rows, mouse_rows=mouse_rows)

    e.save_recording = hooked_save
    saved = e.autosave_step(min_samples=20)
    assert saved and os.path.isfile(saved), saved
    assert len(e.rec) == 1 and e.rec[0][2] == 3.3, e.rec
    assert len(e.rec_mouse) == 1 and e.rec_mouse[0][1] == 77, e.rec_mouse


def t_autosave_failure_restores_buffers():
    """If autosave fails on disk error, segment + new events must all be preserved."""
    e = make_engine()
    e.start_recording()
    t0 = time.perf_counter()
    for i in range(25):
        e.on_depth(t0 + i * 0.01, 31, 1.5)
        e.on_mouse(5, 0, t=t0 + i * 0.01)

    def failing_save(rec_rows=None, mouse_rows=None):
        e.on_depth(t0 + 2.0, 31, 3.1)
        e.on_mouse(99, 0, t=t0 + 2.0)
        return None

    e.save_recording = failing_save
    assert e.autosave_step(min_samples=20) is None
    assert len(e.rec) == 26 and e.rec[0][2] == 1.5 and e.rec[-1][2] == 3.1
    assert len(e.rec_mouse) == 26 and e.rec_mouse[0][1] == 5 and e.rec_mouse[-1][1] == 99


def t_rapid_click_and_wheel_preserved():
    """Sub-10ms clicks and multiple wheel ticks inside one sampling bucket must survive."""
    e = make_engine()
    e.start_recording()
    t0 = time.perf_counter()
    for i in range(10):
        e.on_depth(t0 + i * 0.02, 31, 2.0 if i == 2 else 0.0)
    # 5ms rapid LMB click entirely between 0.020s and 0.040s samples
    e.on_mouse(0, 0, btns={"l": True}, t=t0 + 0.024)
    e.on_mouse(0, 0, btns={"l": False}, t=t0 + 0.029)
    # 3 rapid wheel-up ticks inside one 10ms wide.csv bucket (0.060s..0.070s)
    e.on_mouse(0, 0, wheel=1, t=t0 + 0.062)
    e.on_mouse(0, 0, wheel=1, t=t0 + 0.065)
    e.on_mouse(0, 0, wheel=1, t=t0 + 0.068)
    out = e.save_recording()
    assert out
    stamp = os.path.basename(os.path.dirname(out))
    ser = e.wb_series(step=0.02, stamp=stamp)
    m = ser["mouse"]
    assert m and m["clicks"], m
    c0 = m["clicks"][0]
    assert abs(c0[0] - 0.024) < 0.002 and c0[1] == 1 and abs(c0[2] - 5.0) < 1.0, c0
    assert any((b & 1) for b in m["b"]), "rapid click missing from bucket mask"
    assert sum(m["w"]) == 3 and max(m["w"]) == 3, m["w"]
    assert len(m["wheels"]) == 3, m["wheels"]


def t_mouse_memory_cap_and_mouse_only_and_integrity():
    """Memory cap bounds rec_mouse, mouse-only recordings replay, and bad mouse.csv fails."""
    e = make_engine()
    e.max_rec_mouse = 200
    e.start_recording()
    t0 = time.perf_counter()
    for i in range(600):
        e.on_mouse(4, 1, btns={"l": (i == 550)}, t=t0 + i * 0.002)
    assert len(e.rec_mouse) <= 200 and e.mouse_rec_dropped > 0, (len(e.rec_mouse), e.mouse_rec_dropped)
    assert "\u4e0a\u9650" in e.notice, e.notice

    # Mouse-only recording (zero keyboard events) saves and replays cleanly
    out = e.save_recording()
    assert out and os.path.isfile(out), out
    stamp = os.path.basename(os.path.dirname(out))
    info = e.wb_info(stamp)
    assert info["ok"] and info["has_mouse"] is True and info["keys"] == [], info
    ser = e.wb_series(step=0.02, stamp=stamp)
    assert ser["ok"] and ser["keys"] == [] and ser["mouse"] and len(ser["t"]) > 5, ser

    # Corrupt mouse.csv write must fail integrity and NOT commit latest.json
    latest_p = os.path.join(dl.recordings_dir(), "latest.json")
    before = open(latest_p, encoding="utf-8").read()
    e.mouse_lines = lambda t0=None, rows=None: iter(["# truncated only\n"])
    assert e.save_recording() is None
    assert open(latest_p, encoding="utf-8").read() == before


def t_rt_release_preserves_true_travel():
    """RT OS Key-Up while magnet is still at 3.25mm must NOT inject an artificial 0.0."""
    e = make_engine()
    e.start_recording()
    e.on_depth(1.000, 31, 3.50)
    e.apply_key_state(0x41, True, 1.000)
    e.on_depth(1.010, 31, 3.50)
    # RT fires OS Key-Up at 1.015s while physical magnet is only slightly lifted
    e.apply_key_state(0x41, False, 1.015)
    assert e.keys[31]["mm"] == 3.50, "RT key-up wrongly slammed mm to %r" % e.keys[31]["mm"]
    assert all(r[2] > 0.0 for r in e.rec), "artificial 0.0 injected into recording: %r" % e.rec
    assert e.press_state[31] and e.press_state[31]["up"] == 3.50, e.press_state[31]
    # Hardware continues streaming real rebound travel every 10ms
    for i, mm in enumerate((3.25, 2.80, 2.41, 1.49, 0.35), start=1):
        t = 1.015 + i * 0.010
        e.on_depth(t, 31, mm)
        e.sweep_silent_released(t + 0.004)
        assert e.keys[31]["mm"] == mm, (mm, e.keys[31]["mm"])
    # Hardware finally sends physical 0.00 mm
    e.on_depth(1.080, 31, 0.0)
    assert e.keys[31]["mm"] == 0.0
    assert e.last_press and e.last_press["peak"] == 3.50


def t_socd_hold_and_silent_fallback():
    """SOCD OS Key-Up keeps true depth while HID streams, and zeroes after 120ms silence."""
    e = make_engine()
    e.on_depth(2.000, 31, 3.50)
    e.apply_key_state(0x41, True, 2.000)
    # SOCD triggers A Key-Up while finger keeps holding A at 3.50mm
    e.apply_key_state(0x41, False, 2.010)
    for i in range(1, 20):
        t = 2.010 + i * 0.010
        e.on_depth(t, 31, 3.50)
        e.sweep_silent_released(t + 0.005)
        assert e.keys[31]["mm"] == 3.50
    # Once HID stream goes silent for >120ms while OS-up, fallback zeroes cleanly
    e.sweep_silent_released(2.010 + 0.20 + 0.15)
    assert e.keys[31]["mm"] == 0.0, e.keys[31]["mm"]


def t_wb_fps_analysis():
    """/wb/analysis detects bad overlaps, clean gaps, moving shots, key std & mouse heatmap."""
    base = dl.recordings_dir()
    stamp = "20261128-075900"
    vdir = os.path.join(base, stamp)
    os.makedirs(vdir, exist_ok=True)
    wide = os.path.join(vdir, "wide.csv")
    mouse_p = os.path.join(vdir, "mouse.csv")
    # 2.00s at 100Hz:
    # - 0.10..0.46 A=3.5; 0.40..0.75 D=3.2 => 60ms bad overlap A->D at 0.40..0.46
    # - 0.78..1.10 A=3.0 => 20ms clean gap D->A at 0.76..0.78
    with io.open(wide, "w", encoding="utf-8") as f:
        f.write("# test\nt_s,wall,A,D\n")
        for i in range(201):
            t = i * 0.01
            a = 3.5 if 0.10 <= t <= 0.46 else (3.0 if 0.78 <= t <= 1.10 else 0.0)
            d = 3.2 if 0.40 <= t <= 0.75 else 0.0
            f.write("%.3f,2026-11-28T07:59:00,%.2f,%.2f\n" % (t, a, d))
    with io.open(mouse_p, "w", encoding="utf-8") as f:
        f.write("# tool=depth-live  mouse_span=10000\nt_s,dx,dy,btn,wheel\n")
        # Shot 1 at 0.55s while D=3.2 (moving shot)
        f.write("0.500,300,50,0,0\n0.550,20,0,1,0\n0.600,0,0,0,0\n")
        # Shot 2 at 1.40s while A=D=0.0 (still shot)
        f.write("1.350,-250,-40,0,0\n1.400,0,0,1,0\n1.450,0,0,0,0\n")
    e = dl.Engine()
    an = e.wb_analysis(stamp=stamp, pair=("A", "D"))
    assert an["ok"], an
    st = an["strafes"]
    assert st["total"] == 2 and st["overlap_count"] == 1 and st["clean_count"] == 1, st
    assert abs(st["clean_pct"] - 50.0) < 0.2 and st["worst"][0]["ms"] >= 55.0, st
    sh = an["shots"]
    assert sh["total"] == 2 and sh["still_count"] == 1 and abs(sh["still_pct"] - 50.0) < 0.2, sh
    assert sh["moving"][0]["key"] == "D" and sh["moving"][0]["mm"] >= 3.0, sh
    ks = {k["key"]: k for k in an["key_stats"]}
    assert ks["A"]["presses"] == 2 and ks["A"]["peak_std"] > 0.1, ks
    assert an["mouse_heat"]["max"] > 0 and len(an["mouse_heat"]["lmb"]) == 2, an["mouse_heat"]
    # Legacy recording without mouse.csv
    os.remove(mouse_p)
    an2 = e.wb_analysis(stamp=stamp, pair=("A", "D"))
    assert an2["ok"] and an2["shots"] is None and an2["mouse_heat"] is None and an2["strafes"]["total"] == 2


def t_kb_frames_mouse_overlay_and_webm():
    """kb-frames.py renders synced keyboard+mouse RGBA frames (side/stack) and VP9 alpha WebM."""
    import glob, shutil, subprocess, tempfile
    from PIL import Image
    here = os.path.dirname(os.path.abspath(__file__))
    kb = os.path.join(here, "kb-frames.py")
    tmp = tempfile.mkdtemp()
    try:
        wide = os.path.join(tmp, "wide.csv")
        mouse = os.path.join(tmp, "mouse.csv")
        with io.open(wide, "w", encoding="utf-8") as f:
            f.write("# test\nt_s,wall,W,A,S,D\n")
            for i in range(25):
                t = i * 0.02
                f.write("%.3f,2026-11-28T08:00:00,%.2f,%.2f,0.00,%.2f\n"
                        % (t, 3.4 if i < 10 else 0.0, 3.2 if 8 <= i < 16 else 0.0, 3.5 if i >= 14 else 0.0))
        with io.open(mouse, "w", encoding="utf-8") as f:
            f.write("# tool=depth-live  mouse_span=10000\nt_s,dx,dy,btn,wheel\n")
            for i in range(25):
                f.write("%.3f,%d,%d,%d,0\n" % (i * 0.02, 160 if i < 12 else -120, 40, 1 if 10 <= i <= 16 else 0))

        # Test side-by-side & stack PNG alpha frames
        for mlay in ("side", "stack"):
            out_dir = os.path.join(tmp, "f_" + mlay)
            r = subprocess.run([sys.executable, kb, wide, "--mouse", mouse, "--mouse-layout", mlay,
                                "--layout", "hud", "--fps", "20", "--size", "640x360",
                                "--jobs", "1", "--png-only", "-o", out_dir],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
            frames = sorted(glob.glob(os.path.join(out_dir, "*.png")))
            assert len(frames) >= 8, len(frames)
            img = Image.open(frames[len(frames) // 2])
            assert img.mode == "RGBA" and img.size == (640, 360)
            a_min, a_max = img.getchannel("A").getextrema()
            assert a_min == 0 and a_max > 200, (mlay, a_min, a_max)

        # Test WebM VP9 transparent export if ffmpeg is installed
        if shutil.which("ffmpeg"):
            out_base = os.path.join(tmp, "out_vp9")
            r = subprocess.run([sys.executable, kb, wide, "--mouse", mouse, "--mouse-layout", "side",
                                "--layout", "hud", "--fps", "20", "--size", "640x360",
                                "--jobs", "1", "--format", "webm", "-o", out_base],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
            webm_path = out_base + ".webm"
            assert os.path.isfile(webm_path) and os.path.getsize(webm_path) > 1000, webm_path
            ff = subprocess.run(["ffmpeg", "-c:v", "libvpx-vp9", "-i", webm_path], capture_output=True, text=True)
            info_s = (ff.stderr or "").lower()
            assert "vp9" in info_s and ("alpha_mode" in info_s or "yuva420p" in info_s), ff.stderr

        # Standalone kb-viz.py HTML export
        viz_out = os.path.join(tmp, "viz.html")
        rv = subprocess.run([sys.executable, os.path.join(here, "kb-viz.py"),
                             os.path.join(here, "sample", "sample-wide.csv"), "-o", viz_out],
                            capture_output=True, text=True)
        assert rv.returncode == 0 and os.path.isfile(viz_out) and os.path.getsize(viz_out) > 2000, rv.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------- daily user journeys

def t_user_settings_and_bind_unbind_flow():
    """User tweaks full_mm/theme/mouse_span, restarts, binds & right-click unbinds a key."""
    dl.ENGINE = make_engine()
    srv, port = start_server()
    try:
        body = json.dumps({"full_mm": 3.80, "theme": "mint", "mouse_span": 11200}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/settings" % port, data=body, method="POST")
        req.add_header("X-Depth-Token", dl.ENGINE.token)
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read().decode("utf-8"))
        assert res["ok"] and abs(res["full_mm"] - 3.80) < 0.01 and res["theme"] == "mint" and res["mouse_span"] == 11200
        # Restarted Engine must reload persisted settings
        e2 = dl.Engine()
        assert abs(e2.full_mm - 3.80) < 0.01 and e2.theme == "mint" and e2.mouse_span == 11200

        # Left-click bind + right-click unbind flow (simulate key moving 0 -> 2.2mm)
        api(port, "/bind?key=W")
        assert dl.ENGINE.pending_label == "W", dl.ENGINE.pending_label
        now_t = time.perf_counter()
        dl.ENGINE.on_depth(now_t - 0.02, 88, 0.0)
        dl.ENGINE.on_depth(now_t, 88, 2.2)
        dl.ENGINE.apply_key_state(0x57, True, now_t)
        assert dl.ENGINE.mapping.get(88) == 0x57, (dl.ENGINE.mapping, dl.ENGINE.bind_msg)
        _, raw_un = api(port, "/unbind?key=W")
        assert json.loads(raw_un.decode("utf-8"))["ok"] is True
        assert 88 not in dl.ENGINE.mapping, dl.ENGINE.mapping
    finally:
        srv.shutdown()


def t_user_close_page_auto_exits():
    """Closing the browser tab (SSE disconnect) triggers clean idle auto-exit."""
    dl.ENGINE = make_engine()
    srv, port = start_server()
    idle = threading.Thread(target=dl.ENGINE.run_idle_exit, args=(2, srv), daemon=True)
    idle.start()
    resp = urllib.request.urlopen("http://127.0.0.1:%d/events" % port, timeout=10)
    try:
        # While tab is open, SSE keeps backend alive past idle window
        got = 0
        deadline = time.time() + 5
        while got < 3 and time.time() < deadline:
            if resp.readline().startswith(b"data:"):
                got += 1
        assert got >= 3
        time.sleep(2.4)
        fetch(port, "/snapshot")
    finally:
        resp.close()
    # After user closes browser tab, backend auto-exits promptly
    idle.join(timeout=6.0)
    assert not idle.is_alive() and dl.ENGINE.stop_io.is_set(), "backend did not auto-exit after tab closed"


def t_user_overlay_custom_keys_and_preset_switch():
    """All 4 web pages pass JS syntax; /overlay parses Tab/Shift/Alt aliases & clears keys on preset switch."""
    import re, shutil, subprocess, tempfile
    pages = {
        "main": dl.page_html(),
        "overlay": dl.overlay_html(),
        "mouse": dl.mouse_overlay_html(),
        "wb": dl.workbench_html(),
    }
    ov_html = pages["overlay"]
    assert "pQuickKeys" in ov_html and "pCap" in ov_html, "custom key chips/capture missing"
    assert "cfg.keys = ''" in ov_html and "createDocumentFragment" in ov_html, "preset switch fix missing"
    assert "finally" in ov_html and "#kb.many .k" in ov_html, "rAF keepalive / GPU lag guard missing"
    # Verify mouse overlay uses Home reset (not game reload R) and supports independent mopacity
    m_html = pages["mouse"]
    assert "mopacity" in m_html and "Home" in m_html and "e.code === 'KeyR'" not in m_html

    if shutil.which("node"):
        tmp = tempfile.mkdtemp()
        try:
            for k, html in pages.items():
                js = re.findall(r"<script>(.*?)</script>", html, re.S)[0]
                p = os.path.join(tmp, k + ".js")
                with io.open(p, "w", encoding="utf-8") as f:
                    f.write(js)
                subprocess.check_call(["node", "--check", p])
            # Actually execute parseCustomKeys from overlay JS with Chinese/typo aliases
            ov_js = re.findall(r"<script>(.*?)</script>", ov_html, re.S)[0]
            head = ov_js.split("function clamp(", 1)[0]
            test_js = head + "\nconsole.log(JSON.stringify(parseCustomKeys('tap\uff0c\u4e0a\u6863\uff0calt\uff0cW A S D \u7a7a\u683c')));"
            tp = os.path.join(tmp, "test_keys.js")
            with io.open(tp, "w", encoding="utf-8") as f:
                f.write(test_js)
            out = subprocess.check_output(["node", tp], text=True).strip()
            parsed = json.loads(out)
            assert parsed == ["Tab", "LShift", "LAlt", "W", "A", "S", "D", "Space"], parsed
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def t_user_workbench_history_switch_and_delete():
    """User records 2 sessions (synced + legacy), switches between them in /wb, then deletes one."""
    dl.ENGINE = make_engine()
    t0 = time.perf_counter()
    # Session 1: synced keyboard + mouse
    dl.ENGINE.start_recording()
    for i in range(25):
        dl.ENGINE.on_depth(t0 + i * 0.02, 31, 3.2 if i < 12 else 0.0)
        dl.ENGINE.on_mouse(80, 20, btns={"l": (i == 10)}, t=t0 + i * 0.02)
    p1 = dl.ENGINE.stop_recording(save=True)
    s1 = os.path.basename(os.path.dirname(p1))
    # Session 2: keyboard-only
    dl.ENGINE.start_recording()
    for i in range(25):
        dl.ENGINE.on_depth(t0 + 2.0 + i * 0.02, 33, 3.0 if i < 10 else 0.0)
    p2 = dl.ENGINE.stop_recording(save=True)
    s2 = os.path.basename(os.path.dirname(p2))

    srv, port = start_server()
    try:
        lst = json.loads(fetch(port, "/wb/list")[1].decode("utf-8"))
        stamps = {it["stamp"]: it for it in lst["items"]}
        assert s1 in stamps and stamps[s1]["has_mouse"] is True
        assert s2 in stamps and stamps[s2]["has_mouse"] is False
        # Switch to session 1 and verify series + FPS analysis work over HTTP
        ser1 = json.loads(fetch(port, "/wb/series?stamp=" + s1)[1].decode("utf-8"))
        an1 = json.loads(fetch(port, "/wb/analysis?pair=A,D&stamp=" + s1)[1].decode("utf-8"))
        assert ser1["ok"] and ser1["mouse"] is not None
        assert an1["ok"] and an1["shots"] is not None
        # Delete session 2 via POST /wb/delete
        req = urllib.request.Request("http://127.0.0.1:%d/wb/delete" % port,
                                     data=json.dumps({"stamp": s2}).encode("utf-8"), method="POST")
        req.add_header("X-Depth-Token", dl.ENGINE.token)
        with urllib.request.urlopen(req, timeout=5) as r:
            dres = json.loads(r.read().decode("utf-8"))
        assert dres["ok"] is True
        lst2 = json.loads(fetch(port, "/wb/list")[1].decode("utf-8"))
        assert all(it["stamp"] != s2 for it in lst2["items"])
    finally:
        srv.shutdown()


def main():
    print("depth monitor user-workflow gate tests")
    for name, fn in [
        # 1. 主监测页 / 实时行程、RT/SOCD、改绑与设置持久化、关页自动退出
        ("RT release preserves true travel", t_rt_release_preserves_true_travel),
        ("SOCD hold + silent fallback zero", t_socd_hold_and_silent_fallback),
        ("multi-key hold writer + firmware switch-zero", t_hold_without_mapping),
        ("settings persistence + bind/unbind flow", t_user_settings_and_bind_unbind_flow),
        ("close browser tab auto-exits backend", t_user_close_page_auto_exits),
        # 2. 直播设备 /overlay & /overlay/mouse（自定义键、预设切换、鼠标配置）
        ("overlay custom keys + preset switch + JS check", t_user_overlay_custom_keys_and_preset_switch),
        # 3. 键鼠同步录制、自动分段保存防丢、长录制内存上限与完整性
        ("synced mouse record + wb replay + legacy", t_sync_mouse_record_and_wb_replay),
        ("autosave preserves inflight events", t_autosave_preserves_inflight_events),
        ("autosave failure restores buffers", t_autosave_failure_restores_buffers),
        ("rapid click and wheel preserved", t_rapid_click_and_wheel_preserved),
        ("mouse memory cap, mouse-only & integrity", t_mouse_memory_cap_and_mouse_only_and_integrity),
        # 4. 素材工作台 /wb（历史切换删除、FPS 急停/开火诊断）
        ("workbench history switch & delete flow", t_user_workbench_history_switch_and_delete),
        ("workbench FPS diagnosis analysis", t_wb_fps_analysis),
        # 5. 视频与网页导出（键鼠透明帧、WebM VP9 透明、多进程一致性、kb-viz）
        ("kb-frames mouse overlay + WebM VP9 + kb-viz", t_kb_frames_mouse_overlay_and_webm),
        ("render alpha + multiprocess match", t_render_alpha_and_multiprocess_match),
        # 6. 安全与单实例运行保护
        ("single instance guard", t_single_instance_guard),
        ("quit protection (token + 409 on save fail)", t_quit_http_409_when_save_fails),
        ("local origin/host security guard", t_cross_origin_state_change_rejected),
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
