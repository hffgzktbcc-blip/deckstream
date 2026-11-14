#!/usr/bin/env python3
"""Tests for early_launch_calc.py (the head-start formula), with hand-computed expectations."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_launch_calc as c  # noqa: E402

MB = 1_000_000
profile = {"ranges": [
    {"t": 10, "f": "exe", "o": 0, "n": 100 * MB},          # rank 0
    {"t": 20, "f": "pak", "o": 0, "n": 400 * MB},          # rank 100 MB
    {"t": 30, "f": "pak", "o": 1000 * MB, "n": 500 * MB},  # rank 500 MB
], "later": [{"t": 40, "f": "pak", "o": 5000 * MB, "n": 300 * MB}]}      # rank 1000 MB with --include-later
LINE = 20 * MB

# 1) order and ranks
ordered, total = c.order_ranges(profile)
assert total == 1000 * MB and [r[3] for r in ordered] == [0, 100 * MB, 500 * MB], ordered
idx = c.build_index(ordered)
assert c.rank_of(idx, "pak", 1000 * MB + 5) == 500 * MB + 5
assert c.rank_of(idx, "pak", 900 * MB) is None and c.rank_of(idx, "nope", 0) is None

# 2) formula: reads (t, file, offset, size)
ev = [(0, "exe", 0, 1 * MB),                        # needs 1 MB at t=0
      (10, "pak", 0, 4 * MB),                       # rank 100 MB + 4 MB - 20*10 = -96 MB -> fine
      (5, "pak", 1000 * MB, 10 * MB)]               # rank 500 MB + 10 MB - 20*5 = 410 MB  <- the tightest
r = c.head_start(profile, ev, LINE)
assert r["h_min_bytes"] == 410 * MB and r["uncovered"] == 0 and r["worst"]["t"] == 5, r
# a read that comes late needs no head start
r = c.head_start(profile, [(100, "pak", 1000 * MB, 10 * MB)], LINE)   # 510 MB - 2000 MB < 0
assert r["h_min_bytes"] == 0, r
# a read at t=0 deep in the order needs the whole prefix
r = c.head_start(profile, [(0, "pak", 1000 * MB, 10 * MB)], LINE)
assert r["h_min_bytes"] == 510 * MB, r
# uncovered reads are counted, not guessed; later part only counts with include_later
r = c.head_start(profile, [(0, "pak", 5000 * MB, MB), (0, "exe", 0, MB)], LINE)
assert r["uncovered"] == 1 and r["h_min_bytes"] == 1 * MB, r
r = c.head_start(profile, [(0, "pak", 5000 * MB, MB)], LINE, include_later=True)
assert r["uncovered"] == 0 and r["h_min_bytes"] == 1000 * MB + MB, r

# 3) rclone log parsing (1 s timestamps; the first read is t=0; windowing)
lines = [
    "2026/10/11 17:38:30 DEBUG : FINAL FANTASY VII REMAKE/ff7remake.exe(0x1234abcd): _readAt: size=131072, off=0",
    "2026/10/11 17:38:30 DEBUG : something else entirely",
    "2026/10/11 17:38:35 DEBUG : End/Content/Paks/pak0.pak(0xdeadbeef01): _readAt: size=4096, off=8192",
    "2026/10/11 17:39:00 INFO  : not a read line",
    "2026/10/11 17:40:00 DEBUG : End/Content/Paks/pak0.pak(0xdeadbeef01): _readAt: size=1048576, off=1048576",
]
ev = c.parse_rclone_log(lines)
assert [(round(t), f, o, s) for t, f, o, s in ev] == [
    (0, "FINAL FANTASY VII REMAKE/ff7remake.exe", 0, 131072),
    (5, "End/Content/Paks/pak0.pak", 8192, 4096),
    (90, "End/Content/Paks/pak0.pak", 1048576, 1048576)], ev
ev = c.parse_rclone_log(lines, start="2026/10/11 17:38:34")
assert len(ev) == 2 and ev[0][0] == 0.0, "t=0 is the first read inside the window"
assert len(c.parse_rclone_log(lines, end="2026/10/11 17:38:31")) == 1

# 4) command line
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "p.json")
    lg = os.path.join(d, "r.log")
    json.dump(profile, open(p, "w"))
    open(lg, "w").write("\n".join([
        "2026/10/11 17:38:30 DEBUG : exe(0x1): _readAt: size=1000000, off=0",
        "2026/10/11 17:38:31 DEBUG : pak(0x2): _readAt: size=10000000, off=1000000000"]))
    import subprocess
    out = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_launch_calc.py"),
                          "--profile", p, "--rclone-log", lg, "--line-mbps", "20", "--margin", "0.5", "--json"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    j = json.loads(out.stdout)
    # second read: t=1 s, rank 500 MB + 10 MB - 20 MB = 490 MB; margin 50% -> 735 MB
    assert j["h_min_bytes"] == 490 * MB and j["safe_head_start_bytes"] == 735 * MB, j
    empty = os.path.join(d, "e.log")
    open(empty, "w").write("nothing\n")
    out = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_launch_calc.py"),
                          "--profile", p, "--rclone-log", empty], capture_output=True, text=True)
    assert out.returncode == 2, "no reads in the log is an error, not a zero head start"

# A read beginning inside a range must not conceal an uncovered tail or gap.
assert c.head_start({"ranges": [{"f": "a", "o": 0, "n": 10}]}, [(0, "a", 5, 10)], 1)["uncovered"] == 1
assert c.read_rank_end(c.build_index([("a", 0, 5, 0), ("a", 5, 5, 100)]), "a", 0, 10) == 105
assert c.read_rank_end(c.build_index([("a", 0, 5, 0), ("a", 6, 5, 5)]), "a", 0, 10) is None
assert c.head_start({"ranges": [{"f": "a", "o": 0, "n": 10}]}, [(0.5, "a", 0, 10)], 1)["h_min_bytes"] == 10

print("[ok] early-launch head-start formula, log parsing and command line")
print("\nEARLY LAUNCH CALC TESTS PASSED")
