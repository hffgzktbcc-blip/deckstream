"""language_fix.py: English overrides for releases that set their language in a file, wired into the stream runner."""
import importlib.util, io, json, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lf", ROOT / "daemon" / "language_fix.py")
lf = importlib.util.module_from_spec(spec); spec.loader.exec_module(lf)

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    game, writes = tmp / "games" / "new_slug", tmp / "writes" / "new_slug"
    (game / "Game Dir" / "bin" / "x64").mkdir(parents=True)
    gog = {"gameId": "1", "language": "Russian", "languages": ["ru-RU"], "name": "Some Game", "playTasks": [{"path": "bin/x64/g.exe"}]}
    (game / "Game Dir" / "goggame-1.info").write_text(json.dumps(gog))
    (game / "Game Dir" / "bin" / "x64" / "g.exe").write_bytes(b"MZ")
    (game / "Game Dir" / "goggame-2.info").write_text(json.dumps({"gameId": "2", "language": "English", "languages": ["en-US"]}))
    (game / "Game Dir" / "goggame-3.info").write_text("")                      # unreadable over the mount: skipped
    (game / "steam_settings").mkdir()
    (game / "steam_settings" / "force_language.txt").write_text("russian")
    (game / "steam_settings" / "configs.user.ini").write_text("[user::general]\naccount_name=x\nlanguage=german\n")
    deep = game / "a" / "b" / "c" / "d"; deep.mkdir(parents=True)
    (deep / "goggame-9.info").write_text(json.dumps({"language": "Polish"}))  # too deep: not walked

    # 1. a new game id: every non-English file gets an English copy; nothing else, cloud files untouched
    got = sorted(lf.run(str(game), str(writes), depth=4, out=io.StringIO()))
    want = sorted(["Game Dir/goggame-1.info", "steam_settings/force_language.txt", "steam_settings/configs.user.ini"])
    assert got == want, got
    fixed = json.loads((writes / "Game Dir" / "goggame-1.info").read_text())
    assert fixed["language"] == "English" and fixed["languages"] == ["en-US"], fixed
    assert fixed["playTasks"] == gog["playTasks"] and fixed["gameId"] == "1", "everything but the language is kept"
    assert json.loads((game / "Game Dir" / "goggame-1.info").read_text())["language"] == "Russian", "cloud file unchanged"
    assert (writes / "steam_settings" / "force_language.txt").read_text() == "english\n"
    ini = (writes / "steam_settings" / "configs.user.ini").read_text()
    assert "language=english" in ini and "account_name=x" in ini, ini
    assert not list(writes.rglob("*.tmp"))
    print("[ok] a new game id gets English copies of goggame .info and Goldberg language files")

    # 2. a file already in the write layer (the user's or the game's) is never replaced; a second run writes nothing
    (writes / "Game Dir" / "goggame-1.info").write_text('{"language": "French"}')
    assert lf.run(str(game), str(writes), out=io.StringIO()) == []
    assert "French" in (writes / "Game Dir" / "goggame-1.info").read_text()
    print("[ok] existing write-layer files are kept, repeat runs are no-ops")

    # 3. the CLI: a missing game folder is not an error
    r = subprocess.run([sys.executable, "-I", str(ROOT / "daemon/language_fix.py"), "--game", str(tmp / "nope"), "--writes", str(tmp / "w")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r
    print("[ok] CLI exits 0 without a game folder")

# 4. wiring: the runner calls it before mounting the write layer, bounded and switchable; the manifest ships it
runner = (ROOT / "daemon" / "stream-runner.sh").read_text()
call = runner.index("language_fix.py\" --game \"$GAME_DIR\" --writes \"$WRITE_DIR\"")
assert call < runner.index("OVERLAY_ERR=$(fuse-overlayfs"), "English copies must be written before the overlay mounts"
assert "DECKSTREAM_ENGLISH:-1" in runner and "bounded 30 python3 \"$HOME/.config/deckstream/language_fix.py\"" in runner
assert '"language_fix.py"' in (ROOT / "scripts" / "make_manifest.py").read_text()
print("[ok] stream runner writes English copies before the overlay, with a timeout and DECKSTREAM_ENGLISH=0 to turn off")
