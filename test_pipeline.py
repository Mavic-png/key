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
    """Re-stamping one key must not re-validate state from the old mapping."""
    e = dl.Engine()
    for idx, vk in ((31, 0x41), (33, 0x44)):
        e.keys[idx] = {"vk": vk, "mm": 1.0, "count": 0, "speeds": []}
    e.mark_key(0x41, True, 1.0)
    e.mark_key(0x44, True, 1.0)
    e.bump_mapping()
    e.refresh_key_state(0x41, True, 1.0)
    assert e.os_key_down(0x41)
    assert not e.os_key_down(0x44), "old-generation key must stay invalid"


def t_stale_state_after_mapping_change():
    """A physical state captured under an older mapping must not suppress a zero."""
    e = dl.Engine()
    e.keys[31] = {"vk": 0x41, "mm": 0.0, "count": 0, "speeds": []}
    e.on_depth(1.0, 31, 2.5)
    e.vk_held[0x41] = True
    e.key_state[0x41] = {"gen": e.mapping_gen, "ts": 1.0}
    assert e.os_key_down(0x41), "fresh state should count"
    e.bump_mapping()                       # mapping changed while the key is held
    assert not e.os_key_down(0x41), "stale state must be ignored"
    e.on_depth(1.1, 31, 0.0)               # so the zero is a real release
    assert e.keys[31]["mm"] == 0.0, e.keys[31]["mm"]


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
        ("firmware zero while held", t_firmware_zero_while_held),
        ("physical release zeroes", t_physical_release_zeroes),
        ("learning key keeps its state", t_learning_key_keeps_state),
        ("refresh does not resurrect others", t_refresh_does_not_resurrect_others),
        ("stale state after mapping change", t_stale_state_after_mapping_change),
        ("suppressed counter", t_suppressed_counter),
        ("reset mapping clears state", t_reset_mapping_clears_state),
        ("learning never steals a bound key", t_learning_never_steals_bound),
        ("deferred learning binds on report", t_deferred_learning),
        ("missed key-up edge clears held", t_missed_keyup_edge),
        ("late refresh blocked after reset", t_refresh_blocked_after_reset),
        ("save mapping failure keeps memory", t_save_mapping_failure_keeps_memory),
        ("ffmpeg start failure cleans up", t_ffmpeg_start_failure),
        ("render alpha + multiprocess match", t_render_alpha_and_multiprocess_match),
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
