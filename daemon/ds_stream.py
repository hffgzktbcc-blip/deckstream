"""Stream sessions, running cache fills and the charger/power checks that gate them.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import errno
import os
import sys
import time
import json
import shutil
import subprocess
import signal
from pathlib import Path

from ds_core import HOME


SESSIONS_DIR = HOME / ".local" / "share" / "deckstream" / "sessions"
STARTUP_PROFILE = HOME / ".config" / "deckstream" / "startup_profile.py"
_finishing = {}
PRESTAGE_DIR = HOME / ".local" / "share" / "deckstream" / "prestage"


POWER_SUPPLY_DIR = Path("/sys/class/power_supply")


def on_ac_power(root=None):
    """True when a mains supply is online (the Deck: ACAD/online = 1 while charging). Battery status is not used: it
    can read 'Full' or 'Not charging' on the charger."""
    root = Path(root) if root else POWER_SUPPLY_DIR
    try:
        supplies = list(root.iterdir())
    except OSError:
        return False
    for d in supplies:
        try:
            if (d / "type").read_text().strip() == "Mains" and (d / "online").read_text().strip() == "1":
                return True
        except OSError:
            continue
    return False


def _cmdlines(proc_root="/proc"):
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if raw:
            yield pid, [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")]


def game_running(proc_root="/proc"):
    """True while a game plays or is starting: a stream runner, any rclone mount (a stream session, or a cloud install
    reading from RemoteStorage), or a game
    Steam started (Steam wraps every launch, non-Steam shortcuts included, in `reaper SteamLaunch AppId=...`). The Deck
    should not download in the background then: the game needs the line, the CPU and the battery."""
    for _pid, args in _cmdlines(proc_root):
        if os.path.basename(args[0]) == "rclone" and "mount" in args[1:3]:
            return True
        if "SteamLaunch" in args[1:4]:
            return True
        # the runner before it mounts (its own pre-mount fill writes this game's cache then)
        if any(os.path.basename(a) == "stream-runner.sh" for a in args[:3]):
            return True
    return False


def sessions_unfinished(max_age=6 * 3600, now=None):
    """True while a stream session's marker waits for its finisher (which rewrites rclone's range records in that
    game's cache, so no fill may write there meanwhile). A marker older than `max_age` no longer blocks fills."""
    now = now if now is not None else time.time()
    try:
        markers = list(SESSIONS_DIR.glob("*.json"))
    except OSError:
        return False
    for m in markers:
        try:
            if now - m.stat().st_mtime < max_age:
                return True
        except OSError:
            continue
    return False


def fills_blocked(proc_root="/proc"):
    return game_running(proc_root) or sessions_unfinished()


def running_fills(proc_root="/proc", prestage_dir=None):
    """Slugs whose background fill (prestage or overnight warm-up) is still running. A pid file whose process is gone,
    or is no longer ours (pids are reused after a reboot), does not count and is removed."""
    d = Path(prestage_dir) if prestage_dir else PRESTAGE_DIR
    out = []
    for pf in sorted(d.glob("*.pid")) if d.is_dir() else []:
        try:
            pid = pf.read_text().strip()
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
                ours = b"startup_profile.py" in f.read()
        except (OSError, ValueError):
            ours = False
        if ours:
            out.append(pf.name[:-len(".pid")])
        else:
            pf.unlink(missing_ok=True)
            (d / (pf.name[:-len(".pid")] + ".inhibit")).unlink(missing_ok=True)
    return out


_off_charger = {"passes": 0}


def stop_fills_off_charger(proc_root="/proc", ac=None, kill=os.killpg):
    """A fill started on the charger holds a sleep lock ('On battery the user's sleep always wins'). When the charger goes
    away, end those fills: an overnight warm-up resumes on the next charge (fill-cache only fetches what is missing), a
    queue-time prestage is queued again and restarts at once without the lock. Returns the slugs stopped."""
    if (on_ac_power() if ac is None else ac) or not PRESTAGE_DIR.is_dir():
        _off_charger["passes"] = 0
        return []
    # The user's charger was seen flapping: act only after two passes in a row off the charger (~40 s), so a blip
    # does not kill an hour-long fill.
    _off_charger["passes"] += 1
    if _off_charger["passes"] < 2:
        return []
    stopped = []
    for slug in running_fills(proc_root):
        mark = PRESTAGE_DIR / f"{slug}.inhibit"
        if not mark.exists():
            continue
        try:
            info = json.loads(mark.read_text())
            pid = int((PRESTAGE_DIR / f"{slug}.pid").read_text().strip())
            kill(pid, signal.SIGTERM)          # its own session: the whole group (systemd-inhibit, bash, fill-cache)
        except (OSError, ValueError):
            info = {}
        (PRESTAGE_DIR / f"{slug}.pid").unlink(missing_ok=True)
        mark.unlink(missing_ok=True)
        if not info.get("overnight") and info.get("folder"):
            (PRESTAGE_DIR / f"{slug}.pending.json").write_text(json.dumps({"folder": info["folder"], "hot": info.get("hot", [])}))
        stopped.append(slug)
        print(f"[*] Charger disconnected: stopped the background fill of '{slug}' so the Deck can sleep"
              + ("" if info.get("overnight") else "; it continues without keeping the Deck awake"))
    return stopped


def discard_failed_download(folder, temp_file, folder_existed):
    """Clean up after a failed download. Only a folder this job created is removed: re-queueing a game that is already
    installed (an update, a retry) downloads into its existing folder, and deleting that would take the installed
    game and any saves kept next to it."""
    folder, temp_file = Path(folder), Path(temp_file)
    if folder_existed:
        try:
            temp_file.unlink(missing_ok=True)
            temp_file.with_name(temp_file.name + ".parts").unlink(missing_ok=True)   # parallel_download's resume record
        except OSError:
            pass
    else:
        shutil.rmtree(folder, ignore_errors=True)


def rclone_using_cache(cache_dir, proc_root="/proc"):
    """True if a running rclone was started with --cache-dir CACHE_DIR. A game quit and started again within a few
    seconds has a NEW mount on the same cache folder while the old session's marker is still waiting to be finished;
    finishing it then would rewrite rclone's range records under the live mount and back up saves mid-game."""
    if not cache_dir:
        return False
    want = os.path.normpath(cache_dir)
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
                args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0")]
        except OSError:
            continue
        if not args or os.path.basename(args[0]) != "rclone":
            continue
        for i, a in enumerate(args):
            if (a == "--cache-dir" and i + 1 < len(args) and os.path.normpath(args[i + 1]) == want) \
                    or (a.startswith("--cache-dir=") and os.path.normpath(a.split("=", 1)[1]) == want):
                return True
    return False


def finish_stream_sessions(sessions_dir=None, script=None, spawn=subprocess.Popen, proc_root="/proc"):
    """The stream runner leaves a marker per session. Steam ends the runner's process tree right after the game
    exits, so the work that must follow (repair rclone's range records, merge older cache folders, build the
    start-up profile, back up saves) cannot live in the runner. The daemon is not part of that tree: it starts
    `startup_profile.py finish-session` for each marker, which waits (exit 3) while rclone is still running and
    removes the marker when it has finished. Returns the list of markers it started a finisher for."""
    d = Path(sessions_dir) if sessions_dir else SESSIONS_DIR
    script = Path(script) if script else STARTUP_PROFILE
    started = []
    if not script.exists():
        return started
    try:
        markers = sorted(d.glob("*.json"))
    except OSError:
        return started
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    for m in markers:
        proc = _finishing.get(m.name)
        if proc is not None and proc.poll() is None:
            continue                                   # a finisher for this session is still running
        try:
            cache_dir = json.loads(m.read_text()).get("cache_dir", "")
        except (OSError, ValueError, AttributeError):
            cache_dir = ""
        if rclone_using_cache(cache_dir, proc_root):
            continue                                   # a live mount uses this cache (quick relaunch): finish it later
        try:
            _finishing[m.name] = spawn([sys.executable, str(script), "finish-session", "--marker", str(m)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            started.append(m.name)
        except OSError:
            continue
    return started


STREAM_GAMES_DIR = HOME / ".local" / "share" / "deckstream" / "stream" / "games"


def stale_stream_mounts(base=None, stat=os.listdir):
    """Stream mount folders whose rclone process died (Steam kills the whole process tree when a game
    exits), which leaves a FUSE mount that fails with 'Transport endpoint is not connected'.
    The check LISTS the folder: stat() still succeeds on a dead mount because rclone mounts use a long
    attribute cache (--attr-timeout 24h), but reading the directory fails."""
    base = Path(base) if base else STREAM_GAMES_DIR
    stale = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return stale
    for d in entries:
        try:
            stat(d)
        except OSError as e:
            if getattr(e, "errno", None) == errno.ENOTCONN:
                stale.append(d)
    return stale


def clean_stale_stream_mounts(base=None, stat=os.listdir, run=subprocess.run):
    """Lazily unmount stale stream mounts. A live mount is never touched (it can be read)."""
    cleaned = []
    for d in stale_stream_mounts(base, stat):
        for tool in ("fusermount3", "fusermount"):
            try:
                if run([tool, "-uz", str(d)], capture_output=True, timeout=10).returncode == 0:
                    cleaned.append(d)
                    print(f"[*] Cleared a stale stream mount: {d.name}")
                    break
            except (OSError, subprocess.SubprocessError):
                continue
    return cleaned
