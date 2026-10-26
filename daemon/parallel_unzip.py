#!/usr/bin/env python3
"""Extract a (possibly remote, mounted) zip with several files in flight at once.

Usage: parallel_unzip.py ARCHIVE DEST_DIR [WORKERS] [--strip PREFIX]

Each worker opens its own handle on the archive, so each file is its own network stream.
Prints a throughput line every few seconds; exits non-zero if any file fails.
Resumes: a file already on disk with its listed size is kept; a partial one (cut off by a lost connection, up to
WORKERS at once) is written again. --strip keeps only members under PREFIX and drops it from their paths (a GOG Linux
.sh installer is a shell script with a zip appended; the game is under data/noarch/). Unix exec bits are kept.
"""
import os
import stat
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

CHUNK = 1024 * 1024


def safe_target(dest_real, name):
    out = os.path.realpath(os.path.join(dest_real, name))
    if out != dest_real and not out.startswith(dest_real + os.sep):
        return None
    return out


def main():
    args = sys.argv[1:]
    strip = ""
    if "--strip" in args:
        i = args.index("--strip")
        strip = args[i + 1]
        del args[i:i + 2]
    archive, dest = args[0], args[1]
    workers = int(args[2]) if len(args) > 2 else 4
    dest_real = os.path.realpath(dest)
    os.makedirs(dest_real, exist_ok=True)

    with zipfile.ZipFile(archive) as z:
        members = z.infolist()

    files, skipped, kept = [], 0, 0
    for m in members:
        name = m.filename
        if strip:
            if not name.startswith(strip) or name == strip:
                continue
            name = name[len(strip):]
        target = safe_target(dest_real, name)
        if target is None:
            skipped += 1  # path traversal attempt
            continue
        if m.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        if stat.S_ISLNK(m.external_attr >> 16):
            skipped += 1  # never create links from an archive
            continue
        try:
            if os.path.getsize(target) == m.file_size and not os.path.islink(target):
                kept += 1      # already extracted by an earlier, interrupted run
                continue
        except OSError:
            pass
        files.append((m, target))
    if skipped:
        print(f"Skipped {skipped} unsafe entries", flush=True)
    if kept:
        print(f"Resuming: {kept} files already extracted are kept", flush=True)

    total = sum(m.file_size for m, _ in files)
    done_bytes = 0
    lock = threading.Lock()
    local = threading.local()

    def handle():
        if not hasattr(local, "z"):
            local.z = zipfile.ZipFile(archive)
        return local.z

    def extract(item):
        nonlocal done_bytes
        m, target = item
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with handle().open(m) as src, open(target, "wb") as out:
            while True:
                buf = src.read(CHUNK)
                if not buf:
                    break
                out.write(buf)
                with lock:
                    done_bytes += len(buf)
        mode = (m.external_attr >> 16) & 0o777
        if mode & 0o111:
            os.chmod(target, (mode & 0o755) | 0o600)   # launch scripts and Linux binaries must stay runnable; never setuid

    # Biggest files first so the long ones start early and the tail is short.
    files.sort(key=lambda it: it[0].file_size, reverse=True)
    start = last_print = time.time()
    last_bytes = 0
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract, it): it for it in files}
        pending = set(futures)
        while pending:
            finished = [f for f in pending if f.done()]
            for f in finished:
                pending.discard(f)
                if f.exception():
                    failed.append((futures[f][0].filename, f.exception()))
            now = time.time()
            if now - last_print >= 5:
                with lock:
                    cur = done_bytes
                rate = (cur - last_bytes) / (now - last_print) / 1e6
                pct = f" of {total / 1e9:.1f} GB" if total else ""
                print(f"Extracted {cur / 1e9:.1f} GB{pct} ({rate:.0f} MB/s)", flush=True)
                last_print, last_bytes = now, cur
            time.sleep(0.2)

    if failed:
        for name, err in failed[:5]:
            print(f"FAILED {name}: {err}", flush=True)
        sys.exit(1)
    elapsed = max(time.time() - start, 0.001)
    print(f"Extracted {done_bytes / 1e9:.1f} GB in {elapsed:.0f}s ({done_bytes / elapsed / 1e6:.0f} MB/s avg)", flush=True)


if __name__ == "__main__":
    main()
