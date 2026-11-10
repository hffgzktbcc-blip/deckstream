"""What the phone and the Decky panel show: fill progress, launches, streamed games and the heartbeat status.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import os
import time
import json
import re
from pathlib import Path

import ds_core
from ds_core import HOME, STATE_DIR
from ds_shortcuts import LIVE_REGISTRY
from ds_stream import on_ac_power
from ds_cache import (STREAM_CACHE_BASE_FILE, _GAMES_CACHE, _REMOVE_SLUG_RE, _tree_bytes, deck_mode_state,
    startup_gb, warmup_applied_at, warmup_on)
from ds_update import AUTO_UPDATE_OFF, UPDATE_SIGNERS, _load_update_state, battery_percent, installed_version


def _tile_names(registry_file=None):
    """slug -> the game's title as shown on its Steam tile (from the live-shortcut registry)."""
    try:
        registry = json.loads(Path(registry_file or LIVE_REGISTRY).read_text())
    except (OSError, ValueError):
        registry = {}
    names = {}
    for name, v in registry.items():
        base = os.path.basename(str(v.get("exe", ""))) if isinstance(v, dict) else ""
        if base.startswith("stream_") and base.endswith(".sh"):
            names[base[7:-3]] = re.sub(r"\s*\(Cloud Stream\)\s*$", "", name)
    return names


_FILL_LINE_RE = re.compile(r"^(\d\d):(\d\d):(\d\d) ([0-9.]+) of ([0-9.]+) GB$")


def fill_progress(logs_dir=None, now=None, max_age=120, registry_file=None):
    """The game being prepared right now (the pre-launch download or background prestage), for the phone's 'Getting it ready' card: read from
    the fill's own log (a 'X of Y GB' line every ~30 s). A finished or stale log means nothing is being prepared.
    Returns an active_game dict or None."""
    logs_dir = Path(logs_dir) if logs_dir else STATE_DIR / "logs"
    now = now if now is not None else time.time()
    best = None
    try:
        files = list(logs_dir.glob("stream_fill_*.log")) + list(logs_dir.glob("prestage_*.log"))
    except OSError:
        return None
    for f in files:
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if now - m <= max_age and (best is None or m > best[0]):
            best = (m, f)
    if not best:
        # nothing is filling: a queue-time prestage that finished in the last 10 minutes is "ready to play"
        for f in sorted(logs_dir.glob("prestage_*.log"), key=lambda q: q.stat().st_mtime, reverse=True)[:3]:
            try:
                last = f.read_text(errors="replace").splitlines()[-1:]
                got = _ready_from_prestage((f.stat().st_mtime, f), last[0] if last else "", now, registry_file)
            except OSError:
                continue
            if got:
                return got
        return None
    try:
        lines = best[1].read_text(errors="replace").splitlines()[-6:]
    except OSError:
        return None
    if not lines:
        return None
    if lines[-1].split(" ", 1)[-1].startswith("done"):
        return _ready_from_prestage(best, lines[-1], now, registry_file)
    pts = []
    for ln in lines:
        mt = _FILL_LINE_RE.match(ln.strip())
        if mt:
            h, mi, se, done, total = mt.groups()
            pts.append((int(h) * 3600 + int(mi) * 60 + int(se), float(done), float(total)))
    if not pts:
        return None
    t, done, total = pts[-1]
    eta = 0
    if len(pts) >= 2:
        dt = (t - pts[-2][0]) % 86400                   # the clock wraps at midnight
        rate = (done - pts[-2][1]) / dt if dt else 0    # GB per second
        if rate > 0 and total > done:
            eta = int((total - done) / rate)
    fname = best[1].name
    if fname.startswith("stream_fill_"):
        slug = fname[len("stream_fill_"):-len(".log")]
    elif fname.startswith("prestage_"):
        slug = fname[len("prestage_"):-len(".log")]
    else:
        slug = fname[:-len(".log")]
    pct = int(min(99, done / total * 100)) if total > 0 else 0
    name = _tile_names(registry_file).get(slug) or slug.replace("_", " ").strip().title()
    return {"slug": slug, "name": name, "phase": "preparing", "progress": pct,
            "phase_label": getting_ready_text(done, total, eta),
            "bytes_done": int(done * 1e9), "bytes_total": int(total * 1e9), "eta_s": eta}


def getting_ready_text(done_gb, total_gb, eta_s=0):
    """The one wording the phone and the Decky panel show for a fill: 'Getting ready: 7.0 of 12.0 GB, 3 min left'."""
    text = f"Getting ready: {done_gb:.1f} of {total_gb:.1f} GB"
    if eta_s and eta_s > 0:
        text += f", {max(1, round(eta_s / 60))} min left"
    return text


def _ready_from_prestage(best, last_line, now, registry_file=None, profiles_dir=None, ready_for=600):
    """A queue-time prestage that just finished and covers the game's whole start-up set: phase 'ready' for 10 minutes
    (the phone and Decky show it; api/status.js turns the phase change into a push). Otherwise None."""
    mtime, f = best
    if not f.name.startswith("prestage_") or now - mtime > ready_for:
        return None
    slug = f.name[len("prestage_"):-len(".log")]
    m = re.search(r"done: ([\d.]+) GB", last_line)
    need = startup_gb(slug, profiles_dir)
    if not m or need <= 0 or float(m.group(1)) < need * 0.98:
        return None
    name = _tile_names(registry_file).get(slug) or slug.replace("_", " ").strip().title()
    return {"slug": slug, "name": name, "phase": "ready", "progress": 100, "phase_label": "Ready to play"}


# The words for a launch's exit_reason (the enum the runner writes; same list as daemon/launch_report.py).
EXIT_TEXT = {
    "dxvk": "graphics driver (Vulkan) did not start", "vkd3d": "DirectX 12 layer is missing", "oom": "ran out of memory",
    "access_violation": "the game crashed on start", "d3d11": "the display could not be set up",
    "prefix_timeout": "the Windows layer timed out", "anticheat": "anti-cheat is not supported here",
    "steam_api": "the Steam API is missing (this release needs a manual step)", "unknown_early": "no clear cause in the log",
}
LAUNCHES_DIR = STATE_DIR / "launches"


def _runner_alive(path, proc_root="/proc"):
    """The runner's pid is the last-but-one part of the launch file's name: <slug>.<epoch>.<pid>.txt."""
    try:
        pid = int(Path(path).name.rsplit(".", 2)[-2])
        return b"stream-runner.sh" in (Path(proc_root) / str(pid) / "cmdline").read_bytes()
    except (ValueError, OSError, IndexError):
        return False


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def latest_launch(home=None, now=None, alive=_runner_alive):
    """The newest launch the stream runner recorded (launches/<slug>.<epoch>.<pid>.txt, KEY=VALUE lines), as a stage plus
    plain numbers: stage is preparing / filling / starting / playing while its runner lives, else ended. While it plays,
    cache_mb is the game's cache now and mbps how fast it grew over the runner's last two 30 s samples (what is being
    pulled from RemoteStorage). None when there is none. Read-only; the Decky panel and the heartbeat both use it."""
    d = (Path(home) / ".local" / "share" / "deckstream" / "launches") if home else LAUNCHES_DIR
    try:
        files = sorted((f for f in d.glob("*.*.*.*") if f.suffix in (".txt", ".sent")), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    if not files:
        return None
    f = files[-1]
    vals, samples = {}, []
    try:
        for line in f.read_text(errors="replace").splitlines():
            k, sep, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if not (sep and k):
                continue
            vals[k] = v
            if k == "last_seen" and _int(v) is not None and _int(vals.get("cache_mb_last")) is not None:
                samples.append((_int(v), _int(vals["cache_mb_last"])))
    except OSError:
        return None
    n = {k: _int(v) for k, v in vals.items()}
    now = int(now if now is not None else time.time())
    live = f.suffix == ".txt" and alive(f) and "exit_seconds" not in vals
    if not live:
        stage = "ended"
    elif n.get("launched"):
        stage = "playing"
    elif n.get("fill_end"):
        stage = "starting"
    elif n.get("fill_start"):
        stage = "filling"
    else:
        stage = "preparing"
    out = {"slug": vals.get("slug", ""), "stage": stage, "play": n.get("play"),
           "ago_s": max(0, now - n["play"]) if n.get("play") else None}
    if n.get("fill_start") is not None and n.get("fill_end") is not None and n["fill_end"] >= n["fill_start"]:
        out["fill_seconds"] = n["fill_end"] - n["fill_start"]
    if n.get("play") is not None and n.get("launched") is not None and n["launched"] >= n["play"]:
        out["play_to_launch_seconds"] = n["launched"] - n["play"]
    if n.get("exit_seconds") is not None:
        out["exit_seconds"] = n["exit_seconds"]
    if vals.get("exit_reason") in EXIT_TEXT:
        out["exit_reason"] = vals["exit_reason"]
        out["exit_text"] = EXIT_TEXT[vals["exit_reason"]]
    if stage == "playing":
        mb = samples[-1][1] if samples else n.get("cache_mb_at_launch")
        if mb is not None and mb >= 0:
            out["cache_mb"] = mb
        if len(samples) >= 2 and now - samples[-1][0] <= 120:   # a stale sample says nothing about the line now
            (t1, m1), (t2, m2) = samples[-2], samples[-1]
            if t2 > t1:
                out["mbps"] = round(max(0, m2 - m1) * 1.048576 / (t2 - t1), 1)   # du -sm counts MiB
    return out


def stream_activity(home=None, now=None, alive=_runner_alive, registry_file=None):
    """The live launch as the phone's and the Decky panel's status line (active_game shape), or None when no runner
    lives: 'Getting ready' (the launch fill), 'Starting the game', 'Streaming: 3.2 GB cached, 9 MB/s'."""
    ll = latest_launch(home, now=now, alive=alive)
    if not ll or ll["stage"] == "ended" or not ll.get("slug"):
        return None
    slug = ll["slug"]
    name = _tile_names(registry_file).get(slug) or slug.replace("_", " ").strip().title()
    if ll["stage"] == "playing":
        label = "Streaming"
        if ll.get("cache_mb") is not None:
            label += f": {ll['cache_mb'] * 1.048576 / 1000:.1f} GB cached"
            if ll.get("mbps") is not None:
                label += f", {ll['mbps']:.0f} MB/s" if ll["mbps"] >= 10 else f", {ll['mbps']:.1f} MB/s"
        out = {"slug": slug, "name": name, "phase": "playing", "progress": 100, "phase_label": label}
        if ll.get("cache_mb") is not None:
            out["cache_gb"] = round(ll["cache_mb"] * 1.048576 / 1000, 1)
        if ll.get("mbps") is not None:
            out["mbps"] = ll["mbps"]
        return out
    if ll["stage"] == "starting":
        return {"slug": slug, "name": name, "phase": "starting", "progress": 100, "phase_label": "Starting the game"}
    return {"slug": slug, "name": name, "phase": "preparing", "progress": 0, "phase_label": "Getting ready"}


def live_activity(home=None, now=None, logs_dir=None, registry_file=None, alive=_runner_alive):
    """The one status line both screens show: a game streaming now wins; otherwise a fill (the launch fill or a
    queue-time prestage, with GB and time left) or a finished prestage ('Ready to play'); otherwise the runner's stage."""
    s = stream_activity(home, now=now, alive=alive, registry_file=registry_file)
    if s and s["phase"] == "playing":
        return s
    try:
        f = fill_progress(logs_dir=logs_dir, now=now, registry_file=registry_file)
    except Exception:
        f = None
    return f or s


def streamed_games(home=None, cache_base_file=None, registry_file=None):
    """The streamed games on this Deck for the phone's storage list: slug, name and how much card space each one's cache
    uses (largest first). The slug comes from the launcher file, the name from the Steam tile registry."""
    home = Path(home) if home else HOME
    cache_base_file = Path(cache_base_file) if cache_base_file else STREAM_CACHE_BASE_FILE
    registry_file = Path(registry_file) if registry_file else LIVE_REGISTRY
    launchers = home / ".local" / "share" / "deckstream" / "launchers"
    bases = [home / ".local" / "share" / "deckstream" / "stream" / "cache"]
    try:
        card = Path(cache_base_file.read_text().strip())
        if card.name == "stream-cache":
            bases.append(card)
    except OSError:
        pass
    names = _tile_names(registry_file)
    games = []
    try:
        files = sorted(launchers.glob("stream_*.sh"))
    except OSError:
        files = []
    for f in files:
        slug = f.name[7:-3]
        if not _REMOVE_SLUG_RE.match(slug):
            continue
        size = 0
        for b in bases:
            c = b / slug
            if c.is_dir() and not c.is_symlink():
                size += _tree_bytes(c)
        games.append({"slug": slug, "name": names.get(slug) or slug.replace("_", " ").strip().title(),
                      "cache_gb": round(size / 1e9, 1)})
    games.sort(key=lambda g: -g["cache_gb"])
    return games[:50]


def streamed_games_cached(ttl=300, now=None):
    """streamed_games(), recomputed at most every few minutes (measuring tens of GB of cache is not free)."""
    now = now if now is not None else time.time()
    if now - _GAMES_CACHE["at"] >= ttl:
        try:
            _GAMES_CACHE["games"] = streamed_games()
        except Exception:
            pass
        _GAMES_CACHE["at"] = now
    return _GAMES_CACHE["games"]


def deck_status():
    """Heartbeat fields for the phone's Deck status card (the names api/queue.js stores): version, power, last
    self-update."""
    st = _load_update_state()
    out = {"version": installed_version(), "ac_online": on_ac_power(),
           "auto_update": UPDATE_SIGNERS.exists() and not AUTO_UPDATE_OFF.exists(),
           "warmup": warmup_on(), "warmup_applied_at": warmup_applied_at(), "deck_mode": deck_mode_state()}
    bat = battery_percent()
    if bat is not None:
        out["battery_pct"] = bat
    out["games"] = streamed_games_cached()
    out["active_game"] = live_activity()        # None clears the card on the phone once nothing fills or streams
    if st.get("last_update"):
        out["last_update"] = st["last_update"]
    return out


LAUNCH_REPORT = HOME / ".config" / "deckstream" / "launch_report.py"
_launch_report_mod = {}


def send_launch_reports(script=None):
    """Send finished launch timings (launch_report.py, written by the stream runner) to the user's site. Loaded from
    the installed file so an older install without it simply skips. Returns the number sent."""
    script = Path(script) if script else LAUNCH_REPORT
    if not ds_core.SERVER_URL or not script.exists():
        return 0
    mod = _launch_report_mod.get(str(script))
    if mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("launch_report", str(script))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _launch_report_mod[str(script)] = mod
    n = mod.send(ds_core.SERVER_URL, ds_core.QUEUE_SECRET, ds_core.DEVICE_ID)
    if n:
        print(f"[*] Sent {n} launch timing report(s) to your DeckStream site")
    return n
