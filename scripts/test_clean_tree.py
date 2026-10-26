#!/usr/bin/env python3
"""Keeps the public tree free of piracy sources and the old private project's names: no indexers, debrid services,
release groups or torrent flows in any text file. CONTRIBUTING.md states the rule itself, so it is the one exception."""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(
    r"torbox|debrid|torrent|magnet:|\bmagnets?\b|seeders?\b|fitgirl|dodi|steam-?rip|steam-?gg|elamigos|insaneramzes|"
    r"rutor|apibay|1337x|rarbg|prowlarr|jackett|newznab|\bnzb|usenet|repack|warez|igg-games|gog-games|codex|skidrow|"
    r"empress|tenoke|razor1911|emudeck|\.nsp\b|\.xci\b", re.I)
ALLOWED = {"CONTRIBUTING.md", "scripts/test_clean_tree.py"}
BINARY = (".zip", ".woff2", ".png", ".jpg", ".ico", ".webp")

files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True,
                       check=True).stdout.split("\n")
hits = []
for name in filter(None, files):
    p = ROOT / name
    if name in ALLOWED or name.endswith(BINARY) or not p.is_file():
        continue
    for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
        m = FORBIDDEN.search(line)
        if m:
            hits.append(f"{name}:{n}: {m.group(0)!r}: {line.strip()[:120]}")
for name in filter(None, files):          # the built plugin Decky downloads, too
    if name.endswith(".zip"):
        with zipfile.ZipFile(ROOT / name) as z:
            for inner in z.namelist():
                m = FORBIDDEN.search(z.read(inner).decode(errors="replace")) if not inner.endswith("/") else None
                if m:
                    hits.append(f"{name}!{inner}: {m.group(0)!r}")
if hits:
    print("\n".join(hits[:40]))
    sys.exit(f"{len(hits)} line(s) name piracy sources or the old project")
print(f"[ok] clean tree: {len([f for f in files if f])} files, no piracy sources or old project names")
