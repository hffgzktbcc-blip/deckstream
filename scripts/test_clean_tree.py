#!/usr/bin/env python3
"""Keeps the public tree free of piracy sources and the old private project's names: no indexers, download services,
release groups or file-sharing flows in any text file, and no private keys, tokens or personal home paths. CONTRIBUTING.md states the rule itself, so it is the one exception."""
import base64
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The pattern is base64 so this file does not itself name what it keeps out.
FORBIDDEN = re.compile(base64.b64decode(
    "dG9yYm94fGRlYnJpZHx0b3JyZW50fG1hZ25ldDp8XGJtYWduZXRzP1xifHNlZWRlcnM/XGJ8Zml0Z2lybHxkb2RpfHN0ZWFtLT9yaXB8c3RlYW0tP2dnfGVsYW1pZ29zfGluc2FuZXJhbXplc3xydXRvcnxhcGliYXl8MTMzN3h8cmFyYmd8cHJvd2xhcnJ8amFja2V0dHxuZXd6bmFifFxibnpifHVzZW5ldHxyZXBhY2t8d2FyZXp8aWdnLWdhbWVzfGdvZy1nYW1lc3xjb2RleHxza2lkcm93fCIKICAgIHIiZW1wcmVzc3x0ZW5va2V8cmF6b3IxOTExfGVtdWRlY2t8XC5uc3BcYnxcLnhjaVxi"
).decode(), re.I)
ALLOWED = {"CONTRIBUTING.md"}
# Keys, tokens and personal machine paths. Placeholders such as /home/deck stay allowed.
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{20,}"
                    r"|\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}|\bxox[abps]-[A-Za-z0-9-]{10,}|/Users/(?!Shared\b)[A-Za-z]"
                    r"|[A-Z]:\\Users\\(?!Public\b)\w|/home/(?!deck\b|user\b|runner\b)[a-z]")
BINARY = (".zip", ".woff2", ".png", ".jpg", ".ico", ".webp")

files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True,
                       check=True).stdout.split("\n")
hits = []
for name in filter(None, files):
    p = ROOT / name
    if name in ALLOWED or name.endswith(BINARY) or not p.is_file():
        continue
    for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
        m = FORBIDDEN.search(line) or SECRET.search(line)
        if m:
            hits.append(f"{name}:{n}: {m.group(0)!r}: {line.strip()[:120]}")
for name in filter(None, files):          # the built plugin Decky downloads, too
    if name.endswith(".zip"):
        with zipfile.ZipFile(ROOT / name) as z:
            for inner in z.namelist():
                text = z.read(inner).decode(errors="replace") if not inner.endswith("/") else ""
                m = FORBIDDEN.search(text) or SECRET.search(text)
                if m:
                    hits.append(f"{name}!{inner}: {m.group(0)!r}")
if hits:
    print("\n".join(hits[:40]))
    sys.exit(f"{len(hits)} line(s) name piracy sources, the old project, a key or a personal path")
print(f"[ok] clean tree: {len([f for f in files if f])} files, no piracy sources, old project names, keys or personal paths")
