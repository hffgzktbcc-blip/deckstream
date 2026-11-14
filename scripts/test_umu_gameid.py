#!/usr/bin/env python3
"""umu_gameid.py: GOG id first, exact title second, never a guess."""
import importlib.util, os, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("ug", Path(__file__).parent.parent / "daemon" / "umu_gameid.py")
ug = importlib.util.module_from_spec(spec); spec.loader.exec_module(ug)

tmp = Path(tempfile.mkdtemp())
db = tmp / "umu-database.csv"
db.write_text("""TITLE,STORE,CODENAME,UMU_ID,COMMON ACRONYM (Optional),NOTE (Optional),EXE_STRINGS (Optional)
Cyberpunk 2077,egs,Ginger,umu-1091500,,,
Cyberpunk 2077,gog,1423049311,umu-1091500,,,
Sifu,egs,d36336f190094951873ed6138ac208d8,umu-2138710,,,
Prey,gog,1111,umu-480490,,,
Prey,egs,Other,umu-1000,,,
Win64,gog,2222,umu-777,,,
Ghosts Of Somewhere,gog,3333,umu-999,,,
Some Steam Game,steam,555,umu-555,,,
Twin A,steam,666,umu-1,,,
Twin B,steam,666,umu-2,,,
""")
rows = ug.load(str(db))


def game(*parts, gog=None):
    p = tmp.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    if gog:
        (p.parent.parent / f"goggame-{gog}.info").write_text("{}")
    return str(p)


# 1. GOG id from goggame-<id>.info two folders above the exe (the Cyberpunk rip layout)
exe = game("Rip.Name-GROUP", "Cyberpunk 2077", "bin", "x64", "Cyberpunk2077.exe", gog="1423049311")
(Path(exe).parents[2] / "goggame-1423049311.info").write_text("{}")
assert ug.lookup(exe, "", rows) == "umu-1091500"
print("[ok] a GOG game is found by the id in its goggame-<id>.info")

# 2. exact title via the folder name, ignoring case, punctuation and a year
exe = game("Games", "SIFU (2022)", "Sifu", "Sifu.exe")
assert ug.lookup(exe, "", rows) == "umu-2138710"
assert ug.lookup(game("x", "unknown", "g.exe"), "Sifu", rows) == "umu-2138710", "an explicit title is used too"
print("[ok] a title match ignores case, punctuation and the year")

# 3. never a guess: unknown, near-miss and ambiguous titles give nothing
assert ug.lookup(game("Games", "Ghost of Tsushima Directors Cut", "GhostOfTsushima.exe"), "", rows) is None
assert ug.lookup(game("Games", "Sifu Remastered Extra", "a.exe"), "", rows) is None
assert ug.lookup(game("Games", "Prey", "prey.exe"), "", rows) is None, "two different umu ids for one title is ambiguous"
print("[ok] unknown, near-miss and ambiguous titles give no answer")

# 4. generic folder names (Win64, bin...) are never tried as titles
assert ug.lookup(game("Misc", "Something Else", "Win64", "g.exe"), "", rows) is None
print("[ok] generic folder names like Win64 are not treated as game titles")

# 5. the database in a missing place is harmless; the command line prints the id or nothing
assert ug.find_database(str(tmp / "nope.csv")) is None
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ug.main(["--exe", game("Games", "Sifu", "Sifu.exe"), "--database", str(db)])
assert rc == 0 and buf.getvalue().strip() == "umu-2138710"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ug.main(["--exe", game("Games", "Nothing Here", "a.exe"), "--database", str(db)])
assert buf.getvalue() == ""
print("[ok] the command prints the umu id, or nothing")

# 5. Steam Rips (row 58): steam_appid.txt next to the exe (or above it) -> the `steam` row with that CODENAME. A folder
#    name that matches nothing is no obstacle; an id with no row, or two umu ids for one id, gives nothing.
exe = game("Rip-GROUP", "Obscure Folder Name", "Game", "game.exe")
(Path(exe).parent / "steam_appid.txt").write_text("555\n")
assert ug.lookup(exe, "", rows) == "umu-555"
exe = game("Rip2-GROUP", "Another Odd Name", "Game", "bin", "game.exe")
(Path(exe).parents[1] / "steam_appid.txt").write_text("555")
assert ug.lookup(exe, "", rows) == "umu-555", "found one folder up"
exe = game("Rip3-GROUP", "Odd Name Three", "Game", "game.exe")
(Path(exe).parent / "steam_appid.txt").write_text("999999\n")
assert ug.lookup(exe, "", rows) is None, "an id with no row: nothing, never a guess"
exe = game("Rip4-GROUP", "Odd Name Four", "Game", "game.exe")
(Path(exe).parent / "steam_appid.txt").write_text("666\n")
assert ug.lookup(exe, "", rows) is None, "two umu ids for one Steam id is ambiguous"
exe = game("Rip5-GROUP", "Odd Name Five", "Game", "game.exe")
(Path(exe).parent / "steam_appid.txt").write_text("not a number\n")
assert ug.lookup(exe, "", rows) is None
print("[ok] a Steam Rip is found by steam_appid.txt; unknown, ambiguous and junk ids give nothing")

print("\nUMU GAMEID TESTS PASSED")
