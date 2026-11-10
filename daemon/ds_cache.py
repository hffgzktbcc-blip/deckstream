"""Stream caches: prestage, start-up profile backup, warm-up, Deck mode, eviction and removing a game's data.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import hashlib
import math
import os
import sys
import time
import json
import shutil
import subprocess
import re
import urllib.request
from pathlib import Path

import ds_core
from ds_core import HOME, find_steam_userdata_dir, get_micro_sd_path, queue_request
from ds_steam import parse_binary_vdf, serialize_binary_vdf
from ds_shortcuts import LIVE_REGISTRY, _cef_target, _ws_eval
from ds_stream import PRESTAGE_DIR, STARTUP_PROFILE, fills_blocked, on_ac_power, rclone_using_cache, running_fills


# ---- prestage: fill a stream game's cache when it is QUEUED, so the first Play is already warm ---------------------
PROFILES_DIR = HOME / ".local" / "share" / "deckstream" / "profiles"
STREAM_NS_FILE = HOME / ".config" / "deckstream" / "stream_ns"
STREAM_CACHE_BASE_FILE = HOME / ".config" / "deckstream" / "stream_cache_base"
PRESTAGE_FLAG = HOME / ".config" / "deckstream" / "prestage.enabled"


def prestage_enabled():
    return PRESTAGE_FLAG.exists() or os.environ.get("DECKSTREAM_PRESTAGE") == "1"


def startup_gb(slug, profiles_dir=None):
    """The recorded start-up set of a game in GB (the part the game reads before its menu), 0 if there is no profile."""
    f = (Path(profiles_dir) if profiles_dir else PROFILES_DIR) / f"{slug}.json"
    try:
        return float(json.loads(f.read_text()).get("total_bytes") or 0) / 1e9
    except (OSError, ValueError, AttributeError):
        return 0.0


def later_gb(slug, profiles_dir=None):
    """What the game read after its start-up set (in-world loading, the profile's `later` part) in GB, 0 if none."""
    f = (Path(profiles_dir) if profiles_dir else PROFILES_DIR) / f"{slug}.json"
    try:
        return float(json.loads(f.read_text()).get("later_bytes") or 0) / 1e9
    except (OSError, ValueError, AttributeError):
        return 0.0


def prestage_limit_gb(slug, profiles_dir=None):
    """Queue-time prestage size: the whole profile (+5 %), start-up set AND the later in-world part, when it is known
    (Cyberpunk start-up: 12.9 GB), at least 8 GB, at most 45 GB. fill-cache fetches start-up first, so the later part
    only ever adds to the fill. The free-space share in _prestage_cap_gb still applies (a full card gets less)."""
    return min(45, max(8, math.ceil((startup_gb(slug, profiles_dir) + later_gb(slug, profiles_dir)) * 1.05)))


def _prestage_cap_gb(cache_base, free_fn=shutil.disk_usage, limit=8, share=0.25):
    """At most `limit` GB (8 for a queue-time prestage), and never more than `share` of the card's free space. The
    stream runner's cache cap is 60% of (free + this game's cache), so staying under that share never makes rclone
    evict at mount what was just filled."""
    try:
        free_gb = free_fn(str(cache_base)).free / 1e9
    except OSError:
        return 0
    return max(0, min(limit, int(free_gb * share)))


_INSTALLER_OR_ARCHIVE_NAME_RE = re.compile(
    r"((?:^|[\W_])(installer|archive)(?:[\W_]|$)|(\.(zip|rar|7z|iso)($|[\W_])))", re.I
)

_ARCHIVE_RANGE_RE = re.compile(r"(\.(zip|rar|7z|r\d{2}|part\d+\.rar)$|(^|/)(setup[^/]*-\d+|setup-?\d*)\.bin$)", re.I)


def _is_streamable_release(slug, folder="", plan=None, prestage_dir=None, profiles_dir=None):
    """True only when a game is streamable (kind == 'plain' and not an installer/archive folder or failed archive launch)."""
    if isinstance(plan, dict):
        pk = str(plan.get("kind") or "").strip().lower()
        if pk and pk != "plain":
            return False
    if _INSTALLER_OR_ARCHIVE_NAME_RE.search(f"{slug or ''} {folder or ''}"):
        return False
    d = Path(prestage_dir) if prestage_dir else PRESTAGE_DIR
    pd = Path(profiles_dir) if profiles_dir else PROFILES_DIR
    for meta_file in (d / f"{slug}.newgame.json", d / f"{slug}.pending.json", pd / f"{slug}.json"):
        try:
            meta = json.loads(meta_file.read_text())
            if isinstance(meta, dict):
                mk = str(meta.get("kind") or "").strip().lower()
                if mk and mk != "plain":
                    return False
        except (OSError, ValueError):
            pass
    starter = d / f"{slug}.starter.json"
    try:
        st_data = json.loads(starter.read_text())
        if isinstance(st_data, dict):
            for r in st_data.get("ranges") or []:
                if isinstance(r, dict) and _ARCHIVE_RANGE_RE.search(str(r.get("f") or "")):
                    return False
    except (OSError, ValueError):
        pass
    stream_log = HOME / ".local" / "share" / "deckstream" / "logs" / f"stream_{slug}.log"
    try:
        if "can't be streamed" in stream_log.read_text(errors="ignore"):
            return False
    except OSError:
        pass
    return True


def start_prestage(slug, webdav_folder, hot=None, spawn=subprocess.Popen, script=None, proc_root="/proc",
                   overnight=False, whole=False, plan=None):
    """Start filling this game's stream cache in the background (exact byte ranges, no rclone mount), using its
    start-up profile if it has one, else a starter profile built from the plan's start-up files and the edges of big
    archives. Needs the cache namespace and cache folder the stream runner recorded on an earlier mount; skipped
    otherwise. The stream runner stops it (and any other fill) before mounting. Returns the pid or None.
    Only one fill runs at a time, and none while a game plays: the request is then kept as pending and the janitor's
    warmup_pass starts it later. `overnight` fills the whole profile with a bigger cap (the Deck is charging and idle)
    and holds a sleep inhibitor while it runs. `whole` (a game with no recorded profile, overnight on the charger)
    fetches the WHOLE release, smallest files first, up to a cap of min(45 GB, a quarter of the free space), so the first
    launch of a never-played game is warm too."""
    script = Path(script) if script else STARTUP_PROFILE
    if not prestage_enabled() or not script.exists():
        return None
    if not _is_streamable_release(slug, webdav_folder, plan=plan):
        return None
    if not _has_real_profile(slug) and webdav_folder and not whole:
        # Remember a game queued before it was ever played, so an idle night on the charger can warm all of it.
        try:
            PRESTAGE_DIR.mkdir(parents=True, exist_ok=True)
            (PRESTAGE_DIR / f"{slug}.newgame.json").write_text(
                json.dumps({"folder": webdav_folder, "hot": [h for h in (hot or []) if isinstance(h, str)][:200]}))
        except OSError:
            pass
    running = running_fills(proc_root)
    if slug in running:
        return None
    if running or fills_blocked(proc_root):
        if not overnight:
            PRESTAGE_DIR.mkdir(parents=True, exist_ok=True)
            pending = {"folder": webdav_folder, "hot": [h for h in (hot or []) if isinstance(h, str)][:200]}
            (PRESTAGE_DIR / f"{slug}.pending.json").write_text(json.dumps(pending))
            print(f"[*] Prestage of '{slug}' waits until no game is playing and no other fill runs")
        return None
    try:
        ns = STREAM_NS_FILE.read_text().strip()
        cache_base = Path(STREAM_CACHE_BASE_FILE.read_text().strip())
    except OSError:
        return None
    if not ns or not cache_base.is_dir():
        return None
    if whole:
        cap = _prestage_cap_gb(cache_base, limit=45, share=0.25)   # 25% of free space: the card stays above the 15% clearing line
    else:
        cap = _prestage_cap_gb(cache_base, limit=40, share=0.4) if overnight else _prestage_cap_gb(cache_base, limit=prestage_limit_gb(slug))
    if cap < 1:
        return None
    PRESTAGE_DIR.mkdir(parents=True, exist_ok=True)
    (PRESTAGE_DIR / f"{slug}.pending.json").unlink(missing_ok=True)
    cache_dir = cache_base / slug
    profile = PROFILES_DIR / f"{slug}.json"
    log = HOME / ".local" / "share" / "deckstream" / "logs" / f"prestage_{slug}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    py = [sys.executable, str(script)]
    steps = []
    if not _has_real_profile(slug):
        # A game never played here: build a starter profile first (start-up files + archive edges).
        hot_file = PRESTAGE_DIR / f"{slug}.hot.json"
        hot_file.write_text(json.dumps([h for h in (hot or []) if isinstance(h, str)][:200]))
        profile = PRESTAGE_DIR / f"{slug}.starter.json"
        syn = py + ["synth-profile", "--folder", webdav_folder, "--hot-file", str(hot_file), "--out", str(profile)]
        if whole:
            syn += ["--whole-gb", str(cap)]
        steps.append(syn)
    steps.append(py + ["fill-cache", "--cache-dir", str(cache_dir), "--ns", ns, "--folder", webdav_folder,
                       "--profile", str(profile), "--workers", "8", "--max-gb", str(cap), "--log", str(log), "--include-later"])
    if shutil.which("notify-send"):
        nice_title = slug.replace("_", " ").strip().title()
        steps.append(["bash", "-c", f"notify-send '🎮 {nice_title}' 'Stream cache ready to play!' 2>/dev/null || true"])
    # One detached shell runs the steps in order at low priority, outside the daemon and outside Steam.
    import shlex
    cmd = " && ".join(" ".join(shlex.quote(a) for a in s) for s in steps)
    if overnight:
        # Remember the profile this fill completed, so the next night skips a game whose profile has not changed.
        stamp = "whole" if whole else _profile_signature(profile)
        cmd += " && printf %s " + shlex.quote(stamp) + " > " + shlex.quote(str(_warm_stamp(slug)))
    argv = ["nice", "-n", "10", "bash", "-c", f"exec {cmd}" if len(steps) == 1 and not overnight else cmd]
    # Keep the Deck awake while charging (a fill stalls while it sleeps). On battery the user's sleep always wins.
    inhibit = on_ac_power() and shutil.which("systemd-inhibit")
    if inhibit:
        argv = ["systemd-inhibit", "--what=sleep", "--who=DeckStream", "--why=Filling a game's stream cache",
                "--mode=block"] + argv
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    try:
        proc = spawn(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError:
        return None
    (PRESTAGE_DIR / f"{slug}.pid").write_text(str(proc.pid))
    if inhibit:
        # This fill holds the sleep lock; stop_fills_off_charger ends it if the charger goes away.
        (PRESTAGE_DIR / f"{slug}.inhibit").write_text(json.dumps(
            {"overnight": bool(overnight), "folder": webdav_folder, "hot": [h for h in (hot or []) if isinstance(h, str)][:200]}))
    what = "Warming all of" if whole else ("Warming" if overnight else "Prestaging")
    print(f"[*] {what} '{slug}' in the background (up to {cap} GB) so the next launch starts warm")
    return proc.pid


# ---- profile backup: a start-up profile (which byte ranges a release's game reads, and when) is kept on the user's
# own DeckStream site too, so a new SD card or a reinstall does not start every game cold again. Private (queue
# password), keyed by a hash of the release folder name; only paths and offsets inside that release, no game data.
PROFILE_UPLOADS_DIR = HOME / ".local" / "share" / "deckstream" / "profile_uploads"
PROFILE_MAX_RANGES = 20000


def profile_key(folder):
    return hashlib.sha256(str(folder).encode("utf-8")).hexdigest()


def _safe_rel_path(v):
    return (isinstance(v, str) and 0 < len(v) <= 400 and not v.startswith("/") and "\0" not in v and "\\" not in v
            and all(p not in ("", ".", "..") for p in v.split("/")))


def sanitize_profile(p, folder):
    """A profile from the network, checked like the server checks it, for THIS release only; None if anything is off.
    Its paths are joined to the cache folder by fill-cache, so an escaping path must never get through."""
    if not isinstance(p, dict) or p.get("folder") != folder or not _safe_rel_path(folder):
        return None
    out = {"version": 1, "folder": folder}
    for part in ("ranges", "later", "demand"):
        if part == "demand" and "demand" not in p:
            continue
        rs = p.get(part, [])
        if not isinstance(rs, list) or len(rs) > PROFILE_MAX_RANGES:
            return None
        clean = []
        for r in rs:
            if not isinstance(r, dict) or not _safe_rel_path(r.get("f")):
                return None
            o, n, t = r.get("o"), r.get("n"), r.get("t", 0)
            if type(o) is not int or type(n) is not int or not (0 <= o <= 2 ** 50) or not (0 < n <= 2 ** 40):
                return None
            t = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t <= 1e7 else 0.0
            clean.append({"t": t, "f": r["f"], "o": o, "n": n})
        if part != "demand" or clean:
            out[part] = clean
    if not out["ranges"] and not out["later"]:
        return None
    for k in ("runs", "last_added_bytes", "later_bytes", "total_bytes", "early_fill_bytes"):
        if type(p.get(k)) is int and p[k] >= 0:
            out[k] = p[k]
    if isinstance(p.get("stable"), bool):
        out["stable"] = p["stable"]
    return out


def restore_profile(slug, folder, opener=None):
    """If this game has no local profile, fetch the saved one for its release. Returns True when one was restored."""
    dest = PROFILES_DIR / f"{slug}.json"
    if dest.exists() or not ds_core.SERVER_URL or not folder:
        return False
    opener = opener or urllib.request.urlopen
    url = f"{ds_core.SERVER_URL}/api/profile?key={profile_key(folder)}"
    try:
        with opener(queue_request(url), timeout=10) as r:
            body = json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))
    except Exception:
        return False          # 404 (never saved), offline, old site: the game simply starts as before
    prof = sanitize_profile((body or {}).get("profile"), folder)
    if not prof:
        print(f"[!] Saved start-up profile for '{slug}' was not valid; ignored")
        return False
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prof))
    os.replace(tmp, dest)
    # It came from the site, so it need not be uploaded again until it grows.
    PROFILE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_UPLOADS_DIR / slug).write_text(_profile_signature(dest))
    print(f"[+] Restored '{slug}' start-up profile from your DeckStream site ({len(prof['ranges'])} ranges)")
    return True


def backup_profiles(opener=None, limit=1):
    """Upload profiles that changed since their last upload (at most `limit` per call; the janitor calls this every
    20 s). A failed upload is retried no sooner than an hour later. Returns the slugs uploaded."""
    if not ds_core.SERVER_URL or not PROFILES_DIR.is_dir():
        return []
    opener = opener or urllib.request.urlopen
    PROFILE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    done = []
    for p in sorted(PROFILES_DIR.glob("*.json")):
        slug, sig = p.stem, _profile_signature(p)
        stamp, failed = PROFILE_UPLOADS_DIR / slug, PROFILE_UPLOADS_DIR / f"{slug}.failed"
        try:
            if stamp.read_text() == sig:
                continue
        except OSError:
            pass
        try:
            if time.time() - failed.stat().st_mtime < 3600:
                continue
        except OSError:
            pass
        try:
            raw = json.loads(p.read_text())
            prof = sanitize_profile(raw, raw.get("folder")) if isinstance(raw, dict) else None
        except (OSError, ValueError):
            prof = None
        if not prof or raw.get("synthetic"):
            stamp.write_text(sig)          # nothing worth keeping; don't look again until it changes
            continue
        data = json.dumps({"key": profile_key(prof["folder"]), "profile": prof}).encode("utf-8")
        try:
            with opener(queue_request(f"{ds_core.SERVER_URL}/api/profile", data=data, headers={"Content-Type": "application/json"}),
                        timeout=15) as r:
                r.read(4096)
            stamp.write_text(sig)
            failed.unlink(missing_ok=True)
            done.append(slug)
        except Exception as e:
            failed.touch()
            print(f"[!] Could not back up the start-up profile of '{slug}': {e}")
        if len(done) >= limit:
            break
    return done


WARMUP_FLAG = HOME / ".config" / "deckstream" / "warmup.enabled"
WARMUP_APPLIED = HOME / ".config" / "deckstream" / "warmup.applied"   # ms time of the last on/off choice applied here
WARMUP_RECENT_DAYS = 30        # games played in the last month
WARMUP_MAX_GAMES = 4
WARMUP_RETRY_SECONDS = 30 * 60   # fill-cache only fetches what is missing, so a retry (after a game or the charger
                                 # interrupted it, or a game removed from RemoteStorage) is cheap
_warmup_note = {}


def warmup_on():
    """The overnight warm-up switch as the phone and the Decky panel show it: on only when both flag files exist (the
    warm-up runs inside the prestage pass, so warmup.enabled alone does nothing)."""
    return WARMUP_FLAG.exists() and prestage_enabled()


def warmup_applied_at():
    try:
        return int(WARMUP_APPLIED.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def set_warmup(on, at_ms=None):
    """Turn the overnight warm-up on (both flag files) or off (warmup.enabled only: prestage stays as it was), and
    remember when, so an older choice from the phone is not applied over a newer one made on the Deck."""
    WARMUP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    if on:
        PRESTAGE_FLAG.touch()
        WARMUP_FLAG.touch()
    else:
        WARMUP_FLAG.unlink(missing_ok=True)
    tmp = WARMUP_APPLIED.with_name(WARMUP_APPLIED.name + ".tmp")
    tmp.write_text(str(int(at_ms if at_ms is not None else time.time() * 1000)))
    os.replace(tmp, WARMUP_APPLIED)   # replace, not rewrite: the Decky backend (root) may have made the old file


def apply_warmup_wanted(wanted):
    """The phone's choice from the heartbeat reply ({"on": bool, "at": ms}). Applied once, and only when it is newer
    than the last choice made here. Returns True when the switch was changed."""
    if not isinstance(wanted, dict) or not isinstance(wanted.get("on"), bool):
        return False
    try:
        at = int(wanted.get("at") or 0)
    except (TypeError, ValueError):
        return False
    if at <= warmup_applied_at():
        return False
    set_warmup(wanted["on"], at)
    print(f"[*] Overnight warm-up turned {'on' if wanted['on'] else 'off'} from the phone")
    return True


# --- Deck mode: the per-game "Deck mode: 30 fps, low graphics" switch (phone and Decky, off by default) -------------
# On = ~/.config/deckstream/deck_mode/<slug>.env, which both launchers' game_env block reads (stream and installed games).
# deck_mode.json keeps {slug: {"on", "at"}} so an older choice still waiting on the site never undoes a newer one.
DECK_MODE_DIR = HOME / ".config" / "deckstream" / "deck_mode"
DECK_MODE_STATE = HOME / ".config" / "deckstream" / "deck_mode.json"
DECK_MODE_ENV = "DECKSTREAM_FPS_CAP=30\nDECKSTREAM_LOW_PRESET=1\n"
DECK_MODE_MAX = 200


def deck_mode_state():
    """{slug: {"on": bool, "at": ms}} for every game a choice was made for, as the heartbeat reports it."""
    try:
        raw = json.loads(DECK_MODE_STATE.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for slug, v in (raw.items() if isinstance(raw, dict) else []):
        if _REMOVE_SLUG_RE.match(str(slug)) and isinstance(v, dict) and isinstance(v.get("on"), bool):
            out[slug] = {"on": (DECK_MODE_DIR / f"{slug}.env").is_file(), "at": int(v.get("at") or 0)}
    return out


def set_deck_mode(slug, on, at_ms=None):
    """Write or remove the game's Deck mode file and remember when. Returns False for a slug that is not a plain name."""
    if not _REMOVE_SLUG_RE.match(str(slug or "")):
        return False
    DECK_MODE_DIR.mkdir(parents=True, exist_ok=True)
    f = DECK_MODE_DIR / f"{slug}.env"
    if on:
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(DECK_MODE_ENV)
        os.replace(tmp, f)
    else:
        f.unlink(missing_ok=True)
    st = deck_mode_state()
    st[slug] = {"on": bool(on), "at": int(at_ms if at_ms is not None else time.time() * 1000)}
    keep = dict(sorted(st.items(), key=lambda kv: -kv[1]["at"])[:DECK_MODE_MAX])
    tmp = DECK_MODE_STATE.with_name(DECK_MODE_STATE.name + ".tmp")
    tmp.write_text(json.dumps(keep))
    os.replace(tmp, DECK_MODE_STATE)   # replace, not rewrite: the Decky backend (root) may have made the old file
    return True


def apply_deck_mode_wanted(wanted):
    """The phone's choices from the heartbeat reply ({slug: {"on", "at"}}). Each is applied once, and only when newer
    than the last choice made here for that game. Returns how many were changed."""
    if not isinstance(wanted, dict):
        return 0
    have = deck_mode_state()
    changed = 0
    for slug, w in list(wanted.items())[:DECK_MODE_MAX]:
        if not isinstance(w, dict) or not isinstance(w.get("on"), bool):
            continue
        try:
            at = int(w.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if at <= have.get(slug, {}).get("at", 0):
            continue
        if set_deck_mode(slug, w["on"], at):
            changed += 1
            print(f"[*] Deck mode {'on' if w['on'] else 'off'} for {slug} from the phone")
    return changed


def _warm_stamp(slug):
    return PRESTAGE_DIR / f"{slug}.warmed"


def _profile_signature(profile):
    try:
        st = Path(profile).stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def _in_library(slug, launchers_dir=None):
    """True while the game is still on this Deck: its stream launcher exists (Remove deletes it). Profiles and games.json
    outlive a removal on purpose (a re-added game starts warm), so the warm-up must not take them as "still here": on 17 Oct
    it re-downloaded games removed that afternoon (Elden Ring up to 45 GB, BG3 40 GB, 007 First Light 37 GB)."""
    d = Path(launchers_dir) if launchers_dir else HOME / ".local" / "share" / "deckstream" / "launchers"
    return (d / f"stream_{slug}.sh").is_file()


def warmup_candidates(now=None, profiles_dir=None):
    """(slug, folder) of recently played stream games whose profile changed since their last completed overnight fill,
    most recently played first. A game tried in the last 6 hours is skipped (a game removed from RemoteStorage would
    otherwise be retried every pass)."""
    now = now if now is not None else time.time()
    d = Path(profiles_dir) if profiles_dir else PROFILES_DIR
    found = []
    for p in d.glob("*.json") if d.is_dir() else []:
        try:
            st = p.stat()
            if now - st.st_mtime > WARMUP_RECENT_DAYS * 86400:
                continue
            prof = json.loads(p.read_text())
            folder = prof.get("folder") or ""
            empty = not (prof.get("ranges") or prof.get("later"))
        except (OSError, ValueError, AttributeError):
            continue
        slug = p.stem
        if not folder or empty or not _in_library(slug) or not _is_streamable_release(slug, folder, plan=prof, profiles_dir=d):
            continue
        try:
            if _warm_stamp(slug).read_text() == _profile_signature(p):
                continue
        except OSError:
            pass
        tried = PRESTAGE_DIR / f"{slug}.tried"
        try:
            if now - tried.stat().st_mtime < WARMUP_RETRY_SECONDS:
                continue
        except OSError:
            pass

        pending = (PRESTAGE_DIR / f"{slug}.pending.json").exists()
        runs = prof.get("runs", 0)
        score = math.exp(-(now - st.st_mtime) / 86400) + (1.0 if pending else 0) + (0.5 / (runs + 1))
        found.append((score, slug, folder))
    found.sort(reverse=True)
    return [(slug, folder) for _s, slug, folder in found[:WARMUP_MAX_GAMES]]


def _has_real_profile(slug, profiles_dir=None):
    """A recorded profile with something in it. An empty file (a game launched but never recorded) does not count."""
    f = (Path(profiles_dir) if profiles_dir else PROFILES_DIR) / f"{slug}.json"
    try:
        prof = json.loads(f.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(prof, dict) and bool(prof.get("ranges") or prof.get("later"))


def newgame_candidates(now=None, prestage_dir=None, profiles_dir=None):
    """(slug, folder, hot) of games queued but never played here (no recorded profile, or only an empty one), whose whole
    release has not been warmed yet, newest first. Same 6-hour retry rule as the other warm-ups."""
    now = now if now is not None else time.time()
    d = Path(prestage_dir) if prestage_dir else PRESTAGE_DIR
    pd = Path(profiles_dir) if profiles_dir else PROFILES_DIR
    seen, found = set(), []

    games_file = HOME / ".config" / "deckstream" / "games.json"
    active_slugs = None
    if games_file.is_file():
        try:
            gdata = json.loads(games_file.read_text())
            if isinstance(gdata, dict):
                active_slugs = set(gdata.keys())
        except (OSError, ValueError):
            pass

    def consider(slug, folder, hot, mtime, meta=None):
        if active_slugs is not None and slug not in active_slugs:
            return
        if not _in_library(slug):
            return
        if slug in seen or not folder or _has_real_profile(slug, pd):
            return
        if not _is_streamable_release(slug, folder, plan=meta, prestage_dir=d, profiles_dir=pd):
            return
        seen.add(slug)
        try:
            if (d / f"{slug}.warmed").read_text() == "whole":
                return
        except OSError:
            pass
        try:
            if now - (d / f"{slug}.tried").stat().st_mtime < WARMUP_RETRY_SECONDS:
                return
        except OSError:
            pass
        found.append((mtime, slug, folder, hot))

    for p in d.glob("*.newgame.json") if d.is_dir() else []:
        try:
            st, info = p.stat(), json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(info, dict):
            consider(p.name[:-len(".newgame.json")], info.get("folder"), info.get("hot") or [], st.st_mtime, info)
    for p in pd.glob("*.json") if pd.is_dir() else []:       # launched once but never recorded: an empty profile
        try:
            st, prof = p.stat(), json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(prof, dict):
            consider(p.stem, prof.get("folder"), [], st.st_mtime, prof)
    found.sort(reverse=True)
    return [(slug, folder, hot) for _m, slug, folder, hot in found[:2]]


EVICT_UNUSED_DAYS = 30
EVICT_BELOW_FREE = 0.15        # start clearing when less than 15% of the card is free
EVICT_UNTIL_FREE = 0.20        # and stop at 20%


def _last_used(slug, cache_dir):
    """When this game was last played: its profile, its profile log, or rclone's range records (rclone rewrites them
    when a session closes the files), whichever is newest."""
    best = 0.0
    for p in (PROFILES_DIR / f"{slug}.json", PROFILES_DIR / f"{slug}.log.merged", PROFILES_DIR / f"{slug}.log",
              Path(cache_dir)):
        try:
            best = max(best, p.stat().st_mtime)
        except OSError:
            pass
    for root, _dirs, files in os.walk(Path(cache_dir) / "vfsMeta"):
        for f in files:
            try:
                best = max(best, os.lstat(os.path.join(root, f)).st_mtime)
            except OSError:
                pass
    return best


def evict_old_caches(now=None, usage_fn=shutil.disk_usage, proc_root="/proc"):
    """When the card is nearly full, delete the stream caches of games not played for a month (oldest first) so the
    games being played can be warmed. Only the stream cache (re-downloadable from RemoteStorage) is touched: never saves,
    write layers, profiles, prefixes or installed games, never a cache rclone is using, never while a game plays.
    Returns the slugs cleared."""
    now = now if now is not None else time.time()
    try:
        base = Path(STREAM_CACHE_BASE_FILE.read_text().strip())
    except OSError:
        return []
    if not base.is_dir() or base.name != "stream-cache" or fills_blocked(proc_root):
        return []
    try:
        u = usage_fn(str(base))
    except OSError:
        return []
    if u.total <= 0 or u.free / u.total >= EVICT_BELOW_FREE:
        return []
    old = []
    for c in base.iterdir():
        if not c.is_dir() or c.is_symlink():
            continue
        used = _last_used(c.name, c)
        if now - used >= EVICT_UNUSED_DAYS * 86400:
            old.append((used, c))
    cleared, free = [], u.free
    running = set(running_fills(proc_root))
    for _used, c in sorted(old):
        if free / u.total >= EVICT_UNTIL_FREE:
            break
        if c.name in running or rclone_using_cache(str(c), proc_root):
            continue
        size = 0
        for root, _dirs, files in os.walk(c):
            for f in files:
                try:
                    size += os.lstat(os.path.join(root, f)).st_blocks * 512
                except OSError:
                    pass
        shutil.rmtree(c, ignore_errors=True)
        (PRESTAGE_DIR / f"{c.name}.warmed").unlink(missing_ok=True)   # it must be filled again if played
        free += size
        cleared.append(c.name)
        print(f"[*] Card nearly full: cleared the stream cache of '{c.name}' (not played for {EVICT_UNUSED_DAYS}+ days, "
              f"{size / 1e9:.1f} GB). It streams again from RemoteStorage when played.")
    return cleared


REMOVE_SCOPES = ("cache", "game")
_REMOVE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,119}$")


def _tree_bytes(path):
    """Disk space a folder really uses (blocks, not apparent size: rclone's cache files are sparse)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_blocks * 512
            except OSError:
                pass
    return total


def live_remove_shortcut(app_id, cef_target=None, evaluate=None):
    """Remove a shortcut through the running Steam client. True if Steam took it, False if that route is not available."""
    cef_target = cef_target or _cef_target
    evaluate = evaluate or _ws_eval
    try:
        ws = cef_target()
        if not ws:
            return False
        value = evaluate(ws, f"(async () => {{ SteamClient.Apps.RemoveShortcut({int(app_id)}); return {{ok: true}}; }})()")
    except Exception as e:
        print(f"[!] Steam did not remove the shortcut live: {e}")
        return False
    return bool(isinstance(value, dict) and value.get("ok"))


def remove_disk_shortcut(app_id, exe_path="", home=None):
    """Removes a shortcut from Steam's binary shortcuts.vdf on disk."""
    try:
        userdata = find_steam_userdata_dir()
        if not userdata and home:
            candidates = list(Path(home).glob(".local/share/Steam/userdata/*")) + list(Path(home).glob(".steam/root/userdata/*"))
            userdata = candidates[0] if candidates else None
        if not userdata:
            return False
        shortcuts_file = userdata / "config" / "shortcuts.vdf"
        if not shortcuts_file.exists():
            return False
        data = shortcuts_file.read_bytes()
        shortcuts = parse_binary_vdf(data, strict=True)
        initial_len = len(shortcuts)
        filtered = [
            s for s in shortcuts
            if s.get("appid") != app_id and (not exe_path or str(s.get("Exe", "")).strip('"\'') != str(exe_path).strip('"\''))
        ]
        if len(filtered) < initial_len:
            shortcuts_file.write_bytes(serialize_binary_vdf(filtered))
            grid_dir = userdata / "config" / "grid"
            if grid_dir.is_dir() and app_id:
                for art in grid_dir.glob(f"{app_id}*"):
                    try:
                        art.unlink(missing_ok=True)
                    except OSError:
                        pass
            return True
    except Exception as e:
        print(f"[!] Could not remove shortcut from shortcuts.vdf: {e}")
    return False


def remove_game_data(slug, scope, proc_root="/proc", home=None, cache_base_file=None, prestage_dir=None,
                     registry_file=None, remove_tile=None, ismount=os.path.ismount, target_path=None, game_name=None):
    """Free the disk a streamed or installed game uses. scope 'cache': only its stream cache (re-downloadable from
    RemoteStorage; the game stays in the library and streams again when played). scope 'game': that, plus its write layer,
    work and mount folders, installed folders on internal/SD storage, its launcher and its Steam tile. Never touched:
    Proton prefix (saves), save backups, learned start-up profiles. Refuses while any game plays or a fill runs, while
    rclone uses the cache, or while a folder is still mounted. Every folder is checked to sit directly inside its
    expected parent and not be a link. Returns (ok, freed_bytes, message)."""
    if not isinstance(slug, str) or not _REMOVE_SLUG_RE.match(slug):
        return False, 0, "That game name is not valid."
    if scope not in REMOVE_SCOPES:
        return False, 0, "Unknown action."
    home = Path(home) if home else HOME
    prestage_dir = Path(prestage_dir) if prestage_dir else PRESTAGE_DIR
    cache_base_file = Path(cache_base_file) if cache_base_file else STREAM_CACHE_BASE_FILE
    registry_file = Path(registry_file) if registry_file else LIVE_REGISTRY
    stream = home / ".local" / "share" / "deckstream" / "stream"
    if fills_blocked(proc_root):
        return False, 0, "A game is playing or a download is running. Try again when the Deck is idle."

    bases = [stream / "cache"]
    try:
        card_base = Path(cache_base_file.read_text().strip())
        if card_base.name == "stream-cache":
            bases.append(card_base)
    except OSError:
        pass
    targets = [b / slug for b in bases]
    if scope == "game":
        targets += [stream / "writes" / slug, stream / "work" / slug, stream / "merged" / slug, stream / "games" / slug]
        # Also clean up locally downloaded/installed games on internal or SD if present
        game_roots = [home / "Games", home / ".local/share/deckstream/games"]
        sd = get_micro_sd_path()
        if sd and sd.is_dir() and sd not in game_roots:
            game_roots.append(sd)
        try:
            for p in Path("/run/media").glob("*/*/Games"):
                if p.is_dir() and p not in game_roots:
                    game_roots.append(p)
            for p in Path("/run/media").glob("*/Games"):
                if p.is_dir() and p not in game_roots:
                    game_roots.append(p)
        except OSError:
            pass

        if target_path:
            tp = Path(target_path)
            try:
                resolved_tp = tp.resolve()
                if resolved_tp.is_dir() and not resolved_tp.is_symlink():
                    if resolved_tp.parent.name in ("cache", "stream-cache", "writes", "work", "merged", "games", "Games"):
                        if resolved_tp not in targets:
                            targets.append(resolved_tp)
            except OSError:
                pass

        slug_norm = slug.lower().strip("_")
        for groot in game_roots:
            if not groot.is_dir():
                continue
            cand = groot / slug
            if cand.is_dir() and not cand.is_symlink() and cand not in targets:
                targets.append(cand)
            try:
                for child in groot.iterdir():
                    if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
                        continue
                    c_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", child.name).lower().strip("_")
                    c_name_norm = child.name.lower().replace(" ", "_")
                    if c_slug == slug_norm or c_name_norm == slug_norm or (game_name and child.name == game_name):
                        if child not in targets:
                            targets.append(child)
            except OSError:
                pass

    checked = []
    for t in targets:
        if not os.path.lexists(t):
            continue
        if t.is_symlink() or not t.is_dir() or t.resolve() != t.parent.resolve() / t.name or t.parent.name not in (
                "cache", "stream-cache", "writes", "work", "merged", "games", "Games"):
            return False, 0, f"Skipped: {t.parent.name}/{t.name} is not a plain folder."
        if ismount(str(t)) or rclone_using_cache(str(t), proc_root):
            return False, 0, "That game is still mounted. Quit it and try again."
        checked.append(t)

    freed = 0
    for t in checked:
        freed += _tree_bytes(t)
        shutil.rmtree(t, ignore_errors=True)
    for marker in (f"{slug}.warmed", f"{slug}.pending.json", f"{slug}.newgame.json", f"{slug}.hot.json", f"{slug}.starter.json", f"{slug}.tried", f"{slug}.inhibit", f"{slug}.pid"):
        (prestage_dir / marker).unlink(missing_ok=True)   # it must not be refilled by the overnight warm-up
    notes = []
    if scope == "game":
        launcher_dir = home / ".local" / "share" / "deckstream" / "launchers"
        for prefix in ("stream_", "launch_", "play_"):
            (launcher_dir / f"{prefix}{slug}.sh").unlink(missing_ok=True)
        if launcher_dir.is_dir():
            for lf in launcher_dir.glob("*.sh"):
                if f"_{slug}.sh" in lf.name or f"play_{slug}" in lf.name:
                    lf.unlink(missing_ok=True)
        try:
            registry = json.loads(registry_file.read_text())
        except (OSError, ValueError):
            registry = {}
        for name, v in list(registry.items()):
            if isinstance(v, dict):
                exe = str(v.get("exe", ""))
                is_match = (
                    exe.endswith(f"/stream_{slug}.sh") or
                    exe.endswith(f"/launch_{slug}.sh") or
                    exe.endswith(f"/play_{slug}.sh") or
                    f"_{slug}.sh" in exe or
                    name.lower() == (game_name or slug).lower() or
                    re.sub(r"[^a-zA-Z0-9_-]", "_", name).lower().strip("_") == slug
                )
                if is_match:
                    app_id = int(v.get("appid", 0)) if v.get("appid") else 0
                    gone = False
                    if app_id:
                        gone = (remove_tile or live_remove_shortcut)(app_id)
                        if remove_tile is None:
                            disk_gone = remove_disk_shortcut(app_id, str(v.get("exe", "")), home=home)
                            if disk_gone:
                                gone = True
                    registry.pop(name, None)
                    if not gone:
                        notes.append("Its Steam tile is still there: remove it from the Steam library.")
        try:
            registry_file.write_text(json.dumps(registry))
        except OSError:
            pass

        games_file = home / ".config" / "deckstream" / "games.json"
        if games_file.is_file():
            try:
                gdata = json.loads(games_file.read_text())
                if isinstance(gdata, dict):
                    to_del = [k for k in gdata if k == slug or re.sub(r"[^a-zA-Z0-9_-]", "_", k).lower().strip("_") == slug]
                    for k in to_del:
                        del gdata[k]
                    if to_del:
                        games_file.write_text(json.dumps(gdata, indent=2))
            except (OSError, ValueError):
                pass

    # Invalidate games cache immediately so the next heartbeat reports fresh status
    _GAMES_CACHE["at"] = 0.0
    _GAMES_CACHE["games"] = []

    gb = freed / 1e9
    what = "Cleared the cache" if scope == "cache" else "Deleted the game"
    return True, freed, f"{what}: freed {gb:.1f} GB." + ("".join(" " + n for n in notes))
_GAMES_CACHE = {"at": 0.0, "games": []}
