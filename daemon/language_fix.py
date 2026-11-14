#!/usr/bin/env python3
"""English text for streamed games, whatever language the release ships with.

Some releases pick the game's language from a small file in the game folder, not from the game's own settings:
  - GOG rips: goggame-<id>.info ("language": "Russian"), read by Galaxy64.dll at every start-up (Cyberpunk, 10 Oct).
  - Goldberg Steam emulator: steam_settings/force_language.txt, or language= in steam_settings/configs.user.ini.
The stream runner calls this before it mounts the write layer: an English copy at the same path in the game's write
folder shadows the cloud file without changing it. Every game (and every new game id for the same release) gets it,
so nothing is typed in per game. A file already in the write folder is never touched (the user or the game owns it).

Usage: language_fix.py --game <mounted game folder> --writes <write-layer folder> [--depth 4]
Prints one line per file written. Exit 0 unless the arguments are wrong. Off with DECKSTREAM_ENGLISH=0 (the runner).
"""
import argparse, json, os, re, sys

ENGLISH = {"english", "en", "en-us", "en_us", "en-gb", "en_gb"}


def _read(path, limit=1 << 20):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8-sig", "replace")
    except OSError:
        return ""


def _english(value):
    return str(value or "").strip().lower() in ENGLISH


def fix_goggame(text):
    """An English copy of a goggame-*.info, or None when it is already English or unreadable."""
    try:
        info = json.loads(text)
    except ValueError:
        return None
    if not isinstance(info, dict) or "language" not in info or _english(info.get("language")):
        return None
    info["language"] = "English"
    if "languages" in info:
        info["languages"] = ["en-US"]
    return json.dumps(info, indent=2, ensure_ascii=False) + "\n"


def fix_force_language(text):
    return None if _english(text) or not text.strip() else "english\n"


LANG_LINE = re.compile(r"^(\s*language\s*=\s*)(.*?)(\s*)$", re.I | re.M)


def fix_configs_user(text):
    m = LANG_LINE.search(text)
    if not m or _english(m.group(2)):
        return None
    return LANG_LINE.sub(lambda mm: mm.group(1) + "english" + mm.group(3), text, count=1)


def pick(name, parent):
    low = name.lower()
    if low.startswith("goggame-") and low.endswith(".info"):
        return fix_goggame
    if parent.lower() == "steam_settings":
        if low == "force_language.txt":
            return fix_force_language
        if low == "configs.user.ini":
            return fix_configs_user
    return None


def run(game, writes, depth=4, out=sys.stdout):
    written = []
    game = os.path.abspath(game)
    for root, dirs, files in os.walk(game):
        rel_root = os.path.relpath(root, game)
        level = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        if level >= depth:      # only folders above this level are looked in
            dirs[:] = []
            continue
        for name in files:
            fixer = pick(name, os.path.basename(root))
            if not fixer:
                continue
            rel = os.path.normpath(os.path.join(rel_root, name))
            if rel.startswith("..") or os.path.isabs(rel):
                continue
            dest = os.path.join(writes, rel)
            if os.path.lexists(dest):
                continue
            new = fixer(_read(os.path.join(root, name)))
            if new is None:
                continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".tmp", "w", encoding="utf-8", newline="") as fh:
                    fh.write(new)
                os.replace(dest + ".tmp", dest)
            except OSError as e:
                print(f"[lang] could not write {rel}: {e}", file=out)
                continue
            written.append(rel)
            print(f"[lang] English override written: {rel}", file=out)
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--game", required=True)
    ap.add_argument("--writes", required=True)
    ap.add_argument("--depth", type=int, default=4)
    a = ap.parse_args(argv)
    if not os.path.isdir(a.game):
        print(f"[lang] no game folder: {a.game}")
        return 0
    run(a.game, a.writes, a.depth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
