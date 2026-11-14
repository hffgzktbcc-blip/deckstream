#!/usr/bin/env python3
"""How big a head start does a game need before it can launch while its start-up data is still downloading?

DeckStream: collect a warm WebDAV read trace with DECKSTREAM_DEMAND_LOG=1.
This is a model, not a measured hardware launch time.

Model. The download delivers the profile's ranges in FIRST-NEED order at LINE bytes/s. The game, launched after a head
start of H bytes, reads at known times t (seconds after launch) taken from a WARM run's rclone debug log (a warm run is
not slowed by the network, so it shows the game's true read timeline). A read of `size` bytes at `offset` in `file` finds
its data when  rank + size <= H + LINE * t , where rank is that range's byte position in the download order. So

    H_min = max(0, max over all reads of (rank + size - LINE * t))

Reads the profile does not cover are reported separately (they would be fetched on demand by rclone and stall the game).

Usage:
  early_launch_calc.py --profile PROFILE.json --rclone-log RCLONE.log [--start "YYYY/MM/DD HH:MM:SS"] [--end "..."]
                       [--line-mbps 21] [--margin 0.2] [--include-later] [--json]
  --start/--end pick the warm run inside an appended log (default: the whole log, t=0 at its first read).
"""
import argparse
import math
import json
import re
import sys
import time

MB = 1_000_000
LINE_RE = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)(?:\.\d+)? DEBUG : (.+?)\(0x[0-9a-fA-F]+\): _readAt: size=(\d+), off=(\d+)")


def parse_ts(s):
    return time.mktime(time.strptime(s, "%Y/%m/%d %H:%M:%S"))


def parse_rclone_log(lines, start=None, end=None):
    """[(seconds since the first read, file, offset, size)] from rclone's DEBUG '_readAt' lines (1 s resolution)."""
    lo = parse_ts(start) if start else None
    hi = parse_ts(end) if end else None
    out, t0 = [], None
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        ts = parse_ts(m.group(1))
        if (lo is not None and ts < lo) or (hi is not None and ts > hi):
            continue
        if t0 is None:
            t0 = ts
        out.append((ts - t0, m.group(2), int(m.group(4)), int(m.group(3))))
    return out


def order_ranges(profile, include_later=False):
    """Profile ranges in download order with their cumulative start: [(file, offset, length, rank_start)]."""
    rs = list(profile.get("ranges", []))
    if include_later:
        rs += list(profile.get("later", []))
    rs.sort(key=lambda r: (r.get("t", 0.0), r["f"], r["o"]))
    out, pos = [], 0
    for r in rs:
        out.append((r["f"], r["o"], r["n"], pos))
        pos += r["n"]
    return out, pos


def build_index(ordered):
    idx = {}
    for f, o, n, rank in ordered:
        idx.setdefault(f, []).append((o, n, rank))
    for f in idx:
        idx[f].sort()
    return idx


def rank_of(idx, f, off):
    """Download-order position of byte `off` of file `f`, the earliest one if several ranges cover it; None if uncovered."""
    best = None
    for o, n, rank in idx.get(f, []):
        if o > off:
            break
        if off < o + n:
            r = rank + (off - o)
            best = r if best is None else min(best, r)
    return best


def read_rank_end(idx, f, off, size):
    """Latest required download position for the complete read; None if any byte is uncovered."""
    if off < 0 or size <= 0:
        return None
    end = off + size
    boundaries = {off, end}
    for start, length, _rank in idx.get(f, []):
        boundaries.update(point for point in (start, start + length) if off < point < end)
    points = sorted(boundaries)
    required = 0
    for start, stop in zip(points, points[1:]):
        rank = rank_of(idx, f, start)
        if rank is None:
            return None
        required = max(required, rank + stop - start)
    return required


def head_start(profile, events, line_bytes_per_s, include_later=False):
    """Returns dict(h_min_bytes, total_bytes, reads, uncovered, worst={...})."""
    ordered, total = order_ranges(profile, include_later)
    idx = build_index(ordered)
    need, worst, uncovered = 0.0, None, 0
    for t, f, off, size in events:
        r = read_rank_end(idx, f, off, size)
        if r is None:
            uncovered += 1
            continue
        c = r - line_bytes_per_s * t
        if c > need:
            need, worst = c, {"t": t, "file": f, "offset": off, "rank": rank_of(idx, f, off)}
    return {"h_min_bytes": math.ceil(max(0.0, need)), "total_bytes": total, "reads": len(events), "uncovered": uncovered,
            "worst": worst}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--rclone-log", required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--line-mbps", type=float, default=21.0)
    ap.add_argument("--margin", type=float, default=0.2, help="extra head start as a fraction of H_min (default 20%%)")
    ap.add_argument("--include-later", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not math.isfinite(a.line_mbps) or a.line_mbps <= 0 or not math.isfinite(a.margin) or a.margin < 0:
        ap.error("line-mbps must be positive and margin nonnegative (finite numbers)")
    with open(a.profile) as fh:
        profile = json.load(fh)
    with open(a.rclone_log, errors="replace") as f:
        events = parse_rclone_log(f, a.start, a.end)
    if not events:
        print("no _readAt lines found in the chosen window of the rclone log", file=sys.stderr)
        return 2
    line = a.line_mbps * MB
    r = head_start(profile, events, line, a.include_later)
    h = r["h_min_bytes"]
    safe = int(h * (1 + a.margin))
    r.update({"line_mbps": a.line_mbps, "safe_head_start_bytes": safe, "safe_head_start_seconds": safe / line,
              "full_fill_seconds": r["total_bytes"] / line,
              "seconds_saved": max(0.0, (r["total_bytes"] - safe) / line)})
    if a.json:
        print(json.dumps(r))
    else:
        print(f"reads analysed: {r['reads']}  (uncovered by the profile: {r['uncovered']})")
        print(f"profile download order: {r['total_bytes']/1e9:.2f} GB = {r['full_fill_seconds']/60:.1f} min at {a.line_mbps:g} MB/s")
        print(f"minimum head start H_min: {h/1e9:.2f} GB   (+{a.margin:.0%} margin: {safe/1e9:.2f} GB = {r['safe_head_start_seconds']/60:.1f} min)")
        print(f"launch earlier than a full fill by about {r['seconds_saved']/60:.1f} min")
        if r["worst"]:
            w = r["worst"]
            print(f"tightest read: t={w['t']:.0f}s {w['file']} @ {w['offset']} (rank {w['rank']/1e9:.2f} GB into the download order)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
