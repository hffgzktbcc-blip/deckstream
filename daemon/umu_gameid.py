#!/usr/bin/env python3
"""Find umu's GAMEID for an installed or streamed game, so umu can apply its per-game fixes.

umu-launcher only applies a game's fixes when GAMEID is "umu-<id>"; without one it uses "umu-default" and no fixes.
GE-Proton ships umu's database (protonfixes/umu-database.csv: TITLE, STORE, CODENAME, UMU_ID). This looks a game up,
in order of reliability:
  1. GOG: a goggame-<id>.info file next to the game names its GOG id, which is the CODENAME of the "gog" rows
  2. an exact title match (case, punctuation and years ignored) on the title given and on the game's folder names
A match is used only when every matching row names the SAME umu id; anything ambiguous or unknown gives no answer
(the caller keeps umu-default). Nothing is ever guessed.

Usage: umu_gameid.py --exe /path/Game.exe [--title "Game"] [--database /path/umu-database.csv]
Prints "umu-<id>" or nothing; exit code 0 either way.
"""
import argparse
import csv
import glob
import os
import re
import sys


def normalise(text):
    text = re.sub(r"\(\s*(19|20)\d\d\s*\)", " ", text or "")        # "(2022)"
    text = re.sub(r"[^a-z0-9]+", "", text.lower())
    return text


def find_database(explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    roots = [os.path.expanduser("~/.steam/root/compatibilitytools.d"),
             os.path.expanduser("~/.local/share/Steam/compatibilitytools.d")]
    best = None
    for root in roots:
        for p in sorted(glob.glob(os.path.join(root, "GE-Proton*", "protonfixes", "umu-database.csv"))):
            best = p                                              # sorted: the last (newest name) wins
    return best


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            rows.append({k: (v or "").strip() for k, v in r.items() if k})
    return rows


def gog_ids(exe, levels=5):
    """GOG game ids from goggame-<id>.info files in the exe's folder and its parents."""
    ids, d = [], os.path.dirname(os.path.abspath(exe))
    for _ in range(levels):
        for p in glob.glob(os.path.join(d, "goggame-*.info")):
            m = re.match(r"goggame-(\d+)\.info$", os.path.basename(p))
            if m:
                ids.append(m.group(1))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ids


def steam_ids(exe, levels=5):
    """Steam app ids from a steam_appid.txt in the exe's folder and its parents (Steam Rips ship one)."""
    ids, d = [], os.path.dirname(os.path.abspath(exe))
    for _ in range(levels):
        p = os.path.join(d, "steam_appid.txt")
        try:
            with open(p, errors="replace") as f:
                m = re.match(r"\s*(\d{1,10})\s*$", f.readline())
            if m:
                ids.append(m.group(1))
        except OSError:
            pass
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ids


GENERIC_FOLDERS = {"bin", "bin32", "bin64", "x64", "x86", "win64", "win32", "binaries", "game", "games", "content",
                   "shipping", "retail", "release", "data", "engine", "app", "apps", "common", "steamapps", "steam",
                   "gog", "gog games", "installed"}


def candidate_titles(exe, title, levels=3):
    out = [title] if title else []
    d = os.path.dirname(os.path.abspath(exe))
    for _ in range(levels):
        name = os.path.basename(d)
        if name.lower() not in GENERIC_FOLDERS:
            out.append(name)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return [t for t in out if t]


def lookup(exe, title, rows):
    """Returns "umu-<id>" or None."""
    gog = set(gog_ids(exe))
    if gog:
        found = {r["UMU_ID"] for r in rows if r.get("STORE", "").lower() == "gog" and r.get("CODENAME") in gog and r.get("UMU_ID")}
        if len(found) == 1:
            return found.pop()
    steam = set(steam_ids(exe))
    if steam:   # same id or nothing: only a row whose CODENAME is that Steam app id, and only if exactly one umu id fits
        found = {r["UMU_ID"] for r in rows if r.get("STORE", "").lower() == "steam" and r.get("CODENAME") in steam and r.get("UMU_ID")}
        if len(found) == 1:
            return found.pop()
    by_title = {}
    for r in rows:
        n = normalise(r.get("TITLE", ""))
        if n and r.get("UMU_ID"):
            by_title.setdefault(n, set()).add(r["UMU_ID"])
    for cand in candidate_titles(exe, title):
        ids = by_title.get(normalise(cand))
        if ids and len(ids) == 1:
            return next(iter(ids))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--exe", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--database")
    a = ap.parse_args(argv)
    db = find_database(a.database)
    if not db:
        return 0
    try:
        found = lookup(a.exe, a.title, load(db))
    except OSError:
        return 0
    if found:
        print(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
