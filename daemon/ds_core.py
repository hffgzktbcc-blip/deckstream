"""Settings, paths, the download-host checks, job state, the config file and the Steam/SD card locations.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import os
import time
import json
import shutil
import re
import urllib.request
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
