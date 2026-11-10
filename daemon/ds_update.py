"""Self-update from the user's site and the Decky plugin update.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import hashlib
import os
import time
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

import ds_core
from ds_core import CURRENT_JOB, HOME, STATE_DIR
from ds_stream import POWER_SUPPLY_DIR, fills_blocked, on_ac_power, running_fills


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
    if AUTO_UPDATE_OFF.exists() or not ds_core.SERVER_URL:
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
        with opener(urllib.request.Request(f"{ds_core.SERVER_URL}/daemon/{name}", headers={"User-Agent": "DeckStream/1.0"}),
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
        + env_args + ["bash", str(inst), ds_core.SERVER_URL, ds_core.DEVICE_ID]
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
    if not plugin_dir.is_dir() or not ds_core.SERVER_URL:
        return False
    main_py = plugin_dir / "main.py"
    dist_js = plugin_dir / "dist" / "index.js"
    if not (main_py.exists() and os.access(main_py, os.W_OK) and dist_js.exists() and os.access(dist_js, os.W_OK)):
        return False
    opener = opener or urllib.request.urlopen
    url = f"{ds_core.SERVER_URL.rstrip('/')}/decky/DeckStream.zip"
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
