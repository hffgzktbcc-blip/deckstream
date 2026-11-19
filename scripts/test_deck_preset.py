#!/usr/bin/env python3
"""Unit tests for unreal_project, wine_root, and apply in daemon/deck_preset.py."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))
import deck_preset as dp  # noqa: E402


class TestDeckPreset(unittest.TestCase):
    def test_unreal_project_detection(self):
        with tempfile.TemporaryDirectory(prefix="ds_preset_") as tmpdir:
            tmp = Path(tmpdir)
            game = tmp / "Games" / "Sifu"
            (game / "Engine").mkdir(parents=True)
            shipping = game / "Sifu" / "Binaries" / "Win64" / "Sifu-Win64-Shipping.exe"
            shipping.parent.mkdir(parents=True)
            shipping.write_text("")
            (game / "Sifu" / "Content").mkdir(parents=True)
            launcher = game / "Sifu.exe"
            launcher.write_text("")

            self.assertEqual(dp.unreal_project(shipping), "Sifu")
            self.assertEqual(dp.unreal_project(launcher), "Sifu")

            # Without Engine/ directory it is not recognized as Unreal
            not_ue = tmp / "Games" / "Custom"
            not_ue_bin = not_ue / "Custom" / "Binaries" / "Win64" / "Custom.exe"
            not_ue_bin.parent.mkdir(parents=True)
            not_ue_bin.write_text("")
            self.assertIsNone(dp.unreal_project(not_ue_bin))
            self.assertIsNone(dp.unreal_project(not_ue / "Custom.exe"))

    def test_wine_root_layouts(self):
        with tempfile.TemporaryDirectory(prefix="ds_wineroot_") as tmpdir:
            tmp = Path(tmpdir)
            # Fresh prefix defaults to <prefix>/pfx for proton and <prefix> for umu
            fresh = tmp / "fresh"
            self.assertEqual(dp.wine_root(fresh, "proton"), fresh / "pfx")
            self.assertEqual(dp.wine_root(fresh, "umu"), fresh)

            # Existing drive_c directly in prefix
            direct = tmp / "direct"
            (direct / "drive_c").mkdir(parents=True)
            self.assertEqual(dp.wine_root(direct, "proton"), direct)

            # Existing pfx subfolder wins and is resolved
            proton_pfx = tmp / "proton_pfx"
            (proton_pfx / "pfx" / "drive_c").mkdir(parents=True)
            self.assertEqual(dp.wine_root(proton_pfx, "umu"), (proton_pfx / "pfx").resolve())

    def test_apply_writes_once_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory(prefix="ds_apply_") as tmpdir:
            tmp = Path(tmpdir)
            game = tmp / "Stray"
            (game / "Engine").mkdir(parents=True)
            (game / "Hk_project" / "Content").mkdir(parents=True)
            exe = game / "Hk_project" / "Binaries" / "Win64" / "Stray-Win64-Shipping.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("")

            other = tmp / "Other" / "Other.exe"
            other.parent.mkdir(parents=True)
            other.write_text("")
            self.assertIn("not an Unreal game", dp.apply(other, tmp / "pfx_other"))

            prefix = tmp / "pfx_stray"
            msg = dp.apply(exe, prefix, "proton")
            self.assertIn("wrote low settings for Hk_project", msg)
            cfg_base = prefix / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Hk_project" / "Saved" / "Config"
            win_ini = cfg_base / "Windows" / "GameUserSettings.ini"
            noed_ini = cfg_base / "WindowsNoEditor" / "GameUserSettings.ini"
            self.assertTrue(win_ini.is_file() and noed_ini.is_file())
            raw = win_ini.read_bytes().decode("utf-8")
            self.assertIn("sg.ShadowQuality=1\r\n", raw)
            self.assertIn("FrameRateLimit=30.000000\r\n", raw)

            # Existing user file must never be overwritten
            win_ini.write_text("custom=1\n", encoding="utf-8")
            msg2 = dp.apply(exe, prefix, "proton")
            self.assertIn("already has its settings file", msg2)
            self.assertEqual(win_ini.read_text(encoding="utf-8"), "custom=1\n")

            # CLI entry point
            self.assertEqual(dp.main(["deck_preset.py"]), 2)
            self.assertEqual(dp.main(["deck_preset.py", str(exe), str(prefix)]), 0)


if __name__ == "__main__":
    unittest.main()
