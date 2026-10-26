#!/usr/bin/env python3
"""Demand-Curve Launch Gate, Safety Margin Verification (1.5x D(t)), and Replay Planner.

Mathematically proves whether a streamed game's prefetch fill outruns its execution
demand curve D(t) with a 1.5x safety factor before authorizing early launch.
Tracks runtime on-demand reads, promotes cache misses, and enforces a miss budget.
"""
import argparse
import json
import os
import sys
import time

MB = 1_000_000


def compute_demand_curve(events):
    """Compute cumulative demand points D(t) from access events [(t, file, offset, size)].

    Returns sorted list of dicts:
      [{"t": float, "bytes_demanded": int, "reads_count": int, "file": str, "offset": int, "size": int}]
    """
    sorted_ev = sorted(events, key=lambda e: (e[0], e[1], e[2]))
    cum_bytes = 0
    curve = []
    for count, (t, f, off, size) in enumerate(sorted_ev, 1):
        cum_bytes += size
        curve.append({
            "t": float(t),
            "bytes_demanded": cum_bytes,
            "reads_count": count,
            "file": f,
            "offset": off,
            "size": size,
        })
    return curve


def evaluate_launch_gate(demand_curve, fill_rate_bytes_per_s, current_fill_bytes, safety_factor=1.5):
    """Evaluate whether current fill and projected fill rate stay ahead of 1.5x D(t).

    Condition for safety at all times t:
      Current_Fill + Fill_Rate * t >= safety_factor * D(t)

    Returns:
      {
        "safe": bool,
        "current_fill_bytes": int,
        "fill_rate_bytes_per_s": float,
        "safety_factor": float,
        "min_headroom_bytes": int,
        "max_deficit_bytes": int,
        "required_head_start_bytes": int,
        "tightest_point": {"t": float, "demanded": int, "required_fill": int} or None
      }
    """
    if not demand_curve:
        return {
            "safe": True,
            "current_fill_bytes": current_fill_bytes,
            "fill_rate_bytes_per_s": fill_rate_bytes_per_s,
            "safety_factor": safety_factor,
            "min_headroom_bytes": current_fill_bytes,
            "max_deficit_bytes": 0,
            "required_head_start_bytes": 0,
            "tightest_point": None,
        }

    max_deficit = 0
    min_headroom = float("inf")
    tightest = None
    required_head_start = 0

    for pt in demand_curve:
        t = pt["t"]
        demanded = pt["bytes_demanded"]
        target = demanded * safety_factor
        projected_fill = current_fill_bytes + (fill_rate_bytes_per_s * t)

        margin = projected_fill - target
        if margin < min_headroom:
            min_headroom = margin
            tightest = {"t": t, "demanded": demanded, "target_with_margin": int(target)}

        # What initial head-start would be required at t=0 so this point t is covered?
        # H + fill_rate * t >= target  =>  H >= target - fill_rate * t
        needed_initial = max(0, int(target - fill_rate_bytes_per_s * t))
        if needed_initial > required_head_start:
            required_head_start = needed_initial

        if projected_fill < target:
            deficit = int(target - projected_fill)
            if deficit > max_deficit:
                max_deficit = deficit

    is_safe = (max_deficit == 0)
    return {
        "safe": is_safe,
        "current_fill_bytes": current_fill_bytes,
        "fill_rate_bytes_per_s": fill_rate_bytes_per_s,
        "safety_factor": safety_factor,
        "min_headroom_bytes": int(max(0, min_headroom)) if is_safe else 0,
        "max_deficit_bytes": max_deficit,
        "required_head_start_bytes": required_head_start,
        "tightest_point": tightest,
    }


class MissTracker:
    """Tracks on-demand cache misses, promotes missing ranges to the fill queue,

    and enforces a miss budget (e.g. max 10 misses before failing safe).
    """

    def __init__(self, budget=10):
        self.budget = budget
        self.miss_count = 0
        self.hit_count = 0
        self.miss_log = []
        self.promoted_ranges = []

    def record_read(self, file, offset, size, in_cache, t=None):
        if t is None:
            t = time.time()
        if in_cache:
            self.hit_count += 1
            return {"promoted": False, "miss_count": self.miss_count}

        self.miss_count += 1
        record = {"file": file, "offset": offset, "size": size, "t": t}
        self.miss_log.append(record)
        self.promoted_ranges.append(record)
        return {
            "promoted": True,
            "miss_count": self.miss_count,
            "budget_exceeded": self.is_budget_exceeded(),
        }

    def is_budget_exceeded(self):
        return self.miss_count > self.budget

    def get_status(self):
        state = "exceeded_budget" if self.is_budget_exceeded() else ("warning" if self.miss_count > 0 else "optimal")
        return {
            "state": state,
            "miss_count": self.miss_count,
            "hit_count": self.hit_count,
            "budget": self.budget,
            "recent_misses": self.miss_log[-5:],
        }


def calculate_optimal_coalesce_gap(bandwidth_bytes_per_s=21 * 1_000_000, ttfb_s=0.8, cap_mb=4.0):
    """Calculate the mathematically optimal byte gap threshold for range coalescing.

    Cost(Two Requests) = 2*TTFB + (size1 + size2)/BW
    Cost(Merged) = TTFB + (size1 + G + size2)/BW
    A single merged request is faster whenever G <= BW * TTFB.
    """
    theoretical_optimal = bandwidth_bytes_per_s * ttfb_s
    cap_bytes = int(cap_mb * 1_000_000)
    return int(min(theoretical_optimal, cap_bytes))


def coalesce_demand_ranges(ranges, max_gap_bytes=None):
    """Coalesce nearby ranges separated by small gaps (<= max_gap_bytes) to cut network round trips.

    Takes a list of range dicts: [{"f": str, "o": int, "n": int, "t": float}].
    Returns coalesced list ordered by first access time.
    """
    if max_gap_bytes is None:
        max_gap_bytes = calculate_optimal_coalesce_gap()
    by_file = {}
    for r in ranges:
        by_file.setdefault(r["f"], []).append(r)

    coalesced = []
    for f, file_ranges in by_file.items():
        # Sort by offset
        file_ranges.sort(key=lambda r: r["o"])
        cur = None
        for r in file_ranges:
            if cur is None:
                cur = {"f": f, "o": r["o"], "n": r["n"], "t": r.get("t", 0.0)}
                continue
            cur_end = cur["o"] + cur["n"]
            # If overlapping or gap <= max_gap_bytes, merge
            if r["o"] <= cur_end + max_gap_bytes:
                new_end = max(cur_end, r["o"] + r["n"])
                cur["n"] = new_end - cur["o"]
                cur["t"] = min(cur["t"], r.get("t", 0.0))
            else:
                coalesced.append(cur)
                cur = {"f": f, "o": r["o"], "n": r["n"], "t": r.get("t", 0.0)}
        if cur is not None:
            coalesced.append(cur)

    # Re-order by earliest time seen
    coalesced.sort(key=lambda r: (r.get("t", 0.0), r["f"], r["o"]))
    return coalesced


def main():
    parser = argparse.ArgumentParser(description="Demand-Curve Launch Gate")
    parser.add_argument("--profile", required=True, help="Path to profile JSON")
    parser.add_argument("--line-mbps", type=float, default=21.0, help="Measured fill rate in MB/s")
    parser.add_argument("--fill-bytes", type=int, default=0, help="Current prefilled bytes")
    parser.add_argument("--safety-factor", type=float, default=1.5, help="Safety margin factor (default 1.5)")
    args = parser.parse_args()

    with open(args.profile) as f:
        data = json.load(f)

    ranges = data.get("demand") or data.get("ranges") or []
    events = [(r.get("t", 0.0), r.get("f", "file"), r.get("o", 0), r.get("n", 0)) for r in ranges]
    curve = compute_demand_curve(events)
    res = evaluate_launch_gate(
        curve,
        fill_rate_bytes_per_s=args.line_mbps * MB,
        current_fill_bytes=args.fill_bytes,
        safety_factor=args.safety_factor
    )
    print(json.dumps(res, indent=2))
    return 0 if res["safe"] else 1


if __name__ == "__main__":
    sys.exit(main())
