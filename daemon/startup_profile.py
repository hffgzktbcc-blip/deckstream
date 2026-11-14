#!/usr/bin/env python3
"""Start-up profiles for stream mode.

A game's start-up reads a small, repeatable part of its files (Sifu: ~5 GB of 33 GB). rclone's cache file for
each game file is sparse, so the filesystem itself knows exactly which byte ranges were downloaded. This tool

  record    samples those ranges every few seconds while a game runs and logs the NEW ranges with a timestamp
  build     turns that log into a profile: the ranges in the order they were first read (optionally only the
            first N seconds / N GB, i.e. the start-up part)
  prefetch  replays a profile through the mount, in order and in parallel, so a cold cache is filled with what
            the game is about to ask for (large ranges fetched in parallel instead of many slow random reads)
  prefetch-edges  for a game with no profile yet: read the first and last 1 MB of every big file in parallel
                  (archive headers and indexes are always read first), skipping what is cached
  fill-cache  fetch EXACTLY the profile's bytes with direct HTTP range requests and write them into rclone's cache files
              (no over-fetch); only while rclone is not using that cache
  synth-profile  a STARTER profile for a never-played game: its start-up files whole + the first/last MB of big files
  lang-skips  other-language files of a rip listing (cloud-install.sh leaves them out; the whole warm too)
  show      prints a profile summary
  repair-meta  rebuilds rclone's cache metadata from the sparse data files (see below)
  adopt-cache  moves cached files from older rclone cache folders into the current one
  merge-cache  after a session: merges the ranges left in older folders into the current one and removes them
  merge-pending  merges a recording a badly ended session left behind (the runner calls it at Play)
  session-start / session-done / finish-session
               a stream session leaves a small marker file; the daemon (which Steam does not kill) runs
               finish-session once rclone has exited: repair, merge, profile build, saves backup

The idea is the one lazy-loading systems use for container start-up (profile the start-up read set, fetch it first).
Everything here is read-only with respect to the game; the only things written are the log and profile files.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

BLOCK = 4 * 1024 * 1024
MB1 = 1024 * 1024


# ---- sparse-file extents ---------------------------------------------------------------------------
def extents(path):
    """[(offset, length)] of the data present in a sparse file (what rclone has downloaded)."""
    out = []
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return out
    try:
        size = os.fstat(fd).st_size
        pos = 0
        while pos < size:
            try:
                data = os.lseek(fd, pos, os.SEEK_DATA)
            except OSError:
                break                                   # ENXIO: no more data
            hole = os.lseek(fd, data, os.SEEK_HOLE)
            out.append((data, hole - data))
            pos = hole
    finally:
        os.close(fd)
    return out


def merge(ranges):
    """Merge overlapping or touching (offset, length) ranges."""
    out = []
    for off, n in sorted(ranges):
        if out and off <= out[-1][0] + out[-1][1]:
            end = max(out[-1][0] + out[-1][1], off + n)
            out[-1] = (out[-1][0], end - out[-1][0])
        else:
            out.append((off, n))
    return out


def subtract(new, old):
    """Parts of `new` that are not covered by `old` (both lists of (offset, length))."""
    old = merge(old)
    result = []
    for off, n in merge(new):
        cur, end = off, off + n
        for o_off, o_n in old:
            o_end = o_off + o_n
            if o_end <= cur:
                continue
            if o_off >= end:
                break
            if o_off > cur:
                result.append((cur, o_off - cur))
            cur = max(cur, o_end)
            if cur >= end:
                break
        if cur < end:
            result.append((cur, end - cur))
    return result


def find_root(cache_dir, folder):
    """The directory holding the cached data files of one mounted RemoteStorage folder:
    <cache>/vfs/<remote name{id}>/<folder>. The remote's directory name contains a random id, so look it up."""
    base = os.path.join(cache_dir, "vfs")
    try:
        remotes = sorted(os.listdir(base))
    except OSError:
        return None
    for r in remotes:
        cand = os.path.join(base, r, folder)
        if os.path.isdir(cand):
            return cand
    return None


def snapshot(root, previous=None):
    """{relative path: (allocated blocks, [extents])}. Files whose allocated size did not change since
    `previous` are not re-read (their extents are reused), so frequent sampling stays cheap."""
    previous = previous or {}
    snap = {}
    for dp, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dp, f)
            try:
                blocks = os.stat(p).st_blocks
            except OSError:
                continue
            if blocks == 0:
                continue
            rel = os.path.relpath(p, root)
            if rel in previous and previous[rel][0] == blocks:
                snap[rel] = previous[rel]
            else:
                snap[rel] = (blocks, extents(p))
    return snap


# ---- record ----------------------------------------------------------------------------------------
def sample_once(root, previous, t0, log_f, now=None):
    """Take one sample; append the ranges that are new since `previous` to the log. Returns the new snapshot."""
    now = time.time() if now is None else now
    snap = snapshot(root, previous)
    for rel, (_blocks, ext) in snap.items():
        old = previous.get(rel, (0, []))[1]
        for off, n in subtract(ext, old):
            log_f.write(json.dumps({"t": round(now - t0, 1), "f": rel, "o": off, "n": n}) + "\n")
    log_f.flush()
    return snap


def record(root, log_path, interval=10.0, stop_file=None, baseline=False):
    """Log the byte ranges that appear in rclone's cache while the game runs. With baseline=True, what is ALREADY cached
    when recording starts (an earlier session, a pre-mount fill, a prestage) is taken as the starting point and not
    logged: otherwise every cached byte would look like a read at second 0 and bloat a growing profile."""
    t0 = time.time()
    previous = snapshot(root) if baseline and os.path.isdir(root) else {}
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "w") as log_f:
        while not (stop_file and os.path.exists(stop_file)):
            if os.path.isdir(root):
                previous = sample_once(root, previous, t0, log_f)
            time.sleep(interval)


# ---- build -----------------------------------------------------------------------------------------
def build(log_path, out_path, max_seconds=0, max_bytes=0, folder=""):
    entries = []
    with open(log_path) as f:
        for line in f:
            try:
                e = json.loads(line)
                entries.append((float(e["t"]), e["f"], int(e["o"]), int(e["n"])))
            except (ValueError, KeyError):
                continue
    entries.sort()
    ranges, total = [], 0
    for t, f, o, n in entries:
        if max_seconds and t > max_seconds:
            break
        if max_bytes and total + n > max_bytes:
            n = max_bytes - total
            if n <= 0:
                break
        ranges.append({"t": t, "f": f, "o": o, "n": n})
        total += n
    profile = {"version": 1, "folder": folder, "total_bytes": total, "ranges": ranges}
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(profile, f)
    os.replace(tmp, out_path)
    return profile


# ---- growing profiles ------------------------------------------------------------------------------
STABLE_ADDED_BYTES = 64 * MB1          # a run that adds less than this means the start-up profile has converged


def merge_profile(profile_path, log_path, window=1200.0, max_bytes=12 * 1024 ** 3, folder=""):
    """Grow a profile with one run's recording. Ranges already in the profile keep their place (first seen wins); the
    run adds only bytes the profile did not cover yet, at the time this run first needed them. Ranges read within
    `window` seconds go to the start-up part (`ranges`, used by the pre-mount fill) until `max_bytes`; the rest go to
    `later` (used by the queue-time prestage). Returns the number of bytes this run added."""
    try:
        with open(profile_path) as f:
            prof = json.load(f)
    except (OSError, ValueError):
        prof = {"version": 1, "folder": folder, "ranges": [], "later": []}
    old = [dict(r) for r in prof.get("ranges", [])] + [dict(r) for r in prof.get("later", [])]
    covered = {}
    for r in old:
        covered.setdefault(r["f"], []).append((int(r["o"]), int(r["n"])))
    entries = []
    try:
        with open(log_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    entries.append((float(e["t"]), e["f"], int(e["o"]), int(e["n"])))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    entries.sort()
    added = 0
    for t, rel, o, n in entries:
        for off, ln in subtract([(o, n)], covered.get(rel, [])):
            old.append({"t": t, "f": rel, "o": off, "n": ln})
            covered.setdefault(rel, []).append((off, ln))
            added += ln
    old.sort(key=lambda r: (float(r["t"]), r["f"], int(r["o"])))
    start, later, total = [], [], 0
    for r in old:
        if float(r["t"]) <= window and total + int(r["n"]) <= max_bytes:
            start.append(r)
            total += int(r["n"])
        else:
            later.append(r)
    runs = int(prof.get("runs", 1 if prof.get("ranges") else 0)) + 1
    prof.update({"version": 1, "folder": prof.get("folder") or folder, "ranges": start, "later": later,
                 "total_bytes": total, "later_bytes": sum(int(r["n"]) for r in later), "runs": runs,
                 "last_added_bytes": added, "stable": runs >= 2 and added < STABLE_ADDED_BYTES})
    prof.pop("synthetic", None)
    os.makedirs(os.path.dirname(profile_path) or ".", exist_ok=True)
    tmp = profile_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prof, f)
    os.replace(tmp, profile_path)
    return added


def verify_profile_fingerprint(profile, actual_files):
    """Verify stored profile fingerprint (file sizes) against actual release files.

    Refuses replay if any file size has changed (e.g. game patch).
    """
    fingerprint = profile.get("fingerprint")
    if not fingerprint:
        return {"valid": True, "mismatches": []}

    mismatches = []
    for rel_path, expected_size in fingerprint.items():
        if rel_path in actual_files:
            actual_size = actual_files[rel_path]
            if actual_size != expected_size:
                mismatches.append({
                    "file": rel_path,
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                })
    return {
        "valid": len(mismatches) == 0,
        "mismatches": mismatches,
    }


def merge_run_frequency_weighted(profile, entries):
    """Merge new trace entries into profile with hit counts and required/likely tiers."""
    runs = profile.get("runs", 1) + 1
    existing_ranges = [dict(r) for r in profile.get("ranges", [])]

    # Track which ranges were hit in this run
    for t, rel, o, n in entries:
        for r in existing_ranges:
            if r["f"] == rel and not (o + n <= r["o"] or o >= r["o"] + r["n"]):
                r["hit_count"] = r.get("hit_count", 1) + 1
                r["t"] = min(r.get("t", t), t)

    # Update tiers: seen in 100% of runs -> required, else likely
    for r in existing_ranges:
        count = r.get("hit_count", 1)
        r["tier"] = "required" if count >= runs else "likely"

    out = dict(profile)
    out["runs"] = runs
    out["ranges"] = existing_ranges
    return out


# ---- prefetch --------------------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, mbps):
        self.rate = mbps * 1024 * 1024
        self.lock = threading.Lock()
        self.next_free = time.monotonic()

    def take(self, nbytes):
        if self.rate <= 0:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_free)
            self.next_free = start + nbytes / self.rate
            wait = start - now
        if wait > 0:
            time.sleep(wait)


def missing_ranges(profile, cache_root):
    """The parts of the profile's ranges that are NOT in the cache yet, in profile order. Reading a range that is
    cached still makes rclone read ahead past its end into uncached space (measured: replaying a fully cached
    5.3 GB profile downloaded 1.5 GB of data nobody asked for), so cached ranges must not be read at all."""
    out, have = [], {}
    for r in profile.get("ranges", []):
        f = r["f"]
        if f not in have:
            have[f] = merge(extents(os.path.join(cache_root, f))) if cache_root else []
        for off, n in subtract([(r["o"], r["n"])], have[f]):
            out.append({"t": r.get("t", 0), "f": f, "o": off, "n": n})
    return out


_READ_RE = re.compile(r"^(\d{4}/\d\d/\d\d) (\d\d:\d\d:\d\d(?:\.\d+)?) DEBUG : (.+?)\(0x[0-9a-f]+\): _readAt: size=(\d+), off=(\d+)")


def parse_demand_log(path, bucket=10.0, gap=MB1):
    """The game's TRUE demand curve from an rclone DEBUG log (DECKSTREAM_DEMAND_LOG=1): every '_readAt' line is a read
    the game made through the mount. Returns ranges {t, f, o, n} of bytes in the order the game FIRST asked for them,
    t in seconds after the first read. Unlike a profile's t (when bytes arrived in the cache during a cold, line-bound
    run), these times come from a run with the data already local, so they show how fast the game really wants it.
    Reads within `bucket` seconds of each other and `gap` bytes apart are merged to keep the result small."""
    import datetime
    seen, pieces, t0 = {}, [], None
    with open(path, errors="replace") as f:
        for line in f:
            m = _READ_RE.match(line)
            if not m:
                continue
            ts = datetime.datetime.strptime(m.group(1) + " " + m.group(2).split(".")[0], "%Y/%m/%d %H:%M:%S").timestamp()
            if "." in m.group(2):
                ts += float("0." + m.group(2).split(".")[1])
            t0 = ts if t0 is None else t0
            fname, n, off = m.group(3), int(m.group(4)), int(m.group(5))
            if n <= 0:
                continue
            new = subtract([(off, n)], seen.get(fname, []))
            if new:
                seen[fname] = merge(seen.get(fname, []) + new)
                pieces.extend({"t": round(ts - t0, 3), "f": fname, "o": o, "n": k} for o, k in new)
    out = []
    for p in pieces:                      # compact: same file, same time bucket, close together
        q = out[-1] if out else None
        if q and q["f"] == p["f"] and p["t"] - q["t"] < bucket and 0 <= p["o"] - (q["o"] + q["n"]) <= gap:
            q["n"] = p["o"] + p["n"] - q["o"]
        else:
            out.append(dict(p))
    return out


def retime_profile(prof, demand, window=1200.0):
    """Reorder a profile's start-up set by a measured demand curve (parse_demand_log of a DECKSTREAM_DEMAND_LOG=1 run).
    Profile t are cache-arrival times; ranges recorded before the recorder had a baseline all got t=0 and were cut to the
    start-up cap in file-name order (Cyberpunk 18 Oct: 742 misses before the title on t=0 bytes left in `later`).
    User rule (18 Oct): this may change only the ORDER, never the size: the start-up set keeps its byte total. It is
    the bytes the game read within `window` seconds, in that order, then the old ranges (old start-up set first) until
    the old total is reached; the rest stays in `later`. Nothing is dropped. Returns the new profile."""
    size = int(prof.get("total_bytes") or sum(int(r["n"]) for r in prof.get("ranges", [])))
    covered, first, late, rest = {}, [], [], []
    for r in demand:
        r = {"t": float(r["t"]), "f": r["f"], "o": int(r["o"]), "n": int(r["n"])}
        covered.setdefault(r["f"], []).append((r["o"], r["n"]))
        (first if r["t"] <= window else late).append(r)
    for r in list(prof.get("ranges", [])) + list(prof.get("later", [])):
        for off, n in subtract([(int(r["o"]), int(r["n"]))], covered.get(r["f"], [])):
            rest.append({"t": window + 1, "f": r["f"], "o": off, "n": n})
    start, later, total = [], [], 0
    for r in first + rest + late:
        room = size - total
        if room <= 0:
            later.append(r)
        elif r["n"] <= room:
            start.append(r); total += r["n"]
        else:
            start.append(dict(r, n=room)); later.append(dict(r, o=r["o"] + room, n=r["n"] - room)); total = size
    out = dict(prof, ranges=start, later=later, total_bytes=total, later_bytes=sum(r["n"] for r in later),
               retimed=len(demand))
    out.pop("synthetic", None)
    return out

def early_fill_bytes(ranges, rate_mbps=15.0, margin=0.10, floor_mb=300, lag=0.0):
    """How much to fill before launching so the in-session prefetch (same first-need order, `rate_mbps` effective
    through the mount) never falls behind the game: P = max over t of [D(t) - R*t], plus `margin` and `floor_mb`.
    D(t) = bytes first needed by time t (shifted `lag` s earlier when t are coarse samples). Returns (P, total, t_tight)."""
    rs = sorted(ranges, key=lambda r: r.get("t", 0))
    total, need, tight = 0, 0.0, 0.0
    rate = rate_mbps * 1e6
    for r in rs:
        total += r["n"]
        t = max(0.0, r.get("t", 0) - lag)
        if total - rate * t > need:
            need, tight = total - rate * t, t
    return int(min(total, need * (1 + margin) + floor_mb * 1e6)), total, tight


def coalesce(ranges, gap=4 * MB1, split=256 * MB1):
    """Merge a profile's ranges into few LARGE sequential reads. The recorder logs the ranges that appeared in each
    10 s sample, so one contiguous region the game read over a minute is stored as many adjacent slices; replaying
    them separately with several workers made rclone fetch overlapping chunks (measured: 2.5x more bytes downloaded
    than needed). Touching ranges, or ranges at most `gap` apart, per file become one range; ranges longer than
    `split` are cut so that several workers can still share them. The result is ordered by the earliest time seen."""
    by_file = {}
    for r in ranges:
        by_file.setdefault(r["f"], []).append((r["o"], r["n"], r.get("t", 0)))
    out = []
    for f, rs in by_file.items():
        rs.sort()
        cur = None
        for o, n, t in rs:
            if cur and o <= cur[0] + cur[1] + gap:
                end = max(cur[0] + cur[1], o + n)
                cur = (cur[0], end - cur[0], min(cur[2], t))
            else:
                if cur:
                    out.append((f,) + cur)
                cur = (o, n, t)
        if cur:
            out.append((f,) + cur)
    pieces = []
    for f, o, n, t in out:
        pos = o
        while pos < o + n:
            step = min(split, o + n - pos)
            pieces.append({"t": t, "f": f, "o": pos, "n": step})
            pos += step
    pieces.sort(key=lambda r: (r["t"], r["f"], r["o"]))
    return pieces


def edge_ranges(mount_dir, min_size=1024 ** 3, head=MB1, tail=MB1, max_files=40, cache_root=None):
    """The first and last `head`/`tail` bytes of the biggest files (>= min_size) under the mount. Archive formats
    (UE .pak, REDengine .archive, ...) keep their header and index at the ends of the file, and a game always reads
    them first. Parts already in the cache are left out."""
    big = []
    for dp, _dirs, files in os.walk(mount_dir):
        for f in files:
            path = os.path.join(dp, f)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size >= min_size:
                big.append((size, os.path.relpath(path, mount_dir)))
    big.sort(reverse=True)
    out = []
    for size, rel in big[:max_files]:
        want = [(0, min(head, size))]
        if size > head:
            want.append((max(head, size - tail), min(tail, size - head)))
        have = merge(extents(os.path.join(cache_root, rel))) if cache_root else []
        for off, n in subtract(want, have):
            out.append({"t": 0, "f": rel, "o": off, "n": n})
    return out


def prefetch(mount_dir, profile_path, workers=6, mbps=0.0, max_bytes=0, stop_file=None, log=None, cache_root=None,
             merge_slices=True, include_later=False):
    """Read the profile's ranges through the mount, in profile order, with `workers` in parallel.
    With `cache_root` (the rclone cache folder of this game) ranges that are already cached are skipped.
    With `include_later` the later part follows (first-need order): for a game whose profile is bigger than the
    pre-mount fill's cap, this reads ahead what the game needed later in earlier runs."""
    with open(profile_path) as f:
        profile = json.load(f)
    if profile.get("fingerprint") and os.path.isdir(mount_dir):
        actual_sizes = {}
        for rel in profile["fingerprint"]:
            fpath = os.path.join(mount_dir, rel)
            if os.path.isfile(fpath):
                try:
                    actual_sizes[rel] = os.path.getsize(fpath)
                except OSError:
                    pass
        res = verify_profile_fingerprint(profile, actual_sizes)
        if not res["valid"]:
            msg = f"[!] Refusing prefetch: profile fingerprint mismatch (game patched: {res['mismatches']})"
            print(msg, flush=True)
            if log:
                try:
                    with open(log, "a") as lf:
                        lf.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
                except OSError:
                    pass
            return {"bytes": 0, "ranges": 0, "skipped": 0, "refused": True}
    if include_later and profile.get("later"):
        profile = dict(profile, ranges=list(profile.get("ranges", [])) + list(profile["later"]))
    todo = missing_ranges(profile, cache_root) if cache_root else profile.get("ranges", [])
    if merge_slices:
        todo = coalesce(todo)
    return run_prefetch(mount_dir, todo, workers, mbps, max_bytes, stop_file, log)


def run_prefetch(mount_dir, todo, workers=6, mbps=0.0, max_bytes=0, stop_file=None, log=None):
    profile = {"total_bytes": sum(r["n"] for r in todo)}
    limiter = RateLimiter(mbps)
    stats = {"bytes": 0, "ranges": 0, "skipped": 0}
    lock = threading.Lock()
    stop = threading.Event()

    def say(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        if log:
            try:
                with open(log, "a") as lf:
                    lf.write(line + "\n")
            except OSError:
                pass

    def should_stop():
        if stop.is_set():
            return True
        if (stop_file and os.path.exists(stop_file)) or not os.path.isdir(mount_dir):
            stop.set()
        return stop.is_set()

    def one(r):
        if should_stop():
            return
        path = os.path.join(mount_dir, r["f"])
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            with lock:
                stats["skipped"] += 1
            return
        try:
            pos, end = r["o"], r["o"] + r["n"]
            while pos < end and not should_stop():
                with lock:
                    if max_bytes and stats["bytes"] >= max_bytes:
                        stop.set()
                        return
                want = min(BLOCK, end - pos)
                limiter.take(want)
                chunk = os.pread(fd, want, pos)
                if not chunk:
                    break
                pos += len(chunk)
                with lock:
                    stats["bytes"] += len(chunk)
        except OSError:
            with lock:
                stats["skipped"] += 1
            return
        finally:
            os.close(fd)
        with lock:
            stats["ranges"] += 1

    say(f"prefetching {len(todo)} ranges, {profile.get('total_bytes', 0) / 1e9:.2f} GB, {workers} workers")
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        list(pool.map(one, todo))
    say(f"done: {stats['bytes'] / 1e9:.2f} GB in {stats['ranges']} ranges, {stats['skipped']} skipped"
        + (" (stopped early)" if stop.is_set() else ""))
    return stats


# ---- repair rclone's cache metadata -----------------------------------------------------------------
def repair_meta(cache_dir, now=None):
    """rclone records which byte ranges of a cached file it holds in <cache>/vfsMeta/..., but it writes that only when
    the file is closed. Steam kills the whole process tree when a game exits, so the metadata is often left empty
    while the data is on disk, and at the next mount rclone treats the file as "nothing cached" and DISCARDS the
    data (measured: a 5.7 GB cached pak shrank to 0.5 GB on the first read after a remount). The sparse data file
    knows exactly what it holds, so put that back into the metadata before rclone looks at it.
    Only files that already have metadata (with the right size) are touched. Returns (files repaired, bytes restored)."""
    now = time.time() if now is None else now
    data_root = os.path.join(cache_dir, "vfs")
    meta_root = os.path.join(cache_dir, "vfsMeta")
    repaired, restored = 0, 0
    for dp, _dirs, files in os.walk(data_root):
        for f in files:
            dpath = os.path.join(dp, f)
            mpath = os.path.join(meta_root, os.path.relpath(dpath, data_root))
            try:
                with open(mpath) as mf:
                    meta = json.load(mf)
                size = os.stat(dpath).st_size
            except (OSError, ValueError):
                continue
            if meta.get("Size") != size:
                continue                                   # the file changed: leave it to rclone
            have = merge(extents(dpath))
            known = merge([(r["Pos"], r["Size"]) for r in (meta.get("Rs") or [])])
            have_bytes = sum(n for _, n in have)
            if have_bytes <= sum(n for _, n in known):
                continue                                   # the metadata already knows at least as much
            meta["Rs"] = [{"Pos": o, "Size": n} for o, n in have]
            meta["Dirty"] = False
            tmp = mpath + ".tmp"
            with open(tmp, "w") as mf:
                json.dump(meta, mf)
            os.replace(tmp, mpath)
            repaired += 1
            restored += have_bytes - sum(n for _, n in known)
    return repaired, restored


# ---- cache folders (one per rclone remote name) ------------------------------------------------------
import fcntl


def _paths(cache_dir, ns, folder, rel=""):
    return (os.path.join(cache_dir, "vfs", ns, folder, rel), os.path.join(cache_dir, "vfsMeta", ns, folder, rel))


def _other_namespaces(cache_dir, current):
    try:
        return sorted(n for n in os.listdir(os.path.join(cache_dir, "vfs"))
                      if n != current and os.path.isdir(os.path.join(cache_dir, "vfs", n)))
    except OSError:
        return []


def _files(cache_dir, ns, folder):
    root = _paths(cache_dir, ns, folder)[0]
    for dp, _d, fs in os.walk(root):
        for f in fs:
            yield os.path.relpath(os.path.join(dp, f), root)


def _data_bytes(path):
    return sum(n for _, n in extents(path))


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)


class CacheLock:
    """Only one adopt/merge at a time (a merge started after a session can still be running at the next launch)."""

    def __init__(self, cache_dir, wait=0.0):
        os.makedirs(cache_dir, exist_ok=True)
        self.path = os.path.join(cache_dir, ".adopt.lock")
        self.wait = wait
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.5)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def adopt_cache(cache_dir, current, folder, wait=60.0):
    """rclone puts each remote's cache under vfs/<remote name>/ and the name of an environment-defined remote carries
    a hash of its settings, so earlier runs left their data in other folders. Move, for every file the current
    folder does not have yet, the copy holding the most data (a rename: instant). Files that exist in several old
    folders keep their other copies for merge_cache. Run this after the mount is up and before the game starts.
    Returns (files adopted, bytes adopted)."""
    adopted, nbytes = 0, 0
    with CacheLock(cache_dir, wait) as got:
        if not got:
            return 0, 0
        best = {}
        for ns in _other_namespaces(cache_dir, current):
            for rel in _files(cache_dir, ns, folder):
                d, m = _paths(cache_dir, ns, folder, rel)
                if not os.path.isfile(m):
                    continue
                b = _data_bytes(d)
                if rel not in best or b > best[rel][0]:
                    best[rel] = (b, ns)
        for rel, (b, ns) in best.items():
            dd, dm = _paths(cache_dir, current, folder, rel)
            if os.path.exists(dd):
                continue
            sd, sm = _paths(cache_dir, ns, folder, rel)
            try:
                _move(sd, dd)
                _move(sm, dm)
            except OSError:
                continue
            adopted += 1
            nbytes += b
    return adopted, nbytes


def _copy_range(src_fd, dst_fd, off, n):
    pos, end = off, off + n
    while pos < end:
        step = min(8 * 1024 * 1024, end - pos)
        chunk = os.pread(src_fd, step, pos)
        if not chunk:
            break
        os.pwrite(dst_fd, chunk, pos)
        pos += len(chunk)


def merge_cache(cache_dir, current, folder, wait=0.0):
    """Offline (rclone not running on this cache): copy into the current folder every range that only an older folder
    holds, then delete the older copies. Only files of the same size are merged. Returns (files merged, bytes copied)."""
    files, copied = 0, 0
    with CacheLock(cache_dir, wait) as got:
        if not got:
            return 0, 0
        for ns in _other_namespaces(cache_dir, current):
            for rel in list(_files(cache_dir, ns, folder)):
                sd, sm = _paths(cache_dir, ns, folder, rel)
                dd, dm = _paths(cache_dir, current, folder, rel)
                try:
                    if not os.path.exists(dd):
                        if os.path.isfile(sm):
                            _move(sd, dd)
                            _move(sm, dm)
                            files += 1
                        continue
                    if os.stat(sd).st_size != os.stat(dd).st_size:
                        continue
                    todo = subtract(extents(sd), extents(dd))
                    sfd = os.open(sd, os.O_RDONLY)
                    dfd = os.open(dd, os.O_RDWR)
                    try:
                        for off, n in todo:
                            _copy_range(sfd, dfd, off, n)
                            copied += n
                    finally:
                        os.close(sfd)
                        os.close(dfd)
                    os.unlink(sd)
                    if os.path.isfile(sm):
                        os.unlink(sm)
                    files += 1
                except OSError:
                    continue
        for ns in _other_namespaces(cache_dir, current):                      # remove folders that are now empty
            for sub in ("vfs", "vfsMeta"):
                root = os.path.join(cache_dir, sub, ns)
                for dp, _d, _f in os.walk(root, topdown=False):          # deepest folders first
                    try:
                        os.rmdir(dp)
                    except OSError:
                        pass
    return files, copied


# ---- stream session markers --------------------------------------------------------------------------
def _pid_is(pid, name):
    try:
        with open(f"/proc/{int(pid)}/comm") as f:
            return f.read().strip() == name
    except (OSError, ValueError):
        return False


def boot_time(stat="/proc/stat"):
    """When this machine booted (epoch seconds), 0 if unknown."""
    try:
        with open(stat) as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def merge_pending(profile, plog, window=1200.0, folder=""):
    """Merge a session's recording into its game's profile once, then set the log aside (log.merged). Called by
    finish-session and by the stream runner at Play, before the new session's recorder truncates the same log: a session
    that ended badly (battery death, crash, quit mid-load) and was not finished yet would otherwise lose its reads.
    Returns the bytes added, or None when there was nothing to merge."""
    if not (profile and plog and os.path.exists(plog) and os.path.getsize(plog) > 0):
        return None
    added = merge_profile(profile, plog, window=window,
                          max_bytes=int(float(os.environ.get("DECKSTREAM_PROFILE_MAX_GB", "12")) * 1024 ** 3), folder=folder)
    try:
        os.replace(plog, plog + ".merged")
    except OSError:
        pass
    return added


def session_start(marker, **fields):
    fields.update({"started": time.time(), "done": False})
    os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
    tmp = marker + ".tmp"
    with open(tmp, "w") as f:
        json.dump(fields, f)
    os.replace(tmp, marker)


def session_done(marker):
    try:
        with open(marker) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return False
    m["done"] = True
    m["ended"] = time.time()
    tmp = marker + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, marker)
    return True


def finish_session(marker, saves_py=None, run=None):
    """Everything that must happen after a stream session, in order, once rclone has exited. Steam ends the runner
    script's process tree right after the game exits, so the runner cannot do this itself; the daemon calls this.
    Returns "finished", "waiting" (rclone still running), "busy" (another finish is running) or "none"."""
    import subprocess
    run = run or subprocess.run
    try:
        mfd = os.open(marker, os.O_RDWR)
    except OSError:
        return "none"
    try:
        try:
            fcntl.flock(mfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "busy"
        with open(marker) as f:
            m = json.load(f)
        # A session from before the last boot (battery died mid-game) has no live rclone: its pid may now be another one.
        if m.get("rclone_pid") and float(m.get("started", 0)) >= boot_time() and _pid_is(m["rclone_pid"], "rclone"):
            return "waiting"
        # After a quick relaunch a NEW rclone may already be using this cache; repairing or merging it, or backing up saves
        # while the game runs again, would be wrong. Wait for a later pass.
        if rclone_uses_cache(m["cache_dir"]):
            return "waiting"
        cache_dir, ns, folder = m["cache_dir"], m.get("ns", ""), m.get("folder", "")
        # Grow the start-up profile with every run, not only the first cold one: a run quit from the Steam menu never
        # reaches session-done but its reads are just as real. Merged FIRST, before the cache repair: a repair or merge
        # that fails must not cost the profile what a crashed or quit-mid-load session recorded.
        merge_pending(m.get("profile"), m.get("profile_log"), window=float(m.get("profile_seconds", 1200)), folder=folder)
        repair_meta(cache_dir)
        if ns:
            merge_cache(cache_dir, ns, folder, wait=300)
            repair_meta(cache_dir)
        # Back up saves after EVERY session. Steam ends the runner the moment the game exits, so "done" is almost never
        # set; requiring it meant post-session backups silently never ran (seen on the Deck 2026-10-11, Sifu and FF7).
        # By now rclone has exited, so the game has too.
        if saves_py and m.get("game_id") and os.path.exists(saves_py):
            env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
            try:
                run([sys.executable, saves_py, "backup", m["game_id"]], capture_output=True, timeout=900, env=env)
            except Exception:                                  # a failed backup must not keep the marker alive forever
                pass
        os.unlink(marker)
        return "finished"
    finally:
        os.close(mfd)


# ---- exact fill: fetch precisely the profile's bytes, bypassing rclone's chunking ----------------------
import http.client
import urllib.parse


def rclone_uses_cache(cache_dir):
    """True if a running rclone was started with this cache dir (writing into its cache behind its back is unsafe)."""
    want = os.path.abspath(cache_dir)
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if args and os.path.basename(args[0].decode("utf-8", "replace")) == "rclone" and any(want.encode() in a for a in args):
            return True
    return False


class BeyondEndError(Exception):
    """HTTP 416: the requested range starts at or after the end of the file (padding past EOF); nothing to fetch."""


class FatalFetchError(Exception):
    """An answer that retrying cannot fix (wrong key, file not found): fail at once instead of backing off."""


class RangeFetcher:
    """One persistent HTTP connection per worker; retries, backs off on 429/5xx, follows one redirect."""

    def __init__(self, base_url, user, password, agent="DeckStream/1.0", retries=5):
        self.u = urllib.parse.urlparse(base_url)
        self.auth = "Basic " + __import__("base64").b64encode(f"{user}:{password}".encode()).decode() if user else None
        self.agent, self.retries, self.conn = agent, retries, None

    def _connect(self, scheme, host, port):
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        return cls(host, port, timeout=60)

    def get(self, path, start, length):
        """Returns (bytes, total_size_or_None). Raises IOError after the retries."""
        last = None
        for attempt in range(self.retries):
            try:
                if self.conn is None:
                    self.conn = self._connect(self.u.scheme, self.u.hostname, self.u.port)
                headers = {"Range": f"bytes={start}-{start + length - 1}", "User-Agent": self.agent}
                if self.auth:
                    headers["Authorization"] = self.auth
                self.conn.request("GET", path, headers=headers)
                r = self.conn.getresponse()
                if r.status in (301, 302, 307, 308) and r.getheader("Location"):
                    loc = urllib.parse.urlparse(r.getheader("Location")); r.read()
                    c2 = self._connect(loc.scheme, loc.hostname, loc.port)
                    c2.request("GET", loc.path + ("?" + loc.query if loc.query else ""),
                               headers={"Range": headers["Range"], "User-Agent": self.agent})
                    r = c2.getresponse(); self.conn.close(); self.conn = c2
                body = r.read()
                if r.status == 206:
                    m = re.match(r"bytes \d+-\d+/(\d+)", r.getheader("Content-Range") or "")
                    if len(body) != length and not (m and start + len(body) == int(m.group(1))):
                        raise IOError(f"short read: wanted {length}, got {len(body)}")
                    return body, int(m.group(1)) if m else None
                if r.status in (429, 500, 502, 503, 504):
                    last = IOError(f"HTTP {r.status}")
                    time.sleep(min(2 ** attempt, 20) * (2 if r.status == 429 else 1))
                    self.conn.close(); self.conn = None
                    continue
                if r.status == 416:
                    raise BeyondEndError(path)
                raise FatalFetchError(f"HTTP {r.status} for {path}")
            except (FatalFetchError, BeyondEndError):
                raise
            except (OSError, http.client.HTTPException) as e:
                last = e
                try:
                    self.conn and self.conn.close()
                except OSError:
                    pass
                self.conn = None
                time.sleep(min(2 ** attempt, 10))
        raise IOError(f"giving up on {path} @ {start}: {last}")


def _storage_key(conf):
    key = os.environ.get("STORAGE_KEY", "")
    if not key:
        try:
            for line in open(conf):
                if line.startswith("STORAGE_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass
    return key


def list_remote_files(base_url, user, password, folder, agent="DeckStream/1.0", max_dirs=2000, timeout=60):
    """{relative path: size} of every file under `folder` on the WebDAV server (PROPFIND, one level per request)."""
    import base64
    import xml.etree.ElementTree as ET
    u = urllib.parse.urlparse(base_url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    root_path = "/" + urllib.parse.quote(folder.strip("/"), safe="/") + "/"
    out, queue, seen = {}, [root_path], set()
    conn = cls(u.hostname, u.port, timeout=timeout)
    try:
        while queue and len(seen) < max_dirs:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            conn.request("PROPFIND", path, body=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                         b'<d:getcontentlength/><d:resourcetype/></d:prop></d:propfind>',
                         headers={"Depth": "1", "Authorization": auth, "User-Agent": agent,
                                  "Content-Type": "application/xml"})
            r = conn.getresponse()
            if r.status in (401, 403):
                raise FatalFetchError(f"WebDAV refused the listing (HTTP {r.status})")
            body = r.read()
            if r.status != 207:
                raise IOError(f"WebDAV listing of {path} failed (HTTP {r.status})")
            for resp in ET.fromstring(body).findall("{DAV:}response"):
                href = urllib.parse.urlparse(resp.findtext("{DAV:}href") or "").path
                if not href.startswith(root_path) or href.rstrip("/") == path.rstrip("/"):
                    continue
                is_dir = resp.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
                if is_dir:
                    queue.append(href if href.endswith("/") else href + "/")
                    continue
                size = resp.findtext(".//{DAV:}getcontentlength")
                rel = urllib.parse.unquote(href[len(root_path):])
                if rel and size and size.isdigit():
                    out[rel] = int(size)
    finally:
        conn.close()
    return out


# Other-language files a rip carries next to the English ones (measured 24 Oct on RemoteStorage listings: Cyberpunk GOG
# lang_ru_voice.archive 5.4 GB, FF7 Remake FR/DE movies 7.0 GB, BG3 "Language Pack" folders). Only these exact engine
# layouts, and only when the English twin is there, so the game always has English. Videos with no language suffix and
# any layout not listed here are never touched.
_LANG_RED = re.compile(r"^(?P<dir>(?:.*/)?)lang_(?P<lang>[a-z]{2}(?:-[a-z]{2})?)_(?P<kind>voice|text)\.archive$")
_LANG_EMOV = re.compile(r"^(?P<stem>.+)_(?P<lang>de|fr|it|es|jp|ja)(?P<pc>_pc)?\.emov$")
_LANG_PACK = re.compile(r"^(?P<dir>(?:.*/)?)language pack/(?P<lang>[^/]+)/")


def lang_skips(files):
    """Other-language files of `files` (rel path -> size) that a rip install or the overnight whole warm can leave out:
    REDengine lang_<xx>_(voice|text).archive when lang_en_<kind>.archive is in the same folder, Square Enix
    <name>_(DE|FR|IT|ES|JP)[_PC].emov when <name>_US[_PC].emov exists, and "Language Pack/<X>/..." when
    "Language Pack/ENG/" exists. Returns {rel path: size}."""
    low = {f.lower(): f for f in files}
    packs = {m.group("dir") for k in low if (m := _LANG_PACK.match(k)) and m.group("lang") == "eng"}
    out = {}
    for k, f in low.items():
        m = _LANG_RED.match(k)
        if m and m.group("lang") != "en" and f"{m.group('dir')}lang_en_{m.group('kind')}.archive" in low:
            out[f] = files[f]
            continue
        m = _LANG_EMOV.match(k)
        if m and f"{m.group('stem')}_us{m.group('pc') or ''}.emov" in low:
            out[f] = files[f]
            continue
        m = _LANG_PACK.match(k)
        if m and m.group("lang") != "eng" and m.group("dir") in packs:
            out[f] = files[f]
    return out


def synth_profile(files, hot, out_path, folder="", edge=2 * MB1, big=256 * MB1, hot_max=2 * 1024 * MB1, whole_max=0):
    """A STARTER profile for a game that has never been played here (no recorded profile yet): the small start-up files
    the planner picked (executables, libraries, configs) whole, smallest first, then the first and last `edge` bytes of
    every file of `big` bytes or more (pak footers, IoStore tables of contents and archive headers live there, and a
    game reads them before anything else). Same format as a recorded profile, so fill-cache takes it as is."""
    ranges, total = [], 0
    for rel in sorted({h for h in hot if h in files}, key=lambda h: (files[h], h)):
        n = files[rel]
        if n <= 0 or total + n > hot_max:
            continue
        ranges.append({"t": 0.0, "f": rel, "o": 0, "n": n})
        total += n
    for rel, n in sorted(files.items(), key=lambda kv: -kv[1]):
        if n < big:
            continue
        head = min(edge, n)
        ranges.append({"t": 1.0, "f": rel, "o": 0, "n": head})
        tail_start = max(head, n - edge)
        if tail_start < n:
            ranges.append({"t": 1.0, "f": rel, "o": tail_start, "n": n - tail_start})
        total += head + (n - tail_start)
    if whole_max:
        # The whole release, for a game nobody has played here yet: after the start-up files and archive edges, every
        # file in full, smallest first (many small files are data a game reads at once), until `whole_max` bytes.
        # The last file may be cut short; fill-cache bridges and skips what is already requested.
        # Other-language files (lang_skips) are left out: the game reads them only in that language.
        used = 0                      # the budget counts the whole-file bytes only (hot files and edges overlap them)
        skip = lang_skips(files)
        for rel, n in sorted(files.items(), key=lambda kv: (kv[1], kv[0])):
            if rel in skip:
                continue
            room = whole_max - used
            if room <= 0:
                break
            if n <= 0:
                continue
            take = min(n, room)
            ranges.append({"t": 2.0, "f": rel, "o": 0, "n": take})
            used += take
        total += used
    profile = {"version": 1, "folder": folder, "total_bytes": total, "ranges": ranges, "synthetic": True}
    if whole_max:
        profile["whole"] = True
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(profile, f)
    os.replace(tmp, out_path)
    return profile


def cap_ranges_first(ranges, max_bytes):
    """Cut `ranges` (profile order = first-need order) to at most `max_bytes` in total, BEFORE the cache is subtracted.
    Row 55: capping after `missing_ranges` meant "N GiB of NEW bytes", so a launch re-filled the full cap even when the
    warm-up had already cached the first part. Return a new list of {"f","o","n",...} dicts; keep the order; the range
    that crosses the cap is shortened, not dropped."""
    out, total = [], 0
    for r in ranges:
        room = max_bytes - total
        if room <= 0:
            break
        if r["n"] <= 0:
            continue
        out.append(r if r["n"] <= room else dict(r, n=room))
        total += out[-1]["n"]
    return out


def fill_cache(cache_dir, ns, folder, profile_path, base_url, user, password, workers=8, piece=16 * MB1,
               max_bytes=0, stop_file=None, log=None, force=False, include_later=False):
    """Fetch the profile's missing byte ranges (merged, in pieces of `piece`) with direct HTTP range requests and write
    them into rclone's cache for namespace `ns`, creating the sparse files and rclone's metadata for new files.
    Safe only while rclone is NOT using `cache_dir` (refuses otherwise unless force). Resumable: pieces already present
    are skipped. Returns {"bytes": n, "files": n, "failed": n, "refused": bool}."""
    stats = {"bytes": 0, "files": 0, "failed": 0, "refused": False}

    def say(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        if log:
            try:
                with open(log, "a") as lf:
                    lf.write(line + "\n")
            except OSError:
                pass

    if not force and rclone_uses_cache(cache_dir):
        say("refusing to fill: rclone is running on this cache")
        stats["refused"] = True
        return stats
    with open(profile_path) as f:
        profile = json.load(f)
    data_root = os.path.join(cache_dir, "vfs", ns, folder)
    meta_root = os.path.join(cache_dir, "vfsMeta", ns, folder)
    if profile.get("fingerprint") and os.path.isdir(data_root):
        actual_sizes = {}
        for rel in profile["fingerprint"]:
            fpath = os.path.join(data_root, rel)
            if os.path.isfile(fpath):
                try:
                    actual_sizes[rel] = os.path.getsize(fpath)
                except OSError:
                    pass
        res = verify_profile_fingerprint(profile, actual_sizes)
        if not res["valid"]:
            say(f"refusing to fill: profile fingerprint mismatch (game patched: {res['mismatches']})")
            stats["refused"] = True
            return stats
    # rclone reads in 128 KB blocks at arbitrary offsets, and the kernel's read-ahead keeps reading a few blocks past
    # the last one the game asked for. A block past the end of a recorded range misses the cache and makes rclone fetch
    # a whole 64 MB parallel chunk. Measured on the Deck: one 128 KB block of padding still left 49 misses (1.9 GB
    # re-downloaded, all read-ahead just past range ends); pad 1 MB (8 blocks) each side.
    pad = int(os.environ.get("DECKSTREAM_FILL_PAD_KB", "1024")) * 1024
    # The later part is padded too (it used to be added after padding, so every later range ended in a miss).
    if include_later and profile.get("later"):
        profile = dict(profile, ranges=list(profile.get("ranges", [])) + list(profile["later"]))
    padded = []
    for r in profile.get("ranges", []):
        start = max(0, r["o"] - pad)
        end = r["o"] + r["n"] + pad
        try:
            end = min(end, os.path.getsize(os.path.join(data_root, r["f"])))   # a known file size caps the padding
        except OSError:
            pass
        padded.append(dict(r, o=start, n=max(0, end - start)) if end > start else r)
    if max_bytes and os.environ.get("DECKSTREAM_FILL_CAP_FIRST", "1") != "0":    # row 55: ON since the 18 Oct Deck A/B (587 s -> 201 s); =0 turns it off
        padded = cap_ranges_first(padded, max_bytes)
    profile = dict(profile, ranges=padded)
    # what is still missing, per file, merged and cut into pieces (in first-need order, so a byte cap keeps what the
    # game needs first)
    todo = missing_ranges(profile, data_root)
    todo = coalesce(todo, gap=MB1, split=piece)     # exact: only gaps of up to 1 MB are bridged
    if max_bytes:
        cut, total = [], 0
        for r in todo:
            if total + r["n"] > max_bytes:
                break
            cut.append(r); total += r["n"]
        todo = cut
    need = sum(r["n"] for r in todo)
    say(f"exact fill: {len(todo)} pieces, {need / 1e9:.2f} GB, {workers} workers")
    if not todo:
        return stats
    sizes, lock, stop = {}, threading.Lock(), threading.Event()
    files_touched = set()

    def ensure_file(rel, total):
        dpath = os.path.join(data_root, rel)
        mpath = os.path.join(meta_root, rel)
        with lock:
            if rel in sizes:
                return dpath
            os.makedirs(os.path.dirname(dpath), exist_ok=True)
            os.makedirs(os.path.dirname(mpath), exist_ok=True)
            if not os.path.exists(dpath):
                with open(dpath, "wb") as f:
                    f.truncate(total)
            elif os.path.getsize(dpath) != total:
                with open(dpath, "r+b") as f:
                    f.truncate(total)
            if not os.path.exists(mpath):
                now = time.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000+00:00"
                with open(mpath, "w") as f:
                    json.dump({"ModTime": now, "ATime": now, "Size": total, "Rs": [], "Fingerprint": str(total), "Dirty": False}, f)
            sizes[rel] = total
            files_touched.add(rel)
        return dpath

    def work(r):
        if stop.is_set() or (stop_file and os.path.exists(stop_file)):
            return
        fetcher = getattr(tls, "fetcher", None)
        if fetcher is None:
            fetcher = tls.fetcher = RangeFetcher(base_url, user, password)
        path = "/" + urllib.parse.quote(folder + "/" + r["f"], safe="/")
        try:
            try:
                body, total = fetcher.get(path, r["o"], r["n"])
            except BeyondEndError:
                return                                  # padding reached past the end of the file: nothing to fetch
            if total is None:
                raise IOError("server did not report the file size")
            dpath = ensure_file(r["f"], total)
            fd = os.open(dpath, os.O_WRONLY)
            try:
                os.pwrite(fd, body, r["o"])
            finally:
                os.close(fd)
            with lock:
                stats["bytes"] += len(body)
        except (OSError, IOError, FatalFetchError) as e:
            with lock:
                stats["failed"] += 1
            say(f"failed {r['f']} @ {r['o']}: {e}")

    tls = threading.local()
    last = [time.monotonic()]
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futs = [pool.submit(work, r) for r in todo]
        for fu in futs:
            fu.result()
            now = time.monotonic()
            if now - last[0] > 30:
                last[0] = now
                say(f"{stats['bytes'] / 1e9:.2f} of {need / 1e9:.2f} GB")
    stats["files"] = len(files_touched)
    repair_meta(cache_dir)            # record the ranges now present in rclone's metadata
    say(f"done: {stats['bytes'] / 1e9:.2f} GB, {stats['files']} files, {stats['failed']} failed")
    return stats


# ---- CLI -------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--cache-dir", required=True)
    r.add_argument("--folder", required=True)
    r.add_argument("--log", required=True)
    r.add_argument("--interval", type=float, default=10)
    r.add_argument("--stop-file")
    r.add_argument("--baseline", action="store_true", help="ignore what is already cached when recording starts")
    mg = sub.add_parser("merge")
    mg.add_argument("--profile", required=True)
    mg.add_argument("--log", required=True)
    mg.add_argument("--window", type=float, default=1200)
    mg.add_argument("--max-gb", type=float, default=12)
    mg.add_argument("--folder", default="")
    b = sub.add_parser("build")
    b.add_argument("--log", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--folder", default="")
    b.add_argument("--max-seconds", type=float, default=0)
    b.add_argument("--max-gb", type=float, default=0)
    p = sub.add_parser("prefetch")
    p.add_argument("--mount", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--mbps", type=float, default=0)
    p.add_argument("--max-gb", type=float, default=0)
    p.add_argument("--stop-file")
    p.add_argument("--log")
    p.add_argument("--no-coalesce", action="store_true", help="replay the recorded slices one by one (the old behaviour)")
    p.add_argument("--cache-dir", default="", help="skip ranges already in this game's rclone cache")
    p.add_argument("--folder", default="", help="the mounted RemoteStorage folder (to find the cache)")
    p.add_argument("--include-later", action="store_true", help="also read ahead the profile's later part")
    e = sub.add_parser("prefetch-edges")
    e.add_argument("--mount", required=True)
    e.add_argument("--cache-dir", default="")
    e.add_argument("--folder", default="")
    e.add_argument("--min-gb", type=float, default=1.0)
    e.add_argument("--max-files", type=int, default=40)
    e.add_argument("--workers", type=int, default=8)
    e.add_argument("--log")
    dm = sub.add_parser("demand", help="build the true demand curve from an rclone DEBUG log (DECKSTREAM_DEMAND_LOG=1)")
    dm.add_argument("--log", required=True)
    dm.add_argument("--profile", help="store it in this profile as 'demand' (and the resulting early_fill_bytes)")
    dm.add_argument("--out", help="or write it to this file")
    rt = sub.add_parser("retime", help="reorder the start-up set by an rclone DEBUG log (true first-need order; same size)")
    rt.add_argument("--profile", required=True)
    rt.add_argument("--log", required=True)
    rt.add_argument("--window", type=float, default=1200)
    ef = sub.add_parser("early-fill", help="how much to fill before an early launch (uses 'demand' when present)")
    ef.add_argument("--profile", required=True)
    ef.add_argument("--rate-mbps", type=float, default=15.0)
    fc = sub.add_parser("fill-cache")
    fc.add_argument("--cache-dir", required=True)
    fc.add_argument("--ns", required=True, help="the rclone remote name, e.g. tbstream{AbCdE}")
    fc.add_argument("--folder", required=True)
    fc.add_argument("--profile", required=True)
    fc.add_argument("--webdav-url", default="https://webdav.storage.local")
    fc.add_argument("--user", default="storage")
    fc.add_argument("--conf", default=os.path.expanduser("~/.config/deckstream/storage.conf"), help="file holding STORAGE_KEY")
    fc.add_argument("--workers", type=int, default=8)
    fc.add_argument("--piece-mb", type=int, default=16)
    fc.add_argument("--max-gb", type=float, default=0)
    fc.add_argument("--include-later", action="store_true", help="also fetch the profile's later part (prestage)")
    fc.add_argument("--log")
    sp = sub.add_parser("synth-profile")
    sp.add_argument("--folder", required=True)
    sp.add_argument("--hot-file", default="", help="JSON list of start-up files (paths inside the folder)")
    sp.add_argument("--out", required=True)
    sp.add_argument("--edge-mb", type=float, default=2)
    sp.add_argument("--big-mb", type=float, default=256)
    sp.add_argument("--whole-gb", type=float, default=0, help="also add every file whole, smallest first, up to this many GB")
    sp.add_argument("--webdav-url", default="https://webdav.storage.local")
    sp.add_argument("--user", default="storage")
    sp.add_argument("--conf", default=os.path.expanduser("~/.config/deckstream/storage.conf"), help="file holding STORAGE_KEY")
    ls = sub.add_parser("lang-skips", help="read 'size<TAB>path' lines on stdin, print the other-language ones (lang_skips)")
    ls.add_argument("--json-out", default="", help="also write {folder, bytes, files} here")
    ls.add_argument("--folder", default="")
    s = sub.add_parser("show")
    s.add_argument("--profile", required=True)
    m = sub.add_parser("repair-meta")
    m.add_argument("--cache-dir", required=True)
    ss = sub.add_parser("session-start")
    ss.add_argument("--marker", required=True)
    for k in ("game-id", "cache-dir", "ns", "folder", "profile", "profile-log"):
        ss.add_argument("--" + k, default="")
    ss.add_argument("--rclone-pid", type=int, default=0)
    ss.add_argument("--cold", type=int, default=0)
    ss.add_argument("--profile-seconds", type=float, default=600)
    sd = sub.add_parser("session-done")
    sd.add_argument("--marker", required=True)
    mp = sub.add_parser("merge-pending", help="merge a leftover recording into its profile (a session that ended badly)")
    mp.add_argument("--profile", required=True)
    mp.add_argument("--log", required=True)
    mp.add_argument("--window", type=float, default=1200)
    mp.add_argument("--folder", default="")
    fs = sub.add_parser("finish-session")
    fs.add_argument("--marker", required=True)
    fs.add_argument("--saves-py", default=os.path.expanduser("~/.config/deckstream/saves.py"))
    for name in ("adopt-cache", "merge-cache"):
        c = sub.add_parser(name)
        c.add_argument("--cache-dir", required=True)
        c.add_argument("--current", required=True, help="the current rclone remote name, e.g. tbstream{AbCdE}")
        c.add_argument("--folder", required=True)
        c.add_argument("--wait", type=float, default=60.0 if name == "adopt-cache" else 0.0)
    a = ap.parse_args(argv)

    if a.cmd == "record":
        root = None
        while root is None:                                  # the cache folder appears once rclone has read a file
            root = find_root(a.cache_dir, a.folder)
            if root is None:
                if a.stop_file and os.path.exists(a.stop_file):
                    return 0
                time.sleep(a.interval)
        record(root, a.log, a.interval, a.stop_file, baseline=a.baseline)
        return 0
    if a.cmd == "merge":
        added = merge_profile(a.profile, a.log, a.window, int(a.max_gb * 1024 ** 3), a.folder)
        print(f"merged: {added / 1e6:.0f} MB new")
        return 0
    if a.cmd == "build":
        prof = build(a.log, a.out, a.max_seconds, int(a.max_gb * 1024 ** 3), a.folder)
        print(f"profile: {len(prof['ranges'])} ranges, {prof['total_bytes'] / 1e9:.2f} GB -> {a.out}")
        return 0
    if a.cmd == "prefetch":
        if not os.path.isfile(a.profile):
            print(f"no profile at {a.profile}", file=sys.stderr)
            return 2
        root = find_root(a.cache_dir, a.folder) if a.cache_dir and a.folder else None
        prefetch(a.mount, a.profile, a.workers, a.mbps, int(a.max_gb * 1024 ** 3), a.stop_file, a.log,
                 cache_root=root or (os.path.join(a.cache_dir, "__none__") if a.cache_dir else None),
                 merge_slices=not a.no_coalesce, include_later=a.include_later)
        return 0
    if a.cmd == "session-start":
        session_start(a.marker, game_id=a.game_id, cache_dir=a.cache_dir, ns=a.ns, folder=a.folder, profile=a.profile,
                      profile_log=a.profile_log, rclone_pid=a.rclone_pid, cold=bool(a.cold), profile_seconds=a.profile_seconds)
        return 0
    if a.cmd == "session-done":
        return 0 if session_done(a.marker) else 1
    if a.cmd == "merge-pending":
        added = merge_pending(a.profile, a.log, a.window, a.folder)
        if added is not None:
            print(f"merged a leftover recording: {added / 1e6:.0f} MB added")
        return 0
    if a.cmd == "finish-session":
        status = finish_session(a.marker, a.saves_py)
        print(status)
        return {"finished": 0, "none": 0, "waiting": 3, "busy": 4}[status]
    if a.cmd == "adopt-cache":
        n, b = adopt_cache(a.cache_dir, a.current, a.folder, a.wait)
        print(f"adopted {n} cached file(s), {b / 1e6:.0f} MB, from earlier cache folders")
        return 0
    if a.cmd == "merge-cache":
        n, b = merge_cache(a.cache_dir, a.current, a.folder, a.wait)
        print(f"merged {n} file(s), copied {b / 1e6:.0f} MB from earlier cache folders")
        return 0
    if a.cmd == "repair-meta":
        n, b = repair_meta(a.cache_dir)
        print(f"repaired metadata for {n} file(s), {b / 1e6:.0f} MB of cached data restored to rclone's records")
        return 0
    if a.cmd == "prefetch-edges":
        root = find_root(a.cache_dir, a.folder) if a.cache_dir and a.folder else None
        todo = edge_ranges(a.mount, int(a.min_gb * 1024 ** 3), MB1, MB1, a.max_files,
                           root or (os.path.join(a.cache_dir, "__none__") if a.cache_dir else None))
        run_prefetch(a.mount, todo, a.workers, 0.0, 0, None, a.log)
        return 0
    if a.cmd == "demand":
        dem = parse_demand_log(a.log)
        if a.profile:
            prof = json.load(open(a.profile))
            prof["demand"] = dem
            prof["early_fill_bytes"] = early_fill_bytes(dem)[0]
            tmp = a.profile + ".tmp"
            json.dump(prof, open(tmp, "w")); os.replace(tmp, a.profile)
        if a.out:
            json.dump({"demand": dem}, open(a.out, "w"))
        p, total, tight = early_fill_bytes(dem)
        print(f"demand: {len(dem)} ranges, {total / 1e9:.2f} GB; early fill {p / 1e9:.2f} GB (tightest at {tight:.0f} s)")
        return 0
    if a.cmd == "retime":
        dem = parse_demand_log(a.log)
        if not dem:
            print("retime: no reads in that log, profile unchanged")
            return 1
        prof = retime_profile(json.load(open(a.profile)), dem, a.window)
        tmp = a.profile + ".tmp"
        json.dump(prof, open(tmp, "w")); os.replace(tmp, a.profile)
        print(f"retime: start-up set {prof['total_bytes'] / 1e9:.2f} GB ({len(prof['ranges'])} ranges), "
              f"later {prof['later_bytes'] / 1e9:.2f} GB")
        return 0
    if a.cmd == "early-fill":
        prof = json.load(open(a.profile))
        measured = bool(prof.get("demand"))
        rs = prof["demand"] if measured else list(prof.get("ranges", [])) + list(prof.get("later", []))
        p, total, tight = early_fill_bytes(rs, a.rate_mbps, lag=0.0 if measured else 10.0)
        print(json.dumps({"early_fill_bytes": p, "profile_bytes": total, "tightest_s": round(tight),
                          "minutes_before_launch": round(p / 21e6 / 60, 1), "measured": measured,
                          "note": "" if measured else "no measured demand: profile t are cold-run arrival times, so this is optimistic"}))
        return 0
    if a.cmd == "lang-skips":
        files = {}
        for line in sys.stdin:
            size, _, rel = line.rstrip("\n").partition("\t")
            if rel and not rel.endswith("/") and size.isdigit():
                files[rel] = int(size)
        skip = lang_skips(files)
        for rel in sorted(skip):
            print(rel)
        if a.json_out and skip:
            body = {"folder": a.folder, "bytes": sum(skip.values()), "files": sorted(skip)}
            with open(a.json_out + ".tmp", "w") as f:
                json.dump(body, f)
            os.replace(a.json_out + ".tmp", a.json_out)
        return 0
    if a.cmd == "synth-profile":
        key = _storage_key(a.conf)
        if not key:
            print("no STORAGE_KEY (environment or conf file)", file=sys.stderr)
            return 2
        try:
            hot = json.load(open(a.hot_file)) if a.hot_file else []
        except (OSError, ValueError):
            hot = []
        try:
            files = list_remote_files(a.webdav_url, a.user, key, a.folder)
        except (OSError, IOError, FatalFetchError, ValueError) as e:
            print(f"could not list {a.folder}: {e}", file=sys.stderr)
            return 1
        prof = synth_profile(files, [h for h in hot if isinstance(h, str)], a.out, a.folder,
                             int(a.edge_mb * MB1), int(a.big_mb * MB1), whole_max=int(a.whole_gb * 1e9))
        print(f"starter profile: {len(prof['ranges'])} ranges, {prof['total_bytes'] / 1e6:.0f} MB -> {a.out}")
        return 0
    if a.cmd == "fill-cache":
        key = os.environ.get("STORAGE_KEY", "")
        if not key:
            try:
                for line in open(a.conf):
                    if line.startswith("STORAGE_KEY="):
                        key = line.split("=", 1)[1].strip().strip("\"'")
            except OSError:
                pass
        if not key:
            print("no STORAGE_KEY (environment or conf file)", file=sys.stderr)
            return 2
        st = fill_cache(a.cache_dir, a.ns, a.folder, a.profile, a.webdav_url, a.user, key, a.workers,
                        a.piece_mb * MB1, int(a.max_gb * 1024 ** 3), None, a.log, include_later=a.include_later)
        return 3 if st["refused"] else (1 if st["failed"] else 0)
    if a.cmd == "show":
        with open(a.profile) as f:
            prof = json.load(f)
        rs = prof.get("ranges", [])
        files = {}
        for e in rs:
            files[e["f"]] = files.get(e["f"], 0) + e["n"]
        print(f"{len(rs)} ranges, {prof.get('total_bytes', 0) / 1e9:.2f} GB, {len(files)} files, "
              f"spanning {rs[0]['t'] if rs else 0}s to {rs[-1]['t'] if rs else 0}s")
        for f, n in sorted(files.items(), key=lambda x: -x[1])[:8]:
            print(f"  {n / 1e6:9.0f} MB  {f}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
