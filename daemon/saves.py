#!/usr/bin/env python3
"""DeckStream saves: back up and restore game saves with Ludusavi. Everything stays on the Deck.

  saves.py register --slug S --title "Game name" --prefix DIR [--kind installed|stream]
  saves.py backup [SLUG | --all]
  saves.py restore SLUG [--yes]        (without --yes it only shows what would be restored)
  saves.py title SLUG "Official title" (tell Ludusavi what the game is really called)
  saves.py status [--json]

Backups go to the INTERNAL drive (~/.local/share/deckstream/saves-backup) so that a dead SD card, where
the games and their Proton prefixes live, cannot take the backups with it.

Limits worth knowing:
  * Ludusavi must know the game by its official title. Folder names are messy, so several cleaned-up
    candidates are tried; `title` pins the right one. Games with no match are reported, never skipped silently.
  * Ludusavi does not back up registry-based saves from a custom Wine prefix, so user.reg is copied as well.
  * `ludusavi restore` has no --wine-prefix option: it restores to the original paths. Keep the prefix
    folder where it was (DeckStream names prefixes consistently) or add a redirect in Ludusavi's config.
Exit codes: 0 ok, 1 some games failed, 2 usage, 3 Ludusavi not installed.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
CONF = HOME / ".config" / "deckstream"
DATA = HOME / ".local" / "share" / "deckstream"
GAMES_FILE = CONF / "games.json"
TITLES_FILE = CONF / "ludusavi_titles.json"
STATUS_FILE = DATA / "saves-status.json"
LOG_FILE = DATA / "logs" / "saves.log"
LOCK_FILE = DATA / "saves.lock"
FULL_LIMIT = int(os.environ.get("DECKSTREAM_SAVES_FULL_LIMIT", "5"))
EXTRAS_KEEP = 3
WARN_MB = 500
MIN_FREE_MB = 200


def saves_dir():
    return Path(os.environ.get("DECKSTREAM_SAVES_DIR") or (DATA / "saves-backup"))


# ---------------------------------------------------------------- small helpers
def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def say(msg):
    print(msg, flush=True)
    log(msg)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def slugify(name):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name)).lower()


def find_ludusavi():
    for cand in (os.environ.get("LUDUSAVI"), shutil.which("ludusavi"), str(HOME / ".local" / "bin" / "ludusavi")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ---------------------------------------------------------------- naming
_EDITION = re.compile(
    r"\b(director'?s?\s+cut|deluxe\s+edition|digital\s+deluxe(\s+edition)?|ultimate\s+edition|complete\s+edition|"
    r"game\s+of\s+the\s+year(\s+edition)?|goty|definitive\s+edition|gold\s+edition|premium\s+edition|"
    r"collector'?s\s+edition|legacy\s+edition|special\s+edition|standard\s+edition)\b", re.I)
_NOISE = re.compile(
    r"\b(installer|archive|setup|vfs|package|bundle|standalone|portable|direct\s+play|fixed|pc)\b", re.I)


def clean_title(name):
    s = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", str(name))          # [tags] and (years)
    s = re.sub(r"\bv?\d+(\.\d+)+\b", " ", s)                     # version numbers (before dots become spaces)
    s = s.replace(".", " ").replace("_", " ").replace("|", " ")
    # trailing -GROUP, but only group-looking ones (ALLCAPS or MiXeD), so "Spider-Man" survives
    s = re.sub(r"-\s*([A-Z0-9]{2,}|[A-Za-z]*[a-z][A-Z][A-Za-z]*)\s*$", " ", s)
    s = _NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def title_candidates(name, override=None):
    out = []

    def add(t):
        t = (t or "").strip()
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)

    add(override)
    full = clean_title(name)
    add(full)
    add(re.sub(r"\s+", " ", _EDITION.sub(" ", full)).strip(" -"))
    return out


# ---------------------------------------------------------------- registry of games
def register(slug, title, prefix, kind="installed"):
    games = load_json(GAMES_FILE, {})
    games[slug] = {"title": title, "prefix": str(prefix), "kind": kind, "registered": int(time.time())}
    save_json(GAMES_FILE, games)


def wine_prefix(prefix):
    """The folder that contains drive_c: Proton keeps it in <compat>/pfx, umu/plain Wine use the prefix itself."""
    p = Path(prefix)
    if (p / "pfx" / "drive_c").is_dir():
        return p / "pfx"
    if (p / "drive_c").is_dir():
        return p
    return None


# ---------------------------------------------------------------- ludusavi
STREAM_GAMES_DIR = Path.home() / ".local" / "share" / "deckstream" / "stream" / "games"


def clear_stale_stream_mounts(base=None, listdir=os.listdir, run=subprocess.run):
    """A dead rclone stream mount (rclone was killed with the game) makes Ludusavi panic with 'Transport endpoint
    is not connected' when it walks the Wine prefix (dosdevices z: points at /). Lazily unmount any such mount.
    The check LISTS the folder: stat() still works on a dead mount because rclone caches attributes for 24 h."""
    import errno
    base = Path(base) if base else STREAM_GAMES_DIR
    cleared = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return cleared
    for d in entries:
        try:
            listdir(d)
        except OSError as e:
            if getattr(e, "errno", None) != errno.ENOTCONN:
                continue
            for tool in ("fusermount3", "fusermount"):
                try:
                    if run([tool, "-uz", str(d)], capture_output=True, timeout=10).returncode == 0:
                        cleared.append(d)
                        break
                except (OSError, subprocess.SubprocessError):
                    continue
    return cleared


def run_ludusavi(lud, args, timeout=600):
    def once():
        # Inside a Steam launch LD_PRELOAD holds Steam's 32-bit overlay library; Ludusavi is 64-bit, so the
        # loader prints an error line that used to be mistaken for a failed backup.
        env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        r = subprocess.run([lud, *args], capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    try:
        rc, out, err = once()
        if rc == 101 and "not connected" in err.lower() and clear_stale_stream_mounts():
            rc, out, err = once()                 # a dead stream mount was in the way; retry once after clearing it
        return rc, out, err
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as e:
        return 127, "", str(e)


def parse_game_result(stdout):
    """(file_count, total_bytes) summed over the games in Ludusavi's --api JSON; tolerant of shape changes."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return 0, 0
    files = total = 0
    for g in (data.get("games") or {}).values():
        for info in (g.get("files") or {}).values():
            if isinstance(info, dict) and info.get("ignored"):
                continue
            files += 1
            total += int((info or {}).get("bytes", 0)) if isinstance(info, dict) else 0
    return files, total


def copy_extras(slug, wp):
    """Registry-based saves live in user.reg; Ludusavi skips them for custom prefixes, so keep a copy."""
    src = Path(wp) / "user.reg"
    if not src.is_file():
        return
    dest = saves_dir() / "_extras" / slug
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    shutil.copy2(src, dest / f"user.reg.{time.strftime('%Y%m%d-%H%M%S')}")
    old = sorted(dest.glob("user.reg.*"))[:-EXTRAS_KEEP]
    for f in old:
        f.unlink(missing_ok=True)


WRITES_DIR = DATA / "stream" / "writes"
WRITES_SKIP_EXT = (".dxvk-cache", ".foz", ".vkd3d-proton.cache", ".cache", ".log", ".tmp", ".dmp", ".mdmp")
WRITES_FILE_MAX = 64 * 1048576       # a single file bigger than this is not a save (logs, caches, crash dumps)
WRITES_TOTAL_MAX = 512 * 1048576     # never let one game's snapshots fill the internal drive


def copy_write_layer(slug, writes_root=None):
    """Streamed games run from a read-only mount with a local write layer (stream/writes/<slug>). A game that saves
    into its OWN folder (common for older games and many releases) writes there, where Ludusavi never looks. Keep
    timestamped snapshots of the small files in it (shader caches, logs and big files are skipped), newest EXTRAS_KEEP.
    Restore by hand: copy a snapshot's files back into stream/writes/<slug>/ while the game is not running.
    Returns the number of files copied (0 = nothing to keep)."""
    src = Path(writes_root or WRITES_DIR) / slug
    if not src.is_dir():
        return 0
    picked, total = [], 0
    for root, _dirs, files in os.walk(src):
        for name in files:
            p = Path(root) / name
            try:
                st = p.lstat()
            except OSError:
                continue
            # overlayfs whiteouts are character devices, and links could point anywhere: regular files only
            if not os.path.isfile(p) or p.is_symlink() or name.lower().endswith(WRITES_SKIP_EXT) or st.st_size > WRITES_FILE_MAX:
                continue
            if "shadercache" in str(p).lower() or "shader_cache" in str(p).lower():
                continue
            picked.append((p, st.st_size))
            total += st.st_size
    if not picked:
        return 0
    if total > WRITES_TOTAL_MAX:
        log(f"{slug}: write layer has {total // 1048576} MB of small files; too big to snapshot, skipped")
        return 0
    base = saves_dir() / "_extras" / slug
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    dest = base / f"writes.{time.strftime('%Y%m%d-%H%M%S')}"
    tmp = base / (dest.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    for p, _size in picked:
        target = tmp / p.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    tmp.rename(dest)                     # a snapshot is either complete or absent
    for old in sorted(base.glob("writes.*"))[:-EXTRAS_KEEP]:
        if not old.name.endswith(".partial"):
            shutil.rmtree(old, ignore_errors=True)
    return len(picked)


def backup_one(lud, slug, entry, overrides):
    title0 = entry.get("title") or slug
    wp = wine_prefix(entry.get("prefix", ""))
    rec = {"time": int(time.time()), "title": title0, "tried": [], "files": 0, "bytes": 0}
    if wp is None:
        rec.update(status="prefix-missing", message=f"No Wine prefix found at {entry.get('prefix')} (game not run yet?)")
        return rec
    sd = saves_dir()
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    for cand in title_candidates(title0, overrides.get(slug)):
        rec["tried"].append(cand)
        rc, out, err = run_ludusavi(lud, ["backup", cand, "--wine-prefix", str(wp), "--force",
                                          "--path", str(sd), "--full-limit", str(FULL_LIMIT), "--api"])
        if rc not in (0, 1):   # 1 can mean "some games failed" for a multi-game run; hard errors are >1
            rec.update(status="error", message=f"ludusavi exit {rc}: {(err or out).strip()[:200]}")
            return rec
        files, size = parse_game_result(out)
        if files:
            copy_extras(slug, wp)
            copy_write_layer(slug)
            overrides[slug] = cand      # remember the title that worked
            rec.update(status="backed-up", title=cand, files=files, bytes=size, message="ok")
            return rec
    copy_extras(slug, wp)
    if copy_write_layer(slug):
        rec.update(status="game-folder-only", files=0, bytes=0,
                   message="Ludusavi found no saves for: " + "; ".join(rec["tried"]) + "; the files the game wrote into its "
                           "own folder were kept (_extras/" + slug + "/writes.*)")
        return rec
    if not user_files_written(wp):
        rec.update(status="no-saves-yet",
                   message="The game has not written any files to its user folders yet (not played far enough to save).")
        return rec
    rec.update(status="no-saves-found",
               message="Ludusavi found no saves for: " + "; ".join(rec["tried"]) +
                       ". If the game has saves, run: saves.py title " + slug + ' "Official title"')
    return rec


USER_SAVE_DIRS = ("Documents", "Saved Games", "AppData/Roaming", "AppData/Local", "AppData/LocalLow")
USER_SKIP = ("AppData/Local/Temp", "AppData/Roaming/Microsoft", "AppData/Local/Microsoft", "AppData/Local/D3DSCache")


def user_files_written(wp):
    """True if anything under the prefix user's save-like folders was written after the prefix was set up. A game
    launched once that never reached a save writes nothing there, so "no saves found" is then expected, not a title
    problem."""
    users = Path(wp) / "drive_c" / "users"
    # Proton writes tracked_files / version when it finishes setting the prefix up; files older than that are its own.
    marker = next((m for m in (Path(wp).parent / "tracked_files", Path(wp) / "tracked_files", Path(wp).parent / "version")
                   if m.exists()), None)
    since = marker.stat().st_mtime + 5 if marker else 0
    for user in (users.iterdir() if users.is_dir() else []):
        if user.name == "Public" or not user.is_dir():
            continue
        for sub in USER_SAVE_DIRS:
            top = user / sub
            for root, dirs, files in os.walk(top):
                rel = Path(root).relative_to(user).as_posix()
                if any(rel == s or rel.startswith(s + "/") for s in USER_SKIP):
                    dirs[:] = []
                    continue
                for f in files:
                    try:
                        if os.lstat(os.path.join(root, f)).st_mtime > since:
                            return True
                    except OSError:
                        pass
    return False


def dir_size_mb(path):
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1048576


def cmd_backup(args):
    lud = find_ludusavi()
    if not lud:
        say("Ludusavi is not installed (expected in ~/.local/bin). Nothing backed up.")
        return 3
    games = load_json(GAMES_FILE, {})
    wanted = list(games) if args.all or not args.slug else [args.slug]
    if args.slug and args.slug not in games:
        say(f"Unknown game '{args.slug}'. Known: {', '.join(sorted(games)) or '(none)'}")
        return 2
    sd = saves_dir()
    sd.mkdir(parents=True, exist_ok=True)
    free_mb = shutil.disk_usage(sd).free / 1048576
    if free_mb < MIN_FREE_MB:
        say(f"Only {free_mb:.0f} MB free where backups are kept; skipping to avoid filling the drive.")
        return 1
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("Another backup is already running; skipping.")
        return 0
    overrides = load_json(TITLES_FILE, {})
    status = load_json(STATUS_FILE, {})
    failed = 0
    for slug in wanted:
        rec = backup_one(lud, slug, games[slug], overrides)
        status[slug] = rec
        mark = {"backed-up": "OK ", "game-folder-only": "OK~", "no-saves-found": "?? ", "no-saves-yet": "-- ", "prefix-missing": "-- "}.get(rec["status"], "!! ")
        say(f"{mark}{slug}: {rec['status']} ({rec['files']} files, {rec['bytes']} bytes) {'' if rec['status']=='backed-up' else rec['message']}")
        if rec["status"] in ("error",):
            failed += 1
    save_json(TITLES_FILE, overrides)
    save_json(STATUS_FILE, status)
    size = dir_size_mb(sd)
    if size > WARN_MB:
        say(f"Note: backups now use {size:.0f} MB. Lower DECKSTREAM_SAVES_FULL_LIMIT or clear old games.")
    return 1 if failed else 0


def restore_extras(slug, wp, writes_root=None):
    """Restore the latest user.reg and write-layer snapshot from _extras.
    Returns number of files restored.
    """
    extras_dir = saves_dir() / "_extras" / slug
    if not extras_dir.is_dir():
        return 0

    restored = 0
    # 1. Restore latest user.reg
    reg_files = sorted(extras_dir.glob("user.reg.*"))
    if reg_files:
        latest_reg = reg_files[-1]
        dest_reg = Path(wp) / "user.reg"
        shutil.copy2(latest_reg, dest_reg)
        restored += 1

    # 2. Restore latest write-layer snapshot
    write_files = sorted(extras_dir.glob("writes.*"))
    if write_files:
        latest_write = write_files[-1]
        if not latest_write.name.endswith(".partial"):
            dest_write = Path(writes_root or WRITES_DIR) / slug
            if dest_write.exists():
                shutil.rmtree(dest_write)
            shutil.copytree(latest_write, dest_write)
            restored += 1

    return restored

def cmd_restore(args):
    lud = find_ludusavi()
    if not lud:
        say("Ludusavi is not installed (expected in ~/.local/bin).")
        return 3
    games = load_json(GAMES_FILE, {})
    if args.slug not in games:
        say(f"Unknown game '{args.slug}'.")
        return 2
    overrides = load_json(TITLES_FILE, {})
    title = overrides.get(args.slug) or title_candidates(games[args.slug].get("title") or args.slug)[0]
    sd = saves_dir()
    rc, out, err = run_ludusavi(lud, ["restore", title, "--path", str(sd), "--preview", "--api"])
    files, size = parse_game_result(out)
    if rc > 1 or not files:
        say(f"No backup found for '{title}' in {sd}. {(err or '').strip()[:150]}")
        return 1
    say(f"Would restore {files} file(s), {size} bytes for '{title}'.")
    if not args.yes:
        say("Nothing changed. Run again with --yes to restore (a safety copy of what is there now is made first).")
        return 0
    safety = sd / "_pre-restore"
    safety.mkdir(parents=True, exist_ok=True)
    wp = wine_prefix(games[args.slug].get("prefix", ""))
    if wp:
        run_ludusavi(lud, ["backup", title, "--wine-prefix", str(wp), "--force", "--path", str(safety), "--full-limit", "3", "--api"])
    rc, out, err = run_ludusavi(lud, ["restore", title, "--path", str(sd), "--force", "--api"])

    # Restore extras (user.reg and write-layer snapshots)
    extras_restored = restore_extras(args.slug, wp) if wp else 0

    if rc > 1:
        say(f"Restore failed: {(err or out).strip()[:200]}")
        return 1
    say(f"Restored '{title}' and {extras_restored} extra snapshot(s). The previous state is in {safety}.")
    return 0


def cmd_register(args):
    register(args.slug or slugify(args.title), args.title, args.prefix, args.kind)
    return 0


def cmd_title(args):
    overrides = load_json(TITLES_FILE, {})
    overrides[args.slug] = args.title
    save_json(TITLES_FILE, overrides)
    say(f"{args.slug} will be backed up as '{args.title}'.")
    return 0


def cmd_status(args):
    status = load_json(STATUS_FILE, {})
    games = load_json(GAMES_FILE, {})
    if args.json:
        print(json.dumps({"games": games, "status": status, "dir": str(saves_dir())}, indent=2))
        return 0
    if not games:
        print("No games registered yet.")
        return 0
    for slug in sorted(games):
        s = status.get(slug)
        if not s:
            print(f"{slug}: never backed up")
            continue
        age = int(time.time()) - s["time"]
        ago = f"{age // 3600}h {age % 3600 // 60}m ago" if age >= 3600 else f"{age // 60}m ago"
        print(f"{slug}: {s['status']} {ago} ({s['files']} files, {s['bytes']} bytes) {'' if s['status']=='backed-up' else s['message']}")
    print(f"Backups are in {saves_dir()} ({dir_size_mb(saves_dir()):.1f} MB)" if saves_dir().exists() else "No backups yet.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup"); b.add_argument("slug", nargs="?"); b.add_argument("--all", action="store_true")
    r = sub.add_parser("restore"); r.add_argument("slug"); r.add_argument("--yes", action="store_true")
    g = sub.add_parser("register"); g.add_argument("--slug"); g.add_argument("--title", required=True)
    g.add_argument("--prefix", required=True); g.add_argument("--kind", default="installed", choices=["installed", "stream"])
    t = sub.add_parser("title"); t.add_argument("slug"); t.add_argument("title")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    return {"backup": cmd_backup, "restore": cmd_restore, "register": cmd_register,
            "title": cmd_title, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
