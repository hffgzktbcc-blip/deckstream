"""DeckStream for Decky Loader: what the Deck daemon knows, shown in Gaming Mode's quick-access menu.

It loads the installed daemon (~/.config/deckstream/deckstream-daemon.py) and calls the same functions that fill the
phone's Deck card: version, power, the live status line (live_activity: getting ready, ready, streaming) and the
streamed games (streamed_games).
It also reads the same install queue the phone reads (your site's /api/queue, with the daemon's own config), and its
writes are the phone's queue buttons sent to that queue: Cancel, Try again, Search's Stream / Install to Deck,
and Free up space's Clear cache / Delete (a remove job the daemon checks and runs). It changes no file on the Deck
itself (only the warm-up switch's flags) and runs no command; the panel's Play button opens the game's own Steam shortcut, which the daemon created and
which runs the same stream-runner as tapping the game in the Steam library.
"""
import importlib.util
import json
import os
import re
import shlex
import shutil
import ssl
import sys
import time
import asyncio
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import decky  # provided by Decky Loader
except ImportError:  # tests
    decky = None

_MODULE = {"mod": None, "mtime": 0.0}


def user_home():
    home = getattr(decky, "DECKY_USER_HOME", "") if decky else ""
    return Path(home or os.environ.get("HOME") or Path.home())


def load_daemon(home=None):
    """The installed daemon as a module (reloaded when the file changes, e.g. after a self-update). None if not installed."""
    home = Path(home) if home else user_home()
    path = home / ".config" / "deckstream" / "deckstream-daemon.py"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _MODULE["mod"] is not None and _MODULE["mtime"] == mtime and _MODULE.get("path") == str(path):
        return _MODULE["mod"]
    os.environ["HOME"] = str(home)  # the daemon's paths come from $HOME at import
    spec = importlib.util.spec_from_file_location("deckstream_daemon_for_decky", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # no __pycache__ left in the daemon's folder
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = keep
        if sys.path and sys.path[0] == str(path.parent):
            sys.path.pop(0)
    _MODULE.update(mod=mod, mtime=mtime, path=str(path))
    return mod


def service_active(home=None, proc="/proc"):
    """True when the daemon process is running, False when not. Read from /proc: Decky runs as root, where
    `systemctl --user` cannot reach the Deck user's session (the panel showed "Unknown" on the Deck, 18 Oct)."""
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            if b"deckstream-daemon.py" in Path(proc, pid, "cmdline").read_bytes():
                return True
        except OSError:
            continue
    return False


def shortcut_appids(home=None):
    """slug -> Steam shortcut app id, from the daemon's live-shortcut registry (name -> {appid, exe: .../stream_<slug>.sh})."""
    home = Path(home) if home else user_home()
    try:
        registry = json.loads((home / ".config" / "deckstream" / "live_shortcuts_added.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for v in registry.values() if isinstance(registry, dict) else []:
        if not isinstance(v, dict):
            continue
        base = os.path.basename(str(v.get("exe", "")))
        appid = v.get("appid")
        if base.startswith("stream_") and base.endswith(".sh") and isinstance(appid, int) and not isinstance(appid, bool) and appid != 0:
            out[base[7:-3]] = appid & 0xFFFFFFFF
    return out


def card_mount(mounts="/proc/mounts"):
    """Where the microSD card is mounted, or None. Read from /proc/mounts (the daemon's own lookup creates a Games folder)."""
    try:
        lines = Path(mounts).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("/dev/mmcblk"):
            return Path(parts[1].replace("\\040", " "))
    return None


def space(path):
    """{used_gb, total_gb, free_gb} of the drive holding path, or None."""
    try:
        u = shutil.disk_usage(path)
    except (OSError, TypeError):
        return None
    return {"used_gb": round(u.used / 1e9, 1), "total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1)}


def space_warning(sp, games, full_pct=85):
    """The phone's full-card hint: a drive at 85 % or more names the streamed game whose cache uses the most room."""
    for label, s in (("microSD card", (sp or {}).get("card")), ("Internal drive", (sp or {}).get("internal"))):
        if s and s.get("total_gb"):
            pct = round(s["used_gb"] / s["total_gb"] * 100)
            if pct >= full_pct:
                big = sorted((g for g in games or [] if (g.get("cache_gb") or 0) >= 1), key=lambda g: -g["cache_gb"])
                out = {"drive": label, "pct": pct, "free_gb": s.get("free_gb")}
                if big:
                    out.update(name=big[0].get("name") or big[0].get("slug"), gb=big[0]["cache_gb"])
                return out
    return None


def game_slug(name):
    """The phone's removeGame slug for an installed folder: 'Hades II' -> 'hades_ii'."""
    return re.sub(r"[^a-z0-9_-]", "_", str(name or "").lower()).strip("_")[:120]


def installed_games(roots):
    """Games installed to the Deck (cloudinstall unpacks into <drive>/Games/<name>): folder names and paths only, no sizes
    (walking every file of every game each refresh is too slow for a panel)."""
    out, seen = [], set()
    for root in roots:
        try:
            dirs = sorted(d for d in Path(root).iterdir() if d.is_dir() and not d.is_symlink() and not d.name.startswith("."))
        except OSError:
            continue
        for d in dirs:
            if d.name.lower() not in seen:
                seen.add(d.name.lower())
                g = {"name": d.name, "path": str(d), "slug": game_slug(d.name)}
                sk = skipped_files(d)
                if sk:
                    g["skipped"] = sk
                out.append(g)
    return out[:50]


def skipped_files(game_dir):
    """The daemon's skipped_files: what a rip install left out (other-language files), {folder, bytes} or None."""
    try:
        d = json.loads((Path(game_dir) / ".deckstream-skipped.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("folder"), str) or not d["folder"]:
        return None
    return {"folder": d["folder"], "bytes": _num(d.get("bytes")) or 0}


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def latest_launch(home=None, now=None, alive=None):
    """The newest launch the stream runner recorded, with its stage (the daemon's latest_launch, one reader for the
    panel and the phone). None when there is none, or when the installed daemon predates it (it self-updates)."""
    home = Path(home) if home else user_home()
    d = load_daemon(home)
    fn = getattr(d, "latest_launch", None) if d else None
    if fn is None:
        return None
    return fn(home, now=now) if alive is None else fn(home, now=now, alive=alive)


# --- Overnight warm-up: the same two flag files the daemon reads (warmup_pass); on only when both exist ---------------

def _flags(home):
    return home / ".config" / "deckstream"


def warmup_on(home=None):
    f = _flags(Path(home) if home else user_home())
    return (f / "warmup.enabled").exists() and (f / "prestage.enabled").exists()


def set_warmup(on, home=None, now_ms=None):
    """Turn the overnight warm-up on (both flags) or off (warmup.enabled only), and stamp warmup.applied so an older
    choice still waiting on the phone's site is not applied over this one. The backend runs as root: every file it
    makes is handed to the user who owns the home, or the daemon could not replace it later."""
    home = Path(home) if home else user_home()
    f = _flags(home)
    try:
        owner = home.stat()
        f.mkdir(parents=True, exist_ok=True)
        made = []
        if on:
            for name in ("prestage.enabled", "warmup.enabled"):
                (f / name).touch()
                made.append(f / name)
        else:
            (f / "warmup.enabled").unlink(missing_ok=True)
        tmp = f / "warmup.applied.tmp"
        tmp.write_text(str(int(now_ms if now_ms is not None else time.time() * 1000)))
        os.replace(tmp, f / "warmup.applied")
        made.append(f / "warmup.applied")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            for m in made:
                os.chown(m, owner.st_uid, owner.st_gid)
    except OSError as e:
        return {"ok": False, "on": warmup_on(home), "text": f"Could not change it: {e.strerror or type(e).__name__}"}
    return {"ok": True, "on": warmup_on(home), "text": f"Overnight warm-up {'on' if on else 'off'}"}


# --- Deck mode: the per-game "Deck mode: 30 fps, low graphics" switch, the same files the daemon writes ---------------
# deck_mode/<slug>.env (read by both launchers) and deck_mode.json ({slug: {"on", "at"}}, so an older choice still
# waiting on the phone's site is not applied over this one).
DECK_MODE_ENV = "DECKSTREAM_FPS_CAP=30\nDECKSTREAM_LOW_PRESET=1\n"


def deck_mode_state(home=None):
    f = _flags(Path(home) if home else user_home())
    try:
        raw = json.loads((f / "deck_mode.json").read_text())
    except (OSError, ValueError):
        return {}
    return {slug: {"on": (f / "deck_mode" / f"{slug}.env").is_file(), "at": int(v.get("at") or 0)}
            for slug, v in (raw.items() if isinstance(raw, dict) else []) if _SLUG_RE.match(str(slug)) and isinstance(v, dict)}


def set_deck_mode(slug, on, home=None, now_ms=None):
    """Write or remove the game's Deck mode file. Runs as root: every file it makes goes to the home's owner."""
    home = Path(home) if home else user_home()
    if not _SLUG_RE.match(str(slug or "")):
        return {"ok": False, "on": False, "text": "That game has no name the Deck can use"}
    f = _flags(home)
    try:
        owner = home.stat()
        (f / "deck_mode").mkdir(parents=True, exist_ok=True)
        made = [f / "deck_mode"]
        env = f / "deck_mode" / f"{slug}.env"
        if on:
            tmp = env.with_name(env.name + ".tmp")
            tmp.write_text(DECK_MODE_ENV)
            os.replace(tmp, env)
            made.append(env)
        else:
            env.unlink(missing_ok=True)
        st = deck_mode_state(home)
        st[slug] = {"on": bool(on), "at": int(now_ms if now_ms is not None else time.time() * 1000)}
        st = dict(sorted(st.items(), key=lambda kv: -kv[1]["at"])[:200])
        tmp = f / "deck_mode.json.tmp"
        tmp.write_text(json.dumps(st))
        os.replace(tmp, f / "deck_mode.json")
        made.append(f / "deck_mode.json")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            for m in made:
                os.chown(m, owner.st_uid, owner.st_gid)
    except OSError as e:
        return {"ok": False, "on": env.is_file() if "env" in locals() else False,
                "text": f"Could not change it: {e.strerror or type(e).__name__}"}
    return {"ok": True, "on": bool(on), "text": f"Deck mode {'on' if on else 'off'}: starts with the next launch"}


def status(home=None, refresh=False, mounts="/proc/mounts"):
    """Everything the panel shows, as plain JSON. Never raises: a broken or missing install says so. refresh measures the
    streamed games' caches now instead of using the daemon's few-minutes-old figures."""
    home = Path(home) if home else user_home()
    try:
        d = load_daemon(home)
    except Exception as e:  # a daemon that does not load is shown, not crashed on
        return {"installed": True, "error": f"Could not read the DeckStream daemon: {type(e).__name__}"}
    if d is None:
        return {"installed": False}
    out = {"installed": True, "running": service_active(home), "at": int(time.time())}
    live = "live_activity" if hasattr(d, "live_activity") else "fill_progress"   # an older daemon: fills only
    for key, fn in (("version", "installed_version"), ("ac_online", "on_ac_power"), ("battery_pct", "battery_percent"),
                    ("active_game", live)):
        try:
            out[key] = getattr(d, fn)()
        except Exception:
            out[key] = None
    try:
        games = d.streamed_games() if refresh or not hasattr(d, "streamed_games_cached") else d.streamed_games_cached()
    except Exception:
        games = []
    ids = shortcut_appids(home)
    out["games"] = [{"slug": g.get("slug"), "name": g.get("name"), "cache_gb": g.get("cache_gb", 0),
                     "appid": ids.get(g.get("slug"))} for g in (games or []) if isinstance(g, dict)]
    card = card_mount(mounts)
    try:
        out["last_launch"] = latest_launch(home)
    except Exception:
        out["last_launch"] = None
    out["space"] = {"internal": space(home), "card": space(card) if card else None}
    out["warmup"] = warmup_on(home)
    out["deck_mode"] = {k: v["on"] for k, v in deck_mode_state(home).items()}
    out["space_warning"] = space_warning(out["space"], out["games"])
    out["installed_games"] = installed_games(game_roots(home, card))
    return out


def game_roots(home, card):
    return [home / "Games", home / ".local" / "share" / "deckstream" / "games"] + ([card / "Games"] if card else [])


# --- The install queue (what the phone's Queue tab shows), read from the user's own site with the daemon's config ---
CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem")
_QUEUE_CACHE = {"at": 0.0, "home": "", "val": None}


def queue_config(home=None):
    """(server_url, device_id, secret) from the daemon's config, or None when it has no site set."""
    home = Path(home) if home else user_home()
    try:
        cfg = json.loads((home / ".config" / "deckstream" / "daemon_config.json").read_text())
    except (OSError, ValueError):
        return None
    url = str(cfg.get("server_url") or "").strip().rstrip("/") if isinstance(cfg, dict) else ""
    if not url.startswith("https://") and not url.startswith("http://"):
        return None
    return url, str(cfg.get("device_id") or "deck-main"), str(cfg.get("queue_secret") or "")


def _ssl_context():
    """Decky's bundled Python may ship without CA certificates; SteamOS keeps them in /etc/ssl."""
    for f in CA_FILES:
        if os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


def http_json(method, url, secret, body=None, timeout=6, token=""):
    """One request to the user's site; returns (http status, parsed JSON or {}). token: the storage key, for search and send."""
    headers = {"Content-Type": "application/json", "User-Agent": "DeckStream-Decky/1"}
    if secret:
        headers["x-deckstream-secret"] = secret
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            code, raw = r.status, r.read(8_000_000)   # a whole library (mylist) can be a few MB
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read(100_000)
    try:
        return code, json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return code, {}


def mode_label(j):
    """The phone's jobModeLabel."""
    if j.get("mode") == "stream" or j.get("targetDir") == "stream":
        return "Stream"
    if j.get("mode") == "cloudinstall":
        return "Install to Deck"
    if j.get("mode") == "remove":
        return "Delete from Deck"
    return "Download to internal drive" if j.get("targetDir") == "internal" else "Download to microSD"


def _ms(v):
    try:
        return int(v) // 1000 if v else None
    except (TypeError, ValueError):
        return None


def _txt(v, n=300):
    return str(v or "")[:n]


def queue(home=None, fetch=http_json, now=None, ttl=8):
    """The phone's Queue tab, as plain JSON: {ok, current, jobs, history, error} or {ok: False, why}. Never raises.
    Cached for a few seconds: the panel refreshes every 5 s while open."""
    home = Path(home) if home else user_home()
    now = time.time() if now is None else now
    if _QUEUE_CACHE["val"] is not None and _QUEUE_CACHE["home"] == str(home) and now - _QUEUE_CACHE["at"] < ttl:
        return _QUEUE_CACHE["val"]
    cfg = queue_config(home)
    if not cfg:
        return {"ok": False, "why": "No DeckStream site is set on this Deck (reinstall from the app's Deck tab)."}
    url, device, secret = cfg
    try:
        code, d = fetch("GET", f"{url}/api/queue?action=status&device={urllib.parse.quote(device)}", secret)
    except Exception as e:
        return {"ok": False, "why": f"Could not reach your DeckStream site ({type(e).__name__})."}
    if code == 401:
        return {"ok": False, "why": "Your site refused the queue password in the daemon's config."}
    if code != 200 or not isinstance(d, dict):
        return {"ok": False, "why": f"Your DeckStream site answered {code}."}
    st = d.get("status") if isinstance(d.get("status"), dict) else {}
    cur = st.get("currentJob") if isinstance(st.get("currentJob"), dict) else None
    out = {"ok": True, "current": None, "jobs": [], "history": [], "error": None}
    if cur:
        out["current"] = {"id": _txt(cur.get("id"), 80), "name": _txt(cur.get("name"), 200), "status": _txt(cur.get("status"), 30),
                          "progress": max(0, min(100, _num(cur.get("progress")) or 0)), "message": _txt(cur.get("message")),
                          "mode": mode_label(cur)}
    for j in (d.get("jobs") or [])[:20]:
        if isinstance(j, dict) and not (cur and j.get("id") == cur.get("id")):
            out["jobs"].append({"id": _txt(j.get("id"), 80), "name": _txt(j.get("name"), 200), "mode": mode_label(j),
                                "size_gb": round(float(j.get("size") or 0) / 1e9, 1) if isinstance(j.get("size"), (int, float)) else None})
    for h in reversed((d.get("history") or [])[-6:]):
        if isinstance(h, dict):
            out["history"].append({"id": _txt(h.get("id"), 80), "name": _txt(h.get("name"), 200), "status": _txt(h.get("status"), 30),
                                   "message": _txt(h.get("message")), "mode": mode_label(h), "finished": _ms(h.get("finishedAt"))})
    err = st.get("error")
    if isinstance(err, dict) and err.get("message"):
        out["error"] = {"message": _txt(err.get("message")), "can_retry": bool(err.get("can_retry")), "job_id": _txt(err.get("job_id"), 60)}
    _QUEUE_CACHE.update(at=now, home=str(home), val=out)
    return out


def queue_action(action, job_id, home=None, fetch=http_json):
    """The phone's Cancel and Try again buttons. Only these two actions are sent. Returns {ok, text}."""
    if action not in ("cancel", "retry"):
        return {"ok": False, "text": "Unknown action"}
    cfg = queue_config(home)
    if not cfg:
        return {"ok": False, "text": "No DeckStream site is set on this Deck."}
    url, device, secret = cfg
    try:
        code, d = fetch("POST", f"{url}/api/queue?device={urllib.parse.quote(device)}", secret,
                        {"action": action, "jobId": str(job_id or "")[:80]})
    except Exception as e:
        return {"ok": False, "text": f"Could not reach your DeckStream site ({type(e).__name__})."}
    _QUEUE_CACHE["val"] = None   # show the change on the next refresh
    if code == 401:
        return {"ok": False, "text": "Wrong queue password"}
    if code != 200:
        return {"ok": False, "text": (d.get("error") if isinstance(d, dict) else "") or f"Server error {code}"}
    return {"ok": True, "text": "Cancelled" if action == "cancel" else "Trying again on the Deck"}


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,119}$")   # api/queue.js accepts only this


def remove_game(slug, scope, name="", path="", home=None, fetch=http_json, mounts="/proc/mounts"):
    """The phone's Free up space buttons (removeGame): Clear cache (scope 'cache': it streams again next time) or Delete
    (scope 'game': files, launcher and Steam tile; saves stay). Sent as a remove job; the daemon re-checks everything and
    refuses while a game plays. An installed folder's path is only sent when it is one this panel listed. Returns {ok, text}."""
    slug, scope, name = str(slug or ""), str(scope or ""), _txt(name, 120).strip()
    if scope not in ("cache", "game") or not _SLUG_RE.match(slug):
        return {"ok": False, "text": "That game name is not valid."}
    home = Path(home) if home else user_home()
    body = {"action": "enqueue", "mode": "remove", "slug": slug, "scope": scope, "name": name or slug}
    if path:
        card = card_mount(mounts)
        if scope != "game" or str(path) not in {g["path"] for g in installed_games(game_roots(home, card))}:
            return {"ok": False, "text": "That folder is not one of the Deck's installed games."}
        body["path"] = str(path)
    cfg = queue_config(home)
    if not cfg:
        return {"ok": False, "text": "No DeckStream site is set on this Deck."}
    url, device, secret = cfg
    try:
        code, d = fetch("POST", f"{url}/api/queue?device={urllib.parse.quote(device)}", secret, body)
    except Exception as e:
        return {"ok": False, "text": f"Could not reach your DeckStream site ({type(e).__name__})."}
    _QUEUE_CACHE["val"] = None
    if code == 401:
        return {"ok": False, "text": "Wrong queue password"}
    if code != 200:
        return {"ok": False, "text": "Could not send that: " + ((_txt(d.get("error"), 160) if isinstance(d, dict) else "") or f"server error {code}")}
    shown = name or slug
    return {"ok": True, "text": f"Deleting {shown}…" if scope == "game" else f"Clearing {shown}'s cache…"}


def get_skipped(path, home=None, fetch=http_json, mounts="/proc/mounts"):
    """The phone's "Get skipped files": a cloudinstall job with keepAll for an installed game this panel listed, which
    copies only the other-language files its rip install left out. Returns {ok, text}."""
    home = Path(home) if home else user_home()
    hit = [g for g in installed_games(game_roots(home, card_mount(mounts))) if g["path"] == str(path or "") and g.get("skipped")]
    if not hit:
        return {"ok": False, "text": "Nothing was skipped in that game."}
    g = hit[0]
    cfg = queue_config(home)
    if not cfg:
        return {"ok": False, "text": "No DeckStream site is set on this Deck."}
    url, device, secret = cfg
    body = {"action": "enqueue", "mode": "cloudinstall", "keepAll": True, "name": g["name"], "path": g["path"],
            "webdavPath": g["skipped"]["folder"], "size": g["skipped"]["bytes"],
            "targetDir": "sd" if g["path"].startswith("/run/media/") else "internal"}
    try:
        code, d = fetch("POST", f"{url}/api/queue?device={urllib.parse.quote(device)}", secret, body)
    except Exception as e:
        return {"ok": False, "text": f"Could not reach your DeckStream site ({type(e).__name__})."}
    _QUEUE_CACHE["val"] = None
    if code == 401:
        return {"ok": False, "text": "Wrong queue password"}
    if code != 200:
        return {"ok": False, "text": "Could not send that: " + ((_txt(d.get("error"), 160) if isinstance(d, dict) else "") or f"server error {code}")}
    return {"ok": True, "text": f"Getting the skipped files of {g['name']}…"}


# --- Your library and Send to Deck: the phone's Library and Search tabs, through the same site calls ---
NO_KEY = "No storage key on this Deck. Run the installer from the app's Settings tab again."


def storage_key(home=None):
    """The storage key install.sh saved (~/.config/deckstream/storage.conf, STORAGE_KEY=<shell-quoted>), or ''."""
    home = Path(home) if home else user_home()
    try:
        text = (home / ".config" / "deckstream" / "storage.conf").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("STORAGE_KEY="):
            try:
                v = shlex.split(line[len("STORAGE_KEY="):])
            except ValueError:
                return ""
            return v[0].strip() if v else ""
    return ""


def _cached(t):
    """The phone's isItemCached."""
    if any(t.get(k) is True for k in ("download_finished", "download_present", "cached", "finished")):
        return True
    p = t.get("progress")
    if isinstance(p, (int, float)) and (p >= 1 or p == 100):
        return True
    return bool(re.search(r"completed|cached|downloaded|ready", str(t.get("download_state") or ""), re.I))


def _top(f):
    return str((f or {}).get("name") or (f or {}).get("short_name") or "").split("/")[0]


def folder_clash(t, library):
    """The phone's mergedFolderClash: another library game in the same WebDAV folder with different files."""
    files = t.get("files") or []
    mine = _top(files[0]) if files else ""
    if not mine or not all(_top(f) == mine for f in files):
        return ""
    sizes = {str(f.get("name") or ""): _num(f.get("size")) or 0 for f in files}
    for o in library:
        of = o.get("files") if isinstance(o, dict) else None
        if not of or str(o.get("id")) == str(t.get("id")) or _top(of[0]) != mine:
            continue
        if any(str(f.get("name") or "") in sizes and sizes[str(f.get("name") or "")] != (_num(f.get("size")) or 0) for f in of):
            return str(o.get("name") or "another game")
    return ""


def webdav_folder(t, plan):
    """The phone's webdavFolderFor."""
    if plan and plan.get("webdavFolder"):
        return str(plan["webdavFolder"])
    names = [str(f.get("name") or "") for f in (t.get("files") or [])]
    top = names[0].split("/")[0] if names else ""
    if top and all("/" in n and n.split("/")[0] == top for n in names):
        return top
    return str(t.get("name") or "")


def _deliver(call, device, t, lib, name, size, wait):
    """The phone's Send to Deck for a game in the library: the planner reads its files, a game folder streams, anything
    else installs (cloudinstall). Raises on network errors (the callers turn that into text)."""
    if not _cached(t):
        pr = t.get("progress")
        pct = round(pr * 100 if isinstance(pr, (int, float)) and pr <= 1 else 0)
        return {"ok": True, "text": f"Your storage server is still preparing this ({pct}%). Send it once it is ready."}
    plan = None
    for i in range(6):   # the storage server can still be indexing a new folder (404): the phone's retry loop
        if i:
            wait(2.5)
        code, p = call("GET", f"/api/plan?game_id={urllib.parse.quote(str(t.get('id')))}")
        if code == 200 and isinstance(p, dict) and isinstance(p.get("plan"), dict):
            plan = p["plan"]
            break
        if code != 404:
            break
    if not plan:
        return {"ok": False, "text": "Couldn't check what this game is yet. Try again in a moment."}
    if plan.get("kind") == "suspicious" or plan.get("recommendation") == "unsupported":
        return {"ok": False, "text": _txt(plan.get("reason"), 200) or "This one can't be sent to the Deck"}
    if plan.get("recommendation") == "download":
        return {"ok": False, "text": "Send this one from the phone (it downloads a file to the Deck)."}
    mode = "stream" if plan.get("recommendation") == "stream" and plan.get("kind") == "plain" else "cloudinstall"
    if mode == "stream":
        clash = folder_clash(t, lib)
        if clash:
            return {"ok": False, "text": f'This and "{_txt(clash, 80)}" share one folder with different files, so streaming would mix them. Keep one copy on your storage server, then send again.'}
    hot = plan.get("hot") if isinstance(plan.get("hot"), dict) else {}
    body = {"action": "enqueue", "storageId": str(t.get("id")), "name": name, "downloadUrl": "", "mode": mode,
            "targetDir": "stream" if mode == "stream" else "sd", "webdavPath": webdav_folder(t, plan),
            "size": _num(t.get("size")) or _num(size) or 0,
            "plan": {"kind": plan.get("kind"), "recommendation": plan.get("recommendation"), "exe": plan.get("exe") or "",
                     "hot": hot.get("files", [])[:200] if isinstance(hot.get("files"), list) else []}}
    code, d = call("POST", f"/api/queue?device={urllib.parse.quote(device)}", body)
    _QUEUE_CACHE["val"] = None
    if code == 200 and isinstance(d, dict) and d.get("success"):
        return {"ok": True, "text": f"Sent {name} to your Deck: " + ("it streams" if mode == "stream" else "it installs first")}
    err = _txt(d.get("error"), 200) if isinstance(d, dict) else ""
    return {"ok": False, "text": "Queue error: " + ("Couldn't check what this game is yet. Try again in a moment." if re.search("launch plan", err, re.I) else err or f"server error {code}")}


LIBRARY_GROUPS = ["Ready to play", "Needs installing", "Still caching", "Other files"]
_HELPER_EXE = re.compile(r"^(setup|unins|redist|vcredist|dxsetup|dotnet|crashreport|quicksfv)")


def library_group(t):
    """The phone's libraryGroupFor(t) with classifyItem's file rules (index.html): only a ready game folder with the
    game's own .exe is 'Ready to play'; installers, archives and anything unrecognised install; the planner decides on send."""
    if not _cached(t):
        return "Still caching"
    names = [str((f or {}).get("name") or (f or {}).get("short_name") or "").lower() for f in (t.get("files") or []) if isinstance(f, dict)]
    if not names:
        return "Needs installing"
    if all(re.search(r"\.(flac|mp3|ogg|wav|m4a|m4b|aac|mkv|mp4|avi|txt|nfo|jpe?g|png|pdf)$", n) for n in names):
        return "Other files"
    if any(re.search(r"\.(iso|mdf|mds|cue|nrg)$", n) for n in names):
        return "Other files"
    if any(n.endswith(".exe") and not _HELPER_EXE.search(n.split("/")[-1]) for n in names):
        return "Ready to play"
    return "Needs installing"


def _site(home):
    """(make_call, device) for the site's storage relay and queue with the Deck's own key, or (None, why)."""
    cfg = queue_config(home)
    if not cfg:
        return None, "No DeckStream site is set on this Deck (reinstall from the app's Settings tab)."
    key = storage_key(home)
    if not key:
        return None, NO_KEY
    url, device, secret = cfg
    return (lambda fetch: lambda method, path, body=None, timeout=20: fetch(method, f"{url}{path}", secret, body, timeout=timeout, token=key)), device


def _mylist(call):
    code, lib = call("GET", "/api/storage?p=games/mylist")
    if code == 401:
        raise PermissionError("Your site refused the queue password in the daemon's config.")
    data = lib.get("data") if isinstance(lib, dict) else lib
    if code != 200 or not isinstance(data, list):
        why = _txt(lib.get("detail") or lib.get("error"), 160) if isinstance(lib, dict) else ""
        raise LookupError(f"Storage server: {why}" if why else f"Could not read your library ({code}).")
    return [t for t in data if isinstance(t, dict)]


def library(home=None, fetch=http_json):
    """The phone's Library tab: {ok, items: [{id, name, size_gb, group, pct}]} newest first, or {ok: False, why}."""
    mk, device = _site(home)
    if not mk:
        return {"ok": False, "why": device}
    try:
        lib = _mylist(mk(fetch))
    except (PermissionError, LookupError) as e:
        return {"ok": False, "why": str(e)}
    except Exception as e:
        return {"ok": False, "why": f"Could not reach your DeckStream site ({type(e).__name__})."}
    lib.sort(key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""), reverse=True)
    out = []
    for t in lib[:60]:
        if not re.match(r"^\d{1,12}$", str(t.get("id") or "")):
            continue
        pr, size = t.get("progress"), _num(t.get("size"))
        out.append({"id": str(t["id"]), "name": _txt(t.get("name"), 200), "group": library_group(t),
                    "size_gb": round(size / 1e9, 1) if size else None,
                    "pct": round(pr * 100 if pr <= 1 else pr) if isinstance(pr, (int, float)) else 0})
    return {"ok": True, "items": out}


def search(query, home=None, fetch=http_json):
    """The phone's Search: {ok, results: [{id, title, size_gb, group, sendable}]} (your library first, then Steam store
    titles, which are details only), or {ok: False, why}."""
    q = str(query or "").strip()[:100]
    if not q:
        return {"ok": False, "why": "Type a game to search for."}
    mk, device = _site(home)
    if not mk:
        return {"ok": False, "why": device}
    try:
        code, d = mk(fetch)("GET", f"/api/search?q={urllib.parse.quote(q)}", timeout=30)
    except Exception as e:
        return {"ok": False, "why": f"Could not reach your DeckStream site ({type(e).__name__})."}
    if code == 401:
        return {"ok": False, "why": "Your site refused the queue password in the daemon's config."}
    if code != 200 or not isinstance(d, list):
        return {"ok": False, "why": _txt(d.get("error") if isinstance(d, dict) else "") or f"Search failed ({code})."}
    out = []
    for r in d[:20]:
        if not isinstance(r, dict):
            continue
        lib = r.get("source") == "library" and re.match(r"^\d{1,12}$", str(r.get("id") or ""))
        size = r.get("size")
        out.append({"id": str(r.get("id")) if lib else "", "title": _txt(r.get("title"), 200),
                    "group": ("Streams from your storage" if r.get("streamable") else "Install on the Deck") if lib else "On the Steam store",
                    "size_gb": round(float(size) / 1e9, 1) if isinstance(size, (int, float)) and size > 0 else None,
                    "sendable": bool(lib)})
    return {"ok": True, "results": out}


def send_library(storage_id, name, home=None, fetch=http_json, wait=time.sleep):
    """The phone's Stream to Deck / Install to Deck for a game in your library. Returns {ok, text}. Never raises."""
    sid = str(storage_id or "")
    if not re.match(r"^\d{1,12}$", sid):
        return {"ok": False, "text": "That game has no library id."}
    name = _txt(name, 120).strip() or "Game"
    mk, device = _site(home)
    if not mk:
        return {"ok": False, "text": device}
    try:
        call = mk(fetch)
        lib = _mylist(call)
        t = next((t for t in lib if str(t.get("id")) == sid), None)
        if not t:
            return {"ok": False, "text": f"{name} is no longer in your library."}
        return _deliver(call, device, t, lib, name, 0, wait)
    except (PermissionError, LookupError) as e:
        return {"ok": False, "text": str(e)}
    except Exception as e:
        return {"ok": False, "text": f"Could not reach your DeckStream site ({type(e).__name__})."}


class Plugin:
    async def get_status(self, refresh=False):
        return status(refresh=bool(refresh))

    async def set_warmup(self, on=False):
        return set_warmup(bool(on))

    async def set_deck_mode(self, slug="", on=False):
        return set_deck_mode(str(slug), bool(on))

    async def get_queue(self):
        return await asyncio.to_thread(queue)   # a network call: off Decky's event loop

    async def queue_action(self, action, job_id=""):
        return await asyncio.to_thread(queue_action, str(action), str(job_id or ""))

    async def search(self, query=""):
        return await asyncio.to_thread(search, str(query or ""))

    async def library(self):
        return await asyncio.to_thread(library)

    async def send_library(self, storage_id="", name=""):
        return await asyncio.to_thread(send_library, str(storage_id or ""), str(name or ""))

    async def remove_game(self, slug="", scope="", name="", path=""):
        return await asyncio.to_thread(remove_game, str(slug or ""), str(scope or ""), str(name or ""), str(path or ""))

    async def get_skipped(self, path=""):
        return await asyncio.to_thread(get_skipped, str(path or ""))

    async def _main(self):
        if decky:
            decky.logger.info("DeckStream panel loaded")

    async def _unload(self):
        pass
