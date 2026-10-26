#!/usr/bin/env python3
"""Hedged range requests for the stream mount. OFF unless DECKSTREAM_HEDGE=1 (stream-runner.sh), no Deck run yet.

Why: a first launch of a never-played game waits on one read at a time, and every miss waits for RemoteStorage's first byte:
median 1.40 s, p90 3.2 s in the Deck's own BG3 cold log (1,260 range requests), with two requests sent at the same
moment almost unrelated (correlation 0.12) and no slower when more are in flight. So: send each range request twice
and use whichever answers first; the other is cancelled before its body is read. Replay rig (real rclone, the Deck's
measured first-byte times): 1 stream 169-185 s -> 132 s on BG3's start-up reads. Not yet measured on a Deck, so
stream-runner.sh starts it only with DECKSTREAM_HEDGE=1.

    rclone (webdav remote, url=http://127.0.0.1:PORT) -> this proxy -> https://webdav.storage.local

Listens on 127.0.0.1 only. It holds no credentials: rclone's own Authorization header is passed through (and dropped
if RemoteStorage redirects to another host). Only GETs with a Range header are raced; everything else is forwarded once.
Nothing is logged but counts. Exits when --parent-pid (the runner) is gone.
"""
import argparse
import http.client
import http.server
import json
import os
import queue
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse

HEALTH = "/__deckstream_hedge__"
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade", "host",
       "proxy-authorization", "proxy-authenticate"}
CHUNK = 256 * 1024


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.v = {"requests": 0, "raced": 0, "second_won": 0, "upstream_errors": 0, "failed": 0, "first_byte_s": 0.0}

    def add(self, **kw):
        with self.lock:
            for k, n in kw.items():
                self.v[k] += n

    def snapshot(self):
        with self.lock:
            out = dict(self.v)
        out["first_byte_s"] = round(out["first_byte_s"], 1)
        return out


class Pool:
    """Idle upstream connections per (scheme, host), so a request does not pay a new TLS handshake every time."""

    def __init__(self, timeout, keep=8, context=None):
        self.timeout, self.keep = timeout, keep
        self.ctx = context or ssl.create_default_context()
        self.idle, self.lock = {}, threading.Lock()

    def get(self, scheme, host):
        with self.lock:
            lst = self.idle.get((scheme, host))
            if lst:
                return lst.pop(), True
        if scheme == "https":
            return http.client.HTTPSConnection(host, timeout=self.timeout, context=self.ctx), False
        return http.client.HTTPConnection(host, timeout=self.timeout), False

    def put(self, scheme, host, conn):
        with self.lock:
            lst = self.idle.setdefault((scheme, host), [])
            if len(lst) < self.keep:
                lst.append(conn)
                return
        conn.close()


def _kill(conn):
    """Stop a connection now, even if another thread is blocked reading from it."""
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    conn.close()


class Upstream:
    def __init__(self, base, pool, stats, max_redirects=3):
        u = urllib.parse.urlsplit(base)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("upstream must be an http(s) URL")
        self.scheme, self.host, self.prefix = u.scheme, u.netloc, u.path.rstrip("/")
        self.pool, self.stats, self.max_redirects = pool, stats, max_redirects

    def open(self, method, path, headers, body, holder=None):
        """One request, following redirects for GET/HEAD. Returns (scheme, host, conn, resp) with the body unread."""
        scheme, host, target = self.scheme, self.host, self.prefix + path
        headers = dict(headers)
        for _ in range(self.max_redirects + 1):
            conn, reused = self.pool.get(scheme, host)
            if holder is not None:
                holder.append(conn)
            try:
                conn.request(method, target, body=body, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException):
                conn.close()
                if not reused:
                    raise
                conn = self._fresh(scheme, host)
                if holder is not None:
                    holder.append(conn)
                conn.request(method, target, body=body, headers=headers)     # a stale idle connection: once more, fresh
                resp = conn.getresponse()
            loc = resp.getheader("Location")
            if resp.status in (301, 302, 303, 307, 308) and loc and method in ("GET", "HEAD"):
                resp.read()
                self.pool.put(scheme, host, conn)
                n = urllib.parse.urlsplit(urllib.parse.urljoin("%s://%s%s" % (scheme, host, target), loc))
                if n.scheme not in ("http", "https"):
                    break
                if n.netloc != host:
                    headers.pop("Authorization", None)       # never hand the RemoteStorage key to another host
                scheme, host = n.scheme, n.netloc
                target = (n.path or "/") + ("?" + n.query if n.query else "")
                continue
            return scheme, host, conn, resp
        raise http.client.HTTPException("too many redirects")

    def _fresh(self, scheme, host):
        if scheme == "https":
            return http.client.HTTPSConnection(host, timeout=self.pool.timeout, context=self.pool.ctx)
        return http.client.HTTPConnection(host, timeout=self.pool.timeout)

    def race(self, path, headers, n, delay):
        """Send the same GET n times (the k-th after k*delay seconds unless one has answered) and return the first
        2xx (or, if none, the first answer). The others are cut off before their bodies are read."""
        results, done = queue.Queue(), threading.Event()
        holders = [[] for _ in range(n)]
        t0 = time.monotonic()

        def attempt(i):
            if i and delay and done.wait(delay * i):
                results.put((i, None))
                return
            if done.is_set():
                results.put((i, None))
                return
            try:
                results.put((i, self.open("GET", path, headers, None, holders[i])))
            except Exception as e:  # noqa: BLE001 - any failure of one racer only matters if every racer fails
                results.put((i, e))

        for i in range(n):
            threading.Thread(target=attempt, args=(i,), daemon=True).start()
        winner, fallback, seen = None, None, 0
        while seen < n and winner is None:
            i, r = results.get()
            seen += 1
            if r is None:
                continue
            if isinstance(r, Exception):
                self.stats.add(upstream_errors=1)
                continue
            if 200 <= r[3].status < 300:
                winner = (i, r)
            elif fallback is None:
                fallback = (i, r)
            else:
                _kill(r[2])
        done.set()
        pick = winner or fallback
        if pick and fallback and pick is not fallback:
            _kill(fallback[1][2])
        for j, h in enumerate(holders):               # cut the losers off now, even mid-request
            if pick is None or j != pick[0]:
                for c in h:
                    _kill(c)

        def reap(left):
            for _ in range(left):
                _i, r = results.get()
                if r is not None and not isinstance(r, Exception):
                    _kill(r[2])
        threading.Thread(target=reap, args=(n - seen,), daemon=True).start()
        if pick is None:
            return None
        self.stats.add(raced=1 if n > 1 else 0, second_won=1 if pick[0] else 0, first_byte_s=time.monotonic() - t0)
        return pick[1]


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(up, stats, race, delay):
    own_origin = {}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"         # one request per local connection: no keep-alive bookkeeping

        def log_message(self, *a):
            pass

        def _send(self, scheme, host, conn, resp, rewrite):
            body = None
            if rewrite:
                body = resp.read()
                # absolute hrefs in a PROPFIND answer must point back at this proxy
                body = body.replace(("%s://%s" % (up.scheme, up.host)).encode(), own_origin["o"].encode())
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP or (body is not None and k.lower() == "content-length"):
                    continue
                self.send_header(k, v)
            if body is not None:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                resp.read()
                up.pool.put(scheme, host, conn)
                return
            if body is not None:
                self.wfile.write(body)
                up.pool.put(scheme, host, conn)
                return
            try:
                while True:
                    c = resp.read(CHUNK)
                    if not c:
                        break
                    self.wfile.write(c)
            except (BrokenPipeError, ConnectionResetError):
                _kill(conn)                    # rclone dropped a speculative chunk: the connection is mid-body
                return
            except (OSError, http.client.HTTPException):
                _kill(conn)
                return
            if resp.isclosed() and not resp.will_close:
                up.pool.put(scheme, host, conn)
            else:
                conn.close()

        def _handle(self):
            if self.path == HEALTH:
                data = json.dumps(stats.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            own_origin.setdefault("o", "http://%s:%d" % self.server.server_address[:2])
            stats.add(requests=1)
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            try:
                if self.command == "GET" and self.headers.get("Range") and race > 1:
                    r = up.race(self.path, hdrs, race, delay)
                else:
                    r = up.open(self.command, self.path, hdrs, body)
            except Exception:  # noqa: BLE001
                r = None
                stats.add(upstream_errors=1)
            if r is None:
                stats.add(failed=1)
                try:
                    self.send_error(502, "Upstream unavailable")
                except OSError:
                    pass
                return
            ctype = (r[3].getheader("Content-Type") or "").lower()
            self._send(*r, rewrite=self.command == "PROPFIND" and "xml" in ctype)

        do_GET = do_HEAD = do_PROPFIND = do_OPTIONS = _handle

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--upstream", default="https://webdav.storage.local")
    ap.add_argument("--port", type=int, default=47811)
    ap.add_argument("--race", type=int, default=2, help="copies of each range request (1 = plain proxy)")
    ap.add_argument("--delay-ms", type=int, default=0, help="wait this long before sending each extra copy")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--parent-pid", type=int, default=0, help="exit when this process is gone")
    ap.add_argument("--stats", help="write the counts here (JSON) every 10 s and at exit")
    a = ap.parse_args(argv)
    stats = Stats()
    up = Upstream(a.upstream, Pool(a.timeout), stats)
    srv = Server(("127.0.0.1", a.port), make_handler(up, stats, max(1, min(a.race, 4)), max(0, a.delay_ms) / 1000.0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def save():
        if a.stats:
            try:
                tmp = a.stats + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(stats.snapshot(), fh)
                os.replace(tmp, a.stats)
            except OSError:
                pass
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    last = time.monotonic()
    try:
        while True:
            time.sleep(1)
            if a.parent_pid:
                try:
                    os.kill(a.parent_pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    pass
            if time.monotonic() - last >= 10:
                save()
                last = time.monotonic()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        save()
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
