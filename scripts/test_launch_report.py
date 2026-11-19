#!/usr/bin/env python3
"""Unit tests for parse and summarize in daemon/launch_report.py."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))
import launch_report as lr  # noqa: E402


class TestLaunchReport(unittest.TestCase):
    def test_parse_last_value_wins_and_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory(prefix="ds_lr_unit_") as tmpdir:
            tmp = Path(tmpdir)
            self.assertEqual(lr.parse(tmp / "nonexistent.txt"), {})

            f = tmp / "game.1000.42.txt"
            f.write_text(
                "slug = hades_ii \n"
                "play=1000\n"
                "not_a_pair_line\n"
                "=empty_key\n"
                "cache_mb_last=120\n"
                "last_seen=1030\n"
                "cache_mb_last=340\n"
                "last_seen=1060\n",
                encoding="utf-8",
            )
            got = lr.parse(f)
            self.assertEqual(
                got,
                {
                    "slug": "hades_ii",
                    "play": "1000",
                    "cache_mb_last": "340",
                    "last_seen": "1060",
                },
            )

    def test_summarize_modes_milestones_and_exit_reasons(self):
        # Gaming mode with all milestones populated
        full = lr.summarize({
            "slug": "cyberpunk",
            "play": "1000",
            "desktop": "gamescope-wayland",
            "fill_start": "1002",
            "fill_end": "1012",
            "launched": "1015",
            "last_seen": "1315",
            "cache_mb_before_fill": "100",
            "cache_mb_at_launch": "600",
            "cache_mb_last": "850",
            "fill_rc": "0",
            "exit_seconds": "12",
            "exit_reason": "dxvk",
        })
        self.assertEqual(
            full,
            {
                "slug": "cyberpunk",
                "play": 1000,
                "mode": "gaming",
                "fill_seconds": 10,
                "play_to_launch_seconds": 15,
                "session_seconds": 300,
                "fill_mb": 500,
                "downloaded_while_playing_mb": 250,
                "fill_rc": 0,
                "exit_seconds": 12,
                "exit_reason": "dxvk",
            },
        )

        # Desktop mode with missing milestones and unrecognised exit_reason omitted
        desktop = lr.summarize({
            "slug": "sifu",
            "play": "2000",
            "desktop": "KDE",
            "fill_start": "2020",
            "fill_end": "2010",  # inverted timestamps omitted
            "exit_reason": "arbitrary free text not in enum",
        })
        self.assertEqual(desktop, {"slug": "sifu", "play": 2000, "mode": "desktop"})

        # Unknown mode when desktop is "unknown" or absent
        unknown = lr.summarize({"slug": "celeste", "play": "3000", "desktop": "unknown"})
        self.assertEqual(unknown, {"slug": "celeste", "play": 3000, "mode": "unknown"})
        implicit = lr.summarize({"slug": "celeste", "play": "3000"})
        self.assertEqual(implicit, {"slug": "celeste", "play": 3000, "mode": "unknown"})


if __name__ == "__main__":
    unittest.main()
