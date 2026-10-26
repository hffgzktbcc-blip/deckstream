"""Parallel, resumable, stall-aware file download for the DeckStream daemon.

Measured against RemoteStorage from South Africa: one stream gets ~10 MB/s, eight get ~22 MB/s (the line limit),
so a single-stream download leaves half the speed on the table.

The caller supplies `opener(url, headers, timeout)`, which returns an object usable as a context manager with
`.status`, `.headers` and `.read(n)`. The daemon passes an opener that enforces its URL policy (https only,
allow-listed hosts, public addresses, every redirect re-checked), so none of that is duplicated here.

Behaviour:
  * probes with `Range: bytes=0-0`; a 206 reply with Content-Range means the server supports ranges
  * big files are fetched as fixed-size segments by several workers, written with pwrite
  * a `<dest>.parts` file records finished segments, so an interrupted download resumes
  * a server that ignores Range (RemoteStorage "zip" links do) falls back to one plain stream
  * if no bytes arrive for `stall_timeout` seconds the download aborts instead of hanging forever
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

MB = 1024 * 1024


class DownloadError(Exception):
    pass


class StallError(DownloadError):
    pass


def _probe(opener, url, timeout):
    """Returns (supports_ranges, total_size, validator)."""
    with opener(url, {"Range": "bytes=0-0"}, timeout) as resp:
        status = getattr(resp, "status", 200)
        cr = resp.headers.get("Content-Range", "") or ""
        validator = resp.headers.get("ETag") or resp.headers.get("Last-Modified") or ""
        if status == 206:
            m = re.match(r"bytes\s+0-0/(\d+)", cr)
            if m:
                return True, int(m.group(1)), validator
        return False, int(resp.headers.get("Content-Length", 0) or 0), validator


def _single_stream(opener, url, dest, progress, timeout, stall_timeout):
    done = 0
    last = time.time()
    last_report = 0.0
    with opener(url, {}, timeout) as resp:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(MB)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > stall_timeout:
                    raise StallError("no data for too long")
                last = now
                if progress and now - last_report > 1.5:
                    progress(round(done / total * 100, 1) if total else 0, done, total)
                    last_report = now
    if progress:
        progress(100.0, done, total or done)
    return done


def download(url, dest, opener, progress=None, workers=8, segment=32 * MB, min_parallel=64 * MB,
             stall_timeout=300, retries=3, timeout=60, backoff=1.0):
    """Download `url` to `dest`. Returns the number of bytes written. Raises DownloadError."""
    dest = str(dest)
    try:
        ranged, total, validator = _probe(opener, url, timeout)
    except Exception as e:  # noqa: BLE001
        raise DownloadError(f"probe failed: {e}") from e

    if not ranged or total < min_parallel:
        try:
            return _single_stream(opener, url, dest, progress, timeout, stall_timeout)
        except StallError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DownloadError(f"download failed: {e}") from e

    nseg = (total + segment - 1) // segment
    parts_file = dest + ".parts"
    done = set()
    try:
        meta = json.load(open(parts_file))
        if (meta.get("size") == total and meta.get("segment") == segment
                and meta.get("validator", "") == validator and os.path.getsize(dest) == total):
            done = {i for i in meta.get("done", []) if 0 <= i < nseg}
    except (OSError, ValueError):
        pass
    if not done:
        with open(dest, "wb") as f:
            f.truncate(total)

    lock = threading.Lock()
    abort = threading.Event()
    state = {"bytes": sum(min(segment, total - i * segment) for i in done), "last_change": time.time()}
    fd = os.open(dest, os.O_RDWR)

    def save_parts():
        tmp = parts_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"size": total, "segment": segment, "validator": validator, "done": sorted(done)}, f)
        os.replace(tmp, parts_file)

    def fetch(i):
        if i in done:
            return
        start = i * segment
        end = min(start + segment, total) - 1
        for attempt in range(1, retries + 1):
            if abort.is_set():
                return
            got = 0
            try:
                with opener(url, {"Range": f"bytes={start}-{end}"}, timeout) as resp:
                    if getattr(resp, "status", 206) != 206:
                        raise DownloadError(f"server stopped honouring Range (HTTP {resp.status})")
                    pos = start
                    while pos <= end and not abort.is_set():
                        chunk = resp.read(min(MB, end - pos + 1))
                        if not chunk:
                            break
                        os.pwrite(fd, chunk, pos)
                        pos += len(chunk)
                        got += len(chunk)
                        with lock:
                            state["bytes"] += len(chunk)
                            state["last_change"] = time.time()
                    if pos != end + 1:
                        if abort.is_set():
                            return
                        raise DownloadError(f"segment {i} ended early ({pos - start} of {end - start + 1} bytes)")
                with lock:
                    done.add(i)
                    save_parts()
                return
            except DownloadError as e:
                err = e
            except Exception as e:  # noqa: BLE001
                err = e
            with lock:
                state["bytes"] -= got        # this attempt's bytes will be fetched again
            if attempt == retries:
                abort.set()
                raise DownloadError(f"segment {i} failed after {retries} tries: {err}")
            time.sleep(min(backoff * 2 ** (attempt - 1), 10))

    pool = ThreadPoolExecutor(max_workers=max(1, int(workers)))
    futures = [pool.submit(fetch, i) for i in range(nseg)]
    last_report = 0.0
    try:
        while True:
            if all(f.done() for f in futures):
                break
            now = time.time()
            with lock:
                got, idle = state["bytes"], now - state["last_change"]
            if idle > stall_timeout:
                abort.set()
                raise StallError(f"no data for {int(idle)}s")
            if progress and now - last_report > 1.5:
                progress(round(got / total * 100, 1), got, total)
                last_report = now
            time.sleep(0.1)
        for f in futures:
            f.result()          # re-raise the first real error
    except BaseException:
        abort.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        os.close(fd)
    if len(done) != nseg:
        raise DownloadError("download finished with missing segments")
    try:
        os.unlink(parts_file)
    except OSError:
        pass
    if progress:
        progress(100.0, total, total)
    return total
