"""doctor.py with the outside world faked: one healthy Deck, then each thing it should catch."""
import hashlib, importlib, io, json, os, sys, tempfile
from contextlib import redirect_stdout
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
home = Path(tempfile.mkdtemp()); os.environ["HOME"] = str(home)
sys.path.insert(0, str(ROOT / "daemon"))
doctor = importlib.import_module("doctor")
CONF = home / ".config/deckstream"; DATA = home / ".local/share/deckstream"
SECRET, KEY = "queue-secret-123456", "storage-key-abcdef789"
STORAGE_CONF = f"STORAGE_KEY={KEY}\nSTORAGE_WEBDAV_URL=http://nas.lan:8781\nSTORAGE_WEBDAV_USER=deckstream\n"

def sha(b): return hashlib.sha256(b).hexdigest()

def setup(files_ok=True):
    CONF.mkdir(parents=True, exist_ok=True); DATA.mkdir(parents=True, exist_ok=True)
    (CONF / "daemon_config.json").write_text(json.dumps({"server_url": "https://deckstream.example", "device_id": "deck-main", "queue_secret": SECRET}))
    (CONF / "storage.conf").write_text(STORAGE_CONF)
    files = {"deckstream-daemon.py": b"print(1)\n", "saves.py": b"print(2)\n"}
    for n, b in files.items(): (CONF / n).write_bytes(b)
    (CONF / "manifest.json").write_text(json.dumps({"version": "v1", "files": {n: {"sha256": sha(b)} for n, b in files.items()}}))
    ge = home / ".steam/root/compatibilitytools.d/GE-Proton11-7"; ge.mkdir(parents=True, exist_ok=True)
    (ge / "proton").write_text(""); (ge / "toolmanifest.vdf").write_text("")

def fake(**o):
    """Install fakes; overrides: dns_fail=set, queue=status, skew=seconds, storage=status, rclone, cards, free, active, journal, remote_ver"""
    dns_fail = o.get("dns_fail", set())
    doctor.resolve = lambda host, timeout=4: host not in dns_fail
    def http(url, headers=None, timeout=8, method="GET"):
        if "/api/queue" in url:
            assert headers and headers.get("x-deckstream-secret") == SECRET
            return o.get("queue", 200), {"Date": formatdate(doctor.now() + o.get("skew", 0), usegmt=True)}, b"{}"
        if url.startswith("http://nas.lan:8781"):
            import base64
            assert method == "PROPFIND" and headers["Authorization"] == "Basic " + base64.b64encode(f"deckstream:{KEY}".encode()).decode()
            return o.get("storage", 207), {}, b""
        if url.endswith("/daemon/manifest.json"):
            return 200, {}, json.dumps({"version": o.get("remote_ver", "v1")}).encode()
        raise AssertionError(url)
    doctor.http = http
    def run(argv, timeout=8):
        a = " ".join(argv)
        if a.startswith("systemctl --user is-active deckstream-saves.timer"): return 0, "active\n"
        if a.startswith("systemctl --user is-active deckstream"): return 0, o.get("active", "active") + "\n"
        if argv[0] == "journalctl": return 0, o.get("journal", "ok line\nanother line\n")
        if "rclone" in argv[0] and argv[1] == "version": return 0, o.get("rclone_out", "rclone v1.75.1\n- os/version: steamos\n")
        if argv[0] == "iw" and argv[1] == "dev" and len(argv) == 2: return 0, "phy#0\n\tInterface wlan0\n"
        if argv[0] == "iw": return 0, "Power save: on\n"
        if argv[0] == "steamos-readonly": return 0, "enabled\n"
        if argv[0] == "pactl" and argv[1] == "get-default-sink": return 0, o.get("sink", "alsa_output.pci-0000_04_00.5-platform-acp5x_mach.0.HiFi__Speaker__sink") + "\n"
        return 0, ""
    doctor.run_cmd = run
    have = {"rclone": "/home/deck/.local/bin/rclone", "ludusavi": "/home/deck/.local/bin/ludusavi"}
    for gone in o.get("missing", []): have.pop(gone, None)
    doctor.which = lambda n: have.get(n)
    doctor.fuse_device_ready = lambda: True
    doctor.shutil.which = lambda n: "/usr/bin/" + n
    doctor.read_mounts = lambda: [f"/dev/mmcblk0p1 /run/media/deck/CARD {fs} rw 0 0" for fs in o.get("cards", ["ext4"])]
    doctor.disk_free_gb = lambda p: o.get("free", 50.0)

def res(**o):
    fake(**o); return {r.title: r for r in doctor.run_checks()}

setup()
# 1. healthy
r = res(); bad = {t: x.status for t, x in r.items() if x.status in ("fail", "warn")}
assert not bad, bad
buf = io.StringIO()
with redirect_stdout(buf): rc = doctor.main(["--brief"])
assert rc == 0 and "Everything checks out" in buf.getvalue()
print("[ok] a healthy Deck: no failures, no warnings, exit code 0")
w = r["Wi-Fi power saving"]
assert w.status == "info" and "Developer > Wi-Fi power management" in w.fix and "iw dev" not in w.fix, w.fix
print("[ok] Wi-Fi power saving on: points at the Deck's Developer toggle (survives reboots), not a command that resets")

# 2. DNS: optional hosts warn (with the fix), required hosts fail
r = res(dns_fail={"github.com", "repo.steampowered.com"})
assert r["DNS: github.com"].status == "warn" and "nmcli" in r["DNS: github.com"].fix and "mirror" in r["DNS: github.com"].detail
assert r["DNS: repo.steampowered.com"].status == "warn" and "NOT mirrored" in r["DNS: repo.steampowered.com"].detail
r = res(dns_fail={"nas.lan"}); assert r["DNS: nas.lan"].status == "fail"
print("[ok] DNS trouble is named host by host, with the exact nmcli fix; the storage server failing is a failure")

# 3. server / password / clock
assert res(queue=401)["Queue password"].status == "fail"
assert res(queue=503)["DeckStream server"].status == "warn"
r = res(skew=3600); assert r["Clock"].status == "fail"
print("[ok] wrong queue password, a server with no password set, and a badly wrong clock are each reported")

# 4. storage server
assert res(storage=401)["Storage server"].status == "fail"
(CONF / "storage.conf").unlink(); assert res()["Storage server"].status == "warn"; (CONF / "storage.conf").write_text(STORAGE_CONF)
print("[ok] a rejected key or a missing storage server is reported")

# 5. rclone, SD card, disk, daemon, journal
assert res(rclone_out="rclone v1.60.0\n")["rclone"].status == "warn"
assert res(missing=["rclone"])["rclone"].status == "warn"
assert res(cards=["exfat"])["SD card CARD"].status == "fail"
assert res(free=0.5)["Internal drive space"].status == "fail" and res(free=2.0)["Internal drive space"].status == "warn"
assert res(active="inactive")["DeckStream daemon"].status == "fail"
j = res(journal="x\nDaemon loop error: boom\n")["Recent daemon errors"]; assert j.status == "warn" and "boom" in j.detail
print("[ok] old/missing rclone, a wrong-format SD card, low space, a stopped daemon and recent errors are caught")

# 6. installed files vs manifest, update available
(CONF / "saves.py").write_bytes(b"tampered\n")
r = res(); assert r["Installed files"].status == "warn" and "saves.py (changed)" in r["Installed files"].detail
(CONF / "saves.py").write_bytes(b"print(2)\n")
(CONF / "saves.py").unlink(); assert "saves.py (missing)" in res()["Installed files"].detail; (CONF / "saves.py").write_bytes(b"print(2)\n")
r = res(remote_ver="v2"); assert r["Update available"].status == "warn"
print("[ok] changed or missing files and a newer version on the server are reported")

# 6b. audio: a streaming sink as the default output explains "no game sound"
assert res()["Audio output"].status == "ok"
r = res(sink="steam-streaming-playback")["Audio output"]
assert r.status == "warn" and "Remote Play" in r.fix and "set-default-sink" in r.fix, r
print("[ok] a Steam streaming sink as the default audio output is flagged, with the fix")

# 6c. stale stream mounts are flagged with the fix
doctor.stale_stream_mounts = lambda: ["cyberpunk_2077_pc___gog_rip"]
r = res()["Stale stream mounts"]
assert r.status == "warn" and "fusermount3 -uz" in r.fix and "cyberpunk" in r.detail, r
doctor.stale_stream_mounts = lambda: []
assert "Stale stream mounts" not in res()
print("[ok] a stale stream mount is flagged; none means no line")

# 7. a check that crashes must not hide the others
doctor.check_proton = lambda: 1 / 0
r = {x.title: x for x in doctor.run_checks()}
assert any(x.status == "warn" and "the check itself failed" in x.detail for x in r.values()) and "Python" in r
importlib.reload(doctor)
print("[ok] a crashing check is reported as such and the rest still run")

# 8. bundle + json never contain a secret
fake(); 
buf = io.StringIO()
with redirect_stdout(buf): doctor.main(["--bundle"])
path = Path(buf.getvalue().split("Wrote ")[1].split(" (")[0])
text = path.read_text()
assert SECRET not in text and KEY not in text and (path.stat().st_mode & 0o777) == 0o600
buf = io.StringIO()
with redirect_stdout(buf): doctor.main(["--json"])
assert json.loads(buf.getvalue()) and SECRET not in buf.getvalue() and KEY not in buf.getvalue()
assert doctor.redact(f"https://x/y?token={KEY}&a=1 Authorization: Bearer {KEY}") .count(KEY) == 0
print("[ok] reports, JSON and the shareable bundle contain neither the queue password nor the storage key (bundle is private, 600)")
print("\nDOCTOR TESTS PASSED")
