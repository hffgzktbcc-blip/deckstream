#!/usr/bin/env python3
"""Unit tests for the pure byte-range helpers (merge, subtract, coalesce) in daemon/startup_profile.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))
import startup_profile as sp  # noqa: E402

MB = sp.MB1


class TestStartupProfileRanges(unittest.TestCase):
    def test_merge_empty_disjoint_touching_and_overlapping(self):
        self.assertEqual(sp.merge([]), [])
        self.assertEqual(sp.merge([(100, 20), (0, 10)]), [(0, 10), (100, 20)])
        # Touching ranges merge into one contiguous extent
        self.assertEqual(sp.merge([(0, 10), (10, 15), (25, 5)]), [(0, 30)])
        # Overlapping and contained ranges collapse cleanly
        self.assertEqual(sp.merge([(10, 20), (0, 15), (12, 5), (40, 10)]), [(0, 30), (40, 10)])

    def test_subtract_uncovered_trimmed_and_split_ranges(self):
        self.assertEqual(sp.subtract([], [(0, 10)]), [])
        self.assertEqual(sp.subtract([(10, 20)], []), [(10, 20)])
        # Complete cover returns empty
        self.assertEqual(sp.subtract([(10, 20)], [(0, 50)]), [])
        # Left and right trims
        self.assertEqual(sp.subtract([(10, 20)], [(0, 15)]), [(15, 15)])
        self.assertEqual(sp.subtract([(10, 20)], [(25, 15)]), [(10, 15)])
        # Middle hole punches one range into two, and multiple holes into three
        self.assertEqual(sp.subtract([(0, 100)], [(20, 30), (60, 10)]), [(0, 20), (50, 10), (70, 30)])
        # Unsorted overlapping inputs on both sides are normalized first
        self.assertEqual(
            sp.subtract([(50, 30), (0, 60)], [(10, 10), (15, 25)]),
            [(0, 10), (40, 40)],
        )

    def test_coalesce_merges_within_gap_splits_large_and_orders_by_time(self):
        self.assertEqual(sp.coalesce([]), [])
        ranges = [
            {"f": "a.pak", "o": 0, "n": 2 * MB, "t": 5.0},
            {"f": "a.pak", "o": 4 * MB, "n": 2 * MB, "t": 2.0},     # gap 2 MB <= 4 MB -> merged, earliest t=2.0
            {"f": "a.pak", "o": 20 * MB, "n": 1 * MB, "t": 9.0},    # gap 14 MB > 4 MB -> separate
            {"f": "b.pak", "o": 0, "n": 10 * MB, "t": 1.0},         # different file, longer than split=4 MB
        ]
        got = sp.coalesce(ranges, gap=4 * MB, split=4 * MB)
        self.assertEqual(
            got,
            [
                {"t": 1.0, "f": "b.pak", "o": 0, "n": 4 * MB},
                {"t": 1.0, "f": "b.pak", "o": 4 * MB, "n": 4 * MB},
                {"t": 1.0, "f": "b.pak", "o": 8 * MB, "n": 2 * MB},
                {"t": 2.0, "f": "a.pak", "o": 0, "n": 4 * MB},
                {"t": 2.0, "f": "a.pak", "o": 4 * MB, "n": 2 * MB},
                {"t": 9.0, "f": "a.pak", "o": 20 * MB, "n": 1 * MB},
            ],
        )


if __name__ == "__main__":
    unittest.main()
