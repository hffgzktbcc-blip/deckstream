"""The background janitor and warm-up passes that tie the other parts together.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import time
import json
import subprocess
import threading

from ds_stream import (PRESTAGE_DIR, clean_stale_stream_mounts, fills_blocked, finish_stream_sessions, on_ac_power,
    running_fills, stop_fills_off_charger)
from ds_cache import (WARMUP_FLAG, _warmup_note, backup_profiles, evict_old_caches, newgame_candidates,
    prestage_enabled, start_prestage, warmup_candidates)
from ds_update import self_update_pass, update_decky_plugin_if_needed
from ds_status import send_launch_reports


def warmup_pass(proc_root="/proc", spawn=subprocess.Popen, script=None, ac=None, now=None):
    """Called by the janitor. When no game plays and no fill runs: first start a prestage that had to wait (the game
    was queued during play), then, if overnight warm-up is on (warmup.enabled) and the Deck is charging, fill the
    next recently played game's whole profile (start-up + later) so its next launch reads nothing from RemoteStorage.
    Returns the slug it started, or None."""
    if not prestage_enabled():
        return None
    stop_fills_off_charger(proc_root, ac=ac)
    if running_fills(proc_root) or fills_blocked(proc_root):
        return None

    def mtime(p):           # a pending file can be consumed between glob() and stat() by the job thread
        try:
            return p.stat().st_mtime
        except OSError:
            return 0
    pendings = sorted(PRESTAGE_DIR.glob("*.pending.json"), key=mtime) if PRESTAGE_DIR.is_dir() else []
    for p in pendings:
        slug = p.name[:-len(".pending.json")]
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            p.unlink(missing_ok=True)
            continue
        p.unlink(missing_ok=True)
        if start_prestage(slug, d.get("folder", ""), d.get("hot"), spawn=spawn, script=script, proc_root=proc_root):
            return slug
    if not WARMUP_FLAG.exists():
        return None
    if not (on_ac_power() if ac is None else ac):
        # Say once per change why nothing is warming (it was not clear from the log whether the charger was in).
        if not _warmup_note.get("waiting") and (warmup_candidates(now) or newgame_candidates(now)):
            print("[*] Overnight warm-up: games are waiting, but the Deck is on battery; it starts on the charger")
            _warmup_note["waiting"] = True
        return None
    _warmup_note["waiting"] = False
    evict_old_caches(proc_root=proc_root)
    for slug, folder in warmup_candidates(now):
        PRESTAGE_DIR.mkdir(parents=True, exist_ok=True)
        (PRESTAGE_DIR / f"{slug}.tried").touch()
        if start_prestage(slug, folder, spawn=spawn, script=script, proc_root=proc_root, overnight=True):
            return slug
    for slug, folder, hot in newgame_candidates(now):
        PRESTAGE_DIR.mkdir(parents=True, exist_ok=True)
        (PRESTAGE_DIR / f"{slug}.tried").touch()
        if start_prestage(slug, folder, hot, spawn=spawn, script=script, proc_root=proc_root, overnight=True, whole=True):
            return slug
    return None


def janitor_pass():
    """Clear dead stream mounts and finish ended stream sessions. Never raises."""
    try:
        clean_stale_stream_mounts()
    except Exception as e:
        print(f"[!] Stale-mount check failed: {e}")
    try:
        finish_stream_sessions()
    except Exception as e:
        print(f"[!] Finishing stream sessions failed: {e}")
    try:
        warmup_pass()
    except Exception as e:
        print(f"[!] Background warm-up check failed: {e}")
    try:
        backup_profiles()
    except Exception as e:
        print(f"[!] Profile backup failed: {e}")
    try:
        send_launch_reports()
    except Exception as e:
        print(f"[!] Launch report failed: {e}")
    try:
        self_update_pass()
    except Exception as e:
        print(f"[!] Self-update check failed: {e}")
    try:
        update_decky_plugin_if_needed()
    except Exception as e:
        print(f"[!] Decky plugin update check failed: {e}")


def start_janitor(interval=20, pass_fn=None, sleep=time.sleep):
    """Run the janitor on its own thread. It used to run only between jobs, so a stream session played while an
    install job ran (which can take an hour) waited that long for its save backup and cache repair."""
    pass_fn = pass_fn or janitor_pass

    def loop():
        while True:
            pass_fn()
            sleep(interval)

    t = threading.Thread(target=loop, name="deckstream-janitor", daemon=True)
    t.start()
    return t
