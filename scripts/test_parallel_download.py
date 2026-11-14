"""parallel_download against a fake range-capable server with misbehaviours."""
import hashlib, json, os, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import parallel_download as pd

MB = 1024 * 1024
DATA = os.urandom(12 * MB + 12345)           # not a multiple of the segment size
SHA = hashlib.sha256(DATA).hexdigest()
cfg = {}; log = []; live = {"now": 0, "max": 0}; lock = threading.Lock(); seen_once = set()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        rng = self.headers.get("Range")
        with lock:
            log.append(rng); live["now"] += 1; live["max"] = max(live["max"], live["now"])
        try:
            mode = cfg.get("mode", "normal")
            if rng is None or mode == "no_range":
                self.send_response(200); self.send_header("Content-Length", str(len(DATA))); self.end_headers()
                self.wfile.write(DATA); return
            a, b = [int(x) for x in rng.split("=")[1].split("-")]
            b = min(b, len(DATA) - 1)
            if mode == "range_200" and rng != "bytes=0-0":
                self.send_response(200); self.send_header("Content-Length", str(len(DATA))); self.end_headers()
                self.wfile.write(DATA); return
            body = DATA[a:b + 1]
            self.send_response(206); self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
            self.send_header("Content-Length", str(len(body))); self.send_header("ETag", cfg.get("etag", '"v1"')); self.end_headers()
            if rng == "bytes=0-0":
                self.wfile.write(body); return
            if mode == "flaky" and rng not in seen_once:
                seen_once.add(rng); self.wfile.write(body[: len(body) // 2]); self.wfile.flush(); self.connection.close(); return
            if mode == "bad_segment" and a == cfg["bad_start"]:
                self.send_response(500) if False else None
                self.wfile.write(body[:10]); self.wfile.flush(); self.connection.close(); return
            if mode == "hang" and a >= cfg.get("hang_from", 0):
                self.wfile.write(body[:1000]); self.wfile.flush(); time.sleep(5); return
            if cfg.get("rate"):
                for i in range(0, len(body), 64 * 1024):
                    self.wfile.write(body[i:i + 64 * 1024]); time.sleep(64 * 1024 / cfg["rate"])
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with lock: live["now"] -= 1


srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
tmp = Path(tempfile.mkdtemp())

def opener(url, headers, timeout):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def reset(**c):
    # Let the previous test's server threads finish first (a rate-limited one can still be writing): zeroing the
    # counter under them made them decrement it later and skew this test's connection count (CI flake).
    for _ in range(100):
        with lock:
            if live["now"] <= 0: break
        time.sleep(0.05)
    cfg.clear(); cfg.update(c); log.clear(); seen_once.clear(); live.update(now=0, max=0)
KW = dict(segment=1 * MB, min_parallel=2 * MB, backoff=0.05, timeout=5)

# 1. normal: identical bytes, several connections at once, progress reaches 100 monotonically
# (16 MB/s per connection: an instant local server let each piece finish before the next started on fast CI machines)
reset(rate=16 * MB); d = tmp / "a.bin"; prog = []
n = pd.download(URL, d, opener, progress=lambda p, b, t: prog.append((p, b, t)), workers=6, **KW)
assert n == len(DATA) and sha(d) == SHA, "content differs"
assert live["max"] >= 2, f"never ran in parallel (max {live['max']})"
assert prog and prog[-1][0] == 100.0 and all(prog[i][0] <= prog[i + 1][0] for i in range(len(prog) - 1))
assert not Path(str(d) + ".parts").exists(), "sidecar left behind"
print(f"[ok] parallel download is byte-identical (up to {live['max']} connections at once), progress reaches 100%, no leftovers")

# 2. speed: with a per-connection cap, 8 workers are several times faster than 1
reset(rate=3 * MB); t0 = time.time(); pd.download(URL, tmp / "s1.bin", opener, workers=1, **KW); t1 = time.time() - t0
reset(rate=3 * MB); t0 = time.time(); pd.download(URL, tmp / "s8.bin", opener, workers=8, **KW); t8 = time.time() - t0
assert sha(tmp / "s8.bin") == SHA and t1 / t8 > 2.5, (t1, t8)
print(f"[ok] 1 worker {t1:.1f}s vs 8 workers {t8:.1f}s with a per-connection cap ({t1 / t8:.1f}x faster)")

# 3. servers that ignore Range fall back to one stream (RemoteStorage zip links do this)
# Checked by the request log, not the live connection count: the server answers the bytes=0-0 probe with the whole
# file, and its thread can still be writing that when the single stream arrives (max 2 on busy CI runners, run 90).
reset(mode="no_range"); d = tmp / "nr.bin"; pd.download(URL, d, opener, workers=6, **KW)
assert sha(d) == SHA and log == ["bytes=0-0", None], log
reset(mode="range_200"); d = tmp / "r200.bin"
try:
    pd.download(URL, d, opener, workers=2, retries=1, **KW); ok = sha(d) == SHA
except pd.DownloadError:
    ok = True      # a server that agrees to ranges and then stops is reported, never silently corrupted
assert ok
print("[ok] a server that ignores Range -> single clean stream; one that stops honouring it mid-way -> clear error")

# 4. truncated segments are retried and the file still comes out right
reset(mode="flaky"); d = tmp / "fl.bin"; pd.download(URL, d, opener, workers=4, retries=3, **KW)
assert sha(d) == SHA
print("[ok] connections that die half-way are retried; result still identical")

# 5. a segment that keeps failing aborts the download with an error
reset(mode="bad_segment", bad_start=3 * MB); d = tmp / "bad.bin"
try:
    pd.download(URL, d, opener, workers=4, retries=2, **KW); raise SystemExit("[x] should have failed")
except pd.DownloadError as e:
    assert "segment" in str(e)
print("[ok] a permanently failing segment -> DownloadError (not a silently short file)")

# 6. a stalled server is detected instead of hanging forever
reset(mode="hang", hang_from=2 * MB); d = tmp / "hang.bin"; t0 = time.time()
try:
    pd.download(URL, d, opener, workers=3, stall_timeout=1.5, **{**KW, "timeout": 3}); raise SystemExit("[x] should have stalled")
except pd.StallError:
    assert time.time() - t0 < 10, "took too long to notice the stall"
print(f"[ok] stall detected after ~{time.time() - t0:.1f}s of no data")

# 7. resume: only the missing segments are requested
reset(); d = tmp / "res.bin"; done = [0, 1, 2, 3, 4, 5]
with open(d, "wb") as f:
    f.truncate(len(DATA)); f.seek(0); f.write(DATA[: 6 * MB])
json.dump({"size": len(DATA), "segment": MB, "validator": '"v1"', "done": done}, open(str(d) + ".parts", "w"))
pd.download(URL, d, opener, workers=4, **KW)
asked = [r for r in log if r and r != "bytes=0-0"]
assert sha(d) == SHA and len(asked) == 13 - 6 and all(int(r.split("=")[1].split("-")[0]) >= 6 * MB for r in asked), asked
print(f"[ok] resume: {len(done)} of 13 segments kept, only the other {len(asked)} re-requested")

# 8. a changed file (different validator) is NOT stitched onto old data
reset(etag='"v2"'); d = tmp / "res.bin2"
with open(d, "wb") as f:
    f.truncate(len(DATA)); f.write(b"\x00" * (6 * MB))
json.dump({"size": len(DATA), "segment": MB, "validator": '"v1"', "done": done}, open(str(d) + ".parts", "w"))
pd.download(URL, d, opener, workers=4, **KW)
assert sha(d) == SHA and len([r for r in log if r and r != "bytes=0-0"]) == 13
print("[ok] if the remote file changed, the old partial data is discarded and everything is refetched")

# 9. the caller's URL policy is respected (opener can refuse)
def refusing(url, headers, timeout): raise PermissionError("host not allowed")
try:
    pd.download(URL, tmp / "no.bin", refusing, **KW); raise SystemExit("[x] policy bypassed")
except pd.DownloadError as e:
    assert "host not allowed" in str(e)
print("[ok] URL policy from the daemon is honoured (refused hosts never download)")
print("\nPARALLEL DOWNLOAD TESTS PASSED")
