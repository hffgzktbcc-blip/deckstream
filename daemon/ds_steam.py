"""Steam's binary VDF format, finding a game's executable and writing its launcher scripts.

Part of the daemon: loaded by deckstream-daemon.py (see the note there)."""
import os
import zlib
import struct
import re
from pathlib import Path

from ds_core import HOME


def calculate_shortcut_id(target, app_name):
    # Standard Valve CRC calculation for shortcut AppIDs
    combined = f"{target}{app_name}".encode("utf-8")
    crc = zlib.crc32(combined)
    # Steam unsigned 32-bit ID with high bit set
    app_id = (crc | 0x80000000) & 0xFFFFFFFF
    return app_id


def parse_binary_vdf(data, strict=False):
    """
    Parses Steam binary shortcuts.vdf into a list of shortcut dictionaries.
    With strict=True anything unexpected (unknown value type, truncated data, missing header) raises
    ValueError, so a caller that is about to rewrite the file can refuse instead of losing entries.
    """
    if not data or not data.startswith(b"\x00shortcuts\x00"):
        if strict and data:
            raise ValueError("not a shortcuts.vdf file")
        return []
    
    idx = len(b"\x00shortcuts\x00")

    def read_string(offset):
        end = data.find(b"\x00", offset)
        if end == -1:
            return "", offset
        val = data[offset:end].decode("utf-8", errors="replace")
        return val, end + 1

    def read_dict(offset):
        res = {}
        while offset < len(data):
            type_byte = data[offset:offset+1]
            offset += 1
            if type_byte == b"\x08" or not type_byte:
                break
            key, offset = read_string(offset)
            if type_byte == b"\x00":
                sub, offset = read_dict(offset)
                res[key] = sub
            elif type_byte == b"\x01":
                val, offset = read_string(offset)
                res[key] = val
            elif type_byte == b"\x02":
                if strict and offset + 4 > len(data):
                    raise ValueError("truncated integer")
                val = struct.unpack("<I", data[offset:offset+4])[0]
                offset += 4
                res[key] = val
            elif strict:
                raise ValueError(f"unsupported value type {type_byte!r} for '{key}'")
        return res, offset

    try:
        root_dict, _ = read_dict(idx)
        shortcuts = []
        for k in sorted(root_dict.keys(), key=lambda x: int(x) if x.isdigit() else 9999):
            if isinstance(root_dict[k], dict):
                shortcuts.append(root_dict[k])
        return shortcuts
    except Exception as e:
        if strict:
            raise ValueError(str(e)) from e
        print(f"[!] Error parsing shortcuts.vdf: {e}")
        return []


def serialize_binary_vdf(shortcuts):
    """
    Serializes a list of shortcut dictionaries into Steam binary shortcuts.vdf.
    """
    out = bytearray()
    out.extend(b"\x00shortcuts\x00")

    for i, item in enumerate(shortcuts):
        out.extend(b"\x00")
        out.extend(str(i).encode("utf-8") + b"\x00")
        
        for k, v in item.items():
            if isinstance(v, str):
                out.extend(b"\x01")
                out.extend(k.encode("utf-8") + b"\x00")
                out.extend(v.encode("utf-8") + b"\x00")
            elif isinstance(v, int):
                out.extend(b"\x02")
                out.extend(k.encode("utf-8") + b"\x00")
                out.extend(struct.pack("<I", v & 0xFFFFFFFF))
            elif isinstance(v, dict):
                out.extend(b"\x00")
                out.extend(k.encode("utf-8") + b"\x00")
                for sub_k, sub_v in v.items():
                    out.extend(b"\x01")
                    out.extend(str(sub_k).encode("utf-8") + b"\x00")
                    out.extend(str(sub_v).encode("utf-8") + b"\x00")
                out.extend(b"\x08")
        out.extend(b"\x08")
    out.extend(b"\x08\x08")
    return bytes(out)


def find_best_executable(game_dir):
    """
    Finds the main game executable in game_dir.
    Filters out launchers, unins000, crash handlers, redists, and dependencies.
    """
    p = Path(game_dir)
    exe_candidates = []
    
    ignore_patterns = [
        "unins", "setup", "crash", "reporter", "dxwebsetup", "vcredist",
        "dotnet", "easyanticheat", "battleye", "steam_api", "unitycrash",
        "redist", "prerequisites", "install", "config", "benchmark", "patch", "handler"
    ]

    for root, _, files in os.walk(p):
        for f in files:
            if f.lower().endswith(".exe"):
                full_path = Path(root) / f
                name_lower = f.lower()
                
                # Check ignore
                if any(ign in name_lower for ign in ignore_patterns):
                    continue
                
                size = full_path.stat().st_size
                # Depth from game_dir
                rel = full_path.relative_to(p)
                depth = len(rel.parts)
                
                exe_candidates.append({
                    "path": full_path,
                    "name": f,
                    "size": size,
                    "depth": depth,
                    "name_match": 1 if p.name.lower() in name_lower else 0
                })

    if not exe_candidates:
        return None

    # Sort by: name match desc, depth asc, size desc
    exe_candidates.sort(key=lambda x: (x["name_match"], -x["depth"], x["size"]), reverse=True)
    return str(exe_candidates[0]["path"])


def find_native_start(folder):
    """A native Linux game (GOG .sh unpacked by cloud-install.sh): start.sh, else an executable ELF file near the top."""
    folder = Path(folder)
    start = folder / "start.sh"
    if start.is_file() and not start.is_symlink():
        return start
    for depth in ("*", "*/*"):
        for f in sorted(folder.glob(depth)):
            try:
                if f.is_file() and not f.is_symlink() and os.access(f, os.X_OK):
                    with open(f, "rb") as fh:
                        if fh.read(4) == b"\x7fELF":
                            return f
            except OSError:
                continue
    return None


def make_native_runner(game_name, start_path):
    """Shortcut script for a native Linux game: no Proton prefix, no umu, no game-launcher.sh. Runs from its own folder."""
    runners_dir = HOME / ".local" / "share" / "deckstream" / "runners"
    runners_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", game_name).lower().strip("_")[:80] or "game"
    runner = runners_dir / f"native_{slug}.sh"
    start_path = Path(start_path)

    def q(x):
        return "'" + str(x).replace("'", "'\"'\"'") + "'"

    runner.write_text(f"#!/usr/bin/env bash\ncd {q(start_path.parent)} || exit 1\nexec {q(start_path)} \"$@\"\n")
    runner.chmod(0o755)
    return str(runner)


def ensure_numeric_path(path, slug):
    """GE-Proton crashes if the prefix path has no digits at all (it reads the game id from it).
    Existing prefixes that already contain a digit keep their name so saves are not orphaned."""
    path = Path(path)
    if re.search(r"\d", str(path)):
        return path
    return path.with_name(f"{path.name}_{zlib.crc32(slug.encode()) & 0xFFFFFFFF}")


def prefix_dir_for(exe_path, slug):
    """Where the game's Proton prefix lives: on the same SD card as the game when that card
    supports symlinks (ext4), otherwise on the internal drive."""
    exe = Path(exe_path)
    parts = exe.parts
    if len(parts) > 4 and parts[1] == "run" and parts[2] == "media":
        base = Path(*parts[:5]) / ".deckstream" / "prefixes"
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / f".symlink_probe_{os.getpid()}"
            os.symlink("probe", probe)
            probe.unlink()
            return ensure_numeric_path(base / slug, slug)
        except OSError:
            pass
    return ensure_numeric_path(HOME / ".local" / "share" / "deckstream" / "prefixes" / slug, slug)


def make_game_launcher(game_name, exe_path):
    """Windows games need Proton, and a plain non-Steam shortcut to an .exe has no compatibility tool
    (Steam then tries to run it natively and nothing happens). Point the shortcut at a tiny script
    that runs the game through game-launcher.sh instead. Returns the script path."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", game_name).lower()[:80] or "game"
    launcher_dir = HOME / ".local" / "share" / "deckstream" / "launchers"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    script = launcher_dir / f"play_{slug}.sh"
    helper = HOME / ".config" / "deckstream" / "game-launcher.sh"

    def q(s):
        return "'" + str(s).replace("'", "'\"'\"'") + "'"

    with open(script, "w") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write(f"exec bash {q(helper)} {q(exe_path)} {q(prefix_dir_for(exe_path, slug))} {q(slug)} {q(game_name)}\n")
    os.chmod(script, 0o755)
    return str(script)
