#!/usr/bin/env python3
"""DeckStream doctor: checks that this Deck is set up to work, and says what to do when it is not.

  doctor.py            full report
  doctor.py --brief    only problems (used by the installer)
  doctor.py --json     machine-readable
  doctor.py --bundle   write a redacted report (+ recent logs) to a file you can share

Exit code: 0 = nothing failed, 1 = at least one check failed. Warnings do not fail it.
Secrets (the queue password and the RemoteStorage key) are never printed or written to a bundle.
"""
import base64
import concurrent.futures
import hashlib
import json
import errno
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
CONF = HOME / ".config" / "deckstream"
DATA = HOME / ".local" / "share" / "deckstream"
UA = "DeckStream-doctor/1.0"

NEEDED_HOSTS = []   # plus the DeckStream site and the storage server from the config files
OPTIONAL_HOSTS = {
    "github.com": "rclone / umu / Ludusavi downloads (your DeckStream site can mirror them under /daemon/tools)",
    "downloads.rclone.org": "rclone downloads (mirrored)",
    "repo.steampowered.com": "umu's game runtime (NOT mirrored; only needed if you use umu)",
}
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", INFO: "ℹ️ "}

DNS_FIX = ("Set a public DNS for your Wi-Fi connection:\n"
           "     nmcli -t -f NAME,TYPE connection show --active      (note the Wi-Fi name)\n"
           "     nmcli connection modify \"WIFI_NAME\" ipv4.dns \"1.1.1.1 9.9.9.9\" ipv4.ignore-auto-dns yes\n"
           "     nmcli connection up \"WIFI_NAME\"")


# ------------------------------------------------------------------ real-world helpers (patched in tests)
def resolve(host, timeout=4):
    try:
        socket.setdefaulttimeout(timeout)
        return bool(socket.getaddrinfo(host, 443))
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(None)


def http(url, headers=None, timeout=8, method="GET"):
    """Returns (status, headers_dict, body_bytes). HTTP error statuses are returned, not raised."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(65536)


def run_cmd(argv, timeout=8):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def which(name):
    return shutil.which(name) or (str(HOME / ".local" / "bin" / name) if (HOME / ".local" / "bin" / name).exists() else None)


def read_mounts():
    try:
        return Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return []


def disk_free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def now():
    return time.time()


# ------------------------------------------------------------------ result handling
class R:
    def __init__(self, status, title, detail="", fix=""):
        self.status, self.title, self.detail, self.fix = status, title, detail, fix

    def as_dict(self):
        return {"status": self.status, "title": self.title, "detail": self.detail, "fix": self.fix}


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def storage_setting(name):
    try:
        m = re.search(name + r"=(?:'([^']*)'|\"([^\"]*)\"|(\S+))", (CONF / "storage.conf").read_text())
        return next((g for g in m.groups() if g), "") if m else ""
    except OSError:
        return ""


def storage_key():
    return storage_setting("STORAGE_KEY")


def version_tuple(text):
    m = re.search(r"v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


# ------------------------------------------------------------------ the checks
def check_python():
    v = sys.version_info
    return [R(OK if v >= (3, 9) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}",
              "" if v >= (3, 9) else "DeckStream needs Python 3.9 or newer")]


def check_config(cfg):
    out = []
    if not cfg:
        return [R(FAIL, "DeckStream config", f"{CONF / 'daemon_config.json'} is missing or unreadable",
                  "Run the DeckStream installer again.")]
    url = cfg.get("server_url", "")
    ok_url = url.startswith("https://") or url.startswith("http://127.0.0.1") or url.startswith("http://localhost")
    out.append(R(OK if ok_url else FAIL, "Server address", url or "(empty)",
                 "" if ok_url else "server_url must be your own https DeckStream address (re-run the installer)."))
    out.append(R(OK if cfg.get("queue_secret") else FAIL, "Queue password saved",
                 "yes" if cfg.get("queue_secret") else "no",
                 "" if cfg.get("queue_secret") else "Re-run the installer and enter the queue password."))
    return out


def check_dns(cfg):
    hosts = dict.fromkeys(NEEDED_HOSTS)
    server = urllib.parse.urlparse(cfg.get("server_url", "")).hostname if cfg else None
    if server:
        hosts[server] = None
    storage = urllib.parse.urlparse(storage_setting("STORAGE_WEBDAV_URL")).hostname
    if storage:
        hosts[storage] = None
    names = list(hosts) + list(OPTIONAL_HOSTS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        results = dict(zip(names, ex.map(resolve, names)))
    out = []
    dns = ", ".join(re.findall(r"^nameserver\s+(\S+)", Path("/etc/resolv.conf").read_text(), re.M)) \
        if Path("/etc/resolv.conf").exists() else "unknown"
    for h in hosts:
        out.append(R(OK if results[h] else FAIL, f"DNS: {h}", "resolves" if results[h] else f"cannot be looked up (DNS servers: {dns})",
                     "" if results[h] else DNS_FIX))
    for h, why in OPTIONAL_HOSTS.items():
        out.append(R(OK if results[h] else WARN, f"DNS: {h}", "resolves" if results[h] else f"cannot be looked up; affects {why} (DNS servers: {dns})",
                     "" if results[h] else DNS_FIX))
    return out


def check_server(cfg):
    if not cfg or not cfg.get("server_url"):
        return []
    base, secret = cfg["server_url"].rstrip("/"), cfg.get("queue_secret", "")
    out = []
    try:
        status, headers, _ = http(f"{base}/api/queue?action=status&device={cfg.get('device_id', 'deck-main')}",
                                  {"x-deckstream-secret": secret})
    except (OSError, urllib.error.URLError) as e:
        return [R(FAIL, "DeckStream server", f"cannot reach {base}: {e}", "Check the Wi-Fi connection and the server address.")]
    if status == 200:
        out.append(R(OK, "DeckStream server", "reachable; queue password accepted"))
    elif status == 401:
        out.append(R(FAIL, "Queue password", "the server rejected it", "Re-run the installer and enter the current DECKSTREAM_SECRET."))
    elif status == 503:
        out.append(R(WARN, "DeckStream server", "reachable, but DECKSTREAM_SECRET is not set on the server", "Set it in Vercel and redeploy."))
    else:
        out.append(R(WARN, "DeckStream server", f"unexpected HTTP {status}"))
    date = headers.get("Date") or headers.get("date")
    if date:
        try:
            from email.utils import parsedate_to_datetime
            skew = abs(parsedate_to_datetime(date).timestamp() - now())
            if skew > 300:
                out.append(R(FAIL, "Clock", f"off by {int(skew)} s", "Fix the date/time (Settings > System); secure connections fail when it is wrong."))
            elif skew > 60:
                out.append(R(WARN, "Clock", f"off by {int(skew)} s"))
            else:
                out.append(R(OK, "Clock", f"within {int(skew)} s of the server"))
        except (TypeError, ValueError):
            pass
    return out


def check_storage():
    key, url = storage_key(), storage_setting("STORAGE_WEBDAV_URL")
    user = storage_setting("STORAGE_WEBDAV_USER") or "deckstream"
    if not key or not url:
        return [R(WARN, "Storage server", "not set on this Deck",
                  f"Put STORAGE_KEY, STORAGE_WEBDAV_URL and STORAGE_WEBDAV_USER in {CONF / 'storage.conf'} (streaming and installs need them).")]
    auth = base64.b64encode(f"{user}:{key}".encode()).decode()
    try:
        status, _, _ = http(url.rstrip("/") + "/", {"Authorization": f"Basic {auth}", "Depth": "0"}, method="PROPFIND")
    except (OSError, urllib.error.URLError, ValueError) as e:
        return [R(WARN, "Storage server", f"set, but {url} could not be reached ({e})")]
    if status in (200, 207):
        return [R(OK, "Storage server", f"{url} accepts this Deck's key")]
    if status in (401, 403):
        return [R(FAIL, "Storage server", f"{url} rejected the key", "Put the current key in storage.conf.")]
    return [R(WARN, "Storage server", f"{url} answered HTTP {status}")]


def check_rclone():
    exe = which("rclone")
    if not exe:
        return [R(WARN, "rclone", "not installed yet", "The first stream or cloud install installs it automatically.")]
    rc, out = run_cmd([exe, "version"])
    ver = version_tuple(out.splitlines()[0] if out else "")
    res = []
    if ver is None:
        return [R(WARN, "rclone", "installed but its version could not be read")]
    good = ver >= (1, 67)
    res.append(R(OK if good else WARN, "rclone", ".".join(map(str, ver)) + f" at {exe}",
                 "" if good else "Older than 1.67: parallel chunk reads are unavailable. Delete it and let DeckStream reinstall the pinned version."))
    return res


def fuse_device_ready():
    dev = Path("/dev/fuse")
    return dev.exists() and os.access(dev, os.R_OK | os.W_OK)


def check_fuse():
    out = []
    ready = fuse_device_ready()
    out.append(R(OK if ready else FAIL, "FUSE device", "available" if ready else "/dev/fuse missing or not accessible",
                 "" if ready else "Cloud mounts cannot work without it."))
    have = [n for n in ("fusermount3", "fusermount") if shutil.which(n)]
    out.append(R(OK if have else FAIL, "fusermount", ", ".join(have) or "not found", "" if have else "Needed to mount and unmount the cloud folder."))
    out.append(R(OK if shutil.which("fuse-overlayfs") else WARN, "fuse-overlayfs",
                 "present" if shutil.which("fuse-overlayfs") else "not found",
                 "" if shutil.which("fuse-overlayfs") else "Streaming still runs, but without the local write layer."))
    return out


def check_proton():
    roots = [HOME / ".steam/root/compatibilitytools.d", HOME / ".local/share/Steam/compatibilitytools.d"]
    found = sorted({str(p) for r in roots if r.is_dir() for p in r.glob("*")
                    if (p / "proton").is_file() and (p / "toolmanifest.vdf").is_file()})
    stock = [p for p in (HOME / ".local/share/Steam/steamapps/common").glob("Proton*") if (p / "proton").is_file()] \
        if (HOME / ".local/share/Steam/steamapps/common").is_dir() else []
    if found or stock:
        names = ", ".join(sorted(Path(x).name for x in found) + sorted(p.name for p in stock))
        return [R(OK, "Proton", names)]
    return [R(FAIL, "Proton", "no Proton version found", "Install GE-Proton with ProtonUp-Qt (Discover store) or Proton Experimental in Steam.")]


def check_disks():
    out = []
    free = disk_free_gb(HOME)
    if free is not None:
        st = FAIL if free < 1 else WARN if free < 3 else OK
        out.append(R(st, "Internal drive space", f"{free:.1f} GB free",
                     "" if st == OK else "Low. Steam and DeckStream need room here; free some space."))
    cards = []
    for line in read_mounts():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("/run/media/"):
            cards.append((parts[1].replace("\\040", " "), parts[2]))
    if not cards:
        out.append(R(INFO, "SD card", "none mounted"))
    for path, fs in cards:
        f = disk_free_gb(path)
        bad_fs = fs in ("exfat", "vfat", "ntfs", "fuseblk", "ntfs3")
        out.append(R(FAIL if bad_fs else OK, f"SD card {Path(path).name}", f"{fs}, {f:.0f} GB free" if f is not None else fs,
                     "Proton needs ext4 (symlinks fail on this format). Reformat in Desktop Mode." if bad_fs else ""))
    return out


def check_services():
    out = []
    rc, state = run_cmd(["systemctl", "--user", "is-active", "deckstream"])
    active = state.strip() == "active"
    out.append(R(OK if active else FAIL, "DeckStream daemon", state.strip() or "unknown",
                 "" if active else "Start it: systemctl --user restart deckstream   (logs: journalctl --user -u deckstream -n 50)"))
    rc, tstate = run_cmd(["systemctl", "--user", "is-active", "deckstream-saves.timer"])
    out.append(R(OK if tstate.strip() == "active" else WARN, "Daily save backup timer", tstate.strip() or "unknown",
                 "" if tstate.strip() == "active" else "Re-run the installer to set it up."))
    rc, log = run_cmd(["journalctl", "--user", "-u", "deckstream", "-n", "200", "--no-pager"], timeout=10)
    errors = [l for l in log.splitlines() if "Daemon loop error" in l or "Traceback" in l]
    if errors:
        out.append(R(WARN, "Recent daemon errors", f"{len(errors)} in the last 200 log lines; latest: {errors[-1][-160:]}",
                     "Run: journalctl --user -u deckstream -n 80"))
    else:
        out.append(R(OK, "Recent daemon errors", "none in the last 200 log lines"))
    return out


def check_saves():
    lud = which("ludusavi")
    out = [R(OK if lud else WARN, "Ludusavi (save backups)", lud or "not installed",
             "" if lud else "Re-run the installer (needs github.com or your DeckStream mirror).")]
    status = load_json(DATA / "saves-status.json", {})
    if status:
        bad = sorted(k for k, v in status.items() if v.get("status") in ("no-saves-found", "error"))   # "no-saves-yet" is normal
        out.append(R(WARN if bad else OK, "Save backups", f"{len(status)} game(s) tracked" + (f"; need attention: {', '.join(bad)}" if bad else ""),
                     "For an unmatched game: python3 ~/.config/deckstream/saves.py title <game> \"Official name\"" if bad else ""))
    return out


def check_system():
    out = []
    rc, text = run_cmd(["iw", "dev"])
    m = re.search(r"Interface\s+(\S+)", text)
    if m:
        rc, ps = run_cmd(["iw", "dev", m.group(1), "get", "power_save"])
        if "Power save" in ps:
            state = ps.strip().split(":")[-1].strip()
            # Streaming makes many small reads; power saving delays each one. The Deck's own Developer toggle stays off
            # across reboots (the old `iw ... power_save off` hint reset at every boot). Not proven on the Deck yet:
            # board row 29 measures it, so this stays a suggestion.
            out.append(R(INFO, "Wi-Fi power saving", state,
                         "If streamed games load slowly or stutter, try it off and compare a launch: Settings > System > "
                         "Developer Mode on, then Settings > Developer > Wi-Fi power management off (stays off after a "
                         "reboot)." if state.lower() == "on" else ""))
    rc, st = run_cmd(["steamos-readonly", "status"])
    if "disabled" in st.lower():
        out.append(R(WARN, "SteamOS system partition", "unlocked", "Lock it again: sudo steamos-readonly enable (updates discard what you install there)."))
    pend = load_json(CONF / "pending_shortcuts.json", [])
    if pend:
        out.append(R(WARN, "Steam shortcuts waiting", f"{len(pend)} waiting for Steam to close", "Exit Steam (Desktop Mode: Steam > Exit) and they will be added."))
    return out


def stale_stream_mounts():
    base = HOME / ".local" / "share" / "deckstream" / "stream" / "games"
    out = []
    try:
        for d in sorted(base.iterdir()):
            try:
                os.listdir(d)       # stat() succeeds on a dead rclone mount (long attribute cache); listing does not
            except OSError as e:
                if getattr(e, "errno", None) == errno.ENOTCONN:
                    out.append(d.name)
    except OSError:
        pass
    return out


def check_stale_mounts():
    stale = stale_stream_mounts()
    if not stale:
        return []
    return [R(WARN, "Stale stream mounts", ", ".join(stale),
              "Left behind when a game exited. The daemon clears them within a minute; by hand: fusermount3 -uz <folder under ~/.local/share/deckstream/stream/games>")]


def check_audio():
    """Steam Link / Remote Play make Steam's virtual "steam-streaming-playback" the default output, and
    every game then plays into it instead of the speakers (seen on the Deck 2026-10-09: no game sound)."""
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    rc, sink = run_cmd(["pactl", "get-default-sink"])
    sink = sink.strip()
    if rc != 0 or not sink:
        return []
    if "streaming" in sink.lower():
        return [R(WARN, "Audio output", f"default output is '{sink}' (Steam Link / Remote Play streaming), so game sound will not reach the speakers",
                  "Turn Remote Play / Steam Link off (Steam > Settings > Remote Play), or switch the output: "
                  "pactl list short sinks   then   pactl set-default-sink <speaker sink name>")]
    return [R(OK, "Audio output", sink)]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_install(cfg):
    local = load_json(CONF / "manifest.json")
    if not local:
        return [R(INFO, "Installed files", "no manifest.json (installed before checksums existed)", "Re-run the installer once.")]
    bad = []
    for name, info in sorted(local.get("files", {}).items()):
        p = CONF / name
        if not p.is_file():
            bad.append(f"{name} (missing)")
        elif sha256(p) != info.get("sha256"):
            bad.append(f"{name} (changed)")
    out = [R(WARN if bad else OK, "Installed files", ("; ".join(bad) if bad else f"all {len(local.get('files', {}))} match version {local.get('version', '?')}"),
             "Re-run the installer to repair." if bad else "")]
    if cfg and cfg.get("server_url"):
        try:
            status, _, body = http(cfg["server_url"].rstrip("/") + "/daemon/manifest.json")
            remote = json.loads(body) if status == 200 else None
        except (OSError, ValueError, urllib.error.URLError):
            remote = None
        if remote and remote.get("version") != local.get("version"):
            out.append(R(WARN, "Update available", f"installed {local.get('version', '?')}, latest {remote.get('version')}",
                         "Run the installer command again (it waits for a running job)."))
        elif remote:
            out.append(R(OK, "Up to date", f"version {local.get('version', '?')}"))
    return out


def run_checks():
    cfg = load_json(CONF / "daemon_config.json", {})
    results = []
    for fn in (check_python, lambda: check_config(cfg), lambda: check_dns(cfg), lambda: check_server(cfg), check_storage,
               check_rclone, check_fuse, check_proton, check_disks, check_services, check_saves, check_system,
               check_audio, check_stale_mounts, lambda: check_install(cfg)):
        try:
            results.extend(fn())
        except Exception as e:  # noqa: BLE001 - a broken check must never hide the others
            results.append(R(WARN, getattr(fn, "__name__", "check"), f"the check itself failed: {e}"))
    return results


# ------------------------------------------------------------------ output
def redact(text):
    secrets = {storage_key(), (load_json(CONF / "daemon_config.json", {}) or {}).get("queue_secret", "")}
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    text = re.sub(r"(token|api_key|apikey|secret|password)=([^&\s\"']+)", r"\1=***", text, flags=re.I)
    return re.sub(r"(Bearer\s+)\S+", r"\1***", text)


def render(results, brief=False):
    lines = []
    for r in results:
        if brief and r.status in (OK, INFO):
            continue
        lines.append(f"{ICON[r.status]} {r.title}" + (f": {r.detail}" if r.detail else ""))
        if r.fix and r.status in (WARN, FAIL, INFO):
            lines.append("     → " + r.fix)
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    lines.append("")
    lines.append(f"{fails} problem(s), {warns} warning(s)." if (fails or warns) else "Everything checks out.")
    return "\n".join(lines)


def write_bundle(results):
    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / f"diag-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    parts = ["DeckStream diagnostics (secrets removed)", time.strftime("%Y-%m-%d %H:%M:%S"), "", render(results), ""]
    for title, argv in (("daemon log (last 80 lines)", ["journalctl", "--user", "-u", "deckstream", "-n", "80", "--no-pager"]),
                        ("disk usage", ["df", "-h", "/home"]), ("mounts under /run/media", ["sh", "-c", "mount | grep /run/media || true"])):
        rc, txt = run_cmd(argv, timeout=10)
        parts += [f"--- {title}", txt.strip(), ""]
    status = load_json(DATA / "saves-status.json")
    if status:
        parts += ["--- save backups", json.dumps(status, indent=2), ""]
    out.write_text(redact("\n".join(parts)))
    os.chmod(out, 0o600)
    return out


def main(argv=None):
    args = set(argv if argv is not None else sys.argv[1:])
    results = run_checks()
    if "--json" in args:
        print(redact(json.dumps([r.as_dict() for r in results], indent=2)))
    elif "--bundle" in args:
        print(render(results))
        print(f"\nWrote {write_bundle(results)} (secrets removed). Share that file.")
    else:
        print(redact(render(results, brief="--brief" in args)))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
