#!/usr/bin/env python3
"""
DeckStream - Decky Loader Plugin Backend
Provides status, stream session telemetry, cache management, and performance toggles in SteamOS Gaming Mode.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
CONF_DIR = HOME / ".config" / "deckstream"
STATE_DIR = HOME / ".local" / "share" / "deckstream"
STREAM_DIR = STATE_DIR / "stream"

_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
DECK_MODE_ENV = "DECKSTREAM_FPS_CAP=30\nDECKSTREAM_LOW_PRESET=1\n"


def user_home():
    return HOME


def _conf(home=None):
    h = Path(home) if home else user_home()
    p = h / ".config" / "deckstream"
    p.mkdir(parents=True, exist_ok=True)
    return p


def deck_mode_state(home=None):
    c = _conf(home)
    try:
        raw = json.loads((c / "deck_mode.json").read_text())
    except (OSError, ValueError):
        return {}
    return {slug: {"on": (c / "deck_mode" / f"{slug}.env").is_file(), "at": int(v.get("at") or 0)}
            for slug, v in (raw.items() if isinstance(raw, dict) else []) if _SLUG_RE.match(str(slug)) and isinstance(v, dict)}


def set_deck_mode(slug, on, home=None, now_ms=None):
    """Write or remove the game's Deck mode file. Runs as root: every file it makes goes to the home's owner."""
    home = Path(home) if home else user_home()
    if not _SLUG_RE.match(str(slug or "")):
        return {"ok": False, "on": False, "text": "That game has no name the Deck can use"}
    c = _conf(home)
    try:
        owner = home.stat()
        (c / "deck_mode").mkdir(parents=True, exist_ok=True)
        made = [c / "deck_mode"]
        env = c / "deck_mode" / f"{slug}.env"
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
        tmp = c / "deck_mode.json.tmp"
        tmp.write_text(json.dumps(st))
        os.replace(tmp, c / "deck_mode.json")
        made.append(c / "deck_mode.json")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            for m in made:
                os.chown(m, owner.st_uid, owner.st_gid)
    except OSError as e:
        return {"ok": False, "on": env.is_file() if "env" in locals() else False,
                "text": f"Could not change it: {e.strerror or type(e).__name__}"}
    return {"ok": True, "on": bool(on), "text": f"Deck mode {'on' if on else 'off'}: starts with the next launch"}


def status(home=None, refresh=False):
    st = deck_mode_state(home)
    return {
        "deck_mode": {slug: info["on"] for slug, info in st.items()}
    }



class Plugin:
    async def _main(self):
        pass

    async def get_status(self):
        """Returns the daemon status, current version, and active mount status."""
        active_game = None
        current_marker = STREAM_DIR / "current_game.txt"
        if current_marker.exists():
            try:
                active_game = current_marker.read_text().strip()
            except Exception:
                pass

        daemon_running = False
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", "deckstream.service"],
                               capture_output=True, text=True, timeout=2)
            daemon_running = (r.stdout.strip() == "active")
        except Exception:
            pass

        return {
            "daemon_running": daemon_running,
            "active_game": active_game,
            "timestamp": int(time.time()),
        }

    async def get_storage_stats(self):
        """Returns local stream cache usage and total space on media root."""
        cache_dir = STREAM_DIR / "cache"
        cache_bytes = 0
        if cache_dir.exists():
            for root, _, files in os.walk(cache_dir):
                for f in files:
                    try:
                        cache_bytes += (Path(root) / f).stat().st_size
                    except OSError:
                        pass

        total_bytes = 0
        free_bytes = 0
        try:
            stat = os.statvfs(str(STREAM_DIR))
            total_bytes = stat.f_blocks * stat.f_frsize
            free_bytes = stat.f_bavail * stat.f_frsize
        except Exception:
            pass

        return {
            "cache_bytes": cache_bytes,
            "cache_gb": round(cache_bytes / (1024 ** 3), 2),
            "free_gb": round(free_bytes / (1024 ** 3), 2),
            "total_gb": round(total_bytes / (1024 ** 3), 2),
        }

    async def clear_cache(self, game_id=None):
        """Clears local cache for a specific game or all stream caches."""
        cache_dir = STREAM_DIR / "cache"
        freed = 0
        if not cache_dir.exists():
            return {"ok": True, "freed_gb": 0}

        target = cache_dir / game_id if game_id else cache_dir
        if target.exists():
            for root, _, files in os.walk(target):
                for f in files:
                    try:
                        p = Path(root) / f
                        freed += p.stat().st_size
                        p.unlink()
                    except OSError:
                        pass
        return {"ok": True, "freed_gb": round(freed / (1024 ** 3), 2)}

    async def set_deck_mode(self, game_id, enabled):
        """Sets per-game Deck mode preset (30 fps cap & low graphics)."""
        mode_file = CONF_DIR / "deck_modes" / f"{game_id}.json"
        mode_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if enabled:
                mode_file.write_text(json.dumps({"fps_cap": 30, "low_preset": True}))
            else:
                if mode_file.exists():
                    mode_file.unlink()
            return {"ok": True, "enabled": enabled}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def get_deck_mode(self, game_id):
        """Gets per-game Deck mode setting."""
        mode_file = CONF_DIR / "deck_modes" / f"{game_id}.json"
        if mode_file.exists():
            try:
                data = json.loads(mode_file.read_text())
                return {"enabled": True, "data": data}
            except Exception:
                pass
        return {"enabled": False}
