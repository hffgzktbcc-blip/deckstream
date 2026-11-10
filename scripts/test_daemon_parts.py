#!/usr/bin/env python3
"""The daemon is deckstream-daemon.py plus its ds_*.py parts. Checks that every part ships, that the Decky panel's
way of loading the daemon still finds every name on it, that a second load with another $HOME gets fresh parts
(the panel reloads the daemon after a self-update), and that the daemon still runs as a script."""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAEMON = ROOT / "daemon" / "deckstream-daemon.py"
sys.path.insert(0, str(ROOT / "scripts"))
import make_manifest  # noqa: E402


def load(home, name):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(name, str(DAEMON))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


home1, home2 = Path(tempfile.mkdtemp(prefix="dd_parts1_")), Path(tempfile.mkdtemp(prefix="dd_parts2_"))
d1 = load(home1, "d1")

# 1. every part on disk is loaded by the daemon and shipped (installed, checksummed, mode 644)
on_disk = sorted(p.stem for p in (ROOT / "daemon").glob("ds_*.py"))
assert sorted(d1._PARTS) == on_disk, (d1._PARTS, on_disk)
for part in on_disk:
    assert f"{part}.py" in make_manifest.SHIPPED and f"{part}.py" in make_manifest.MODULES, part
print(f"[ok] {len(on_disk)} parts, all loaded by the daemon and listed in the manifest")

# 2. names the Decky panel and the tests call are reachable on the daemon module, as before the split
for name in ("installed_version", "on_ac_power", "battery_percent", "live_activity", "fill_progress", "latest_launch",
             "streamed_games", "streamed_games_cached", "deck_status", "deck_mode_state", "apply_deck_mode_wanted",
             "DECK_MODE_ENV", "run_daemon", "janitor_pass", "self_update_pass", "_cef_target", "_GAMES_CACHE"):
    assert hasattr(d1, name), name
assert d1.CONFIG_FILE == home1 / ".config" / "deckstream" / "daemon_config.json"
print("[ok] the panel's names are on the daemon module")

# 3. a second load (another $HOME, as after a self-update) gets fresh parts, not the first load's
d2 = load(home2, "d2")
assert d2.CONFIG_FILE.is_relative_to(home2) and d2.INSTALL_DIR.is_relative_to(home2)
assert d2.ds_core is not d1.ds_core and d2.ds_core.HOME == home2 and d1.ds_core.HOME == home1
assert str(ROOT / "daemon") not in sys.path, "loading the daemon leaves sys.path as it was"
print("[ok] a reload gets fresh parts with the new $HOME")

# 4. settings set at start-up are seen by every part (they live in ds_core)
d2.ds_core.SERVER_URL = "https://example.invalid"
assert sys.modules["ds_update"].ds_core.SERVER_URL == "https://example.invalid"
print("[ok] start-up settings are shared by all parts")

# 5. still runs as a script
r = subprocess.run([sys.executable, str(DAEMON), "--live-shortcuts", "status"], capture_output=True, text=True,
                   env={**os.environ, "HOME": str(home1)}, timeout=60)
assert r.returncode == 0, r.stderr
print("[ok] runs as a script")
