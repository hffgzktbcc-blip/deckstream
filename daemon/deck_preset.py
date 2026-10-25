#!/usr/bin/env python3
"""Deck mode graphics ("Deck mode: 30 fps, low graphics"): low settings for Unreal Engine games, written only when the
game has no settings file yet. An existing file is never touched, so a game the user already tuned keeps its settings.

Usage: deck_preset.py GAME.exe PREFIX_DIR [proton|umu]   (run by game-launcher.sh when DECKSTREAM_LOW_PRESET=1)

Unreal reads [ScalabilityGroups] from Saved/Config/<platform>/GameUserSettings.ini under %LOCALAPPDATA%\\<Project>.
Level 1 is "Medium" on the 0-3 scale (0 Low looks broken in many games); FrameRateLimit matches the 30 fps cap. UE4
before 4.25 names the folder WindowsNoEditor, later versions Windows: both are written, the unused one is ignored.
The file lives in the Wine prefix (local drive), never in the game folder or the stream mount. Other engines and games
that keep settings elsewhere are left alone; most Deck Verified games pick their own Deck preset.
"""
import os
import sys
from pathlib import Path

UE_PRESET = """[ScalabilityGroups]
sg.ResolutionQuality=100
sg.ViewDistanceQuality=1
sg.AntiAliasingQuality=1
sg.ShadowQuality=1
sg.GlobalIlluminationQuality=1
sg.ReflectionQuality=1
sg.PostProcessQuality=1
sg.TextureQuality=1
sg.EffectsQuality=1
sg.FoliageQuality=1
sg.ShadingQuality=1

[/Script/Engine.GameUserSettings]
Version=5
FrameRateLimit=30.000000
"""
UE_PLATFORMS = ("Windows", "WindowsNoEditor")


def unreal_project(exe):
    """The Unreal project name for a game exe, or None. Either the real binary (<Root>/<Project>/Binaries/Win64/x.exe)
    or the small launcher next to it (<Root>/<Project>.exe); both need <Root>/Engine to be sure it is Unreal."""
    exe = Path(exe)
    p = exe.parent
    if p.name.lower() in ("win64", "wingdk", "win32") and p.parent.name.lower() == "binaries":
        proj = p.parent.parent
        return proj.name if (proj.parent / "Engine").is_dir() else None
    root = exe.parent
    if not (root / "Engine").is_dir():
        return None
    try:
        for d in sorted(root.iterdir()):
            if d.is_dir() and d.name != "Engine" and (d / "Binaries").is_dir() and (d / "Content").is_dir():
                return d.name
    except OSError:
        pass
    return None


def wine_root(prefix, runner="proton"):
    """The folder holding drive_c. Proton keeps it in <prefix>/pfx; umu uses the prefix itself (and links pfx to it).
    A prefix no game has run in yet gets the layout its runner will use."""
    prefix = Path(prefix)
    if (prefix / "pfx").exists():
        return (prefix / "pfx").resolve()
    if (prefix / "drive_c").is_dir():
        return prefix
    return prefix if runner == "umu" else prefix / "pfx"


def apply(exe, prefix, runner="proton"):
    """Write the preset when it fits. Returns a short line for the log."""
    proj = unreal_project(exe)
    if not proj or "/" in proj or proj in (".", ".."):
        return "Deck mode graphics: not an Unreal game, settings left as they are"
    local = wine_root(prefix, runner) / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / proj / "Saved" / "Config"
    files = [local / plat / "GameUserSettings.ini" for plat in UE_PLATFORMS]
    if any(f.exists() for f in files):
        return f"Deck mode graphics: {proj} already has its settings file, left as it is"
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".deckstream-tmp")
        tmp.write_text(UE_PRESET.replace("\n", "\r\n"))
        os.replace(tmp, f)
    return f"Deck mode graphics: wrote low settings for {proj} (Unreal)"


def main(argv):
    if len(argv) < 3:
        print(__doc__.strip().splitlines()[2])
        return 2
    try:
        print("[*] " + apply(argv[1], argv[2], argv[3] if len(argv) > 3 else "proton"))
    except OSError as e:
        print(f"[!] Deck mode graphics not written: {e.strerror or type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
