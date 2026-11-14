#!/usr/bin/env python3
"""Unit tests for daemon/suspend-resume-hook.sh.

Tests pre-suspend state snapshotting, post-resume network wait, hung mount
detection, lazy unmount triggers, and logging.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = ROOT / "daemon" / "suspend-resume-hook.sh"

assert HOOK_SCRIPT.is_file(), f"Missing hook script: {HOOK_SCRIPT}"

with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_path = Path(tmp_dir)
    state_dir = tmp_path / "deckstream"
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    # Never inspect or unmount this computer's real mounts or wait for its network.
    for name, body in {
        "mount": "exit 0",
        "ip": "echo default",
        "timeout": 'if [ "${MOCK_HUNG:-0}" = 1 ]; then exit 1; fi; exit 0',
        "fusermount": 'printf "%s\\n" "$*" >> "$MOCK_UNMOUNTS"',
    }.items():
        tool = mock_bin / name
        tool.write_text("#!/usr/bin/env bash\n" + body + "\n")
        tool.chmod(0o755)
    env = {
        **os.environ,
        "DECKSTREAM_STATE_DIR": str(state_dir),
        "PATH": str(mock_bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "MOCK_UNMOUNTS": str(tmp_path / "unmounts"),
    }

    print("[*] 1. Testing 'pre' suspend hook...")
    res_pre = subprocess.run([str(HOOK_SCRIPT), "pre"], env=env, capture_output=True, text=True)
    assert res_pre.returncode == 0, f"Pre-suspend hook failed: {res_pre.stderr}"

    suspend_state = state_dir / "suspend_state.json"
    assert suspend_state.is_file(), "suspend_state.json must be written on pre-suspend"
    with open(suspend_state) as f:
        state_data = json.load(f)
    assert "suspended_at" in state_data, "Must record suspended_at timestamp"
    assert "active_mounts" in state_data, "Must record active_mounts array"
    print("[✓] Pre-suspend correctly snapshot state!")

    print("\n[*] 2. Testing 'post' resume hook with simulated mount...")
    # Seed dummy active mount
    mock_mount = tmp_path / "mock_mount"
    mock_mount.mkdir()
    state_data["active_mounts"] = [str(mock_mount)]
    with open(suspend_state, "w") as f:
        json.dump(state_data, f)

    res_post = subprocess.run([str(HOOK_SCRIPT), "post"], env=env, capture_output=True, text=True)
    assert res_post.returncode == 0, f"Post-resume hook failed: {res_post.stderr}"
    assert not suspend_state.exists(), "suspend_state.json should be cleaned up post-resume"

    resume_trigger = state_dir / "resume_trigger"
    assert resume_trigger.is_file(), "resume_trigger must be touched on resume"

    log_file = state_dir / "suspend-resume.log"
    assert log_file.is_file(), "suspend-resume.log must be created"
    log_content = log_file.read_text()
    assert "[resume] Steam Deck resumed" in log_content
    assert "[resume] Recovery complete" in log_content
    assert not (tmp_path / "unmounts").exists(), "responsive mount must stay mounted"
    suspend_state.write_text(json.dumps({"active_mounts": [str(mock_mount)]}))
    hung = subprocess.run([str(HOOK_SCRIPT), "post"], env={**env, "MOCK_HUNG": "1"}, capture_output=True, text=True, timeout=10)
    assert hung.returncode == 0, hung.stderr
    assert (tmp_path / "unmounts").read_text().strip() == "-u -z " + str(mock_mount)
    print("[✓] Post-resume preserves responsive mounts and lazily unmounts a simulated hung mount!")

    print("\n[*] 3. Testing invalid argument rejection...")
    res_bad = subprocess.run([str(HOOK_SCRIPT), "invalid_action"], env=env, capture_output=True, text=True)
    assert res_bad.returncode != 0, "Unknown action must exit non-zero"
    print("[✓] Invalid actions properly rejected!")

# 4. what the hook must NOT do, and that the daemon really triggers it (it used to be triggered by nothing)
hook_src = HOOK_SCRIPT.read_text()
code_lines = [l for l in hook_src.splitlines() if not l.strip().startswith("#")]
assert not any("ss -K" in l for l in code_lines), "ss -K needs root and would cut every HTTPS connection on the Deck"
assert not any("8787" in l or "curl" in l for l in code_lines), "the hook must not call the dev-server port or any URL"
print("[✓] The hook no longer kills sockets or calls a dev-server URL!")

import importlib.util
with tempfile.TemporaryDirectory() as module_home, patch.dict(os.environ, {"HOME": module_home}):
    spec = importlib.util.spec_from_file_location("deckstream_daemon", ROOT / "daemon" / "deckstream-daemon.py")
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)
    assert daemon.resumed_from_sleep(1000.0, 1010.0, 10) is False, "a normal sleep is not a resume"
    assert daemon.resumed_from_sleep(1000.0, 1065.0, 10) is False, "a slightly long sleep (within the 60 s slack) is not a resume"
    assert daemon.resumed_from_sleep(1000.0, 1100.0, 10) is True, "40 s past the allowance is a resume"
    assert daemon.resumed_from_sleep(1000.0, 5000.0, 10) is True, "a sleep that took ~67 minutes of wall time is a resume"
    dsrc = (ROOT / "daemon" / "deckstream-daemon.py").read_text()
    assert 'cfg.get("suspend_hook") and resumed_from_sleep(' in dsrc and "run_resume_hook()" in dsrc, "the daemon loop must call the hook on a resume, behind the config flag"
    assert 'suspend-resume-hook.sh' in (ROOT / "scripts" / "make_manifest.py").read_text(), "the hook must be shipped to the Deck"
    assert daemon.run_resume_hook(timeout_s=1) is False, "missing hook is harmless in an isolated home"
print("[✓] The daemon detects a wall-clock jump across a sleep and runs the hook (off unless suspend_hook is set)!")

print("\n🎉 SUSPEND/RESUME HOOK TESTS PASSED!\n")
