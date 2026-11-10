"""Downloading, extracting and installing games (direct and cloud installs).

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import os
import time
import json
import shutil
import subprocess
import tarfile
import re
import urllib.request
import urllib.error
import signal
import threading
from pathlib import Path

import ds_core
from ds_core import GAMES_DIR_INTERNAL, HOME, get_micro_sd_path, open_checked, queue_request
from ds_steam import find_best_executable, find_native_start, make_native_runner
from ds_shortcuts import request_shortcut
from ds_stream import on_ac_power


ARCHIVE_MAGIC = (
    b"PK\x03\x04", b"PK\x05\x06",            # zip (an empty zip starts with the end record)
    b"7z\xbc\xaf\x27\x1c",                   # 7z
    b"Rar!\x1a\x07",                         # rar 4 and 5
    b"\x1f\x8b",                             # gzip (.tar.gz)
    b"\xfd7zXZ\x00",                         # xz
    b"BZh",                                  # bzip2
)


def looks_like_archive(path):
    """True if the file starts with an archive signature (the download is saved as download.pkg, so the name says
    nothing). Plain tar has its signature at offset 257."""
    try:
        with open(path, "rb") as f:
            head = f.read(262)
    except OSError:
        return False
    return head.startswith(ARCHIVE_MAGIC) or head[257:262] == b"ustar"


try:
    import parallel_download
except ImportError:          # an older install without the module: keep the single-stream path
    parallel_download = None

DOWNLOAD_FAILURES = (ValueError, urllib.error.URLError, OSError) + (
    (parallel_download.DownloadError,) if parallel_download else ())


def download_file(url, dest_path, progress_callback=None):
    """Parallel + resumable when the server allows ranges; stops if no data arrives for 5 minutes."""
    if parallel_download is not None:
        def opener(u, headers, timeout):
            return open_checked(u, ds_core.ALLOWED_DOWNLOAD_HOSTS, timeout, headers)
        parallel_download.download(url, dest_path, opener, progress=progress_callback,
                                   workers=8, segment=32 * 1024 * 1024, stall_timeout=300)
        return
    with open_checked(url, ds_core.ALLOWED_DOWNLOAD_HOSTS, 30) as resp:
        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0
        block_size = 1024 * 1024 # 1MB chunks

        with open(dest_path, "wb") as f:
            last_report = 0
            while True:
                chunk = resp.read(block_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                
                now = time.time()
                if progress_callback and (now - last_report > 1.5 or downloaded == total_size):
                    pct = round((downloaded / total_size) * 100, 1) if total_size > 0 else 0
                    progress_callback(pct, downloaded, total_size)
                    last_report = now


def _purge_unsafe_extraction(extract_dir):
    """External extractors can create symlinks that point outside the game folder. Remove them."""
    root = Path(extract_dir).resolve()
    for path in list(Path(extract_dir).rglob("*")):
        if path.is_symlink():
            target = path.resolve()
            if root != target and root not in target.parents:
                print(f"[!] Removing unsafe link from archive: {path}")
                path.unlink()


def extract_archive(archive_path, extract_dir):
    p = Path(archive_path)
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Extracting {p.name}...")
    if p.suffix.lower() == ".zip":
        import zipfile
        with zipfile.ZipFile(p, "r") as z:
            z.extractall(extract_dir)
    elif p.suffix.lower() in [".tar", ".gz", ".xz"]:
        with tarfile.open(p, "r:*") as t:
            if hasattr(tarfile, "data_filter"):
                # Rejects absolute paths, ../ escapes, links outside the folder and device files
                t.extractall(extract_dir, filter="data")
            else:
                root = extract_dir.resolve()
                for m in t.getmembers():
                    target = (root / m.name).resolve()
                    if root not in target.parents and target != root:
                        raise RuntimeError(f"Unsafe path in archive: {m.name}")
                    if m.issym() or m.islnk() or m.isdev():
                        raise RuntimeError(f"Archive contains a link or device file: {m.name}")
                t.extractall(extract_dir)
    elif shutil.which("7z"):
        subprocess.run(["7z", "x", str(p), f"-o{extract_dir}", "-y"],
                       check=True, stdout=subprocess.DEVNULL)
        _purge_unsafe_extraction(extract_dir)
    elif shutil.which("unzip"):
        subprocess.run(["unzip", "-q", str(p), "-d", str(extract_dir)], check=True)
        _purge_unsafe_extraction(extract_dir)
    else:
        raise RuntimeError("No suitable decompression tool found (install 7z or unzip).")

    # Clean up archive to save space
    try:
        p.unlink()
        print(f"[✓] Cleaned up temporary archive {p.name}")
    except Exception:
        pass


# A folder counts as an installed game only if it holds something that runs: a Windows game or a Linux launcher
INSTALLED_GAME_SUFFIXES = {".exe", ".sh", ".x86_64", ".appimage"}


SKIPPED_MARK = ".deckstream-skipped.json"


def skipped_files(game_dir):
    """What cloud-install.sh left out of a rip install (other-language files, startup_profile.lang_skips):
    {"folder", "bytes", "files"} for the app's "Get skipped files" button, or None."""
    try:
        d = json.loads((Path(game_dir) / SKIPPED_MARK).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("folder"), str) or not d["folder"]:
        return None
    files = d.get("files") if isinstance(d.get("files"), list) else []
    return {"folder": d["folder"], "bytes": int(d.get("bytes") or 0), "files": len(files)}


def list_installed_games(target_root=None):
    roots = []
    if target_root:
        if isinstance(target_root, (list, tuple, set)):
            roots.extend([Path(r) for r in target_root])
        else:
            roots.append(Path(target_root))
    if not roots or roots == [GAMES_DIR_INTERNAL]:
        if GAMES_DIR_INTERNAL not in roots:
            roots.append(GAMES_DIR_INTERNAL)
        local_dd = HOME / ".local" / "share" / "deckstream" / "games"
        if local_dd not in roots:
            roots.append(local_dd)
        sd = get_micro_sd_path()
        if sd and sd.is_dir() and sd not in roots:
            roots.append(sd)
        try:
            for p in Path("/run/media").glob("*/*/Games"):
                if p.is_dir() and p not in roots:
                    roots.append(p)
            for p in Path("/run/media").glob("*/Games"):
                if p.is_dir() and p not in roots:
                    roots.append(p)
        except OSError:
            pass

    games = []
    seen_paths = set()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for d in root.iterdir():
                if d.is_dir() and not d.is_symlink() and not d.name.startswith("."):
                    res_path = str(d.resolve())
                    if res_path in seen_paths:
                        continue
                    seen_paths.add(res_path)
                    size, playable = 0, False
                    try:
                        for f in d.rglob("*"):
                            if f.is_file():
                                size += f.stat().st_size
                                playable = playable or f.suffix.lower() in INSTALLED_GAME_SUFFIXES
                    except (OSError, ValueError):
                        pass
                    # An empty folder left by a cancelled or removed install, or an unpacked readme (a .docx is a zip),
                    # is not a game: the phone listed four such "installed games" (17 Oct)
                    if not playable:
                        continue
                    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", d.name).lower().strip("_")[:80] or "game"
                    g = {
                        "name": d.name,
                        "slug": slug,
                        "path": res_path,
                        "size": size,
                        "kind": "installed"
                    }
                    sk = skipped_files(d)
                    if sk:
                        g["skipped"] = sk
                    games.append(g)
        except OSError:
            continue
    return games


def report_job(server_url, device_id, job_id, status, progress, message, **extra):
    payload = {"action": "update_progress", "jobId": job_id, "status": status, "progress": progress, "message": message}
    payload.update(extra)
    try:
        r = queue_request(
            f"{server_url}/api/queue?device={device_id}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return {}


def deliver_rom_or_executable(game_name, dest_game_folder, job, server_url, device_id, job_id):
    """
    Finalizes an installed release folder:
    1. If Windows executables exist: creates a Proton game launcher and Steam shortcut.
    2. If Native Linux binaries exist: creates a native runner and Steam shortcut.
    3. Reports completion or failure status to the server.
    """
    exe_path = find_best_executable(dest_game_folder)
    if not exe_path:
        exes = list(dest_game_folder.rglob("*.exe"))
        exe_path = str(exes[0]) if exes else None

    if exe_path:
        print(f"[✓] Primary executable: {exe_path}")
        applied, app_id = request_shortcut(game_name, exe_path, job.get("artwork"))
        report_job(
            server_url, device_id, job_id, "completed", 100,
            (f"Installed! AppID: {app_id}. Restart Steam to play." if applied
             else "Installed! Close Steam (Desktop Mode: Steam > Exit) to add it to your library."),
            notifyTopic=job.get("notifyTopic", "")
        )
        if shutil.which("notify-send"):
            try:
                subprocess.run(["notify-send", f"🎮 {game_name} Installed!", "Ready in Steam Gaming Mode."], timeout=10)
            except Exception:
                pass
        return True

    native = find_native_start(dest_game_folder)
    if native:
        print(f"[✓] Native Linux game: {native}")
        applied, app_id = request_shortcut(game_name, make_native_runner(game_name, native), job.get("artwork"))
        report_job(
            server_url, device_id, job_id, "completed", 100,
            (f"Installed (native Linux, no Proton)! AppID: {app_id}. Restart Steam to play." if applied
             else "Installed (native Linux, no Proton)! Close Steam (Desktop Mode: Steam > Exit) to add it to your library."),
            notifyTopic=job.get("notifyTopic", "")
        )
        return True

    print("[!] No playable .exe executable found in game folder.")
    report_job(server_url, device_id, job_id, "failed", 0, "No playable .exe executable detected in game folder.")
    return False
    return False


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


_cloud_rate = {}   # job id -> (first sample time, last sample time, last bytes) for the MB/s and elapsed in the progress line
INSTALL_PAUSED_LINE = "Paused: plug in to continue"


def report_cloud_progress(server_url, device_id, job_id, dest, release_size, last_line, now=None):
    """Reports what is really known: GB written to the game folder, the write rate since the last report, and how long it
    has run (the phone and the Decky panel show the message). The bar is still bytes written vs the release size,
    capped at 95 until the job finishes (unpacked games are usually bigger than the release)."""
    now = time.time() if now is None else now
    written = folder_size(dest)
    first, last_t, last_b = _cloud_rate.get(job_id, (now, now, written))
    rate = (written - last_b) / (now - last_t) / 1e6 if now > last_t else 0.0
    _cloud_rate[job_id] = (first, now, written)
    mins = int(now - first) // 60
    elapsed = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
    pct = min(95, round(written / release_size * 100)) if release_size else 5
    if last_line == INSTALL_PAUSED_LINE:
        msg = f"{INSTALL_PAUSED_LINE} ({written / 1e9:.1f} GB written so far)"
    else:
        msg = f"{written / 1e9:.1f} GB written, {max(rate, 0):.0f} MB/s, {elapsed} elapsed - {last_line}"
    res = report_job(server_url, device_id, job_id, "installing", pct, msg)
    if isinstance(res, dict) and (res.get("cancelled") or res.get("cancelledJobId") == job_id):
        raise InterruptedError("Cancelled by user")


INSTALL_CHARGER_ONLY = HOME / ".config" / "deckstream" / "install.charger_only"   # opt-in: installs run only on the charger


class ChargerGate:
    """Opt-in (touch ~/.config/deckstream/install.charger_only): pause an install off the charger. Off for two readings in
    a row pauses (the user's charger was seen flapping, like stop_fills_off_charger); one reading on resumes."""

    def __init__(self, ac=None, enabled=None):
        self.ac = ac or on_ac_power
        self.enabled = enabled or INSTALL_CHARGER_ONLY.exists
        self.off = 0

    def should_pause(self):
        if not self.enabled() or self.ac():
            self.off = 0
            return False
        self.off += 1
        return self.off >= 2


def install_argv(script, slug, webdav_folder, dest, which=shutil.which):
    """The installer runs at the lowest CPU and disk priority, so the Deck stays usable while an installer unpacks for hours."""
    argv = []
    if which("nice"):
        argv += ["nice", "-n", "19"]
    if which("ionice"):
        argv += ["ionice", "-c3"]
    return argv + ["bash", str(script), slug, webdav_folder, str(dest)]


def hold_awake(pid, why, which=shutil.which):
    """Keep the Deck from sleeping (or idling into sleep) while process `pid` runs: a systemd-inhibit lock held by a
    `tail --pid` that ends with the install, so the lock goes away on success, failure or a daemon crash. None when
    systemd-inhibit is missing; if it is refused (no polkit) it exits at once and nothing changes."""
    if not which("systemd-inhibit") or not which("tail"):
        return None
    try:
        return subprocess.Popen(
            ["systemd-inhibit", "--what=sleep:idle", "--who=DeckStream", f"--why={why}", "--mode=block",
             "tail", f"--pid={pid}", "-f", "/dev/null"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


def release_awake(holder):
    if holder is None or holder.poll() is not None:
        return
    try:
        os.killpg(holder.pid, signal.SIGTERM)
    except OSError:
        holder.terminate()
    try:
        holder.wait(timeout=5)
    except subprocess.TimeoutExpired:
        holder.kill()


def run_cloud_install(script, slug, webdav_folder, dest, on_tick, size_fn=None, stall_seconds=2700, tick_seconds=15,
                      awake_why=None, paused_fn=None, keep_all=False):
    """Run cloud-install.sh and keep the queue informed. Returns (ok, message).

    If neither the output nor the size of the destination folder changes for `stall_seconds`, the
    installer (and everything it started) is stopped instead of hanging forever. `awake_why` holds a sleep lock for the
    whole run. `paused_fn` (ChargerGate) returning True stops the installer's process group until it returns False;
    time spent paused never counts as a stall. `keep_all` ("Get skipped files") copies the files a rip install left out.
    """
    env = {**os.environ, "DECKSTREAM_INSTALL_ALL": "1"} if keep_all else None
    proc = subprocess.Popen(
        install_argv(script, slug, webdav_folder, dest),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True, env=env,
    )
    holder = hold_awake(proc.pid, awake_why) if awake_why else None
    state = {"last": "Starting...", "error": None, "done": False, "lines": 0}

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            print(f"    | {line}")
            state["lines"] += 1
            if line.startswith("DECKSTREAM_ERROR:"):
                state["error"] = line.split(":", 1)[1].strip()
            elif line == "DECKSTREAM_DONE":
                state["done"] = True
            elif not line.startswith("DECKSTREAM_KIND="):
                state["last"] = line[:200]

    def stop(wait_s):
        for sgn in (signal.SIGCONT, signal.SIGTERM, signal.SIGKILL):   # a paused (stopped) group must run to see TERM
            try:
                os.killpg(proc.pid, sgn)
            except OSError:
                break
            if sgn == signal.SIGCONT:
                continue
            try:
                proc.wait(timeout=wait_s)
                break
            except subprocess.TimeoutExpired:
                continue
        t.join(timeout=wait_s)
        release_awake(holder)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    # Long silent stretches (an installer can run for hours) must not look like a dead Deck.
    last_sig = (size_fn() if size_fn else None, state["lines"])
    last_change = time.time()
    paused = False
    while proc.poll() is None:
        time.sleep(tick_seconds)
        want = bool(paused_fn and paused_fn())
        if want != paused and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGSTOP if want else signal.SIGCONT)
                paused = want
                print("[*] Install paused: the Deck is off the charger" if want else "[*] Install resumed: back on the charger")
            except OSError:
                pass
        try:
            on_tick(INSTALL_PAUSED_LINE if paused else state["last"])
        except InterruptedError:
            print("[!] Cloud install cancelled by user. Terminating process group...")
            stop(5)
            return False, "Cancelled by user"
        if paused:
            last_change = time.time()
            continue
        sig = (size_fn() if size_fn else None, state["lines"])
        if sig != last_sig:
            last_sig, last_change = sig, time.time()
        elif time.time() - last_change > stall_seconds:
            stop(10)
            return False, f"Stalled: nothing new was written for {stall_seconds // 60} minutes, so it was stopped"
    t.join(timeout=5)
    release_awake(holder)
    if proc.returncode == 0 and state["done"]:
        return True, "Installed"
    return False, state["error"] or f"Installer exited with code {proc.returncode}"


def fail_job(server_url, device_id, job_id, message):
    print(f"[!] {message}")
    try:
        r = queue_request(
            f"{server_url}/api/queue?device={device_id}",
            data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": message}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(r, timeout=10)
    except Exception:
        pass


def resumed_from_sleep(t_before, t_after, requested_s, slack_s=60):
    """True when a sleep of `requested_s` seconds took `slack_s` longer by the WALL clock: on Linux time.sleep() uses the monotonic
    clock, which does not run during a suspend, so a Deck that slept in the middle of our sleep shows a big wall-clock gap."""
    return (t_after - t_before) > requested_s + slack_s


def run_resume_hook(timeout_s=20):
    """After a resume: probe the cloud mounts and lazily unmount a hung one (daemon/suspend-resume-hook.sh post). Bounded, never raises."""
    hook = Path.home() / ".config" / "deckstream" / "suspend-resume-hook.sh"
    if not hook.is_file():
        return False
    try:
        subprocess.run(["bash", str(hook), "post"], timeout=timeout_s, capture_output=True)
        return True
    except Exception:
        return False


def idle_poll_interval(base, idle_polls):
    """Seconds to wait before the next job poll. Right after a job (or a fresh start) the base interval keeps 'Send to Deck' snappy;
    a Deck that has sat idle backs off (every poll is a serverless call plus Redis commands on the free Vercel/Upstash plans:
    one Deck polling every 10 s all month is ~345k calls with its heartbeat). The first idle polls are 10 s apart, after ~5 minutes
    20 s, after ~30 minutes 30 s. A job resets the count (idle_polls = 0)."""
    if idle_polls < 30:
        return base
    if idle_polls < 180:
        return max(base, 20)
    return max(base, 30)
