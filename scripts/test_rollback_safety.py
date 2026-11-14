#!/usr/bin/env python3
"""Unit tests for safe rollback and game_running guards in installer & rollback scripts."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "daemon" / "install.sh"
ROLLBACK_SH = ROOT / "daemon" / "rollback.sh"

assert INSTALL_SH.is_file()
assert ROLLBACK_SH.is_file()

with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_path = Path(tmp_dir)
    install_dir = tmp_path / "install"
    state_dir = tmp_path / "state"
    install_dir.mkdir()
    state_dir.mkdir()

    mock_processes = tmp_path / "processes"
    mock_processes.mkdir()
    pgrep = mock_processes / "pgrep"
    pgrep.write_text("#!/usr/bin/env bash\nexit 1\n")
    pgrep.chmod(0o755)
    env = {
        **os.environ,
        "DECKSTREAM_DIR": str(install_dir),
        "DECKSTREAM_STATE_DIR": str(state_dir),
        "PATH": str(mock_processes) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
    }

    print("[*] 1. Validating game_running check in rollback.sh...")
    # Create a dummy backup
    backup_root = install_dir / "backup"
    backup_1 = backup_root / "20261014-120000"
    backup_1.mkdir(parents=True)
    (backup_1 / "test_file.sh").write_text("#!/usr/bin/env bash\necho v1\n")
    (install_dir / "test_file.sh").write_text("#!/usr/bin/env bash\necho v2\n")

    # Simulate an active game: the runner's REAL lock (runtime dir, one folder per game with the live runner's pid inside)
    runtime_dir = tmp_path / "runtime"
    locks_dir = runtime_dir / "deckstream_locks"
    lock_folder = locks_dir / "runner_cyberpunk.lock"
    lock_folder.mkdir(parents=True)
    active_lock = lock_folder / "pid"
    active_lock.write_text(str(os.getpid()))          # this test process is alive
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)

    # Run rollback.sh - should detect game_running and defer restart
    res_rb = subprocess.run(["bash", str(ROLLBACK_SH)], env=env, capture_output=True, text=True)
    assert res_rb.returncode == 0, f"Rollback failed: {res_rb.stderr}"
    assert "A game or job is running" in res_rb.stdout, "Rollback must detect active game lock and defer restart"
    assert (state_dir / "restart-pending").is_file(), "restart-pending must be created"
    assert (install_dir / "test_file.sh").read_text() == "#!/usr/bin/env bash\necho v1\n", "Target file must be restored"
    print("[✓] Rollback correctly deferred restart while game is active!")

    print("\n[*] 1b. A STALE lock (dead pid) must not defer the update forever...")
    active_lock.write_text("999999")
    (state_dir / "restart-pending").unlink()
    mock_bin0 = tmp_path / "bin0"
    mock_bin0.mkdir()
    (mock_bin0 / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (mock_bin0 / "systemctl").chmod(0o755)
    env_stale = {**env, "PATH": f"{mock_bin0}:{env['PATH']}"}
    (backup_1 / "test_file.sh").write_text("#!/usr/bin/env bash\necho v1\n")
    res_st = subprocess.run(["bash", str(ROLLBACK_SH)], env=env_stale, capture_output=True, text=True)
    assert "A game or job is running" not in res_st.stdout, "a dead-pid lock must not count as a running game: " + res_st.stdout
    print("[✓] A stale lock does not block the update!")

    print("\n[*] 2. Validating rollback when game is not running...")
    active_lock.unlink()
    (state_dir / "restart-pending").unlink(missing_ok=True)
    # Mock systemctl to succeed
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    (mock_bin / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (mock_bin / "systemctl").chmod(0o755)
    env["PATH"] = f"{mock_bin}:{env['PATH']}"

    res_rb2 = subprocess.run(["bash", str(ROLLBACK_SH)], env=env, capture_output=True, text=True)
    assert res_rb2.returncode == 0, f"Rollback idle failed: {res_rb2.stderr}"
    assert "Daemon restarted." in res_rb2.stdout or "Restored the scripts" in res_rb2.stdout
    assert not (state_dir / "restart-pending").exists(), "restart-pending should not be set when idle"
    print("[✓] Rollback executed cleanly when idle!")

print("\n🎉 SAFE ROLLBACK & UPDATER GUARD TESTS PASSED!\n")
