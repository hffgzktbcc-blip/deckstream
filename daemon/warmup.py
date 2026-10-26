#!/usr/bin/env python3
"""Background cache warm-up for stream mode.

The game's own reads are latency-bound (about 8 MB/s in the Sifu test) while the mount can deliver 18-22 MB/s,
so there is spare bandwidth. This reads the game's files through the rclone mount, smallest first, so they
land in rclone's cache before the game asks for them. It is deliberately gentle:
  * waits --delay seconds before starting (the game's own start-up reads come first)
  * stays under --mbps in total
  * stops when everything is read, when --max-gb has been read, when the mount goes away, or on a stop file
  * never writes anything; unreadable files are skipped

Usage: warmup.py MOUNTED_FOLDER [--delay 120] [--mbps 8] [--max-gb 0] [--workers 3]
                                [--skip-ext .mp3,.flac,...] [--stop-file PATH] [--log PATH]
"""
import argparse
import os
import sys
import threading
import time

BLOCK = 4 * 1024 * 1024
DEFAULT_SKIP = ".mp3,.flac,.ogg,.nfo,.txt,.url,.md,.pdf,.jpg,.jpeg,.png"


class RateLimiter:
    """Shared by all workers: keeps the average rate under `mbps` (0 = unlimited)."""

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


def list_files(root, skip_ext):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(skip_ext):
                continue
            path = os.path.join(dirpath, name)
            try:
                out.append((os.path.getsize(path), path))
            except OSError:
                continue
    out.sort()                       # smallest first: configs and small data are read early by games
    return out


def warm(root, delay=120, mbps=8.0, max_gb=0.0, workers=3, skip_ext=DEFAULT_SKIP, stop_file=None, log=None,
         sleep=time.sleep):
    skip = tuple(e.strip().lower() for e in skip_ext.split(",") if e.strip())
    stop = threading.Event()
    stats = {"bytes": 0, "files": 0, "skipped": 0}
    lock = threading.Lock()
    limiter = RateLimiter(mbps)
    cap = int(max_gb * 1024 ** 3)

    def say(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        if log:
            try:
                with open(log, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def should_stop():
        if stop.is_set():
            return True
        if stop_file and os.path.exists(stop_file):
            stop.set()
        elif not os.path.isdir(root):
            say("the mount is gone; stopping")
            stop.set()
        return stop.is_set()

    for _ in range(int(delay)):          # sleep in 1 s steps so a stop request is honoured quickly
        if should_stop():
            return stats
        sleep(1)

    files = list_files(root, skip)
    total = sum(s for s, _ in files)
    say(f"warming {len(files)} files, {total / 1024 ** 3:.1f} GB, at most {mbps} MB/s with {workers} workers")
    queue = iter(files)
    qlock = threading.Lock()
    last_report = [time.monotonic()]

    def worker():
        while not should_stop():
            with qlock:
                item = next(queue, None)
            if item is None:
                return
            size, path = item
            read = 0
            try:
                with open(path, "rb", buffering=0) as f:
                    while not should_stop():
                        if cap and stats["bytes"] >= cap:
                            stop.set()
                            break
                        limiter.take(BLOCK)
                        chunk = f.read(BLOCK)
                        if not chunk:
                            break
                        read += len(chunk)
                        with lock:
                            stats["bytes"] += len(chunk)
            except OSError:
                with lock:
                    stats["skipped"] += 1
                continue
            with lock:
                stats["files"] += 1
                now = time.monotonic()
                if now - last_report[0] > 60:
                    last_report[0] = now
                    say(f"read {stats['bytes'] / 1024 ** 3:.2f} GB, {stats['files']}/{len(files)} files")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, int(workers)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    say(f"done: {stats['bytes'] / 1024 ** 3:.2f} GB read, {stats['files']} files, {stats['skipped']} skipped"
        + (" (stopped early)" if stop.is_set() else ""))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--delay", type=float, default=120)
    ap.add_argument("--mbps", type=float, default=8.0)
    ap.add_argument("--max-gb", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--skip-ext", default=DEFAULT_SKIP)
    ap.add_argument("--stop-file")
    ap.add_argument("--log")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.folder):
        print(f"not a folder: {a.folder}", file=sys.stderr)
        return 2
    warm(a.folder, a.delay, a.mbps, a.max_gb, a.workers, a.skip_ext, a.stop_file, a.log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
