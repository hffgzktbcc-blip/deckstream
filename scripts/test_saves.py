"""saves.py with a stand-in Ludusavi (the real one only runs on the Deck)."""
import fcntl, json, os, stat, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAVES = str(ROOT / "daemon" / "saves.py")
import importlib.util as _iu
_spec = _iu.spec_from_file_location("saves_mod", SAVES); saves_mod = _iu.module_from_spec(_spec); _spec.loader.exec_module(saves_mod)
tmp = Path(tempfile.mkdtemp()); home = tmp / "home"; home.mkdir()
calls = tmp / "calls.log"
fake = tmp / "ludusavi"
fake.write_text(f'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open({str(calls)!r}, "a").write(json.dumps(a) + "\\n")
known = [k for k in os.environ.get("FAKE_KNOWN", "").split("|") if k]
title = a[1] if len(a) > 1 else ""
if a[0] == "backup" and title in known:
    print(json.dumps({{"games": {{title: {{"decision": "Processed", "files": {{"/p/save1.dat": {{"bytes": 100}}, "/p/save2.dat": {{"bytes": 50}}}}}}}}}}))
elif a[0] == "restore" and "--preview" in a and title in known:
    print(json.dumps({{"games": {{title: {{"decision": "Processed", "files": {{"/p/save1.dat": {{"bytes": 100}}}}}}}}}}))
elif a[0] == "restore" and "--force" in a and title in known:
    print(json.dumps({{"games": {{title: {{"decision": "Processed", "files": {{"/p/save1.dat": {{"bytes": 100}}}}}}}}}}))
else:
    print(json.dumps({{"games": {{}}}}))
''')
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

def run(*args, known="", lud=str(fake), env_extra=None):
    env = {**os.environ, "HOME": str(home), "LUDUSAVI": lud, "FAKE_KNOWN": known, **(env_extra or {})}
    env.pop("DECKSTREAM_SAVES_DIR", None)
    return subprocess.run([sys.executable, SAVES, *args], env=env, capture_output=True, text=True)

def last_calls():
    return [json.loads(l) for l in calls.read_text().splitlines()] if calls.exists() else []

# 1. title cleaning
sys.path.insert(0, str(ROOT / "daemon")); os.environ["HOME"] = str(home)
import importlib; saves = importlib.import_module("saves")
cases = {
    "Ghost.of.Tsushima.Directors.Cut-InsaneRamZes": ["Ghost of Tsushima Directors Cut", "Ghost of Tsushima"],
    "ELDEN.RING.Deluxe.Edition.Steam.Rip-InsaneRamZes": ["ELDEN RING Deluxe Edition", "ELDEN RING"],
    "Cyberpunk 2077 [v 1.03] (2020) PC | GOG-Rip": ["Cyberpunk 2077"],
    "Marvel's Spider-Man": ["Marvel's Spider-Man"],
    "Hades II v1.2.3-FLT": ["Hades II"],
}
for name, want in cases.items():
    got = saves.title_candidates(name)
    assert got == want, (name, got, want)
assert saves.title_candidates("x", override="Official")[0] == "Official"
print("[ok] torrent-style names turn into likely official titles (Spider-Man survives, release groups don't)")

# 2. register + prefix layouts
proton = tmp / "compat" / "id"; (proton / "pfx" / "drive_c").mkdir(parents=True)
plain = tmp / "umu" / "prefix"; (plain / "drive_c").mkdir(parents=True)
# the unknown game HAS written a save (so "no saves found" means Ludusavi didn't match it)
(plain / "drive_c/users/steamuser/Documents/Some Game").mkdir(parents=True)
(plain / "drive_c/users/steamuser/Documents/Some Game/slot1.sav").write_bytes(b"x")
assert saves.wine_prefix(proton) == proton / "pfx" and saves.wine_prefix(plain) == plain and saves.wine_prefix(tmp / "nope") is None
(proton / "pfx" / "user.reg").write_text("REGEDIT4")
for i in range(5):
    pass
r = run("register", "--slug", "ghost", "--title", "Ghost.of.Tsushima.Directors.Cut-InsaneRamZes", "--prefix", str(proton))
assert r.returncode == 0, r.stderr
run("register", "--slug", "mystery", "--title", "Some Unknown Game", "--prefix", str(plain))
run("register", "--slug", "never", "--title", "Never Run Game", "--prefix", str(tmp / "empty"))
g = json.loads((home / ".config/deckstream/games.json").read_text())
assert set(g) == {"ghost", "mystery", "never"} and g["ghost"]["kind"] == "installed"
assert oct((home / ".config/deckstream/games.json").stat().st_mode & 0o777) == "0o600"
print("[ok] games registered privately; Proton's pfx/ folder and plain Wine prefixes are both understood")

# 3. backup tries candidates until one matches, passes the right flags, remembers the winner
r = run("backup", "--all", known="Ghost of Tsushima")
assert r.returncode == 0, r.stdout + r.stderr
bk = [c for c in last_calls() if c[0] == "backup"]
ghost = [c for c in bk if str(proton / "pfx") in c]
assert [c[1] for c in ghost] == ["Ghost of Tsushima Directors Cut", "Ghost of Tsushima"], ghost
last = ghost[-1]
for flag in ("--force", "--api"):
    assert flag in last
assert last[last.index("--full-limit") + 1] == "5"
assert last[last.index("--path") + 1] == str(home / ".local/share/deckstream/saves-backup")
assert last[last.index("--wine-prefix") + 1] == str(proton / "pfx")
st = json.loads((home / ".local/share/deckstream/saves-status.json").read_text())
assert st["ghost"]["status"] == "backed-up" and st["ghost"]["files"] == 2 and st["ghost"]["bytes"] == 150 and st["ghost"]["title"] == "Ghost of Tsushima"
assert json.loads((home / ".config/deckstream/ludusavi_titles.json").read_text())["ghost"] == "Ghost of Tsushima"
print("[ok] backup: wrong candidate skipped, right flags (--wine-prefix <prefix>/pfx, --force, --path, --full-limit 5), winner remembered")

# 3b. inside a Steam launch LD_PRELOAD holds Steam's 32-bit overlay; Ludusavi (64-bit) must not inherit it
preload = tmp / "ludusavi_preload_sensitive"
preload.write_text(f'''#!/usr/bin/env python3
import json, os, sys
if os.environ.get("LD_PRELOAD"):
    sys.stderr.write("ERROR: ld.so: object 'gameoverlayrenderer.so' from LD_PRELOAD cannot be preloaded (wrong ELF class: ELFCLASS32): ignored.\\n")
    sys.exit(101)
print(json.dumps({{"games": {{"Ghost of Tsushima": {{"decision": "Processed", "files": {{"/p/a.sav": {{"bytes": 10}}}}}}}}}}))
''')
preload.chmod(preload.stat().st_mode | stat.S_IEXEC)
r = run("backup", "ghost", lud=str(preload), env_extra={"LD_PRELOAD": "/steam/ubuntu12_32/gameoverlayrenderer.so"})
st = json.loads((home / ".local/share/deckstream/saves-status.json").read_text())
assert st["ghost"]["status"] == "backed-up", st["ghost"]
print("[ok] a Steam LD_PRELOAD in the environment does not break the Ludusavi backup")

# 3c. a dead stream mount makes Ludusavi panic (exit 101, "not connected"); the tool clears it and retries once
import errno as _errno
dead = tmp / "games_dead"; (dead / "g1").mkdir(parents=True); (dead / "g2").mkdir(parents=True)
def fake_listdir(path):
    if Path(path).name == "g1": raise OSError(_errno.ENOTCONN, "Transport endpoint is not connected")
    return os.listdir(path)
fused = []
class _R: returncode = 0
cleared = saves_mod.clear_stale_stream_mounts(dead, listdir=fake_listdir, run=lambda a, **k: (fused.append(a), _R())[1])
assert [p.name for p in cleared] == ["g1"] and fused == [["fusermount3", "-uz", str(dead / "g1")]], (cleared, fused)
once_marker = tmp / "panic_once"
flaky = tmp / "ludusavi_flaky"
flaky.write_text(f'''#!/usr/bin/env python3
import json, os, sys
m = {str(once_marker)!r}
if not os.path.exists(m):
    open(m, "w").write("x")
    sys.stderr.write("thread 'main' panicked at src/path.rs:217:44:\\nOs {{ code: 107, kind: NotConnected, message: \\"Transport endpoint is not connected\\" }}\\n")
    sys.exit(101)
print(json.dumps({{"games": {{"Ghost of Tsushima": {{"decision": "Processed", "files": {{"/p/a.sav": {{"bytes": 10}}}}}}}}}}))
''')
flaky.chmod(flaky.stat().st_mode | stat.S_IEXEC)
saves_mod.clear_stale_stream_mounts = lambda *a, **k: [Path("/x")]        # pretend one dead mount was cleared
rc, out, err = saves_mod.run_ludusavi(str(flaky), ["backup", "x"])
assert rc == 0 and "Ghost of Tsushima" in out, (rc, out, err)
print("[ok] a Ludusavi panic caused by a dead stream mount is retried once after the mount is cleared")

# 4. the unmatched / unrun games are reported, not hidden
assert st["mystery"]["status"] == "no-saves-found" and "saves.py title mystery" in st["mystery"]["message"]
assert st["never"]["status"] == "prefix-missing"
print("[ok] a game Ludusavi doesn't recognise, and one that was never run, are reported with advice")
# a game launched once that never saved: Proton's own setup files don't count, nor Temp / Microsoft folders
fresh = tmp / "compat" / "fresh"; u = fresh / "pfx" / "drive_c" / "users" / "steamuser"
(u / "AppData/Roaming/Microsoft").mkdir(parents=True); (u / "AppData/Local/Temp").mkdir(parents=True); (u / "Documents").mkdir()
(u / "Documents/desktop.ini").write_text("x")                       # written by Proton's setup
import time as _tt, os as _os
old_t = _tt.time() - 600
_os.utime(u / "Documents/desktop.ini", (old_t, old_t))
(fresh / "tracked_files").write_text(""); _os.utime(fresh / "tracked_files", (old_t + 1, old_t + 1))
(u / "AppData/Roaming/Microsoft/x.dat").write_text("x"); (u / "AppData/Local/Temp/y.tmp").write_text("x")
assert not saves.user_files_written(fresh / "pfx"), "setup files, Temp and Microsoft folders are not saves"
(u / "AppData/Roaming/EldenRing").mkdir(); (u / "AppData/Roaming/EldenRing/ER0000.sl2").write_bytes(b"x")
assert saves.user_files_written(fresh / "pfx"), "a file the game wrote counts"
print("[ok] a game that never wrote a save is 'no-saves-yet' (not a title problem); a written save still counts")

# 5. user.reg copy, pruned to the last 3
for _ in range(4):
    run("backup", "ghost", known="Ghost of Tsushima"); import time as _t; _t.sleep(1.1)
extras = sorted((home / ".local/share/deckstream/saves-backup/_extras/ghost").glob("user.reg.*"))
assert len(extras) == 3, extras
assert oct((home / ".local/share/deckstream/saves-backup").stat().st_mode & 0o777) == "0o700"
print("[ok] user.reg kept (registry saves), last 3 only; backup folder is private (700)")

# 6. second candidate not retried once a title is remembered (override tried first)
calls.write_text("")
run("backup", "ghost", known="Ghost of Tsushima")
assert [c[1] for c in last_calls() if c[0] == "backup"] == ["Ghost of Tsushima"]
print("[ok] a remembered title is used first")

# 7. restore: preview by default; --yes makes a safety copy first, then restores
calls.write_text("")
r = run("restore", "ghost", known="Ghost of Tsushima")
assert r.returncode == 0 and "Nothing changed" in r.stdout
assert not any(c[0] == "restore" and "--force" in c for c in last_calls())
calls.write_text("")
r = run("restore", "ghost", "--yes", known="Ghost of Tsushima")
seq = [(c[0], "--force" in c, "--preview" in c) for c in last_calls()]
assert seq == [("restore", False, True), ("backup", True, False), ("restore", True, False)], seq
assert (home / ".local/share/deckstream/saves-backup/_pre-restore").is_dir()
assert run("restore", "nonexistent").returncode == 2
assert run("restore", "ghost", "--yes", known="").returncode == 1     # no backup known -> refuses, changes nothing
print("[ok] restore previews first, takes a safety copy before overwriting, refuses when there is no backup")

# 8. Ludusavi missing: calm exit 3, nothing crashes
r = run("backup", "--all", lud=str(tmp / "missing"), env_extra={"PATH": "/usr/bin:/bin"})
assert r.returncode == 3 and "not installed" in r.stdout
print("[ok] no Ludusavi installed -> clear message, exit code 3")

# 9. a backup already running blocks a second one politely
lock = open(home / ".local/share/deckstream/saves.lock", "w"); fcntl.flock(lock, fcntl.LOCK_EX)
r = run("backup", "--all", known="Ghost of Tsushima")
assert r.returncode == 0 and "already running" in r.stdout
fcntl.flock(lock, fcntl.LOCK_UN)
print("[ok] concurrent runs are serialised")

# 10. status text and JSON
r = run("status"); assert "ghost: backed-up" in r.stdout and "mystery: no-saves-found" in r.stdout
j = json.loads(run("status", "--json").stdout); assert "ghost" in j["status"] and j["dir"].endswith("saves-backup")
print("[ok] status readable by people and by the app (--json)")

# 11. a streamed game that saves into its OWN folder: the write layer is snapshotted (small files only, last 3)
import time as _t2
wl = home / ".local/share/deckstream/stream/writes/oldgame"
(wl / "Saves").mkdir(parents=True); (wl / "Saves" / "slot1.sav").write_bytes(b"progress-1")
(wl / "settings.ini").write_text("lang=en")
(wl / "Game.dxvk-cache").write_bytes(b"x" * 1000)                         # shader cache: skipped
(wl / "shadercache").mkdir(); (wl / "shadercache" / "a.bin").write_bytes(b"y")   # skipped
(wl / "huge.dat").write_bytes(b"\0"); os.truncate(wl / "huge.dat", saves.WRITES_FILE_MAX + 1)   # too big to be a save
os.symlink("/etc/passwd", wl / "link.sav")                                 # links are never followed
pfx = home / "pfx_oldgame" / "pfx" / "drive_c"; pfx.mkdir(parents=True)
run("register", "--slug", "oldgame", "--title", "Some Old Game", "--prefix", str(pfx.parent.parent), "--kind", "stream")
r = run("backup", "oldgame", known="")                                     # Ludusavi does not know the game
st = json.loads((home / ".local/share/deckstream/saves-status.json").read_text())
assert st["oldgame"]["status"] == "game-folder-only" and "writes." in st["oldgame"]["message"], st["oldgame"]
snaps = sorted((home / ".local/share/deckstream/saves-backup/_extras/oldgame").glob("writes.*"))
assert len(snaps) == 1 and not snaps[0].name.endswith(".partial")
got = sorted(str(p.relative_to(snaps[0])) for p in snaps[0].rglob("*") if p.is_file())
assert got == ["Saves/slot1.sav", "settings.ini"], got
assert (snaps[0] / "Saves" / "slot1.sav").read_bytes() == b"progress-1"
for i in range(4):
    (wl / "Saves" / "slot1.sav").write_bytes(f"progress-{i + 2}".encode()); _t2.sleep(1.1)
    assert saves.copy_write_layer("oldgame", writes_root=wl.parent) == 2
snaps = sorted((home / ".local/share/deckstream/saves-backup/_extras/oldgame").glob("writes.*"))
assert len(snaps) == 3 and (snaps[-1] / "Saves" / "slot1.sav").read_bytes() == b"progress-5", snaps
assert saves.copy_write_layer("nogame", writes_root=wl.parent) == 0
assert "oldgame: game-folder-only" in run("status").stdout
print("[ok] saves a streamed game writes into its own folder are snapshotted (no caches, big files or links), last 3 kept")
print("\nSAVES TESTS PASSED")
