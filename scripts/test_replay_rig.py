#!/usr/bin/env python3
"""Unit tests for scripts/replay_rig.py: the parts that need no rclone and no timing (traces, limiter, proxy counting, maths).
Includes a real rclone WebDAV + HTTP VFS integration when installed. Manual example: scripts/replay_rig.py --synthetic scattered --compare default streams1."""
import http.client
import http.server
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_rig as rr  # noqa: E402

MB = rr.MB


def test_synthetic_traces():
    files = ["a.pak", "b.pak"]
    size = 256 * MB
    for kind in ("scattered", "sequential", "mixed"):
        t = rr.synthetic_trace(kind, files, size, n=50, seed=3)
        assert 0 < len(t) <= 50, kind
        for f, off, n in t:
            assert f in files and 0 <= off and off + n <= size, (kind, f, off, n)
    seq = rr.synthetic_trace("sequential", files, size, n=10)
    assert [o for _f, o, _n in seq] == [i * MB for i in range(10)], "sequential reads must be back to back"
    assert rr.synthetic_trace("scattered", files, size, 20, seed=5) == rr.synthetic_trace("scattered", files, size, 20, seed=5), \
        "the same seed must give the same trace"
    assert rr.synthetic_trace("scattered", files, size, 20, seed=5) != rr.synthetic_trace("scattered", files, size, 20, seed=6)
    try:
        rr.synthetic_trace("nope", files, size)
        raise AssertionError("an unknown trace kind must raise")
    except ValueError:
        pass
    print("[ok] synthetic traces: bounded, seeded, sequential is contiguous")


def test_csv_and_rclone_log_traces():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.csv")
        open(p, "w").write("file,offset,size\nx.pak,0,1048576\ny.pak,4096,65536\n")
        assert rr.load_csv_trace(p) == [("x.pak", 0, 1048576), ("y.pak", 4096, 65536)]
        # rclone's DEBUG '_readAt' lines, as early_launch_calc.parse_rclone_log expects them
        import early_launch_calc as elc
        lines = ["2026/10/12 19:56:35.204892 DEBUG : Example Game/Data/Models.pak(0xc0001b2000): _readAt: size=1048576, off=141303808",
                 "2026/10/12 19:56:36.100000 DEBUG : Example Game/Data/Gustav.pak(0xc0001b3000): _readAt: size=65536, off=4096",
                 "2026/10/12 19:56:36.200000 DEBUG : vfs cache: looking for range={Pos:0 Size:131072} in []"]   # not a read: ignored
        assert elc.LINE_RE.match(lines[0]) and elc.LINE_RE.match(lines[1]) and not elc.LINE_RE.match(lines[2])
        lp = os.path.join(d, "demand.log")
        open(lp, "w").write("\n".join(lines) + "\n")
        got = rr.load_rclone_log(lp)
        assert got == [("Example Game/Data/Models.pak", 141303808, 1048576), ("Example Game/Data/Gustav.pak", 4096, 65536)], got
        assert rr.load_rclone_log(lp, limit=1) == got[:1]
        print("[ok] CSV and rclone-log traces load (only _readAt lines count)")


def test_limiter_spaces_bytes():
    lim = rr.Limiter(1000)         # 1000 bytes/s: 500 bytes take ~0.5 s, and the next 500 queue behind them
    t0 = time.monotonic()
    lim.take(500)
    lim.take(500)
    lim.take(1)
    dt = time.monotonic() - t0
    assert 0.8 <= dt <= 1.6, "token bucket must queue sends behind each other, took %.2f s" % dt
    free = rr.Limiter(0)
    t0 = time.monotonic()
    free.take(10 ** 9)
    assert time.monotonic() - t0 < 0.1, "rate 0 means unlimited"
    print("[ok] bandwidth limiter queues sends and rate 0 is unlimited")


class _Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    BODY = b"x" * 300000

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)

    do_HEAD = do_PROPFIND = do_GET


def test_proxy_counts_and_delays():
    up = rr.ProxyServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    counters = rr.Counters()
    proxy = rr.make_proxy(up.server_address[1], 0.3, 0, counters)
    try:
        port = proxy.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        t0 = time.monotonic()
        c.request("GET", "/f", headers={"Range": "bytes=0-9"})
        body = c.getresponse().read()
        dt = time.monotonic() - t0
        c.close()
        assert len(body) == len(_Upstream.BODY), "the proxy must pass the body through unchanged"
        assert dt >= 0.3, "latency must be added before the first byte (%.2f s)" % dt
        assert counters.idle.wait(5), "proxy request did not finish counting"
        snap = counters.snapshot()
        assert snap["requests"] == 1 and snap["range_requests"] == 1 and snap["body_bytes"] == len(_Upstream.BODY), snap
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("PROPFIND", "/")
        c.getresponse().read()
        c.close()
        assert counters.idle.wait(5), "proxy request did not finish counting"
        snap = counters.snapshot()
        assert snap["requests"] == 2 and snap["range_requests"] == 1, "PROPFIND is a request but not a range request: %s" % snap
    finally:
        proxy.shutdown()
        proxy.server_close()
        up.shutdown()
        up.server_close()
    print("[ok] proxy passes bodies through, adds latency, counts requests, ranges and bytes")


def test_percentile_and_configs():
    xs = [1, 2, 3, 4, 100]
    assert rr.percentile(xs, 0.5) == 3 and rr.percentile(xs, 1.0) == 100 and rr.percentile([], 0.5) == 0.0
    assert set(rr.CONFIGS) >= {"default", "streams1", "small", "noreadahead"}
    # the default config must mirror the Deck's mount flags (daemon/stream-runner.sh)
    assert rr.BASE == {"vfs-read-chunk-size": "16M", "vfs-read-chunk-streams": "1", "vfs-read-ahead": "32M", "buffer-size": "8M"}
    assert rr.CONFIGS["default"] == {}
    print("[ok] percentile maths and the default config mirrors the Deck's flags")


def test_real_rclone():
    import shutil
    rclone = shutil.which("rclone")
    if not rclone:
        print("[skip] real rclone replay: install rclone and add it to PATH")
        return
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.mkdir(data)
        with open(os.path.join(data, "test.pak"), "wb") as f:
            f.write(bytes(range(256)) * (64 * 1024))
        upstream, up_port = rr.start_upstream(rclone, data)
        counters = rr.Counters()
        proxy = rr.make_proxy(up_port, 0.01, 32 * MB, counters)
        try:
            results = []
            for config in ("default", "streams4", "small"):
                result = rr.run_config(rclone, config, rr.CONFIGS[config],
                                       [("test.pak", 4096, MB), ("test.pak", 8 * MB, MB)],
                                       counters, proxy.server_address[1], 1, tmp)
                assert result["errors"] == 0 and result["served_mb"] == 2.0, result
                assert result["range_requests"] > 0 and result["pulled_mb"] >= 2, result
                results.append(result)
            import json
            print("[ok] real WebDAV -> shaped proxy -> rclone HTTP VFS replay: " + json.dumps(results))
        finally:
            rr.stop(upstream)
            proxy.shutdown()
            proxy.server_close()


if __name__ == "__main__":
    test_synthetic_traces()
    test_csv_and_rclone_log_traces()
    test_limiter_spaces_bytes()
    test_proxy_counts_and_delays()
    test_percentile_and_configs()
    test_real_rclone()
    print("\nREPLAY RIG TESTS PASSED")
