#!/usr/bin/env python3
"""Write daemon/manifest.json: the list of files the installer ships, with SHA-256 and file mode.

  python3 scripts/make_manifest.py            regenerate daemon/manifest.json
  python3 scripts/make_manifest.py --check    verify manifest is current
"""
import hashlib
import json
import os
import sys
from pathlib import Path

SHIPPED = [
    "deckstream-daemon.py", "stream-runner.sh", "startup_profile.py",
    "demand_gate.py", "hedge_proxy.py", "saves.py", "proton_matcher.py",
    "doctor.py", "language_fix.py", "umu_gameid.py", "deck_preset.py",
    "parallel_download.py", "parallel_unzip.py", "suspend-resume-hook.sh",
    "focus_game.sh", "game-launcher.sh", "install.sh"
]
MODULES = {"parallel_download.py"}


def build(folder):
    files = {}
    for name in SHIPPED:
        target = folder / name
        if not target.exists():
            continue
        data = target.read_bytes()
        files[name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": "644" if name in MODULES else "755",
            "bytes": len(data)
        }
    combined = hashlib.sha256("".join(f"{n}:{files[n]['sha256']}" for n in sorted(files)).encode()).hexdigest()
    return {"version": f"v{combined[:10]}", "files": files}


def main():
    root = Path(__file__).resolve().parent.parent
    daemon_dir = root / "daemon"
    manifest_file = daemon_dir / "manifest.json"

    check_only = "--check" in sys.argv

    manifest = build(daemon_dir)
    encoded = json.dumps(manifest, indent=2) + "\n"

    if check_only:
        if not manifest_file.exists():
            print("manifest.json missing", file=sys.stderr)
            sys.exit(1)
        existing = manifest_file.read_text()
        if existing != encoded:
            print("manifest.json is stale; run make_manifest.py", file=sys.stderr)
            sys.exit(1)
        print("manifest is current")
        return

    manifest_file.write_text(encoded)
    print(f"wrote {manifest_file} ({len(manifest['files'])} files)")


if __name__ == "__main__":
    main()
