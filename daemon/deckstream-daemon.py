#!/usr/bin/env python3
"""
DeckStream Daemon for Steam Deck (SteamOS)
Monitors DeckStream queue, downloads games from RemoteStorage directly to Steam Deck,
extracts archives, detects the game executable, configures Proton compatibility,
adds the game as a Non-Steam shortcut, and fetches SteamGridDB artwork.
"""

import errno
import hashlib
import math
import os
import sys
import time
import json
import zlib
import struct
import shutil
import subprocess
import tarfile
import re
import traceback
import urllib.request
import urllib.error
import signal
import threading
import urllib.parse
import socket
import ipaddress
from pathlib import Path

def cap_proton_logs():
    """Rotate any Proton log over 25 MB (a launch that never reached the runner's exit step leaves it growing)."""
    try:
        import proton_matcher
        proton_matcher.cap_logs_in(str(Path.home() / ".local/share/deckstream/logs"))
    except Exception:
        pass


# Defaults & Locations on Steam Deck
HOME = Path.home()
GAMES_DIR_INTERNAL = HOME / "Games"
GAMES_DIR_SD = Path("/run/media")

STEAM_DIR_CANDIDATES = [
    HOME / ".steam" / "steam",
    HOME / ".local" / "share" / "Steam",
    HOME / ".steam" / "root"
]

CONFIG_FILE = HOME / ".config" / "deckstream" / "daemon_config.json"
QUEUE_SECRET = ""
SERVER_URL = ""          # set from the config when the daemon starts; used by the profile backup
DEVICE_ID = "deck-main"


DEFAULT_DOWNLOAD_HOSTS = ["storage.app", "tb-cdn.io", "tb-cdn.to"]  # RemoteStorage CDN links moved to *.tb-cdn.to (e.g. nexus-210.zafr.tb-cdn.to, Johannesburg) in 2026
ARTWORK_HOSTS = ["steamgriddb.com", "steamstatic.com"]
ALLOWED_DOWNLOAD_HOSTS = list(DEFAULT_DOWNLOAD_HOSTS)


def host_matches(host, allowed):
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def is_public_host(host):
    """True only if every address the host resolves to is public (blocks LAN, loopback, metadata)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return bool(infos)


def check_remote_url(url, allowed_hosts):
    """Raise unless url is https, on an allowed host, and resolves to public addresses."""
    u = urllib.parse.urlparse(str(url))
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ValueError("only plain https URLs are allowed")
    if not host_matches(u.hostname, allowed_hosts):
        raise ValueError(f"host not allowed: {u.hostname}")
    if not is_public_host(u.hostname):
        raise ValueError(f"host does not resolve to a public address: {u.hostname}")


def safe_dir_name(name):
    """Turn a job name into a single safe folder name (no separators, no dot-dot, bounded)."""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", str(name)).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)[:100].strip(" .")
    return cleaned or "Unnamed Game"


class _NoRedirectToUnsafe(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop against the same URL policy."""
    allowed = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_remote_url(newurl, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_checked(url, allowed_hosts, timeout, headers=None):
    check_remote_url(url, allowed_hosts)
    handler = _NoRedirectToUnsafe()
    handler.allowed = allowed_hosts
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "DeckStream/1.0", **(headers or {})})
    return opener.open(req, timeout=timeout)


# --- job state: lets the installer know a job is running so it can wait before restarting the daemon
STATE_DIR = HOME / ".local" / "share" / "deckstream"
CURRENT_JOB = STATE_DIR / "current_job.json"
RESTART_MARKER = STATE_DIR / "restart-pending"


def set_current_job(job_id, name):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_JOB.write_text(json.dumps({"id": job_id, "name": name, "pid": os.getpid(), "started": int(time.time())}))


def clear_current_job():
    try:
        CURRENT_JOB.unlink()
    except OSError:
        pass


def consume_restart_request():
    """True if an update asked for a restart while a job was running; clears the request."""
    if RESTART_MARKER.exists():
        try:
            RESTART_MARKER.unlink()
        except OSError:
            pass
        return True
    return False


def queue_request(url, data=None, headers=None):
    """Build a request to the DeckStream queue, adding the shared queue password."""
    h = dict(headers or {})
    if QUEUE_SECRET:
        h["x-deckstream-secret"] = QUEUE_SECRET
    return urllib.request.Request(url, data=data, headers=h)


def load_config():
    default_cfg = {
        "server_url": "",  # required: your own Vercel domain, set by install.sh
        "device_id": "deck-main",
        "poll_interval_sec": 10,
        "default_storage": "internal", # "internal" or "sd"
        "proton_compatibility": True,
        "allowed_download_hosts": []  # extra hosts (suffix match) a download job may use
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                default_cfg.update(loaded)
        except Exception as e:
            print(f"[!] Warning reading config: {e}")
    else:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=2)
    return default_cfg


def get_disk_usage(path):
    try:
        p = Path(path)
        if not p.exists():
            return {"free": 0, "total": 0, "percent": 0}
        stat = shutil.disk_usage(p)
        return {
            "free": stat.free,
            "total": stat.total,
            "used": stat.used,
            "percent": round((stat.used / stat.total) * 100, 1)
        }
    except Exception:
        return {"free": 0, "total": 0, "percent": 0}


def get_micro_sd_path():
    # 1. Most reliable: find where the SD card (mmcblk*) is actually mounted
    candidates = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/mmcblk"):
                    candidates.append(Path(parts[1].replace("\\040", " ")))
    except OSError:
        pass

    # 2. Fallback: scan /run/media, skipping anything we can't read (e.g. /run/media/root)
    if not candidates and GAMES_DIR_SD.exists():
        try:
            for entry in GAMES_DIR_SD.iterdir():
                try:
                    if not os.access(entry, os.R_OK | os.X_OK):
                        continue
                    if os.path.ismount(entry):
                        candidates.append(entry)
                    else:
                        for card in entry.iterdir():
                            if card.is_dir() and os.path.ismount(card) and os.access(card, os.W_OK):
                                candidates.append(card)
                except OSError:
                    continue
        except OSError:
            pass

    for card in candidates:
        try:
            if os.access(card, os.W_OK):
                games_folder = card / "Games"
                games_folder.mkdir(parents=True, exist_ok=True)
                return games_folder
        except OSError:
            continue
    return None


def find_steam_userdata_dir():
    for base in STEAM_DIR_CANDIDATES:
        userdata = base / "userdata"
        if userdata.exists() and userdata.is_dir():
            users = [u for u in userdata.iterdir() if u.is_dir() and u.name.isdigit() and u.name != "0"]
            if users:
                # Most recently active account (localconfig.vdf is touched on every login)
                def last_used(u):
                    lc = u / "config" / "localconfig.vdf"
                    try:
                        return lc.stat().st_mtime if lc.exists() else u.stat().st_mtime
                    except OSError:
                        return 0
                return max(users, key=last_used)
    return None


def calculate_shortcut_id(target, app_name):
    # Standard Valve CRC calculation for shortcut AppIDs
    combined = f"{target}{app_name}".encode("utf-8")
    crc = zlib.crc32(combined)
    # Steam unsigned 32-bit ID with high bit set
    app_id = (crc | 0x80000000) & 0xFFFFFFFF
    return app_id


def parse_binary_vdf(data, strict=False):
    """
    Parses Steam binary shortcuts.vdf into a list of shortcut dictionaries.
    With strict=True anything unexpected (unknown value type, truncated data, missing header) raises
    ValueError, so a caller that is about to rewrite the file can refuse instead of losing entries.
    """
    if not data or not data.startswith(b"\x00shortcuts\x00"):
        if strict and data:
            raise ValueError("not a shortcuts.vdf file")
        return []
    
    idx = len(b"\x00shortcuts\x00")

    def read_string(offset):
        end = data.find(b"\x00", offset)
        if end == -1:
            return "", offset
        val = data[offset:end].decode("utf-8", errors="replace")
        return val, end + 1

    def read_dict(offset):
        res = {}
        while offset < len(data):
            type_byte = data[offset:offset+1]
            offset += 1
            if type_byte == b"\x08" or not type_byte:
                break
            key, offset = read_string(offset)
            if type_byte == b"\x00":
                sub, offset = read_dict(offset)
                res[key] = sub
            elif type_byte == b"\x01":
                val, offset = read_string(offset)
                res[key] = val
            elif type_byte == b"\x02":
                if strict and offset + 4 > len(data):
                    raise ValueError("truncated integer")
                val = struct.unpack("<I", data[offset:offset+4])[0]
                offset += 4
                res[key] = val
            elif strict:
                raise ValueError(f"unsupported value type {type_byte!r} for '{key}'")
        return res, offset

    try:
        root_dict, _ = read_dict(idx)
        shortcuts = []
        for k in sorted(root_dict.keys(), key=lambda x: int(x) if x.isdigit() else 9999):
            if isinstance(root_dict[k], dict):
                shortcuts.append(root_dict[k])
        return shortcuts
    except Exception as e:
        if strict:
            raise ValueError(str(e)) from e
        print(f"[!] Error parsing shortcuts.vdf: {e}")
        return []


def serialize_binary_vdf(shortcuts):
    """
    Serializes a list of shortcut dictionaries into Steam binary shortcuts.vdf.
    """
    out = bytearray()
    out.extend(b"\x00shortcuts\x00")

    for i, item in enumerate(shortcuts):
        out.extend(b"\x00")
        out.extend(str(i).encode("utf-8") + b"\x00")
        
        for k, v in item.items():
            if isinstance(v, str):
                out.extend(b"\x01")
                out.extend(k.encode("utf-8") + b"\x00")
                out.extend(v.encode("utf-8") + b"\x00")
            elif isinstance(v, int):
                out.extend(b"\x02")
                out.extend(k.encode("utf-8") + b"\x00")
                out.extend(struct.pack("<I", v & 0xFFFFFFFF))
            elif isinstance(v, dict):
                out.extend(b"\x00")
                out.extend(k.encode("utf-8") + b"\x00")
                for sub_k, sub_v in v.items():
                    out.extend(b"\x01")
                    out.extend(str(sub_k).encode("utf-8") + b"\x00")
                    out.extend(str(sub_v).encode("utf-8") + b"\x00")
                out.extend(b"\x08")
        out.extend(b"\x08")
    out.extend(b"\x08\x08")
    return bytes(out)


def find_best_executable(game_dir):
    """
    Finds the main game executable in game_dir.
    Filters out launchers, unins000, crash handlers, redists, and dependencies.
    """
    p = Path(game_dir)
    exe_candidates = []
    
    ignore_patterns = [
        "unins", "setup", "crash", "reporter", "dxwebsetup", "vcredist",
        "dotnet", "easyanticheat", "battleye", "steam_api", "unitycrash",
        "redist", "prerequisites", "install", "config", "benchmark", "patch", "handler"
    ]

    for root, _, files in os.walk(p):
        for f in files:
            if f.lower().endswith(".exe"):
                full_path = Path(root) / f
                name_lower = f.lower()
                
                # Check ignore
                if any(ign in name_lower for ign in ignore_patterns):
                    continue
                
                size = full_path.stat().st_size
                # Depth from game_dir
                rel = full_path.relative_to(p)
                depth = len(rel.parts)
                
                exe_candidates.append({
                    "path": full_path,
                    "name": f,
                    "size": size,
                    "depth": depth,
                    "name_match": 1 if p.name.lower() in name_lower else 0
                })

    if not exe_candidates:
        return None

    # Sort by: name match desc, depth asc, size desc
    exe_candidates.sort(key=lambda x: (x["name_match"], -x["depth"], x["size"]), reverse=True)
    return str(exe_candidates[0]["path"])


def find_native_start(folder):
    """A native Linux game (GOG .sh unpacked by cloud-install.sh): start.sh, else an executable ELF file near the top."""
    folder = Path(folder)
    start = folder / "start.sh"
    if start.is_file() and not start.is_symlink():
        return start
    for depth in ("*", "*/*"):
        for f in sorted(folder.glob(depth)):
            try:
                if f.is_file() and not f.is_symlink() and os.access(f, os.X_OK):
                    with open(f, "rb") as fh:
                        if fh.read(4) == b"\x7fELF":
                            return f
            except OSError:
                continue
    return None


def make_native_runner(game_name, start_path):
    """Shortcut script for a native Linux game: no Proton prefix, no umu, no game-launcher.sh. Runs from its own folder."""
    runners_dir = HOME / ".local" / "share" / "deckstream" / "runners"
    runners_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", game_name).lower().strip("_")[:80] or "game"
    runner = runners_dir / f"native_{slug}.sh"
    start_path = Path(start_path)

    def q(x):
        return "'" + str(x).replace("'", "'\"'\"'") + "'"

    runner.write_text(f"#!/usr/bin/env bash\ncd {q(start_path.parent)} || exit 1\nexec {q(start_path)} \"$@\"\n")
    runner.chmod(0o755)
    return str(runner)


def ensure_numeric_path(path, slug):
    """GE-Proton crashes if the prefix path has no digits at all (it reads the game id from it).
    Existing prefixes that already contain a digit keep their name so saves are not orphaned."""
    path = Path(path)
    if re.search(r"\d", str(path)):
        return path
    return path.with_name(f"{path.name}_{zlib.crc32(slug.encode()) & 0xFFFFFFFF}")


def prefix_dir_for(exe_path, slug):
    """Where the game's Proton prefix lives: on the same SD card as the game when that card
    supports symlinks (ext4), otherwise on the internal drive."""
    exe = Path(exe_path)
    parts = exe.parts
    if len(parts) > 4 and parts[1] == "run" and parts[2] == "media":
        base = Path(*parts[:5]) / ".deckstream" / "prefixes"
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / f".symlink_probe_{os.getpid()}"
            os.symlink("probe", probe)
            probe.unlink()
            return ensure_numeric_path(base / slug, slug)
        except OSError:
            pass
    return ensure_numeric_path(HOME / ".local" / "share" / "deckstream" / "prefixes" / slug, slug)


def make_game_launcher(game_name, exe_path):
    """Windows games need Proton, and a plain non-Steam shortcut to an .exe has no compatibility tool
    (Steam then tries to run it natively and nothing happens). Point the shortcut at a tiny script
    that runs the game through game-launcher.sh instead. Returns the script path."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", game_name).lower()[:80] or "game"
    launcher_dir = HOME / ".local" / "share" / "deckstream" / "launchers"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    script = launcher_dir / f"play_{slug}.sh"
    helper = HOME / ".config" / "deckstream" / "game-launcher.sh"

    def q(s):
        return "'" + str(s).replace("'", "'\"'\"'") + "'"

    with open(script, "w") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write(f"exec bash {q(helper)} {q(exe_path)} {q(prefix_dir_for(exe_path, slug))} {q(slug)} {q(game_name)}\n")
    os.chmod(script, 0o755)
    return str(script)


PENDING_FILE = HOME / ".config" / "deckstream" / "pending_shortcuts.json"


def steam_running():
    try:
        return subprocess.run(["pgrep", "-x", "steam"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _load_pending():
    try:
        return json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        return []


# ---- live shortcuts (opt-in): add games while Steam runs, through Steam's own client API -------------------
# Steam only accepts new shortcuts while running through its client-side JavaScript API (window.SteamClient), reachable
# over Steam's CEF remote-debugging port on 127.0.0.1:8080. That port exists only when the file
# .cef-enable-remote-debugging is in Steam's folder when Steam starts. Heroic, NonSteamLaunchers and Decky Loader use
# the same interface. It has NO authentication: while it is on, any program running as this user can control Steam
# through it, so it is off unless the user turns it on (deckstream-daemon.py --live-shortcuts on).
LIVE_CONF = HOME / ".config" / "deckstream" / "live_shortcuts.json"     # {"enabled": bool, "created_flag": bool}
LIVE_REGISTRY = HOME / ".config" / "deckstream" / "live_shortcuts_added.json"   # game name -> {"appid", "exe"}
CEF_PORT = 8080
CEF_TITLES = ("SharedJSContext", "Steam Shared Context presented by Valve™", "Steam", "SP")
ART_TYPES = {"grid": 0, "hero": 1, "logo": 2, "header": 3}           # SteamClient.Apps.SetCustomArtworkForApp types


def steam_root():
    for p in (HOME / ".steam" / "steam", HOME / ".local" / "share" / "Steam"):
        if p.is_dir():
            return p
    return HOME / ".steam" / "steam"


def cef_flag_file():
    return steam_root() / ".cef-enable-remote-debugging"


def live_enabled():
    try:
        return bool(json.loads(LIVE_CONF.read_text()).get("enabled"))
    except (OSError, ValueError, AttributeError):
        return False


def set_live_shortcuts(on):
    """Turn live shortcuts on or off. On: create Steam's CEF flag file (takes effect at the next Steam start) and
    remember whether we created it. Off: remove the flag file only if we created it (Decky Loader needs it too)."""
    try:
        conf = json.loads(LIVE_CONF.read_text())
    except (OSError, ValueError):
        conf = {}
    flag = cef_flag_file()
    if on:
        if not flag.exists():
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
            conf["created_flag"] = True
        conf["enabled"] = True
    else:
        if conf.get("created_flag"):
            flag.unlink(missing_ok=True)
        conf = {"enabled": False, "created_flag": False}
    LIVE_CONF.parent.mkdir(parents=True, exist_ok=True)
    LIVE_CONF.write_text(json.dumps(conf))
    return conf


def _cef_target(port=CEF_PORT, timeout=3):
    """The websocket URL of Steam's shared JS context, or None. Loopback only."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=timeout) as r:
        targets = json.loads(r.read().decode("utf-8", "replace"))
    for title in CEF_TITLES:
        for t in targets:
            ws = str(t.get("webSocketDebuggerUrl") or "")
            if t.get("title") == title and ws.startswith(("ws://127.0.0.1:", "ws://localhost:")):
                return ws
    return None


def _ws_eval(ws_url, expression, timeout=15):
    """Minimal websocket client (RFC 6455) that sends one Runtime.evaluate and returns its result value."""
    import base64
    u = urllib.parse.urlparse(ws_url)
    if u.hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError("refusing a non-loopback debugger address")
    s = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("debugger closed the connection")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("debugger refused the websocket: " + head.split(b"\r\n", 1)[0].decode("latin-1"))
        buf = head.split(b"\r\n\r\n", 1)[1]
        payload = json.dumps({"id": 1, "method": "Runtime.evaluate",
                              "params": {"expression": expression, "awaitPromise": True, "returnByValue": True}}).encode()
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                               bytes([0x80 | 126]) + n.to_bytes(2, "big") if n < 65536 else
                               bytes([0x80 | 127]) + n.to_bytes(8, "big"))
        s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        def need(k):
            nonlocal buf
            while len(buf) < k:
                chunk = s.recv(65536)
                if not chunk:
                    raise RuntimeError("debugger closed the connection")
                buf += chunk
            out, buf = buf[:k], buf[k:]
            return out

        message = b""
        while True:
            b0, b1 = need(2)
            length = b1 & 0x7F
            if length == 126:
                length = int.from_bytes(need(2), "big")
            elif length == 127:
                length = int.from_bytes(need(8), "big")
            m = need(4) if b1 & 0x80 else None
            data = need(length)
            if m:
                data = bytes(b ^ m[i % 4] for i, b in enumerate(data))
            opcode = b0 & 0x0F
            if opcode == 8:
                raise RuntimeError("debugger closed the websocket")
            if opcode in (0, 1):
                message += data
                if b0 & 0x80:
                    reply = json.loads(message.decode("utf-8", "replace"))
                    message = b""
                    if reply.get("id") == 1:
                        if reply.get("result", {}).get("exceptionDetails"):
                            raise RuntimeError(str(reply["result"]["exceptionDetails"].get("exception", {}).get("description", "script error"))[:300])
                        return reply.get("result", {}).get("result", {}).get("value")
    finally:
        s.close()


def live_shortcut_script(name, exe, start_dir, existing_appid=0, art=()):
    """The JavaScript run inside Steam. AddShortcut ignores the start dir and launch options (and newer clients the
    name), so the Set* calls follow once Steam has the app's overview. Paths are passed unquoted (Steam quotes them)."""
    a = json.dumps
    return f"""(async () => {{
  const A = SteamClient.Apps, name = {a(name)}, exe = {a(exe)}, dir = {a(start_dir)};
  const has = (id) => !!(id && window.appStore && appStore.GetAppOverviewByAppID(id));
  let id = {int(existing_appid or 0)};
  if (id && window.appStore && !has(id)) id = 0;
  if (!id) {{
    id = await A.AddShortcut(name, exe, dir, "");
    if (!id) return {{ok: false, error: "AddShortcut returned no app id"}};
    for (let i = 0; i < 30 && window.appStore && !has(id); i++) await new Promise(r => setTimeout(r, 100));
  }}
  A.SetShortcutName(id, name); A.SetShortcutExe(id, exe); A.SetShortcutStartDir(id, dir); A.SetAppLaunchOptions(id, "");
  for (const [data, ext, type] of {a([list(x) for x in art])}) {{ try {{ await A.SetCustomArtworkForApp(id, data, ext, type); }} catch (e) {{}} }}
  return {{ok: true, appid: id}};
}})()"""


def _vdf_appid_for(game_name, exe_path):
    """App id (unsigned) of our existing shortcut for this game in shortcuts.vdf, or 0."""
    userdata = find_steam_userdata_dir()
    f = (userdata / "config" / "shortcuts.vdf") if userdata else None
    if not f or not f.exists():
        return 0
    try:
        for s in parse_binary_vdf(f.read_bytes(), strict=True):
            if _is_ours(s, game_name, str(exe_path)) and isinstance(s.get("appid"), int):
                return s["appid"] & 0xFFFFFFFF          # stored signed; the client API takes unsigned
    except Exception:
        pass
    return 0


def live_add_shortcut(game_name, exe_path, artwork_urls=None, cef_target=None, evaluate=None):
    """Add or update a shortcut through the running Steam client. Returns the app id, or None if the live route is
    not available (then the caller falls back to adding it when Steam closes)."""
    cef_target = cef_target or _cef_target
    evaluate = evaluate or _ws_eval
    try:
        ws = cef_target()
    except Exception:
        return None
    if not ws:
        return None
    if str(exe_path).lower().endswith(".exe"):
        exe_path = make_game_launcher(game_name, exe_path)
    try:
        registry = json.loads(LIVE_REGISTRY.read_text())
    except (OSError, ValueError):
        registry = {}
    known = registry.get(game_name, {})
    existing = known.get("appid", 0) if known.get("exe") == str(exe_path) else 0
    if not existing:
        existing = _vdf_appid_for(game_name, exe_path)   # already in Steam from shortcuts.vdf: update it, don't add a twin
    art = []
    grid_dir = None
    if isinstance(artwork_urls, dict) and artwork_urls:
        userdata = find_steam_userdata_dir()
        grid_dir = (userdata / "config" / "grid") if userdata else None
    try:
        value = evaluate(ws, live_shortcut_script(game_name, str(exe_path), str(Path(exe_path).parent), existing))
    except Exception as e:
        print(f"[!] Live shortcut for '{game_name}' failed: {e}")
        return None
    if not isinstance(value, dict) or not value.get("ok") or not value.get("appid"):
        print(f"[!] Live shortcut for '{game_name}' was not created: {value}")
        return None
    app_id = int(value["appid"])
    registry[game_name] = {"appid": app_id, "exe": str(exe_path)}
    LIVE_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    LIVE_REGISTRY.write_text(json.dumps(registry))
    if grid_dir:
        # Keep the files in Steam's grid folder (survives restarts) and hand them to the running Steam as well.
        import base64
        grid_dir.mkdir(parents=True, exist_ok=True)
        download_artwork(grid_dir, app_id, artwork_urls)
        names = {"grid": f"{app_id}p.jpg", "hero": f"{app_id}_hero.jpg", "logo": f"{app_id}_logo.png", "header": f"{app_id}.jpg"}
        for kind, fname in names.items():
            f = grid_dir / fname
            if f.is_file() and f.stat().st_size < 8 * 1048576:
                art.append((base64.b64encode(f.read_bytes()).decode(), fname.rsplit(".", 1)[1], ART_TYPES[kind]))
        if art:
            try:
                evaluate(ws, live_shortcut_script(game_name, str(exe_path), str(Path(exe_path).parent), app_id, art))
            except Exception as e:
                print(f"[!] Artwork for '{game_name}' was not applied live ({e}); it shows after Steam restarts")
    print(f"[✓] Added '{game_name}' to Steam (live, AppID {app_id})")
    return app_id


def request_shortcut(game_name, exe_path, artwork_urls=None):
    """Steam keeps shortcuts.vdf in memory and rewrites it when it exits, which erases anything added
    while it runs. Add the shortcut now if Steam is closed; if it runs and live shortcuts are on, add it through
    Steam's client API; otherwise remember it for later. Returns (applied, app_id)."""
    if not steam_running():
        return True, add_non_steam_shortcut(game_name, exe_path, artwork_urls)
    if live_enabled():
        app_id = live_add_shortcut(game_name, exe_path, artwork_urls)
        if app_id:
            return True, app_id
    pending = [p for p in _load_pending() if p.get("name") != game_name]
    pending.append({"name": game_name, "exe": str(exe_path), "artwork": artwork_urls if isinstance(artwork_urls, dict) else None})
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(pending))
    print(f"[*] Steam is running; '{game_name}' will be added to your library as soon as Steam is closed.")
    return False, None


SESSIONS_DIR = HOME / ".local" / "share" / "deckstream" / "sessions"
STARTUP_PROFILE = HOME / ".config" / "deckstream" / "startup_profile.py"
_finishing = {}


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


# ---- prestage: fill a stream game's cache when it is QUEUED, so the first Play is already warm ---------------------
PRESTAGE_DIR = HOME / ".local" / "share" / "deckstream" / "prestage"
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


_REPACK_OR_ARCHIVE_NAME_RE = re.compile(
    r"((?:^|[\W_])(installer|archive|repack)(?:[\W_]|$)|(\.(zip|rar|7z|iso|nsp|xci|nsz|xcz)($|[\W_])))", re.I
)

_ARCHIVE_RANGE_RE = re.compile(r"(\.(zip|rar|7z|r\d{2}|part\d+\.rar)$|(^|/)(fg-\d+|fg-optional[^/]*|setup-?\d*)\.bin$)", re.I)


def _is_streamable_release(slug, folder="", plan=None, prestage_dir=None, profiles_dir=None):
    """True only when a release is streamable (kind == 'plain' and not a repack/archive folder or failed archive launch)."""
    if isinstance(plan, dict):
        pk = str(plan.get("kind") or "").strip().lower()
        if pk and pk != "plain":
            return False
    if _REPACK_OR_ARCHIVE_NAME_RE.search(f"{slug or ''} {folder or ''}"):
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
    if dest.exists() or not SERVER_URL or not folder:
        return False
    opener = opener or urllib.request.urlopen
    url = f"{SERVER_URL}/api/profile?key={profile_key(folder)}"
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
    if not SERVER_URL or not PROFILES_DIR.is_dir():
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
            with opener(queue_request(f"{SERVER_URL}/api/profile", data=data, headers={"Content-Type": "application/json"}),
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


# ---- self-update: install new versions from the user's site by itself, only when the Deck is idle, and only when the
# manifest is signed by the update key pinned at the first install (update_signers). install.sh does the staging,
# checksums, syntax checks, backup, atomic swap, health check and rollback (DECKSTREAM_AUTO_UPDATE=1).
INSTALL_DIR = HOME / ".config" / "deckstream"
UPDATE_SIGNERS = INSTALL_DIR / "update_signers"
AUTO_UPDATE_OFF = INSTALL_DIR / "auto_update.disabled"
SELF_UPDATE_STATE = STATE_DIR / "self_update.json"
SELF_UPDATE_DIR = STATE_DIR / "self_update"
SELF_UPDATE_CHECK_SECONDS = 30 * 60
SELF_UPDATE_RETRY_SECONDS = 6 * 3600
MANIFEST_NAMESPACE = "deckstream-manifest"
_self_update_note = {}


def _load_update_state():
    try:
        return json.loads(SELF_UPDATE_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_update_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SELF_UPDATE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, SELF_UPDATE_STATE)


def installed_version():
    try:
        return json.loads((INSTALL_DIR / "manifest.json").read_text()).get("version", "")
    except (OSError, ValueError):
        return ""


def installed_released():
    try:
        r = json.loads((INSTALL_DIR / "manifest.json").read_text()).get("released")
        return r if isinstance(r, int) else None
    except (OSError, ValueError):
        return None


def verify_manifest_signature(manifest_path, sig_path, signers=None, run=subprocess.run):
    """True only when ssh-keygen confirms sig_path signs manifest_path with a key in the pinned signers file."""
    signers = Path(signers) if signers else UPDATE_SIGNERS
    if not signers.exists() or not Path(sig_path).exists() or not shutil.which("ssh-keygen"):
        return False
    try:
        with open(manifest_path, "rb") as f:
            r = run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", "deckstream", "-n", MANIFEST_NAMESPACE,
                     "-s", str(sig_path)], stdin=f, capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _uptime_seconds(path="/proc/uptime"):
    try:
        return float(Path(path).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def battery_percent(root=None):
    root = Path(root) if root else POWER_SUPPLY_DIR
    try:
        for d in root.iterdir():
            if (d / "type").read_text().strip() == "Battery":
                return int((d / "capacity").read_text().strip())
    except (OSError, ValueError):
        pass
    return None


def deck_idle_for_update(proc_root="/proc"):
    """Why the Deck is not idle enough to update, or '' when it is."""
    if fills_blocked(proc_root):
        return "a game or a session wrap-up is running"
    if running_fills(proc_root):
        return "a background fill is running"
    if CURRENT_JOB.exists():
        return "an install job is running"
    if _uptime_seconds() < 300:
        return "the Deck has just started"
    bat = battery_percent()
    if not on_ac_power() and bat is not None and bat < 30:
        return "the battery is under 30% and not charging"
    return ""


def self_update_pass(now=None, opener=None, spawn=subprocess.Popen, proc_root="/proc", force=False):
    """Check the site for a new signed version (every 30 min) and install it when idle. Returns what it did."""
    now = now if now is not None else time.time()
    if AUTO_UPDATE_OFF.exists() or not SERVER_URL:
        return "off"
    if not UPDATE_SIGNERS.exists():
        if not _self_update_note.get("nokey"):
            print("[*] Self-update is off: no update key pinned yet (re-run the installer once to enable it)")
            _self_update_note["nokey"] = True
        return "no-key"
    state = _load_update_state()
    if not force and now - state.get("last_check", 0) < SELF_UPDATE_CHECK_SECONDS:
        return "later"
    last = state.get("last_update") or {}
    failed = str(last.get("result") or "").startswith(("rolled-back", "failed"))
    if not force and failed and now - last.get("time", 0) < SELF_UPDATE_RETRY_SECONDS:
        return "backoff"
    state["last_check"] = now
    _save_update_state(state)
    opener = opener or urllib.request.urlopen
    SELF_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    man, sig = SELF_UPDATE_DIR / "manifest.json", SELF_UPDATE_DIR / "manifest.json.sig"

    def fetch(name, dest, limit):
        with opener(urllib.request.Request(f"{SERVER_URL}/daemon/{name}", headers={"User-Agent": "DeckStream/1.0"}),
                    timeout=20) as r:
            data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{name} is too large")
        dest.write_bytes(data)
        return data

    try:
        fetch("manifest.json", man, 256 * 1024)
        fetch("manifest.json.sig", sig, 16 * 1024)
    except Exception:
        return "offline"
    if not verify_manifest_signature(man, sig):
        print("[!] Self-update: the site's manifest is not signed by this Deck's update key; not installing it")
        return "bad-signature"
    try:
        manifest = json.loads(man.read_text())
        new = manifest["version"]
        want = manifest["files"]["install.sh"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return "bad-manifest"
    if new == installed_version():
        return "current"
    released, have = manifest.get("released"), installed_released()
    if have is not None and (not isinstance(released, int) or released <= have):
        print(f"[!] Self-update: the site offers {new}, which is not newer than the installed version; not installing it")
        return "not-newer"
    if new in state.get("bad_versions", []):
        return "known-bad"
    if not shutil.which("systemd-run"):
        # The installer must run outside this service: it restarts the daemon, which would kill it (and its health
        # check and rollback) if it ran in the daemon's own cgroup.
        return "no-systemd-run"
    busy = deck_idle_for_update(proc_root)
    if busy:
        state["last_check"] = now - SELF_UPDATE_CHECK_SECONDS + 300      # look again in 5 minutes
        _save_update_state(state)
        return "busy: " + busy
    inst = SELF_UPDATE_DIR / "install.sh"
    try:
        data = fetch("install.sh", inst, 1024 * 1024)
    except Exception:
        return "offline"
    if hashlib.sha256(data).hexdigest() != want:
        print("[!] Self-update: install.sh does not match the signed manifest; not running it")
        return "bad-installer"
    log = STATE_DIR / "logs" / "self_update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # No secrets on this command line (others can read it, systemd may log it): the installer reads the queue password
    # and RemoteStorage key from the Deck's own private config files.
    env_args = ["env", "DECKSTREAM_AUTO_UPDATE=1", f"DECKSTREAM_MANIFEST_FILE={man}"]
    # Its own unit, so the installer survives the daemon restart it triggers.
    cmd = ["systemd-run", "--user", "--collect", "--quiet", f"--unit=deckstream-selfupdate-{int(now)}",
           "-p", "StandardInput=null", "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}"] \
        + env_args + ["bash", str(inst), SERVER_URL, DEVICE_ID]
    try:
        spawn(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        print(f"[!] Self-update could not start the installer: {e}")
        return "spawn-failed"
    print(f"[*] Self-update: installing {new} (was {installed_version() or '?'}); the daemon restarts when it is done")
    return "started " + new


DECKY_PLUGIN_DIR = HOME / "homebrew" / "plugins" / "DeckStream"
_last_decky_check = 0.0


def update_decky_plugin_if_needed(now=None, opener=None, force=False, home=None):
    """If the Decky Loader plugin is installed, keep its files synchronized with the server's DeckStream.zip.
    Runs every 30 minutes or on force. Returns True if any file was updated, False otherwise."""
    global _last_decky_check
    now = time.time() if now is None else now
    if not force and now - _last_decky_check < SELF_UPDATE_CHECK_SECONDS:
        return False
    _last_decky_check = now
    plugin_dir = (Path(home) / "homebrew" / "plugins" / "DeckStream") if home else DECKY_PLUGIN_DIR
    if not plugin_dir.is_dir() or not SERVER_URL:
        return False
    main_py = plugin_dir / "main.py"
    dist_js = plugin_dir / "dist" / "index.js"
    if not (main_py.exists() and os.access(main_py, os.W_OK) and dist_js.exists() and os.access(dist_js, os.W_OK)):
        return False
    opener = opener or urllib.request.urlopen
    url = f"{SERVER_URL.rstrip('/')}/decky/DeckStream.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DeckStream/1.0"})
        with opener(req, timeout=15) as r:
            data = r.read(10 * 1024 * 1024)
        if len(data) < 100:
            return False
        import io, zipfile
        updated = False
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            targets = [
                ("DeckStream/main.py", main_py),
                ("DeckStream/dist/index.js", dist_js),
                ("DeckStream/package.json", plugin_dir / "package.json"),
                ("DeckStream/plugin.json", plugin_dir / "plugin.json"),
            ]
            for arcname, dest in targets:
                if arcname in z.namelist() and (not dest.exists() or os.access(dest, os.W_OK)):
                    content = z.read(arcname)
                    if not dest.exists() or dest.read_bytes() != content:
                        tmp = dest.with_suffix(".tmp")
                        tmp.write_bytes(content)
                        os.replace(tmp, dest)
                        updated = True
        if updated:
            print("[*] Decky plugin auto-update: synchronized to latest server build")
        return updated
    except Exception as e:
        return False


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


_GAMES_CACHE = {"at": 0.0, "games": []}


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
    if not SERVER_URL or not script.exists():
        return 0
    mod = _launch_report_mod.get(str(script))
    if mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("launch_report", str(script))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _launch_report_mod[str(script)] = mod
    n = mod.send(SERVER_URL, QUEUE_SECRET, DEVICE_ID)
    if n:
        print(f"[*] Sent {n} launch timing report(s) to your DeckStream site")
    return n


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


def apply_pending_shortcuts():
    pending = _load_pending()
    if not pending:
        return
    if steam_running():
        # With live shortcuts on, games waiting for Steam to close can be added right away.
        if not live_enabled():
            return
        left = [p for p in pending if not live_add_shortcut(p["name"], p["exe"], p.get("artwork"))]
        if len(left) != len(pending):
            if left:
                PENDING_FILE.write_text(json.dumps(left))
            else:
                PENDING_FILE.unlink(missing_ok=True)
        return
    for p in pending:
        try:
            add_non_steam_shortcut(p["name"], p["exe"], p.get("artwork"))
        except Exception as e:
            print(f"[!] Could not add pending shortcut '{p.get('name')}': {e}")
    PENDING_FILE.unlink(missing_ok=True)
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "DeckStream", "New games were added to your Steam library. Start Steam to see them."], timeout=10)
        except Exception:
            pass


SHORTCUT_TAG = "DeckStream"
LAUNCHER_DIR = HOME / ".local" / "share" / "deckstream" / "launchers"


def _shortcut_tags(entry):
    tags = entry.get("tags")
    return list(tags.values()) if isinstance(tags, dict) else []


def _unquote(value):
    value = str(value or "")
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def _is_ours(entry, game_name, exe_path):
    """A shortcut is ours if it carries our tag. Entries added by older versions have no tag; adopt one
    only when it is clearly ours: same name, and it points at this exact target or at a launcher script
    we generated. Anything else (for example a shortcut the user made by hand) is left alone."""
    name = entry.get("AppName")
    exe = _unquote(entry.get("Exe"))
    if SHORTCUT_TAG in _shortcut_tags(entry):
        # tagged entries are ours, but each tagged entry is only the one game it was made for
        return name in (game_name, f"{game_name} ({SHORTCUT_TAG})") or exe == str(exe_path)
    if name != game_name:
        return False
    return exe == str(exe_path) or exe.startswith(str(LAUNCHER_DIR) + os.sep)


def upsert_shortcut(shortcuts, game_name, exe_path):
    """Adds or updates our shortcut in the list. Returns (app_id, created).
    - only entries that are ours are updated; a same-named shortcut of the user's is never touched
      (ours is then added as "<name> (DeckStream)")
    - an updated entry keeps everything the user may have changed: launch options, play time, icon,
      hidden flag, other tags, and its AppID (so existing artwork keeps matching)."""
    exe_path = str(exe_path)
    existing = next((s for s in shortcuts if _is_ours(s, game_name, exe_path)), None)
    if existing is not None:
        existing["Exe"] = f'"{exe_path}"'
        existing["StartDir"] = f'"{Path(exe_path).parent}"'
        for key, default in (("IsHidden", 0), ("AllowDesktopConfig", 1), ("AllowOverlay", 1), ("OpenVR", 0),
                             ("Devkit", 0), ("DevkitGameID", ""), ("DevkitOverrideAppId", 0),
                             ("LastPlayTime", 0), ("LaunchOptions", ""), ("icon", ""), ("ShortcutPath", "")):
            existing.setdefault(key, default)
        tags = existing["tags"] if isinstance(existing.get("tags"), dict) else {}
        if SHORTCUT_TAG not in tags.values():
            tags[str(len(tags))] = SHORTCUT_TAG
        existing["tags"] = tags
        if not isinstance(existing.get("appid"), int):
            existing["appid"] = calculate_shortcut_id(exe_path, existing.get("AppName", game_name))
        return existing["appid"], False

    name = game_name
    if any(s.get("AppName") == name for s in shortcuts):
        name = f"{game_name} ({SHORTCUT_TAG})"
    app_id = calculate_shortcut_id(exe_path, name)
    shortcuts.append({
        "appid": app_id,
        "AppName": name,
        "Exe": f'"{exe_path}"',
        "StartDir": f'"{Path(exe_path).parent}"',
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": "",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppId": 0,
        "LastPlayTime": 0,
        "tags": {"0": SHORTCUT_TAG},
    })
    return app_id, True


def write_file_atomic(path, data, backup_suffix=".deckstream-bak"):
    """Replace `path` with `data` without ever leaving a half-written file: keep a copy of the old file,
    write a temp file next to it, flush it to disk, then rename it over the original."""
    path = Path(path)
    if path.exists():
        shutil.copy2(path, str(path) + backup_suffix)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def add_non_steam_shortcut(game_name, exe_path, artwork_urls=None):
    # Windows executables go through a Proton launcher; scripts (stream mode) are used as they are.
    if str(exe_path).lower().endswith(".exe"):
        exe_path = make_game_launcher(game_name, exe_path)
    userdata = find_steam_userdata_dir()
    if not userdata:
        print("[!] Could not locate Steam userdata directory.")
        return None

    config_dir = userdata / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shortcuts_file = config_dir / "shortcuts.vdf"
    grid_dir = config_dir / "grid"
    grid_dir.mkdir(parents=True, exist_ok=True)

    shortcuts = []
    if shortcuts_file.exists():
        try:
            shortcuts = parse_binary_vdf(shortcuts_file.read_bytes(), strict=True)
        except Exception as e:
            # Never rewrite a file we could not read completely: that would erase the user's shortcuts.
            print(f"[!] Could not read {shortcuts_file} ({e}); leaving it untouched.")
            return None

    app_id, created = upsert_shortcut(shortcuts, game_name, exe_path)
    write_file_atomic(shortcuts_file, serialize_binary_vdf(shortcuts))
    print(f"[✓] {'Added' if created else 'Updated'} '{game_name}' in Steam shortcuts with AppID {app_id}")

    # Download SteamGridDB artwork if provided
    if artwork_urls and isinstance(artwork_urls, dict):
        download_artwork(grid_dir, app_id, artwork_urls)

    return app_id


def download_artwork(grid_dir, app_id, artwork_urls):
    """
    Downloads custom artwork into Steam's grid directory:
    - Vertical capsule: <appid>p.png/jpg
    - Header/banner: <appid>.jpg
    - Hero: <appid>_hero.jpg
    - Logo: <appid>_logo.png
    """
    mapping = {
        "grid": f"{app_id}p.jpg",
        "header": f"{app_id}.jpg",
        "hero": f"{app_id}_hero.jpg",
        "logo": f"{app_id}_logo.png",
        "icon": f"{app_id}_icon.png"
    }

    for art_type, target_filename in mapping.items():
        url = artwork_urls.get(art_type)
        if url and isinstance(url, str) and url.startswith("https://"):
            try:
                dest = grid_dir / target_filename
                with open_checked(url, ARTWORK_HOSTS, 15) as resp, open(dest, "wb") as out:
                    shutil.copyfileobj(resp, out, 1024 * 1024)
                print(f"[✓] Saved {art_type} artwork to {target_filename}")
            except Exception as e:
                print(f"[!] Failed to fetch {art_type} artwork: {e}")


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
            return open_checked(u, ALLOWED_DOWNLOAD_HOSTS, timeout, headers)
        parallel_download.download(url, dest_path, opener, progress=progress_callback,
                                   workers=8, segment=32 * 1024 * 1024, stall_timeout=300)
        return
    with open_checked(url, ALLOWED_DOWNLOAD_HOSTS, 30) as resp:
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


# A folder counts as an installed game only if it holds something that runs: a Windows game, a Switch ROM, a Linux launcher
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
    2. If a Switch ROM exists: delivers all ROM files to EmuDeck's roms/switch directory and,
       if an emulator binary (e.g. Eden) is present, creates an executable shell runner in
       ~/.local/share/deckstream/runners/ and registers the Steam shortcut with custom artwork.
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
    """The installer runs at the lowest CPU and disk priority, so the Deck stays usable while a repack unpacks for hours."""
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
    env = {**os.environ, "DECKSTREAM_REPACK_ALL": "1"} if keep_all else None
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
    # Long silent stretches (a repack installer can run for hours) must not look like a dead Deck.
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


def run_daemon():
    cfg = load_config()
    server_url = cfg["server_url"].rstrip("/")
    device_id = cfg["device_id"]
    poll_sec = max(3, int(cfg.get("poll_interval_sec", 10)))
    global QUEUE_SECRET, SERVER_URL, DEVICE_ID
    QUEUE_SECRET = cfg.get("queue_secret", "")
    SERVER_URL = server_url
    DEVICE_ID = device_id
    global ALLOWED_DOWNLOAD_HOSTS
    ALLOWED_DOWNLOAD_HOSTS = DEFAULT_DOWNLOAD_HOSTS + [
        str(h).lower() for h in cfg.get("allowed_download_hosts", []) if h
    ]
    parsed_server = urllib.parse.urlparse(server_url)
    local_dev = parsed_server.hostname in ("localhost", "127.0.0.1", "::1")
    if not server_url or parsed_server.scheme not in ("https", "http") or (parsed_server.scheme == "http" and not local_dev):
        print(f"[!] server_url must be your own https DeckStream URL (got '{server_url}').")
        print(f"    Fix it in {CONFIG_FILE} (the queue password is only ever sent over https).")
        sys.exit(1)
    if not QUEUE_SECRET:
        print("[!] No queue_secret in config - the server will reject this Deck.")
        print(f"    Add \"queue_secret\" to {CONFIG_FILE} (same value as DECKSTREAM_SECRET).")

    print("==========================================")
    print("   🎮 DeckStream Daemon - Steam Deck    ")
    print("==========================================")
    print(f"Server:    {server_url}")
    print(f"Device ID: {device_id}")
    print(f"Home Dir:  {HOME}")
    GAMES_DIR_INTERNAL.mkdir(parents=True, exist_ok=True)
    print(f"Games Dir: {GAMES_DIR_INTERNAL}")
    print("------------------------------------------")

    last_heartbeat = 0.0
    clear_current_job()
    start_janitor()                                   # stream sessions are finished even while a long job runs
    idle_polls = 0                                    # consecutive polls with no job: drives idle_poll_interval()
    while True:
        try:
            clear_current_job()                       # the previous job (if any) has finished
            if consume_restart_request():
                print("[*] An update was installed while a job was running; restarting now to use it.")
                sys.exit(0)                           # systemd (Restart=always) starts the new version
            apply_pending_shortcuts()
            # 1. Report heartbeat & disk space (every 30s; polling also keeps the Deck marked online)
            ssd_stat = get_disk_usage(GAMES_DIR_INTERNAL)
            sd_path = get_micro_sd_path()
            sd_stat = get_disk_usage(sd_path) if sd_path else {"free": 0, "total": 0}

            if time.time() - last_heartbeat >= 30:
                cap_proton_logs()
                installed = list_installed_games(GAMES_DIR_INTERNAL)
                heartbeat_payload = {
                    "action": "heartbeat",
                    "diskSSD": ssd_stat,
                    "diskSD": sd_stat,
                    "installedGames": installed,
                    **deck_status(),
                }
                req = queue_request(
                    f"{server_url}/api/queue?device={device_id}",
                    data=json.dumps(heartbeat_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as hb:
                    try:   # the reply carries the phone's warm-up and Deck mode choices; an older site sends none
                        reply = json.loads(hb.read().decode("utf-8") or "{}")
                        if apply_warmup_wanted(reply.get("warmup_wanted")) | bool(apply_deck_mode_wanted(reply.get("deck_mode_wanted"))):
                            last_heartbeat = 0.0   # report the new state on the next pass, not in 30 s
                        else:
                            last_heartbeat = time.time()
                    except (ValueError, AttributeError, OSError):
                        last_heartbeat = time.time()

            # 2. Poll for pending job
            poll_req = queue_request(f"{server_url}/api/queue?action=poll&device={device_id}")
            with urllib.request.urlopen(poll_req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                job = data.get("job")
            idle_polls = 0 if job else idle_polls + 1

            if job and job.get("status") == "pending" and job.get("mode") == "remove":
                # Free a streamed game's disk (cache, or the whole game) on request from the app; nothing is downloaded.
                job_id = job["id"]
                ok, freed, msg = remove_game_data(str(job.get("slug") or ""), str(job.get("scope") or ""), target_path=job.get("path"), game_name=job.get("name"))
                print(f"[{'+' if ok else '!'}] Remove '{job.get('slug')}' ({job.get('scope')}): {msg}")
                _GAMES_CACHE["at"] = 0.0
                _GAMES_CACHE["games"] = []
                report_job(server_url, device_id, job_id, "completed" if ok else "failed", 100 if ok else 0, msg, freedBytes=freed, games=streamed_games(), installedGames=list_installed_games(GAMES_DIR_INTERNAL))
                time.sleep(2)
                continue

            if job and job.get("status") == "pending" and job.get("mode") in ("ai_heal", "ai_clean"):
                # switched off: the 17 Oct versions deleted Wine prefixes (saves) and registry files; the queue refuses them too
                report_job(server_url, device_id, job["id"], "failed", 0, "AI clean/heal is switched off (it could delete saves)")
                time.sleep(2)
                continue

            if job and job.get("status") == "pending":
                job_id = job["id"]
                game_name = safe_dir_name(job.get("name"))
                dl_url = job.get("downloadUrl", "")
                storage_target = job.get("targetDir", "internal")
                dest_root = sd_path if (storage_target == "sd" and sd_path) else GAMES_DIR_INTERNAL

                print(f"\n[+] Processing Job {job_id}: '{game_name}'")
                set_current_job(job_id, game_name)

                # Update progress: downloading
                def report_progress(pct, cur, total):
                    update = {
                        "action": "update_progress",
                        "jobId": job_id,
                        "status": "downloading",
                        "progress": pct,
                        "message": f"Downloading: {round(cur / 1048576)}MB / {round(total / 1048576)}MB"
                    }
                    try:
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps(update).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        with urllib.request.urlopen(r, timeout=8) as resp:
                            res_json = json.loads(resp.read().decode("utf-8"))
                            if res_json.get("cancelled") or res_json.get("cancelledJobId") == job_id:
                                raise InterruptedError("Cancelled by user")
                    except InterruptedError:
                        raise
                    except Exception:
                        pass

                job_mode = job.get("mode", "download") # "download" or "stream"

                # If cloud stream mode, configure stream launcher shortcut (0 GB local SSD)
                if job_mode == "stream":
                    print(f"[*] Cloud Stream Mode requested for '{game_name}'. Configuring 0-GB Stream Runner...")
                    stream_runner_path = Path.home() / ".config" / "deckstream" / "stream-runner.sh"
                    
                    # Create desktop launch script
                    desktop_launcher = Path.home() / ".local" / "share" / "deckstream" / "launchers"
                    desktop_launcher.mkdir(parents=True, exist_ok=True)
                    clean_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', game_name).lower()
                    launcher_file = desktop_launcher / f"stream_{clean_slug}.sh"
                    
                    webdav_folder = (job.get("webdavPath") or "").strip().strip("/")
                    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
                    plan_kind = str(plan.get("kind") or "").strip().lower()
                    if not plan or not plan_kind:
                        err_msg = "Stream job has no launch plan. Only verified ready-to-play folders can be streamed."
                        print(f"[!] {err_msg}")
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        try:
                            urllib.request.urlopen(r, timeout=10)
                        except Exception:
                            pass
                        time.sleep(poll_sec)
                        continue

                    if plan_kind in ("archive", "repack", "installer", "switch", "disc-image", "not-a-game", "suspicious"):
                        err_msg = f"Cannot stream release of type '{plan_kind}'. It must be extracted or installed first."
                        print(f"[!] {err_msg}")
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        try:
                            urllib.request.urlopen(r, timeout=10)
                        except Exception:
                            pass
                        time.sleep(poll_sec)
                        continue

                    if not webdav_folder:
                        err_msg = "Stream job has no RemoteStorage folder name. Re-send it from the updated web app."
                        print(f"[!] {err_msg}")
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        urllib.request.urlopen(r, timeout=10)
                        time.sleep(poll_sec)
                        continue

                    def sh_quote(s):
                        return "'" + str(s).replace("'", "'\"'\"'") + "'"

                    # Credentials are NOT embedded here; stream-runner reads ~/.config/deckstream/storage.conf
                    with open(launcher_file, "w") as lf:
                        lf.write("#!/usr/bin/env bash\n")
                        plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
                        plan_exe = str(plan.get("exe") or "").strip().strip("/")
                        if any(part in ("", ".", "..") for part in plan_exe.split("/")) or len(plan_exe) > 300:
                            plan_exe = ""   # only a clean path inside the mount is passed on
                        extra = f" {sh_quote(plan_exe)}" if plan_exe else ""
                        lf.write(f"exec bash {sh_quote(stream_runner_path)} {sh_quote(clean_slug)} {sh_quote(game_name)} {sh_quote(webdav_folder)}{extra}\n")
                    os.chmod(launcher_file, 0o755)

                    # Add non-steam shortcut pointing to the stream launcher
                    applied, app_id = request_shortcut(f"{game_name} (Cloud Stream)", str(launcher_file), job.get("artwork"))
                    if shutil.which("notify-send"):
                        try:
                            subprocess.run(["notify-send", f"⚡ {game_name} Stream Ready", "Added to Steam. Warming cache..."], timeout=10)
                        except Exception:
                            pass
                    # Start warming the cache now, while the user walks to the Deck (off until enabled; never fatal)
                    try:
                        # A game learned on an earlier card or install starts warm again: get its profile back first.
                        restore_profile(clean_slug, webdav_folder)
                        start_prestage(clean_slug, webdav_folder, (plan or {}).get("hot"))
                    except Exception as e:
                        print(f"[!] Prestage not started: {e}")

                    # Report completed
                    done_update = {
                        "action": "update_progress",
                        "jobId": job_id,
                        "status": "completed",
                        "progress": 100,
                        "message": ("⚡ Stream Ready! Added to your Steam library with 0 GB SSD usage." if applied
                                    else "⚡ Stream ready. Close Steam (Desktop Mode: Steam > Exit) to add it to your library."),
                        "installedGames": list_installed_games(GAMES_DIR_INTERNAL)
                    }
                    r = queue_request(
                        f"{server_url}/api/queue?device={device_id}",
                        data=json.dumps(done_update).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(r, timeout=10)
                    time.sleep(poll_sec)
                    continue

                # Cloud install: unpack a repack/zip straight from the RemoteStorage mount onto the SD card
                if job_mode == "cloudinstall":
                    webdav_folder = (job.get("webdavPath") or "").strip().strip("/")
                    script = Path.home() / ".config" / "deckstream" / "cloud-install.sh"
                    if not webdav_folder:
                        fail_job(server_url, device_id, job_id, "Cloud install job has no RemoteStorage folder name.")
                        time.sleep(poll_sec)
                        continue
                    if not script.exists():
                        fail_job(server_url, device_id, job_id, "cloud-install.sh missing on the Deck. Re-run the DeckStream installer.")
                        time.sleep(poll_sec)
                        continue
                    if storage_target == "sd" and not sd_path and job.get("keepAll") is not True:
                        fail_job(server_url, device_id, job_id, "No MicroSD card found. Insert one, or choose internal storage.")
                        time.sleep(poll_sec)
                        continue
                    dest_game_folder = (dest_root / game_name).resolve()
                    keep_all = job.get("keepAll") is True
                    if keep_all:
                        # "Get skipped files": the folder of a rip install that left files out, as the app listed it
                        hit = [g for g in list_installed_games() if g["path"] == str(job.get("path") or "") and g.get("skipped")]
                        if not hit or hit[0]["skipped"]["folder"] != webdav_folder:
                            fail_job(server_url, device_id, job_id, "Nothing was skipped in that game, or it is not installed here.")
                            time.sleep(poll_sec)
                            continue
                        dest_game_folder = Path(hit[0]["path"])
                        dest_root = dest_game_folder.parent
                    if dest_root.resolve() not in dest_game_folder.parents:
                        fail_job(server_url, device_id, job_id, "Refusing to write outside the games folder")
                        time.sleep(poll_sec)
                        continue
                    free = (sd_stat if (storage_target == "sd" and sd_path) else ssd_stat)["free"]
                    if job.get("size", 0) and free < job["size"]:
                        fail_job(server_url, device_id, job_id,
                                 f"Not enough free space: {round(free / 1073741824, 1)}GB free, release is {round(job['size'] / 1073741824, 1)}GB.")
                        time.sleep(poll_sec)
                        continue

                    report_job(server_url, device_id, job_id, "installing", 5, "Reading release from RemoteStorage...")
                    ok, msg = run_cloud_install(
                        script, re.sub(r"[^a-zA-Z0-9_-]", "_", game_name).lower(), webdav_folder, dest_game_folder,
                        lambda last: report_cloud_progress(server_url, device_id, job_id, dest_game_folder, job.get("size", 0), last),
                        size_fn=lambda: folder_size(dest_game_folder),
                        awake_why=f"DeckStream installing {game_name}", paused_fn=ChargerGate().should_pause, keep_all=keep_all,
                    )
                    _cloud_rate.pop(job_id, None)
                    if not ok:
                        report_job(server_url, device_id, job_id, "failed", 0, f"Install failed: {msg}")
                        time.sleep(poll_sec)
                        continue
                    if keep_all:      # the game is already in Steam: only the left-out files were added
                        report_job(server_url, device_id, job_id, "completed", 100, "Got the skipped files",
                                   installedGames=list_installed_games())
                        time.sleep(poll_sec)
                        continue

                    deliver_rom_or_executable(game_name, dest_game_folder, job, server_url, device_id, job_id)
                    time.sleep(poll_sec)
                    continue

                # Check disk space for regular downloads
                required_space = job.get("size", 0) * 1.5  # archive + extracted
                avail_space = ssd_stat["free"] if storage_target != "sd" else sd_stat["free"]
                if required_space > 0 and avail_space < required_space:
                    err_msg = f"Insufficient disk space. Needed ~{round(required_space / 1073741824, 1)}GB, Free {round(avail_space / 1073741824, 1)}GB"
                    print(f"[!] {err_msg}")
                    r = queue_request(
                        f"{server_url}/api/queue?device={device_id}",
                        data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(r, timeout=10)
                    time.sleep(poll_sec)
                    continue

                # Download
                dest_game_folder = (dest_root / game_name).resolve()
                if dest_root.resolve() not in dest_game_folder.parents:
                    fail_job(server_url, device_id, job_id, "Refusing to write outside the games folder")
                    time.sleep(poll_sec)
                    continue
                folder_existed = dest_game_folder.exists()
                dest_game_folder.mkdir(parents=True, exist_ok=True)
                temp_file = dest_game_folder / "download.pkg"

                try:
                    download_file(dl_url, temp_file, report_progress)
                except DOWNLOAD_FAILURES as ex:
                    # Rejected URL, cancellation, or failed transfer: clean up immediately
                    fail_msg = "Download cancelled by user" if isinstance(ex, InterruptedError) else f"Download failed: {ex}"
                    print(f"[!] {fail_msg}")
                    fail_job(server_url, device_id, job_id, fail_msg)
                    discard_failed_download(dest_game_folder, temp_file, folder_existed)
                    clear_current_job()
                    time.sleep(poll_sec)
                    continue

                if not temp_file.exists() or temp_file.stat().st_size == 0:
                    err_msg = "Download was empty (0 bytes received from CDN). Check RemoteStorage link."
                    print(f"[!] {err_msg}")
                    fail_job(server_url, device_id, job_id, err_msg)
                    discard_failed_download(dest_game_folder, temp_file, folder_existed)
                    time.sleep(poll_sec)
                    continue

                # Extract if archive
                extract_update = {
                    "action": "update_progress",
                    "jobId": job_id,
                    "status": "extracting",
                    "progress": 95,
                    "message": "Extracting game files..."
                }
                try:
                    r = queue_request(
                        f"{server_url}/api/queue?device={device_id}",
                        data=json.dumps(extract_update).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(r, timeout=8)
                except Exception:
                    pass

                if looks_like_archive(temp_file):
                    try:
                        extract_archive(temp_file, dest_game_folder)
                    except Exception as ex:
                        # A truncated or corrupt archive used to be treated as "a single file": the job went on, found an
                        # .exe among the half-extracted files and reported the game as installed. Fail instead.
                        print(f"[!] Extraction failed: {ex}")
                        fail_job(server_url, device_id, job_id, f"Extraction failed (the download may be damaged): {ex}")
                        discard_failed_download(dest_game_folder, temp_file, folder_existed)
                        time.sleep(poll_sec)
                        continue
                else:
                    print("[*] Not an archive: keeping the downloaded file as it is")
                    pass

                deliver_rom_or_executable(game_name, dest_game_folder, job, server_url, device_id, job_id)
                time.sleep(poll_sec)
                continue

        except urllib.error.URLError as e:
            # Server temporarily unreachable
            pass
        except Exception as e:
            print(f"[!] Daemon loop error: {e}")

        slept_for = idle_poll_interval(poll_sec, idle_polls)
        t_before_sleep = time.time()
        time.sleep(slept_for)
        # OFF unless "suspend_hook": true in config.json (no Deck run yet): recover cloud mounts that hung across a suspend
        if cfg.get("suspend_hook") and resumed_from_sleep(t_before_sleep, time.time(), slept_for):
            print("[*] Resumed from sleep: checking the cloud mounts.")
            run_resume_hook()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--live-shortcuts" and sys.argv[2] in ("on", "off", "status"):
        if sys.argv[2] != "status":
            set_live_shortcuts(sys.argv[2] == "on")
        on, flag = live_enabled(), cef_flag_file().exists()
        try:
            reachable = bool(_cef_target())
        except Exception:
            reachable = False
        print(f"Live shortcuts: {'ON' if on else 'off'}; Steam debug flag file: {'present' if flag else 'absent'}; "
              f"Steam API reachable now: {'yes' if reachable else 'no'}")
        if on and not reachable:
            print("Restart Steam once (or reboot) so it opens its debugging interface; new games then appear without a restart.")
        if on:
            print("Note: while this is on, any program running as this user can control Steam through 127.0.0.1:8080.")
        sys.exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == "--add-shortcut":
        # Repair/re-create the Steam shortcut for an already-installed game:
        #   deckstream-daemon.py --add-shortcut "Game Name" "/path/to/Game.exe"
        # Close Steam first (it rewrites shortcuts.vdf when it exits).
        sys.exit(0 if add_non_steam_shortcut(sys.argv[2], sys.argv[3]) else 1)
    run_daemon()
