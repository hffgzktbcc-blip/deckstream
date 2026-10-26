#!/usr/bin/env python3
"""Launch timings for streamed games.

stream-runner.sh writes one key=value file per launch in ~/.local/share/deckstream/launches/ (Play, fill start/end,
mount, game start, cache size at launch and every 30 s while the game runs). Steam ends the runner when the game exits,
so this module, run by the daemon, turns FINISHED launches into a short summary and sends them to the user's own
DeckStream site (/api/launch, behind the queue password), so every launch is measured without anyone watching.

Usage: launch_report.py list            # summaries of finished launches not yet sent
       launch_report.py send            # send them (uses ~/.config/deckstream/daemon_config.json)
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
LAUNCH_DIR = HOME / ".local" / "share" / "deckstream" / "launches"
CONFIG = HOME / ".config" / "deckstream" / "daemon_config.json"
QUIET_SECONDS = 120          # no stats line for 2 minutes and the runner gone = the session is over
KEEP_SENT = 50


def parse(path):
    """The last value of each key (the stats loop appends cache_mb_last/last_seen every 30 s)."""
    vals = {}
    try:
        for line in Path(path).read_text(errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and k:
                vals[k.strip()] = v.strip()
    except OSError:
        pass
    return vals


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


EXIT_REASONS = ("dxvk", "vkd3d", "oom", "access_violation", "d3d11", "prefix_timeout", "anticheat", "steam_api",
                "unknown_early")        # same list as proton_matcher.EXIT_REASONS and api/launch.js


def summarize(vals):
    """A summary with plain numbers (seconds, MB); missing milestones are left out."""
    n = {k: _num(v) for k, v in vals.items()}
    out = {"slug": vals.get("slug", ""), "play": n.get("play"),
           "mode": "gaming" if "gamescope" in vals.get("desktop", "").lower() else
                   ("desktop" if vals.get("desktop", "unknown") != "unknown" else "unknown")}

    def diff(a, b):
        return n[b] - n[a] if n.get(a) is not None and n.get(b) is not None and n[b] >= n[a] else None

    fields = {
        "fill_seconds": diff("fill_start", "fill_end"),
        "play_to_launch_seconds": diff("play", "launched"),
        "session_seconds": diff("launched", "last_seen"),
        "fill_mb": diff("cache_mb_before_fill", "cache_mb_at_launch"),
        "downloaded_while_playing_mb": diff("cache_mb_at_launch", "cache_mb_last"),
        "fill_rc": n.get("fill_rc"),
        "exit_seconds": n.get("exit_seconds"),
        "exit_reason": vals.get("exit_reason") if vals.get("exit_reason") in EXIT_REASONS else None,
    }
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def _runner_alive(path):
    """The runner's pid is the last part of the file name: <slug>.<epoch>.<pid>.txt."""
    try:
        pid = int(Path(path).name.rsplit(".", 2)[-2])
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"stream-runner.sh" in f.read()
    except (ValueError, OSError, IndexError):
        return False


def finished(launch_dir=None, now=None, alive=_runner_alive):
    """(path, summary) of launches whose session is over, oldest first."""
    d = Path(launch_dir) if launch_dir else LAUNCH_DIR
    now = now if now is not None else time.time()
    out = []
    for p in sorted(d.glob("*.txt")) if d.is_dir() else []:
        vals = parse(p)
        last = _num(vals.get("last_seen")) or _num(vals.get("launched")) or _num(vals.get("play")) or 0
        if alive(p) or now - max(last, p.stat().st_mtime) < QUIET_SECONDS:
            continue
        s = summarize(vals)
        if s["slug"] and s.get("play"):
            out.append((p, s))
        else:
            p.rename(p.with_suffix(".bad"))           # never retried
    return out


def send(server_url, secret, device_id, launch_dir=None, opener=urllib.request.urlopen, now=None):
    """Post each finished launch; a sent file is renamed .sent (the newest KEEP_SENT are kept). Returns the count."""
    sent = 0
    for p, s in finished(launch_dir, now):
        body = json.dumps({"device": device_id, "launch": s}).encode()
        req = urllib.request.Request(server_url.rstrip("/") + "/api/launch", data=body,
                                     headers={"Content-Type": "application/json", "x-deckstream-secret": secret})
        try:
            with opener(req, timeout=10) as r:
                r.read(4096)
        except Exception:
            break                                     # offline or old site: try again next time
        p.rename(p.with_suffix(".sent"))
        sent += 1
    d = Path(launch_dir) if launch_dir else LAUNCH_DIR
    for old in sorted(d.glob("*.sent"), key=lambda q: q.stat().st_mtime)[:-KEEP_SENT] if d.is_dir() else []:
        old.unlink(missing_ok=True)
    return sent


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        for p, s in finished():
            print(p.name, json.dumps(s))
        return 0
    if cmd == "send":
        try:
            cfg = json.loads(CONFIG.read_text())
        except (OSError, ValueError):
            return 2
        if not cfg.get("server_url"):
            return 2
        print(f"sent {send(cfg['server_url'], cfg.get('queue_secret', ''), cfg.get('device_id', 'deck-main'))}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
