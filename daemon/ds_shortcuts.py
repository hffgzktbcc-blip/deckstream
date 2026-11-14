"""Non-Steam shortcuts: pending and live (added through Steam's CEF debugger) shortcuts, shortcuts.vdf and artwork.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import os
import json
import shutil
import subprocess
import urllib.request
import urllib.parse
import socket
from pathlib import Path

from ds_core import ARTWORK_HOSTS, HOME, find_steam_userdata_dir, open_checked
from ds_steam import calculate_shortcut_id, make_game_launcher, parse_binary_vdf, serialize_binary_vdf


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
