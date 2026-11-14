#!/usr/bin/env python3
"""Offline reliability checks against the daemon's real parts, with isolated state."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent

with tempfile.TemporaryDirectory() as work:
    tmp = Path(work)
    with patch.dict(os.environ, {"HOME": work}):
        spec = importlib.util.spec_from_file_location("safety_daemon", ROOT / "daemon/deckstream-daemon.py")
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
    core, stream, install = (sys.modules[n] for n in ("ds_core", "ds_stream", "ds_install"))

    core.set_current_job("job_1", "Example Game")
    marker = json.loads(core.CURRENT_JOB.read_text())
    assert marker["id"] == "job_1" and marker["pid"] == os.getpid()
    core.clear_current_job()
    core.clear_current_job()
    assert not core.CURRENT_JOB.exists()
    assert not core.consume_restart_request()
    core.RESTART_MARKER.write_text("1")
    assert core.consume_restart_request() and not core.consume_restart_request()
    print("[ok] job marker lifecycle and one-shot restart request")

    # No network is contacted when a download URL fails the production policy.
    with patch.object(core.urllib.request, "urlopen", side_effect=AssertionError("network contacted")):
        try:
            install.download_file("http://127.0.0.1/file", tmp / "blocked")
            raise AssertionError("unsafe download accepted")
        except install.DOWNLOAD_FAILURES:
            pass
    assert not (tmp / "blocked").exists()
    assert core.host_matches("files.storage.example", ["storage.example"])
    assert not core.host_matches("storage.example.attacker.invalid", ["storage.example"])
    print("[ok] download policy blocks unsafe URLs before contacting the network")

    existing = tmp / "installed"
    (existing / "Saves").mkdir(parents=True)
    (existing / "Game.exe").write_bytes(b"game")
    (existing / "Saves/slot.sav").write_bytes(b"progress")
    temp = existing / "download.pkg"
    temp.write_bytes(b"partial")
    temp.with_name(temp.name + ".parts").write_text("{}")
    stream.discard_failed_download(existing, temp, True)
    assert (existing / "Game.exe").read_bytes() == b"game"
    assert (existing / "Saves/slot.sav").read_bytes() == b"progress"
    assert not temp.exists() and not temp.with_name(temp.name + ".parts").exists()
    fresh = tmp / "fresh"
    fresh.mkdir()
    stream.discard_failed_download(fresh, fresh / "download.pkg", False)
    assert not fresh.exists()
    print("[ok] failed download preserves installed games and saves; removes only its new folder")

    mounts = tmp / "mounts"
    for name in ("live", "stale", "denied"):
        (mounts / name).mkdir(parents=True)
    def probe(path):
        if path.name == "stale":
            raise OSError(errno.ENOTCONN, "disconnected")
        if path.name == "denied":
            raise OSError(errno.EACCES, "permission denied")
        return []
    calls = []
    def unmount(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    assert [p.name for p in stream.clean_stale_stream_mounts(mounts, stat=probe, run=unmount)] == ["stale"]
    assert calls == [["fusermount3", "-uz", str(mounts / "stale")]]
    print("[ok] only disconnected mounts are lazily unmounted")

    proc = tmp / "proc"
    proc.mkdir()
    def process(pid, argv):
        folder = proc / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    process(9100, ["systemd-inhibit", "bash", "startup_profile.py", "fill-cache"])
    process(9200, ["bash", "startup_profile.py", "fill-cache"])
    process(9300, ["unrelated"])
    stream.PRESTAGE_DIR.mkdir(parents=True, exist_ok=True)
    for slug, pid in (("queued", 9100), ("battery", 9200), ("reused", 9300)):
        (stream.PRESTAGE_DIR / (slug + ".pid")).write_text(str(pid))
    (stream.PRESTAGE_DIR / "queued.inhibit").write_text(json.dumps({"folder": "Example", "hot": ["Example/Game.exe"]}))
    (stream.PRESTAGE_DIR / "reused.inhibit").write_text("{}")
    assert stream.running_fills(str(proc)) == ["battery", "queued"]
    assert not (stream.PRESTAGE_DIR / "reused.inhibit").exists()
    killed = []
    kill = lambda pid, sig: killed.append((pid, sig))
    assert stream.stop_fills_off_charger(str(proc), ac=True, kill=kill) == []
    assert stream.stop_fills_off_charger(str(proc), ac=False, kill=kill) == []
    assert stream.stop_fills_off_charger(str(proc), ac=False, kill=kill) == ["queued"]
    assert killed == [(9100, signal.SIGTERM)]
    assert json.loads((stream.PRESTAGE_DIR / "queued.pending.json").read_text()) == {"folder": "Example", "hot": ["Example/Game.exe"]}
    assert (stream.PRESTAGE_DIR / "battery.pid").exists()
    print("[ok] reused PIDs cleared; charger debounce stops inhibited fill and queues it again; battery fill stays")

    # Execute the actual cold-fill block, with only the commands it invokes replaced.
    runner = (ROOT / "daemon/stream-runner.sh").read_text()
    block = runner.split("    FILL_INHIBIT=()", 1)[1].split('    mark fill_end;', 1)[0]
    snippet = tmp / "fill.sh"
    snippet.write_text('mark() { :; }; SP_PY=x.py; CACHE_DIR=c; FILL_ARGS=(--example)\nFILL_INHIBIT=()' + block + '\necho "fill_status=$FILL_RC"\n')
    mock_bin = tmp / "bin"
    mock_bin.mkdir()
    for name, body in {
        "systemd-inhibit": 'echo "inhibit:$*" >> "$FILL_LOG"\n[ "${MOCK_INHIBIT:-1}" = 1 ] || exit 1\nwhile [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"',
        "python3": 'echo "python:$*" >> "$FILL_LOG"\nexit "${MOCK_FILL_RC:-0}"',
    }.items():
        tool = mock_bin / name
        tool.write_text("#!/usr/bin/env bash\n" + body + "\n")
        tool.chmod(0o755)
    for enabled, available, rc in (("1", "1", "0"), ("0", "1", "0"), ("1", "0", "0"), ("1", "1", "7")):
        log = tmp / "fill-calls"
        log.write_text("")
        result = subprocess.run(["bash", str(snippet)], capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": str(mock_bin) + os.pathsep + os.environ["PATH"], "FILL_LOG": str(log),
                 "DECKSTREAM_FILL_INHIBIT": enabled, "MOCK_INHIBIT": available, "MOCK_FILL_RC": rc})
        calls = log.read_text()
        assert "python:x.py fill-cache --cache-dir c --example" in calls, calls
        assert ("--who=DeckStream" in calls) == (enabled == "1" and available == "1"), calls
        assert "fill_status=" + rc in result.stdout, result.stdout + result.stderr
    print("[ok] real pre-mount fill block inhibits when available, supports opt-out and keeps fill exit status")

    def script(name, text):
        path = tmp / name
        path.write_text("#!/usr/bin/env bash\n" + text)
        path.chmod(0o755)
        return path
    hung = script("hung.sh", 'sleep 301 &\necho $! > "$3/child.pid"\nwait\n')
    dest = tmp / "dest"
    dest.mkdir()
    ok, message = install.run_cloud_install(hung, "g", "Example", dest, lambda _: None,
                                            size_fn=lambda: 0, stall_seconds=0.3, tick_seconds=0.1)
    assert not ok and message.startswith("Stalled"), (ok, message)
    child = int((dest / "child.pid").read_text())
    deadline = time.monotonic() + 3
    while True:
        status = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()
        if not status or status.startswith("Z"):
            break
        assert time.monotonic() < deadline, "watchdog left a live child process"
        time.sleep(0.05)
    successful = script("ok.sh", 'echo DECKSTREAM_DONE\n')
    assert install.run_cloud_install(successful, "g", "Example", dest, lambda _: None,
                                     stall_seconds=1, tick_seconds=0.1)[0]
    failing = script("fail.sh", 'echo "DECKSTREAM_ERROR: storage unavailable"\nexit 1\n')
    assert install.run_cloud_install(failing, "g", "Example", dest, lambda _: None,
                                     stall_seconds=1, tick_seconds=0.1) == (False, "storage unavailable")
    print("[ok] stalled installer and children stopped; success and explicit failure preserved")

print("DAEMON SAFETY TESTS PASSED")
