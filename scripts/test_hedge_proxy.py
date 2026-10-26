#!/usr/bin/env python3
"""daemon/hedge_proxy.py (racing range requests, OFF unless DECKSTREAM_HEDGE=1) and the runner block that starts it.
A fake upstream answers each request after a delay we choose, so we can see which copy wins. With rclone on PATH it
also mounts-by-HTTP through the proxy (real rclone VFS) and checks the bytes; without rclone that part is skipped."""
import http.client
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "..", "daemon")
sys.path.insert(0, DAEMON)
import hedge_proxy as hp  # noqa: E402

DATA = bytes(range(256)) * 4096          # 1 MiB of known bytes


class Upstream(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_upstream(delays=None, statuses=None, redirect_to=None):
    """delays/statuses: lists consumed one per request (in arrival order). Records each request's headers."""
    seen, lock = [], threading.Lock()
    delays, statuses = list(delays or []), list(statuses or [])

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            with lock:
                seen.append((self.command, self.path, dict(self.headers)))
                d = delays.pop(0) if delays else 0
                st = statuses.pop(0) if statuses else None
            time.sleep(d)
            if redirect_to and self.path.startswith("/redir"):
                self.send_response(302)
                self.send_header("Location", redirect_to + "/file.bin")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if st and st >= 400:
                self.send_response(st)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rng = self.headers.get("Range")
            if rng:
                a, b = rng.split("=")[1].split("-")
                a, b = int(a), min(int(b), len(DATA) - 1)
                body = DATA[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(DATA)))
            else:
                body = DATA
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_PROPFIND(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            with lock:
                seen.append((self.command, self.path, dict(self.headers)))
            me = "http://%s:%d" % self.server.server_address[:2]
            body = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>%s/file.bin</d:href>'
                    '</d:response></d:multistatus>' % me).encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = Upstream(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_proxy(upstream_url, race=2, delay_ms=0, timeout=10):
    stats = hp.Stats()
    up = hp.Upstream(upstream_url, hp.Pool(timeout), stats)
    srv = hp.Server(("127.0.0.1", 0), hp.make_handler(up, stats, race, delay_ms / 1000.0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, stats


def get(port, path, headers=None, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    t0 = time.monotonic()
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, time.monotonic() - t0, r


class TestRace(unittest.TestCase):
    def tearDown(self):
        for s in getattr(self, "servers", []):
            s.shutdown()

    def serve(self, *a, **kw):
        up, seen = make_upstream(*a, **kw)
        self.servers = getattr(self, "servers", []) + [up]
        return up, seen

    def proxy(self, url, **kw):
        p, stats = start_proxy(url, **kw)
        self.servers = getattr(self, "servers", []) + [p]
        return p, stats

    def test_fast_copy_wins_and_bytes_are_right(self):
        up, seen = self.serve(delays=[3.0, 0.05])
        p, stats = self.proxy("http://127.0.0.1:%d" % up.server_address[1])
        st, data, dt, _ = get(p.server_address[1], "/file.bin", {"Range": "bytes=1000-5095", "Authorization": "Basic x"})
        self.assertEqual(st, 206)
        self.assertEqual(data, DATA[1000:5096])
        self.assertLess(dt, 1.5, "the slow copy (3 s) must not be waited for")
        self.assertEqual(len([s for s in seen if s[0] == "GET"]), 2, "the range was asked for twice")
        # Not second_won: the upstream hands out its delays in arrival order, and either copy can arrive first.
        self.assertEqual(stats.snapshot()["raced"], 1)
        self.assertTrue(all(s[2].get("Authorization") == "Basic x" for s in seen), "rclone's login is passed through")

    def test_plain_get_and_race_1_are_sent_once(self):
        up, seen = self.serve()
        p, _ = self.proxy("http://127.0.0.1:%d" % up.server_address[1])
        st, data, _, _ = get(p.server_address[1], "/file.bin")
        self.assertEqual((st, len(data)), (200, len(DATA)))
        self.assertEqual(len(seen), 1, "a GET without Range is not raced")
        up2, seen2 = self.serve()
        p2, _ = self.proxy("http://127.0.0.1:%d" % up2.server_address[1], race=1)
        get(p2.server_address[1], "/file.bin", {"Range": "bytes=0-9"})
        self.assertEqual(len(seen2), 1, "race=1 is a plain proxy")

    def test_error_copy_loses_to_good_copy_and_errors_pass_through(self):
        up, _ = self.serve(delays=[0.0, 0.3], statuses=[429, None])
        p, _ = self.proxy("http://127.0.0.1:%d" % up.server_address[1])
        st, data, _, _ = get(p.server_address[1], "/file.bin", {"Range": "bytes=0-99"})
        self.assertEqual((st, data), (206, DATA[:100]), "a 429 on one copy must not beat a good answer")
        up2, _ = self.serve(statuses=[429, 429])
        p2, _ = self.proxy("http://127.0.0.1:%d" % up2.server_address[1])
        st, _, _, _ = get(p2.server_address[1], "/file.bin", {"Range": "bytes=0-99"})
        self.assertEqual(st, 429, "when every copy is refused, rclone sees RemoteStorage's own answer and backs off")

    def test_upstream_down_is_502(self):
        p, stats = self.proxy("http://127.0.0.1:%d" % free_port())
        st, _, _, _ = get(p.server_address[1], "/file.bin", {"Range": "bytes=0-9"})
        self.assertEqual(st, 502)
        self.assertEqual(stats.snapshot()["failed"], 1)

    def test_propfind_hrefs_point_back_at_the_proxy(self):
        up, seen = self.serve()
        p, _ = self.proxy("http://127.0.0.1:%d" % up.server_address[1])
        st, data, _, r = get(p.server_address[1], "/", {"Depth": "1", "Content-Type": "text/xml"}, "PROPFIND", b"<x/>")
        self.assertEqual(st, 207)
        self.assertIn(("http://127.0.0.1:%d/file.bin" % p.server_address[1]).encode(), data)
        self.assertNotIn(("127.0.0.1:%d" % up.server_address[1]).encode(), data)
        self.assertEqual(int(r.getheader("Content-Length")), len(data))
        self.assertEqual(len(seen), 1, "PROPFIND is sent once")

    def test_redirect_to_another_host_drops_the_login(self):
        other, other_seen = self.serve()
        up, seen = self.serve(redirect_to="http://localhost:%d" % other.server_address[1])
        p, _ = self.proxy("http://127.0.0.1:%d" % up.server_address[1], race=1)
        st, data, _, _ = get(p.server_address[1], "/redir", {"Range": "bytes=0-9", "Authorization": "Basic secret"})
        self.assertEqual((st, data), (206, DATA[:10]))
        self.assertEqual(seen[0][2].get("Authorization"), "Basic secret")
        self.assertNotIn("Authorization", other_seen[0][2], "the RemoteStorage key must not go to another host")

    def test_health_endpoint(self):
        up, _ = self.serve()
        p, _ = self.proxy("http://127.0.0.1:%d" % up.server_address[1])
        st, data, _, _ = get(p.server_address[1], hp.HEALTH)
        self.assertEqual(st, 200)
        self.assertIn("raced", json.loads(data))

    def test_only_listens_on_localhost(self):
        src = open(os.path.join(DAEMON, "hedge_proxy.py")).read()
        self.assertIn('Server(("127.0.0.1", a.port)', src)
        self.assertNotIn("0.0.0.0", src)


class TestProcess(unittest.TestCase):
    def test_exits_with_its_parent_and_writes_stats(self):
        tmp = tempfile.mkdtemp()
        try:
            parent = subprocess.Popen(["sleep", "30"])
            port = free_port()
            stats = os.path.join(tmp, "s.json")
            p = subprocess.Popen([sys.executable, "-I", os.path.join(DAEMON, "hedge_proxy.py"), "--port", str(port),
                                  "--upstream", "http://127.0.0.1:%d" % free_port(), "--parent-pid", str(parent.pid),
                                  "--stats", stats])
            for _ in range(40):
                try:
                    if get(port, hp.HEALTH)[0] == 200:
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("proxy did not start")
            parent.kill()
            parent.wait()
            p.wait(timeout=10)
            self.assertTrue(os.path.exists(stats), "counts are written at exit")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def runner_block():
    src = open(os.path.join(DAEMON, "stream-runner.sh")).read()
    a, b = src.index("# >>> hedge_proxy"), src.index("# <<< hedge_proxy")
    return src[a:b]


class TestRunnerBlock(unittest.TestCase):
    def run_block(self, env_extra, py=None):
        tmp = tempfile.mkdtemp()
        try:
            launch = os.path.join(tmp, "launch.txt")
            script = ('set -u\nCONF_DIR=%s\nLOG_DIR=%s\nGAME_ID=g\nWEBDAV_URL=http://127.0.0.1:%d\nCYAN=""\nNC=""\n'
                      'RCLONE_CONFIG_TBSTREAM_URL="$WEBDAV_URL"\nmark() { printf "%%s=%%s\\n" "$1" "${2:-}" >> %s; }\n'
                      % (tmp, tmp, free_port(), launch)) + runner_block() + (
                      '\nstart_hedge_proxy\necho "URL=$RCLONE_CONFIG_TBSTREAM_URL"\n'
                      '[ -n "$HEDGE_PID" ] && kill "$HEDGE_PID"\n')
            env = dict(os.environ, **env_extra)
            if py:
                env["HEDGE_PY"] = py
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30).stdout
            marks = open(launch).read() if os.path.exists(launch) else ""
            return out, marks
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_off_by_default(self):
        out, marks = self.run_block({"DECKSTREAM_HEDGE": "0"}, os.path.join(DAEMON, "hedge_proxy.py"))
        self.assertNotIn("127.0.0.1:47811", out)
        self.assertEqual(marks, "")
        self.assertIn('"${DECKSTREAM_HEDGE:-0}" = "1"', runner_block(), "default must be off")

    @unittest.skipUnless(shutil.which("curl"), "curl missing")
    def test_on_points_rclone_at_the_proxy(self):
        port = free_port()
        out, marks = self.run_block({"DECKSTREAM_HEDGE": "1", "DECKSTREAM_HEDGE_PORT": str(port)},
                                    os.path.join(DAEMON, "hedge_proxy.py"))
        self.assertIn("URL=http://127.0.0.1:%d" % port, out)
        self.assertIn("hedge=2", marks)

    def test_falls_back_to_direct_when_the_helper_fails(self):
        tmp = tempfile.mkdtemp()
        try:
            bad = os.path.join(tmp, "bad.py")
            open(bad, "w").write("import sys; sys.exit(1)\n")
            port = free_port()
            out, marks = self.run_block({"DECKSTREAM_HEDGE": "1", "DECKSTREAM_HEDGE_PORT": str(port)}, bad)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertNotIn("URL=http://127.0.0.1:%d" % port, out, "the mount must keep the direct RemoteStorage URL")
        self.assertIn("hedge_failed=start", marks)

    def test_cleanup_stops_the_helper(self):
        src = open(os.path.join(DAEMON, "stream-runner.sh")).read()
        clean = src[src.index("cleanup() {"):src.index("trap cleanup EXIT")]
        self.assertIn('kill "$HEDGE_PID"', clean)


@unittest.skipUnless(shutil.which("rclone"), "SKIP: rclone not installed")
class TestRealRclone(unittest.TestCase):
    """Real rclone VFS reading through the proxy from a real WebDAV server: bytes must match exactly."""

    def test_rclone_reads_through_the_proxy(self):
        tmp = tempfile.mkdtemp()
        procs = []
        try:
            data_dir = os.path.join(tmp, "data", "Game Folder")
            os.makedirs(data_dir)
            blob = os.urandom(3 * 1024 * 1024)
            open(os.path.join(data_dir, "big file.pak"), "wb").write(blob)
            wport, vport = free_port(), free_port()
            procs.append(subprocess.Popen(["rclone", "serve", "webdav", os.path.join(tmp, "data"), "--addr",
                                           "127.0.0.1:%d" % wport, "--read-only"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(1.0)
            p, stats = start_proxy("http://127.0.0.1:%d" % wport)
            procs.append(subprocess.Popen(["rclone", "serve", "http", ":webdav,url='http://127.0.0.1:%d':" %
                                           p.server_address[1], "--addr", "127.0.0.1:%d" % vport, "--read-only",
                                           "--vfs-cache-mode", "full", "--cache-dir", os.path.join(tmp, "cache"),
                                           "--vfs-read-chunk-size", "1M"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", vport), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            for off, n in ((0, 4096), (2 * 1024 * 1024 + 7, 100000), (1234567, 1)):
                st, got, _, _ = get(vport, "/Game%20Folder/big%20file.pak", {"Range": "bytes=%d-%d" % (off, off + n - 1)})
                self.assertIn(st, (200, 206))
                self.assertEqual(got, blob[off:off + n])
            self.assertGreater(stats.snapshot()["raced"], 0, "rclone's range requests went through the race")
            p.shutdown()
        finally:
            for pr in procs:
                pr.terminate()
                pr.wait(5)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
