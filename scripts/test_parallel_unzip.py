"""parallel_unzip.py: identical output, unsafe entries skipped, failures reported."""
import hashlib, os, stat, subprocess, sys, tempfile, zipfile
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "daemon" / "parallel_unzip.py"
tmp = tempfile.mkdtemp()
z = os.path.join(tmp, "a.zip")
with zipfile.ZipFile(z, "w") as f:
    for i in range(24):
        f.writestr(f"game/data/f{i}.bin", os.urandom(300_000) + bytes([i]))
    f.writestr("game/Game.exe", b"MZ" + b"0" * 1000)
    f.writestr("../../escape.txt", b"bad")
    f.writestr("/abs/escape2.txt", b"bad")
    zi = zipfile.ZipInfo("game/link"); zi.external_attr = (stat.S_IFLNK | 0o777) << 16
    f.writestr(zi, "/etc/passwd")
    f.writestr(zipfile.ZipInfo("game/emptydir/"), "")

dest = os.path.join(tmp, "out")
r = subprocess.run([sys.executable, "-I", str(TOOL), z, dest, "4"], capture_output=True, text=True)
assert r.returncode == 0, r.stdout + r.stderr
names = sorted(os.path.relpath(os.path.join(d, x), dest) for d, _, fs in os.walk(dest) for x in fs)
assert len(names) == 25 and "game/Game.exe" in names, names
assert not os.path.exists(os.path.join(tmp, "escape.txt")) and not os.path.exists("/abs/escape2.txt")
assert not os.path.lexists(os.path.join(dest, "game", "link"))
assert os.path.isdir(os.path.join(dest, "game", "emptydir"))
src = zipfile.ZipFile(z)
for n in names:
    assert hashlib.md5(src.read(n)).digest() == hashlib.md5(open(os.path.join(dest, n), "rb").read()).digest(), n
print("[✓] contents identical; traversal, absolute path and symlink entries skipped")

bad = os.path.join(tmp, "bad.zip"); open(bad, "wb").write(b"not a zip")
assert subprocess.run([sys.executable, "-I", str(TOOL), bad, dest + "2"], capture_output=True).returncode != 0
print("[✓] corrupt archive exits non-zero")
# Resume: a finished file is not rewritten, a half-written one and a wrong-size one are redone, a missing one is written.
keep = os.path.join(dest, "game/data/f0.bin"); half = os.path.join(dest, "game/data/f1.bin")
wrong = os.path.join(dest, "game/data/f2.bin"); gone = os.path.join(dest, "game/data/f3.bin")
os.utime(keep, (1, 1))
open(half, "r+b").truncate(1000)
open(wrong, "ab").write(b"extra")
os.remove(gone)
r = subprocess.run([sys.executable, "-I", str(TOOL), z, dest, "4"], capture_output=True, text=True)
assert r.returncode == 0 and "Resuming: 22 files" in r.stdout, r.stdout + r.stderr
assert os.stat(keep).st_mtime == 1, "a complete file was written again"
for n in ("game/data/f1.bin", "game/data/f2.bin", "game/data/f3.bin"):
    assert src.read(n) == open(os.path.join(dest, n), "rb").read(), n
print("[✓] resume keeps complete files and rewrites only partial, wrong-size and missing ones")

# GOG Linux .sh: a shell script with a zip appended; only data/noarch/ is kept, start.sh stays runnable
sh = os.path.join(tmp, "gog_game_1_0.sh")
zbuf = os.path.join(tmp, "payload.zip")
with zipfile.ZipFile(zbuf, "w") as f:
    zi = zipfile.ZipInfo("data/noarch/start.sh"); zi.external_attr = 0o755 << 16
    f.writestr(zi, "#!/bin/bash\necho hi\n")
    zi = zipfile.ZipInfo("data/noarch/game/bin"); zi.external_attr = 0o4755 << 16
    f.writestr(zi, b"\x7fELF")
    f.writestr("data/noarch/game/data.pak", b"x" * 5000)
    f.writestr("scripts/postinst.sh", "rm -rf /\n")
with open(sh, "wb") as out:
    out.write(b"#!/bin/sh\n# makeself header\n" + b"#" * 4000 + b"\n")
    out.write(open(zbuf, "rb").read())
gdest = os.path.join(tmp, "gog")
r = subprocess.run([sys.executable, "-I", str(TOOL), sh, gdest, "2", "--strip", "data/noarch/"], capture_output=True, text=True)
assert r.returncode == 0, r.stdout + r.stderr
got = sorted(os.path.relpath(os.path.join(d, x), gdest) for d, _, fs in os.walk(gdest) for x in fs)
assert got == ["game/bin", "game/data.pak", "start.sh"], got
assert os.access(os.path.join(gdest, "start.sh"), os.X_OK)
assert not (os.stat(os.path.join(gdest, "game/bin")).st_mode & stat.S_ISUID), "setuid bit must not be kept"
print("[✓] GOG .sh payload: only data/noarch/ extracted, exec bit kept, setuid dropped")
print("\n🎉 PARALLEL UNZIP TESTS PASSED")
