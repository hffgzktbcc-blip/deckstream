#!/usr/bin/env python3
"""Replay rig: measure rclone's read-ahead / chunking behaviour on THIS machine, with no Deck and nobody playing.

   upstream WebDAV (rclone serve webdav, sparse test files)
        ^
   latency + bandwidth proxy (this file; counts every byte rclone pulls)
        ^
   rclone serve http  <- the REAL rclone VFS layer with the Deck's own --vfs-* flags (no mount, no macFUSE, no sudo)
        ^
   replay client (this file): issues the recorded reads (offset, size) and times each one

It answers: "for this read pattern and these flags, how many bytes does rclone download per byte the game asked for
(over-fetch), and how long does each read wait?" The over-fetch ratio describes this replay workload;
absolute latencies are only as good as --latency/--bandwidth (use measured values for your own WebDAV server).
NOT modelled: the SD card, Wi-Fi, storage-server throttling or transient errors, the kernel's read-ahead, fuse-overlayfs.

Examples:
  scripts/replay_rig.py --synthetic scattered --compare default streams4 small
  scripts/replay_rig.py --rclone-log demand.log --compare default streams4 --concurrency 4
Traces: --synthetic {scattered,sequential,mixed} or --rclone-log FILE (rclone DEBUG demand log, parsed with
early_launch_calc.parse_rclone_log) or --trace FILE.csv (columns: file,offset,size).
"""
import argparse
import csv
import http.client
import http.server
import json
import os
import random
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

MB = 1024 * 1024

# The Deck's mount flags (daemon/stream-runner.sh) as rclone `serve http` flags. Variants change one thing at a time.
BASE = {"vfs-read-chunk-size": "16M", "vfs-read-chunk-streams": "1", "vfs-read-ahead": "32M", "buffer-size": "8M"}
CONFIGS = {
    "default": {},
    "streams4": {"vfs-read-chunk-streams": "4"},
    "streams1": {"vfs-read-chunk-streams": "1"},
    "streams2": {"vfs-read-chunk-streams": "2"},
    "small": {"vfs-read-chunk-size": "2M", "vfs-read-ahead": "4M", "vfs-read-chunk-streams": "1"},
    "noreadahead": {"vfs-read-ahead": "0"},
}


# ---------------------------------------------------------------------------------------------------------------- traces
def synthetic_trace(kind, files, file_size, n=300, seed=1):
    """[(file, offset, size)]: 'scattered' = random 1 MB reads at random offsets, 'sequential', or 'mixed'."""
    rng = random.Random(seed)
    out = []
    if kind == "sequential":
        f, off = files[0], 0
        while len(out) < n and off + MB <= file_size:
            out.append((f, off, MB))
            off += MB
    elif kind == "scattered":
        for _ in range(n):
            out.append((rng.choice(files), rng.randrange(0, file_size - MB) // 4096 * 4096, MB))
    elif kind == "mixed":
        for i in range(n):
            if i % 4 == 0:   # a short sequential run, then a jump
                f, off = rng.choice(files), rng.randrange(0, file_size - 8 * MB) // 4096 * 4096
                for k in range(4):
                    out.append((f, off + k * MB, MB))
            if len(out) >= n:
                break
        out = out[:n]
    else:
        raise ValueError("unknown synthetic trace: " + kind)
    return out


def load_csv_trace(path):
    with open(path, newline="") as fh:
        return [(r["file"], int(r["offset"]), int(r["size"])) for r in csv.DictReader(fh)]


def load_rclone_log(path, limit=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import early_launch_calc as elc
    with open(path, errors="replace") as fh:
        events = elc.parse_rclone_log(fh)
    out = [(f, off, size) for _t, f, off, size in events if size > 0]
    return out[:limit] if limit else out


# ----------------------------------------------------------------------------------------------------------------- proxy
class Limiter:
    """A token bucket shared by every connection: total bytes/s through the proxy (0 = unlimited)."""

    def __init__(self, bytes_per_s):
        self.rate = bytes_per_s
        self.lock = threading.Lock()
        self.next_free = time.monotonic()

    def take(self, n):
        if not self.rate:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_free)
            self.next_free = start + n / self.rate
            wait = start - now
        if wait > 0:
            time.sleep(wait)


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.idle = threading.Event()
        self.idle.set()
        self.requests = 0           # every upstream HTTP request
        self.range_requests = 0     # GETs with a Range header
        self.body_bytes = 0         # bytes of response bodies sent to rclone (what a real network would carry)

    def add(self, requests=0, ranges=0, body=0):
        with self.lock:
            self.requests += requests
            self.range_requests += ranges
            self.body_bytes += body

    def begin(self):
        with self.lock:
            self.active += 1
            self.idle.clear()

    def end(self):
        with self.lock:
            self.active -= 1
            if self.active == 0:
                self.idle.set()

    def snapshot(self):
        with self.lock:
            return {"requests": self.requests, "range_requests": self.range_requests, "body_bytes": self.body_bytes}


class ProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def proxy_paths(files):
    """Build upstream targets solely from the fixture file list, including WebDAV directories."""
    allowed_paths: dict[str, str] = {"/": "/"}
    for name in files:
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("fixture paths must be relative and contain no traversal")
        path = "/" + urllib.parse.quote(name, safe="/")
        allowed_paths[path] = path
        for depth in range(1, len(parts)):
            directory = "/" + urllib.parse.quote("/".join(parts[:depth]), safe="/") + "/"
            allowed_paths[directory] = directory
    return allowed_paths


def make_proxy(upstream_port, latency_s, bandwidth, counters, allowed_paths: dict[str, str]):
    limiter = Limiter(bandwidth)

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"      # one request per connection: no keep-alive bookkeeping

        def log_message(self, *a):
            pass

        def _forward(self):
            counters.begin()
            try:
                self._request()
            finally:
                counters.end()

        def _request(self):
            target = allowed_paths.get(self.path)
            if target is None:
                self.send_error(404, "Not Found")
                return
            time.sleep(latency_s)          # the "first byte takes ~1 s" cost of every upstream request
            body = b""
            if self.headers.get("Content-Length"):
                body = self.rfile.read(int(self.headers["Content-Length"]))
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "proxy-connection")}
            conn = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=120)
            try:
                conn.request(self.command, target, body=body or None, headers=hdrs)
                resp = conn.getresponse()
            except (ConnectionRefusedError, socket.error, OSError):
                try:
                    self.send_error(502, "Bad Gateway")
                except Exception:
                    pass
                return
            try:
                counters.add(requests=1, ranges=1 if (self.command == "GET" and self.headers.get("Range")) else 0)
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("connection", "transfer-encoding", "keep-alive"):
                        self.send_header(k, v)
                self.end_headers()
                if self.command == "HEAD":
                    return
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    limiter.take(len(chunk))
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break              # rclone cancelled a speculative chunk: only what was sent is counted
                    counters.add(body=len(chunk))
            finally:
                conn.close()

        do_GET = do_HEAD = do_PROPFIND = do_OPTIONS = _forward

    srv = ProxyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------------------------------------------------ processes
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def start_upstream(rclone, data_dir):
    port = free_port()
    p = subprocess.Popen([rclone, "serve", "webdav", data_dir, "--addr", "127.0.0.1:%d" % port, "--read-only", "--config", os.devnull],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_port(port):
        p.terminate()
        raise RuntimeError("rclone serve webdav did not start")
    return p, port


def start_vfs(rclone, proxy_port, cfg, cache_dir):
    port = free_port()
    flags = dict(BASE)
    flags.update(cfg)
    cmd = [rclone, "serve", "http", ":webdav,url='http://127.0.0.1:%d':" % proxy_port, "--addr", "127.0.0.1:%d" % port,
           "--read-only", "--vfs-cache-mode", "full", "--vfs-cache-max-size", "20G", "--no-modtime", "--config", os.devnull,
           "--cache-dir", cache_dir]
    for k, v in flags.items():
        cmd += ["--" + k, v]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not wait_port(port):
        err = p.stderr.read().decode(errors="replace")[:300] if p.poll() is not None else ""
        p.terminate()
        raise RuntimeError("rclone serve http did not start: " + err)
    return p, port, flags


def stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)


# ------------------------------------------------------------------------------------------------------------- replay
def get_range(port, name, off, size):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    t0 = time.monotonic()
    try:
        conn.request("GET", "/" + urllib.parse.quote(name), headers={"Range": "bytes=%d-%d" % (off, off + size - 1)})
        r = conn.getresponse()
        got = len(r.read())
        return time.monotonic() - t0, got, r.status
    finally:
        conn.close()


def replay(port, trace, concurrency):
    lat, errors, got_total = [], 0, 0
    lock = threading.Lock()
    it = iter(trace)

    def worker():
        nonlocal errors, got_total
        while True:
            with lock:
                item = next(it, None)
            if item is None:
                return
            try:
                dt, got, status = get_range(port, *item)
            except Exception:
                with lock:
                    errors += 1
                continue
            with lock:
                lat.append(dt)
                got_total += got
                if status != 206 or got != item[2]:
                    errors += 1

    t0 = time.monotonic()
    ts = [threading.Thread(target=worker) for _ in range(max(1, concurrency))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return lat, errors, got_total, time.monotonic() - t0


def percentile(xs, q):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def run_config(rclone, name, cfg, trace, counters, proxy_port, concurrency, tmp):
    cache = tempfile.mkdtemp(prefix="cache_" + name + "_", dir=tmp)
    before = counters.snapshot()
    p, port, flags = start_vfs(rclone, proxy_port, cfg, cache)
    try:
        lat, errors, got, wall = replay(port, trace, concurrency)
    finally:
        stop(p)
    if not counters.idle.wait(30):
        raise RuntimeError("upstream requests did not settle after stopping VFS; measurement discarded")
    after = counters.snapshot()
    asked = sum(s for _f, _o, s in trace)
    pulled = after["body_bytes"] - before["body_bytes"]
    return {
        "measurement": "replay-rig", "config": name, "flags": {k: v for k, v in flags.items() if BASE.get(k) != v or k in cfg},
        "reads": len(trace), "errors": errors, "asked_mb": round(asked / MB, 1), "served_mb": round(got / MB, 1),
        "pulled_mb": round(pulled / MB, 1), "overfetch": round(pulled / asked, 2) if asked else 0,
        "upstream_requests": after["requests"] - before["requests"],
        "range_requests": after["range_requests"] - before["range_requests"],
        "wall_s": round(wall, 1),
        "lat_p50_s": round(percentile(lat, 0.5), 3), "lat_p95_s": round(percentile(lat, 0.95), 3),
        "lat_max_s": round(max(lat), 3) if lat else 0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--synthetic", choices=["scattered", "sequential", "mixed"])
    src.add_argument("--rclone-log")
    src.add_argument("--trace")
    ap.add_argument("--reads", type=int, default=300, help="max reads to replay (default 300)")
    ap.add_argument("--seed", type=int, default=1, help="seed for synthetic traces (default 1)")
    ap.add_argument("--file-mb", type=int, default=1024, help="size of each synthetic upstream file (default 1024)")
    ap.add_argument("--files", type=int, default=3)
    ap.add_argument("--latency", type=float, default=0.8, help="seconds added to every upstream request (default 0.8)")
    ap.add_argument("--bandwidth-mbps", type=float, default=23.0, help="shared upstream MB/s, 0 = unlimited (default 23)")
    ap.add_argument("--concurrency", type=int, default=1, help="reads in flight at once (a game issues several)")
    ap.add_argument("--compare", nargs="*", default=["default"], help="config names: " + ", ".join(CONFIGS))
    ap.add_argument("--rclone", default=shutil.which("rclone") or "rclone")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if (a.reads <= 0 or a.files <= 0 or a.file_mb <= 8 or a.concurrency <= 0
            or a.latency < 0 or a.bandwidth_mbps < 0 or not a.compare):
        ap.error("reads, files, concurrency and configurations must be positive; file-mb > 8; latency/bandwidth >= 0")

    if not (a.synthetic or a.rclone_log or a.trace):
        a.synthetic = "scattered"
    for c in a.compare:
        if c not in CONFIGS:
            ap.error("unknown config %r (have: %s)" % (c, ", ".join(CONFIGS)))
    if not shutil.which(a.rclone) and not os.path.isfile(a.rclone):
        print("rclone not found: install it or pass --rclone", file=sys.stderr)
        return 2

    tmp = tempfile.mkdtemp(prefix="replay_rig_")
    procs, srv = [], None
    try:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        names = ["file%d.pak" % i for i in range(a.files)]
        for n in names:
            with open(os.path.join(data, n), "wb") as fh:
                fh.truncate(a.file_mb * MB)       # sparse: zeros, no disk used
        if a.synthetic:
            trace = synthetic_trace(a.synthetic, names, a.file_mb * MB, a.reads, a.seed)
        else:
            trace = load_rclone_log(a.rclone_log, a.reads) if a.rclone_log else load_csv_trace(a.trace)
            # real traces name real files: map each distinct file onto the synthetic files, clamping offsets to their size
            mapping, out = {}, []
            trace = trace[:a.reads]
            for f, off, size in trace:
                if size <= 0 or size > a.file_mb * MB or off < 0:
                    ap.error("trace reads must fit within file-mb and have nonnegative offsets")
                mapping.setdefault(f, names[len(mapping) % len(names)])
                out.append((mapping[f], off % (a.file_mb * MB - size + 1), size))
            trace = out
        if not trace:
            print("the trace is empty", file=sys.stderr)
            return 2

        up, up_port = start_upstream(a.rclone, data)
        procs.append(up)
        counters = Counters()
        srv = make_proxy(up_port, a.latency, int(a.bandwidth_mbps * MB), counters, proxy_paths(names))
        proxy_port = srv.server_address[1]

        results = [run_config(a.rclone, c, CONFIGS[c], trace, counters, proxy_port, a.concurrency, tmp) for c in a.compare]
    finally:
        for p in procs:
            stop(p)
        if srv:
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    if a.json:
        print(json.dumps(results, indent=2))
        return 1 if any(r["errors"] for r in results) else 0
    print("replay-rig measurement (not a hardware launch)")
    print("trace: %d reads, %.1f MB asked | latency %.2f s, bandwidth %.0f MB/s, concurrency %d"
          % (len(trace), results[0]["asked_mb"], a.latency, a.bandwidth_mbps, a.concurrency))
    print("%-12s %9s %9s %9s %8s %8s %8s %8s" % ("config", "asked MB", "pulled MB", "overfetch", "reqs", "wall s", "p50 s", "p95 s"))
    for r in results:
        print("%-12s %9.1f %9.1f %8.2fx %8d %8.1f %8.3f %8.3f%s" % (
            r["config"], r["asked_mb"], r["pulled_mb"], r["overfetch"], r["upstream_requests"], r["wall_s"],
            r["lat_p50_s"], r["lat_p95_s"], ("   ERRORS: %d" % r["errors"]) if r["errors"] else ""))
    return 1 if any(r["errors"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
