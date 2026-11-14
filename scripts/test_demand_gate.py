#!/usr/bin/env python3
"""Test suite for Demand-Curve Launch Gate, Safety Factor Margin (1.5x), and Miss Budgeting."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))
import demand_gate as dg

MB = 1_000_000


class TestDemandGate(unittest.TestCase):
    def test_compute_demand_curve(self):
        events = [
            (0.0, "main.exe", 0, 5 * MB),
            (2.0, "data.pak", 0, 10 * MB),
            (5.0, "data.pak", 20 * MB, 15 * MB),
            (10.0, "sound.pak", 0, 20 * MB),
        ]
        curve = dg.compute_demand_curve(events)
        self.assertEqual(len(curve), 4)
        self.assertEqual(curve[0]["t"], 0.0)
        self.assertEqual(curve[0]["bytes_demanded"], 5 * MB)
        self.assertEqual(curve[1]["t"], 2.0)
        self.assertEqual(curve[1]["bytes_demanded"], 15 * MB)
        self.assertEqual(curve[3]["t"], 10.0)
        self.assertEqual(curve[3]["bytes_demanded"], 50 * MB)
        self.assertEqual(curve[3]["reads_count"], 4)

    def test_launch_gate_insufficient_fill(self):
        # 50 MB total demand over 10 seconds.
        # At fill_rate = 2 MB/s, in 10 seconds it fills 20 MB.
        # If current_fill_bytes = 0, F(0) = 0 < 1.5 * 5 MB = 7.5 MB -> NOT safe!
        events = [
            (0.0, "main.exe", 0, 5 * MB),
            (5.0, "data.pak", 0, 20 * MB),
            (10.0, "data.pak", 50 * MB, 25 * MB),
        ]
        curve = dg.compute_demand_curve(events)
        eval_result = dg.evaluate_launch_gate(
            curve,
            fill_rate_bytes_per_s=2 * MB,
            current_fill_bytes=0,
            safety_factor=1.5
        )
        self.assertFalse(eval_result["safe"])
        self.assertGreater(eval_result["required_head_start_bytes"], 0)
        self.assertGreater(eval_result["max_deficit_bytes"], 0)

    def test_launch_gate_sufficient_fill(self):
        events = [
            (0.0, "main.exe", 0, 2 * MB),
            (5.0, "data.pak", 0, 10 * MB),
            (10.0, "data.pak", 20 * MB, 10 * MB),
        ]
        curve = dg.compute_demand_curve(events)
        # Required at t=0: 1.5 * 2 = 3 MB
        # Required at t=5: 1.5 * 12 = 18 MB; fill provides 15 MB + 20*5 = 115 MB
        eval_result = dg.evaluate_launch_gate(
            curve,
            fill_rate_bytes_per_s=20 * MB,  # 20 MB/s line
            current_fill_bytes=15 * MB,     # 15 MB prefilled
            safety_factor=1.5
        )
        self.assertTrue(eval_result["safe"])
        self.assertGreater(eval_result["min_headroom_bytes"], 0)
        self.assertEqual(eval_result["max_deficit_bytes"], 0)

    def test_miss_tracker_and_budget(self):
        tracker = dg.MissTracker(budget=3)
        self.assertFalse(tracker.is_budget_exceeded())

        # Miss 1
        res1 = tracker.record_read(file="engine.dll", offset=0, size=1024, in_cache=False, t=1.0)
        self.assertTrue(res1["promoted"])
        self.assertEqual(tracker.miss_count, 1)
        self.assertFalse(tracker.is_budget_exceeded())

        # Hit
        res2 = tracker.record_read(file="engine.dll", offset=0, size=1024, in_cache=True, t=1.2)
        self.assertFalse(res2["promoted"])
        self.assertEqual(tracker.miss_count, 1)

        # Miss 2
        tracker.record_read(file="data.pak", offset=1000, size=2048, in_cache=False, t=2.0)
        self.assertEqual(tracker.miss_count, 2)
        self.assertFalse(tracker.is_budget_exceeded())

        # Miss 3
        tracker.record_read(file="data.pak", offset=4000, size=2048, in_cache=False, t=3.0)
        self.assertEqual(tracker.miss_count, 3)
        self.assertFalse(tracker.is_budget_exceeded())

        # Miss 4 -> exceeds budget of 3
        tracker.record_read(file="data.pak", offset=8000, size=2048, in_cache=False, t=4.0)
        self.assertEqual(tracker.miss_count, 4)
        self.assertTrue(tracker.is_budget_exceeded())
        self.assertEqual(tracker.get_status()["state"], "exceeded_budget")

    def test_gap_coalescing(self):
        ranges = [
            {"f": "pak0.pak", "o": 0, "n": 100 * 1024, "t": 0.0},
            {"f": "pak0.pak", "o": 150 * 1024, "n": 100 * 1024, "t": 1.0},  # gap 50 KB <= 256 KB
            {"f": "pak0.pak", "o": 10 * MB, "n": 100 * 1024, "t": 2.0},     # gap ~9.75 MB > 256 KB
            {"f": "other.bin", "o": 0, "n": 50 * 1024, "t": 0.5},
        ]
        # Coalesce with 256 KB gap
        coalesced = dg.coalesce_demand_ranges(ranges, max_gap_bytes=256 * 1024)
        pak_ranges = [r for r in coalesced if r["f"] == "pak0.pak"]
        self.assertEqual(len(pak_ranges), 2)
        # First range coalesced from 0 to 250 KB
        self.assertEqual(pak_ranges[0]["o"], 0)
        self.assertEqual(pak_ranges[0]["n"], 250 * 1024)
        self.assertEqual(pak_ranges[1]["o"], 10 * MB)


if __name__ == "__main__":
    unittest.main()
