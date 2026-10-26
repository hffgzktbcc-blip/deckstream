#!/usr/bin/env python3
"""Deck mode ("Deck mode: 30 fps, low graphics"), per game: the daemon applies the phone's choice from the heartbeat
reply once and only when newer, reports it, the Decky panel writes the same files, and deck_preset.py writes Unreal
low settings only where no settings file exists yet."""
import importlib.util, json, os, subprocess, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
home = Path(tempfile.mkdtemp(prefix="dd_deck_mode_")); os.environ["HOME"] = str(home)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


d = load(ROOT / "daemon" / "deckstream-daemon.py", "d")
conf = home / ".config" / "deckstream"

# 1. off by default; the phone's choice is applied once and reported
assert d.deck_mode_state() == {} and d.deck_status()["deck_mode"] == {}
t = int(time.time() * 1000)
assert d.apply_deck_mode_wanted({"hades_ii": {"on": True, "at": t}, "../x": {"on": True, "at": t}, "bad": {"on": "yes"}}) == 1
env = conf / "deck_mode" / "hades_ii.env"
assert env.read_text() == "DECKSTREAM_FPS_CAP=30\nDECKSTREAM_LOW_PRESET=1\n"
assert not (conf / "deck_mode" / "...x.env").exists() and len(list((conf / "deck_mode").iterdir())) == 1
assert d.deck_status()["deck_mode"] == {"hades_ii": {"on": True, "at": t}}
assert d.apply_deck_mode_wanted({"hades_ii": {"on": True, "at": t}}) == 0, "the same choice is applied once"
assert d.apply_deck_mode_wanted(None) == 0 and d.apply_deck_mode_wanted([1]) == 0
print("[ok] the phone's choice writes deck_mode/<slug>.env once, bad slugs refused, the heartbeat reports it")

# 2. a newer choice made on the Deck (Decky) wins over an older one still on the site; off removes the file
plugin = load(ROOT / "decky" / "main.py", "plugin")
r = plugin.set_deck_mode("hades_ii", False, home=home, now_ms=t + 5000)
assert r["ok"] and not env.exists(), r
assert d.apply_deck_mode_wanted({"hades_ii": {"on": True, "at": t + 1000}}) == 0 and not env.exists()
assert plugin.set_deck_mode("../etc", True, home=home)["ok"] is False
r = plugin.set_deck_mode("sifu", True, home=home)
assert r["ok"] and (conf / "deck_mode" / "sifu.env").read_text() == d.DECK_MODE_ENV
import shutil; shutil.copy(ROOT / "daemon" / "deckstream-daemon.py", conf / "deckstream-daemon.py")
assert plugin.status(home)["deck_mode"] == {"hades_ii": False, "sifu": True}
assert d.deck_mode_state()["sifu"]["on"] is True, "the daemon reads what the panel wrote"
print("[ok] Decky writes the same files; a newer Deck choice wins; the panel shows each game's switch")

# 3. Unreal preset: written into the prefix only when the game has no settings file; never overwrites
pre = load(ROOT / "daemon" / "deck_preset.py", "pre")
g = home / "Games" / "Stray"
(g / "Engine").mkdir(parents=True); (g / "Hk_project" / "Binaries" / "Win64").mkdir(parents=True); (g / "Hk_project" / "Content").mkdir()
exe = g / "Hk_project" / "Binaries" / "Win64" / "Stray-Win64-Shipping.exe"; exe.write_text("")
launcher_exe = g / "Stray.exe"; launcher_exe.write_text("")
assert pre.unreal_project(exe) == "Hk_project" and pre.unreal_project(launcher_exe) == "Hk_project"
other = home / "Games" / "Hades"; other.mkdir(parents=True); (other / "Hades.exe").write_text("")
assert pre.unreal_project(other / "Hades.exe") is None
assert "not an Unreal game" in pre.apply(other / "Hades.exe", home / "pfx_hades")
prefix = home / "prefixes" / "stray"
assert "wrote low settings" in pre.apply(exe, prefix, "proton")
ini = prefix / "pfx/drive_c/users/steamuser/AppData/Local/Hk_project/Saved/Config/Windows/GameUserSettings.ini"
txt = ini.read_bytes().decode()
assert "sg.ShadowQuality=1" in txt and "FrameRateLimit=30.000000" in txt and "Version=5" in txt and "\r\n" in txt
assert (ini.parent.parent / "WindowsNoEditor" / "GameUserSettings.ini").exists()
ini.write_text("user tuned\n")
assert "already has its settings file" in pre.apply(exe, prefix, "proton") and ini.read_text() == "user tuned\n"
umu_prefix = home / "prefixes" / "stray_umu"
pre.apply(launcher_exe, umu_prefix, "umu")
assert (umu_prefix / "drive_c/users/steamuser/AppData/Local/Hk_project/Saved/Config/Windows/GameUserSettings.ini").exists()
assert not (umu_prefix / "pfx").exists(), "a fresh umu prefix keeps drive_c in the prefix itself"
out = subprocess.run(["python3", str(ROOT / "daemon" / "deck_preset.py"), str(exe), str(prefix)], capture_output=True, text=True)
assert out.returncode == 0 and "left as it is" in out.stdout, out
print("[ok] Unreal low settings go in the prefix only when absent; an existing file is never touched; other engines skipped")

# 4. shipped and wired in
assert '"deck_preset.py"' in (ROOT / "scripts" / "make_manifest.py").read_text()
gl = (ROOT / "daemon" / "game-launcher.sh").read_text()
assert 'deck_preset.py" "$EXE" "$PREFIX"' in gl and "DECKSTREAM_LOW_PRESET" in gl
print("DECK MODE TESTS PASSED")
