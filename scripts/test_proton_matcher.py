#!/usr/bin/env python3
"""Test suite for daemon/proton_matcher.py (Proton Crash Signature Matcher & Log Rotator)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))
import proton_matcher as pm


class TestProtonMatcher(unittest.TestCase):
    def test_dxvk_signature(self):
        log = """
        info:  Game: Cyberpunk2077.exe
        warn:  OpenVR: Failed to locate module
        err:   DXVK: Failed to initialize Vulkan loader.
        wine:  Unhandled page fault.
        """
        match = pm.match_signature(log)
        self.assertIsNotNone(match)
        self.assertIn("DXVK", match["category"])
        self.assertIn("Vulkan", match["cause"])

    def test_vkd3d_signature(self):
        log = "err:   vkd3d_create_instance: Failed to find vkd3d-proton d3d12core interfaces."
        match = pm.match_signature(log)
        self.assertIsNotNone(match)
        self.assertIn("VKD3D", match["category"])

    def test_oom_signature(self):
        log = "wine: Unhandled exception 0xc0000017 in thread 42"
        match = pm.match_signature(log)
        self.assertIsNotNone(match)
        self.assertIn("OOM", match["category"])

    def test_access_violation_signature(self):
        log = "Unhandled exception: c0000005 in 64-bit code (0x0000000140001234)"
        match = pm.match_signature(log)
        self.assertIsNotNone(match)
        self.assertIn("Access Violation", match["category"])

    def test_anticheat_signature(self):
        log1 = "LoadLibrary failed for EasyAntiCheat_x64.so: not found"
        match1 = pm.match_signature(log1)
        self.assertIsNotNone(match1)
        self.assertIn("Anti-Cheat", match1["category"])

        log2 = "Failed to start BattlEye Service (1053)"
        match2 = pm.match_signature(log2)
        self.assertIsNotNone(match2)
        self.assertIn("Anti-Cheat", match2["category"])

    def test_healthy_log_not_flagged_as_anticheat(self):
        log = """
        info: Game: EldenRing.exe
        info: Checking EasyAntiCheat directory /prefix/drive_c/Program Files/EasyAntiCheat
        info: Loaded libvulkan.so.1 successfully
        info: Presentation mode: VK_PRESENT_MODE_FIFO_KHR
        """
        match = pm.match_signature(log)
        self.assertIsNone(match)

    def test_log_rotation_and_cap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "steam-1091500.log")
            # Write 2 MB of repetitive data
            chunk = "Line of wine debug info with timestamp\n" * 1000
            with open(log_file, "w") as f:
                for _ in range(50):
                    f.write(chunk)

            self.assertGreater(os.path.getsize(log_file), 1_000_000)
            pm.rotate_and_cap_log(log_file, max_bytes=500_000, keep=3)

            # Active log is capped or rotated
            self.assertTrue(os.path.exists(log_file) or os.path.exists(log_file + ".1"))

    def test_rotated_file_is_trimmed_not_just_renamed(self):
        # The old code moved a 1 GB log to .1 and left it 1 GB: the cap never reduced disk use.
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "steam-1.log")
            with open(log_file, "wb") as f:
                f.write(b"x" * 2_000_000)
            pm.rotate_and_cap_log(log_file, max_bytes=500_000, keep=3)
            self.assertLessEqual(os.path.getsize(log_file + ".1"), 500_000)
            self.assertLess(os.path.getsize(log_file), 1000)

    def test_cap_logs_in_only_touches_big_proton_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            big, small, other = (os.path.join(tmpdir, n) for n in ("steam-1.log", "steam-2.log", "stream_x.log"))
            for path, n in ((big, 600_000), (small, 100), (other, 600_000)):
                with open(path, "wb") as f:
                    f.write(b"y" * n)
            self.assertEqual(pm.cap_logs_in(tmpdir, max_bytes=500_000), 1)
            self.assertEqual(os.path.getsize(small), 100)
            self.assertEqual(os.path.getsize(other), 600_000)
            self.assertTrue(os.path.exists(big + ".1"))

    def test_steam_api_signature_and_keys(self):
        m = pm.match_signature("info: loading\nerr: SteamAPI_Init() failed; no steam client running\n")
        self.assertEqual(m["key"], "steam_api")
        for sig in pm.SIGNATURES:
            self.assertIn(sig["key"], pm.EXIT_REASONS)
        self.assertEqual(len(set(pm.EXIT_REASONS)), len(pm.EXIT_REASONS))

    def test_key_flag_prints_enum_or_none(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "steam-1.log"); open(log, "w").write("SteamAPI_Init failed\n")
            out = subprocess.run([sys.executable, pm.__file__, log, "--key"], capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, "steam_api")
            open(log, "w").write("all fine\n")
            out = subprocess.run([sys.executable, pm.__file__, log, "--key"], capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, "none")


if __name__ == "__main__":
    unittest.main()
